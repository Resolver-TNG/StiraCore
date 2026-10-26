"""Shared Settings and tool-permission routes for both dashboard servers."""

from __future__ import annotations

import contextlib
import os
import re
import threading
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, redirect, render_template, request

from stiracore.agent.model_factory import DEFAULT_MODELS
from stiracore.config import AgentConfig, GatewayConfig, resolve_config, stira_delivery
from stiracore.config import settings_surface as surface
from stiracore.config.models import DEFAULT_AGENT_CONFIG_PATH, DEFAULT_GATEWAY_CONFIG_PATH
from stiracore.config.resolver import model_spec_from
from stiracore.observability import settings_hub as hub
from stiracore.observability.stage import stage_enabled
from stiracore.providers import (
    PROVIDERS,
    capabilities_for,
    normalize_provider,
    provider_available,
)
from stiracore.providers.connection_test import (
    DEFAULT_TIMEOUT_SEC,
    PROBE_MAX_TOKENS,
    ConnectionTestResult,
    run_connection_test,
)
from stiracore.providers.credentials import (
    LOCAL_PROVIDERS,
    MissingCredentialError,
    credential_status,
    is_loopback_url,
    require_credentials,
    validate_base_url,
    validate_env_name,
)
from stiracore.tools import tool_policy, tool_profiles, tool_registry
from stiracore.tools.tool_config import (
    ALL_BLOCKED,
    RECOVER_WORD,
    WRITE_BLOCKED,
    ToolConfig,
    ToolRulesLocked,
    anchor_lock_path,
    anchor_path,
    head_path,
    latch_path,
    level_name,
    secret_commands,
)
from stiracore.tools.tool_config import ANCHOR_ENV as TOOL_ANCHOR_ENV
from stiracore.tools.tool_config import CONFIG_KEYS as TOOL_CONFIG_KEYS
from stiracore.tools.tool_config import DEFAULT_PATH as DEFAULT_TOOL_CONFIG_PATH
from stiracore.yolo import allow_runtime
from stiracore.yolo import runtime as yolo_runtime
from stiracore.yolo.loosening import allow_reserved_keys
from stiracore.yolo.targets import SCOPES as YOLO_SCOPES
from stiracore.yolo.targets import yolo_targets

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOUL_PATH = REPO_ROOT / "examples" / "personas" / "resolver" / "SOUL.md"
SOUL_PREVIEW_CHARS = 4000
_JEV_CLASSIFIERS = (
    {"id": "auto", "label": "Automatic (Sokudan when installed, else rule-based)"},
    {"id": "rule", "label": "Rule-based (dependency-free)"},
    {"id": "sokudan", "label": "Sokudan Jev classifier (ja)"},
)
_JEV_CLASSIFIER_IDS = frozenset(item["id"] for item in _JEV_CLASSIFIERS)
# Reported as ``editable_fields`` by GET /api/config; they are edited in the settings hub
# (the old /api/config/breaker write route is gone, #494).
_BREAKER_EDITABLE_FIELDS = ("stability_warning", "stability_critical", "bc_threshold")
# How a hub save was made, recorded in the change history (#195).
_HUB_SOURCES = frozenset(
    {
        "manual",
        "defaults",
        *(f"preset:{p}" for p in hub.PRESET_IDS),
        # #450: a tool profile put in the draft (recorded as such in the history).
        *(f"profile:{p}" for p in tool_profiles.PROFILE_IDS),
        # ADR 0025 §8/§9: Make Always from an approval card (only with its save token).
        "approval_card",
    }
)


_MISSING = object()


class _CardStale(Exception):
    """ADR 0025 §9: the card's save token went stale between its spend and the commit."""


_CARD_MESSAGES = {
    "stale_confirmation": "Something changed since this confirmation was shown; nothing was "
    "saved. Check the result and confirm again.",
    "op_token_used": "This confirmation was already sent; nothing more was saved.",
    "call_not_finished": "You can save once the approved call has run.",
}


def _card_refusal(code: str):
    return (
        jsonify(
            error=_CARD_MESSAGES.get(code, "Not saved."),
            code=code,
            error_code=code,
            errors=[],
        ),
        409,
    )


def _bool_or_none(value) -> bool | None:
    """A runtime switch as reported: a real boolean, else ``None`` (never another value)."""
    return value if type(value) is bool else None


_SOKUDAN_AVAILABLE: bool | None = None


def _sokudan_available() -> bool:
    """Whether ``sokudan`` imports; cached per process (#540 §3).

    The first import pulls in transformers and can take tens of seconds, so it runs at
    most once. ``clear_sokudan_cache()`` forces a re-check (e.g. after installing it).
    """
    global _SOKUDAN_AVAILABLE
    if _SOKUDAN_AVAILABLE is None:
        try:
            import_module("sokudan")
        except ImportError:
            _SOKUDAN_AVAILABLE = False
        else:
            _SOKUDAN_AVAILABLE = True
    return _SOKUDAN_AVAILABLE


def clear_sokudan_cache() -> None:
    """Forget the cached ``_sokudan_available()`` result."""
    global _SOKUDAN_AVAILABLE
    _SOKUDAN_AVAILABLE = None


def _resolved_classifier(choice: str, sokudan_available: bool) -> tuple[str, bool]:
    """``(class name, fallback)`` the runtime picks for ``choice`` at the next start (#162).

    The same resolution as ``startup_classifier`` / ``get_classifier``: ``auto`` and
    ``sokudan`` give Sokudan when it is installed; otherwise both fall back to the
    rule-based classifier (``fallback`` is True only when Sokudan was asked for).
    """
    if choice in ("auto", "sokudan") and sokudan_available:
        return "SokudanClassifier", False
    return "RuleBasedClassifier", choice == "sokudan"


def _clone_gateway_config(gateway: GatewayConfig, **overrides) -> GatewayConfig:
    data = gateway.as_dict()
    data.update(overrides)
    clone = GatewayConfig(**data, path=gateway.path)
    if gateway.hb_intervals_reset and not set(overrides) & {
        "hb_interval_floor_sec",
        "hb_interval_ceil_sec",
    }:
        # #512 (review r568 M-2): bounds reset on load stay reset (and the file's bounds
        # are written back) until a person saves bounds.
        clone.keep_reset_intervals(gateway.hb_intervals_file)
    return clone


def _clone_agent_config(agent: AgentConfig, **overrides) -> AgentConfig:
    data = agent.as_dict()
    data.pop("tool_config", None)
    data.update(overrides)
    clone = AgentConfig(**data, tool_config=agent.tool_config, path=agent.path)
    if agent.breaker_pairs_reset and "stira_config_overrides" not in overrides:
        # #512: thresholds reset on load stay reset (the file's are written back) until a
        # person saves thresholds; a caller passing the overrides decides (``persist``).
        clone.keep_reset_breaker_pairs(agent.breaker_pairs_file)
    return clone


def _stira_config_dict(config) -> dict:
    return {
        "stability_warning": config.stability_warning,
        "stability_critical": config.stability_critical,
        "bc_threshold": config.bc_threshold,
        "bc_window_size": config.bc_window_size,
        "decay_min_interval_sec": config.decay_min_interval_sec,
        "protected_layers": sorted(config.protected_layers),
        "sycophancy_threshold": config.sycophancy_threshold,
        "sycophancy_window_size": config.sycophancy_window_size,
        "stability_warning_exit": config.stability_warning_exit,
        "stability_critical_exit": config.stability_critical_exit,
        "bc_exit_threshold": config.bc_exit_threshold,
        "critical_action": config.critical_action,
        "critical_cooldown_turns": config.critical_cooldown_turns,
        "identity_anchors": list(config.identity_anchors),
        "blocked_tool_names": sorted(config.blocked_tool_names),
        "editable_fields": list(_BREAKER_EDITABLE_FIELDS),
    }


# Validated, saved and shown, but read by nothing at run time (#272): listed so a
# settings view never presents them as in force. ``autonomy_boundary`` is not
# enforced (TODO(#67) in core/engine.py); the server's data directory is fixed.
STORED_ONLY_FIELDS = (
    "state_base_path",
    "notify_targets",
    "hb_session_mode",
    "hb_task_policy",
    "autonomy_boundary",
)


def _resolved_config_dict(resolved) -> dict:
    return {
        "model_id": resolved.model_id,
        "max_output_tokens": resolved.max_output_tokens,
        "region": resolved.region,
        # Provider references only (#205): env var NAMES and readiness, never values.
        "provider": resolved.provider,
        "base_url": resolved.base_url,
        "api_key_env": resolved.api_key_env,
        "organization_env": resolved.organization_env,
        "credentials": resolved.credential_status(),
        "port": resolved.port,
        "state_base_path": resolved.state_base_path,
        "hb_interval_floor_sec": resolved.hb_interval_floor_sec,
        "hb_interval_ceil_sec": resolved.hb_interval_ceil_sec,
        "jev_enabled": resolved.jev_enabled,
        "notify_targets": list(resolved.notify_targets),
        "identity_anchors": list(resolved.identity_anchors),
        "soul_path": resolved.soul_path,
        "read_only_paths": list(resolved.read_only_paths),
        "tool_config": resolved.tool_config.as_dict(),
        "hb_session_mode": resolved.hb_session_mode,
        "hb_task_policy": resolved.hb_task_policy,
        "autonomy_boundary": resolved.autonomy_boundary,
        "stira_config": _stira_config_dict(resolved.stira_config),
        # True: the file's bc_window_size was rounded up to the minimum on load (#337).
        "bc_window_clamped": resolved.bc_window_clamped,
        "protected_layers_clamped": resolved.protected_layers_clamped,
        "hitl_timeout_clamped": resolved.hitl_timeout_clamped,
        # True: the file's heartbeat bounds had floor > ceil; the defaults are used (#512).
        "hb_intervals_reset": resolved.hb_intervals_reset,
        # True: the agent file's breaker thresholds broke a pair rule; the defaults are used.
        "breaker_pairs_reset": resolved.breaker_pairs_reset,
        "stored_only_fields": list(STORED_ONLY_FIELDS),
    }


def _model_catalog(gateway: GatewayConfig, agent: AgentConfig) -> list[str]:
    """Known model IDs: role defaults plus whatever is already configured."""
    catalog = list(dict.fromkeys(DEFAULT_MODELS.values()))
    for extra in (gateway.model_id, agent.model_override):
        if extra and extra not in catalog:
            catalog.append(extra)
    return catalog


def _model_roles(model_id: str) -> list[str]:
    return sorted(role.value for role, mid in DEFAULT_MODELS.items() if mid == model_id)


