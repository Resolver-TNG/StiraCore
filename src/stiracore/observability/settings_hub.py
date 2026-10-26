"""Settings hub (#227): one validated draft for classifier, safety and tool settings.

Pure logic (no Flask): the hub's value shape, field-level validation, danger
detection, and the runtime helpers a chat server uses to really apply what the
hub saved (tool switches and breaker thresholds from the next turn, the
classifier choice at start-up). Routes live in ``config_api``.

Values never contain secrets: tool names, booleans, thresholds, literal shell
allowlist commands, and the classifier choice.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stiracore.config import settings_surface, stira_delivery
from stiracore.config.agent_jobs import AGENT_JOB_LIMITS, AgentJobSettings
from stiracore.config.models import BOARD_SVG_DEFAULT, ui_board_svg
from stiracore.core import StiraConfig
from stiracore.core.coherence import BC_MIN_SAMPLES
from stiracore.identity_paths import WORKSPACE_ROOT, broad_root_problem
from stiracore.observability.masking import mask_secrets
from stiracore.tools import tool_policy, tool_registry
from stiracore.tools.delegate_tool import LIMIT_BOUNDS as DELEGATION_LIMITS
from stiracore.tools.delegate_tool import DelegationSettings
from stiracore.tools.tool_config import DEFAULT_PATH as DEFAULT_TOOL_PATH
from stiracore.tools.tool_config import ToolConfig, rules_loosening, secret_commands

GROUPS = (
    "classifier",
    "tools",
    "breaker",
    "breaker_details",
    "write",
    "read",
    "board",
    "output",
    "cache",
    "context",
    "heartbeat",
    "delegation",
    "agent_jobs",
)
BREAKER_FIELDS = ("stability_warning", "stability_critical", "bc_threshold")
EXIT_FIELDS = {
    "stability_warning": "stability_warning_exit",
    "stability_critical": "stability_critical_exit",
    "bc_threshold": "bc_exit_threshold",
}
# The "breaker" group (#471): each entry with its exit, edited, saved and applied as one
# set (the next turn gets all six together, ``apply_breaker_thresholds``).
BREAKER_SET = (*BREAKER_FIELDS, *EXIT_FIELDS.values())
# Upper limits of the details that the engine leaves open (#471 review round 3, M-1),
# measured on the engine (``tests/test_breaker_details_limits.py``). A window caps how far
# back a score looks: with no cap the whole history is the window, so the longer the calm,
# the later a swing or a run of agreeing answers is seen, without end (and the coherence
# window, saved with the state every turn, grows by one sample a turn). At 200 (ten times
# the default) a stability swing still trips the coherence line within 11 turns and the
# sycophancy alert comes within 82 agreeing answers. The cooldown holds Critical for at
# least that many turns (50 is a long session); each edit round is one more approval to
# answer (10 is more than a human re-edits one call). Refused above, never rounded on load:
# a file already above the limit is only re-checked when that field changes.
DETAIL_LIMITS = {
    "bc_window_size": 200,
    "sycophancy_window_size": 200,
    "critical_cooldown_turns": 50,
    "hitl_max_edit_rounds": 10,
}
# The "breaker_details" group (#471): read when the chat server starts. The lower limits are
# the engine's own valves (``StiraConfig``); the upper ones ``DETAIL_LIMITS`` and the 24 h cap
# the delivery gives the approval timeout. The page rounds a value into them and says so,
# the server refuses outside them.
DETAIL_FIELDS = {
    "bc_window_size": {
        "integer": True,
        "min": BC_MIN_SAMPLES,
        "max": DETAIL_LIMITS["bc_window_size"],
    },
    "critical_cooldown_turns": {
        "integer": True,
        "min": 3,
        "max": DETAIL_LIMITS["critical_cooldown_turns"],
    },
    "decay_min_interval_sec": {"integer": False, "min": 0.0, "max": None},
    # ``clamp: False``: the page shows an error instead of rounding (#471 review m-2): the top
    # of the range is the dullest detection, so rounding up to it is not the safe side.
    "sycophancy_threshold": {"integer": False, "min": 0.0, "max": 1.0, "clamp": False},
    "sycophancy_window_size": {
        "integer": True,
        "min": 1,
        "max": DETAIL_LIMITS["sycophancy_window_size"],
    },
    # > 0 (an approval that expires at once is not a timeout); fail-closed when it expires.
    "hitl_timeout_sec": {
        "integer": False,
        "min": 0.0,
        "min_exclusive": True,
        "max": stira_delivery.HITL_TIMEOUT_MAX_SEC,
    },
    "hitl_max_edit_rounds": {
        "integer": True,
        "min": 0,
        "max": DETAIL_LIMITS["hitl_max_edit_rounds"],
    },
}
# Shown, never edited from the hub: the server chooses them, or (Layer 3+) they only grow.
FIXED_FIELDS = {
    "protected_layers": "layer_protection",
    "critical_action": "server_fixed",
    "blocked_tool_names": "server_fixed",
}
CLASSIFIER_CHOICES = ("auto", "rule", "sokudan")
TOOL_MODES = ("custom", "trust_all")
_SHELL_OPERATORS = "\r\n;&|<>`$()"
# Below these the breaker practically never trips: "disabling it" by thresholds.
DANGER_BREAKER_FLOORS = {"stability_warning": 0.3, "stability_critical": 0.1, "bc_threshold": 0.3}
# Where the sycophancy detection is practically off (#471 review M-1), measured on the engine
# (``StiraEngine.record_agreement``): it scores only once its window holds 10 answers, so a
# window below 10 never alerts; it alerts only when the share of agreeing answers is *above*
# the threshold, so 1.0 never alerts and 0.95 (default window 20) needs 20 of 20. Saving such a
# value needs the typed confirmation, like the breaker floors.
SYCOPHANCY_DANGER = {"threshold_at_least": 0.95, "window_below": 10}


class HubError(ValueError):
    """Field-level validation errors: ``[{"field", "code", "params"}]``."""

    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"{e['field']}: {e['code']}" for e in errors))
        self.errors = errors


def classifier_choice(gateway) -> str:
    """``jev_enabled`` false means "auto" (the runtime picks Sokudan when installed)."""
    return gateway.jev_classifier if gateway.jev_enabled else "auto"


def gateway_overrides_for_choice(choice: str, gateway) -> dict:
    if choice == "auto":
        return {"jev_enabled": False, "jev_classifier": gateway.jev_classifier}
    return {"jev_enabled": True, "jev_classifier": choice}


# Where agent-side file tools may write (#243). Outside every root is denied.
WRITE_SCOPES = {
    "workspace": [WORKSPACE_ROOT],
    "workspace_tmp": [WORKSPACE_ROOT, "/tmp"],
}
DEFAULT_WRITE_SCOPE = "workspace"
MAX_WRITE_ROOTS = 32
# Where agent-side file tools may read (#457). The secret-path floor applies to every
# scope (also "everywhere"); adding a root is a loosening: confirmed and recorded.
READ_SCOPES = {
    "workspace": [WORKSPACE_ROOT],
    "workspace_home": [WORKSPACE_ROOT, "~"],
    "everywhere": ["/"],
}
DEFAULT_READ_SCOPE = "workspace"
MAX_READ_ROOTS = 32


def write_scope(roots) -> str:
    """``workspace`` / ``workspace_tmp`` for the built-in scopes, else ``custom``."""
    for scope, preset in WRITE_SCOPES.items():
        if list(roots) == preset:
            return scope
    return "custom"


def board_values(gateway) -> dict:
    """The Stage board (#453): ``svg`` = gateway ``ui.board_svg`` (static SVG pictures;
    on unless the file says ``false``, ADR 0012 addendum 2026-10-24)."""
    return {"svg": ui_board_svg(gateway.ui)}


def gateway_ui_for_board(board: Mapping, gateway) -> dict:
    """The gateway ``ui`` object with the board values applied (``board_svg`` only when
    off: on is the default)."""
    ui = {key: value for key, value in gateway.ui.items() if key != "board_svg"}
    if board.get("svg") is False:
        ui["board_svg"] = False
    return ui


# --- Cost settings (#470): output cap, prompt cache, context management ------------
#
# Stored in gateway-config.json. ``output`` (max_output_tokens) is applied from the next turn
# (the chat server re-resolves the model selection and its cap before every turn);
# ``cache`` and ``context`` are read once when the chat server starts.

CACHE_TTLS = ("5m", "1h")
# A sanity bound only (no documented model is near it); the model's own limit is a warning.
MAX_OUTPUT_TOKENS_LIMIT = 1_000_000
CONTEXT_OPTION_FIELDS = (
    "offload_threshold_tokens",
    "preview_tokens",
    "compression_threshold",
    "summary_ratio",
    "preserve_recent",
    "retention_hours",
    "context_window_tokens",
    "retrieval_max_tokens",
)
# Field rules, mirrored by the page (ContextManagementSettings re-checks the result):
# ``int``/``number``, ``min``/``max`` (``None`` = no bound), ``min_open`` = min itself refused.
CONTEXT_RULES = {
    "offload_threshold_tokens": {"kind": "int", "min": 1, "max": None},
    "preview_tokens": {"kind": "int", "min": 0, "max": None},
    "compression_threshold": {"kind": "number", "min": 0.0, "max": 1.0, "min_open": True},
    "summary_ratio": {"kind": "number", "min": 0.1, "max": 0.8},
    "preserve_recent": {"kind": "int", "min": 0, "max": None},
    "retention_hours": {"kind": "number", "min": 0.0, "max": 24.0 * 365, "min_open": True},
    "context_window_tokens": {"kind": "int", "min": 1, "max": None, "nullable": True},
    "retrieval_max_tokens": {"kind": "int", "min": 1, "max": None},
}
# The settings-surface ids (#468) the hub fields are stored as; the other context.* fields
# are keys of gateway.context_management_options. Where a value comes from, when it applies
# and which variable pins it: ``surface.fields[<id>]`` (#469), the same rule as the refusal.
COST_CONFIG_FIELDS = {
    "output.max_output_tokens": "gateway.max_output_tokens",
    "cache.enabled": "gateway.prompt_cache",
    "cache.ttl": "gateway.prompt_cache_ttl",
    "context.enabled": "gateway.context_management",
    **{f"context.{name}": "gateway.context_management_options" for name in CONTEXT_OPTION_FIELDS},
}
_ON = ("1", "true", "yes", "on")


def _context_defaults() -> dict:
    from stiracore.agent.context_management import ContextManagementSettings

    defaults = ContextManagementSettings().as_dict()
    return {name: defaults[name] for name in CONTEXT_OPTION_FIELDS}


def output_values(gateway) -> dict:
    """``max_output_tokens``: ``None`` = automatic (the model's limit bounded at 32000, #416)."""
    return {"max_output_tokens": gateway.max_output_tokens}


def cache_values(gateway) -> dict:
    return {"enabled": gateway.prompt_cache is True, "ttl": gateway.prompt_cache_ttl}


def context_values(gateway) -> dict:
    """The switch and every option: the saved overrides over the code defaults."""
    options = {**_context_defaults(), **dict(gateway.context_management_options or {})}
    return {"enabled": gateway.context_management is True, **options}


def gateway_context_options(context: Mapping) -> dict:
    """Only the options that differ from the code defaults are written to the file."""
    defaults = _context_defaults()
    return {
        name: context[name]
        for name in CONTEXT_OPTION_FIELDS
        if name in context and context[name] != defaults[name]
    }


def _environ(environ):
    return os.environ if environ is None else environ


def _env_switch(raw: str | None) -> bool | None:
    """An env switch: unset/empty = not set; ``1/true/yes/on`` = on; anything else = off."""
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _ON


def cost_env_pins(environ: Mapping[str, str] | None = None) -> dict:
    """``{field: ENV_NAME}`` for the cost fields an environment variable decides.

    The settings-surface rule (``settings_surface.pinned_by``, the manifest's ``overrides`` /
    ``when``): the server refuses exactly the inputs the page disables. Names only: the
    values are never returned.
    """
    return _env_pins(COST_CONFIG_FIELDS, environ)


def _env_pins(config_fields: Mapping[str, str], environ: Mapping[str, str] | None) -> dict:
    """``{hub field: ENV_NAME}`` for the ``config_fields`` a variable pins (names only)."""
    env = _environ(environ)
    pins = {}
    try:
        for field, item_id in config_fields.items():
            name = settings_surface.pinned_by(item_id, env)
            if name:
                pins[field] = name
    except (OSError, ValueError):
        # A broken manifest (#490: ``surface.available`` is false and the page disables
        # nothing): the save goes through like the page shows; the variable still wins.
        return {}
    return pins


def effective_prompt_cache(gateway, environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """(enabled, ttl) the chat server starts with: STIRA_PROMPT_CACHE(_TTL) over the file."""
    env = _environ(environ)
    switch = _env_switch(env.get("STIRA_PROMPT_CACHE"))
    enabled = bool(gateway.prompt_cache) if switch is None else switch
    ttl = (env.get("STIRA_PROMPT_CACHE_TTL") or "").strip() or gateway.prompt_cache_ttl
    return enabled, ttl


def effective_context_enabled(gateway, environ: Mapping[str, str] | None = None) -> bool:
    """The switch the chat server starts with: STIRA_CONTEXT_MANAGEMENT over the file."""
    env = _environ(environ)
    switch = _env_switch(env.get("STIRA_CONTEXT_MANAGEMENT"))
    return bool(gateway.context_management) if switch is None else switch


def effective_memory_map(gateway, environ: Mapping[str, str] | None = None) -> bool:
    """The Memory Map switch the chat server starts with: STIRA_MEMORY_MAP over the file."""
    env = _environ(environ)
    switch = _env_switch(env.get("STIRA_MEMORY_MAP"))
    return bool(getattr(gateway, "memory_map", True)) if switch is None else switch


def effective_context_profile(gateway, environ: Mapping[str, str] | None = None) -> dict:
    """The context profile the chat server starts with (#368): STIRA_CONTEXT_PROFILE (a known
    name) over the file. Read-only facts for the settings page: the name, the budgets it sets
    (``None`` = the server's own value), whether only the usable tool definitions are sent,
    whether the shorter tool-schema descriptions are sent, and the variable's name when it decides (never its value)."""
    from stiracore.agent import context_profile

    env = _environ(environ)
    configured = getattr(gateway, "context_profile", context_profile.DEFAULT_PROFILE)
    name = context_profile.effective_name(configured, env)
    profile = context_profile.get_profile(name)
    pinned = (env.get("STIRA_CONTEXT_PROFILE") or "").strip() in context_profile.PROFILES
    return {
        "name": name,
        "saved": configured,
        "pinned_by": context_profile.ENV_NAME if pinned else None,
        "max_tokens": profile.max_tokens,
        "episodic_limit": profile.episodic_limit,
        "procedural_limit": profile.procedural_limit,
        "send_allowed_tools_only": context_profile.send_allowed_tools_only(
            profile, getattr(gateway, "tools_send_allowed_only", False) is True
        ),
        "short_tool_schemas": context_profile.short_tool_schemas(
            profile, getattr(gateway, "tools_short_schemas", False) is True
        ),
    }


def cost_model_info(provider: str, model_id: str) -> dict:
    """What the selected model allows: the automatic cap, its documented limit, caching."""
    from stiracore.providers import capabilities_for, resolve_max_output_tokens
    from stiracore.providers.capabilities import automatic_cap_source
    from stiracore.providers.spec import normalize_provider

    try:
        capabilities = capabilities_for(provider, model_id)
        provider_name = normalize_provider(provider)
    except (TypeError, ValueError):
        capabilities, provider_name = None, provider
    return {
        "auto_max_output_tokens": resolve_max_output_tokens(None, provider, model_id),
        "model_max_output": capabilities.max_output if capabilities else None,
        "thinking_uses_output_budget": bool(
            capabilities and capabilities.thinking_uses_output_budget
        ),
        # #416: "model" = bound by its documented limit (plus thinking_reserve when thinking
        # shares the cap); "provider_default" = the limit is not recorded, the provider's safe
        # default is used (the page says so in automatic mode too).
        "auto_max_output_source": automatic_cap_source(provider, model_id),
        "thinking_reserve": (
            capabilities.thinking_reserve
            if capabilities and capabilities.thinking_uses_output_budget
            else None
        ),
        # Only Bedrock models whose capability row says so get cache points (prompt_cache.py).
        "prompt_cache_supported": bool(
            capabilities and capabilities.prompt_cache and provider_name == "bedrock"
        ),
    }


def gateway_overrides_for_cost(candidate: Mapping) -> dict:
    """GatewayConfig fields for the ``output`` / ``cache`` / ``context`` groups."""
    context = candidate["context"]
    return {
        "max_output_tokens": candidate["output"]["max_output_tokens"],
        "prompt_cache": candidate["cache"]["enabled"],
        "prompt_cache_ttl": candidate["cache"]["ttl"],
        "context_management": context["enabled"],
        "context_management_options": gateway_context_options(context),
    }


def field_moved(values: Mapping, current: Mapping | None, *fields: str) -> bool:
    """True when the request moves one of ``fields`` (``group.key``) off its saved value.

    One rule for every section (#470 review M2, #471 review M-1): the shape of a value is
    always checked, a bound only on a field that moves, so a value already in the file that
    the runtime accepts (a cap above the hub's sanity bound, a window saved before its limit
    existed) never blocks saving something else. ``True`` and ``1`` differ; ``60`` and
    ``60.0`` do not (JSON numbers). The page mirrors it (``moved`` in settings_hub.js).
    """
    if current is None:
        return True
    for field in fields:
        group, _, key = field.partition(".")
        saved = current.get(group) or {}
        if key not in saved:
            return True
        before, after = saved[key], (values.get(group) or {}).get(key)
        if isinstance(before, bool) != isinstance(after, bool) or before != after:
            return True
    return False


def _validate_cost(values: Mapping, current: Mapping | None, report, environ=None) -> None:
    """Field errors for the cost groups: the shape always, a bound only when the field moves
    (:func:`field_moved`; the chat server has no upper bound on ``max_output_tokens``)."""

    def bound(field, code, **params):
        related = (
            ("context.preview_tokens", "context.offload_threshold_tokens")
            if code == "must_be_below_other"
            else (field,)
        )
        if field_moved(values, current, *related):
            report(field, code, **params)

    cap = values["output"].get("max_output_tokens")
    if cap is not None:
        if type(cap) is not int:
            report("output.max_output_tokens", "not_integer")
        elif not 1 <= cap <= MAX_OUTPUT_TOKENS_LIMIT:
            bound("output.max_output_tokens", "out_of_range", min=1, max=MAX_OUTPUT_TOKENS_LIMIT)
    cache = values["cache"]
    if type(cache.get("enabled")) is not bool:
        report("cache.enabled", "not_boolean")
    if cache.get("ttl") not in CACHE_TTLS:
        report("cache.ttl", "unknown_choice", allowed=list(CACHE_TTLS))
    context = values["context"]
    if type(context.get("enabled")) is not bool:
        report("context.enabled", "not_boolean")
    good = {}
    for name in CONTEXT_OPTION_FIELDS:
        rule, value = CONTEXT_RULES[name], context.get(name)
        field = f"context.{name}"
        if value is None and rule.get("nullable"):
            good[name] = value
            continue
        if rule["kind"] == "int" and type(value) is not int:
            report(field, "not_integer")
            continue
        if rule["kind"] == "number" and _number(value) is None:
            report(field, "not_number")
            continue
        low, high, open_low = rule["min"], rule["max"], rule.get("min_open", False)
        if (value <= low if open_low else value < low) or (high is not None and value > high):
            if high is None:
                bound(field, "too_small", min=low)
            elif open_low:
                bound(field, "out_of_range_open", min=low, max=high)
            else:
                bound(field, "out_of_range", min=low, max=high)
            continue
        good[name] = value
    preview, offload = good.get("preview_tokens"), good.get("offload_threshold_tokens")
    if preview is not None and offload is not None and preview >= offload:
        bound("context.preview_tokens", "must_be_below_other", other="offload", limit=offload)
    # A field an environment variable decides is not changed here (the save would do
    # nothing visible, inventory Q2); its saved value is kept as it is.
    if current is not None:
        for field, name in cost_env_pins(environ).items():
            if field_moved(values, current, field):
                report(field, "env_pinned", env=name)


# --- Heartbeat (#472): the zero-model background tick --------------------------------
#
# Stored in gateway-config.json (``hb_enabled``, ``hb_interval_floor_sec``,
# ``hb_interval_ceil_sec``) and read once when the chat server starts: the daemon keeps the
# GatewayConfig it was built with (``HeartbeatGateway.gateway_config``), so every change waits
# for a restart. A tick applies decay, takes a snapshot and checks the breaker; it never calls
# a model (``heartbeat/gateway.py``), so turning it on costs no tokens.

HEARTBEAT_CONFIG_FIELDS = {
    "heartbeat.enabled": "gateway.hb_enabled",
    "heartbeat.interval_floor_sec": "gateway.hb_interval_floor_sec",
    "heartbeat.interval_ceil_sec": "gateway.hb_interval_ceil_sec",
}
# Bounds on a value the page moves (seconds). GatewayConfig accepts any finite value >= 0;
# below a minute a tick (a state save each time) would run almost back to back, and a gap of
# more than a day is the same as off. A value already in the file outside them is kept.
HEARTBEAT_INTERVAL_LIMITS = {"min": 60.0, "max": 86_400.0}
HEARTBEAT_INTERVALS = ("interval_floor_sec", "interval_ceil_sec")
# What the page may show of the running daemon's status (no path, no message).
_PLAIN_NAME = re.compile(r"(?:loop:)?[A-Za-z_][A-Za-z0-9_]{0,63}")
HEARTBEAT_LIVE_FIELDS = (
    "enabled",
    "running",
    "state",
    "last_tick_at",
    "next_tick_at",
    "interval_sec",
    "last_level",
    "tick_count",
    "consecutive_failures",
    "degraded",
    "last_error",
)


def heartbeat_values(gateway) -> dict:
    # #512: a pair reset on load shows the file's bounds (the page notes it), so entering
    # bounds — the defaults too — is a change the page can save.
    shown = getattr(gateway, "shown_intervals", None)
    floor, ceil = (
        shown()
        if callable(shown)
        else (float(gateway.hb_interval_floor_sec), float(gateway.hb_interval_ceil_sec))
    )
    return {
        "enabled": gateway.hb_enabled is True,
        "interval_floor_sec": floor,
        "interval_ceil_sec": ceil,
    }


def heartbeat_defaults() -> dict:
    from stiracore.config import GatewayConfig

    defaults = {name: f.default for name, f in GatewayConfig.__dataclass_fields__.items()}
    return {
        "enabled": defaults["hb_enabled"] is True,
        "interval_floor_sec": float(defaults["hb_interval_floor_sec"]),
        "interval_ceil_sec": float(defaults["hb_interval_ceil_sec"]),
    }


def gateway_overrides_for_heartbeat(values: Mapping) -> dict:
    """GatewayConfig fields for the ``heartbeat`` group."""
    return {
        "hb_enabled": values["enabled"],
        "hb_interval_floor_sec": float(values["interval_floor_sec"]),
        "hb_interval_ceil_sec": float(values["interval_ceil_sec"]),
    }


def heartbeat_env_pins(environ: Mapping[str, str] | None = None) -> dict:
    """``{field: ENV_NAME}``: ``STIRA_HEARTBEAT`` pins the switch (the surface rule)."""
    return _env_pins(HEARTBEAT_CONFIG_FIELDS, environ)


def effective_heartbeat_enabled(gateway, environ: Mapping[str, str] | None = None) -> bool:
    """The switch the chat server starts with: STIRA_HEARTBEAT=1/0 over ``hb_enabled``."""
    env = _environ(environ)
    switch = _env_switch(env.get("STIRA_HEARTBEAT"))
    return bool(gateway.hb_enabled) if switch is None else switch


def heartbeat_live(status: Mapping | None) -> dict | None:
    """The running daemon's status for the page: known fields only, plain values only."""
    if not isinstance(status, Mapping):
        return None
    live = {}
    for name in HEARTBEAT_LIVE_FIELDS:
        value = status.get(name)
        if value is None or type(value) in (bool, int, float):
            live[name] = value
        elif isinstance(value, str) and _PLAIN_NAME.fullmatch(value):
            live[name] = value  # a state name, a level, an error class name ("loop:<Class>")
        else:
            live[name] = None
    return live


def _validate_heartbeat(values: Mapping, current: Mapping | None, report, environ=None) -> None:
    """The shape always; a bound only on a field the request moves (:func:`field_moved`)."""
    heartbeat = values["heartbeat"]
    if type(heartbeat.get("enabled")) is not bool:
        report("heartbeat.enabled", "not_boolean")
    good = {}
    low, high = HEARTBEAT_INTERVAL_LIMITS["min"], HEARTBEAT_INTERVAL_LIMITS["max"]
    for name in HEARTBEAT_INTERVALS:
        field, number = f"heartbeat.{name}", _number(heartbeat.get(name))
        if number is None:
            report(field, "not_number")
            continue
        if not low <= number <= high and field_moved(values, current, field):
            # Seconds; the page shows them as minutes / hours (its own code, not "out_of_range").
            report(field, "interval_out_of_range", min=low, max=high)
            continue
        good[name] = number
    floor, ceil = good.get("interval_floor_sec"), good.get("interval_ceil_sec")
    # Only when a bound moves (#512): a pair reset on load is shown as the file holds it and
    # must not block saving the switch or anything else.
    if (
        floor is not None
        and ceil is not None
        and floor > ceil
        and field_moved(
            values, current, "heartbeat.interval_floor_sec", "heartbeat.interval_ceil_sec"
        )
    ):
        report("heartbeat.interval_floor_sec", "floor_above_ceil", limit=ceil)
    if current is not None:
        for field, name in heartbeat_env_pins(environ).items():
            if field_moved(values, current, field):
                report(field, "env_pinned", env=name)


def delegation_values(gateway) -> dict:
    """``delegate`` (#486, ADR 0016): gateway ``delegation`` with the defaults filled in.

    A section that does not validate (cannot happen after ``GatewayConfig`` loaded it)
    is shown as off: the runtime treats it so too."""
    try:
        return DelegationSettings.from_options(getattr(gateway, "delegation", None)).as_dict()
    except ValueError:
        return {**DelegationSettings().as_dict(), "enabled": False}


def gateway_delegation(values: Mapping) -> dict:
    """The gateway ``delegation`` object to save: only the keys that differ from the
    defaults (an all-default section is not written)."""
    defaults = DelegationSettings().as_dict()
    return {key: values[key] for key in defaults if key in values and values[key] != defaults[key]}


def agent_jobs_values(gateway) -> dict:
    """ADR 0021 S2: gateway ``agent_jobs`` with the defaults filled in (bad = off)."""
    try:
        return AgentJobSettings.from_options(getattr(gateway, "agent_jobs", None)).as_dict()
    except ValueError:
        return AgentJobSettings().as_dict()


def gateway_agent_jobs(values: Mapping) -> dict:
    """The gateway ``agent_jobs`` object to save: only keys that differ from the defaults."""
    defaults = AgentJobSettings().as_dict()
    return {key: values[key] for key in defaults if key in values and values[key] != defaults[key]}


def hub_values(gateway, stira_config, tool_config: ToolConfig, agent=None) -> dict:
    roots = agent.write_roots if agent is not None else WRITE_SCOPES[DEFAULT_WRITE_SCOPE]
    read_roots = (getattr(agent, "read_roots", None) if agent is not None else None) or READ_SCOPES[
        DEFAULT_READ_SCOPE
    ]
    return {
        "classifier": {"choice": classifier_choice(gateway)},
        "tools": tool_config.as_dict(),
        "breaker": breaker_values(stira_config, agent),
        "breaker_details": detail_values(stira_config),
        "write": {"roots": list(roots)},
        "read": {"roots": list(read_roots)},
        "board": board_values(gateway),
        "output": output_values(gateway),
        "cache": cache_values(gateway),
        "context": context_values(gateway),
        "heartbeat": heartbeat_values(gateway),
        "delegation": delegation_values(gateway),
        "agent_jobs": agent_jobs_values(gateway),
    }


def breaker_values(stira_config, agent=None) -> dict:
    """The six thresholds the page shows (#512: a set reset on load shows the file's values,
    an omitted one at its default, so entering any valid set is a change it can save)."""
    shown = getattr(agent, "shown_breaker_values", None)
    if callable(shown) and getattr(agent, "breaker_pairs_reset", False) is True:
        values = shown(stira_config)
        return {name: values[name] for name in BREAKER_SET}
    return {name: float(getattr(stira_config, name)) for name in BREAKER_SET}


def detail_values(stira_config) -> dict:
    """The details fold (#471): integers stay integers, the rest are floats."""
    return {
        name: (int if rule["integer"] else float)(getattr(stira_config, name))
        for name, rule in DETAIL_FIELDS.items()
    }


def fixed_values(stira_config, engine: Mapping | None = None) -> list[dict]:
    """Read-only rows: the running engine's value when known, else what the chat server uses.

    With no engine to ask, a server-fixed row shows the server's own choice
    (``stira_delivery.SERVER_CHOSEN``, else the built-in default), never a saved override the
    server ignores (#471 review m-3: a saved ``readonly`` is not what runs, ``halt`` is).
    """
    rows = []
    built_in = None
    for name, reason in FIXED_FIELDS.items():
        if engine is not None and name in engine:
            value = engine[name]
        elif reason == "server_fixed":
            if name in stira_delivery.SERVER_CHOSEN:
                value = stira_delivery.SERVER_CHOSEN[name]
            else:
                built_in = built_in or StiraConfig()
                value = getattr(built_in, name)
        else:
            value = getattr(stira_config, name)
        if isinstance(value, (set, frozenset, list, tuple)):
            value = sorted(value)
        rows.append({"name": name, "value": value, "reason": reason})
    return rows


# --- Presets ------------------------------------------------------------------
#
# Server-defined bundles of tool switches and breaker thresholds. No preset uses
# "trust_all" or switches on an exec-class tool (#163): exec tools stay behind
# STIRA_ENABLE_EXEC_TOOLS whatever preset is chosen.

PRESET_IDS = ("safe", "standard", "developer")
DEFAULT_PRESET = "standard"
# Tools whose effect leaves this machine (requests/notifications to other hosts).
EXTERNAL_SEND_TOOLS = tool_policy.EXTERNAL_SEND_TOOLS
SAFE_BREAKER = {"stability_warning": 0.55, "stability_critical": 0.4, "bc_threshold": 0.65}


def _preset_tool_names(preset_id: str) -> set[str]:
    read = set(tool_registry.READ_TOOLS) - tool_registry.EXEC_TOOL_NAMES
    write = set(tool_registry.WRITE_TOOLS) - tool_registry.EXEC_TOOL_NAMES
    if preset_id == "safe":
        return read
    if preset_id == "standard":
        return read | (write - EXTERNAL_SEND_TOOLS)
    if preset_id == "developer":
        return read | write
    raise KeyError(preset_id)


def preset_values(preset_id: str, current: Mapping) -> dict:
    """``current`` with the preset's tool switches and breaker thresholds applied.

    The classifier choice and the shell allowlist are left as they are.
    """
    if preset_id not in PRESET_IDS:
        raise KeyError(preset_id)
    defaults = StiraConfig()
    # A preset sets the whole set (#471): its entries with the default exits, so a preset
    # never leaves an entry next to a customised exit it was not designed with.
    breaker = {name: float(getattr(defaults, name)) for name in BREAKER_SET}
    if preset_id == "safe":
        breaker.update(SAFE_BREAKER)
    values = copy.deepcopy(dict(current))
    enabled = _preset_tool_names(preset_id)
    values["tools"] = {
        "mode": "custom",
        "enabled_tools": sorted(enabled),
        "shell_allowlist": list(current["tools"]["shell_allowlist"]),
        # A preset never turns an Ask rule into Always (#190): keep the saved Ask set.
        "ask_tools": sorted(set(current["tools"].get("ask_tools") or ()) & enabled),
    }
    values["breaker"] = breaker
    return values


def active_preset(values: Mapping) -> str | None:
    """The preset the saved values match exactly (ignoring unknown tool names), if any."""
    known = known_tool_names()
    tools = values["tools"]
    if tools.get("mode") != "custom":
        return None
    enabled = set(tools.get("enabled_tools") or ()) & known
    for preset_id in PRESET_IDS:
        candidate = preset_values(preset_id, values)
        if enabled == set(candidate["tools"]["enabled_tools"]) and all(
            math.isclose(float(values["breaker"][name]), candidate["breaker"][name])
            for name in BREAKER_SET
            if name in values["breaker"]
        ):
            return preset_id
    return None


def default_values(tool_path=None) -> dict:
    """What "reset to defaults" proposes: the standard preset and automatic classifier.

    The tool rules are the first-run ones (#516): reads Always, local writes Ask, external
    sends and exec off — never looser than a new install (the preset keeps that Ask set).
    """
    tools = ToolConfig.first_run_defaults(tool_path if tool_path is not None else DEFAULT_TOOL_PATH)
    base = {
        "classifier": {"choice": "auto"},
        "tools": {**tools.as_dict(), "shell_allowlist": list(tools.shell_allowlist)},
        "breaker": {},
        "breaker_details": detail_values(StiraConfig()),
        "write": {"roots": list(WRITE_SCOPES[DEFAULT_WRITE_SCOPE])},
        "read": {"roots": list(READ_SCOPES[DEFAULT_READ_SCOPE])},
        "board": {"svg": BOARD_SVG_DEFAULT},
        "output": {"max_output_tokens": None},
        "cache": {"enabled": False, "ttl": "5m"},
        "context": {"enabled": False, **_context_defaults()},
        "heartbeat": heartbeat_defaults(),
        "delegation": DelegationSettings().as_dict(),
        "agent_jobs": AgentJobSettings().as_dict(),
    }
    return preset_values(DEFAULT_PRESET, base)


def version_of(values: Mapping) -> str:
    """Stable fingerprint of the saved values; a stale draft cannot overwrite (#195)."""
    blob = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def constraints(stira_config) -> dict:
    """Rules the browser checks before saving; the server re-checks them."""
    return {
        "breaker": {
            name: {
                "min": 0.0,
                "max": 1.0,
                "below": float(getattr(stira_config, EXIT_FIELDS[name])),
            }
            for name in BREAKER_FIELDS
        },
        "breaker_order": ["stability_critical", "stability_warning"],
        # #471: entry -> its exit; an entry must stay below the exit saved with it.
        "breaker_exits": dict(EXIT_FIELDS),
        "breaker_details": copy.deepcopy(DETAIL_FIELDS),
        "danger_floors": dict(DANGER_BREAKER_FLOORS),
        "sycophancy_danger": dict(SYCOPHANCY_DANGER),
        "classifier_choices": list(CLASSIFIER_CHOICES),
        "tool_modes": list(TOOL_MODES),
        "write_scopes": copy.deepcopy(WRITE_SCOPES),
        "max_write_roots": MAX_WRITE_ROOTS,
        "read_scopes": copy.deepcopy(READ_SCOPES),
        "max_read_roots": MAX_READ_ROOTS,
        # #470: the cost groups (output cap, prompt cache, context management).
        "max_output_tokens": {"min": 1, "max": MAX_OUTPUT_TOKENS_LIMIT},
        "cache_ttls": list(CACHE_TTLS),
        "context": copy.deepcopy(CONTEXT_RULES),
        # #472: interval bounds in seconds (checked on a field the request moves).
        "heartbeat": {name: dict(HEARTBEAT_INTERVAL_LIMITS) for name in HEARTBEAT_INTERVALS},
        "delegation": {
            name: {"min": low, "max": high} for name, (low, high) in DELEGATION_LIMITS.items()
        },
        "agent_jobs": {
            name: {"min": low, "max": high} for name, (low, high) in AGENT_JOB_LIMITS.items()
        },
    }


def known_tool_names() -> set[str]:
    return set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def merge_changes(current: Mapping, changes: Mapping) -> dict:
    """Overlay a (partial) change set on the saved values; unknown keys are errors."""
    errors = []
    if not isinstance(changes, Mapping):
        raise HubError([{"field": "changes", "code": "not_object", "params": {}}])
    merged = copy.deepcopy(dict(current))
    for group, patch in changes.items():
        if group not in GROUPS:
            errors.append({"field": str(group), "code": "unknown_field", "params": {}})
            continue
        if not isinstance(patch, Mapping):
            errors.append({"field": group, "code": "not_object", "params": {}})
            continue
        for key, value in patch.items():
            if key not in merged[group]:
                errors.append({"field": f"{group}.{key}", "code": "unknown_field", "params": {}})
                continue
            merged[group][key] = copy.deepcopy(value)
    if errors:
        raise HubError(errors)
    return merged


def validate_values(
    values: Mapping,
    stira_config,
    *,
    sokudan_available: bool,
    known_tools: Iterable[str] | None = None,
    current: Mapping | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict]:
    """Return field-level errors (empty when the values can be saved).

    Values already saved in ``current`` (a classifier that is no longer
    installed, a tool name from another environment) are not re-flagged, so
    an unrelated change can still be saved.
    """
    errors: list[dict] = []

    def err(field, code, **params):
        errors.append({"field": field, "code": code, "params": params})

    choice = values["classifier"]["choice"]
    if choice not in CLASSIFIER_CHOICES:
        err("classifier.choice", "unknown_choice", allowed=list(CLASSIFIER_CHOICES))
    elif (
        choice == "sokudan"
        and not sokudan_available
        and (current is None or current["classifier"]["choice"] != "sokudan")
    ):
        err("classifier.choice", "not_installed")

    breaker = values["breaker"]
    numbers = {}
    shown = (current or {}).get("breaker") or {}
    for name in BREAKER_SET:
        if name not in breaker and name in EXIT_FIELDS.values():
            # An older draft without the exits: the saved exit stands (#471), as shown.
            numbers[name] = float(shown.get(name, getattr(stira_config, name)))
            continue
        value = _number(breaker.get(name))
        if value is None:
            err(f"breaker.{name}", "not_number")
            continue
        if not 0.0 <= value <= 1.0:
            err(f"breaker.{name}", "out_of_range", min=0.0, max=1.0)
            continue
        numbers[name] = value
    # The pair rules only when a threshold moves (like every bound, ``field_moved``): a set
    # reset on load (#512) is shown as the file holds it and must not block other saves.
    # Moving any threshold checks the whole set, so a saved set is always valid.
    pairs_moved = field_moved(values, current, *(f"breaker.{name}" for name in BREAKER_SET))
    # Only the pair rules are skipped (review r591 m-3): the shape and range checks above ran.
    pairs = numbers if pairs_moved else {}
    for name, exit_name in EXIT_FIELDS.items():
        if name in pairs and exit_name in pairs and pairs[name] >= pairs[exit_name]:
            if (
                current is not None
                and exit_name in breaker
                and breaker[exit_name] != current["breaker"].get(exit_name)
            ):
                # The exit was moved down to (or under) its entry: tie it to the exit.
                err(f"breaker.{exit_name}", "must_be_above", limit=numbers[name])
            else:
                err(f"breaker.{name}", "must_be_below", limit=numbers[exit_name])
    if (
        "stability_critical" in pairs
        and "stability_warning" in pairs
        and pairs["stability_critical"] >= pairs["stability_warning"]
    ):
        err(
            "breaker.stability_critical",
            "must_be_below_field",
            other="stability_warning",
            limit=numbers["stability_warning"],
        )
    if (
        "stability_critical_exit" in pairs
        and "stability_warning_exit" in pairs
        and pairs["stability_critical_exit"] >= pairs["stability_warning_exit"]
    ):
        err(
            "breaker.stability_critical_exit",
            "must_be_below_field",
            other="stability_warning_exit",
            limit=numbers["stability_warning_exit"],
        )
    details = values.get("breaker_details") or {}
    for name, rule in DETAIL_FIELDS.items():
        if name not in details:
            continue
        value = details[name]
        if rule["integer"]:
            if type(value) is not int:
                err(f"breaker_details.{name}", "not_integer")
                continue
        elif _number(value) is None:
            err(f"breaker_details.{name}", "not_number")
            continue
        low, high = rule["min"], rule["max"]
        if high is not None and not field_moved(values, current, f"breaker_details.{name}"):
            # Already saved (before the limit existed): only a field the request moves is
            # checked against it, so an unrelated save still works (#471 review M-1).
            high = None
        if (
            value < low
            or (rule.get("min_exclusive") and value <= low)
            or (high is not None and value > high)
        ):
            err(f"breaker_details.{name}", "out_of_range", min=low, max=rule["max"])
            continue
        numbers[name] = value if rule["integer"] else float(value)

    tools = values["tools"]
    if tools.get("mode") not in TOOL_MODES:
        err("tools.mode", "unknown_choice", allowed=list(TOOL_MODES))
    names = tools.get("enabled_tools")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        err("tools.enabled_tools", "not_list")
    else:
        known = known_tool_names() if known_tools is None else set(known_tools)
        saved = set(current["tools"]["enabled_tools"]) if current is not None else set()
        unknown = sorted(set(names) - known - saved)
        if unknown:
            err("tools.enabled_tools", "unknown_tool", names=unknown)
    ask = tools.get("ask_tools", [])
    if not isinstance(ask, list) or not all(isinstance(n, str) for n in ask):
        err("tools.ask_tools", "not_list")
    else:
        known = known_tool_names() if known_tools is None else set(known_tools)
        saved = set(current["tools"].get("ask_tools") or ()) if current is not None else set()
        unknown = sorted(set(ask) - known - saved)
        if unknown:
            err("tools.ask_tools", "unknown_tool", names=unknown)
    commands = tools.get("shell_allowlist")
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        err("tools.shell_allowlist", "not_list")
    else:
        bad = [c for c in commands if not c.strip() or any(x in c for x in _SHELL_OPERATORS)]
        if bad:
            err("tools.shell_allowlist", "invalid_command", count=len(bad))
        saved_commands = set(current["tools"]["shell_allowlist"]) if current is not None else set()
        secret = [c for c in secret_commands(commands) if c not in saved_commands]
        if secret:
            # Never stored, not even masked (review round 5): refused outright.
            err("tools.shell_allowlist", "secret_command", count=len(secret))
        elif current is not None and dict(tools) != dict(current["tools"]):
            # One saved by an older install is not written again either (#270): a save
            # that rewrites the tool rules waits until it is removed (the removal saves).
            kept = secret_commands(commands)
            if kept:
                err("tools.shell_allowlist", "secret_command_saved", count=len(kept))
    if type(values["board"].get("svg")) is not bool:
        err("board.svg", "not_boolean")
    _validate_cost(values, current, err, environ)
    _validate_heartbeat(values, current, err, environ)
    delegation = values.get("delegation")
    if not isinstance(delegation, Mapping):
        err("delegation", "invalid_combination")
    else:
        if type(delegation.get("enabled")) is not bool:
            err("delegation.enabled", "not_boolean")
        for name, (low, high) in DELEGATION_LIMITS.items():
            value = delegation.get(name)
            if type(value) is not int:
                err(f"delegation.{name}", "not_integer")
            elif not low <= value <= high:
                err(f"delegation.{name}", "out_of_range", min=low, max=high)
    agent_jobs = values.get("agent_jobs")
    if not isinstance(agent_jobs, Mapping):
        err("agent_jobs", "invalid_combination")
    else:
        if type(agent_jobs.get("enabled")) is not bool:
            err("agent_jobs.enabled", "not_boolean")
        for name, (low, high) in AGENT_JOB_LIMITS.items():
            value = agent_jobs.get(name)
            if type(value) is not int:
                err(f"agent_jobs.{name}", "not_integer")
            elif not low <= value <= high:
                err(f"agent_jobs.{name}", "out_of_range", min=low, max=high)
    roots = values["write"].get("roots")
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        err("write.roots", "not_list")
    elif len(roots) > MAX_WRITE_ROOTS:
        err("write.roots", "too_many", max=MAX_WRITE_ROOTS)
    else:
        bad = [r for r in roots if not _valid_root(r)]
        broad = [r for r in roots if r not in bad and r != WORKSPACE_ROOT and _broad(r)]
        if bad:
            err("write.roots", "invalid_root", roots=bad)
        elif broad:
            err("write.roots", "broad_root", roots=broad)
    read_roots = values["read"].get("roots")
    if not isinstance(read_roots, list) or not all(isinstance(r, str) for r in read_roots):
        err("read.roots", "not_list")
    elif not read_roots:
        err("read.roots", "empty")
    elif len(read_roots) > MAX_READ_ROOTS:
        err("read.roots", "too_many", max=MAX_READ_ROOTS)
    else:
        # A broad read root ("/", "~") is allowed (#457): the secret-path floor stays.
        bad = [r for r in read_roots if not _valid_root(r)]
        if bad:
            err("read.roots", "invalid_root", roots=bad)
    if not errors:
        # Final guard: the real dataclasses must accept the result.
        try:
            # A set shown as the file holds it (not moved) is not re-checked as a pair.
            replace(
                stira_config,
                **{k: v for k, v in numbers.items() if pairs_moved or k not in BREAKER_SET},
            )
        except (TypeError, ValueError):
            err("breaker", "invalid_combination")
        try:
            ToolConfig(
                mode=tools["mode"],
                enabled_tools=set(names),
                shell_allowlist=list(commands),
                ask_tools=set(ask),
            )
        except (TypeError, ValueError):
            err("tools", "invalid_combination")
        # The file's options already passed this check when GatewayConfig loaded them.
        if current is None or values["context"] != current["context"]:
            from stiracore.agent.context_management import ContextManagementSettings

            try:
                ContextManagementSettings.from_options(
                    values["context"]["enabled"], gateway_context_options(values["context"])
                )
            except (TypeError, ValueError):
                err("context", "invalid_combination")
    return errors


def _valid_root(root: str) -> bool:
    """``@workspace``, an absolute path or ``~/...``; no NUL, no relative paths."""
    if root == WORKSPACE_ROOT:
        return True
    if not root.strip() or root != root.strip() or "\0" in root:
        return False
    text = root.replace("\\", "/")
    return text.startswith(("/", "~/")) or text == "~" or (len(text) > 2 and text[1:3] == ":/")


def _broad(root: str) -> bool:
    """ "/", a drive root, home or its ancestors (opt-in only via agent-config)."""
    try:
        return broad_root_problem(root) is not None
    except (OSError, RuntimeError, ValueError):
        return True


def dangers(values: Mapping) -> list[str]:
    """Settings that remove safety margins; saving them needs an explicit confirmation."""
    found = []
    if values["tools"].get("mode") == "trust_all":
        found.append("trust_all")
    breaker = values["breaker"]
    if any(
        (_number(breaker.get(name)) or 0.0) < floor for name, floor in DANGER_BREAKER_FLOORS.items()
    ):
        found.append("breaker_low")
    if sycophancy_off(values.get("breaker_details") or {}):
        found.append("sycophancy_off")
    # ADR 0021 S2: unattended model calls on an operator's schedule (read-only tools only).
    if (values.get("agent_jobs") or {}).get("enabled") is True:
        found.append("agent_jobs_on")
    return found


def sycophancy_off(details: Mapping) -> bool:
    """The details switch the sycophancy alert practically off (``SYCOPHANCY_DANGER``)."""
    threshold = _number(details.get("sycophancy_threshold"))
    window = details.get("sycophancy_window_size")
    return (threshold is not None and threshold >= SYCOPHANCY_DANGER["threshold_at_least"]) or (
        type(window) is int and window < SYCOPHANCY_DANGER["window_below"]
    )


def _rule_maps(current: Mapping, candidate: Mapping) -> tuple[dict, dict]:
    names = known_tool_names() | set(current["tools"].get("enabled_tools") or ())
    names |= set(candidate["tools"].get("enabled_tools") or ())
    return (
        tool_policy.policies_from_values(current["tools"], names),
        tool_policy.policies_from_values(candidate["tools"], names),
    )


def loosened_tools(current: Mapping, candidate: Mapping, *, locked: bool = False) -> list[str]:
    """Every way the candidate's tool settings are less strict (one shared check,
    ``tool_config.rules_loosening``: mode, each tool's rule, shell allowlist, ...)."""
    return rules_loosening(current["tools"], candidate["tools"], locked=locked)


def always_write_tools(current: Mapping, candidate: Mapping) -> list[str]:
    """Write/external tools the candidate makes Always: their names must be typed."""
    return tool_policy.newly_always_writes(*_rule_maps(current, candidate))


def tool_rule_change(current: Mapping, candidate: Mapping, *, locked: bool = False) -> dict:
    """The single loosening decision for one tool-rule save, on every path (#270).

    The hub, its rollback and the legacy/new tool APIs all take it once per save and
    derive the confirmation, the breaker refusal, the audit order and the names to
    type from it. ``locked``: the save writes over locked rules (a recovery, always a
    loosening). Returns ``{"locked", "loose", "always_writes"}``.
    """
    return {
        "locked": locked,
        "loose": loosened_tools(current, candidate, locked=locked),
        "always_writes": always_write_tools(current, candidate),
    }


def secret_allowlist(values: Mapping) -> list[dict]:
    """Saved allowlist entries that look like they hold a secret, for the page to mark
    for removal (#270): their index in ``values["tools"]["shell_allowlist"]`` and a
    masked text to show instead of the value."""
    commands = values["tools"].get("shell_allowlist") or []
    found = set(secret_commands(commands))
    return [
        {"index": index, "masked": mask_secrets(command)}
        for index, command in enumerate(commands)
        if command in found
    ]


def typed_names_match(typed: Any, required: list[str]) -> bool:
    """``typed`` (the names the operator typed) is exactly ``required``."""
    if not required:
        return True
    if not isinstance(typed, list) or not all(isinstance(name, str) for name in typed):
        return False
    return sorted({name.strip() for name in typed if name.strip()}) == sorted(required)


def read_roots_widened(current: Mapping, candidate: Mapping) -> list[str]:
    """Read roots the candidate adds (#457): each one is a loosening (confirmed, recorded)."""
    before = set((current.get("read") or {}).get("roots") or ())
    after = (candidate.get("read") or {}).get("roots") or ()
    return [root for root in after if root not in before]


def write_roots_widened(current: Mapping, candidate: Mapping) -> list[str]:
    """Write roots the candidate adds (#536): confirmed and recorded, as read roots are.

    The secret-path floor still applies to every write whatever the roots."""
    before = set((current.get("write") or {}).get("roots") or ())
    after = (candidate.get("write") or {}).get("roots") or ()
    return [root for root in after if root not in before]


def new_dangers(
    current: Mapping,
    candidate: Mapping,
    *,
    loose: list[str] | None = None,
    locked: bool = False,
) -> list[str]:
    """New dangers, plus any loosening of the tool settings (typed confirmation, #190).

    ``loose``/``locked`` come from the caller's single loosening decision; a save over
    locked rules (a recovery) always needs the loosening confirmation. A read root the
    candidate adds is ``read_roots_widened`` (#457), a write root ``write_roots_widened``
    (#536), whatever was there before.
    """
    before = set(dangers(current))
    found = [code for code in dangers(candidate) if code not in before]
    # #453: the board draws pictures from model output (an allow-listed, rebuilt SVG shown
    # as an image). On is the default (ADR 0012 addendum 2026-10-24), so it is no standing
    # danger; turning it back on is a loosening of what the page renders: confirmed.
    if board_turned_on(current, candidate):
        found.append("board_svg")
    if read_roots_widened(current, candidate):
        found.append("read_roots_widened")
    if write_roots_widened(current, candidate):
        found.append("write_roots_widened")
    if loose is None:
        loose = loosened_tools(current, candidate, locked=locked)
    # "trust_all" already names the widest loosening; do not ask twice for one change,
    # except for a recovery, which is confirmed as such.
    if loose and (locked or "trust_all" not in found):
        found.append("tool_policy_loosened")
    return found


def board_turned_on(current: Mapping, candidate: Mapping) -> bool:
    """#453: the candidate turns the Stage board's static SVG on (from off)."""
    return (candidate.get("board") or {}).get("svg") is True and (current.get("board") or {}).get(
        "svg"
    ) is not True


def diff_values(before: Mapping, after: Mapping) -> list[dict]:
    """Field-level changes, e.g. ``{"field": "tools.enabled_tools", "added": [...]}``."""
    changes = []
    for group in GROUPS:
        for key in sorted(set(before.get(group, {})) | set(after.get(group, {}))):
            old, new = before.get(group, {}).get(key), after.get(group, {}).get(key)
            if old == new:
                continue
            entry = {"field": f"{group}.{key}"}
            if isinstance(old, list) and isinstance(new, list):
                entry["added"] = sorted(set(new) - set(old))
                entry["removed"] = sorted(set(old) - set(new))
            else:
                entry["from"], entry["to"] = old, new
            changes.append(entry)
    return changes


# --- Change history (#195): JSONL, append-only, no secret values ----------------

HISTORY_FILE = "settings-history.jsonl"
HISTORY_READ_LIMIT = 200
_HISTORY_LOCK = threading.Lock()


def _redact_commands(values: Mapping) -> tuple[dict, bool]:
    """Mask anything secret-looking in allowlist commands; report whether it did."""
    values = copy.deepcopy(dict(values))
    tools = values.get("tools")
    if not isinstance(tools, dict) or not isinstance(tools.get("shell_allowlist"), list):
        return values, False
    masked = mask_secrets(tools["shell_allowlist"])
    redacted = masked != tools["shell_allowlist"]
    tools["shell_allowlist"] = masked
    return values, redacted


def history_entry(before: Mapping, after: Mapping, source: str, *, now=None) -> dict | None:
    """One history record, or None when nothing changed.

    Only hub fields are stored (classifier choice, tool switches, literal
    allowlist commands, thresholds); allowlist commands pass through
    ``mask_secrets`` and a masked entry cannot be rolled back for that field.
    """
    changes = diff_values(before, after)
    if not changes:
        return None
    fields = {change["field"] for change in changes}
    groups = sorted({field.split(".", 1)[0] for field in fields})
    safe_before, redacted_before = _redact_commands({g: before[g] for g in groups})
    safe_after, redacted_after = _redact_commands({g: after[g] for g in groups})
    redacted = redacted_before or redacted_after
    if redacted:
        changes = diff_values(safe_before, safe_after) or [{"field": "tools.shell_allowlist"}]
    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": uuid.uuid4().hex[:12],
        "at": timestamp,
        "source": source,
        "changes": changes,
        "before": safe_before,
        "after": safe_after,
        "redacted_fields": ["tools.shell_allowlist"] if redacted else [],
    }


def recovery_entry(before: Mapping, after: Mapping, source: str, reason: str) -> dict:
    """The audit of unlocking locked tool rules, even when the values do not change."""
    entry = history_entry(before, after, source)
    if entry is None:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "changes": [],
            "before": {},
            "after": {},
            "redacted_fields": [],
        }
    entry["recovered_lock"] = mask_secrets(str(reason))
    return entry


