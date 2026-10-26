"""Strict validation of grants (ADR 0011 §2, §8; W4 of #296).

Two shapes are validated here, nothing else:

* the **issue request** a person sends from the dashboard (:func:`parse_issue`);
* the **grant record** as the store holds it (:func:`grant_from_dict` /
  :func:`grant_to_dict`).

Rules (each pinned by ``tests/test_grants_schema.py``):

* exactly the known keys — an unknown or missing key is refused, never ignored;
* exactly built-in values (``type(x) is int`` / ``str`` / ``list`` / ``dict``; a ``bool`` is
  not an ``int``, no subclass is evaluated);
* ``kind`` is ``"goal"``: the discriminator against YOLO windows (``kind="yolo"``), checked
  by :func:`stiracore.authority.require_kind` wherever a record is used;
* ``scope.tools`` is a non-empty subset of :data:`TOOLS`; ``scope.workspace_subtree`` is
  an existing directory inside the agent's write roots, resolved (no symlink escape:
  :func:`check_subtree`);
* ``budget`` ``{total_tokens, per_tick_tokens}``, ``ttl_sec`` (default
  :data:`TTL_DEFAULT_SEC`, cap :data:`TTL_CAP_SEC`), ``max_iterations`` (default
  :data:`MAX_ITERATIONS_DEFAULT`, cap :data:`MAX_ITERATIONS_CAP`), ``no_progress_ticks``
  (default :data:`NO_PROGRESS_DEFAULT`), ``verifier`` exactly ``"user"`` (v1: nothing else);
* the **goal** is free text, but only for the grant store: it is refused when
  :func:`stiracore.observability.masking.mask_secrets` would change it, and no function
  here puts it into an error, a code or a log.

Every refusal is a :class:`GrantRefused` with a fixed code of :data:`CODES`.
"""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

__all__ = [
    "CHECK_COUNT_KEYS",
    "CHECK_KINDS",
    "CHECK_STATUSES",
    "CLAIM_CHECKS",
    "CLAIM_PROGRESS",
    "CLAIM_ROWS_KEPT",
    "CLAIM_WORKSPACE",
    "CODES",
    "GOAL_MAX_CHARS",
    "ISSUE_WORD",
    "KIND",
    "MAX_ITERATIONS_CAP",
    "MAX_ITERATIONS_DEFAULT",
    "NO_PROGRESS_CAP",
    "NO_PROGRESS_DEFAULT",
    "RESUME_WORD",
    "SHOW_WORD",
    "TOOLS",
    "TOTAL_TOKENS_CAP",
    "TTL_CAP_SEC",
    "TTL_DEFAULT_SEC",
    "TTL_MIN_SEC",
    "VERIFIERS",
    "BoardBusy",
    "ClaimEvidence",
    "Grant",
    "GrantRefused",
    "IssueRequest",
    "check_goal",
    "check_subtree",
    "evidence_from_dict",
    "evidence_to_dict",
    "grant_from_dict",
    "grant_to_dict",
    "parse_issue",
]

KIND = "goal"
# The v1 scope allow-list (ADR 0011 §8, W5 brief): read tools, the editor and run_checks.
TOOLS = frozenset(
    {
        "file_read",
        "editor",
        "run_checks",
        "memory_read",
        "memory_search",
        "memory_list",
        "current_time",
    }
)
VERIFIERS = frozenset({"user"})
TTL_DEFAULT_SEC = 7200
TTL_CAP_SEC = 21600
TTL_MIN_SEC = 60
MAX_ITERATIONS_DEFAULT = 10
MAX_ITERATIONS_CAP = 20
NO_PROGRESS_DEFAULT = 3
NO_PROGRESS_CAP = 20
TOTAL_TOKENS_CAP = 5_000_000
GOAL_MAX_CHARS = 2000
SUBTREE_MAX_CHARS = 1024
# W6c: the evidence a completion claim carries (codes only, harness-measured).
CLAIM_CHECKS = ("pass", "fail", "not_run", "unknown")
CLAIM_WORKSPACE = ("changed", "unchanged", "unknown")
CLAIM_PROGRESS = ("new", "repeat", "none", "unknown")
# run_checks' kinds (``tools.run_checks.KINDS``, kept equal by a test) — anything else is
# ``other``; its per-check statuses; the counts a row keeps (clamped).
CHECK_KINDS = ("pytest", "unittest", "node_test", "ruff", "compileall")
CHECK_STATUSES = ("passed", "failed", "error", "limited")
CHECK_COUNT_KEYS = ("passed", "failed", "errors", "skipped", "findings")
CHECK_COUNT_CAP = 1_000_000
CLAIM_ROWS_KEPT = 16
ISSUE_WORD = "ISSUE"
RESUME_WORD = "RESUME"
SHOW_WORD = "SHOW"

