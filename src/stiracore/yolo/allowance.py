"""Start-up allowance and clock support (ADR 0012 §1, §7).

``STIRA_YOLO_ALLOW`` is the highest tier a person may pick at the YOLO button; it opens
nothing by itself. Read once at start-up. Phase 7c (operator request 2026-10-19): each
value may also come from the ``yolo`` section of ``gateway-config.json`` — written only by
a person's confirmed save on the settings page (:mod:`.config_file`, never the settings
hub, never an agent tool) — with the precedence **environment > config > default**, per
value. Invalid environment values refuse start-up with a message that names the variable,
never the value; invalid config values are rounded or ignored with one warning (an
unknown allowance is ``off``).

Default (ADR 0012 addendum 2026-10-24, "show it and let people choose"): with neither the
variable nor the config value the allowance is ``external`` — the button and both tiers
are offered, and still nothing is approved until a person presses it and confirms (the
six-hour window, the caps, the breaker, Never, the floors and exec are unchanged).
``STIRA_YOLO_ALLOW=off`` or ``"yolo": {"allow": "off"}`` takes it away. A default
allowance never refuses a wildcard listener: the server falls back to ``off`` there
(:data:`DEFAULT_ALLOW`, ``default_allow`` of :func:`parse_settings`).

The clock check decides whether this platform can offer YOLO at all: the window is
measured with a wall clock, a sleep-including monotonic clock and a sleep-excluding one
(§7). Only measured platforms count; anything else is "not offered" — also with an
allowance.
"""

from __future__ import annotations

import re
import sys
import time as _time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ALLOW_ENV",
    "CAP_ENV",
    "CAP_MAX",
    "DEFAULT_ALLOW",
    "DEFAULT_CAP",
    "DEFAULT_EXEC_CAP",
    "DEFAULT_EXTERNAL_CAP",
    "EXEC_CAP_ENV",
    "EXTERNAL_CAP_ENV",
    "HOST_CHANNEL_VALUES",
    "HOST_CONFIRM_ENV",
    "HOST_CONFIRM_VALUES",
    "TIERS",
    "ClockReading",
    "ClockSupport",
    "YoloSettingError",
    "YoloSettings",
    "clock_support",
    "parse_settings",
    "pinned_by_env",
    "read_clocks",
    "resolve_values",
    "tier_rank",
]

ALLOW_ENV = "STIRA_YOLO_ALLOW"
CAP_ENV = "STIRA_YOLO_MAX_AUTO_APPROVALS"
EXTERNAL_CAP_ENV = "STIRA_YOLO_MAX_EXTERNAL"
# Phase 7 (ADR 0012 §9 option C, operator decision 2026-10-19 12:28): the exec sub-cap of
# a window whose person chose "Include exec" at the press. The choice itself exists only
# with ``STIRA_ENABLE_EXEC_TOOLS`` and ``STIRA_YOLO_ALLOW=external`` (no variable turns
# exec on in a window; revision 3's ``STIRA_YOLO_EXEC`` is not used).
EXEC_CAP_ENV = "STIRA_YOLO_MAX_EXEC"
# The exec tools' own switch (``tool_registry.EXEC_ENV_VAR``), read here once with the
# YOLO variables: the exec choice is offered only when exec tools are exposed.
EXEC_TOOLS_ENV = "STIRA_ENABLE_EXEC_TOOLS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})  # as ``tool_registry.exec_tools_enabled``
# How a person confirms the confirmed operations (§8b, Open (12), revision 6). Unset (or
# ``browser``): in the dashboard, with a checkbox (the default; phase 2c). Set to a host channel —
# ``auto`` (the desktop dialog with OS authentication on the person's macOS session, else
# the command), ``command`` or ``dialog`` — the host is required and the browser
# confirmation is refused (opt-in, stricter).
HOST_CONFIRM_ENV = "STIRA_YOLO_HOST_CONFIRM"
HOST_CONFIRM_VALUES = ("browser", "auto", "command", "dialog")
HOST_CHANNEL_VALUES = ("auto", "command", "dialog")
# Ordered: off < internal < external (§2).
TIERS = ("off", "internal", "external")
# The allowance when neither the variable nor the config sets one (ADR 0012 addendum
# 2026-10-24): offered, not approved — a window still needs a person's press and confirm.
DEFAULT_ALLOW = "external"
DEFAULT_CAP = 2000  # Open (5), proposed value
DEFAULT_EXTERNAL_CAP = 200  # Open (5), proposed value
DEFAULT_EXEC_CAP = 200  # phase 7: the exec sub-cap (operator decision 2026-10-19)
CAP_MAX = 10000
_DIGITS = re.compile(r"[0-9]{1,6}")