def aborted_entry(entry: Mapping) -> dict:
    """Marks an audited change whose save did not happen (the audit is written first)."""
    return {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": f"aborted:{entry.get('id')}",
        "changes": [],
        "before": {},
        "after": {},
        "redacted_fields": [],
    }


def model_history_entry(before: Mapping, after: Mapping, *, now=None) -> dict | None:
    """Record a model/provider switch (display only; roll back from the model section)."""
    keys = ("provider", "model_id")
    old = {k: before.get(k) for k in keys}
    new = {k: after.get(k) for k in keys}
    if old == new:
        return None
    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": uuid.uuid4().hex[:12],
        "at": timestamp,
        "source": "model",
        "changes": [
            {"field": f"model.{k}", "from": mask_secrets(old[k]), "to": mask_secrets(new[k])}
            for k in keys
            if old[k] != new[k]
        ],
        "before": {},
        "after": {},
        "redacted_fields": [],
    }


def append_history(path, entry: Mapping | None) -> None:
    if entry is None:
        return
    path = Path(path)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with _HISTORY_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)


def read_history(path, limit: int = 20) -> list[dict]:
    """Newest first; unreadable lines are skipped (the file is append-only)."""
    path = Path(path)
    if not path.exists():
        return []
    with _HISTORY_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()[-HISTORY_READ_LIMIT:]
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def rollback_candidate(entry: Mapping, current: Mapping) -> dict:
    """``current`` with the fields ``entry`` changed set back to their earlier values."""
    candidate = copy.deepcopy(dict(current))
    redacted = set(entry.get("redacted_fields") or ())
    before = entry.get("before") or {}
    touched = False
    for change in entry.get("changes") or ():
        field = change.get("field", "")
        group, _, key = field.partition(".")
        if field in redacted or group not in GROUPS or key not in candidate[group]:
            continue
        if key not in before.get(group, {}):
            continue
        candidate[group][key] = copy.deepcopy(before[group][key])
        touched = True
    if not touched:
        raise HubError([{"field": "history", "code": "not_restorable", "params": {}}])
    return candidate