# Suggested model IDs per provider (#208). Any other ID can be entered; local
# servers (Ollama / llama.cpp) serve whatever model they have loaded.
_SUGGESTED_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-sonnet-4-5", "claude-haiku-4-5"),
    "openai": ("gpt-5-mini", "gpt-4.1-mini"),
    "openai_responses": ("gpt-5-mini",),
    "ollama": ("llama3.2:3b", "qwen3:8b", "llama3.2-vision:11b"),
    "llamacpp": ("local",),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro"),
    "mistral": ("mistral-large-latest", "pixtral-large-latest"),
}
_DEFAULT_BASE_URLS = {"ollama": "http://localhost:11434", "llamacpp": "http://localhost:8080"}
_MODEL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")
_ENV_PINS = ("STIRA_PROVIDER", "STIRA_MODEL", "STIRA_BASE_URL", "STIRA_API_KEY_ENV")
_SELECTION_KEYS = {"provider", "model_id", "base_url", "api_key_env"}


def _validate_model_id(value) -> str:
    """Model ID charset check; the message never echoes the (possibly pasted) input."""
    if not isinstance(value, str) or not _MODEL_ID_RE.fullmatch(value.strip()):
        raise ValueError("model_id must be 1-200 characters: letters, digits and . _ : / @ + -")
    return value.strip()


def _parse_selection(data: dict, fallback: dict | None = None) -> dict:
    """Validated provider/model/base_url/api_key_env from a request body.

    Missing fields come from ``fallback`` (the current selection); endpoint and
    key-name fields are inherited only while the provider stays the same. An
    empty string clears a field.
    """
    unknown = set(data) - _SELECTION_KEYS
    if unknown:
        raise ValueError("Unsupported field(s): " + ", ".join(sorted(unknown)))
    fallback = fallback or {}
    try:
        provider = normalize_provider(data.get("provider", fallback.get("provider")))
    except ValueError:
        raise ValueError("provider must be one of: " + ", ".join(sorted(PROVIDERS))) from None
    same_provider = provider == fallback.get("provider")
    model_id = _validate_model_id(
        data.get("model_id", fallback.get("model_id") if same_provider else None)
    )
    values = {}
    for key in ("base_url", "api_key_env"):
        value = data[key] if key in data else (fallback.get(key) if same_provider else None)
        values[key] = None if value in (None, "") else value
    base_url = values["base_url"]
    if base_url is not None:
        base_url = validate_base_url(base_url)
    if (
        "base_url" in data
        and base_url is not None
        and provider not in LOCAL_PROVIDERS
        and not is_loopback_url(base_url)
    ):
        # A browser must not be able to send a key reference to an arbitrary host.
        raise ValueError(
            "A non-loopback base_url for an API provider can only be set in "
            "gateway-config.json, not from the settings page"
        )
    api_key_env = values["api_key_env"]
    if api_key_env is not None:
        api_key_env = validate_env_name(api_key_env, "api_key_env")
    return {
        "provider": provider,
        "model_id": model_id,
        "base_url": base_url,
        "api_key_env": api_key_env,
    }


def _env_pins(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in _ENV_PINS if name in env]


def _current_selection(resolved) -> dict:
    return {
        "provider": resolved.provider,
        "model_id": resolved.model_id,
        "base_url": resolved.base_url,
        "api_key_env": resolved.api_key_env,
    }


def _public_credentials(status: dict) -> dict:
    """Readiness only (#205): kind, configured, variable NAME, warnings."""
    return {key: status.get(key) for key in ("kind", "configured", "env", "warnings")}


def _capability_badges(provider: str, model_id: str) -> dict:
    caps = capabilities_for(provider, model_id)
    return {
        "vision": caps.vision,
        "tools": caps.tools,
        "thinking": caps.thinking,
        "known": caps.known,
    }


def _provider_rows(current: dict) -> list[dict]:
    rows = []
    for name, info in PROVIDERS.items():
        is_current = name == current["provider"]
        ids = list(
            dict.fromkeys(DEFAULT_MODELS.values())
            if name == "bedrock"
            else _SUGGESTED_MODELS.get(name, ())
        )
        if is_current and current["model_id"] not in ids:
            ids.append(current["model_id"])
        status = credential_status(
            name,
            api_key_env=current["api_key_env"] if is_current else None,
            base_url=current["base_url"] if is_current else None,
        )
        rows.append(
            {
                "name": name,
                "phase": info.phase,
                "installed": provider_available(name),
                "extra": info.extra,
                "local": name in LOCAL_PROVIDERS,
                "default_base_url": _DEFAULT_BASE_URLS.get(name),
                "credentials": _public_credentials(status),
                "models": [
                    {
                        "id": model_id,
                        "roles": _model_roles(model_id) if name == "bedrock" else [],
                        "capabilities": _capability_badges(name, model_id),
                        "current": is_current and model_id == current["model_id"],
                    }
                    for model_id in ids
                ],
            }
        )
    return rows


def _hub_tools_url(query_string: bytes) -> str:
    """/tools now lives in the settings hub (#227); keep e.g. ``?lang=en``."""
    query = query_string.decode("latin-1")
    return f"/settings?{query}#sec-tools" if query else "/settings#sec-tools"


class ConfigStore:
    """Load, mutate, and save gateway/agent/tool config under one lock."""

    def __init__(self, gateway_path=None, agent_path=None, tool_path=None, history_path=None):
        self.gateway_path = Path(gateway_path or DEFAULT_GATEWAY_CONFIG_PATH)
        self.agent_path = Path(agent_path or DEFAULT_AGENT_CONFIG_PATH)
        self.tool_path = Path(tool_path or DEFAULT_TOOL_CONFIG_PATH)
        # Settings change history (#195): JSONL next to the tool config.
        self.history_path = Path(history_path or self.tool_path.parent / hub.HISTORY_FILE)
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            gateway = GatewayConfig.load(self.gateway_path)
            agent = AgentConfig.load(self.agent_path, tool_config_path=self.tool_path)
            return gateway, agent

    def resolved(self):
        gateway, agent = self.load()
        return resolve_config(gateway, agent)

    def protected_files(self) -> set[str]:
        """Rule, head, lock marker, history and config files: no agent tool may write
        them (#190 review: a policy the agent can rewrite is no policy). Absolute."""
        files = {
            self.tool_path,
            head_path(self.tool_path),
            latch_path(self.tool_path),
            self.history_path,
            self.gateway_path,
            self.agent_path,
        }
        anchor = anchor_path(self.tool_path)  # opt-in rollback anchor (#273)
        if anchor is not None:
            files |= {anchor, anchor_lock_path(self.tool_path)}
        return {str(Path(path).expanduser().absolute()) for path in files}

    def load_tools(self) -> ToolConfig:
        with self.lock:
            return ToolConfig.load(self.tool_path)


UNBOUND_REASON = "not_configured"


def _tool_rows(
    tool_config: ToolConfig,
    breaker_level,
    critical_action: str | None = None,
    unbound: object = None,
) -> tuple[list[dict], str]:
    """One row per registered tool, describing what the runtime would really expose.

    Exec-class tools (#163) are listed under their own ``exec`` category and are
    never reported as effective unless ``STIRA_ENABLE_EXEC_TOOLS`` opts them in,
    mirroring ``tool_registry.get_tools`` / ``ToolConfig.get_allowed_tools``.

    ``unbound`` (#554): ``{name: reason}`` of registry tools the running server did not
    bind (``memory_rate`` without ``memory_autonomy``), plus ``unconfigured_tools()``
    (``web_search`` without an endpoint, #583). Their saved rule is kept (it applies
    once the tool is bound), but the row is not enabled and its effective decision is
    Forbidden by the floor with ``not_configured``. Display only: the gate is unchanged.
    """
    unbound = (
        {str(name) for name in unbound if isinstance(name, str)}
        if isinstance(unbound, dict)
        else set()
    )
    # #583: a tool that needs a setting this process lacks (web_search without
    # STIRA_WEB_SEARCH_ENDPOINT) is shown the same way: kept rule, not configured.
    unbound |= set(tool_registry.unconfigured_tools())
    level = level_name(breaker_level)
    exec_exposed = tool_registry.exec_tools_enabled()
    rows = []
    for name in sorted(set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)):
        write = name in tool_registry.WRITE_TOOLS
        exec_class = tool_registry.is_exec_tool(name)
        configured = tool_config.mode == "trust_all" or name in tool_config.enabled_tools
        blocked = level in ALL_BLOCKED or ((write or exec_class) and level in WRITE_BLOCKED)
        exposed = exec_exposed or not exec_class
        bound = name not in unbound
        if exec_class:
            category = "exec"
        else:
            category = "write" if write else "read"
        row = {
            "name": name,
            "category": category,
            "exec_class": exec_class,
            "configured": configured,
            "exposed": exposed,
            "enabled": configured and exposed and bound and not blocked,
            "blocked_by_breaker": blocked,
            "unavailable_reason": (
                "exec_disabled" if not exposed else None if bound else UNBOUND_REASON
            ),
            # Always/Ask/Never + rule -> breaker -> floor stack (#190, ADR 0010).
            **tool_policy.tool_stack(
                tool_config, name, level, exposed=exposed, critical_action=critical_action
            ),
        }
        if exposed and not bound:
            floor = {**row["stack"]["floor"], "decision": tool_policy.FORBIDDEN}
            floor["reason"] = UNBOUND_REASON
            row["stack"] = {**row["stack"], "floor": floor}
            row["effective"] = tool_policy.effective(row["stack"])
        rows.append(row)
    return rows, level


def _same_tools(a: dict, b: dict) -> bool:
    return (
        a.get("mode") == b.get("mode")
        and set(a.get("enabled_tools") or ()) == set(b.get("enabled_tools") or ())
        and set(a.get("ask_tools") or ()) == set(b.get("ask_tools") or ())
    )


