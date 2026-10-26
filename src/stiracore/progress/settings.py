"""The ``progress`` section of ``gateway-config.json`` and its variables (ADR 0014 §10).

Precedence per value: **environment > config > default**. Every value is off / safe by
default; nothing here calls a model::

    "progress": {
      "enabled": false,            # STIRA_PROGRESS_REPORTS (1 / 0)
      "interval_min": 30,          # STIRA_PROGRESS_INTERVAL_MIN (10-360)
      "continue_after_min": 10,    # STIRA_PROGRESS_CONTINUE_AFTER_MIN (1-120)
      "max_auto_continues": 3,     # STIRA_PROGRESS_MAX_AUTO_CONTINUES (0-10; 0 = never)
      "count_tools": 0,            # STIRA_PROGRESS_COUNT_TOOLS (0 = off, 1-1000)
      "count_tokens": 0,           # STIRA_PROGRESS_COUNT_TOKENS (0 = off, 1000-10000000)
      "declared": false            # STIRA_PROGRESS_DECLARED (1 / 0)
    }

``count_tools`` / ``count_tokens`` (ADR 0014 phase 4, #451): a checkpoint also comes after
that many tool calls / model tokens (input + output) since the turn began or the person
last acted, whichever trigger is first. ``0`` (the default) is off.

``declared`` (ADR 0014 phase 3, #451): the agent may declare a milestone itself with
``handoff_to_user(breakout_of_loop: true)``; one fixed guidance paragraph is then added to
the static prompt while a window with reports on is open. Off by default.

``enabled`` is the default of the YOLO start dialog's 「30 分ごとに中間報告する」 check; the
person's tick in the dialog decides for the window it opens (phase 1 covers YOLO windows
only). Read leniently, like the ``yolo`` section: an out-of-range integer is rounded into
range, anything not understood is left out (its default applies), one warning code each.
Only built-in values are evaluated (``type(x) is int`` / ``bool`` / ``str``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DEFAULTS", "ENV_OF", "LIMITS", "SECTION", "ProgressSettings", "parse_settings"]

SECTION = "progress"
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "interval_min": 30,
    "continue_after_min": 10,
    "max_auto_continues": 3,
    "count_tools": 0,
    "count_tokens": 0,
    "declared": False,
}
_BOOLS = frozenset({"enabled", "declared"})
LIMITS: dict[str, tuple[int, int]] = {
    "interval_min": (10, 360),
    "continue_after_min": (1, 120),
    "max_auto_continues": (0, 10),
    "count_tools": (0, 1000),
    "count_tokens": (0, 10_000_000),
}
# 0 is "off" for the counts; a value below the lower bound but above 0 is rounded up to it.
_COUNT_FLOOR = {"count_tokens": 1000}
ENV_OF: dict[str, str] = {
    "enabled": "STIRA_PROGRESS_REPORTS",
    "interval_min": "STIRA_PROGRESS_INTERVAL_MIN",
    "continue_after_min": "STIRA_PROGRESS_CONTINUE_AFTER_MIN",
    "max_auto_continues": "STIRA_PROGRESS_MAX_AUTO_CONTINUES",
    "count_tools": "STIRA_PROGRESS_COUNT_TOOLS",
    "count_tokens": "STIRA_PROGRESS_COUNT_TOKENS",
    "declared": "STIRA_PROGRESS_DECLARED",
}
_TRUE, _FALSE = ("1", "true", "on", "yes"), ("0", "false", "off", "no")


@dataclass(frozen=True)
class ProgressSettings:
    enabled: bool = False
    interval_min: int = 30
    continue_after_min: int = 10
    max_auto_continues: int = 3
    count_tools: int = 0
    count_tokens: int = 0
    declared: bool = False
    sources: dict[str, str] = field(default_factory=dict, compare=False)
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def interval_sec(self) -> int:
        return self.interval_min * 60

    @property
    def continue_after_sec(self) -> int:
        return self.continue_after_min * 60

    def view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_min": self.interval_min,
            "continue_after_min": self.continue_after_min,
            "max_auto_continues": self.max_auto_continues,
            "count_tools": self.count_tools,
            "count_tokens": self.count_tokens,
            "declared": self.declared,
            "sources": dict(self.sources),
        }


def _int_value(name: str, value: Any, warnings: list[str]) -> int | None:
    low, high = LIMITS[name]
    if type(value) is str:
        text = value.strip()
        if not (text.isascii() and text.isdigit()):
            warnings.append(f"{name}_invalid")
            return None
        value = int(text)
    if type(value) is not int:
        warnings.append(f"{name}_invalid")
        return None
    if not low <= value <= high:
        warnings.append(f"{name}_rounded")
        return min(max(value, low), high)
    floor = _COUNT_FLOOR.get(name)
    if floor is not None and 0 < value < floor:
        warnings.append(f"{name}_rounded")
        return floor
    return value


def _bool_value(name: str, value: Any, warnings: list[str]) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is str and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    warnings.append(f"{name}_invalid")
    return None


def parse_settings(
    environ: Mapping[str, str] | None = None, config: Any = None
) -> ProgressSettings:
    """The settings in use: never refuses (a value not understood keeps its default)."""
    environ = environ or {}
    # Literal names: tests/test_docs_sync.py reads every lookup of the code.
    raw_env = {
        "enabled": environ.get("STIRA_PROGRESS_REPORTS"),
        "interval_min": environ.get("STIRA_PROGRESS_INTERVAL_MIN"),
        "continue_after_min": environ.get("STIRA_PROGRESS_CONTINUE_AFTER_MIN"),
        "max_auto_continues": environ.get("STIRA_PROGRESS_MAX_AUTO_CONTINUES"),
        "count_tools": environ.get("STIRA_PROGRESS_COUNT_TOOLS"),
        "count_tokens": environ.get("STIRA_PROGRESS_COUNT_TOKENS"),
        "declared": environ.get("STIRA_PROGRESS_DECLARED"),
    }
    section = config if isinstance(config, Mapping) else {}
    warnings: list[str] = [] if config is None or isinstance(config, Mapping) else ["section"]
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, default in DEFAULTS.items():
        read = _bool_value if name in _BOOLS else _int_value
        chosen, source = default, "default"
        if name in section:
            value = read(name, section[name], warnings)
            if value is not None:
                chosen, source = value, "config"
        raw = raw_env[name]
        if type(raw) is str and raw.strip():
            value = read(name, raw, warnings)
            if value is not None:
                chosen, source = value, "env"
        values[name], sources[name] = chosen, source
    unknown = sorted(str(key)[:40] for key in section if key not in DEFAULTS)
    warnings.extend(f"unknown_{key}" for key in unknown)
    return ProgressSettings(**values, sources=sources, warnings=tuple(warnings))
