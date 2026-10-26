"""The activity board (#446): what the agent is doing now, what it did last, the YOLO window.

``GET /api/activity`` (registered by the chat API) answers one read for the Stage board's
activity mode: the **state now** (idle / generating / tool / approval / paused / stopped),
the **recent activity** across turns (a diff after the row cursor ``after_id``, or the last
:data:`RECENT_LIMIT` rows on the first read, so a reload restores the list from
``turns.db``), the **YOLO window** (the numbers of ``/api/yolo/status``), the **totals**
since the server started and the **stall** hint (seconds since the last event of the
running turn).

Everything here is built from an allow-list, as the YOLO log line is: event types, codes,
counts, durations, timestamps and **tool names**. No argument, output, preview, payload,
file content, command string, approval reason or note text is read into an item — the
functions below never look at those keys. A value that is not a plain code is left out (or
shown as ``other`` for a tool name). Nothing here writes, calls a model or holds a lock of
the chat API.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "DIFF_LIMIT",
    "GRANT_OUTCOMES",
    "LISTED_TYPES",
    "RECENT_LIMIT",
    "STALL_SEC",
    "activity_item",
    "claim_view",
    "grant_view",
    "live_state",
    "totals_from",
    "window_view",
]

RECENT_LIMIT = 20
DIFF_LIMIT = 100
STALL_SEC = 120
_CODE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
_TOOL = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_TERMINAL = frozenset({"done", "error", "denied", "timeout"})
_FAILED = frozenset({"error", "denied", "timeout"})
# The types the board lists. model.started / model.progress / turn.summary are not rows: a
# model call is listed at its end (with its duration), the running one is the state.
LISTED_TYPES = frozenset(
    {
        "turn.started",
        "turn.completed",
        "tool_call.created",
        "tool_call.updated",
        "approval.requested",
        "approval.resolved",
        "breaker.transition",
        "model.completed",
        "progress.report",
        "progress.reply",
        "progress.auto_continue",
    }
)


def _code(value: Any) -> str | None:
    return value if type(value) is str and _CODE.fullmatch(value) else None


def _tool(value: Any) -> str:
    return value if type(value) is str and _TOOL.fullmatch(value) else "other"


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 2**53 else None


def _seconds(value: Any) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e11:
        return round(float(value), 3)
    return None


def activity_item(row_id: int, event: Mapping) -> dict[str, Any] | None:
    """One board row from one turn event, or None (not a row, or not readable).

    ``{"id", "ts", "turn", "kind", "what", ...}``: ``kind`` is ``turn`` / ``tool`` /
    ``approval`` / ``breaker`` / ``model`` / ``progress``; ``what`` a code; ``ok`` true /
    false where a result is known; ``name`` a tool name (registry names; anything else is
    ``other``); ``duration_ms``. A running tool is not a row (it is the state); its end is.
    """
    if not isinstance(event, Mapping):
        return None
    kind_of = event.get("type")
    if kind_of not in LISTED_TYPES:
        return None
    data = event.get("data")
    data = data if isinstance(data, Mapping) else {}
    turn = event.get("turn_id")
    item: dict[str, Any] = {
        "id": int(row_id),
        "ts": _seconds(event.get("ts")),
        "turn": turn[:32] if type(turn) is str and _CODE.fullmatch(turn[:32]) else None,
    }
    if kind_of == "turn.started":
        item.update(kind="turn", what="started")
        if _code(data.get("auto_continue")):
            item["auto"] = True
    elif kind_of == "turn.completed":
        stop = _code(data.get("stop"))
        item.update(kind="turn", what="completed", ok=data.get("error") is not True)
        if data.get("error") not in (None, False, True):
            item["ok"] = False
        if stop is not None:
            item["stop"] = stop
    elif kind_of in ("tool_call.created", "tool_call.updated"):
        status = data.get("status")
        # #545: one row per call, its end (``updated``). A refused call's ``created`` is
        # already ``denied``; its ``updated`` follows (Strands calls AfterToolCall for a
        # cancelled call too), so the ``created`` is not listed.
        if status not in _TERMINAL or kind_of == "tool_call.created":
            return None
        item.update(kind="tool", what=status, name=_tool(data.get("name")))
        item["ok"] = status not in _FAILED
        if status == "denied" and data.get("cancel") == "declared":
            # a declared checkpoint (ADR 0014 phase 3): cancelled on purpose, not refused
            item["what"] = "declared"
            del item["ok"]
        duration = _count(data.get("duration_ms"))
        if duration is not None:
            item["duration_ms"] = duration
    elif kind_of == "approval.requested":
        item.update(kind="approval", what="requested", name=_tool(data.get("tool_name")))
    elif kind_of == "approval.resolved":
        decision = _code(data.get("decision")) or "other"
        item.update(kind="approval", what=decision)
        item["ok"] = decision in ("approve", "edit")
    elif kind_of == "breaker.transition":
        item.update(kind="breaker", what=_code(data.get("to")) or "other")
        source = _code(data.get("from"))
        if source is not None:
            item["from"] = source
    elif kind_of == "model.completed":
        status = _code(data.get("status")) or "error"
        item.update(kind="model", what=status, ok=status == "success")
        duration = _count(data.get("duration_ms"))
        if duration is not None:
            item["duration_ms"] = duration
    elif kind_of == "progress.report":
        item.update(kind="progress", what="report")
        trigger = _code(data.get("trigger"))
        if trigger is not None:
            item["trigger"] = trigger
        if data.get("count") in ("tools", "tokens"):
            item["count"] = data["count"]
    elif kind_of == "progress.reply":
        item.update(kind="progress", what=_code(data.get("answer")) or "other")
    else:  # progress.auto_continue
        item.update(kind="progress", what="auto_continue")
    return item


def live_state(
    events: Iterable[Mapping] | None,
    *,
    pending: list[Mapping] | None = None,
    breaker: Any = None,
    window: Mapping | None = None,
    latched: bool = False,
    report: Mapping | None = None,
) -> dict[str, Any]:
    """The state now, from the running turn's events (``None``: no turn runs) and the rest.

    Order of precedence: an approval waits > a tool runs > the model generates > the turn
    runs between steps (``working``) > a progress report waits for an answer (``report``,
    ADR 0014) > stopped (breaker CRITICAL, or YOLO latched) > paused (the YOLO window is
    paused) > idle. ``since``: the server time the state began (its
    event's ``ts``), ``last_event``: the newest event of the running turn other than
    ``model.progress`` (the stall hint counts from it).
    """
    out: dict[str, Any] = {"code": "idle", "since": None, "last_event": None}
    level = _code(breaker)
    if level is not None:
        out["breaker"] = level
    tools: dict[str, dict[str, Any]] = {}
    model: dict[str, Any] | None = None
    turn_started = None
    running = events is not None
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        kind_of, ts = event.get("type"), _seconds(event.get("ts"))
        data = event.get("data")
        data = data if isinstance(data, Mapping) else {}
        if kind_of != "model.progress" and ts is not None:
            out["last_event"] = ts
        if kind_of == "turn.started":
            turn_started = ts
        elif kind_of in ("tool_call.created", "tool_call.updated"):
            use_id = data.get("tool_use_id")
            if type(use_id) is not str:
                continue
            if data.get("status") in _TERMINAL:
                tools.pop(use_id, None)
            elif kind_of == "tool_call.created":
                tools[use_id] = {"name": _tool(data.get("name")), "since": ts}
        elif kind_of == "model.started":
            model = {"since": _seconds(data.get("started_at")) or ts}
        elif kind_of == "model.completed":
            model = None
        elif kind_of == "turn.completed":
            running = False
    waiting = [p for p in pending or () if isinstance(p, Mapping)]
    if waiting:
        first = waiting[0]
        out.update(code="approval", tool=_tool(first.get("tool_name")), waiting=len(waiting))
        out["since"] = _seconds(first.get("created_at"))
    elif running and tools:
        current = list(tools.values())[-1]
        out.update(code="tool", tool=current["name"], since=current["since"], running=len(tools))
    elif running and model is not None:
        out.update(code="generating", since=model["since"])
    elif running:
        out.update(code="working", since=turn_started)
    elif isinstance(report, Mapping):
        out.update(code="report", since=_seconds(report.get("ts")))
        trigger = _code(report.get("trigger"))
        if trigger is not None:
            out["trigger"] = trigger
        if report.get("count") in ("tools", "tokens"):
            out["count"] = report["count"]
    elif level == "CRITICAL" or latched:
        out.update(code="stopped", reason="breaker_critical" if level == "CRITICAL" else "latched")
    elif isinstance(window, Mapping) and window.get("state") == "paused":
        out.update(code="paused", reason=_code(window.get("pause_reason")) or "paused")
    out["running"] = running
    return out


def window_view(status: Mapping | None) -> dict[str, Any] | None:
    """The YOLO window's numbers (from ``yolo.runtime.status()``): None without a window."""
    if not isinstance(status, Mapping):
        return None
    window = status.get("window")
    if not isinstance(window, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in ("state", "tier", "pause_reason", "reason", "effective_tier"):
        value = _code(window.get(key))
        if value is not None:
            out[key] = value
    for key in (
        "remaining_sec",
        "used",
        "limit",
        "external_used",
        "external_limit",
        "exec_used",
        "exec_limit",
        "resume_in_sec",
    ):
        value = _count(window.get(key))
        if value is not None:
            out[key] = value
    for key in ("t0", "ends_at"):
        value = _seconds(window.get(key))
        if value is not None:
            out[key] = value
    if window.get("exec") is True:
        out["exec"] = True
    reasons = window.get("pause_breaker_reasons")
    if isinstance(reasons, list):
        codes = [code for code in (_code(r) for r in reasons[:8]) if code is not None]
        if codes:
            out["pause_breaker_reasons"] = codes
    return out


# --- the goal grant (ADR 0011 stage C, W5b of #296) -----------------------------------------

GRANT_OUTCOMES = 5
_SHORT = re.compile(r"[0-9a-f]{8}")
_RUN = frozenset({"idle", "running", "yielded"})
_LIVE = frozenset({"active", "paused", "halted"})


@functools.cache
def _known() -> dict[str, frozenset[str]]:
    """The grant block's fixed code sets (W5b fix round 1): the state machine's statuses and
    reasons, the executor's end and detail codes, the scope's tool names. A value outside
    its set is ``other`` (a state, an end, a tool) or left out (a reason, a detail): the
    block is an allow-list of values, not of shapes. Imported on the first grant read only
    (stage C on)."""
    from stiracore.grants import executor, schema, state
    from stiracore.scheduler import grant_tick

    return {
        "grant_state": frozenset(state.GRANT_STATUSES),
        "goal_state": frozenset(state.GOAL_STATUSES),
        "grant_reason": frozenset().union(*state.GRANT_REASONS.values()) - {""},
        "goal_reason": frozenset().union(*state.GOAL_REASONS.values()) - {""},
        "end": frozenset(executor.END_CODES),
        "detail": frozenset(executor.DETAIL_CODES) - {""},
        "tool": frozenset(schema.TOOLS),
        # W6c: the claim evidence's codes.
        "claim_checks": frozenset(schema.CLAIM_CHECKS),
        "claim_workspace": frozenset(schema.CLAIM_WORKSPACE),
        "claim_progress": frozenset(schema.CLAIM_PROGRESS),
        "check_kind": frozenset(schema.CHECK_KINDS) | {"other"},
        "check_status": frozenset(schema.CHECK_STATUSES),
        # W6d: the heartbeat start's skip reasons (and "started" as the last outcome).
        "tick_skip": frozenset(grant_tick.SKIP_REASONS),
    }


def _member(value: Any, kind: str) -> str | None:
    return value if type(value) is str and value in _known()[kind] else None


def _codes(value: Any, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_member(v, "tool") or "other" for v in value[:limit]]


_CLAIM_SHOWN = frozenset({"awaiting_verification", "complete"})
_CLAIM_COUNTS = (
    "iteration",
    "max_iterations",
    "used_tokens",
    "budget_left",
    "ttl_left_sec",
    "waiting_sec",
    "stale_after_sec",
)


def _claim_rows(value: Any) -> list[dict[str, Any]] | None:
    """The evidence's per-check rows (kind, status, counts), or None when any row is not
    readable (the block's checks are then ``unknown``, never ``pass``)."""
    from stiracore.grants.schema import CHECK_COUNT_KEYS, CLAIM_ROWS_KEPT

    if not isinstance(value, list) or not value or len(value) > CLAIM_ROWS_KEPT:
        return None
    rows = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        status = _member(item.get("status"), "check_status")
        counts = item.get("counts")
        if status is None or not isinstance(counts, Mapping):
            return None
        kept = {key: _count(counts.get(key)) for key in CHECK_COUNT_KEYS}
        if any(v is None for v in kept.values()):
            return None
        rows.append(
            {
                "kind": _member(item.get("kind"), "check_kind") or "other",
                "status": status,
                "counts": kept,
            }
        )
    return rows


def claim_view(view: Mapping) -> dict[str, Any] | None:
    """W6c: the board's claim evidence block (an allow-list, like the rest of the grant
    block) — only while the goal awaits verification or after it was verified (the audit
    trail, read-only); None otherwise. Codes outside their sets are ``unknown``; a check
    row that cannot be read makes the checks ``unknown`` (never ``pass``)."""
    evidence = view.get("claim_evidence")
    if _member(view.get("goal_state"), "goal_state") not in _CLAIM_SHOWN:
        return None
    if not isinstance(evidence, Mapping):
        return None
    out: dict[str, Any] = {}
    at = _seconds(evidence.get("claimed_at"))
    if at is not None:
        out["claimed_at"] = at
    for key in _CLAIM_COUNTS:
        value = _count(evidence.get(key))
        if value is not None:
            out[key] = value
    checks = _member(evidence.get("checks"), "claim_checks") or "unknown"
    rows: list | None = []
    if checks in ("pass", "fail"):
        rows = _claim_rows(evidence.get("check_rows"))
        passed = rows is not None and all(r["status"] == "passed" for r in rows)
        if rows is None or (checks == "pass") != passed:
            checks, rows = "unknown", []
    out["checks"] = checks
    out["check_rows"] = rows
    out["workspace"] = _member(evidence.get("workspace"), "claim_workspace") or "unknown"
    out["progress"] = _member(evidence.get("progress"), "claim_progress") or "unknown"
    out["stale"] = evidence.get("stale") is True
    return out


def grant_tick_codes() -> dict[str, frozenset[str]]:
    from stiracore.scheduler import grant_tick

    return {"unavailable": frozenset(grant_tick.UNAVAILABLE_CODES)}


_TICK_COST_KEYS = (
    "number",
    "model_calls",
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_write",
    "wall_ms",
)


def tick_view(tick: Any, board: Any) -> dict[str, Any]:
    """W6d: the board's heartbeat-start block of a flagged grant — an allow-list: the
    starts, the last outcome (``started`` or a skip reason; else ``other``), the counts
    per known skip reason, the spacing, and the last heartbeat-started iteration's cost
    (numbers only; the cache counters only when the provider reported them)."""
    tick = tick if isinstance(tick, Mapping) else {}
    out: dict[str, Any] = {"on": True}
    for key in ("started", "spacing_sec"):
        value = _count(tick.get(key))
        if value is not None:
            out[key] = value
    last = tick.get("last")
    if last == "started" or _member(last, "tick_skip") is not None:
        out["last"] = last
    elif last not in (None, ""):
        out["last"] = "other"
    if tick.get("unavailable") in grant_tick_codes()["unavailable"]:
        out["unavailable"] = tick["unavailable"]
    skipped = tick.get("skipped")
    if isinstance(skipped, Mapping):
        kept = {}
        for reason, count in skipped.items():
            value = _count(count)
            if _member(reason, "tick_skip") is not None and value is not None:
                kept[reason] = value
        out["skipped"] = kept
    costs = board.get("tick_costs") if isinstance(board, Mapping) else None
    if isinstance(costs, list) and costs and isinstance(costs[-1], Mapping):
        cost = {}
        for key in _TICK_COST_KEYS:
            value = _count(costs[-1].get(key))
            if value is not None:
                cost[key] = value
        out["cost"] = cost
    return out


def grant_view(
    view: Mapping | None,
    board: Mapping | None = None,
    *,
    pending: Iterable[Mapping] = (),
    since: Any = None,
    tick: Mapping | None = None,
) -> dict[str, Any] | None:
    """The board's grant block, or None (no grant to show).

    ``view``: the newest grant's structured view (``GrantService.board_view``), ``board``:
    the executor's read (``GoalIterationExecutor.board``: ``iteration`` and ``outcomes``;
    None without the executor), ``pending``: the approval requests (only their
    ``grant_card`` flag is counted), ``since``: the server's start (wall seconds) — a grant
    issued before it (ended by the restart) is not shown. An allow-list like
    :func:`window_view`: codes, counts and tool names; never the goal, a path, an argument,
    a card's content or any other text.
    """
    if not isinstance(view, Mapping):
        return None
    issued = _seconds(view.get("issued_wall"))
    start = _seconds(since)
    if start is not None and (issued is None or issued < start):
        return None
    short = view.get("short")
    state = _member(view.get("state"), "grant_state") or "other"
    out: dict[str, Any] = {
        "id": short if type(short) is str and _SHORT.fullmatch(short) else None,
        "state": state,
        "live": state in _LIVE,
    }
    for key, kind in (
        ("reason", "grant_reason"),
        ("goal_state", "goal_state"),
        ("goal_reason", "goal_reason"),
    ):
        value = _member(view.get(key), kind)
        if value is not None:
            out[key] = value
    headline = view.get("headline")
    if isinstance(headline, Mapping):
        subject = headline.get("subject")
        subject = subject if subject in ("grant", "goal") else "grant"
        line = {
            "subject": subject,
            "state": _member(headline.get("state"), f"{subject}_state") or "other",
        }
        reason = _member(headline.get("reason"), f"{subject}_reason")
        if reason is not None:
            line["reason"] = reason
        out["headline"] = line
    for key in (
        "iterations",
        "max_iterations",
        "ttl_sec",
        "ttl_left_sec",
        "budget_total",
        "budget_left",
        "budget_per_tick",
    ):
        value = _count(view.get(key))
        if value is not None:
            out[key] = value
    out["awaiting_verification"] = view.get("goal_state") == "awaiting_verification"
    claim = claim_view(view)
    if claim is not None:
        out["claim"] = claim  # W6c (absent without a claim to show)
    cards = sum(1 for p in pending if isinstance(p, Mapping) and p.get("grant_card") is True)
    out["cards"] = cards
    if isinstance(board, Mapping):
        it = board.get("iteration")
        it = it if isinstance(it, Mapping) else {}
        run = it.get("state")
        if it.get("abandoned") is True:
            run = "abandoned"
        elif run not in _RUN:
            run = "other"
        iteration: dict[str, Any] = {"run": run}
        for key in ("end", "detail"):
            value = _member(it.get(key), key)
            if value is not None:
                iteration[key] = value
        for key in ("number", "tick_used"):
            value = _count(it.get(key))
            if value is not None:
                iteration[key] = value
        out["iteration"] = iteration
        out["card"] = cards > 0 or it.get("card") == "waiting"
        outcomes = board.get("outcomes")
        rows = []
        for item in outcomes[-GRANT_OUTCOMES:] if isinstance(outcomes, list) else ():
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {"what": _member(item.get("end"), "end") or "other"}
            for key in ("number", "duration_ms", "tick_used"):
                value = _count(item.get(key))
                if value is not None:
                    row[key] = value
            ts = _seconds(item.get("ts"))
            if ts is not None:
                row["ts"] = ts
            detail = _member(item.get("detail"), "detail")
            if detail:
                row["detail"] = detail
            row["tools"] = _codes(item.get("tools"), 8)
            rows.append(row)
        out["outcomes"] = rows
    else:
        out["card"] = cards > 0
    if view.get("heartbeat_start") is True and tick is not None:
        out["tick"] = tick_view(tick, board)  # W6d: only a flagged grant (switch on)
    return out


def _turn_cost(usage: Mapping, pricing: Any) -> float | None:
    """One turn's cost with ``pricing`` (a ``Pricing``), or None when a needed count or
    price is unknown (``usage.estimate_cost``: cache tokens need their own price)."""
    from stiracore.providers.usage import NormalizedUsage, Pricing, estimate_cost

    if not isinstance(pricing, Pricing):
        return None
    return estimate_cost(
        NormalizedUsage(
            input_tokens=_count(usage.get("input_tokens")),
            output_tokens=_count(usage.get("output_tokens")),
            cache_read_tokens=_count(usage.get("cache_read_tokens")),
            cache_write_tokens=_count(usage.get("cache_write_tokens")),
        ),
        pricing,
    )


def totals_from(
    completed: Iterable[Mapping], rates: Any = None, *, model: Any = None
) -> dict[str, Any]:
    """Turns and tokens from ``turn.completed`` data (oldest first); the cost only with rates.

    ``rates``: ``(input, output)`` USD per million tokens, or a mapping ``model id ->
    Pricing`` (``gateway-config.json`` ``token_prices``, #451: each turn is priced by the
    model named in its ``turn.completed``), or None (no cost is shown: an unknown price is
    never displayed as free). With the mapping, ``cost`` is the sum of the priced turns:
    ``cost_partial`` is true when some turns with tokens could not be priced (their model
    has no entry, or a cache price is missing) — then ``cost`` excludes them; with no turn
    priced it is None, unless the turn list is empty and ``model`` (the model in use) has a
    price (``0``).
    """
    completed = list(completed)
    totals: dict[str, Any] = {
        "turns": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "context_tokens": None,
        "cost": None,
    }
    for data in completed:
        if not isinstance(data, Mapping):
            continue
        totals["turns"] += 1
        usage = data.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        for key, source in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_tokens"),
            ("cache_write", "cache_write_tokens"),
        ):
            value = _count(usage.get(source))
            if value is not None:
                totals[key] += value
        context = _count(usage.get("context_tokens"))
        if context is not None:
            totals["context_tokens"] = context
    if isinstance(rates, Mapping):
        return _priced_totals(totals, completed, rates, model)
    if (
        isinstance(rates, (tuple, list))
        and len(rates) == 2
        and all(type(r) in (int, float) and math.isfinite(r) and r >= 0 for r in rates)
    ):
        totals["cost"] = round(
            (totals["input"] * rates[0] + totals["output"] * rates[1]) / 1_000_000, 6
        )
    return totals


def _priced_totals(totals, completed, prices: Mapping, model: Any) -> dict[str, Any]:
    cost, priced, unpriced = 0.0, 0, 0
    for data in completed:
        if not isinstance(data, Mapping):
            continue
        usage = data.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        tokens = [
            _count(usage.get(key))
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        ]
        if not any(tokens):
            continue  # nothing billed (or nothing reported) for this turn
        name = data.get("model")
        turn = _turn_cost(usage, prices.get(name) if type(name) is str else None)
        if turn is None:
            unpriced += 1
        else:
            cost += turn
            priced += 1
    if priced:
        totals["cost"] = round(cost, 6)
        if unpriced:
            totals["cost_partial"] = True
    elif not unpriced and type(model) is str and model in prices:
        totals["cost"] = 0.0
    return totals
