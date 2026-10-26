"""Thread-safe, budget-aware context assembly.

Memory stores implement the small structural ``MemoryStore`` protocol below.
Entries must be returned newest first; adapters can translate this contract to
a backend's native query API.

This module never calls ``pre_turn``: doing so would acquire an extra engine
snapshot and potentially trigger breaker callbacks. Supply a read-only
``breaker_state_provider`` when the engine does not expose ``last_breaker``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum, Flag
from itertools import islice
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar, Literal, Protocol

from stiracore.agent.prompt_cache import PromptParts
from stiracore.core import StiraEngine
from stiracore.procedural_provenance import (
    E_PROCEDURAL_UNAPPROVED,
    E_PROCEDURAL_WINDOW_LIMIT,
    is_staged_key,
    provenance_problem,
)

logger = logging.getLogger(__name__)

# Upper bound of entries read to find ``procedural_limit`` approved rules (#144).
_PROCEDURAL_WINDOW_MAX = 1024
# Opt-in associative recall (#170); like Episodic Memory it goes into the recalled part.
ASSOCIATED_MEMORY = "Associated Memory"
# SOUL (0) and Identity Anchors (1) are never shortened by the budget (ADR 0026 review B3).
_PROTECTED_PRIORITY = 1
# The bounded index of the layers (tools/memory_index.MemoryMap): recalled part, framed.
MEMORY_MAP = "Memory Map"
# The newest handoff note (wave 23); framed like Associated Memory, in the recalled part.
HANDOFF_NOTE = "Handoff Note"
_RECALLED_TITLES = frozenset({"Episodic Memory", ASSOCIATED_MEMORY, MEMORY_MAP, HANDOFF_NOTE})
# Framed sections (preamble + delimiters): dropped whole, never cut in the middle.
_FRAMED_TITLES = frozenset({ASSOCIATED_MEMORY, MEMORY_MAP, HANDOFF_NOTE})
# Handoff notes (``handoff/`` keys) are never shown as plain episodes: model-written text
# reaches the prompt only through the framed Handoff Note section. When some are skipped,
# this many more entries are read so that the limit can still be filled.
_HANDOFF_KEY_PREFIX = "handoff/"
_HANDOFF_SKIP_EXTRA = 8
# With a known model window, protected text above this share of it fails the turn (B1): some
# local servers silently drop the oldest input (the SOUL) of a request that does not fit.
PROTECTED_WINDOW_SHARE = 0.9


class ProtectedContextOverflow(RuntimeError):
    """The smallest request that is always sent (SOUL + Identity Anchors + the tool
    definitions, no history) would not fit the model's context window."""

    # Read by providers.failure: the chat shows this fixed reason, never the message.
    stira_failure_reason = "context_window_too_small"

    def __init__(self, estimated_tokens: int, window_tokens: int, tool_tokens: int = 0) -> None:
        self.estimated_tokens = estimated_tokens
        self.window_tokens = window_tokens
        self.tool_tokens = tool_tokens
        super().__init__(
            f"SOUL and Identity Anchors (~{estimated_tokens} tokens) plus the tool definitions "
            f"(~{tool_tokens} tokens) exceed {PROTECTED_WINDOW_SHARE:.0%} of the model's "
            f"{window_tokens}-token context window; the turn was not sent. Increase the context "
            "window, shorten the SOUL, use a model with a larger window, or, when the tool "
            "definitions are large, try context_profile=small or tiny."
        )


def tool_definition_tokens(tools: Iterable[Any]) -> int:
    """Estimated tokens of the tool definitions sent with a request (their JSON specs)."""
    total = 0
    for tool in tools:
        spec = getattr(tool, "tool_spec", None)
        if isinstance(spec, Mapping):
            total += estimate_tokens(json.dumps(spec, ensure_ascii=False, default=str))
    return total


class MemoryStoreProtocol(Protocol):
    """Minimal memory query contract used by ContextAssembler.

    Compatible with the real MemoryStore(layer, limit) signature.
    """

    def list_entries(
        self,
        layer: Any,
        limit: int = 20,
    ) -> Iterable[Any]:
        """Return newest-first entries with a ``content`` field."""
        ...


