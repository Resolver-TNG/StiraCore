"""The round ledger: one allow-listed, hash-chained record per grant round (ADR 0028 §5).

**Observation only.** The executor appends one ``k = "round"`` record when an iteration
of a grant has ended (a person's press or a heartbeat start). The record holds what the
harness measured — **codes and counts only** — and nothing reads it back as input to an
authority decision. The ledger is not a lifecycle event, posts no outbox item and never
latches the authority service; a ledger that cannot be written only stops the
*heartbeat* start of a grant (``tick_skipped(ledger_unhealthy)``, Q12).

**Allow-list, refuse instead of truncate.** :func:`validate` accepts exactly the keys of
one record kind, each value exactly a built-in ``int`` / ``str`` / ``None`` / ``dict`` /
``list`` of the listed shape and each string a member of its fixed enum. Anything else —
an unknown key, a free-text value, a string outside its enum, a ``bool`` for a count —
raises :class:`LedgerSchemaError` and **nothing is written** (the bad field is never
dropped silently). The writer's caller maps an unexpected code to ``other`` before it
builds the record; the writer itself maps nothing.

Never in a record: the goal, model text, a path or file name, check output, approval
arguments, a URL or host, a secret. There is no field that could hold one.

**Two kinds, one chain.** ``round`` and ``approval`` (the record of one answered Ask;
``approved_by`` is ``person`` or ``none`` today — a model approver is a later,
person-built step) share ``v``, ``seq``, ``prev`` and so one chain.

**The file** ``grant-rounds.jsonl`` (data directory, ``0600``): one JSON line per record,
``{"v", "k", "seq", "prev", "hash", …fields}``, ``hash`` = ``sha256(
"stiracore-grant-rounds:v1:" + canonical JSON without "hash")``, ``prev`` the previous
line's hash. Appended with ``O_APPEND``, flushed per line and opened under the safe-open
rules of ``authority-lifecycle.jsonl`` (:mod:`stiracore.authority.private_file`). An
append that would take the file past :data:`ROTATE_BYTES` first moves it to
``grant-rounds.1.jsonl`` (replacing an older one); the new file continues the chain. A
file replaced behind the writer, a broken chain, a torn last line or an unsafe file make
the ledger unusable (sticky; nothing more is written, nothing is repaired here).

Limits (those of the lifecycle log): the chain is unkeyed, so it detects corruption, a
removed, reordered or edited line and a torn tail — not a rewrite of the whole file by
the same OS user. Whole lines cut from the end of the file are detected by the running
writer (its next append or :meth:`RoundLedger.verify`), not by a fresh reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any

from stiracore.authority import epoch_store, private_file

from .schema import CHECK_KINDS

__all__ = [
    "APPROVAL_REASONS",
    "APPROVED_BY",
    "AXIS_CODES",
    "AXIS_CODES_AS_OTHER",
    "DETAIL_CODES",
    "END_CODES",
    "ERROR_CODES",
    "LEDGER_FILE",
    "OLD_LEDGER_FILE",
    "ROTATE_BYTES",
    "STOP_CODES",
    "TOOL_KEYS",
    "UNHEALTHY_REASONS",
    "VERSION",
    "LedgerSchemaError",
    "RoundLedger",
    "axis_code",
    "public",
    "summary",
    "validate",
]

LEDGER_FILE = "grant-rounds.jsonl"
OLD_LEDGER_FILE = "grant-rounds.1.jsonl"
ROTATE_BYTES = 1 << 20
_MAX_BYTES = 2 << 20  # the reader's limit per segment: above ROTATE_BYTES + one line
VERSION = 1
_MEMORY_LINES = 2048  # a ledger without a data directory keeps this many records
_PREFIX = "round_ledger"
_DOMAIN = "stiracore-grant-rounds:v1:"
_ZERO = "0" * 64
_HEX12 = re.compile(r"[0-9a-f]{12}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_COUNT = 1_000_000
_MAX_TOKENS = 1 << 53
_MAX_WALL = 100_000_000_000  # seconds: far beyond any real wall clock
_MAX_MS = 1_000_000_000_000

#: Why the ledger is unusable (fixed codes; ``round_ledger_record_refused``: a record was
#: refused by the allow-list in this process — a round was not recorded).
UNHEALTHY_REASONS = frozenset(
    {
        "round_ledger_unsafe",
        "round_ledger_unreadable",
        "round_ledger_malformed",
        "round_ledger_hash_mismatch",
        "round_ledger_sequence_gap",
        "round_ledger_torn",
        "round_ledger_replaced",
        "round_ledger_write_failed",
        "round_ledger_record_refused",
    }
)

ORIGINS = frozenset({"press", "tick"})
PROGRESS = frozenset({"new", "repeat", "none", "unknown"})
#: The iteration end codes (``grants.executor.END_CODES`` plus ``page_tools_unavailable``
#: and ``other``; a test keeps them in step).
END_CODES = frozenset(
    {
        "done",
        "claimed",
        "reported_blocked",
        "yielded_user_turn",
        "permit_stale",
        "budget_refused",
        "token_count_unavailable",
        "approval_timeout",
        "run_checks_unavailable",
        "timeout",
        "model_error",
        "tool_refused",
        "stopped",
        "shutdown",
        "failed",
        "workspace_changed",
        "no_action",
        # ADR 0015 rev 6: a page tool's axis check failed inside the round (S5).
        "page_tools_unavailable",
        "other",
    }
)
#: The axis check's result codes (ADR 0015 rev 6 "Grant round boundary" item 4): the
#: codes of its axis table plus ``pass`` / ``not_run`` / ``axis_stale`` /
#: ``axis_unreadable`` / ``other``. Since S5 the executor measures it for a round of a
#: grant that names a page tool (``start``: the round boundary's full check, ``end``: the
#: light check at the round's end); every other round is written ``not_run``. Nothing
#: reads the value back.
AXIS_CODES = frozenset(
    {
        "pass",
        "not_run",
        "axis_stale",
        "axis_unreadable",
        "guard_pending",
        "browser_guard_unverified",
        "browser_guard_unmeasured",
        "platform_unmeasured",
        "rules_locked",
        "rules_changed",
        "record_unhealthy",
        "ledger_unhealthy",
        "write_floor_changed",
        "reviewer_changed",
        "reviewer_remote",
        "file_read_not_always",
        "not_in_scope",
        "breaker_not_normal",
        "other",
    }
)
_AXIS_KEYS = frozenset({"start", "end"})
#: ``page_look``'s cap codes that are not results of the axis check (they belong to one
#: chat turn's conditional Always: ``tools.page_look.CAP_CODES``). The enum above does not
#: grow by them: :func:`axis_code` writes each as ``other`` (ADR 0015, "#778 as built").
AXIS_CODES_AS_OTHER = frozenset(
    {"origin_not_covered", "repeated_failure", "call_limit", "cap_unreadable"}
)


def axis_code(code: Any) -> str:
    """The ``axis`` value the ledger takes for an axis or cap code: the code itself when
    it is a member of :data:`AXIS_CODES`, ``other`` for :data:`AXIS_CODES_AS_OTHER`, and
    ``axis_unreadable`` for anything else (never ``pass``, never the value as given)."""
    if type(code) is not str:
        return "axis_unreadable"
    if code in AXIS_CODES:
        return code
    return "other" if code in AXIS_CODES_AS_OTHER else "axis_unreadable"


#: The iteration detail codes (``grants.executor.DETAIL_CODES`` without the empty one,
#: which is ``none`` here, plus the failing axis codes — the detail of the end code
#: ``page_tools_unavailable``; a test keeps
#: them in step).
DETAIL_CODES = frozenset(
    {
        *(AXIS_CODES - {"pass", "not_run"}),
        "none",
        "killed",
        "authority_unhealthy",
        "store_unhealthy",
        "grant_not_active",
        "goal_not_active",
        "breaker",
        "clock",
        "ttl",
        "grant_total",
        "per_tick",
        "pool",
        "no_counter",
        "count_failed",
        "no_formatter",
        "provider_unsupported",
        "max_tokens_unknown",
        "not_in_scope",
        "outside_subtree",
        "path_invalid",
        "protected_path",
        "setting_off",
        "not_contained",
        "disabled",
        "containment_unverified",
        "in_flight_cancelled",
        "provider_error",
        "child_error",
        "report_refused",
        "permit_refused",
        "tick_stale",
        "other",
    }
)
#: The ``tools`` keys of a round row written before the ``axis`` key existed (read only).
_LEGACY_TOOL_KEYS = (
    "editor",
    "file_read",
    "run_checks",
    "memory_read",
    "memory_search",
    "memory_list",
    "current_time",
    "goal_report",
)
#: The fixed keys of ``tools`` (the grant scope's tool names, ``goal_report`` and
#: ``page_review``, which no grant scope can name yet: its count is 0).
TOOL_KEYS = (*_LEGACY_TOOL_KEYS, "page_look", "page_review", "view_image")
ERROR_CODES = frozenset(
    {
        "max_tokens",
        "model_error",
        "tool_refused",
        "token_count_unavailable",
        "provider_changed",
        "budget_refused",
        "timeout",
        "failed",
        "other",
    }
)
_MAX_ERRORS = 8
#: The rule that ended or paused the grant in a round (``None`` in a record: none did).
STOP_CODES = frozenset(
    {
        "ttl",
        "max_iterations",
        "budget",
        "no_progress",
        "loop_detected",
        "same_blocker",
        "repeated_failure",
        "approval_timeout",
        "verification",
        "run_checks_unavailable",
        "iteration_timeout",
        "workspace_changed",
        # ADR 0015 rev 6 (S5): the goal is blocked ``axis_failed``. The block is applied
        # at a refused press (no round runs, no row), so a row carries it only if a round
        # ends while the goal is already blocked so; a build older than S5 refuses that row.
        "axis_failed",
        "needs_decision",
        "out_of_scope",
        "missing_input",
        "stop_file",
        "kill",
        "user",
        "restart",
        "session_reset",
        "clock_anomaly",
        "unhealthy",
        "breaker",
        "unknown",
        "other",
    }
)
_LIMIT_INTS = ("iteration_wall_sec", "ttl_left_s", "iterations_left", "tokens_left", "ceiling_min")
# ``out_cap_source`` / ``ctx_mode`` follow #745 / #746 once they exist; before that: unknown.
OUT_CAP_SOURCES = frozenset({"unknown"})
CTX_MODES = frozenset({"unknown"})
_LIMIT_KEYS = frozenset({*_LIMIT_INTS, "out_cap", "out_cap_source", "ctx_mode"})

_ROUND_COUNTS = ("model_calls", "writes", "approval_waits", "reports")
_ROUND_NULLABLE = (
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_write",
    "largest_input",
    "summaries",
)
_ROUND_KEYS = frozenset(
    {
        "k",
        "grant",
        "round",
        "of_max",
        "origin",
        "start",
        "end",
        "wall_ms",
        "end_code",
        "detail",
        "progress",
        *_ROUND_COUNTS,
        "tools",
        "files_changed",
        *_ROUND_NULLABLE,
        "errors",
        "limits",
        "stop",
        "axis",
    }
)
# A round row already on disk from before the ``axis`` key: exactly the keys of that time
# (no ``axis``, the eight ``tools`` keys). Accepted by the **reader** only, as it was
# written — history is never rewritten, so its hash and the chain stay valid. The writer
# (:func:`validate`) refuses this shape.
_LEGACY_ROUND_KEYS = _ROUND_KEYS - {"axis"}
#: #683: the result of a round's done_when gate (``grants.progress``' codes).
DONE_WHEN_RESULTS = frozenset({"met", "not_proven"})

# -- the approval record (ADR 0028 §5, revision 2) ------------------------------------------
# The page tools are not approval tools here: an approval record for one is a later slice.
APPROVAL_TOOLS = frozenset({*_LEGACY_TOOL_KEYS, "other"}) - {"goal_report"}
TARGET_CLASSES = frozenset({"inside_subtree", "outside_subtree", "unknown"})
#: Who was consulted / who approved. Only a person approves today: ``main`` / ``reviewer``
#: (a model approver) are added by the person-built approver step, not here.
CONSULTED = frozenset({"none"})
APPROVED_BY = frozenset({"person", "none"})
DECISIONS = frozenset({"approved", "refused", "handed_to_person"})
APPROVAL_REASONS = frozenset(
    {
        "scope_named",
        "class_expected",
        "within_budget",
        "anomaly_repeat",
        "anomaly_rate",
        "anomaly_budget",
        "class_unexpected",
        "uncertain",
        "not_named_in_scope",
        "loosening",
        "person_only",
        "egress",
        "secret",
        "self_approval",
        "same_session",
        "no_approver",
        "timeout",
        "malformed",
        "breaker_not_normal",
        "ledger_unhealthy",
        "per_iteration_cap",
        "per_grant_cap",
        "budget_exhausted",
        "epoch_changed",
        "repeat_limit",
        "config_changed",
        "person_card",
        "other",
    }
)
_APPROVAL_KEYS = frozenset(
    {
        "k",
        "grant",
        "round",
        "origin",
        "ts",
        "tool",
        "args_digest",
        "target_class",
        "consulted",
        "decision",
        "approved_by",
        "approver_id",
        "reason",
        "latency_ms",
        "approver_tokens_in",
        "approver_tokens_out",
        "budget_left",
        "iteration_approvals",
    }
)
_WRITER_KEYS = ("v", "seq", "prev", "hash")


class LedgerSchemaError(ValueError):
    """A record outside the allow-list (an unknown key, a free-text value, a string
    outside its enum, a wrong type): refused, nothing is written. The message is the
    field's name (a fixed word), never a value."""