class YoloSettingError(ValueError):
    """An invalid YOLO start-up variable; the message names the variable only."""


def tier_rank(tier: Any) -> int:
    """Rank of a tier (``off`` = 0); anything that is not a tier raises ``ValueError``."""
    if not isinstance(tier, str) or tier not in TIERS:
        raise ValueError("not a YOLO tier")
    return TIERS.index(tier)


@dataclass(frozen=True)
class YoloSettings:
    """What the operator allowed at start-up. ``allowance == "off"``: nothing changes."""

    allowance: str = "off"
    cap: int = DEFAULT_CAP
    external_cap: int = DEFAULT_EXTERNAL_CAP
    host_confirm: str = "browser"
    exec_cap: int = DEFAULT_EXEC_CAP
    # ``STIRA_ENABLE_EXEC_TOOLS`` as read at start-up (phase 7).
    exec_tools: bool = False
    # Phase 7c: the config's ``offer_exec`` (no variable; default true = as before). False
    # takes "Include exec" out of the tier dialog even with exec tools and ``external``.
    offer_exec: bool = True
    # Phase 7c: where each value came from ("env" / "config" / "default"), by config
    # field name (:data:`.config_file.FIELDS`). Not part of the comparison.
    sources: Mapping[str, str] = field(default_factory=dict, compare=False)

    @property
    def enabled(self) -> bool:
        return self.allowance != "off"

    @property
    def exec_offered(self) -> bool:
        """Phase 7 (§9 option C): the press may include exec — only with exec tools
        exposed **and** the allowance ``external`` (exec implies the external tier).

        ADR 0012 addendum 2026-10-24: the **default** ``external`` never offers exec — as
        before, exec in a window needs an operator's own ``external`` (variable or config).
        """
        return (
            self.allowance == "external"
            and self.exec_tools
            and self.offer_exec
            and self.sources.get("allow") != "default"
        )

    @property
    def host_required(self) -> bool:
        """``STIRA_YOLO_HOST_CONFIRM`` names a host channel: the browser cannot confirm."""
        return self.host_confirm in HOST_CHANNEL_VALUES

    def allows(self, tier: Any) -> bool:
        """Whether ``tier`` (not ``off``) may be picked under this allowance."""
        try:
            rank = tier_rank(tier)
        except ValueError:
            return False
        return 0 < rank <= tier_rank(self.allowance)


def _integer(raw: str, name: str, low: int, high: int) -> int | None:
    if raw == "":
        return None
    if not _DIGITS.fullmatch(raw):
        raise YoloSettingError(f"{name} must be an integer from {low} to {high}")
    value = int(raw)
    if not low <= value <= high:
        raise YoloSettingError(f"{name} must be an integer from {low} to {high}")
    return value