# --- Runtime helpers (used by stira_server) ------------------------------


def apply_breaker_thresholds(holder, thresholds: Mapping[str, float]) -> dict:
    """Publish validated thresholds on ``holder.config`` (the live engine); return the applied.

    The entry thresholds and their recovery thresholds (``BREAKER_SET``, the same fields as
    ``stira_delivery.NEXT_TURN_FIELDS``)
    go out as one set: ``dataclasses.replace`` validates all of them first (an invalid
    combination raises and leaves the running config untouched), and the validated copy
    replaces ``holder.config`` in a single assignment. Threads that read ``holder.config``
    see the old set or the new set, never a pair in between. Editing the shared object field
    by field would show a reader e.g. an entry of 0.70 with the old exit of 0.65.
    The result lists the whole set (#471: what the hub shows and compares).
    """
    values = {
        name: float(thresholds[name])
        for name in stira_delivery.NEXT_TURN_FIELDS
        if name in thresholds
    }
    holder.config = replace(holder.config, **values)
    return {name: float(getattr(holder.config, name)) for name in BREAKER_SET}


def reset_breaker_thresholds(running) -> dict[str, float]:
    """The thresholds a turn applies while the saved set is reset on load (#512).

    Per field the stricter of the running engine's value and the default: every one of
    the six restricts earlier (entries) or recovers later (exits) when higher
    (``StiraEngine._evaluate_breaker``: a level is entered at ``value < entry`` and left
    only at ``value > exit``, for stability and bcScore alike), so the set is
    never looser than the defaults nor than what already runs. The per-field maximum of two
    valid sets is valid (each ``<`` rule holds for both, so for their maxima).
    """
    defaults = StiraConfig()
    return {
        name: max(float(getattr(running, name)), float(getattr(defaults, name)))
        for name in stira_delivery.NEXT_TURN_FIELDS
    }