_HEX32 = re.compile(r"[0-9a-f]{32}")
# Control characters other than tab / newline are refused in a goal.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Refusal code -> HTTP status. The page maps each code to its wording (grants.code_<code>).
CODES = {
    "grant_body_invalid": 400,
    "grant_field_unknown": 400,
    "grant_field_missing": 400,
    "grant_field_invalid": 400,
    "grant_goal_invalid": 400,
    "grant_goal_secret": 400,
    "grant_tool_invalid": 400,
    "grant_subtree_invalid": 400,
    "grant_subtree_outside": 400,
    "grant_verifier_invalid": 400,
    "grant_cap_exceeded": 400,
    "grant_confirm_mismatch": 400,
    "grant_run_checks_setting_off": 409,
    "grant_run_checks_not_contained": 409,
    "grant_already_active": 409,
    "grant_unknown": 404,
    "grant_terminal": 409,
    "grant_not_active": 409,
    "grant_not_stopped": 409,
    "grant_not_awaiting": 409,
    "grant_still_dangerous": 409,
    "grant_budget_exhausted": 409,
    "grant_resume_needs_new_grant": 409,
    "grant_clock_unavailable": 409,
    "grant_store_unhealthy": 503,
    "grant_store_timeout": 503,
    "grant_issue_aborted_epoch": 409,
    "grant_authority_killed": 409,
    "grant_authority_unhealthy": 503,
    "grant_breaker_not_normal": 409,
    "grant_view_refused": 403,
    "grant_confirmation_required": 428,
    "grant_operation_refused": 409,
    # W5: a "Run one iteration" press that was not admitted (``reason``: a fixed code).
    "grant_iteration_refused": 409,
    # W6d: a grant asked for the heartbeat start while autonomy.heartbeat_start is off.
    "grant_heartbeat_start_off": 409,
}


class GrantRefused(Exception):
    """A refused grant operation; ``code`` is a key of :data:`CODES` (never free text)."""

    def __init__(self, code: str, **extra: Any) -> None:
        if code not in CODES:
            code = "grant_body_invalid"
        super().__init__(code)
        self.code = code
        self.status = CODES[code]
        self.extra = extra


class BoardBusy(RuntimeError):
    """W5b: a lock on the Stage board's read path was held — the board read never waits;
    ``/api/activity`` answers ``grant: {"error": "grant_busy"}`` instead."""

    board_busy = True

    def __init__(self) -> None:
        super().__init__("grant_busy")


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a huge int
        return False


def _int(value: Any, low: int, high: int) -> int:
    if type(value) is not int:
        raise GrantRefused("grant_field_invalid")
    if value < low:
        raise GrantRefused("grant_field_invalid")
    if value > high:
        raise GrantRefused("grant_cap_exceeded")
    return value


def _keys(data: Any, required: Iterable[str], optional: Iterable[str] = ()) -> dict:
    if type(data) is not dict:
        raise GrantRefused("grant_body_invalid")
    required, optional = set(required), set(optional)
    for key in data:
        if type(key) is not str or key not in required | optional:
            raise GrantRefused("grant_field_unknown")
    if not required <= set(data):
        raise GrantRefused("grant_field_missing")
    return data


def check_goal(goal: Any) -> str:
    """The goal as given, if it is storable: non-empty text, at most
    :data:`GOAL_MAX_CHARS`, no control characters, and **unchanged by** ``mask_secrets``
    (a goal the masker would change is refused — ADR 0011 §7 — never stored masked)."""
    from stiracore.observability.masking import mask_secrets

    if type(goal) is not str or not goal.strip() or len(goal) > GOAL_MAX_CHARS:
        raise GrantRefused("grant_goal_invalid")
    if _CONTROL.search(goal):
        raise GrantRefused("grant_goal_invalid")
    if mask_secrets(goal) != goal:
        raise GrantRefused("grant_goal_secret")
    return goal


