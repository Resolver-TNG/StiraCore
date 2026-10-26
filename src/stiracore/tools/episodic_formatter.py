"""Deterministic, model-free Episodic Memory summaries (#77).

Each chat turn used to be persisted as ``message[:100]`` / ``response[:200]``,
which cut Japanese sentences mid-word, dropped conclusions that came after
the first 200 characters and stored raw tool output, secrets and code.

This module replaces that with a rule-based formatter:

1. Sanitize: redact secret-looking values, drop tool-output envelopes and
   ANSI escapes, replace large code blocks with a placeholder and shorten
   long URLs atomically.
2. Segment: split into meaning units -- paragraphs, bullet lines, and
   sentences (Japanese ``。！？`` and English ``. ! ?`` followed by space).
   URLs, inline code and placeholders are protected so a split never lands
   inside them.
3. Select: keep whole units under a per-speaker budget. The user side keeps
   its opening and a trailing question; the agent side prefers
   conclusion-marked units, then its lead and final unit. Gaps become ``…``.
4. Hard cap: only if a single unit still exceeds the budget is it cut, at a
   clause boundary where possible and never inside a URL.

Why no LLM: this runs after every turn. A Bedrock summarization call would
double per-turn cost and latency and add a failure mode to every turn. The
``record_episode`` seam accepts any formatter object, so a model-backed
summarizer can be plugged in later; the deterministic ``fallback_format``
still guarantees an entry is written if it fails.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from stiracore.core.amygdala import SCORING_VERSION, AmygdalaPass
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin

from .episodic_emotion import EmotionSnapshot, write_emotion_snapshot
from .memory_store import MemoryEntry, MemoryLayer

logger = logging.getLogger(__name__)

FORMAT_VERSION = "episodic-summary/v1"
# v1 plus an emotion snapshot captured at save time, kept on the floor (#364,
# ``episodic_emotion``). v1 episodes stay readable; their snapshot reads as None.
FORMAT_VERSION_V2 = "episodic-summary/v2"
ELLIPSIS = "…"

# --------------------------------------------------------------------------
# Sanitization patterns
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# Standalone credential shapes. Replaced wholesale.
_SECRET_TOKENS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
)
# key=value / key: value credentials, including JSON/dict quoted keys.
# Keep the key and separator; consume escaped quotes and whitespace in values.
_SECRET_ASSIGN = re.compile(
    r"(?i)(?<!\w)(?P<key>(?P<quote>[\"']?)(?:aws_)?"
    r"(?:secret(?:_access)?_key|secret|password|passwd|pwd|credentials?|"
    r"api[_-]?key|access[_-]?key|auth[_-]?token|access[_-]?token|token|"
    r"client[_-]?secret|session[_-]?token)(?P=quote))"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s&,;\"'{}\[\]、。]+)"
)
# Restrict userinfo to the URL authority, never a path/query containing '@'.
# Run before token/assignment masking so those replacements cannot split it.
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@?#<>\"'`]+@")
REDACTED = "[REDACTED]"

_TOOL_ENVELOPE = re.compile(
    r"<(tool_result|tool_use|function_results|function_calls|toolResult|toolUse)\b[^>]*>"
    r".*?(?:</\1>|\Z)",
    re.DOTALL,
)
_FENCE = re.compile(
    r"^[ \t]*(```|~~~)([^\n`]*)\n(.*?)(?:^[ \t]*\1[ \t]*$|\Z)", re.DOTALL | re.MULTILINE
)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_URL = re.compile(r"https?://[^\s<>\"'`）」』】、。]+")
# A line that is a bare JSON/log dump rather than prose.
_JSON_LINE = re.compile(r"^\s*[\[{].*[\]}]\s*,?\s*$")

_BULLET = re.compile(r"^\s*(?:[-*+]\s+|[•・]\s*|\d{1,3}[.)]\s+|[①-⑳]\s*)")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_EMPHASIS = re.compile(r"\*\*|__")
# Sentence end: Japanese terminators (with optional closing brackets), ``!``/``?``,
# or an English period followed by whitespace (so ``3.14`` and ``v1.2`` survive).
# URLs and inline code are protected tokens, so a split never falls inside them.
_SENTENCE_END = re.compile(r"[。！？!?]+[」』）)\"']*|\.(?=\s)")
_CLAUSE_BOUNDARY = re.compile(r"[、，,;；:：]\s*|\s+")

_CONCLUSION = re.compile(
    r"結論|つまり|要するに|まとめると|まとめ|したがって|従って|よって|以上から|以上より|"
    r"結果として|最終的に|ということで|おすすめ|推奨|"
    r"(?i:\bin short\b|\bin summary\b|\btherefore\b|\bconclusion\b|\btl;?dr\b|\boverall\b)"
)
_QUESTION_END = re.compile(r"[?？][」』）)]*$|(?:か|かな|ですか|ますか|でしょうか)[。]?$")

_TOKEN = "\x00{}\x00"
_TOKEN_RE = re.compile(r"\x00(\d+)\x00")
_CJK = r"\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff01-\uff60"
_NO_SPACE_JOIN = re.compile(rf"[{_CJK}]$")
# After Japanese text, the next unit joins directly unless it is a bullet,
# placeholder or ellipsis, which read better with a separating space.
_CJK_START = re.compile(r"^(?![・\[…\s])")


def _join(parts: list[str]) -> str:
    """Join units with a space, except between two Japanese/CJK pieces."""
    out = ""
    for part in parts:
        if not out:
            out = part
        elif _NO_SPACE_JOIN.search(out) and _CJK_START.match(part):
            out += part
        else:
            out += " " + part
    return out


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


@dataclass
class _Stats:
    redactions: int = 0
    code_blocks: int = 0
    tool_outputs: int = 0


@dataclass(frozen=True)
class SpeakerSummary:
    text: str
    original_chars: int
    truncated: bool


@dataclass(frozen=True)
class EpisodeSummary:
    """A formatted episode: the stored content plus structured metadata."""

    content: str
    user: SpeakerSummary
    agent: SpeakerSummary
    method: str = "deterministic"
    stats: Mapping[str, int] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        # Raw text is never duplicated into metadata.
        return {
            "format": FORMAT_VERSION,
            "method": self.method,
            "user_chars": self.user.original_chars,
            "agent_chars": self.agent.original_chars,
            "user_truncated": self.user.truncated,
            "agent_truncated": self.agent.truncated,
            **{key: value for key, value in self.stats.items() if value},
        }


class EpisodeFormatterProtocol(Protocol):
    def format(self, user_message: str, agent_response: str) -> EpisodeSummary: ...


# --------------------------------------------------------------------------
# Sanitization / segmentation helpers
# --------------------------------------------------------------------------


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace credential-looking values; return (text, replacement count)."""
    count = 0

    def _sub_whole(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return REDACTED

    def _sub_value(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group('key')}{match.group('separator')}{REDACTED}"

    def _sub_userinfo(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}***:***@"

    text = _URL_USERINFO.sub(_sub_userinfo, text)
    text = _PRIVATE_KEY.sub(_sub_whole, text)
    for pattern in _SECRET_TOKENS:
        text = pattern.sub(_sub_whole, text)
    text = _SECRET_ASSIGN.sub(_sub_value, text)
    return text, count


def shorten_url(url: str, max_chars: int = 60) -> str:
    """Shorten a URL as an atomic unit: keep scheme+host, elide the path."""
    if len(url) <= max_chars:
        return url
    match = re.match(r"(https?://[^/?#]+)(.*)", url)
    if not match:
        return url[: max_chars - 1] + ELLIPSIS
    base, rest = match.groups()
    if len(base) + 2 >= max_chars:
        return base + "/" + ELLIPSIS
    keep = max_chars - len(base) - 1
    # Keep whole leading path segments only.
    segments = re.split(r"(?<=/)", rest)
    kept = ""
    for segment in segments:
        if len(kept) + len(segment) > keep:
            break
        kept += segment
    if not kept:
        kept = "/"
    elif not kept.endswith("/"):
        kept += "/"
    return base + kept + ELLIPSIS


def _clean_line_markup(line: str) -> str:
    line = _HEADING.sub("", line)
    line = _QUOTE.sub("", line)
    line = _EMPHASIS.sub("", line)
    return line.strip()


class _Protector:
    """Swap URLs, inline code and placeholders for split-proof tokens."""

    def __init__(self) -> None:
        self._values: list[str] = []

    def protect(self, value: str) -> str:
        self._values.append(value)
        return _TOKEN.format(len(self._values) - 1)

    def restore(self, text: str) -> str:
        # A protected value may itself hold an earlier token (a URL inside inline code):
        # restore until none is left. Each value only holds lower-numbered tokens (the
        # input has no NUL, see ``_prepare``), so this ends; the bound is a backstop.
        for _ in range(len(self._values) + 1):
            if not _TOKEN_RE.search(text):
                break
            text = _TOKEN_RE.sub(lambda match: self._values[int(match.group(1))], text)
        return _TOKEN_RE.sub("", text)


def _prepare(text: str, stats: _Stats, url_chars: int, inline_code_chars: int) -> list[str]:
    """Sanitize ``text`` and split it into restored meaning units."""
    # NUL is the token delimiter: an input NUL could forge or break a token.
    text = text.replace("\x00", "")
    text = _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text, redactions = redact_secrets(text)
    stats.redactions += redactions

    protector = _Protector()

    def _tool(_: re.Match[str]) -> str:
        stats.tool_outputs += 1
        return "\n\n" + protector.protect("[ツール出力 省略]") + "\n\n"

    text = _TOOL_ENVELOPE.sub(_tool, text)

    def _code(match: re.Match[str]) -> str:
        body = match.group(3).rstrip("\n")
        language = match.group(2).strip()
        lines = body.count("\n") + 1 if body else 0
        stats.code_blocks += 1
        label = f"{language} " if language else ""
        return "\n\n" + protector.protect(f"[コード: {label}{lines}行 省略]") + "\n\n"

    text = _FENCE.sub(_code, text)
    text = _URL.sub(lambda match: protector.protect(shorten_url(match.group(0), url_chars)), text)

    def _inline(match: re.Match[str]) -> str:
        # A URL inside the code is already a token: put it back first, so the code is
        # one protected value (no nested token) and its length is the real one.
        code = protector.restore(match.group(0))
        if len(code) > inline_code_chars:
            code = "`" + code[1 : inline_code_chars - 2] + ELLIPSIS + "`"
        return protector.protect(code)

    text = _INLINE_CODE.sub(_inline, text)

    units: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n+", text):
        prose: list[str] = []

        for raw_line in paragraph.split("\n"):
            if not raw_line.strip():
                continue
            if _JSON_LINE.match(raw_line) and len(raw_line.strip()) > 80:
                _flush(prose, units)
                stats.tool_outputs += 1
                units.append(protector.protect("[ツール出力 省略]"))
                continue
            if _BULLET.match(raw_line):
                _flush(prose, units)
                item = _clean_line_markup(_BULLET.sub("", raw_line, count=1))
                if item:
                    # Each bullet is one unit; long bullets may still split.
                    parts = _sentences(item)
                    units.append("・" + parts[0])
                    units.extend(parts[1:])
                continue
            line = _clean_line_markup(raw_line)
            if _HEADING.match(raw_line):
                _flush(prose, units)
                units.append(line)
                continue
            prose.append(line)
        _flush(prose, units)

    restored = (re.sub(r"\s+", " ", protector.restore(unit)).strip() for unit in units)
    return [unit for unit in restored if unit]


def _flush(prose: list[str], units: list[str]) -> None:
    """Emit buffered prose lines as sentence units."""
    if prose:
        units.extend(_sentences(_join(prose)))
        prose.clear()


def _sentences(text: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if end <= start:
            continue
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [text.strip()]


def _url_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _URL.finditer(text)]


def shorten_unit(text: str, budget: int) -> str:
    """Last-resort shortening of one unit to ``budget`` chars (incl. ``…``).

    Prefers a sentence end, then a clause boundary in the latter half; never
    cuts inside a URL.
    """
    if len(text) <= budget:
        return text
    if budget <= len(ELLIPSIS):
        return ELLIPSIS[:budget]
    limit = budget - len(ELLIPSIS)
    floor = limit // 2

    cut = None
    for pattern in (_SENTENCE_END, _CLAUSE_BOUNDARY):
        candidates = [m.end() for m in pattern.finditer(text, 0, limit + 1) if floor <= m.end()]
        candidates = [c for c in candidates if c <= limit]
        if candidates:
            cut = max(candidates)
            break
    if cut is None:
        cut = limit
    for start, end in _url_spans(text):
        if start < cut < end:
            cut = start
            break
    shortened = text[:cut].rstrip(" 、，,;；:：")
    if not shortened:
        # A URL longer than the budget from position 0: elide it atomically.
        return shorten_url(text[:limit], budget)
    return shortened + ELLIPSIS


def _select(units: list[str], budget: int, priority: list[int]) -> tuple[str, bool]:
    """Keep whole units in ``priority`` order within ``budget``.

    Returns (text in original order with ``…`` for gaps, truncated flag).
    """
    if not units:
        return "", False
    full = _join(units)
    if len(full) <= budget:
        return full, False

    units = list(units)

    def _render(indices: list[int]) -> str:
        out: list[str] = []
        previous = -1
        for index in sorted(indices):
            if index != previous + 1 and not (out and out[-1].endswith(ELLIPSIS)):
                out.append(ELLIPSIS)  # gap before this unit (incl. a skipped lead)
            out.append(units[index])
            previous = index
        if previous != len(units) - 1 and not out[-1].endswith(ELLIPSIS):
            out.append(ELLIPSIS)
        return _join(out)

    # The top-priority unit must survive; if it alone overflows, shorten it at
    # a boundary (the only place a unit is ever cut).
    first = priority[0]
    overflow = len(_render([first])) - budget
    if overflow > 0:
        units[first] = shorten_unit(units[first], max(len(units[first]) - overflow, 1))

    chosen: list[int] = []
    for index in priority:
        if index in chosen:
            continue
        candidate = chosen + [index]
        if len(_render(candidate)) <= budget:
            chosen = candidate
    return _render(chosen), True


def _user_priority(units: list[str]) -> list[int]:
    order = [0]
    last = len(units) - 1
    if last > 0 and _QUESTION_END.search(units[last]):
        order.append(last)
    order.extend(range(1, len(units)))
    return order


def _agent_priority(units: list[str]) -> list[int]:
    last = len(units) - 1
    conclusions = [i for i, unit in enumerate(units) if _CONCLUSION.search(unit)]
    # The latest conclusion first; then the lead, then the closing unit.
    order = list(reversed(conclusions)) + [0, last]
    order.extend(range(1, last))
    return order


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


class EpisodicFormatter:
    """Rule-based episode formatter. Pure and deterministic; no model calls."""

    def __init__(
        self,
        user_label: str = "User",
        agent_label: str = "Agent",
        *,
        user_budget: int = 160,
        agent_budget: int = 320,
        max_chars: int = 600,
        url_chars: int = 60,
        inline_code_chars: int = 60,
    ) -> None:
        for name, value in (
            ("user_budget", user_budget),
            ("agent_budget", agent_budget),
            ("max_chars", max_chars),
            ("url_chars", url_chars),
            ("inline_code_chars", inline_code_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 16:
                raise ValueError(f"{name} must be an integer >= 16")
        self.user_label = user_label
        self.agent_label = agent_label
        self.user_budget = user_budget
        self.agent_budget = agent_budget
        self.max_chars = max_chars
        self.url_chars = url_chars
        self.inline_code_chars = inline_code_chars

    def _summarize(
        self, text: str, budget: int, stats: _Stats, priority: Callable[[list[str]], list[int]]
    ) -> SpeakerSummary:
        units = _prepare(text, stats, self.url_chars, self.inline_code_chars)
        selected, truncated = _select(units, budget, priority(units)) if units else ("", False)
        return SpeakerSummary(selected, len(text), truncated)

    def format(self, user_message: str, agent_response: str) -> EpisodeSummary:
        stats = _Stats()
        user = self._summarize(str(user_message), self.user_budget, stats, _user_priority)
        agent = self._summarize(str(agent_response), self.agent_budget, stats, _agent_priority)
        content = _compose(self.user_label, user.text, self.agent_label, agent.text)
        if len(content) > self.max_chars:
            # Final defense only; budgets normally keep content under the cap.
            content = shorten_unit(content, self.max_chars)
        return EpisodeSummary(
            content,
            user,
            agent,
            "deterministic",
            {
                "redactions": stats.redactions,
                "omitted_code_blocks": stats.code_blocks,
                "omitted_tool_outputs": stats.tool_outputs,
            },
        )


def _compose(user_label: str, user_text: str, agent_label: str, agent_text: str) -> str:
    return f"{user_label}: {user_text or '(空)'} → {agent_label}: {agent_text or '(空)'}"


def fallback_format(
    user_message: Any,
    agent_response: Any,
    *,
    user_label: str = "User",
    agent_label: str = "Agent",
    user_budget: int = 160,
    agent_budget: int = 320,
) -> EpisodeSummary:
    """Minimal-dependency deterministic fallback used when a formatter fails.

    Deliberately simple: redact, drop fenced code / tool envelopes, collapse
    whitespace, cut at a sentence or clause boundary.
    """

    def _one(value: Any, budget: int) -> SpeakerSummary:
        try:
            text = str(value)
        except Exception:  # noqa: BLE001 - __str__ of an arbitrary response object
            text = ""
        original = len(text)
        # ANSI first, as in ``_prepare``: an escape inside a key (``pass\x1b[31mword=``)
        # would hide it from the mask and come back as ``password=`` once removed.
        text = _ANSI.sub("", text)
        text = _TOOL_ENVELOPE.sub(" [ツール出力 省略] ", text)
        text = _FENCE.sub(" [コード 省略] ", text)
        text, _ = redact_secrets(text)
        text = re.sub(r"\s+", " ", text).strip()
        short = shorten_unit(text, budget)
        return SpeakerSummary(short, original, short != text)

    user = _one(user_message, user_budget)
    agent = _one(agent_response, agent_budget)
    return EpisodeSummary(
        _compose(user_label, user.text, agent_label, agent.text), user, agent, "fallback"
    )


# Fields written only by the original-turn scorer (#293). ``extra_metadata`` cannot set them.
RESERVED_SCORE_KEYS = frozenset(
    {"importance", "importance_basis", "importance_version", "importance_error"}
)
SCORE_FAILED = "E_ORIGINAL_SCORE_FAILED"
SCORE_INVALID = "E_ORIGINAL_SCORE_INVALID"


def _original_score_metadata(scorer: Any, user_message: Any, agent_response: Any) -> dict[str, Any]:
    """Score the unsummarized turn. On failure keep a reason code (Harvest stages it).

    Only the exception type and a fixed code are logged: an exception message
    or traceback could carry the original text.
    """
    scorer = scorer or AmygdalaPass()
    version = (
        SCORING_VERSION
        if type(scorer) is AmygdalaPass
        else str(getattr(scorer, "version", None) or f"custom:{type(scorer).__name__}")
    )
    try:
        value = scorer.score(f"{user_message}\n{agent_response}")
    except Exception as error:  # noqa: BLE001 - scoring must not fail the turn
        logger.warning(
            "Episode importance scoring failed (%s, %s)", SCORE_FAILED, type(error).__name__
        )
        return {"importance_error": SCORE_FAILED, "importance_version": version}
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= float(value) <= 1.0
    ):
        logger.warning(
            "Episode importance scoring failed (%s, %s)", SCORE_INVALID, type(value).__name__
        )
        return {"importance_error": SCORE_INVALID, "importance_version": version}
    return {
        "importance": float(value),
        "importance_basis": "original-turn",
        "importance_version": version,
    }


def record_episode(
    store: Any,
    user_message: Any,
    agent_response: Any,
    *,
    formatter: EpisodeFormatterProtocol | None = None,
    key: str | None = None,
    source: str = "web-chat",
    extra_metadata: Mapping[str, Any] | None = None,
    scorer: Any = None,
    emotion: Any = None,
) -> MemoryEntry | None:
    """Summarize one turn and write it to Episodic Memory. Never raises.

    A formatter failure falls back to :func:`fallback_format`; a store failure
    is logged and returns ``None`` so the chat turn itself still succeeds.

    The turn is scored on its original text (#293) and the number is kept as
    ``importance`` (with ``importance_version``); Harvest uses it as a floor
    over re-scoring the summary, which has lost the headings, bullets, code
    blocks and cut-off conclusions the score depends on. A scoring failure is
    kept as ``importance_error`` and Harvest holds that turn for review.
    ``extra_metadata`` cannot set these fields (``RESERVED_SCORE_KEYS``).
    ``scorer`` defaults to :class:`AmygdalaPass`.

    ``emotion`` (#364) is an :class:`~stiracore.tools.episodic_emotion.EmotionSnapshot`
    taken right after the turn (``capture_emotion_snapshot``). With it the episode is
    ``episodic-summary/v2`` and the snapshot is written as a sidecar record on the
    data-directory floor, never into the frontmatter (the memory tools, the search
    index and Harvest do not see it). A failed record write is logged with a code only;
    the episode stays and its snapshot reads as missing (``None``).
    """
    formatter = formatter or EpisodicFormatter()
    labels = {
        "user_label": getattr(formatter, "user_label", "User"),
        "agent_label": getattr(formatter, "agent_label", "Agent"),
    }
    try:
        summary = formatter.format(str(user_message), str(agent_response))
        if not isinstance(summary, EpisodeSummary) or not isinstance(summary.content, str):
            raise TypeError("formatter must return an EpisodeSummary")
    except Exception as error:  # noqa: BLE001 - any formatter defect must not lose the episode
        # Code and type only: a message or traceback could carry the turn text.
        logger.warning(
            "Episodic formatter failed; using deterministic fallback (E_FORMATTER_FAILED, %s)",
            type(error).__name__,
        )
        try:
            summary = fallback_format(user_message, agent_response, **labels)
        except Exception as fallback_error:  # noqa: BLE001 - must not fail the turn
            logger.error(
                "Episodic fallback formatter failed; episode not recorded (E_FALLBACK_FAILED, %s)",
                type(fallback_error).__name__,
            )
            return None

    # origin (#294): the summary is taken from the turn's words, not rewritten.
    metadata: dict[str, Any] = {
        "source": source,
        "origin": ORIGIN_EXTRACTED,
        **summary.metadata(),
    }
    if extra_metadata:
        ignored = sorted(RESERVED_SCORE_KEYS.intersection(extra_metadata))
        if ignored:
            logger.warning("record_episode ignored reserved metadata keys: %s", ", ".join(ignored))
        metadata.update({k: v for k, v in extra_metadata.items() if k not in RESERVED_SCORE_KEYS})
    metadata.update(_original_score_metadata(scorer, user_message, agent_response))
    if emotion is not None and not isinstance(emotion, EmotionSnapshot):
        logger.warning("Emotion snapshot ignored (E_EMOTION_INVALID, %s)", type(emotion).__name__)
        emotion = None
    if emotion is not None:
        metadata["format"] = FORMAT_VERSION_V2
    try:
        entry = store.write(
            MemoryLayer.EPISODIC,
            key or f"chat-{time.time_ns()}",
            summary.content,
            metadata,
        )
    except Exception:  # persistence failure must not fail the turn
        logger.exception("Failed to write episodic memory; chat turn unaffected")
        return None
    if emotion is not None:
        _write_emotion(store, entry, emotion)
    try:  # #294: the verified origin (the front matter alone is agent-writable)
        record_origin(store, MemoryLayer.EPISODIC, entry.key, ORIGIN_EXTRACTED, source)
    except Exception as error:  # noqa: BLE001 - must not fail the turn; reads as unknown
        logger.warning("Origin not recorded (E_ORIGIN_NOT_RECORDED, %s)", type(error).__name__)
    return entry


def _write_emotion(store: Any, entry: MemoryEntry, emotion: EmotionSnapshot) -> None:
    try:
        write_emotion_snapshot(store, entry, emotion)
    except Exception as error:  # noqa: BLE001 - the episode is already stored
        # Code and type only: values are personal data (#364).
        logger.warning(
            "Emotion snapshot not recorded (E_EMOTION_WRITE_FAILED, %s)", type(error).__name__
        )


__all__ = [
    "EpisodeSummary",
    "EpisodicFormatter",
    "SpeakerSummary",
    "fallback_format",
    "record_episode",
    "redact_secrets",
    "shorten_unit",
    "shorten_url",
]