def filter_tools_by_config(
    tools: Iterable[Any],
    tool_config: ToolConfig,
    name_of: Callable[[Any], str | None],
    registry_names: Iterable[str] | None = None,
) -> list[Any]:
    """Drop registry tools the saved switches turn off.

    Only names the tool registry knows are governed by ToolConfig; tools a
    server binds itself (e.g. Stira memory tools) are left to the breaker/HITL.
    This only ever removes tools: the registry, exec gate and breaker decide
    what may be offered in the first place.
    """
    tools = list(tools)
    if getattr(tool_config, "integrity_error", None):
        return []  # locked rules (#190): nothing is offered, not even unregistered tools
    if tool_config.mode == "trust_all":
        return tools
    governed = known_tool_names() if registry_names is None else set(registry_names)
    enabled = set(tool_config.enabled_tools)
    kept = []
    for tool in tools:
        name = name_of(tool)
        if name is None or name not in governed or name in enabled:
            kept.append(tool)
    return kept


def startup_classifier(gateway, get_classifier: Callable[..., Any] | None = None):
    """Classifier for an explicit choice; ``None`` keeps the agent's automatic pick."""
    choice = classifier_choice(gateway)
    if choice == "auto":
        return None
    if get_classifier is None:
        from stiracore.agent.stimulus_classifier import get_classifier
    return get_classifier(prefer_sokudan=choice == "sokudan")