def check_subtree(raw: Any, roots: Iterable[str | Path], policy: Any = None) -> str:
    """``raw`` resolved, if it is an existing directory inside one of ``roots``.

    The path must be absolute and its **resolved** form (every symlink followed) must lie
    inside a resolved root; the final component must not be a symlink. The resolved path
    is what the grant keeps, so a symlink in the given path can never be re-pointed later.
    ``policy`` (the server's ``WritePathPolicy``) additionally applies the write floor
    (identity / data directory / secret paths): a subtree the agent could not write to is
    refused. Codes: ``grant_subtree_invalid`` / ``grant_subtree_outside``."""
    if type(raw) is not str or not raw or len(raw) > SUBTREE_MAX_CHARS or "\0" in raw:
        raise GrantRefused("grant_subtree_invalid")
    if not os.path.isabs(raw):
        raise GrantRefused("grant_subtree_invalid")
    try:
        info = os.lstat(raw)
        if stat.S_ISLNK(info.st_mode):
            raise GrantRefused("grant_subtree_invalid")
        resolved = os.path.realpath(raw)
        if len(resolved) > SUBTREE_MAX_CHARS:
            # The stored (resolved) path obeys the same cap as the reader's.
            raise GrantRefused("grant_subtree_invalid")
        if not stat.S_ISDIR(os.stat(resolved).st_mode):
            raise GrantRefused("grant_subtree_invalid")
    except OSError as error:
        raise GrantRefused("grant_subtree_invalid") from error
    inside = False
    for root in roots:
        try:
            real_root = os.path.realpath(os.fspath(root))
        except (OSError, TypeError, ValueError):
            continue
        if resolved == real_root or resolved.startswith(real_root.rstrip(os.sep) + os.sep):
            inside = True
            break
    if not inside:
        raise GrantRefused("grant_subtree_outside")
    if policy is not None:
        try:
            decision = policy.check(os.path.join(resolved, ".stira-grant-probe"))
        except Exception as error:  # a policy that cannot judge refuses
            raise GrantRefused("grant_subtree_outside") from error
        if not getattr(decision, "allowed", False):
            raise GrantRefused("grant_subtree_outside")
    return resolved


@dataclass(frozen=True)
class IssueRequest:
    """A validated issue request (the goal text is held separately by the caller)."""

    goal: str
    tools: tuple[str, ...]
    workspace_subtree: str
    total_tokens: int
    per_tick_tokens: int
    ttl_sec: int
    max_iterations: int
    no_progress_ticks: int
    verifier: str
    # W6d: the scheduler's grant_tick may start this grant's iterations (default off).
    heartbeat_start: bool = False

    def __repr__(self) -> str:  # never the goal
        return f"IssueRequest(tools={self.tools!r}, ttl_sec={self.ttl_sec})"


_ISSUE_REQUIRED = ("goal", "scope", "budget", "verifier", "confirm")
_ISSUE_OPTIONAL = ("ttl_sec", "max_iterations", "no_progress_ticks", "heartbeat_start")


def parse_issue(body: Any, *, roots: Iterable[str | Path], policy: Any = None) -> IssueRequest:
    """Validate the issue body. Typed confirmation: ``confirm`` must be exactly
    :data:`ISSUE_WORD`. The containment floor for ``run_checks`` is checked by the
    service (it needs the live settings), not here."""
    data = _keys(body, _ISSUE_REQUIRED, _ISSUE_OPTIONAL)
    if type(data["confirm"]) is not str or data["confirm"] != ISSUE_WORD:
        raise GrantRefused("grant_confirm_mismatch")
    if type(data["verifier"]) is not str or data["verifier"] not in VERIFIERS:
        raise GrantRefused("grant_verifier_invalid")
    scope = _keys(data["scope"], ("tools", "workspace_subtree"))
    tools = scope["tools"]
    if type(tools) is not list or not tools or len(tools) > len(TOOLS):
        raise GrantRefused("grant_tool_invalid")
    for tool in tools:
        if type(tool) is not str or tool not in TOOLS:
            raise GrantRefused("grant_tool_invalid")
    if len(set(tools)) != len(tools):
        raise GrantRefused("grant_tool_invalid")
    budget = _keys(data["budget"], ("total_tokens", "per_tick_tokens"))
    total = _int(budget["total_tokens"], 1, TOTAL_TOKENS_CAP)
    per_tick = _int(budget["per_tick_tokens"], 1, total)
    ttl = _int(data.get("ttl_sec", TTL_DEFAULT_SEC), TTL_MIN_SEC, TTL_CAP_SEC)
    iterations = _int(data.get("max_iterations", MAX_ITERATIONS_DEFAULT), 1, MAX_ITERATIONS_CAP)
    no_progress = _int(data.get("no_progress_ticks", NO_PROGRESS_DEFAULT), 1, NO_PROGRESS_CAP)
    heartbeat = data.get("heartbeat_start", False)
    if type(heartbeat) is not bool:
        raise GrantRefused("grant_field_invalid")
    goal = check_goal(data["goal"])
    subtree = check_subtree(scope["workspace_subtree"], roots, policy)
    return IssueRequest(
        goal=goal,
        tools=tuple(sorted(tools)),
        workspace_subtree=subtree,
        total_tokens=total,
        per_tick_tokens=per_tick,
        ttl_sec=ttl,
        max_iterations=iterations,
        no_progress_ticks=no_progress,
        verifier=data["verifier"],
        heartbeat_start=heartbeat,
    )