def _env_values(environ: Mapping[str, str]) -> dict[str, Any]:
    """The variables that are set, parsed strictly (an invalid one raises)."""
    out: dict[str, Any] = {}
    raw = environ.get(ALLOW_ENV, "")
    if raw != "":
        if raw not in TIERS:
            raise YoloSettingError(
                f"{ALLOW_ENV} must be 'off', 'internal' or 'external' (or unset)"
            )
        out["allow"] = raw
    # Each variable by its literal name (tests/test_docs_sync.py reads the lookups).
    for name, value in (
        ("max_auto_approvals", _integer(environ.get(CAP_ENV, ""), CAP_ENV, 1, CAP_MAX)),
        (
            "max_external",
            _integer(environ.get(EXTERNAL_CAP_ENV, ""), EXTERNAL_CAP_ENV, 0, CAP_MAX),
        ),
        ("max_exec", _integer(environ.get(EXEC_CAP_ENV, ""), EXEC_CAP_ENV, 0, CAP_MAX)),
    ):
        if value is not None:
            out[name] = value
    confirm_raw = environ.get(HOST_CONFIRM_ENV, "")
    if confirm_raw != "":
        if confirm_raw not in HOST_CONFIRM_VALUES:
            raise YoloSettingError(
                f"{HOST_CONFIRM_ENV} must be 'browser', 'auto', 'command' or 'dialog' (or unset)"
            )
        out["host_confirm"] = confirm_raw
    return out


_ENV_NAMES = {
    "allow": ALLOW_ENV,
    "max_auto_approvals": CAP_ENV,
    "max_external": EXTERNAL_CAP_ENV,
    "max_exec": EXEC_CAP_ENV,
    "host_confirm": HOST_CONFIRM_ENV,
}


def pinned_by_env(environ: Mapping[str, str]) -> dict[str, bool]:
    """Phase 7c: which config values a set variable pins (the settings page cannot change
    them). ``offer_exec`` has no variable."""
    return {
        "allow": environ.get(ALLOW_ENV, "") != "",
        "offer_exec": False,
        "max_auto_approvals": environ.get(CAP_ENV, "") != "",
        "max_external": environ.get(EXTERNAL_CAP_ENV, "") != "",
        "max_exec": environ.get(EXEC_CAP_ENV, "") != "",
        "host_confirm": environ.get(HOST_CONFIRM_ENV, "") != "",
    }


