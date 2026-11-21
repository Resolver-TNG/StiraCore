"""Tool-call level repetition inside one turn (the "doom loop", #684).

The grant executor judges progress per iteration (:mod:`stiracore.grants.progress`); an
ordinary chat turn had nothing that counts its tool calls. This module adds the counting,
with the grant detector's vocabulary and constants:

* ``loop_detected`` — the turn's write calls keep going to the **same file**
  (:data:`WARN_AFTER` in a row; a write to another file starts over, calls that write
  nothing are not counted);
* ``same_blocker`` — the **same call** (tool and arguments) fails with the same failure
  class :data:`SAME_FAILURE_LIMIT` times with nothing changed in between (a successful
  write, the same call succeeding or failing differently starts over).

:func:`detect` is a pure function over the turn's calls; :func:`classify_failure` maps a
failed result to one of a few classes and :data:`FAILURE_ACTIONS` to what to do about it.
Neither calls a model.

:class:`DoomLoopHook` feeds them from the tool events of one agent. **By default it only
reports**: a :class:`Finding` goes to ``sink`` when a count reaches its notice or its stop
threshold. A finding holds fixed codes, counts and tool names (:meth:`Finding.as_dict`) —
never a path, an argument or a result text; the keys that compare calls are HMACs with a
random per-hook key, in memory only. Two switches, both **off by default**:

* ``nudge`` — append one fixed note to the tool result the finding belongs to (a tool
  result: the cached prompt prefix is untouched);
* ``stop`` — after a finding at the stop threshold, cancel further calls to the same file
  (``loop_detected``) or the same call (``same_blocker``) with a fixed text, until the
  turn ends or the streak is broken as above. It only ever cancels: a call another hook
  cancelled is left alone, no approval, rule or breaker decision is read or changed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

__all__ = [
    "FAILURE_ACTIONS",
    "FAILURE_CLASSES",
    "FAILURE_CUES",
    "FAILURE_STOP_AFTER",
    "HISTORY_KEPT",
    "NOTE_MARKER",
    "RULES",
    "SAME_FAILURE_LIMIT",
    "STOP_AFTER",
    "STOP_TEXT",
    "WARN_AFTER",
    "WRITE_TOOLS",
    "Call",
    "DoomLoopHook",
    "Finding",
    "classify_failure",
    "detect",
    "model_note",
]

_LOG = logging.getLogger(__name__)

RULES = ("loop_detected", "same_blocker")
# The grant detector's numbers (ADR 0011 §8), reused: same file ×3 → notice, ×6 → stop;
# same failure ×2 → notice, ×3 → stop.
# The numbers equal the grant detector's (``grants.progress``: SAME_BLOCKER_LIMIT, LOOP_WINDOW,
# NO_ACTION_LIMIT); a test pins that. They are written here so ``progress`` does not depend on
# ``grants`` (the grant side retuning its numbers must not silently retune the chat's).
WARN_AFTER = 3
STOP_AFTER = 6
SAME_FAILURE_LIMIT = 2
FAILURE_STOP_AFTER = 3
HISTORY_KEPT = 64  # calls of the running turn that are kept (memory only)
# The tools with a structured write path (``tools.identity_tools.PATH_WRITE_TOOLS``).
WRITE_TOOLS = frozenset({"file_write", "editor"})
_EDITOR_READS = frozenset({"view", "find_line"})
_CHECK_TOOL = "run_checks"

FAILURE_CLASSES = (
    "timeout",
    "invalid_args",
    "missing_info",
    "test_failed",
    "permission_denied",
    "other",
)
# What each class asks for (codes; the texts are in ``_ACTION_TEXT``).
FAILURE_ACTIONS: dict[str, str] = {
    "timeout": "wait_retry",
    "invalid_args": "repair_args",
    "missing_info": "gather_info",
    "test_failed": "fix_code",
    "permission_denied": "ask_person",
    "other": "change_approach",
}
# Fixed lower-case cues read in the head of an **error** result, first match wins. Only the
# class leaves this module, never the text.
FAILURE_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "permission_denied",
        (
            "permission denied",
            "operation not permitted",
            "read-only file system",
            "[refused by:",
            "not sent for approval",
        ),
    ),
    ("timeout", ("timed out", "timeout")),
    (
        "missing_info",
        ("no such file", "not found", "does not exist", "no match", "did not match"),
    ),
    (
        "invalid_args",
        ("validation", "invalid", "missing required", "unexpected keyword", "must be"),
    ),
)
_SCAN_CHARS = 4000
_CHECK_SCAN_CHARS = 65536  # a run_checks result is one JSON in a <check-output> block
_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_TRIED_MAX = 20

NOTE_MARKER = "[loop notice:"
_ACTION_TEXT: dict[str, str] = {
    "wait_retry": "It timed out again: wait or narrow the call before one more try.",
    "repair_args": "The arguments were refused again: correct them, do not resend them.",
    "gather_info": "Something it needs is missing: look it up (read, list, search) first.",
    "fix_code": "The checks fail the same way: change the code, not the tests.",
    "ask_person": "This needs a person: say what was refused and ask, do not retry.",
    "change_approach": "Repeating this has not helped: try a different approach.",
    "stop": "Stop repeating this. Tell the person what you tried and what is still open.",
}
STOP_TEXT = (
    "Not run: this turn repeated the same step without progress. Do not retry it. Tell the "
    "person what you tried and what is still open."
)


class Call(NamedTuple):
    """One finished tool call as the detector sees it (keys are opaque strings)."""

    tool: str
    args: str  # key of the tool and its arguments
    target: str = ""  # key of the written file ("" when the call writes none)
    write: bool = False
    failure: str = ""  # a FAILURE_CLASSES member, "" when the call succeeded


@dataclass(frozen=True)
class Finding:
    rule: str  # RULES
    level: str  # "notice" | "stop"
    count: int
    tool: str
    failure: str  # "" for loop_detected
    action: str
    tried: tuple[tuple[str, int, int], ...] = ()  # (tool, calls, failed) of the turn

    def as_dict(self) -> dict[str, Any]:
        """Codes, counts and tool names only (the allow-list a display may show)."""
        return {
            "rule": self.rule,
            "level": self.level,
            "count": self.count,
            "tool": self.tool,
            "failure": self.failure,
            "action": self.action,
            "tried": [
                {"tool": tool, "calls": calls, "failed": failed}
                for tool, calls, failed in self.tried
            ],
        }


def _label(name: Any) -> str:
    return name if type(name) is str and _NAME.fullmatch(name) else "other"


def _result_text(result: Mapping[str, Any], limit: int = _SCAN_CHARS) -> str:
    parts: list[str] = []
    for block in result.get("content") or ():
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"][:limit])
        if sum(map(len, parts)) >= limit:
            break
    return "\n".join(parts)[:limit]


def _check_status(text: str) -> str:
    """``status`` of a ``run_checks`` result: the JSON sits inside the ``<check-output>``
    block (a note line first), so cut it out rather than parse the whole text."""
    start, end = text.find("\n{"), text.rfind("}\n</check-output>")
    if start < 0 or end < start:
        return ""
    try:
        body = json.loads(text[start + 1 : end + 1])
    except ValueError:
        return ""
    return body.get("status", "") if isinstance(body, Mapping) else ""


def classify_failure(tool_name: Any, result: Any) -> str:
    """The failure class of one tool result, ``""`` for a call that did not fail.

    ``run_checks`` answers a failing check as a successful call whose JSON says
    ``"status": "failed"``: that is ``test_failed``. Every other class is read from an
    **error** result only (:data:`FAILURE_CUES`, else ``other``)."""
    if not isinstance(result, Mapping):
        return ""
    text = _result_text(result)
    if (
        tool_name == _CHECK_TOOL
        and _check_status(_result_text(result, _CHECK_SCAN_CHARS)) == "failed"
    ):
        return "test_failed"
    if result.get("status") != "error":
        return ""
    folded = text.casefold()
    for name, cues in FAILURE_CUES:
        if any(cue in folded for cue in cues):
            return name
    return "other"


def _tried(calls: Sequence[Call]) -> tuple[tuple[str, int, int], ...]:
    counts: dict[str, list[int]] = {}
    for call in calls:
        row = counts.setdefault(_label(call.tool), [0, 0])
        row[0] += 1
        row[1] += bool(call.failure)
    ordered = sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))
    return tuple((tool, row[0], row[1]) for tool, row in ordered[:_TRIED_MAX])


def detect(
    calls: Sequence[Call],
    *,
    warn_after: int = WARN_AFTER,
    stop_after: int = STOP_AFTER,
    same_failure: int = SAME_FAILURE_LIMIT,
    failure_stop_after: int = FAILURE_STOP_AFTER,
) -> Finding | None:
    """What the **last** of the turn's ``calls`` (oldest first) amounts to, or ``None``.

    Pure and deterministic. A failed last call is judged for ``same_blocker`` first, a
    write for ``loop_detected``; anything else (another file, a call that succeeds — also
    when it is repeated) is ``None``."""
    if not calls:
        return None
    last = calls[-1]
    if last.failure:
        count = 0
        for call in reversed(calls):
            if call.write and not call.failure:
                break  # something changed since
            if (call.tool, call.args) != (last.tool, last.args):
                continue
            if call.failure != last.failure:
                break
            count += 1
        if count >= same_failure:
            stop = count >= failure_stop_after
            return Finding(
                rule="same_blocker",
                level="stop" if stop else "notice",
                count=count,
                tool=_label(last.tool),
                failure=last.failure,
                action="stop" if stop else FAILURE_ACTIONS.get(last.failure, "change_approach"),
                tried=_tried(calls) if stop else (),
            )
    if last.write and last.target:
        count = 0
        for call in reversed(calls):
            if not call.write:
                continue
            if call.target != last.target:
                break
            count += 1
        if count >= warn_after:
            stop = count >= stop_after
            return Finding(
                rule="loop_detected",
                level="stop" if stop else "notice",
                count=count,
                tool=_label(last.tool),
                failure="",
                action="stop" if stop else "change_approach",
                tried=_tried(calls) if stop else (),
            )
    return None


def model_note(finding: Finding) -> str:
    """The one fixed line a ``nudge`` appends to the tool result (codes and a count)."""
    what = (
        f"edit {finding.count} of the same file in a row"
        if finding.rule == "loop_detected"
        else f"the same call failed {finding.count} times ({finding.failure})"
    )
    return f"{NOTE_MARKER} {finding.rule}] {what}. {_ACTION_TEXT[finding.action]}"


class DoomLoopHook(HookProvider):
    """Count one agent's tool calls per turn and report repetition (see the module text).

    ``sink(finding)`` is called once when a count reaches a threshold (notice, then stop).
    Nothing here raises into the turn; a failing sink is logged by its class name."""

    def __init__(
        self,
        sink: Callable[[Finding], Any] | None = None,
        *,
        warn_after: int = WARN_AFTER,
        stop_after: int = STOP_AFTER,
        same_failure: int = SAME_FAILURE_LIMIT,
        failure_stop_after: int = FAILURE_STOP_AFTER,
        nudge: bool = False,
        stop: bool = False,
    ) -> None:
        self.sink = sink
        self._limits = {
            "warn_after": max(2, int(warn_after)),
            "stop_after": max(2, int(stop_after)),
            "same_failure": max(2, int(same_failure)),
            "failure_stop_after": max(2, int(failure_stop_after)),
        }
        self.nudge = nudge is True
        self.stop = stop is True
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._calls: deque[Call] = deque(maxlen=HISTORY_KEPT)
        self._stopped_targets: set[str] = set()
        self._stopped_args: set[str] = set()
        self._own_cancels: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.begin)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    # -- keys (memory only) ---------------------------------------------------------------
    def _mac(self, *parts: str) -> str:
        material = "\x00".join(parts).encode("utf-8", "replace")
        return hmac.new(self._key, material, hashlib.sha256).hexdigest()

    def _keys(self, tool_use: Mapping[str, Any]) -> tuple[str, str, str, bool]:
        """``(tool, args key, write target key, write)`` of one call."""
        name = tool_use.get("name")
        tool = name if type(name) is str else "other"
        tool_input = tool_use.get("input")
        try:
            material = json.dumps(tool_input, sort_keys=True, default=repr, ensure_ascii=False)
        except (TypeError, ValueError):
            material = repr(type(tool_input))
        args = self._mac("args", tool, material)
        path = tool_input.get("path") if isinstance(tool_input, Mapping) else None
        write = (
            tool in WRITE_TOOLS
            and type(path) is str
            and bool(path)
            and not (tool == "editor" and tool_input.get("command") in _EDITOR_READS)
        )
        target = self._mac("path", os.path.normpath(path)) if write else ""
        return tool, args, target, write

    # -- the events -----------------------------------------------------------------------
    def begin(self, event: BeforeInvocationEvent) -> None:
        with self._lock:
            self._calls.clear()
            self._stopped_targets.clear()
            self._stopped_args.clear()
            self._own_cancels.clear()

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        if not self.stop:
            return
        try:
            if event.cancel_tool:
                return  # another hook's decision stands
            tool_use = event.tool_use if isinstance(event.tool_use, Mapping) else {}
            _tool, args, target, write = self._keys(tool_use)
            with self._lock:
                hit = args in self._stopped_args or (write and target in self._stopped_targets)
                if hit:
                    self._own_cancels.add(str(tool_use.get("toolUseId")))
            if hit:
                event.cancel_tool = STOP_TEXT
        except Exception as error:  # noqa: BLE001 - counting never breaks the call
            _LOG.warning("doom loop: before-tool check failed (%s)", type(error).__name__)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        try:
            finding = self._record(event)
        except Exception as error:  # noqa: BLE001
            _LOG.warning("doom loop: after-tool count failed (%s)", type(error).__name__)
            return
        if finding is None:
            return
        if self.nudge and isinstance(event.result, Mapping):
            content = list(event.result.get("content") or [])
            event.result = {**event.result, "content": [*content, {"text": model_note(finding)}]}
        if self.sink is not None:
            try:
                self.sink(finding)
            except Exception as error:  # noqa: BLE001 - a display must not break the turn
                _LOG.warning("doom loop: sink failed (%s)", type(error).__name__)

    def _record(self, event: AfterToolCallEvent) -> Finding | None:
        tool_use = event.tool_use if isinstance(event.tool_use, Mapping) else {}
        tool, args, target, write = self._keys(tool_use)
        failure = classify_failure(tool, event.result)
        call = Call(tool=tool, args=args, target=target, write=write, failure=failure)
        with self._lock:
            if str(tool_use.get("toolUseId")) in self._own_cancels:
                return None  # this hook's own cancel is not the agent's attempt
            if write and not failure:
                self._stopped_args.clear()
            if write:
                self._stopped_targets &= {target}
            self._calls.append(call)
            finding = detect(tuple(self._calls), **self._limits)
            if finding is None:
                return None
            if finding.level == "stop":
                if finding.rule == "loop_detected":
                    self._stopped_targets.add(target)
                else:
                    self._stopped_args.add(args)
        limits = self._limits
        edges = (
            (limits["warn_after"], limits["stop_after"])
            if finding.rule == "loop_detected"
            else (limits["same_failure"], limits["failure_stop_after"])
        )
        return finding if finding.count in edges else None