def _int(record: dict, key: str, low: int, high: int) -> None:
    value = record[key]
    if type(value) is not int or not low <= value <= high:
        raise LedgerSchemaError(key)


def _int_or_none(record: dict, key: str, high: int) -> None:
    if record[key] is not None:
        _int(record, key, 0, high)


def _enum(record: dict, key: str, allowed: frozenset) -> None:
    value = record[key]
    if type(value) is not str or value not in allowed:
        raise LedgerSchemaError(key)


def _keys(record: Any, allowed: frozenset, name: str) -> None:
    if type(record) is not dict:
        raise LedgerSchemaError(name)
    for key in record:
        if type(key) is not str:
            raise LedgerSchemaError(name)
    if set(record) != allowed:
        raise LedgerSchemaError(name)


def _common(record: dict) -> None:
    if type(record["grant"]) is not str or not _HEX32.fullmatch(record["grant"]):
        raise LedgerSchemaError("grant")
    _int(record, "round", 1, _MAX_COUNT)
    _enum(record, "origin", ORIGINS)


def _done_when(value: Any) -> None:
    """#683: ``{"result", "unmet"}`` — ``met`` with no unmet kind, or ``not_proven`` with
    the distinct, sorted run_checks kinds that did not pass."""
    _keys(value, frozenset({"result", "unmet"}), "done_when")
    _enum(value, "result", DONE_WHEN_RESULTS)
    unmet = value["unmet"]
    if type(unmet) is not list or not all(type(k) is str and k in CHECK_KINDS for k in unmet):
        raise LedgerSchemaError("done_when")
    if unmet != sorted(set(unmet)) or (value["result"] == "met") != (not unmet):
        raise LedgerSchemaError("done_when")


