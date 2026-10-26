"""What a progress report holds (ADR 0014 §3): harness facts and the agent's masked note.

**By construction** the facts read only tool names and statuses from the turn's events,
the turn's token counts and the YOLO window's own status numbers — never an argument, an
output, a command or memory content. The note is the agent's last text of the turn
(soft end), masked (``mask_secrets``), with fenced code blocks dropped and capped; a hard
end has no note.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from stiracore.observability.masking import mask_secrets

__all__ = ["NOTE_CHARS", "NOTE_LINES", "clean_note", "tool_counts", "usage_facts", "yolo_facts"]

NOTE_CHARS = 1200
NOTE_LINES = 12
TOOLS_MAX = 20
CODE_OMITTED = "[code omitted]"
_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)")
_FAILED = ("error", "denied", "timeout")
_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def tool_counts(events: Iterable[Mapping], skip: Iterable[str] = ()) -> dict[str, list[int]]:
    """``{name: [calls, failed]}`` from ``tool_call.*`` events (names only; at most 20).

    ``skip``: the ids the checkpoint cancelled (not the agent's work). A name that is not a
    plain tool name is counted as ``other``.
    """
    skipped = {str(item) for item in skip}
    latest: dict[str, tuple[str, str]] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") not in (
            "tool_call.created",
            "tool_call.updated",
        ):
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        use_id, name, status = data.get("tool_use_id"), data.get("name"), data.get("status")
        if type(use_id) is not str or use_id in skipped or type(status) is not str:
            continue
        label = name if type(name) is str and _NAME.fullmatch(name) else "other"
        latest[use_id] = (label, status)
    counts: dict[str, list[int]] = {}
    for label, status in latest.values():
        row = counts.setdefault(label, [0, 0])
        row[0] += 1
        row[1] += status in _FAILED
    ordered = sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))
    return {name: row for name, row in ordered[:TOOLS_MAX]}


def usage_facts(usage: Mapping | None) -> dict[str, int]:
    usage = usage if isinstance(usage, Mapping) else {}
    out = {}
    for key, source in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache_read", "cache_read_tokens"),
        ("cache_write", "cache_write_tokens"),
    ):
        value = usage.get(source)
        if type(value) is int and value >= 0:
            out[key] = value
    return out


def yolo_facts(window: Any) -> dict[str, Any] | None:
    """The window's own numbers (the page's YOLO badge reads the same status)."""
    if not isinstance(window, Mapping):
        return None
    out: dict[str, Any] = {}
    for key, source in (
        ("remaining_sec", "remaining_sec"),
        ("used", "used"),
        ("limit", "limit"),
        ("external", "external_used"),
        ("external_limit", "external_limit"),
        ("exec", "exec_used"),
        ("exec_limit", "exec_limit"),
    ):
        value = window.get(source)
        if type(value) is int:
            out[key] = value
    for key in ("tier", "state", "pause_reason"):
        value = window.get(key)
        if type(value) is str and _NAME.fullmatch(value):
            out[key] = value
    return out


def clean_note(text: Any) -> str | None:
    """The agent's report text for the board: masked, no code blocks, at most 12 lines."""
    if type(text) is not str:
        return None
    text = _FENCE.sub(CODE_OMITTED, text)
    text = mask_secrets(text)
    lines = [line.rstrip() for line in text.strip().splitlines()]
    lines = [line for index, line in enumerate(lines) if line or (index and lines[index - 1])]
    if len(lines) > NOTE_LINES:
        lines = lines[:NOTE_LINES] + ["…"]
    note = "\n".join(lines).strip()
    if len(note) > NOTE_CHARS:
        note = note[: NOTE_CHARS - 1].rstrip() + "…"
    return note or None