def _tool_profiles(current_tools: dict, rows: list[dict]) -> list[dict]:
    """#450: each tool profile as the page shows it before it is put in the draft — the
    resulting ``tools`` value, one row per tool (its rule, whether it needs
    ``STIRA_ENABLE_EXEC_TOOLS``, what a YOLO window would do with it) and the number of
    tools each YOLO scope could answer. Nothing is saved here."""
    names = [row["name"] for row in rows]
    exposed = tool_registry.exec_tools_enabled()
    profiles = []
    for profile_id in tool_profiles.PROFILE_IDS:
        tools = tool_profiles.profile_tools(profile_id, current_tools, names)
        policies = tool_policy.policies_from_values(tools, names)
        view = ToolConfig(
            mode=tools["mode"],
            enabled_tools=set(tools["enabled_tools"]),
            shell_allowlist=list(tools["shell_allowlist"]),
            ask_tools=set(tools["ask_tools"]),
        )
        targets = yolo_targets(view, names=names, exec_exposed=exposed)
        yolo = {item["name"]: item["reason"] for item in targets["excluded"]}
        before: set[str] = set()
        for scope in YOLO_SCOPES:
            for name in set(targets["scopes"][scope]["tools"]) - before:
                yolo[name] = scope
            before |= set(targets["scopes"][scope]["tools"])
        profiles.append(
            {
                "id": profile_id,
                "tools": tools,
                "rows": [
                    {
                        "name": row["name"],
                        "category": row["category"],
                        "policy": policies[row["name"]],
                        "in_table": row["name"] in tool_profiles.PROFILE_TOOLS,
                        "needs_env": bool(
                            row["exec_class"] and policies[row["name"]] != tool_policy.NEVER
                        ),
                        "env_set": exposed,
                        "yolo": yolo.get(row["name"]),
                    }
                    for row in rows
                ],
                "counts": {
                    policy: sum(1 for value in policies.values() if value == policy)
                    for policy in tool_policy.POLICIES
                },
                "yolo": {scope: targets["scopes"][scope]["count"] for scope in YOLO_SCOPES},
                "saved": _same_tools(tools, current_tools),
            }
        )
    return profiles


class _BreakerActive(Exception):
    """Raised right before commit: the breaker tripped after the first check (#190)."""


def _rule_changes(before: ToolConfig, after: ToolConfig) -> dict:
    """The tool APIs' one decision per save: the hub's ``tool_rule_change`` (#270).

    Every field (review round 4), and a save over locked rules (a recovery) always
    counts as loosening: these APIs always write the rules.
    """
    return hub.tool_rule_change(
        {"tools": before.as_dict()},
        {"tools": after.as_dict()},
        locked=bool(before.integrity_error),
    )


def _breaker_refusal_body(level: str, loose: list[str]) -> dict:
    return {
        "error": "Tool rules cannot be loosened while the breaker is active.",
        "error_code": "breaker_active",
        "breaker_level": level,
        "loosened": loose,
        "errors": [],
    }


def _locked_body(reason) -> dict:
    return {
        "error": "The tool rules are locked; saving them needs the recovery confirmation.",
        "error_code": "tool_rules_locked",
        "reason": str(reason),
        "recover_word": RECOVER_WORD,
        "errors": [],
    }


def _names_refusal_body(names: list[str]) -> dict:
    return {
        "error": "Type the names of the write/external tools being made Always.",
        "error_code": "needs_tool_names",
        "needs_tool_names": names,
        "errors": [],
    }


def _loosening_refusal(change: dict, level: str, confirmed: bool, typed=None):
    """Refuse loosening a tool rule during a breaker, or without a confirmation (#190).

    ``change``: the save's one decision (``_rule_changes``). Returns ``(body, status)``
    or None. Tightening never needs either. Making a write/external tool Always also
    needs its name typed (``confirm_always``).
    """
    loose, writes = change["loose"], change["always_writes"]
    if not loose:
        return None
    if level not in tool_policy.LOOSENING_LEVELS:
        return (_breaker_refusal_body(level, loose), 403)
    if not confirmed:
        return (
            {
                "error": "Confirmation required.",
                "error_code": "needs_confirmation",
                "needs_confirmation": ["tool_policy_loosened"],
                "loosened": loose,
                "errors": [],
            },
            400,
        )
    if not hub.typed_names_match(typed, writes):
        return (_names_refusal_body(writes), 400)
    return None


def _exec_status() -> dict:
    """Expose the exec gate as a fact (env var NAME only, never its value source)."""
    return {
        "exec_tools_enabled": tool_registry.exec_tools_enabled(),
        "exec_env_var": tool_registry.EXEC_ENV_VAR,
        "exec_tool_names": sorted(tool_registry.EXEC_TOOL_NAMES),
    }


