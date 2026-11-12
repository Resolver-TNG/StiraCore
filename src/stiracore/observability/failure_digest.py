"""Failure digest: repeated failures as counts with representative event ids (#695).

Pure functions, as :mod:`.trajectory`: nothing here reads a clock, a store or a model, and
nothing is written anywhere (no memory, no prompt). The caller hands in rows it already
read; the result holds **codes, counts, timestamps and ids only**.

Two sources:

* **tool calls** — ``tool_call.updated`` turn events whose ``status`` is ``error`` or
  ``denied``, grouped by ``status`` / ``error_class`` / ``deny_code`` (#696, the fixed sets
  ``hooks.TOOL_ERROR_CLASSES`` / ``TOOL_DENY_CODES``). ``turns.db`` holds masked previews
  and model-chosen names next to these codes, so only :data:`TOOL_FIELDS` are read
  (:func:`tool_row`); a value outside its set counts as :data:`UNCLASSIFIED` and is never
  echoed. The tool name is not read: for an unknown tool it is text the model chose.
  A checkpoint the agent declared itself (``status: denied`` with ``deny_code: declared``,
  #545: a statement of progress, not a refusal) is not a failure; exactly that pair is
  counted under ``excluded`` (``error`` with ``declared`` stays a failure).
* **grants** — lifecycle events (codes only, ``authority.lifecycle_events``) of a grant, a
  goal, an iteration or a heartbeat start whose ``reason_code`` is in
  :data:`GRANT_FAILURE_REASONS`: the iteration ends without the successes
  (``iteration_done`` / ``iteration_claimed``) and the interruptions
  (``grants.progress.INTERRUPTED_ENDS``), plus ``blocked_*`` / ``halted_*`` /
  ``tick_skipped_*``. They have no ``turn_id``; the representative is ``event_id`` /
  ``subject_id`` (the grant id).

**Counts are a lower bound.** An event that could not be recorded was dropped
(``TurnEventRecorder.drop_counts``, #705: integers of the running process); a rotated
lifecycle segment is gone; ``tick_skipped_*`` is written once per grant and reason in a
process. One incident can also appear as more than one pattern (an iteration's end and its
goal's block).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from stiracore.authority import lifecycle_events
from stiracore.grants.progress import INTERRUPTED_ENDS

from .hooks import TOOL_DENY_CODES, TOOL_ERROR_CLASSES
from .turn_events import DROP_REASONS, TOOL_STATUSES

__all__ = [
    "GRANT_FAILURE_REASONS",
    "GRANT_SUBJECTS",
    "LIFECYCLE_FIELDS",
    "SOURCE_PROBLEMS",
    "TOOL_FAILURE_STATUSES",
    "TOOL_FIELDS",
    "UNCLASSIFIED",
    "build",
    "to_markdown",
    "tool_row",
]

# The only fields read from a ``tool_call.updated`` event (top level, then ``data``).
TOOL_FIELDS = ("event_id", "turn_id", "ts", "status", "error_class", "deny_code")
# The only fields read from a lifecycle event.
LIFECYCLE_FIELDS = ("event_id", "subject_kind", "subject_id", "reason_code", "ts_wall")
TOOL_FAILURE_STATUSES = frozenset({"error", "denied"})
UNCLASSIFIED = "unclassified"  # no ``error_class`` (a row from before #696) or outside the set
GRANT_SUBJECTS = frozenset({"grant", "goal", "grant_iteration", "grant_tick"})
_ITERATION_NOT_FAILED = frozenset({"iteration_done", "iteration_claimed"}) | {
    f"iteration_{end}" for end in INTERRUPTED_ENDS
}
GRANT_FAILURE_REASONS = (lifecycle_events.ITERATION_REASONS - _ITERATION_NOT_FAILED) | {
    code
    for code in lifecycle_events.GRANT_REASONS | lifecycle_events.TICK_REASONS
    if code.startswith(("blocked_", "halted_", "tick_skipped_"))
}
# Why a source gave no rows, as fixed codes (the reader's; plus the lifecycle log's own).
SOURCE_PROBLEMS = frozenset({"turns_db_missing", "turns_db_unreadable", "lifecycle_log_unreadable"})
_SOURCE_CODES = SOURCE_PROBLEMS | lifecycle_events.UNHEALTHY_REASONS

_HEX32 = re.compile(r"[0-9a-f]{32}")
_MAX_TS = 1e11  # as the lifecycle contract: far beyond any real wall clock
_MAX_COUNT = 1 << 53


def _ts(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= _MAX_TS else None


def _ident(value: Any) -> str | None:
    return value if type(value) is str and _HEX32.fullmatch(value) else None


def _code(value: Any, allowed: frozenset[str]) -> str | None:
    return value if type(value) is str and value in allowed else None


def _tool(row: Any) -> dict[str, Any] | None:
    """The allow-listed, validated fields of one flat tool row, else ``None``."""
    if not isinstance(row, Mapping):
        return None
    status = _code(row.get("status"), TOOL_STATUSES)
    ts = _ts(row.get("ts"))
    if status is None or ts is None:
        return None
    return {
        "event_id": _ident(row.get("event_id")),
        "turn_id": _ident(row.get("turn_id")),
        "ts": ts,
        "status": status,
        "error_class": _code(row.get("error_class"), TOOL_ERROR_CLASSES),
        "deny_code": _code(row.get("deny_code"), TOOL_DENY_CODES),
    }


def tool_row(event: Any) -> dict[str, Any] | None:
    """:data:`TOOL_FIELDS` of one stored ``tool_call.updated`` turn event, else ``None``.

    Nothing else of the event is read (no name, no preview, no id of the tool call)."""
    if not isinstance(event, Mapping) or event.get("type") != "tool_call.updated":
        return None
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    return _tool(
        {
            "event_id": event.get("event_id"),
            "turn_id": event.get("turn_id"),
            "ts": event.get("ts"),
            "status": data.get("status"),
            "error_class": data.get("error_class"),
            "deny_code": data.get("deny_code"),
        }
    )


def _lifecycle(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    ts = _ts(row.get("ts_wall"))
    if ts is None:
        return None
    return {
        "event_id": _ident(row.get("event_id")),
        "subject_id": _ident(row.get("subject_id")),
        "ts": ts,
        "subject_kind": _code(row.get("subject_kind"), GRANT_SUBJECTS),
        "reason_code": _code(row.get("reason_code"), GRANT_FAILURE_REASONS),
    }


def _drops(drop_counts: Any) -> dict[str, int] | None:
    if not isinstance(drop_counts, Mapping):
        return None
    return {
        reason: drop_counts[reason]
        for reason in DROP_REASONS
        if type(drop_counts.get(reason)) is int and 0 <= drop_counts[reason] < _MAX_COUNT
    }


def build(
    *,
    tool_rows: Iterable[Any] = (),
    lifecycle_rows: Iterable[Any] = (),
    since: float | None = None,
    until: float | None = None,
    drop_counts: Mapping[str, Any] | None = None,
    source_problems: Iterable[Any] = (),
    top: int = 3,
    examples: int = 3,
) -> dict[str, Any]:
    """Failure patterns of the rows in ``[since, until)``, most frequent first.

    ``tool_rows``: flat rows (:data:`TOOL_FIELDS`, as :func:`tool_row` returns).
    ``lifecycle_rows``: lifecycle events (only :data:`LIFECYCLE_FIELDS` are read).
    ``drop_counts``: ``TurnEventRecorder.drop_counts()`` of the process that recorded, if
    the caller has it. ``source_problems``: fixed codes of a source that could not be read.
    Each pattern carries its ``count``, its ``share`` of all failures and at most
    ``examples`` ids, newest first; ``top`` says what the ``top`` largest patterns sum to.
    """
    if type(top) is not int or type(examples) is not int or top < 1 or examples < 0:
        raise ValueError("top must be an integer >= 1 and examples an integer >= 0.")
    low = None if since is None else _ts(since)
    high = None if until is None else _ts(until)
    if (since is not None and low is None) or (until is not None and high is None):
        raise ValueError("since/until must be finite wall-clock seconds.")

    def inside(ts: float) -> bool:
        return (low is None or ts >= low) and (high is None or ts < high)

    groups: dict[str, dict[str, Any]] = {}
    skipped = 0
    declared = 0

    def add(label: str, source: str, codes: dict[str, str], ts: float, example: dict) -> None:
        group = groups.setdefault(
            label, {"pattern": label, "source": source, **codes, "count": 0, "rows": []}
        )
        group["count"] += 1
        # a total order (two rows equal in all three print the same example)
        other = example.get("turn_id") or example.get("subject_id") or ""
        group["rows"].append((ts, example.get("event_id") or "", other, example))

    for raw in tool_rows:
        row = _tool(raw)
        if row is None:
            skipped += 1
            continue
        if row["status"] not in TOOL_FAILURE_STATUSES or not inside(row["ts"]):
            continue
        if row["status"] == "denied" and row["deny_code"] == "declared":
            declared += 1
            continue
        codes = {"status": row["status"], "error_class": row["error_class"] or UNCLASSIFIED}
        if row["deny_code"] is not None:
            codes["deny_code"] = row["deny_code"]
        add(
            "tool:" + ":".join(codes.values()),
            "tool",
            codes,
            row["ts"],
            {"event_id": row["event_id"], "turn_id": row["turn_id"]},
        )
    for raw in lifecycle_rows:
        row = _lifecycle(raw)
        if row is None:
            skipped += 1
            continue
        if row["subject_kind"] is None or row["reason_code"] is None or not inside(row["ts"]):
            continue
        codes = {"subject_kind": row["subject_kind"], "reason_code": row["reason_code"]}
        add(
            "grant:" + ":".join(codes.values()),
            "grant",
            codes,
            row["ts"],
            {"event_id": row["event_id"], "subject_id": row["subject_id"]},
        )

    total = sum(group["count"] for group in groups.values())
    patterns = []
    for group in sorted(groups.values(), key=lambda item: (-item["count"], item["pattern"])):
        rows = sorted(group.pop("rows"), key=lambda item: item[:3])
        patterns.append(
            {
                **group,
                "share": group["count"] / total,
                "first_ts": rows[0][0],
                "last_ts": rows[-1][0],
                "examples": [row[3] for row in rows[::-1][:examples]],
            }
        )
    top_count = sum(pattern["count"] for pattern in patterns[:top])
    problems = sorted({code for code in source_problems if _code(code, _SOURCE_CODES)})
    return {
        "since": low,
        "until": high,
        "total": total,
        "by_source": {
            source: sum(p["count"] for p in patterns if p["source"] == source)
            for source in ("tool", "grant")
        },
        "patterns": patterns,
        "top": {
            "n": top,
            "patterns": [pattern["pattern"] for pattern in patterns[:top]],
            "count": top_count,
            "share": top_count / total if total else 0.0,
        },
        "excluded": {"declared_checkpoint": declared},
        "skipped_rows": skipped,
        "dropped_events": _drops(drop_counts),
        "source_problems": problems,
        "lower_bound": True,
    }


def _iso(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percent(share: float) -> str:
    return f"{share * 100:.1f}%"


def _example(example: Mapping[str, Any]) -> str:
    parts = [f"{key}={value}" for key, value in example.items() if value is not None]
    return " ".join(parts) if parts else "-"


def to_markdown(digest: Mapping[str, Any]) -> str:
    """The digest of :func:`build` as Markdown (the same codes, counts and ids)."""
    top = digest["top"]
    counted = (
        f"- Failures counted: {digest['total']} (tool calls {digest['by_source']['tool']},"
        f" grants {digest['by_source']['grant']}), at least"
    )
    largest = (
        f"- Top {top['n']} patterns: {top['count']} of {digest['total']}"
        f" ({_percent(top['share'])} of all failures)"
    )
    declared = digest["excluded"]["declared_checkpoint"]
    lines = [
        "# Failure digest",
        "",
        f"- Period (UTC): {_iso(digest['since'])} to {_iso(digest['until'])}",
        counted,
        largest,
        f"- Excluded (not failures): {declared} declared checkpoint(s)",
        "",
    ]
    if digest["patterns"]:
        lines += [
            "| # | Pattern | Count | Share | First (UTC) | Last (UTC) | Representative events |",
            "|---|---|---|---|---|---|---|",
        ]
        for rank, pattern in enumerate(digest["patterns"], 1):
            shown = "<br>".join(_example(example) for example in pattern["examples"]) or "-"
            lines.append(
                f"| {rank} | `{pattern['pattern']}` | {pattern['count']} |"
                f" {_percent(pattern['share'])} | {_iso(pattern['first_ts'])} |"
                f" {_iso(pattern['last_ts'])} | {shown} |"
            )
    else:
        lines.append("No failure was recorded in this period.")
    lines += ["", "## Notes", ""]
    lines.append(
        "- Counts are a lower bound: an event that could not be recorded was dropped, a"
        " rotated lifecycle segment is gone, and `tick_skipped_*` is written once per grant"
        " and reason in a process."
    )
    lines.append(
        "- One incident can appear as more than one pattern (an iteration's end and its"
        " goal's block)."
    )
    dropped = digest["dropped_events"]
    if dropped is None:
        lines.append(
            "- Dropped turn events: not known here (the counter lives in the running server"
            " process and is zero again after a restart)."
        )
    else:
        counts = ", ".join(f"{reason} {count}" for reason, count in dropped.items()) or "none"
        lines.append(f"- Dropped turn events (process counter): {counts}.")
    for code in digest["source_problems"]:
        lines.append(f"- A source could not be read: `{code}` (its failures are not counted).")
    if digest["skipped_rows"]:
        lines.append(f"- Rows skipped as malformed: {digest['skipped_rows']}.")
    return "\n".join(lines) + "\n"