def resolve_values(
    environ: Mapping[str, str],
    config: Mapping[str, Any] | None = None,
    default_allow: str = DEFAULT_ALLOW,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Every value with its source — environment > config > default — also when the
    allowance is ``off`` (the settings page shows what a start would use).

    ``default_allow``: the allowance when neither sets one (:data:`DEFAULT_ALLOW`; the
    server passes ``off`` for a wildcard listener).

    ``config`` is the **cleaned** ``yolo`` section (:func:`.config_file.clean`). Returns
    ``(values, sources, warnings)``; ``warnings`` lists the sub-caps larger than the cap
    because of a config value (rounded down to the cap). Raises ``YoloSettingError`` for
    an invalid variable, or for a sub-cap variable larger than the cap variable / default.
    """
    env = _env_values(environ)
    config = dict(config or {})
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def pick(name: str, default: Any) -> None:
        if name in env:
            values[name], sources[name] = env[name], "env"
        elif name in config:
            values[name], sources[name] = config[name], "config"
        else:
            values[name], sources[name] = default, "default"

    pick("allow", default_allow)
    pick("offer_exec", True)
    pick("max_auto_approvals", DEFAULT_CAP)
    pick("max_external", min(DEFAULT_EXTERNAL_CAP, values["max_auto_approvals"]))
    pick("max_exec", min(DEFAULT_EXEC_CAP, values["max_auto_approvals"]))
    pick("host_confirm", "browser")
    cap = values["max_auto_approvals"]
    warnings: list[str] = []
    for name in ("max_external", "max_exec"):
        if values[name] <= cap:
            continue
        if sources[name] == "env" and sources["max_auto_approvals"] != "config":
            # As before 7c: a sub-cap variable above the cap (variable or default).
            raise YoloSettingError(f"{_ENV_NAMES[name]} must not exceed {CAP_ENV}")
        if sources[name] == "default":
            values[name] = cap  # a default follows a smaller cap, as before
            continue
        values[name] = cap
        warnings.append(f"{name}_above_cap")
    return values, sources, warnings


def parse_settings(
    environ: Mapping[str, str],
    config: Mapping[str, Any] | None = None,
    default_allow: str = DEFAULT_ALLOW,
) -> YoloSettings:
    """Parse the five variables (and read ``STIRA_ENABLE_EXEC_TOOLS``) over the cleaned
    config section (phase 7c); an empty variable is unset; anything else not exact refuses.

    Accepted for ``STIRA_YOLO_ALLOW``: exactly ``off``, ``internal`` or ``external``
    (``on``, ``1``, ``all``, ``Internal``, surrounding spaces … refuse). Unset (and no
    config value): ``default_allow`` (:data:`DEFAULT_ALLOW`). A cap variable set while the
    allowance is ``off`` refuses as well (it would suggest YOLO is on).
    """
    values, sources, _warnings = resolve_values(environ, config, default_allow)
    if values["allow"] == "off":
        for name, env in _ENV_NAMES.items():
            if name != "allow" and sources[name] == "env":
                raise YoloSettingError(f"{env} is set but the YOLO allowance is off")
        return YoloSettings(sources=sources)
    exec_tools = environ.get(EXEC_TOOLS_ENV, "").strip().lower() in _TRUTHY
    return YoloSettings(
        allowance=values["allow"],
        cap=values["max_auto_approvals"],
        external_cap=values["max_external"],
        host_confirm=values["host_confirm"],
        exec_cap=values["max_exec"],
        exec_tools=exec_tools,
        offer_exec=values["offer_exec"],
        sources=sources,
    )


# --- clocks (§7) ---------------------------------------------------------------------

# Platforms whose clocks were measured (ADR 0012 §7): the sleep-including monotonic clock
# by name, and the implementation ``time.monotonic`` must report (sleep-excluding).
# Linux (CLOCK_BOOTTIME), Windows, containers and VMs are not measured yet: not offered.
_MEASURED = {"darwin": ("CLOCK_MONOTONIC", "mach_absolute_time()")}


@dataclass(frozen=True)
class ClockSupport:
    supported: bool
    reason: str | None  # None, "clock_not_measured", "clock_unavailable", "clock_unexpected"
    platform: str


def clock_support(platform: str | None = None, time_module: Any = _time) -> ClockSupport:
    """Whether wall, sleep-including and sleep-excluding clocks are all available here."""
    platform = sys.platform if platform is None else platform
    measured = _MEASURED.get(platform)
    if measured is None:
        return ClockSupport(False, "clock_not_measured", platform)
    name, implementation = measured
    clock_id = getattr(time_module, name, None)
    if clock_id is None:
        return ClockSupport(False, "clock_unavailable", platform)
    try:
        time_module.clock_gettime(clock_id)
        info = time_module.get_clock_info("monotonic")
    except (OSError, ValueError, AttributeError):
        return ClockSupport(False, "clock_unavailable", platform)
    if getattr(info, "implementation", None) != implementation or not info.monotonic:
        # time.monotonic is not the sleep-excluding clock that was measured.
        return ClockSupport(False, "clock_unexpected", platform)
    return ClockSupport(True, None, platform)


@dataclass(frozen=True)
class ClockReading:
    """The three readings of §7: wall ``W``, sleep-including ``B``, sleep-excluding ``U``."""

    wall: float
    boot: float
    mono: float


def read_clocks(platform: str | None = None, time_module: Any = _time) -> ClockReading:
    """Read all three clocks; raises ``RuntimeError`` where :func:`clock_support` says no."""
    support = clock_support(platform, time_module)
    if not support.supported:
        raise RuntimeError(f"YOLO clocks unavailable: {support.reason}")
    name = _MEASURED[support.platform][0]
    return ClockReading(
        wall=time_module.time(),
        boot=time_module.clock_gettime(getattr(time_module, name)),
        mono=time_module.monotonic(),
    )