def _round(record: dict, *, legacy: bool = False) -> None:
    tool_keys = _LEGACY_TOOL_KEYS if legacy else TOOL_KEYS
    allowed = _LEGACY_ROUND_KEYS if legacy else _ROUND_KEYS
    if not legacy and type(record) is dict and "done_when" in record:
        allowed = allowed | {"done_when"}  # #683: only a round that ran the gate
    _keys(record, allowed, "keys")
    if "done_when" in allowed:
        _done_when(record["done_when"])
    _common(record)
    _int(record, "of_max", 0, _MAX_COUNT)
    _int(record, "start", 0, _MAX_WALL)
    _int(record, "end", 0, _MAX_WALL)
    _int(record, "wall_ms", 0, _MAX_MS)
    _enum(record, "end_code", END_CODES)
    _enum(record, "detail", DETAIL_CODES)
    _enum(record, "progress", PROGRESS)
    for key in _ROUND_COUNTS:
        _int(record, key, 0, _MAX_COUNT)
    tools = record["tools"]
    _keys(tools, frozenset(tool_keys), "tools")
    for key in tool_keys:
        _int(tools, key, 0, _MAX_COUNT)
    _int(record, "files_changed", 0, 256)
    for key in _ROUND_NULLABLE:
        _int_or_none(record, key, _MAX_TOKENS)
    errors = record["errors"]
    if type(errors) is not list or len(errors) > _MAX_ERRORS:
        raise LedgerSchemaError("errors")
    for code in errors:
        if type(code) is not str or code not in ERROR_CODES:
            raise LedgerSchemaError("errors")
    limits = record["limits"]
    _keys(limits, _LIMIT_KEYS, "limits")
    for key in _LIMIT_INTS:
        _int(limits, key, 0, _MAX_TOKENS)
    _int_or_none(limits, "out_cap", _MAX_TOKENS)
    _enum(limits, "out_cap_source", OUT_CAP_SOURCES)
    _enum(limits, "ctx_mode", CTX_MODES)
    if record["stop"] is not None:
        _enum(record, "stop", STOP_CODES)
    if not legacy:
        axis = record["axis"]
        _keys(axis, _AXIS_KEYS, "axis")
        for key in sorted(_AXIS_KEYS):
            _enum(axis, key, AXIS_CODES)