def create_config_blueprint(
    config_store: ConfigStore,
    get_breaker_level: Callable[[], object] = lambda: None,
    effective_classifier_provider: Callable[[], dict] | None = None,
    running_model_provider: Callable[[], dict] | None = None,
    runtime_settings_provider: Callable[[], dict] | None = None,
    critical_action_provider: Callable[[], str] | None = None,
    on_tool_rules_saved: Callable[[], object] | None = None,
    config_location_provider: Callable[[], dict] | None = None,
    heartbeat_status_provider: Callable[[], dict] | None = None,
) -> Blueprint:
    """Create config routes backed by the injected store and live breaker provider.

    ``effective_classifier_provider`` reports the classifier the running agent
    really uses, so the settings view cannot silently diverge from runtime (#162).
    ``running_model_provider`` reports the model the running agent uses (#208).
    ``runtime_settings_provider`` reports the hub values the running agent has
    applied (``{"tools", "breaker", "classifier_choice", "error"}``), so the
    settings hub can say "applied" only when the runtime really uses them (#227).
    ``on_tool_rules_saved`` runs after every committed tool-rule save: the server
    voids the open approvals there at once (#287) instead of at the next gate call.
    If it raises, the save stands but the response is an error
    (``approvals_not_revoked``, fail-closed, #330 review): the host stops accepting
    approvals until they are reconciled, and the gate keeps refusing per call.
    ``config_location_provider`` reports whether the configuration files sit in the data
    directory (#272; ``{"matches_data_dir", "data_dir"}``, no path) for the settings
    hub's read-only location card (#469).
    ``heartbeat_status_provider`` reports the running heartbeat daemon (read-only, #472)
    for the settings page's heartbeat card; nothing here starts, stops or triggers a tick.
    """
    blueprint = Blueprint("config_api", __name__)
    # Saved values of the restart fields as the server started with them (#469): a field
    # saved since then waits for a restart. Unreadable files: no restart facts are shown.
    # Any failure (a broken manifest included) only hides the restart facts: the server
    # still starts and the page still saves (#490 review).
    try:
        with config_store.lock:
            startup_saved = surface.saved_snapshot(*config_store.load())
    except Exception:  # noqa: BLE001 - additive facts never block the start
        startup_saved = None
    # One connection test at a time: each one is a (small) billable request.
    connection_test_lock = threading.Lock()

    def running_model():
        if running_model_provider is None:
            return None
        try:
            value = running_model_provider()
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def breaker_level():
        try:
            return get_breaker_level()
        except (OSError, ValueError, TypeError):
            return "unknown"

    def tool_rules_saved(saved_groups=None):
        """None once the open approvals are voided; otherwise the error response."""
        if on_tool_rules_saved is None:
            return None
        try:
            on_tool_rules_saved()
        except Exception as error:  # noqa: BLE001 - reported as a failed save below
            current_app.logger.warning(
                "Open approvals could not be revoked after a tool-rule save (%s)",
                type(error).__name__,
            )
            return (
                jsonify(
                    error=(
                        "The tool rules were saved, but the open approvals could not be "
                        "revoked. Approvals are refused until this is resolved: check the "
                        "approval store, then save the rules again or restart the server."
                    ),
                    error_code="approvals_not_revoked",
                    rules_saved=True,
                    saved=list(saved_groups or ["tools"]),
                    errors=[],
                ),
                500,
            )
        return None

    def critical_action():
        """The live engine's CRITICAL action ("halt"/"readonly"); None = unknown (#190)."""
        if critical_action_provider is None:
            return None
        try:
            value = critical_action_provider()
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return value if value in ("halt", "readonly") else None

    @blueprint.before_request
    def protect_mutations():
        if request.method != "POST":
            return None
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.host
        ):
            return jsonify(error="Cross-origin mutations are not allowed."), 403
        return None

    @blueprint.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @blueprint.get("/tools")
    def tools_page():
        # One tool list only: the settings hub's Safety section (#227).
        return redirect(_hub_tools_url(request.query_string), code=302)

    @blueprint.get("/api/config")
    def get_config():
        with config_store.lock:
            try:
                # One load: the resolved values and which overrides were set come from it.
                gateway, agent = config_store.load()
                resolved = resolve_config(gateway, agent)
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            payload = _resolved_config_dict(resolved)
            # What the running engine really uses, next to what is saved (#272).
            runtime = runtime_settings() or {}
            payload["stira_config_apply"] = stira_delivery.apply_rows(
                resolved.stira_config,
                runtime.get("engine"),
                agent.stira_config_overrides,
                runtime.get("error"),
            )
            return jsonify(payload)

    @blueprint.get("/api/config/models")
    def get_config_models():
        with config_store.lock:
            try:
                gateway, agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            catalog = _model_catalog(gateway, agent)
            return jsonify(
                models=[{"id": model_id, "roles": _model_roles(model_id)} for model_id in catalog],
                gateway_model_id=gateway.model_id,
                agent_override=agent.model_override,
                effective_model_id=agent.model_override or gateway.model_id,
            )

    @blueprint.get("/api/config/providers")
    def get_config_providers():
        with config_store.lock:
            try:
                resolved = config_store.resolved()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            current = _current_selection(resolved)
        return jsonify(
            providers=_provider_rows(current),
            current=current,
            pinned_by_env=_env_pins(),
            running=running_model(),
            applies="next_turn" if running_model_provider is not None else "next_start",
            connection_test={"timeout_sec": DEFAULT_TIMEOUT_SEC, "max_tokens": PROBE_MAX_TOKENS},
        )

    @blueprint.post("/api/config/models/test")
    def post_config_models_test():
        data = request.get_json(silent=True)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object."), 400
        try:
            resolved = config_store.resolved()
        except (OSError, ValueError, TypeError) as error:
            return jsonify(error=f"Config unavailable: {error}"), 503
        try:
            selection = _parse_selection(data, _current_selection(resolved))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        spec = model_spec_from(
            selection["provider"],
            selection["model_id"],
            region=resolved.region,
            base_url=selection["base_url"],
            api_key_env=selection["api_key_env"],
            organization_env=(
                resolved.organization_env if selection["provider"] == resolved.provider else None
            ),
        )
        if not connection_test_lock.acquire(blocking=False):
            result = ConnectionTestResult(False, "busy", spec.provider, spec.model_id)
            return jsonify(result.as_dict()), 429
        try:
            # Tests (and embedders) may inject an offline model builder.
            builder = current_app.extensions.get("stira_model_builder")
            result = run_connection_test(spec, model_builder=builder)
        finally:
            connection_test_lock.release()
        return jsonify(result.as_dict())

    @blueprint.post("/api/config/models")
    def post_config_models():
        data = request.get_json(silent=True)
        if isinstance(data, dict) and "provider" in data:
            return post_provider_selection(data)
        if not isinstance(data, dict) or "model_id" not in data:
            return jsonify(error="Expected a JSON object with 'model_id'."), 400
        model_id = data["model_id"]
        if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
            return jsonify(error="model_id must be a nonempty string or null."), 400
        with config_store.lock:
            try:
                gateway, agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            catalog = _model_catalog(gateway, agent)
            if model_id is not None and model_id not in catalog:
                return jsonify(error="Unknown model id.", allowed=catalog), 400
            try:
                # A catalog pick (or reset) uses the gateway provider (#208).
                candidate = _clone_agent_config(
                    agent,
                    model_override=model_id,
                    provider_override=None,
                    base_url_override=None,
                    api_key_env_override=None,
                )
            except (TypeError, ValueError) as error:
                return jsonify(error=str(error)), 400
            try:
                candidate.save()
            except OSError:
                return jsonify(error="Could not persist agent configuration."), 500
            record_model_change(gateway, agent, candidate)
            return jsonify(
                agent_override=candidate.model_override,
                gateway_model_id=gateway.model_id,
                effective_model_id=candidate.model_override or gateway.model_id,
            )

    def record_model_change(gateway, before_agent, after_agent):
        """Append a model switch to the settings history (#195); never fails the save."""
        try:
            entry = hub.model_history_entry(
                _current_selection(resolve_config(gateway, before_agent)),
                _current_selection(resolve_config(gateway, after_agent)),
            )
            hub.append_history(config_store.history_path, entry)
        except (OSError, ValueError, TypeError):
            pass

    def post_provider_selection(data: dict):
        if "model_id" not in data:
            return jsonify(error="Expected 'provider' and 'model_id'."), 400
        try:
            current = _current_selection(config_store.resolved())
        except (OSError, ValueError, TypeError) as error:
            return jsonify(error=f"Config unavailable: {error}"), 503
        try:
            selection = _parse_selection(data, current)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if not provider_available(selection["provider"]):
            info = PROVIDERS[selection["provider"]]
            return (
                jsonify(
                    error="Provider SDK is not installed.",
                    reason="provider_unavailable",
                    extra=info.extra,
                ),
                400,
            )
        try:
            require_credentials(
                selection["provider"],
                api_key_env=selection["api_key_env"],
                base_url=selection["base_url"],
            )
        except MissingCredentialError as error:
            # Variable NAME only; the value is never read into a response.
            return (
                jsonify(error=str(error), reason="missing_credential", env=error.env_name),
                400,
            )
        with config_store.lock:
            try:
                gateway, agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            same_as_gateway = (
                selection["provider"] == gateway.provider
                and selection["base_url"] == gateway.base_url
                and selection["api_key_env"] == gateway.api_key_env
            )
            overrides = {
                "model_override": selection["model_id"],
                "provider_override": None if same_as_gateway else selection["provider"],
                "base_url_override": None if same_as_gateway else selection["base_url"],
                "api_key_env_override": None if same_as_gateway else selection["api_key_env"],
            }
            try:
                candidate = _clone_agent_config(agent, **overrides)
            except (TypeError, ValueError) as error:
                return jsonify(error=str(error)), 400
            try:
                candidate.save()
            except OSError:
                return jsonify(error="Could not persist agent configuration."), 500
            record_model_change(gateway, agent, candidate)
            resolved = resolve_config(gateway, candidate)
        return jsonify(
            saved=selection,
            current=_current_selection(resolved),
            agent_override=candidate.model_override,
            gateway_model_id=gateway.model_id,
            effective_model_id=resolved.model_id,
            pinned_by_env=_env_pins(),
            applies="next_turn" if running_model_provider is not None else "next_start",
        )

    def config_unavailable(error):
        """Fixed body (#330 review): the exception text (paths, values) stays in the log."""
        current_app.logger.warning("Config unavailable (%s)", type(error).__name__)
        return jsonify(error="Config unavailable.", error_code="config_unavailable"), 503

    @blueprint.get("/api/config/jev")
    def get_config_jev():
        with config_store.lock:
            try:
                gateway, _agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return config_unavailable(error)
            sokudan_available = _sokudan_available()
            classifiers = [
                dict(item, available=(item["id"] != "sokudan" or sokudan_available))
                for item in _JEV_CLASSIFIERS
            ]
            # One source with the settings hub (#162, #227): jev_enabled false is
            # "auto" (the runtime picks Sokudan when installed), never "rule".
            current = hub.classifier_choice(gateway)
            resolved, fallback = _resolved_classifier(current, sokudan_available)
            payload = {
                "classifiers": classifiers,
                "current": current,
                "jev_enabled": gateway.jev_enabled,
                "resolved": resolved,
                "fallback": fallback,
                # The choice is read once at server start (#227).
                "applies": "next_start",
            }
            # The choice the running server started with (read once at start, #227).
            startup = (runtime_settings() or {}).get("classifier_choice")
            if startup not in hub.CLASSIFIER_CHOICES:
                startup = None
            payload["startup_choice"] = startup
            if effective_classifier_provider is not None:
                try:
                    effective = effective_classifier_provider()
                except (OSError, ValueError, TypeError):
                    effective = None
                if isinstance(effective, dict):
                    payload["effective"] = effective
                    name = effective.get("name")
                    if name == "unresolved":
                        # Not resolved yet ("auto" resolves on the first turn): compare
                        # the choices instead; another one saved since the start needs a
                        # restart, an unknown start choice is unknown (None).
                        payload["effective_matches_config"] = None
                        payload["restart_required"] = (
                            None if startup is None else startup != current
                        )
                    else:
                        payload["effective_matches_config"] = name == resolved
                        payload["restart_required"] = name != resolved
            return jsonify(payload)

    @blueprint.post("/api/config/jev")
    def post_config_jev():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object."), 400
        classifier = data.get("classifier")
        enabled = data.get("jev_enabled")
        if "classifier" in data and classifier not in _JEV_CLASSIFIER_IDS:
            return (
                jsonify(error="Unknown classifier.", allowed=sorted(_JEV_CLASSIFIER_IDS)),
                400,
            )
        if "jev_enabled" in data and not isinstance(enabled, bool):
            return jsonify(error="jev_enabled must be a boolean."), 400
        if "classifier" not in data and "jev_enabled" not in data:
            return jsonify(error="Expected 'classifier' and/or 'jev_enabled'."), 400
        if "classifier" in data and "jev_enabled" in data and enabled != (classifier != "auto"):
            # "auto" is jev_enabled false; "rule"/"sokudan" are jev_enabled true (#162).
            return (
                jsonify(
                    error="'classifier' and 'jev_enabled' disagree.",
                    error_code="classifier_conflict",
                ),
                400,
            )
        with config_store.lock:
            try:
                gateway, _agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return config_unavailable(error)
            if "classifier" in data:
                # The posted value is the choice the runtime uses (one source, #162):
                # saving "rule" while jev_enabled stayed false used to leave "auto" on.
                overrides = hub.gateway_overrides_for_choice(classifier, gateway)
            else:
                overrides = {"jev_enabled": enabled}
            try:
                candidate = _clone_gateway_config(gateway, **overrides)
            except (TypeError, ValueError) as error:
                return jsonify(error=str(error)), 400
            try:
                candidate.save()
            except OSError:
                return jsonify(error="Could not persist gateway configuration."), 500
            current = hub.classifier_choice(candidate)
            resolved, fallback = _resolved_classifier(current, _sokudan_available())
            return jsonify(
                current=current,
                jev_enabled=candidate.jev_enabled,
                resolved=resolved,
                fallback=fallback,
                applies="next_start",
            )

    @blueprint.get("/api/config/tools")
    def get_config_tools():
        with config_store.lock:
            try:
                tool_config = config_store.load_tools()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Tool configuration unavailable: {error}"), 503
            rows, level = _tool_rows(
                tool_config, breaker_level(), critical_action(), unbound_tools()
            )
            return jsonify(tools=rows, mode=tool_config.mode, breaker_level=level, **_exec_status())

    @blueprint.post("/api/config/tools")
    def post_config_tools():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object."), 400
        known = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
        with config_store.lock:
            try:
                tool_config = config_store.load_tools()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Tool configuration unavailable: {error}"), 503
            ask = set(tool_config.ask_tools)
            policy = None
            if "name" in data:
                name = data.get("name")
                if not isinstance(name, str) or name not in known:
                    return jsonify(error="Unknown tool name.", allowed=sorted(known)), 400
                updated = set(tool_config.enabled_tools)
                if "policy" in data:
                    # Always / Ask / Never for one tool (#190, ADR 0010).
                    policy = data["policy"]
                    if policy not in tool_policy.POLICIES:
                        return (
                            jsonify(error="Unknown policy.", allowed=list(tool_policy.POLICIES)),
                            400,
                        )
                    if policy == tool_policy.ALWAYS and not tool_policy.always_allowed(name):
                        # No bounded scope can be stated for an exec-class grant.
                        return (
                            jsonify(
                                error="This tool cannot be Always; choose Ask or Never.",
                                error_code="always_not_allowed",
                            ),
                            400,
                        )
                    if policy == tool_policy.NEVER:
                        updated.discard(name)
                        ask.discard(name)
                    else:
                        updated.add(name)
                        (ask.add if policy == tool_policy.ASK else ask.discard)(name)
                else:
                    enabled = data.get("enabled")
                    if not isinstance(enabled, bool):
                        return jsonify(error="enabled must be a boolean."), 400
                    if enabled:
                        updated.add(name)
                    else:
                        updated.discard(name)
            elif "enabled_tools" in data:
                names = data["enabled_tools"]
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    return jsonify(error="enabled_tools must be an array of tool names."), 400
                unknown = sorted(set(names) - known)
                if unknown:
                    return jsonify(error="Unknown tool name(s).", unknown=unknown), 400
                updated = set(names)
            else:
                return jsonify(error="Expected 'name'+'enabled' or 'enabled_tools'."), 400
            mode = data.get("mode", tool_config.mode)
            # ADR 0015 phase 1: page_check is Ask or Never only; on means Ask in the file.
            ask = tool_policy.required_ask(updated, ask, mode)
            try:
                candidate = ToolConfig(
                    mode=mode,
                    enabled_tools=updated,
                    shell_allowlist=tool_config.shell_allowlist,
                    ask_tools=ask,
                    path=tool_config.path,
                )
            except (TypeError, ValueError) as error:
                return jsonify(error=str(error)), 400
            if policy is not None and candidate.policy_of(data["name"], effective=False) != policy:
                # e.g. Never under trust_all: never report a rule that is not in force.
                return (
                    jsonify(error="That policy does not apply in this mode.", error_code="mode"),
                    400,
                )
            return save_tool_config(tool_config, candidate, data, "api:tools")

    # #494: the old ``/api/config/breaker`` route (no screen called it) wrote the entry
    # thresholds without the hub's confirmation and history; breaker values are saved
    # through ``/api/config/hub`` (and its rollback) only.

    @blueprint.get("/api/config/soul")
    def get_config_soul():
        with config_store.lock:
            try:
                _gateway, agent = config_store.load()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            soul_path = Path(agent.soul_path) if agent.soul_path else DEFAULT_SOUL_PATH
        exists = soul_path.exists() and soul_path.is_file()
        preview = ""
        if exists:
            try:
                preview = soul_path.read_text(encoding="utf-8")[:SOUL_PREVIEW_CHARS]
            except OSError as error:
                return jsonify(error=f"Could not read SOUL file: {error}"), 503
        return jsonify(path=str(soul_path), exists=exists, preview=preview)

    # Keep the dedicated Tools page contract on the same live ConfigStore so
    # /api/config/tools and /api/tools/* cannot drift within one server.
    @blueprint.get("/api/tools/config")
    def get_tool_page_config():
        with config_store.lock:
            try:
                return jsonify(config_store.load_tools().as_dict())
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Tool configuration unavailable: {error}"), 503

    def commit_audited(entry, loosening: bool, commit, respond):
        """Loosening: audit first (refused if that fails), re-check the breaker right
        before the commit (same config lock), mark the audit aborted if it fails.
        Tightening: commit, then a best-effort audit (#190, ADR 0010 §4)."""
        if loosening and entry is not None:
            try:
                hub.append_history(config_store.history_path, entry)
            except (OSError, ValueError, TypeError):
                return (
                    jsonify(
                        error="The change could not be audited; nothing was saved.",
                        error_code="audit_failed",
                        errors=[],
                    ),
                    500,
                )

        def precommit():
            level = level_name(breaker_level())
            if loosening and level not in tool_policy.LOOSENING_LEVELS:
                raise _BreakerActive(level)

        def aborted():
            if loosening and entry is not None:
                with contextlib.suppress(OSError, ValueError, TypeError):
                    hub.append_history(config_store.history_path, hub.aborted_entry(entry))

        try:
            outcome = commit(precommit)
        except _BreakerActive as error:
            aborted()
            return jsonify(_breaker_refusal_body(str(error), [])), 403
        except _CardStale as error:
            aborted()
            return _card_refusal(str(error))
        except BaseException:
            aborted()
            raise
        if not loosening and entry is not None:
            with contextlib.suppress(OSError, ValueError, TypeError):
                hub.append_history(config_store.history_path, entry)
        return respond(outcome, entry)

    def save_tool_config(before: ToolConfig, candidate: ToolConfig, data: dict, source: str):
        """Loosening needs ``confirm: true`` and no active breaker; every save is recorded."""
        recover = data.get("recover") == RECOVER_WORD
        if before.integrity_error and not recover:
            return jsonify(_locked_body(before.integrity_error)), 409
        found = secret_commands(candidate.shell_allowlist)
        if set(found) - set(before.shell_allowlist):
            return (
                jsonify(
                    error="A command containing a secret cannot be allowlisted.",
                    error_code="secret_command",
                    errors=[],
                ),
                400,
            )
        if found:
            # Saved by an older install (#270): never written again. Remove it (settings
            # hub, tool permissions, shell allowlist) and the save goes through.
            return (
                jsonify(
                    error="The shell allowlist holds secret-looking commands; remove them "
                    "to save the tool rules.",
                    error_code="secret_command_saved",
                    count=len(found),
                    errors=[],
                ),
                400,
            )
        # One decision for the confirmation, the breaker and the audit order (#270).
        change = _rule_changes(before, candidate)
        refusal = _loosening_refusal(
            change,
            level_name(breaker_level()),
            data.get("confirm") is True,
            data.get("confirm_always", []),
        )
        if refusal is not None:
            return jsonify(refusal[0]), refusal[1]
        loosening = bool(change["loose"])
        if loosening or recover:
            # Only with STIRA_YOLO_ALLOW (ADR 0012 §8b): not while an agent tool call runs,
            # and only after a person confirmed exactly this save on the host (phase 2).
            running = yolo_runtime.tool_call_refusal()
            if running is not None:
                return jsonify(running), 409
            gate = host_confirmation(
                recover,
                data,
                {"tools": before.as_dict(), "integrity": str(before.integrity_error)},
                source,
                change["loose"],
            )
            if gate is not None:
                return gate
        if before.integrity_error:
            entry = hub.recovery_entry(
                {"tools": before.as_dict()},
                {"tools": candidate.as_dict()},
                source,
                before.integrity_error,
            )
        else:
            entry = hub.history_entry(
                {"tools": before.as_dict()}, {"tools": candidate.as_dict()}, source
            )
        committed = []

        def commit(precommit):
            candidate.save(precommit=precommit, recover=recover)
            committed.append(True)

        try:
            response = commit_audited(
                entry, loosening, commit, lambda _outcome, _entry: jsonify(candidate.as_dict())
            )
        except ToolRulesLocked as error:
            return jsonify(_locked_body(error)), 409
        except OSError:
            response = (jsonify(error="Could not persist tool configuration."), 500)
        if committed:
            # After the commit (outside commit_audited: the audit entry stands).
            failure = tool_rules_saved()
            if failure is not None:
                return failure
        return response

    @blueprint.post("/api/tools/config")
    def post_tool_page_config():
        if not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {
            *TOOL_CONFIG_KEYS,
            "confirm",
            "confirm_always",
            "recover",
        }:
            return jsonify(error="Invalid configuration fields"), 400
        fields = {
            key: value
            for key, value in data.items()
            if key not in ("confirm", "confirm_always", "recover")
        }
        known = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
        ask = fields.get("ask_tools", [])
        if not isinstance(ask, list) or not all(isinstance(n, str) and n in known for n in ask):
            # Names only from the registry: never store free text in the history (#190).
            return jsonify(error="ask_tools must list known tool names"), 400
        with config_store.lock:
            try:
                current = config_store.load_tools()
                merged = {**current.as_dict(), **fields}
                if isinstance(merged.get("enabled_tools"), list) and isinstance(
                    merged.get("ask_tools"), list
                ):
                    merged["ask_tools"] = sorted(
                        tool_policy.required_ask(
                            merged["enabled_tools"], merged["ask_tools"], merged.get("mode")
                        )
                    )
                candidate = ToolConfig(**merged, path=current.path)
            except (OSError, TypeError, ValueError) as error:
                return jsonify(error=str(error)), 400
            return save_tool_config(current, candidate, data, "api:tools")

    @blueprint.get("/api/tools/list")
    def get_tool_page_list():
        with config_store.lock:
            try:
                tool_config = config_store.load_tools()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Tool configuration unavailable: {error}"), 503
            rows, level = _tool_rows(
                tool_config, breaker_level(), critical_action(), unbound_tools()
            )
            return jsonify(tools=rows, breaker_level=level, **_exec_status())

    # --- Settings hub (#227) -------------------------------------------------

    def runtime_settings():
        if runtime_settings_provider is None:
            return None
        try:
            value = runtime_settings_provider()
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def unbound_tools():
        """#554: registry tools the running server did not bind (none known without it)."""
        value = (runtime_settings() or {}).get("unbound_tools")
        return value if isinstance(value, dict) else None

    def effective_classifier():
        if effective_classifier_provider is None:
            return None
        try:
            value = effective_classifier_provider()
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def apply_status(values: dict, resolved, gateway) -> dict:
        """Per group: saved / pending (when) / applied / failed, from runtime facts.

        ``gateway`` is the config the caller read for ``values``: one read per hub state
        (#513), so the values and their status never come from two reads of the file.
        """
        runtime = runtime_settings()
        selection = _current_selection(resolved)
        running = running_model()
        status = {}
        if running is None:
            status["model"] = {"status": "saved", "applies": "next_start"}
        elif running.get("apply_error"):
            status["model"] = {
                "status": "failed",
                "applies": "next_turn",
                "reason": running["apply_error"],
            }
        elif (running.get("provider"), running.get("model_id")) == (
            selection["provider"],
            selection["model_id"],
        ):
            status["model"] = {"status": "applied", "applies": "next_turn"}
        else:
            status["model"] = {"status": "pending", "applies": "next_turn"}
        effective = effective_classifier()
        expected = hub.expected_classifier_name(
            values["classifier"]["choice"], _sokudan_available()
        )
        if runtime is None:
            for group in hub.GROUPS:
                status[group] = {"status": "saved", "applies": "next_start"}
        else:
            error = runtime.get("error")
            choice = runtime.get("classifier_choice")
            status["classifier"] = {
                "status": "applied" if choice == values["classifier"]["choice"] else "pending",
                "applies": "restart",
            }
            # Write roots are fixed when the server builds its tools (#243); so are the
            # read roots (#457).
            for group in ("write", "read"):
                status[group] = {
                    "status": "applied" if runtime.get(group) == values[group] else "pending",
                    "applies": "restart",
                }
            for group in ("tools", "breaker"):
                if group == "breaker" and resolved.breaker_pairs_reset:
                    # #512: the saved set is unusable, so it never applies as it is.
                    status[group] = {
                        "status": "failed",
                        "applies": "next_turn",
                        "reason": "breaker_pairs_reset",
                    }
                elif error:
                    status[group] = {"status": "failed", "applies": "next_turn", "reason": error}
                elif runtime.get(group) == values[group]:
                    status[group] = {"status": "applied", "applies": "next_turn"}
                else:
                    status[group] = {"status": "pending", "applies": "next_turn"}
            # The details fold (#471) is read when the chat server starts: compare with the
            # running engine's values (``plain_values``), else it is only saved.
            # An engine that does not report every detail cannot be compared (#471 review
            # m-6): "saved", never a "pending" that no restart would clear.
            engine = runtime.get("engine")
            details = values["breaker_details"]
            if isinstance(engine, dict) and all(name in engine for name in details):
                same = all(engine[name] == value for name, value in details.items())  # 60 == 60.0
                status["breaker_details"] = {
                    "status": "applied" if same else "pending",
                    "applies": "restart",
                }
            else:
                status["breaker_details"] = {"status": "saved", "applies": "restart"}
        # The Stage reads ui.board_svg when its page loads (#453): saved = in effect on reload.
        status["board"] = {"status": "saved", "applies": "reload"}
        status.update(cost_status(gateway, resolved, runtime))
        status["heartbeat"] = heartbeat_apply_status(gateway, runtime)
        # ``delegate`` (#486): read from gateway-config at each turn and each call.
        status["delegation"] = {"status": "saved", "applies": "next_turn"}
        # ADR 0021 S2: the Agent-layer executor and its pool are built when the server starts.
        status["agent_jobs"] = {"status": "saved", "applies": "restart"}
        status["classifier"]["effective"] = (effective or {}).get("name")
        status["classifier"]["expected"] = expected
        return status

    def cost_status(gateway, resolved, runtime) -> dict:
        """#470: the output cap from the next turn; cache and context at the next start.

        Compared with what the running agent reports, after the environment overrides the
        server applies at start-up (a pinned switch is "applied" when the env value runs).
        """
        if runtime is None:
            return {
                group: {"status": "saved", "applies": "next_start"}
                for group in ("output", "cache", "context")
            }
        running_cap = runtime.get("max_output_tokens")
        if running_cap is None:
            status = {"output": {"status": "saved", "applies": "next_turn"}}
        else:
            status = {
                "output": {
                    "status": "applied" if running_cap == resolved.max_output_tokens else "pending",
                    "applies": "next_turn",
                    "effective": running_cap,
                }
            }
        enabled, ttl = hub.effective_prompt_cache(gateway)
        cache = runtime.get("prompt_cache") or {}
        # Only checked values are passed on (5m / 1h, booleans): the same the running agent
        # reports in /api/gateway/status, never a raw environment string (#470 review §2).
        running_cache = {
            "enabled": _bool_or_none(cache.get("enabled")),
            "ttl": cache.get("ttl") if cache.get("ttl") in hub.CACHE_TTLS else None,
        }
        status["cache"] = {
            "status": "applied"
            if (running_cache["enabled"], running_cache["ttl"]) == (enabled, ttl)
            else "pending",
            "applies": "restart",
            # What the running agent uses: a pinned switch shows this instead of the saved
            # value it does not use.
            "running": running_cache,
        }
        context = runtime.get("context_management") or {}
        wanted = {
            "enabled": hub.effective_context_enabled(gateway),
            **{k: v for k, v in hub.context_values(gateway).items() if k != "enabled"},
        }
        if wanted.get("context_window_tokens") is None:
            # Automatic: the runtime may resolve it from the model (not a pending change).
            wanted.pop("context_window_tokens", None)
        status["context"] = {
            "status": "applied"
            if all(context.get(key, _MISSING) == value for key, value in wanted.items())
            else "pending",
            "applies": "restart",
            "running": {"enabled": _bool_or_none(context.get("enabled"))},
        }
        return status

    def heartbeat_apply_status(gateway, runtime) -> dict:
        """#472: read once at start-up. Compared with what the daemon was started with
        (the switch after ``STIRA_HEARTBEAT``, and both bounds), so a pinned switch is
        "applied" when the env value runs."""
        if runtime is None:
            return {"status": "saved", "applies": "next_start"}
        running = runtime.get("heartbeat")
        if not isinstance(running, dict):
            return {"status": "saved", "applies": "restart"}
        try:
            # What the daemon would start with (not the shown bounds of a reset pair, #512).
            wanted = {
                "interval_floor_sec": float(gateway.hb_interval_floor_sec),
                "interval_ceil_sec": float(gateway.hb_interval_ceil_sec),
            }
            wanted["enabled"] = hub.effective_heartbeat_enabled(gateway)
        except Exception as error:  # noqa: BLE001 - one badge, never the whole hub (#497)
            current_app.logger.warning(
                "Heartbeat apply status unavailable (%s)", type(error).__name__
            )
            return {"status": "saved", "applies": "restart"}
        shown = {"enabled": _bool_or_none(running.get("enabled"))}
        error = running.get("error")
        if wanted["enabled"] and shown["enabled"] and isinstance(error, str) and error:
            # Turned on but not started (another process owns the state directory, ...).
            return {"status": "failed", "applies": "restart", "reason": error, "running": shown}
        same = shown["enabled"] == wanted["enabled"] and all(
            running.get(name) == wanted[name] for name in hub.HEARTBEAT_INTERVALS
        )
        return {
            "status": "applied" if same else "pending",
            "applies": "restart",
            "running": shown,
        }

    def heartbeat_live():
        if heartbeat_status_provider is None:
            return None
        try:
            return hub.heartbeat_live(heartbeat_status_provider())
        except Exception:  # noqa: BLE001 - a status read never breaks the page
            return None

    def _workspace_path(agent):
        runtime = runtime_settings()
        if runtime and runtime.get("workspace"):
            return runtime["workspace"]
        if agent.workspace_dir:
            return str(Path(agent.workspace_dir).expanduser().resolve())
        return None

    def load_hub():
        gateway, agent = config_store.load()
        resolved = resolve_config(gateway, agent)
        tool_config = config_store.load_tools()
        return gateway, agent, resolved, tool_config

    def config_location():
        if config_location_provider is None:
            return None
        try:
            value = config_location_provider()
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def surface_state(gateway, agent, tool_config) -> dict:
        """Per field: source, applies, the variables that win (#469); names only.

        Additive facts: whatever fails here (a broken manifest, an unreadable file, a path
        that does not resolve) gives the empty answer with ``available: false``, so the hub
        GET still answers 200 and the page can still save (#490 review).
        """
        try:
            return _surface_state(gateway, agent, tool_config)
        except Exception as error:  # noqa: BLE001 - never takes the hub down
            current_app.logger.warning("Settings surface unavailable (%s)", type(error).__name__)
            return {
                "available": False,
                "fields": {},
                "pinned": [],
                "restart_pending": [],
                "restart_pending_groups": [],
                "restart_known": False,
                "location": {},
            }

    def _surface_state(gateway, agent, tool_config) -> dict:
        fields = surface.describe(gateway, agent, os.environ)
        pending = surface.restart_pending(startup_saved, gateway, agent, fields)
        for item_id, entry in fields.items():
            entry["pending_restart"] = item_id in pending
        location = config_location() or {}
        paths = (config_store.gateway_path, config_store.agent_path, config_store.tool_path)
        return {
            "available": True,
            "fields": fields,
            "pinned": surface.pinned_variables(os.environ),
            "restart_pending": pending,
            # One per change: the classifier choice writes two fields, it is one change.
            "restart_pending_groups": surface.pending_groups(pending, fields),
            "restart_known": startup_saved is not None,
            "location": {
                # File names only, never a path (#291).
                "files": [Path(path).name for path in paths],
                "same_directory": len({Path(path).expanduser().resolve().parent for path in paths})
                == 1,
                "matches_data_dir": location.get("matches_data_dir"),
                "data_dir": location.get("data_dir"),
                "anchor_env": TOOL_ANCHOR_ENV,
                "anchor_configured": anchor_path(config_store.tool_path) is not None,
                "tools_locked": bool(tool_config.integrity_error),
            },
        }

    def _context_profile_facts(gateway):
        try:
            return hub.effective_context_profile(gateway)
        except (OSError, ValueError, TypeError):  # a read fact never breaks the page
            return None

    def _task_model_facts(gateway, resolved):
        from stiracore.agent import task_models

        try:
            return task_models.describe_routes(
                gateway.task_models, resolved.model_spec(), region=resolved.region
            )
        except (OSError, ValueError, TypeError):  # a read fact never breaks the page
            return None

    def hub_state(gateway, resolved, tool_config, agent) -> dict:
        values = hub.hub_values(gateway, resolved.stira_config, tool_config, agent)
        rows, level = _tool_rows(tool_config, breaker_level(), critical_action(), unbound_tools())
        sokudan_available = _sokudan_available()
        status = apply_status(values, resolved, gateway)
        running = running_model()
        return {
            "version": hub.version_of(values),
            "values": values,
            "defaults": hub.default_values(),
            # #453: the board section says when the Stage itself is off (STIRA_STAGE)
            "stage_enabled": stage_enabled(),
            "constraints": hub.constraints(resolved.stira_config),
            "dangers": hub.dangers(values),
            # Effective absolute path of "@workspace" (#243), when known.
            "workspace": _workspace_path(agent),
            "classifiers": [
                {"id": "auto", "available": True},
                {"id": "rule", "available": True},
                {"id": "sokudan", "available": sokudan_available},
            ],
            "tools": rows,
            "breaker_level": level,
            # Read-only: no setting or approval lifts these (#190, ADR 0010).
            "floor": tool_policy.floor_items(
                resolved.read_only_paths,
                data_directory=(runtime_settings() or {}).get("data_directory"),
            ),
            # Rules failed their integrity check: every tool Never until saved again.
            "tools_locked": tool_config.integrity_error,
            # Saved allowlist entries holding a secret (#270): marked for removal, shown
            # masked; the tool rules cannot be saved again until they are removed.
            "secret_allowlist": hub.secret_allowlist(values),
            "bc_window_clamped": resolved.bc_window_clamped,
            "protected_layers_clamped": resolved.protected_layers_clamped,
            "hitl_timeout_clamped": resolved.hitl_timeout_clamped,
            # #512: the file's heartbeat bounds were inverted; the defaults are shown (and
            # written by the next save of the gateway file).
            "hb_intervals_reset": resolved.hb_intervals_reset,
            # #512: the agent file's breaker set broke a pair rule; the file's values are
            # shown, the defaults run, and only a save of thresholds ends it.
            "breaker_pairs_reset": resolved.breaker_pairs_reset,
            # #471: the values shown but never edited here. Where each breaker value comes
            # from and when it applies: ``surface.fields["stira.<name>"]`` (#469).
            "breaker_fixed": hub.fixed_values(
                resolved.stira_config, (runtime_settings() or {}).get("engine")
            ),
            "policies": list(tool_policy.POLICIES),
            **_exec_status(),
            "apply": status,
            # Where each value comes from and when it applies (#469): additive.
            "surface": surface_state(gateway, agent, tool_config),
            # Server-defined bundles (#227); none uses trust_all or exec tools.
            "presets": [
                {
                    "id": preset_id,
                    "values": (preset := hub.preset_values(preset_id, values)),
                    "diff": hub.diff_values(values, preset),
                    "tools_enabled": len(preset["tools"]["enabled_tools"]),
                    "default": preset_id == hub.DEFAULT_PRESET,
                }
                for preset_id in hub.PRESET_IDS
            ],
            # #450: Always / Ask / Never tables by purpose; put in the draft, then saved
            # through the confirmed save like any other change.
            "tool_profiles": _tool_profiles(values["tools"], rows),
            # #470: what the model allows. Where each cost value comes from, when it applies
            # and which variable pins it: ``surface.fields["gateway.<name>"]`` (#469).
            "cost": {
                "model": hub.cost_model_info(resolved.provider, resolved.model_id),
                # #368, read-only: the profile the running server started with (null without
                # a chat server) and the one the next start would use.
                "context_profile": {
                    "running": (runtime_settings() or {}).get("context_profile"),
                    "next_start": _context_profile_facts(gateway),
                },
                # docs/task-models.md, read-only: the model each side task uses (the running
                # server's, null without one, and what the next start would use).
                "task_models": {
                    "running": (runtime_settings() or {}).get("task_models"),
                    "next_start": _task_model_facts(gateway, resolved),
                },
            },
            # #472: the running heartbeat (read-only; null without a chat server).
            "heartbeat": {"live": heartbeat_live()},
            "summary": {
                "model": _current_selection(resolved),
                "running_model": running,
                "classifier": {
                    "choice": values["classifier"]["choice"],
                    "effective": status["classifier"].get("effective"),
                    "expected": status["classifier"]["expected"],
                },
                "breaker": values["breaker"],
                "tools_effective": sum(1 for row in rows if row["enabled"]),
                "tools_total": len(rows),
                "tool_mode": tool_config.mode,
                "preset": hub.active_preset(values),
            },
        }

    def candidate_from_request(data, values, resolved):
        changes = data.get("changes", {})
        candidate = hub.merge_changes(values, changes)
        errors = hub.validate_values(
            candidate,
            resolved.stira_config,
            sokudan_available=_sokudan_available(),
            current=values,
        )
        if errors:
            raise hub.HubError(errors)
        return candidate

    @blueprint.get("/api/config/hub")
    def get_config_hub():
        with config_store.lock:
            try:
                gateway, agent, resolved, tool_config = load_hub()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            return jsonify(hub_state(gateway, resolved, tool_config, agent))

    @blueprint.post("/api/config/hub/validate")
    def post_config_hub_validate():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object.", errors=[]), 400
        with config_store.lock:
            try:
                gateway, agent, resolved, tool_config = load_hub()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            values = hub.hub_values(gateway, resolved.stira_config, tool_config, agent)
            try:
                candidate = candidate_from_request(data, values, resolved)
            except hub.HubError as error:
                return jsonify(ok=False, errors=error.errors)
            change = tool_change(tool_config, values, candidate)
            return jsonify(
                ok=True,
                errors=[],
                diff=hub.diff_values(values, candidate),
                needs_confirmation=hub.new_dangers(
                    values, candidate, loose=change["loose"], locked=change["locked"]
                ),
            )

    @blueprint.post("/api/config/hub")
    def post_config_hub():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object.", errors=[]), 400
        acknowledged = data.get("acknowledged_dangers", [])
        if not isinstance(acknowledged, list):
            return jsonify(error="acknowledged_dangers must be an array.", errors=[]), 400
        source = data.get("source", "manual")
        card = source == "approval_card" or "save_token" in data
        if card and not allow_runtime.enabled():
            # ADR 0025: without the allowance service (or with the constant off) a card-
            # origin save is refused; nothing is saved.
            return (
                jsonify(error="This option is not available.", code="not_enabled", errors=[]),
                409,
            )
        if card and (source != "approval_card" or "changes" in data):
            # The card's save names no change: the server derives the one change (this
            # card's tool Ask → Always) from its own record of the card.
            return jsonify(error="A card save names only its card.", code="card_invalid"), 400
        if allow_reserved_keys(data):
            # ADR 0025 §8: a session allowance is never a setting (any depth).
            return (
                jsonify(
                    error="session_allow is not a setting.",
                    code="allow_not_settable",
                    error_code="allow_not_settable",
                    errors=[],
                ),
                409,
            )
        if source not in _HUB_SOURCES:
            return jsonify(error="Unknown source.", allowed=sorted(_HUB_SOURCES), errors=[]), 400
        with config_store.lock:
            try:
                gateway, agent, resolved, tool_config = load_hub()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            values = hub.hub_values(gateway, resolved.stira_config, tool_config, agent)
            card_guard = None
            if card:
                card_save = make_always_change(data, values, tool_config)
                if isinstance(card_save, tuple):
                    return card_save
                data = {**data, "changes": card_save["changes"]}
                card_guard = card_save["guard"]
            base_version = data.get("base_version")
            if base_version is not None and base_version != hub.version_of(values):
                # Someone saved in between: never overwrite silently (#195).
                return (
                    jsonify(
                        error="Settings changed since this page loaded.",
                        error_code="conflict",
                        state=hub_state(gateway, resolved, tool_config, agent),
                    ),
                    409,
                )
            try:
                candidate = candidate_from_request(data, values, resolved)
            except hub.HubError as error:
                return jsonify(error="Invalid settings.", errors=error.errors), 400
            # One loosening decision (review round 5): confirmation, breaker and audit
            # are all derived from it.
            change = tool_change(tool_config, values, candidate)
            if change["locked"] and data.get("recover") != RECOVER_WORD:
                return jsonify(_locked_body(tool_config.integrity_error)), 409
            refusal = breaker_refusal(change["loose"])
            if refusal is not None:
                return refusal
            missing = [
                d
                for d in hub.new_dangers(
                    values, candidate, loose=change["loose"], locked=change["locked"]
                )
                if d not in acknowledged
            ]
            if missing:
                return (
                    jsonify(
                        error="Confirmation required.",
                        error_code="needs_confirmation",
                        needs_confirmation=missing,
                        errors=[],
                    ),
                    400,
                )
            always_writes = change["always_writes"]
            if not hub.typed_names_match(data.get("confirm_always", []), always_writes):
                return jsonify(_names_refusal_body(always_writes)), 400
            running = loosening_while_tool_runs(values, candidate, change, data)
            if running is not None:
                return running
            if card_guard is not None:
                # §9: the one-time save token, checked and spent now (single use), under
                # the store lock and before the audit row and the file; the fence is
                # compared again in the save's precommit.
                refused = card_guard["spend"]()
                if refused is not None:
                    return refused
            return persist(
                gateway,
                agent,
                tool_config,
                values,
                candidate,
                source,
                recover=data.get("recover") == RECOVER_WORD,
                change=change,
                guard=card_guard["precommit"] if card_guard is not None else None,
            )

    def make_always_change(data, values, tool_config):
        """ADR 0025 §9: the card's one change (its tool Ask → Always) and the token check,
        or a refusal response. The tool comes from the server's record of the card."""
        from stiracore.yolo.session_allow import table

        service = allow_runtime.service()
        request_id = data.get("request_id")
        token = data.get("save_token")
        tool = service.card_tool(request_id) if service is not None else None
        if tool is None or not isinstance(token, str):
            return _card_refusal("stale_confirmation")
        if service.card_terminal(request_id) is None:
            return _card_refusal("call_not_finished")
        tools = values["tools"]
        if tool not in tools["enabled_tools"] or tool not in tools["ask_tools"]:
            return _card_refusal("stale_confirmation")  # no longer Ask: nothing to make Always
        changes = {"tools": {"ask_tools": [t for t in tools["ask_tools"] if t != tool]}}
        version = hub.version_of(values)
        generation = tool_config.generation
        spent = {}

        def spend():
            code = table().spend_save_token(
                token,
                request_id=request_id,
                tool=tool,
                hub_version=version,
                rule_generation=generation,
            )
            if code is not None:
                return _card_refusal(code)
            spent["fence"] = table().snapshot().fence
            return None

        def precommit():
            if table().snapshot().fence != spent.get("fence"):
                raise _CardStale("stale_confirmation")

        return {"changes": changes, "guard": {"spend": spend, "precommit": precommit}}

    @blueprint.get("/api/config/hub/card/<request_id>")
    def get_config_hub_card(request_id):
        """ADR 0025 §9: the Make Always dialog's view of one card — ``waiting`` until its
        call has a terminal result, then that result, the one change, the names to type,
        the dangers to acknowledge and a one-time save token."""
        service = allow_runtime.service()
        tool = service.card_tool(request_id) if service is not None else None
        if not allow_runtime.enabled() or tool is None:
            return jsonify(state="gone", code="stale_confirmation"), 404
        terminal = service.card_terminal(request_id)
        if terminal is None:
            return jsonify(state="waiting", tool=tool, code="call_not_finished"), 200
        with config_store.lock:
            try:
                gateway, agent, resolved, tool_config = load_hub()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            values = hub.hub_values(gateway, resolved.stira_config, tool_config, agent)
            tools = values["tools"]
            if tool not in tools["enabled_tools"] or tool not in tools["ask_tools"]:
                return jsonify(state="not_ask", tool=tool, terminal=terminal), 200
            changes = {"tools": {"ask_tools": [t for t in tools["ask_tools"] if t != tool]}}
            candidate = hub.merge_changes(values, changes)
            change = tool_change(tool_config, values, candidate)
            token, code = service.save_token(
                request_id=request_id,
                hub_version=hub.version_of(values),
                rule_generation=tool_config.generation,
                tool=tool,
            )
            if token is None:
                return jsonify(state="gone", code=code), 409
            return jsonify(
                state="ready",
                tool=tool,
                terminal=terminal,
                token=token,
                base_version=hub.version_of(values),
                confirm_names=change["always_writes"],
                needs_confirmation=hub.new_dangers(
                    values, candidate, loose=change["loose"], locked=change["locked"]
                ),
                breaker_ok=breaker_refusal(change["loose"]) is None,
            )

    def tool_change(tool_config, values, candidate) -> dict:
        """The single loosening decision for a hub save or rollback (review round 5).

        ``locked``: the candidate saves tool settings over locked rules — a recovery,
        always a loosening whatever the values. ``loose``: ``rules_loosening`` reasons;
        ``always_writes``: the names to type (``hub.tool_rule_change``, #270).
        """
        locked = bool(tool_config.integrity_error) and candidate["tools"] != values["tools"]
        return hub.tool_rule_change(values, candidate, locked=locked)

    def loosening_while_tool_runs(values, candidate, change, data, source="hub"):
        """Only with STIRA_YOLO_ALLOW (ADR 0012 §8b): a loosening hub save, rollback or
        ``RECOVER`` is refused while an agent tool call runs (one layer of defence), and
        applies only after a person confirmed exactly this save on the host (phase 2)."""
        dangers = hub.new_dangers(values, candidate, loose=change["loose"], locked=change["locked"])
        recover = data.get("recover") == RECOVER_WORD
        loosening = bool(change["loose"]) or change["locked"] or recover or bool(dangers)
        if not loosening:
            return None
        running = yolo_runtime.tool_call_refusal()
        if running is not None:
            return jsonify({**running, "errors": []}), 409
        return host_confirmation(
            recover, data, {"values": values}, source, [*change["loose"], *dangers]
        )

    def host_confirmation(recover, data, target, source, reasons):
        """ADR 0012 §8b: ``None`` without the allowance or for a confirmed, matching
        operation (consumed now); otherwise the 428 / refusal response."""
        gate = yolo_runtime.host_gate(
            "recover" if recover else "rules_loosen",
            route=request.path,
            body=data,
            target=target,
            summary={"source": source, "changes": yolo_runtime.change_labels(reasons)},
            header=request.headers.get(yolo_runtime.HEADER),
        )
        return None if gate is None else (jsonify(gate[0]), gate[1])

    def breaker_refusal(loose):
        """No tool rule is loosened while the breaker is active, confirmed or not (#190)."""
        level = level_name(breaker_level())
        if not loose or level in tool_policy.LOOSENING_LEVELS:
            return None
        return (
            jsonify(
                error="Tool rules cannot be loosened while the breaker is active.",
                error_code="breaker_active",
                breaker_level=level,
                loosened=loose,
                errors=[],
            ),
            403,
        )

    def persist(
        gateway,
        agent,
        tool_config,
        values,
        candidate,
        source,
        recover=False,
        change=None,
        guard=None,
    ):
        """Save the groups that changed, record history, return the new hub state.

        Loosening a tool rule is audited before anything is saved, and the breaker is
        re-checked right before the tool rules are committed (#190, ADR 0010 §4); the
        tool rules are saved first so a refusal leaves every group unchanged.
        """
        diff = hub.diff_values(values, candidate)
        saved = []
        change = change or tool_change(tool_config, values, candidate)
        if change["locked"]:
            entry = hub.recovery_entry(values, candidate, source, tool_config.integrity_error)
        else:
            entry = hub.history_entry(values, candidate, source)
        # A widened read root (#457) or write root (#536) is a loosening too: recorded
        # before the save.
        loosening = (
            bool(change["loose"])
            or bool(hub.read_roots_widened(values, candidate))
            or bool(hub.write_roots_widened(values, candidate))
        )

        def commit(precommit):
            nonlocal gateway, agent
            if candidate["tools"] != values["tools"]:
                ToolConfig(
                    mode=candidate["tools"]["mode"],
                    enabled_tools=set(candidate["tools"]["enabled_tools"]),
                    shell_allowlist=list(candidate["tools"]["shell_allowlist"]),
                    ask_tools=tool_policy.required_ask(
                        candidate["tools"]["enabled_tools"],
                        candidate["tools"].get("ask_tools") or (),
                        candidate["tools"]["mode"],
                    ),
                    path=tool_config.path,
                ).save(precommit=precommit, recover=recover)
                saved.append("tools")
            if candidate["classifier"] != values["classifier"]:
                gateway = _clone_gateway_config(
                    gateway,
                    **hub.gateway_overrides_for_choice(candidate["classifier"]["choice"], gateway),
                )
                gateway.save()
                saved.append("classifier")
            breaker_changes = {
                name: value
                for group in ("breaker", "breaker_details")
                for name, value in candidate[group].items()
                if value != values[group].get(name)
            }
            if breaker_changes:
                # One write for the whole set (#471): entries, exits and details together,
                # only the fields that changed (the rest keep their saved or default source).
                overrides = dict(agent.stira_config_overrides or {})
                reset_file = agent.breaker_pairs_file if agent.breaker_pairs_reset else None
                if reset_file is not None and set(breaker_changes) & set(hub.BREAKER_SET):
                    # #512: thresholds saved from a reset set: the file's values the page
                    # showed plus the moved ones, one set (validated like any save).
                    overrides.update(reset_file)
                    reset_file = None
                overrides.update(breaker_changes)
                agent = _clone_agent_config(agent, stira_config_overrides=overrides)
                if reset_file is not None:
                    # Only the details moved: the file's thresholds stay as written.
                    agent.keep_reset_breaker_pairs(reset_file)
                agent.save()
                saved.extend(
                    group
                    for group in ("breaker", "breaker_details")
                    if candidate[group] != values[group]
                )
            if candidate["write"] != values["write"]:
                # Write roots (#243): applied when the chat server next starts.
                agent = _clone_agent_config(agent, write_roots=list(candidate["write"]["roots"]))
                agent.save()
                saved.append("write")
            if candidate["read"] != values["read"]:
                # Read roots (#457): applied when the chat server next starts.
                agent = _clone_agent_config(agent, read_roots=list(candidate["read"]["roots"]))
                agent.save()
                saved.append("read")
            if candidate["board"] != values["board"]:
                # Static SVG on the Stage board (#453): read on the next Stage page load.
                gateway = _clone_gateway_config(
                    gateway, ui=hub.gateway_ui_for_board(candidate["board"], gateway)
                )
                gateway.save()
                saved.append("board")
            cost = [g for g in ("output", "cache", "context") if candidate[g] != values[g]]
            if cost:
                # #470: output cap (next turn), prompt cache and context management (start-up).
                gateway = _clone_gateway_config(
                    gateway, **hub.gateway_overrides_for_cost(candidate)
                )
                gateway.save()
                saved.extend(cost)
            if candidate["heartbeat"] != values["heartbeat"]:
                # #472: hb_enabled and the interval bounds, read when the chat server starts.
                overrides = hub.gateway_overrides_for_heartbeat(candidate["heartbeat"])
                if gateway.hb_intervals_reset and all(
                    candidate["heartbeat"][name] == values["heartbeat"][name]
                    for name in hub.HEARTBEAT_INTERVALS
                ):
                    # #512: the switch alone; the file's (reset) bounds are kept.
                    overrides = {"hb_enabled": overrides["hb_enabled"]}
                gateway = _clone_gateway_config(gateway, **overrides)
                gateway.save()
                saved.append("heartbeat")
            if candidate["delegation"] != values["delegation"]:
                # ``delegate`` (#486, ADR 0016): read at the next turn and the next call.
                gateway = _clone_gateway_config(
                    gateway, delegation=hub.gateway_delegation(candidate["delegation"])
                )
                gateway.save()
                saved.append("delegation")
            if candidate["agent_jobs"] != values["agent_jobs"]:
                # ADR 0021 S2: read when the chat server starts (the scheduler is built then).
                gateway = _clone_gateway_config(
                    gateway, agent_jobs=hub.gateway_agent_jobs(candidate["agent_jobs"])
                )
                gateway.save()
                saved.append("agent_jobs")

        def guarded(precommit):
            if guard is not None:
                # ADR 0025 §9: a stop / change since the card's save token was spent (the
                # fence moved) refuses, right before the first file is written.
                guard()
            return commit(precommit)

        def respond(_outcome, recorded):
            state_gateway, state_agent, resolved, state_tools = load_hub()
            return jsonify(
                saved=[group for group in hub.GROUPS if group in saved],
                diff=diff,
                history_id=recorded["id"] if recorded else None,
                state=hub_state(state_gateway, resolved, state_tools, state_agent),
            )

        try:
            response = commit_audited(entry, loosening, guarded, respond)
        except ToolRulesLocked as error:
            return jsonify(_locked_body(error)), 409
        except OSError:
            response = (
                jsonify(
                    error="Could not persist settings.",
                    error_code="persist_failed",
                    saved=[group for group in hub.GROUPS if group in saved],
                    errors=[],
                ),
                500,
            )
        except (TypeError, ValueError) as error:
            response = (
                jsonify(
                    error=str(error),
                    saved=[group for group in hub.GROUPS if group in saved],
                    errors=[],
                ),
                400,
            )
        if "tools" in saved:
            # The tool rules were committed (even if a later group failed): void the
            # open approvals now; a failure is the response (fail-closed, #330 review).
            failure = tool_rules_saved([group for group in hub.GROUPS if group in saved])
            if failure is not None:
                return failure
        return response

    @blueprint.get("/api/config/history")
    def get_config_history():
        limit = request.args.get("limit", default=20, type=int) or 20
        try:
            entries = hub.read_history(config_store.history_path, max(1, min(limit, 100)))
        except OSError as error:
            return jsonify(error=f"History unavailable: {error}"), 503
        return jsonify(entries=entries)

    @blueprint.post("/api/config/history/<entry_id>/rollback")
    def post_config_history_rollback(entry_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object.", errors=[]), 400
        acknowledged = data.get("acknowledged_dangers", [])
        if not isinstance(acknowledged, list):
            return jsonify(error="acknowledged_dangers must be an array.", errors=[]), 400
        with config_store.lock:
            try:
                entries = hub.read_history(config_store.history_path, hub.HISTORY_READ_LIMIT)
                gateway, agent, resolved, tool_config = load_hub()
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Config unavailable: {error}"), 503
            entry = next((e for e in entries if e.get("id") == entry_id), None)
            if entry is None:
                return jsonify(error="Unknown history entry.", errors=[]), 404
            values = hub.hub_values(gateway, resolved.stira_config, tool_config, agent)
            base_version = data.get("base_version")
            if base_version is not None and base_version != hub.version_of(values):
                return (
                    jsonify(
                        error="Settings changed since this page loaded.",
                        error_code="conflict",
                        state=hub_state(gateway, resolved, tool_config, agent),
                    ),
                    409,
                )
            try:
                candidate = hub.rollback_candidate(entry, values)
                errors = hub.validate_values(
                    candidate,
                    resolved.stira_config,
                    sokudan_available=_sokudan_available(),
                    current=values,
                )
                if errors:
                    raise hub.HubError(errors)
            except hub.HubError as error:
                return jsonify(error="Cannot roll back.", errors=error.errors), 400
            # One loosening decision (review round 5): confirmation, breaker and audit
            # are all derived from it.
            change = tool_change(tool_config, values, candidate)
            if change["locked"] and data.get("recover") != RECOVER_WORD:
                return jsonify(_locked_body(tool_config.integrity_error)), 409
            refusal = breaker_refusal(change["loose"])
            if refusal is not None:
                return refusal
            missing = [
                d
                for d in hub.new_dangers(
                    values, candidate, loose=change["loose"], locked=change["locked"]
                )
                if d not in acknowledged
            ]
            if missing:
                return (
                    jsonify(
                        error="Confirmation required.",
                        error_code="needs_confirmation",
                        needs_confirmation=missing,
                        errors=[],
                    ),
                    400,
                )
            always_writes = change["always_writes"]
            if not hub.typed_names_match(data.get("confirm_always", []), always_writes):
                return jsonify(_names_refusal_body(always_writes)), 400
            running = loosening_while_tool_runs(values, candidate, change, data, "rollback")
            if running is not None:
                return running
            return persist(
                gateway,
                agent,
                tool_config,
                values,
                candidate,
                f"rollback:{entry_id}",
                recover=data.get("recover") == RECOVER_WORD,
                change=change,
            )

    # ADR 0015 Addendum rev 4 (#449): page_check's localhost ports, a person-only route.
    from stiracore.observability import page_check_ports_api

    page_check_ports_api.register(
        blueprint,
        config_store,
        breaker_level=breaker_level,
        host_confirmation=host_confirmation,
        commit_audited=commit_audited,
        tool_rules_saved=tool_rules_saved,
        locked_body=_locked_body,
    )

    return blueprint


__all__ = ["DEFAULT_SOUL_PATH", "ConfigStore", "create_config_blueprint"]