# --- the record -------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimEvidence:
    """W6c: what the harness observed when the agent claimed completion — **codes and
    counters only**, never a text the model wrote, never the goal, a path, a digest or a
    file name. One per grant, replaced (never appended) by the next claim; kept after the
    person's verification as the audit trail (read-only).

    Taken at the claim (:func:`.state.claim`, under the service's lock with the clock
    reading of that instant): ``claimed_at`` (wall seconds), the iteration ``k`` of
    ``max_iterations``, tokens used and budget left, TTL left. Measured at the claiming
    iteration's end and published only if nothing changed up to the publication
    (:meth:`.service.GrantService.measure_claim`; ``unknown`` until then and whenever
    something could not be read — never ``pass``): ``checks`` (:data:`CLAIM_CHECKS`, fail
    closed: the combined verdict of every ``run_checks`` call since the grant's last
    editor write — any fail ``fail``, anything unreadable or inconsistent ``unknown``, an
    unread workspace never lets a recorded failure be forgotten — shown as ``pass`` /
    ``fail`` only for the workspace identity the checks ran on, else ``not_run`` /
    ``unknown``) with its per-check ``check_rows`` ``(kind, status, passed, failed, errors,
    skipped, findings)`` (only with ``pass`` / ``fail``), ``workspace``
    (:data:`CLAIM_WORKSPACE`, the W6a workspace identity against the previous claim's;
    the first claim: against the issue) and ``progress`` (:data:`CLAIM_PROGRESS`, the
    claiming iteration's W6a outcome class)."""

    claimed_at: float
    iteration: int
    max_iterations: int
    used_tokens: int
    budget_left: int
    ttl_left_sec: int
    checks: str = "unknown"
    check_rows: tuple[tuple, ...] = ()
    workspace: str = "unknown"
    progress: str = "unknown"


_EVIDENCE_INTS = ("iteration", "max_iterations", "used_tokens", "budget_left", "ttl_left_sec")
_EVIDENCE_KEYS = frozenset(
    {"claimed_at", *_EVIDENCE_INTS, "checks", "check_rows", "workspace", "progress"}
)


def check_row(row: Any) -> tuple | None:
    """A valid evidence row ``(kind, status, *counts)`` or None (the strict shape)."""
    if type(row) is not tuple or len(row) != 2 + len(CHECK_COUNT_KEYS):
        return None
    kind, status, *counts = row
    if kind not in (*CHECK_KINDS, "other") or status not in CHECK_STATUSES:
        return None
    if not all(type(c) is int and 0 <= c <= CHECK_COUNT_CAP for c in counts):
        return None
    return row


def evidence_consistent(evidence: ClaimEvidence) -> bool:
    """The codes are known ones, rows only with ``pass`` / ``fail`` (at most
    :data:`CLAIM_ROWS_KEPT`, every row valid), ``pass`` only when every row passed."""
    rows = evidence.check_rows
    if type(rows) is not tuple or len(rows) > CLAIM_ROWS_KEPT:
        return False
    if any(check_row(r) is None for r in rows):
        return False
    if evidence.checks not in CLAIM_CHECKS or evidence.workspace not in CLAIM_WORKSPACE:
        return False
    if evidence.progress not in CLAIM_PROGRESS:
        return False
    if evidence.checks in ("pass", "fail") and not rows:
        return False
    if evidence.checks not in ("pass", "fail") and rows:
        return False
    passed = all(r[1] == "passed" for r in rows)
    return not (evidence.checks == "pass" and not passed) and not (
        evidence.checks == "fail" and passed
    )