def _approval(record: dict) -> None:
    _keys(record, _APPROVAL_KEYS, "keys")
    _common(record)
    _int(record, "ts", 0, _MAX_WALL)
    _enum(record, "tool", APPROVAL_TOOLS)
    if type(record["args_digest"]) is not str or not _HEX12.fullmatch(record["args_digest"]):
        raise LedgerSchemaError("args_digest")
    _enum(record, "target_class", TARGET_CLASSES)
    _enum(record, "consulted", CONSULTED)
    _enum(record, "decision", DECISIONS)
    _enum(record, "approved_by", APPROVED_BY)
    if (record["decision"] == "approved") != (record["approved_by"] != "none"):
        raise LedgerSchemaError("approved_by")  # ``none`` unless the decision is approved
    if record["approver_id"] is not None:  # only with a reviewer (not built)
        raise LedgerSchemaError("approver_id")
    _enum(record, "reason", APPROVAL_REASONS)
    _int(record, "latency_ms", 0, _MAX_MS)
    _int_or_none(record, "approver_tokens_in", _MAX_TOKENS)
    _int_or_none(record, "approver_tokens_out", _MAX_TOKENS)
    _int(record, "budget_left", 0, _MAX_TOKENS)
    _int(record, "iteration_approvals", 0, _MAX_COUNT)