# Token estimate, in quarter tokens (docs/token-estimate.md). Measured 2026-10-23 on Japanese
# text (sample SOUL, episodes, docs) with o200k, cl100k, Qwen 2.5/3, Gemma 3, Llama 3.2 and
# Mistral v0.3/Nemo: a kana/kanji/full-width character costs 0.63-1.39 tokens (0.63-0.98 on
# the newer tokenizers), so it counts as one whole token. Every other character keeps the
# historical four per token, so text without these characters estimates exactly as before.
_QUARTERS_PER_TOKEN = 4
_WIDE_CHAR_QUARTERS = 4
_WIDE_CHAR = re.compile(
    "[\u3000-\u30ff"  # CJK symbols/punctuation, hiragana, katakana
    "\u31f0-\u31ff"  # katakana phonetic extensions
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK ideographs (ext. A, unified, compat.)
    "\uff00-\uffef"  # full-width forms, half-width katakana
    "\U00020000-\U0003134f]"  # CJK ideograph extensions B-G
)


def _quarters(text: str) -> int:
    """Estimated size in quarter tokens: 1 per character, 4 per wide (CJK) character."""
    return len(text) + (_WIDE_CHAR_QUARTERS - 1) * len(_WIDE_CHAR.findall(text))


def estimate_tokens(text: str) -> int:
    """Estimate tokens, rounded up: four characters per token, one per CJK character."""
    return (_quarters(text) + _QUARTERS_PER_TOKEN - 1) // _QUARTERS_PER_TOKEN


def _tail_chars(text: str, quarters: int) -> int:
    """Fewest trailing characters of ``text`` whose estimate covers ``quarters``."""
    removed = 0
    for count, char in enumerate(reversed(text), start=1):
        removed += _WIDE_CHAR_QUARTERS if _WIDE_CHAR.match(char) else 1
        if removed >= quarters:
            return count
    return len(text)


def _validate_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    if callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Cannot render context value of type {type(value).__name__}")


def _flag_names(value: Flag) -> list[str]:
    """Name each set member; json.dumps would otherwise emit an opaque int."""
    return [
        member.name
        for member in type(value)
        if member.value and member.name and (value & member) == member
    ]