def evidence_to_dict(evidence: ClaimEvidence | None) -> dict | None:
    """The stored (and viewed) form: codes, counters, the claim instant."""
    if evidence is None:
        return None
    out: dict[str, Any] = {"claimed_at": evidence.claimed_at}
    out.update({name: getattr(evidence, name) for name in _EVIDENCE_INTS})
    out["checks"] = evidence.checks
    out["check_rows"] = [
        {"kind": r[0], "status": r[1], "counts": dict(zip(CHECK_COUNT_KEYS, r[2:]))}
        for r in evidence.check_rows
    ]
    out["workspace"] = evidence.workspace
    out["progress"] = evidence.progress
    return out


def evidence_from_dict(data: Any) -> ClaimEvidence | None:
    """The strict reader of :func:`evidence_to_dict` (``None`` stays ``None``); anything
    else raises :class:`GrantRefused` (``grant_body_invalid``)."""
    if data is None:
        return None
    if type(data) is not dict or set(data) != _EVIDENCE_KEYS:
        raise GrantRefused("grant_body_invalid")
    claimed = data["claimed_at"]
    if not _finite(claimed) or not 0 <= claimed <= 1e12:
        raise GrantRefused("grant_body_invalid")
    ints = {}
    for name in _EVIDENCE_INTS:
        value = data[name]
        if type(value) is not int or not 0 <= value < (1 << 53):
            raise GrantRefused("grant_body_invalid")
        ints[name] = value
    raw_rows = data["check_rows"]
    if type(raw_rows) is not list or len(raw_rows) > CLAIM_ROWS_KEPT:
        raise GrantRefused("grant_body_invalid")
    rows = []
    for item in raw_rows:
        if type(item) is not dict or set(item) != {"kind", "status", "counts"}:
            raise GrantRefused("grant_body_invalid")
        counts = item["counts"]
        if type(counts) is not dict or set(counts) != set(CHECK_COUNT_KEYS):
            raise GrantRefused("grant_body_invalid")
        row = check_row((item["kind"], item["status"], *(counts[k] for k in CHECK_COUNT_KEYS)))
        if row is None:
            raise GrantRefused("grant_body_invalid")
        rows.append(row)
    for name in ("checks", "workspace", "progress"):
        if type(data[name]) is not str:
            raise GrantRefused("grant_body_invalid")
    evidence = ClaimEvidence(
        claimed_at=float(claimed),
        checks=data["checks"],
        check_rows=tuple(rows),
        workspace=data["workspace"],
        progress=data["progress"],
        **ints,
    )
    if not evidence_consistent(evidence):
        raise GrantRefused("grant_body_invalid")
    return evidence


@dataclass(frozen=True)
class Grant:
    """One grant and its goal's state (ADR 0011 §2, §8). Immutable: every transition is a
    new record (:mod:`.state`). **The goal text is not here** — the service keeps it next
    to the record and only the store writes it, so no view, event, ``repr`` or error built
    from a record can contain it."""

    grant_id: str
    kind: str
    session_ref: str
    tools: tuple[str, ...]
    workspace_subtree: str
    total_tokens: int
    per_tick_tokens: int
    ttl_sec: int
    max_iterations: int
    no_progress_ticks: int
    verifier: str
    issuer: str
    issued_wall: float
    issued_boot: float
    issued_mono: float
    # The clock reading of the last check (backwards / forward jumps are judged from it).
    last_wall: float
    last_boot: float
    last_mono: float
    generation: int
    # Grant state: status (``state.GRANT_STATUSES``) + reason (fixed codes, "" for none).
    status: str
    reason: str
    # Where a resume returns to (the state before the stop).
    resume_status: str
    resume_reason: str
    # The goal (OpenClaw names + awaiting_verification / unmet).
    goal_status: str
    goal_reason: str
    # Where a goal pause returns to: "" (active) or "awaiting_verification" (ADR §8).
    goal_resume: str
    used_tokens: int
    iterations: int
    no_progress: int
    stop_epoch: int
    recheck: bool
    late_verification: str
    ended_wall: float
    # W6c: the evidence of the last completion claim (codes only), or None.
    claim_evidence: ClaimEvidence | None = None
    # W6d: issued with the heartbeat start (set at issue only, never changed afterwards;
    # stored only when true, so a record without it — every older one — reads as false).
    heartbeat_start: bool = False