_KINDS = {"round": _round, "approval": _approval}


def _plain(value: Any) -> Any:
    """A deep copy of a validated value (built-in containers only)."""
    if type(value) is dict:
        return {key: _plain(value[key]) for key in sorted(value)}
    if type(value) is list:
        return [_plain(item) for item in value]
    return value


def validate(record: Any) -> dict:
    """The record (``k`` and its fields, without the writer's ``v`` / ``seq`` / ``prev`` /
    ``hash``) as a plain copy if it is inside the allow-list, else raise
    :class:`LedgerSchemaError`."""
    if type(record) is not dict:
        raise LedgerSchemaError("not a dict")
    kind = record.get("k")
    if type(kind) is not str or kind not in _KINDS:
        raise LedgerSchemaError("k")
    _KINDS[kind](record)
    return _plain(record)


def _is_legacy(fields: Any) -> bool:
    """A stored ``round`` row from before the ``axis`` key (it has none)."""
    return type(fields) is dict and fields.get("k") == "round" and "axis" not in fields


def _validate_stored(fields: Any) -> None:
    """The reader's check of one stored line's fields: the allow-list of :func:`validate`,
    or — for a ``round`` row without ``axis`` — exactly the shape that was written before
    the key existed. Nothing in between is accepted."""
    if _is_legacy(fields):
        _round(fields, legacy=True)
    else:
        validate(fields)