def expected_classifier_name(choice: str, sokudan_available: bool) -> str:
    if choice == "sokudan" or (choice == "auto" and sokudan_available):
        return "SokudanClassifier"
    return "RuleBasedClassifier"


__all__ = [
    "BREAKER_FIELDS",
    "BREAKER_SET",
    "CACHE_TTLS",
    "CLASSIFIER_CHOICES",
    "CONTEXT_OPTION_FIELDS",
    "COST_CONFIG_FIELDS",
    "DEFAULT_PRESET",
    "DEFAULT_WRITE_SCOPE",
    "DETAIL_FIELDS",
    "DETAIL_LIMITS",
    "EXTERNAL_SEND_TOOLS",
    "FIXED_FIELDS",
    "GROUPS",
    "HEARTBEAT_CONFIG_FIELDS",
    "HEARTBEAT_INTERVALS",
    "HEARTBEAT_INTERVAL_LIMITS",
    "HISTORY_FILE",
    "PRESET_IDS",
    "SYCOPHANCY_DANGER",
    "WRITE_SCOPES",
    "HubError",
    "aborted_entry",
    "active_preset",
    "always_write_tools",
    "append_history",
    "apply_breaker_thresholds",
    "board_turned_on",
    "classifier_choice",
    "constraints",
    "cost_env_pins",
    "cost_model_info",
    "dangers",
    "default_values",
    "detail_values",
    "diff_values",
    "effective_context_enabled",
    "effective_heartbeat_enabled",
    "effective_prompt_cache",
    "expected_classifier_name",
    "field_moved",
    "filter_tools_by_config",
    "fixed_values",
    "gateway_overrides_for_choice",
    "gateway_overrides_for_cost",
    "gateway_overrides_for_heartbeat",
    "heartbeat_defaults",
    "heartbeat_env_pins",
    "heartbeat_live",
    "heartbeat_values",
    "history_entry",
    "hub_values",
    "loosened_tools",
    "merge_changes",
    "model_history_entry",
    "new_dangers",
    "preset_values",
    "read_history",
    "recovery_entry",
    "reset_breaker_thresholds",
    "rollback_candidate",
    "secret_allowlist",
    "startup_classifier",
    "sycophancy_off",
    "tool_rule_change",
    "typed_names_match",
    "validate_values",
    "version_of",
    "write_scope",
]