_FIELD_TYPES = {f.name: f.type for f in fields(Grant)}
_STR = {
    "grant_id",
    "kind",
    "session_ref",
    "workspace_subtree",
    "verifier",
    "issuer",
    "status",
    "reason",
    "resume_status",
    "resume_reason",
    "goal_status",
    "goal_reason",
    "goal_resume",
    "late_verification",
}
_INT = {
    "total_tokens",
    "per_tick_tokens",
    "ttl_sec",
    "max_iterations",
    "no_progress_ticks",
    "generation",
    "used_tokens",
    "iterations",
    "no_progress",
    "stop_epoch",
}
_FLOAT = {
    "issued_wall",
    "issued_boot",
    "issued_mono",
    "last_wall",
    "last_boot",
    "last_mono",
    "ended_wall",
}
_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}|")


def grant_to_dict(grant: Grant, goal: str) -> dict:
    """The stored form: every field plus the goal (only the grant store writes this)."""
    out = {name: getattr(grant, name) for name in _FIELD_TYPES}
    out["tools"] = list(grant.tools)
    out["claim_evidence"] = evidence_to_dict(grant.claim_evidence)
    if not grant.heartbeat_start:
        del out["heartbeat_start"]  # W6d: only a flagged grant carries the key
    out["goal"] = goal
    return out


def grant_from_dict(data: Any) -> tuple[Grant, str]:
    """A stored grant and its goal; raises :class:`GrantRefused` (``grant_body_invalid``)
    for anything outside the schema (the store then treats the file as malformed).

    ``claim_evidence`` (W6c) may be absent — a record written before W6c has none — and
    then reads as ``None``; every other key is required and no unknown key is accepted."""
    from . import state

    expected = set(_FIELD_TYPES) | {"goal"}
    optional = {"claim_evidence", "heartbeat_start"}
    if type(data) is not dict or not expected - optional <= set(data) <= expected:
        raise GrantRefused("grant_body_invalid")
    # W6d: present only as exactly ``true`` (it is written only then).
    if "heartbeat_start" in data and data["heartbeat_start"] is not True:
        raise GrantRefused("grant_body_invalid")
    values: dict[str, Any] = {}
    for name in _STR:
        value = data[name]
        if type(value) is not str or len(value) > SUBTREE_MAX_CHARS:
            raise GrantRefused("grant_body_invalid")
        values[name] = value
    for name in _INT:
        value = data[name]
        if type(value) is not int or not 0 <= value < (1 << 53):
            raise GrantRefused("grant_body_invalid")
        values[name] = value
    for name in _FLOAT:
        value = data[name]
        if not _finite(value) or abs(value) > 1e12:
            raise GrantRefused("grant_body_invalid")
        values[name] = float(value)
    if type(data["recheck"]) is not bool:
        raise GrantRefused("grant_body_invalid")
    values["recheck"] = data["recheck"]
    tools = data["tools"]
    if (
        type(tools) is not list
        or not tools
        or not all(type(t) is str and t in TOOLS for t in tools)
    ):
        raise GrantRefused("grant_body_invalid")
    values["tools"] = tuple(tools)
    if not _HEX32.fullmatch(values["grant_id"]) or values["kind"] != KIND:
        raise GrantRefused("grant_body_invalid")
    if values["session_ref"] != "grant:" + values["grant_id"]:
        raise GrantRefused("grant_body_invalid")
    if values["verifier"] not in VERIFIERS:
        raise GrantRefused("grant_body_invalid")
    for name in (
        "status",
        "reason",
        "resume_status",
        "resume_reason",
        "goal_status",
        "goal_reason",
    ):
        if not _CODE.fullmatch(values[name]):
            raise GrantRefused("grant_body_invalid")
    if (
        values["status"] not in state.GRANT_STATUSES
        or values["goal_status"] not in state.GOAL_STATUSES
    ):
        raise GrantRefused("grant_body_invalid")
    if values["goal_resume"] not in ("", "awaiting_verification"):
        raise GrantRefused("grant_body_invalid")
    if values["late_verification"] not in ("", "pass", "fail"):
        raise GrantRefused("grant_body_invalid")
    if values["ttl_sec"] > TTL_CAP_SEC or values["max_iterations"] > MAX_ITERATIONS_CAP:
        raise GrantRefused("grant_body_invalid")
    goal = data["goal"]
    if type(goal) is not str or not goal or len(goal) > GOAL_MAX_CHARS:
        raise GrantRefused("grant_body_invalid")
    values["claim_evidence"] = evidence_from_dict(data.get("claim_evidence"))
    values["heartbeat_start"] = data.get("heartbeat_start", False) is True
    return Grant(**values), goal