def _digest(line: dict) -> str:
    body = {k: v for k, v in line.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((_DOMAIN + text).encode("ascii")).hexdigest()


def _has_axis(lines: list[dict]) -> bool:
    return any(line["k"] == "round" and "axis" in line for line in lines)


def _parse(
    raw: bytes, prev: str | None, seq: int | None, axis_seen: bool = False
) -> tuple[list[dict] | None, str | None]:
    """The lines of one segment. ``prev`` / ``seq``: where the chain must continue from
    (the older segment's end), or ``None`` for an unknown start (an older segment).
    ``axis_seen``: the older segment already holds a round row with ``axis`` — a row
    without it (the shape of before the key) is accepted only before the first one with."""
    if not raw:
        return [], None
    if not raw.endswith(b"\n"):
        return None, "round_ledger_torn"
    lines: list[dict] = []
    for text in raw.split(b"\n")[:-1]:
        try:
            item = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None, "round_ledger_malformed"
        if type(item) is not dict or not all(key in item for key in _WRITER_KEYS):
            return None, "round_ledger_malformed"
        if item["v"] != VERSION or type(item["v"]) is not int:
            return None, "round_ledger_malformed"
        if type(item["seq"]) is not int or item["seq"] < 1:
            return None, "round_ledger_malformed"
        if not all(type(item[k]) is str and _HEX64.fullmatch(item[k]) for k in ("prev", "hash")):
            return None, "round_ledger_malformed"
        fields = {k: v for k, v in item.items() if k not in _WRITER_KEYS}
        try:
            _validate_stored(fields)
        except LedgerSchemaError:
            return None, "round_ledger_malformed"
        if _is_legacy(fields):
            if axis_seen:
                return None, "round_ledger_malformed"
        elif fields["k"] == "round":
            axis_seen = True
        if item["hash"] != _digest(item):
            return None, "round_ledger_hash_mismatch"
        if lines:
            expected_prev, expected_seq = lines[-1]["hash"], lines[-1]["seq"] + 1
        else:
            expected_prev, expected_seq = prev, seq
        if expected_seq is not None and item["seq"] != expected_seq:
            return None, "round_ledger_sequence_gap"
        if expected_prev is not None and item["prev"] != expected_prev:
            return None, "round_ledger_hash_mismatch"
        lines.append(item)
    return lines, None


def public(line: dict) -> dict:
    """One stored line for a reader: the record with ``v`` and ``seq``, without the chain's
    ``prev`` / ``hash``."""
    return {key: _plain(value) for key, value in line.items() if key not in ("prev", "hash")}


def summary(records: list[dict]) -> dict:
    """The one-line summary of ``round`` records (numbers only): rounds, wall hours, the
    reported tokens (``None``: no round reported any), ``new`` rounds."""
    rounds = [r for r in records if r.get("k") == "round"]
    out: dict[str, Any] = {
        "rounds": len(rounds),
        "hours": round(sum(r["wall_ms"] for r in rounds) / 3_600_000, 2),
        "new": sum(1 for r in rounds if r["progress"] == "new"),
    }
    for key in ("tokens_in", "tokens_out"):
        reported = [r[key] for r in rounds if r[key] is not None]
        out[key] = sum(reported) if reported else None
    return out


class RoundLedger:
    """The append-only, hash-chained round ledger of one data directory: the current
    segment and at most one older one (``data_dir`` ``None``: memory only). Every method
    takes the ledger's own I/O lock; nothing here calls the authority service.

    ``rotate=False`` (an offline reader, the CLI): the file is only read."""

    def __init__(self, data_dir: Path | None, *, rotate: bool = True) -> None:
        self._dir = None if data_dir is None else Path(data_dir)
        self._lock = threading.Lock()
        self._bad: str | None = None
        self._refused = 0
        self._identity: tuple[int, ...] | None = None
        self._old_identity: tuple[int, ...] | None = None
        self._size = 0
        self._last_hash = _ZERO
        self._last_seq = 0
        self._old_lines: list[dict] = []
        self._cur_lines: list[dict] = []
        self._cur_hash = hashlib.sha256()
        self._old_digest: str | None = None
        self._old_size = 0
        if self._dir is None:
            return
        old = private_file.read(self._dir / OLD_LEDGER_FILE, max_bytes=_MAX_BYTES, prefix=_PREFIX)
        if old.status == "bad":
            self._bad = old.reason
            return
        old_lines: list[dict] = []
        if old.status == "ok":
            parsed, reason = _parse(old.raw, None, None)
            if parsed is None:
                self._bad = reason
                return
            old_lines = parsed
            self._old_identity = old.identity
            self._old_digest = hashlib.sha256(old.raw).hexdigest()
            self._old_size = len(old.raw)
        current = private_file.read(self._dir / LEDGER_FILE, max_bytes=_MAX_BYTES, prefix=_PREFIX)
        if current.status == "bad":
            self._bad = current.reason
            return
        lines: list[dict] = []
        if current.status == "ok":
            tail = old_lines[-1] if old_lines else None
            parsed, reason = _parse(
                current.raw,
                tail["hash"] if tail else None,
                tail["seq"] + 1 if tail else None,
                _has_axis(old_lines),
            )
            # Without an older segment the chain must start at its beginning.
            if parsed and tail is None and (parsed[0]["seq"] != 1 or parsed[0]["prev"] != _ZERO):
                parsed, reason = None, "round_ledger_sequence_gap"
            if parsed is None:
                self._bad = reason
                return
            lines = parsed
            self._identity = current.identity
            self._size = len(current.raw)
            self._cur_hash = hashlib.sha256(current.raw)
        last = lines or old_lines
        if last:
            self._last_hash, self._last_seq = last[-1]["hash"], last[-1]["seq"]
        self._old_lines, self._cur_lines = old_lines, lines
        if rotate and current.status == "ok" and self._size > ROTATE_BYTES:
            try:
                self._rotate_locked()
            except private_file.Replaced:
                self._bad = "round_ledger_replaced"
            except OSError:
                self._bad = "round_ledger_write_failed"

    @classmethod
    def open(cls, data_dir: Path | None) -> RoundLedger:
        """The ledger of ``data_dir``; never raises — a directory that cannot be read at
        all gives an unusable ledger (``round_ledger_unreadable``)."""
        try:
            return cls(data_dir)
        except Exception:  # noqa: BLE001 - unreadable: unusable, never a crash at start
            ledger = cls(None)
            ledger._bad = "round_ledger_unreadable"
            return ledger

    def _rotate_locked(self) -> None:
        """The current segment becomes the older one (replacing the one before); the next
        append creates a new current file that continues the chain. The file is checked
        against the identity this writer left **before** it is moved: a file replaced
        behind the writer is never moved over the older segment (#761)."""
        try:
            current = os.lstat(self._dir / LEDGER_FILE)
        except FileNotFoundError as error:
            raise private_file.Replaced("vanished") from error
        if (
            self._identity is None
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or epoch_store.identity(current) != self._identity
        ):
            raise private_file.Replaced("replaced")
        os.replace(self._dir / LEDGER_FILE, self._dir / OLD_LEDGER_FILE)
        from stiracore.durable_dir import fsync_dir

        fsync_dir(self._dir)
        moved = os.lstat(self._dir / OLD_LEDGER_FILE)
        if (
            self._identity is None
            or not stat.S_ISREG(moved.st_mode)
            or (moved.st_dev, moved.st_ino, moved.st_size) != self._identity[:3]
        ):
            raise private_file.Replaced("replaced")
        self._old_identity = epoch_store.identity(moved)
        self._old_digest = self._cur_hash.hexdigest()
        self._old_size = self._size
        self._identity = None
        self._size = 0
        self._cur_hash = hashlib.sha256()
        self._old_lines, self._cur_lines = self._cur_lines, []

    @property
    def unhealthy_reason(self) -> str | None:
        """A fixed code (:data:`UNHEALTHY_REASONS`) when the ledger cannot be written or
        refused a record in this process, else ``None``. Memory only."""
        if self._bad is not None:
            return self._bad
        return "round_ledger_record_refused" if self._refused else None

    @property
    def refused(self) -> int:
        """How many records the allow-list refused in this process."""
        return self._refused

    def mark_refused(self) -> None:
        """A record that could not even be built (the caller's failure): counted like a
        refused one, so the missing round stays visible."""
        with self._lock:
            self._refused += 1

    def records(self, grant_id: Any = None, *, kind: str | None = None) -> list[dict]:
        """The stored records, oldest first (:func:`public` form), of one grant / kind."""
        with self._lock:
            lines = [*self._old_lines, *self._cur_lines]
        return [
            public(line)
            for line in lines
            if (grant_id is None or line["grant"] == grant_id)
            and (kind is None or line["k"] == kind)
        ]

    def try_last(self, grant_id: Any, count: int) -> list[dict] | None:
        """The newest ``count`` ``round`` records of one grant (:func:`public` form,
        oldest first) without waiting: ``None`` when the ledger's lock is held (an append
        is flushing) — the Stage board's read."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            lines = [*self._old_lines, *self._cur_lines]
        finally:
            self._lock.release()
        rounds = [line for line in lines if line["grant"] == grant_id and line["k"] == "round"]
        return [public(line) for line in rounds[-count:]] if count > 0 else []

    def verify(self) -> str | None:
        """Re-read both segments and compare them with what this ledger read or wrote
        last. A difference makes the ledger unusable (sticky)."""
        if self._dir is None:
            return self._bad
        with self._lock:
            if self._bad is None:
                self._bad = self._verify_locked()
            return self._bad

    def _verify_locked(self) -> str | None:
        for name, identity, size, digest in (
            (OLD_LEDGER_FILE, self._old_identity, self._old_size, self._old_digest),
            (LEDGER_FILE, self._identity, self._size, self._cur_hash.hexdigest()),
        ):
            loaded = private_file.read(self._dir / name, max_bytes=_MAX_BYTES, prefix=_PREFIX)
            if loaded.status == "bad":
                return loaded.reason
            if identity is None:
                if loaded.status != "missing":
                    return "round_ledger_replaced"
                continue
            if loaded.status != "ok" or loaded.identity != identity:
                return "round_ledger_replaced"
            if len(loaded.raw) != size or hashlib.sha256(loaded.raw).hexdigest() != digest:
                return "round_ledger_replaced"
        return None

    def append(self, record: Any) -> dict:
        """Validate and append one record, durably (rotating first when the segment is
        full); the stored record (:func:`public` form). Raises
        :class:`LedgerSchemaError` (nothing written; counted, see :attr:`refused`) or
        ``OSError`` (the ledger is then unusable, sticky)."""
        try:
            fields = validate(record)
        except LedgerSchemaError:
            with self._lock:
                self._refused += 1
            raise
        with self._lock:
            if self._bad is not None:
                raise OSError(self._bad)
            line = {"v": VERSION, "seq": self._last_seq + 1, "prev": self._last_hash, **fields}
            line["hash"] = _digest(line)
            body = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
            if self._dir is not None:
                try:
                    if self._size and self._size + len(body) > ROTATE_BYTES:
                        self._rotate_locked()
                    self._identity = private_file.append_line(
                        self._dir / LEDGER_FILE, body, self._identity
                    )
                except private_file.Replaced as error:
                    self._bad = "round_ledger_replaced"
                    raise OSError(self._bad) from error
                except OSError:
                    self._bad = "round_ledger_write_failed"
                    raise
                self._size += len(body)
                self._cur_hash.update(body)
            self._last_seq, self._last_hash = line["seq"], line["hash"]
            self._cur_lines.append(line)
            if self._dir is None and len(self._cur_lines) > _MEMORY_LINES:
                del self._cur_lines[0]  # memory only: bounded like a segment
            return public(line)
