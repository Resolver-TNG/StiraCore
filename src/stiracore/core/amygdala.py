"""Deterministic importance scoring and routing; no model calls."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol


class MemoryEntry(Protocol):
    """Structural interface accepted from any memory backend."""

    content: str
    metadata: dict[str, Any] | None


# re.ASCII: a word boundary is an ASCII-word boundary, so "このbugを" and "今日はAPI設計"
# match (Unicode \b sees no boundary between Japanese and Latin letters, #293).
_ERROR_PATTERN = re.compile(
    r"\b(?:errors?|failures?|bugs?)\b|(?:エラー|失敗|不具合|バグ|問題|未解決|懸念)",
    re.IGNORECASE | re.ASCII,
)
_DECISION_PATTERN = re.compile(
    r"\b(?:decisions?|decided|confirmed)\b|(?:決定|判断|確認|結論|設計|方針|発見|改善|観測|実装)",
    re.IGNORECASE | re.ASCII,
)
# Leading indentation is horizontal only: "^\s*" also crossed newlines, so a run of blank lines
# was rescanned from every line start (quadratic; 40,000 newlines took ~18 s, #293).
_STRUCTURE_PATTERN = re.compile(r"^[^\S\n]*(?:#{1,6}\s|[-*+]\s)", re.MULTILINE)
_NAME_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b", re.ASCII)


# Version of the deterministic scoring rules. Stored with every recorded score
# (``importance_version``) and used as the rule distiller's harvest version.
# v3 (#293): ASCII word boundaries; horizontal-only indentation for structure.
SCORING_VERSION = "amygdala/v3-ascii-boundaries"


def _validate_unit_interval(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between 0 and 1")  # noqa: TRY004 - preserve the public ValueError validation contract
    numeric = float(value)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    return numeric


class AmygdalaPass:
    """Apply v0.1 scoring rules and inclusive routing thresholds.

    Scores below ``auto_threshold`` are discarded; scores at or above
    ``review_threshold`` enter staging. Everything between is automatic.
    """

    def __init__(
        self,
        auto_threshold: float = 0.4,
        review_threshold: float = 0.7,
    ) -> None:
        self.auto_threshold = _validate_unit_interval("auto_threshold", auto_threshold)
        self.review_threshold = _validate_unit_interval("review_threshold", review_threshold)
        if self.auto_threshold > self.review_threshold:
            raise ValueError("auto_threshold must not exceed review_threshold")

    def score(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        """Return a deterministic importance score in [0, 1].

        Only a finite float importance in [0, 1] overrides the heuristics.
        Invalid importance values are ignored. Tags must be a collection of
        non-empty strings; a single string is not interpreted as many tags.
        """
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping or None")
        metadata = metadata if metadata is not None else {}

        importance = metadata.get("importance")
        if isinstance(importance, float) and math.isfinite(importance) and 0.0 <= importance <= 1.0:
            return importance

        result = 0.2
        if len(content) > 500:
            result += 0.1
        if "```" in content:
            result += 0.1
        if _ERROR_PATTERN.search(content):
            result += 0.15
        if _DECISION_PATTERN.search(content):
            result += 0.15
        if _STRUCTURE_PATTERN.search(content):
            result += 0.1
        if "?" in content or "？" in content:
            result += 0.05

        tags = metadata.get("tags")
        if isinstance(tags, (list, tuple, set, frozenset)):
            tag_count = sum(1 for tag in tags if isinstance(tag, str) and tag.strip())
            result += min(tag_count, 3) * 0.05

        if _NAME_PATTERN.search(content):
            result += 0.05

        # Avoid floating-point artifacts at routing boundaries.
        return min(1.0, max(0.0, round(result, 10)))

    def route(self, score: float) -> Literal["discard", "auto", "staging"]:
        """Route a valid score using the configured inclusive thresholds."""
        value = _validate_unit_interval("score", score)
        if value >= self.review_threshold:
            return "staging"
        if value >= self.auto_threshold:
            return "auto"
        return "discard"

    def process_entry(
        self, entry: MemoryEntry
    ) -> tuple[Literal["discard", "auto", "staging"], float]:
        """Score and route one memory entry without mutating it."""
        value = self.score(entry.content, getattr(entry, "metadata", None))
        return self.route(value), value