def _readable(value: Any) -> Any:
    """Pre-convert Flag values, which json treats as ints and never defaults."""
    if isinstance(value, Flag):
        return _flag_names(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _readable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {key: _readable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_readable(item) for item in value]
    return value


def _snapshot_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        _readable(value),
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


@dataclass(frozen=True)
class _Section:
    title: str
    content: str
    priority: int


class ContextAssembler:
    """Assemble SOUL, memory, identity, neural state, and breaker context.

    Startup sections are captured once during construction. Dynamic sections
    are read afresh on each turn-context or system-prompt build.

    Priority, highest first: SOUL, Identity, Breaker, Neural State, Rules,
    Episodic, then the opt-in Associated Memory (#170). Neural State sits
    immediately below Breaker because the required
    ordering does not otherwise specify its priority.

    The lock serializes this assembler's cache and memory-store access.
    External engine/state writers must provide their own synchronization;
    this lock cannot make unsynchronized mutations of those objects atomic.
    """

    def __init__(
        self,
        agent_id: str,
        memory_store: MemoryStoreProtocol,
        engine: StiraEngine,
        soul_path: Path | None = None,
        *,
        max_tokens: int = 8000,
        episodic_limit: int = 5,
        procedural_limit: int = 5,
        breaker_state_provider: Callable[[], Any] | None = None,
        include_identity: bool = True,
        associative_recall: Any | None = None,
        memory_map: Any | None = None,
        handoff_note: Any | None = None,
        context_window_tokens: int | Callable[[], int | None] | None = None,
    ) -> None:
        _validate_limit("max_tokens", max_tokens)
        if context_window_tokens is not None and not callable(context_window_tokens):
            _validate_limit("context_window_tokens", context_window_tokens)
        _validate_limit("episodic_limit", episodic_limit)
        _validate_limit("procedural_limit", procedural_limit)

        self.agent_id = agent_id
        self.memory_store = memory_store
        self.engine = engine
        self.soul_path = Path(soul_path) if soul_path is not None else Path("soul.md")
        self.max_tokens = max_tokens
        self.episodic_limit = episodic_limit
        self.procedural_limit = procedural_limit
        self._breaker_state_provider = breaker_state_provider
        # Opt-in read-side recall (#170): an object with ``section(exclude_keys)``. None =
        # no "Associated Memory" section; the prompt is byte-identical to before.
        self._associative_recall = associative_recall
        # The Memory Map: an object with ``section()`` (tools/memory_index.MemoryMap). None =
        # no "Memory Map" section; the prompt is byte-identical to before.
        self._memory_map = memory_map
        # Handoff note (wave 23): an object with ``section()`` and ``shown_key``. None = no
        # "Handoff Note" section and Episodic Memory as before (byte-identical prompt).
        self._handoff_note = handoff_note
        self._lock = RLock()
        self._protected_overflow_warned = False
        self.include_identity = include_identity
        # The model's window when known (B1), or a callable read on every build (the model can
        # be switched at run time); None = unknown, only the over-budget warning.
        self.context_window_tokens = context_window_tokens

        with self._lock:
            # Hosts whose turn executor already prepends a live identity notice
            # (NXPAgent on INJECT_IDENTITY) pass include_identity=False; otherwise
            # this startup-frozen copy would duplicate it and go stale.
            identity = (self.engine.identity_injection() or "") if include_identity else ""
            self._startup_sections = (
                _Section("SOUL", self.load_soul(), 0),
                _Section("Identity Anchors", identity, 1),
                _Section(
                    "Procedural Rules",
                    self.load_procedural_rules(self.procedural_limit),
                    4,
                ),
            )

    def load_soul(self) -> str:
        """Read soul.md, returning empty text when it is missing.

        Permission and decoding errors are deliberately not hidden.
        """
        with self._lock:
            try:
                return self.soul_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ""

    _LAYER_MAP: ClassVar[dict[str, None]] = {
        "procedural": None,  # resolved at runtime from MemoryLayer
        "episodic": None,
    }

    @staticmethod
    def _resolve_layer(memory_type: str) -> Any:
        """Convert string to MemoryLayer enum if available."""
        try:
            from stiracore.tools.memory_store import MemoryLayer

            return MemoryLayer(memory_type)
        except (ImportError, ValueError):
            return memory_type

    def _memory_text(
        self,
        memory_type: Literal["procedural", "episodic"],
        limit: int,
    ) -> str:
        if memory_type == "procedural":
            # Rules always go through the provenance filter (#144).
            return self._procedural_text(limit)
        if limit == 0:
            return ""

        return self._episodic(limit, memory_type)[0]

    def _is_handoff(self, entry: Any) -> bool:
        key = self._entry_field(entry, "key")
        return isinstance(key, str) and key.startswith(_HANDOFF_KEY_PREFIX)

    def _episodic(self, limit: int, memory_type: str = "episodic") -> tuple[str, list[str]]:
        """The newest ``limit`` entries of ``memory_type`` as prompt text, and their keys.

        ``handoff/`` notes are left out (with the feature on or off): the Handoff Note
        section shows the newest one, framed. Only when one was skipped are a few more
        entries read, so the store sees the same call as before otherwise.
        """
        if limit == 0:
            return "", []
        layer = self._resolve_layer(memory_type)
        entries = list(islice(self.memory_store.list_entries(layer, limit), limit))
        if any(self._is_handoff(entry) for entry in entries):
            fetch = limit + _HANDOFF_SKIP_EXTRA
            entries = list(islice(self.memory_store.list_entries(layer, fetch), fetch))
        entries = [entry for entry in entries if not self._is_handoff(entry)]
        contents: list[str] = []
        keys: list[str] = []
        # Enforce the limit even if a backend returns too many entries.
        for entry in islice(entries, limit):
            if isinstance(entry, Mapping):
                content = entry["content"]
            else:
                content = entry.content
            if not isinstance(content, str):
                raise TypeError("Memory entry content must be a string")
            if content.strip():
                contents.append(f"- {content.strip()}")
                key = self._entry_field(entry, "key")
                if isinstance(key, str):
                    keys.append(key)
        return "\n".join(contents), keys

    @staticmethod
    def _entry_field(entry: Any, name: str) -> Any:
        if isinstance(entry, Mapping):
            return entry.get(name)
        return getattr(entry, name, None)

    def _procedural_text(self, limit: int) -> str:
        """The newest ``limit`` procedural rules that carry approval provenance (#144).

        Proposals under ``staging/`` and entries without valid ``source_turn`` /
        ``approved_by`` / ``approved_at`` / ``review`` are not read; the skipped
        entries are logged with a fixed code and their number only. The window
        grows so pending proposals or legacy entries cannot crowd approved rules
        out, up to ``_PROCEDURAL_WINDOW_MAX`` entries. Beyond that, older approved
        rules can be left out; this is logged as ``E_PROCEDURAL_WINDOW_LIMIT``.
        """
        if limit == 0:
            return ""
        layer = self._resolve_layer("procedural")
        window = limit
        overflow = False
        while True:
            # At the cap, ask for one more entry to tell "all seen" from "more exist".
            fetch = window + 1 if window >= _PROCEDURAL_WINDOW_MAX else window
            entries = list(islice(self.memory_store.list_entries(layer, fetch), fetch))
            overflow = len(entries) > window
            entries = entries[:window]
            contents: list[str] = []
            skipped = 0
            for entry in entries:
                content = self._entry_field(entry, "content")
                if not isinstance(content, str):
                    raise TypeError("Memory entry content must be a string")
                if not content.strip() or is_staged_key(self._entry_field(entry, "key")):
                    continue
                if provenance_problem(self._entry_field(entry, "metadata")) is not None:
                    skipped += 1
                    continue
                contents.append(f"- {content.strip()}")
                if len(contents) >= limit:
                    break
            if len(contents) >= limit or len(entries) < window or window >= _PROCEDURAL_WINDOW_MAX:
                break
            window = min(window * 2, _PROCEDURAL_WINDOW_MAX)
        if skipped:
            logger.warning(
                "Procedural entries without approval provenance were not read (%s): %d",
                E_PROCEDURAL_UNAPPROVED,
                skipped,
            )
        if len(contents) < limit and overflow:
            logger.warning(
                "Only the newest %d procedural entries were searched for approved rules; "
                "older ones may be left out (%s)",
                _PROCEDURAL_WINDOW_MAX,
                E_PROCEDURAL_WINDOW_LIMIT,
            )
        return "\n".join(contents)

    def load_procedural_rules(self, limit: int = 5) -> str:
        """Read the latest approved procedural entries for this agent (#144)."""
        _validate_limit("limit", limit)
        with self._lock:
            return self._procedural_text(limit)

    def _turn_sections(self) -> tuple[_Section, ...]:
        neural_state = _snapshot_text(self.engine.state)

        if self._breaker_state_provider is not None:
            breaker = self._breaker_state_provider()
        else:
            breaker = getattr(self.engine, "last_breaker", None)

        breaker_text = (
            _snapshot_text(breaker)
            if breaker is not None
            else "Unavailable: no read-only breaker snapshot has been supplied."
        )
        handoff = self._handoff_note
        episodic, shown = self._episodic(self.episodic_limit)
        sections = (
            _Section("Neural State", neural_state, 3),
            _Section("Episodic Memory", episodic, 5),
            _Section("Breaker", breaker_text, 2),
        )
        if handoff is not None:
            # Above Episodic Memory (whose tail is trimmed first); dropped whole if short.
            note = handoff.section()
            sections = (*sections, _Section(HANDOFF_NOTE, note, 4))
            if note and getattr(handoff, "shown_key", None):
                shown = [*shown, handoff.shown_key]
        if self._associative_recall is not None:
            # Low priority: trimmed first when the budget is short (#170).
            associated = self._associative_recall.section(exclude_keys=shown)
            sections = (*sections, _Section(ASSOCIATED_MEMORY, associated, 6))
        if self._memory_map is not None:
            # Lowest priority: the first section dropped (whole) when the budget is short.
            sections = (*sections, _Section(MEMORY_MAP, self._memory_map.section(), 7))
        return sections

    @staticmethod
    def _render(sections: list[_Section]) -> str:
        return "\n\n".join(f"## {section.title}\n{section.content}" for section in sections)

    def check_protected_fits(
        self, window: int | None, extra_tokens: int = 0, prepended_identity: str = ""
    ) -> None:
        """Fail before sending when the request's floor would not fit a known window (B1).

        The floor is what every request carries even without history: SOUL + Identity
        Anchors (never shortened) and ``extra_tokens`` (normally the tool definitions this
        turn sends). ``prepended_identity`` is the identity notice the host prepends itself
        (NXPAgent on INJECT_IDENTITY); it counts only when this assembler leaves identity
        out (``include_identity=False``), otherwise its own section already holds the
        anchors and nothing is counted twice. ``window`` None = unknown: nothing to check
        (``_fit`` only warns).
        """
        if type(window) is not int or window <= 0:
            return
        with self._lock:
            protected = self._render(
                [
                    _Section(s.title, s.content.strip(), s.priority)
                    for s in self._startup_sections
                    if s.priority <= _PROTECTED_PRIORITY and s.content.strip()
                ]
            )
        if prepended_identity and not self.include_identity:
            # As NXPAgent prepends it: "<notice>\n\n<prompt>".
            protected = f"{prepended_identity}\n\n{protected}" if protected else prepended_identity
        tokens = estimate_tokens(protected)
        if tokens + max(0, extra_tokens) > PROTECTED_WINDOW_SHARE * window:
            raise ProtectedContextOverflow(tokens, window, max(0, extra_tokens))

    def _fit(self, sections: Iterable[_Section], max_tokens: int | None = None) -> str:
        """Trim section tails, lowest priority first, including header costs.

        SOUL and Identity Anchors are never shortened: when they alone exceed the budget,
        every other section is dropped and they are returned whole, over budget. When the
        model's window is known and they exceed 90% of it, ``ProtectedContextOverflow``.
        """
        active = [
            _Section(section.title, section.content.strip(), section.priority)
            for section in sections
            if section.content.strip()
        ]
        budget = (self.max_tokens if max_tokens is None else max_tokens) * _QUARTERS_PER_TOKEN
        window = self.context_window_tokens
        if callable(window):
            window = window()
        if type(window) is int and window > 0:
            protected = self._render([s for s in active if s.priority <= _PROTECTED_PRIORITY])
            quarters = _quarters(protected)
            if protected and quarters > PROTECTED_WINDOW_SHARE * window * _QUARTERS_PER_TOKEN:
                raise ProtectedContextOverflow(estimate_tokens(protected), window)

        while active:
            rendered = self._render(active)
            # In quarter tokens; for text without CJK characters this is the character count.
            excess = _quarters(rendered) - budget
            if excess <= 0:
                return rendered

            index = max(
                range(len(active)),
                key=lambda position: (active[position].priority, position),
            )
            section = active[index]
            if section.priority <= _PROTECTED_PRIORITY:
                # Only protected sections are left: send them intact rather than cut the SOUL.
                if not self._protected_overflow_warned:
                    self._protected_overflow_warned = True
                    logger.warning(
                        "SOUL and Identity Anchors alone exceed the context budget "
                        "(~%d > %d tokens); sent whole, memory sections dropped",
                        estimate_tokens(rendered),
                        budget // _QUARTERS_PER_TOKEN,
                    )
                return rendered
            # Associated Memory (#170), the Memory Map and the Handoff Note are framed
            # (preamble + delimiters): never cut in half.
            if excess >= _quarters(section.content) or section.title in _FRAMED_TITLES:
                # Removing the whole section also releases its header/separator.
                del active[index]
            else:
                shortened = section.content[: -_tail_chars(section.content, excess)].rstrip()
                if shortened:
                    active[index] = _Section(section.title, shortened, section.priority)
                else:
                    del active[index]
        return ""

    def build_startup_context(self) -> str:
        """Return the budgeted, cached SOUL, rules, and identity context."""
        with self._lock:
            return self._fit(self._startup_sections)

    def build_turn_context(self) -> str:
        """Return budgeted fresh neural, episodic, and breaker context."""
        with self._lock:
            return self._fit(self._turn_sections())

    def build_system_prompt(self) -> str:
        """Combine startup and fresh turn sections under one shared budget."""
        with self._lock:
            # Fit original sections together, rather than concatenating two
            # independently truncated prompts.
            return self._fit((*self._startup_sections, *self._turn_sections()))

    def build_prompt_parts(self) -> PromptParts:
        """Static, control and recalled parts for prompt caching (#254).

        ``static`` (SOUL, rules) has its own budget, so it never depends on the
        per-turn parts and stays byte-identical across turns. ``volatile`` is
        harness-determined control state (Neural State, Breaker); ``recalled`` is
        Episodic Memory, which can quote users and is kept apart so that it is
        never presented with the control state's authority. The two per-turn
        parts share one budget; Episodic Memory (lowest priority) gets what the
        control state leaves, as in :meth:`build_system_prompt`.
        """
        with self._lock:
            sections = self._turn_sections()
            control = self._fit(s for s in sections if s.title not in _RECALLED_TITLES)
            remaining = max(0, self.max_tokens - estimate_tokens(control))
            recalled = self._fit((s for s in sections if s.title in _RECALLED_TITLES), remaining)
            return PromptParts(
                static=self._fit(self._startup_sections),
                volatile=control,
                recalled=recalled,
            )
