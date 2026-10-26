"""Validated JSON persistence for gateway-wide and per-agent configuration."""

# Configuration validation intentionally follows ToolConfig's ValueError contract.
# ruff: noqa: TRY004

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from stiracore.config.stira_delivery import (
    HITL_TIMEOUT_MAX_SEC,
    NEXT_TURN_FIELDS,
    bounded_hitl_timeout,
    check_protected_layers,
    completed_protected_layers,
)
from stiracore.core.coherence import BC_MIN_SAMPLES
from stiracore.core.engine import StiraConfig
from stiracore.identity_paths import DEFAULT_READ_ONLY_PATHS, OUTSIDE_ACTIONS, WORKSPACE_ROOT
from stiracore.providers.credentials import validate_base_url, validate_env_name
from stiracore.providers.spec import PROVIDERS, normalize_provider
from stiracore.tools.tool_config import DEFAULT_PATH as DEFAULT_TOOL_CONFIG_PATH
from stiracore.tools.tool_config import ToolConfig

DEFAULT_GATEWAY_CONFIG_PATH = Path(".local/stira/gateway-config.json")
DEFAULT_AGENT_CONFIG_PATH = Path(".local/stira/agent-config.json")

logger = logging.getLogger(__name__)
_BC_CLAMP_WARNED = False  # the rounding warning is logged once per process (#337)
_HB_RESET_WARNED = False  # the inverted heartbeat bounds warning, once per process (#512)
_PL_CLAMP_WARNED = False  # the layer-completion warning, once per process (#272)
_HT_CLAMP_WARNED = False  # the approval-timeout bound warning, once per process (#272)
_BP_RESET_WARNED = False  # the inverted breaker pairs warning, once per process (#512)

HARVEST_JOB_LIMIT_MAX = 100_000  # harvest_job_max_episodes (#251)
_AUTONOMY_ACTIONS = ("pr_create", "merge_main", "destructive", "external_send")
_STIRA_OVERRIDE_FIELDS = {item.name for item in fields(StiraConfig)} - {"identity_anchors"}
_TOOL_CONFIG_KEYS = {"mode", "enabled_tools", "shell_allowlist", "ask_tools"}


def _require_nonempty_string(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _require_optional_string(value: Any, name: str) -> None:
    if value is not None:
        _require_nonempty_string(value, name)


def _require_finite_nonnegative(value: Any, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0.0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


def _require_json_value(value: Any, name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only JSON-compatible values") from exc


def _validate_provider(value: Any, name: str) -> str:
    """Canonical provider name; the message never echoes the (possibly pasted) input."""
    try:
        return normalize_provider(value)
    except ValueError:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(PROVIDERS))}") from None


def _validate_optional(value: Any, validator, name: str) -> Any:
    return None if value is None else validator(value, name)


def _atomic_save(path: Path, data: dict[str, Any], prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_object(path: Path, allowed_keys: set[str], label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - allowed_keys:
        raise ValueError(f"Invalid {label} configuration")
    return data


UI_HOMES = ("chat", "stage")


def _validate_ui(value: Any) -> dict[str, Any]:
    """``ui`` object of gateway-config.json: ``home`` ("chat" | "stage", #350) and
    ``board_svg`` (bool, #453: static SVG pictures on the Stage board; default **true**
    since the ADR 0012 addendum of 2026-10-24 — shown by default, rebuilt from an allow-list).

    ``board_svg`` is kept only when false (a person turned it off), so a file without it
    stays as it was and follows the default (:func:`ui_board_svg`)."""
    if not isinstance(value, dict) or set(value) - {"home", "board_svg"}:
        raise ValueError("ui must be an object with only the keys 'home' and 'board_svg'")
    home = value.get("home", "chat")
    if home not in UI_HOMES:
        raise ValueError("ui.home must be 'chat' or 'stage'")
    board_svg = value.get("board_svg", BOARD_SVG_DEFAULT)
    if type(board_svg) is not bool:
        raise ValueError("ui.board_svg must be a boolean")
    return {"home": home, **({} if board_svg else {"board_svg": False})}


# #453 / ADR 0012 addendum 2026-10-24: static SVG on the Stage board is shown by default.
BOARD_SVG_DEFAULT = True


def ui_board_svg(ui: Any) -> bool:
    """Whether the Stage board draws static SVG: the default unless ``board_svg: false``."""
    return not (isinstance(ui, dict) and ui.get("board_svg") is False)


@dataclass
class GatewayConfig:
    """Configuration shared by every agent behind one gateway instance."""

    model_id: str = "global.anthropic.claude-sonnet-5"
    # Per-call output cap. None (the default) = automatic: the model's documented output
    # limit bounded at 32000, or the old 8192 when the limit is unknown (#363). A number
    # is used as given.
    max_output_tokens: int | None = None
    region: str = "ap-northeast-1"
    # Model provider (#204/#205). Bedrock keeps using the AWS credential chain;
    # other providers read their key from the environment variable NAMED here.
    # Secret values are never stored in this file.
    provider: str = "bedrock"
    base_url: str | None = None
    api_key_env: str | None = None
    organization_env: str | None = None
    port: int = 5555
    state_base_path: str = ".local/stira/state"
    # Heartbeat pacing bounds. The effective interval scales with stability
    # (e.g. interval = clamp(ceil * stability**2, floor, ceil)): floor is the
    # shortest gap when unstable, ceil the longest gap when calm.
    hb_interval_floor_sec: float = 300.0
    hb_interval_ceil_sec: float = 10800.0
    # Opt-in background heartbeat tick (#238): zero-model decay/snapshot/breaker
    # check only. Off by default; STIRA_HEARTBEAT=1/0 overrides it at start-up.
    hb_enabled: bool = False
    # Opt-in prompt caching (#254): Bedrock Claude only (capabilities.prompt_cache).
    # STIRA_PROMPT_CACHE=1/0 and STIRA_PROMPT_CACHE_TTL override these at start-up.
    prompt_cache: bool = False
    prompt_cache_ttl: str = "5m"
    # Opt-in context management (#258): offload large tool results to a durable,
    # masked stash and summarize at a utilization threshold. Options override the
    # thresholds (ContextManagementSettings field names). STIRA_CONTEXT_MANAGEMENT=1/0
    # overrides the switch at start-up.
    context_management: bool = False
    context_management_options: dict[str, Any] = field(default_factory=dict)
    # Memory Map (wave 23): layer totals and the most important entries, framed in the
    # recalled part of the prompt (tools/memory_index). On by default; an empty store adds
    # nothing. STIRA_MEMORY_MAP=0/1 overrides the switch at start-up. ``memory_map_max_tokens``
    # bounds the section (100-1000).
    memory_map: bool = True
    memory_map_max_tokens: int = 300
    # Small-window profile (#368): "default" (unchanged prompt and tools), "small" or "tiny"
    # (stiracore.agent.context_profile.PROFILES). STIRA_CONTEXT_PROFILE overrides it at
    # start-up. Written only when not "default", so files without it stay as they were.
    context_profile: str = "default"
    # #368: send only the tool definitions the live rules allow (Always / Ask). Off by
    # default; "small" / "tiny" turn it on. Written only when on.
    tools_send_allowed_only: bool = False
    # #368: send shorter property descriptions in the long upstream tool schemas
    # (tools/compact_specs.py; names, types and required unchanged). Off by default;
    # "small" / "tiny" turn it on. Written only when on.
    tools_short_schemas: bool = False
    jev_enabled: bool = False
    jev_classifier: str = "rule"
    notify_targets: list[str] = field(default_factory=list)
    # Dashboard options (#350). ``home``: the page ``/`` opens, "chat" (default) or "stage"
    # (only when the Stage is enabled with STIRA_STAGE=1; otherwise ``/`` stays the chat).
    # ``board_svg`` (#453, opt-in, default off): ```svg blocks become pictures on the board.
    ui: dict[str, Any] = field(default_factory=lambda: {"home": "chat"})
    # YOLO values (ADR 0012 phase 7c), kept as written: ``stiracore.yolo.config_file``
    # reads them (an environment variable wins per value). Written only by a person's
    # confirmed save (``POST /api/yolo/settings``); every other save carries it over.
    # Not an object: treated as absent (the server reads ``{}``; a warning at start-up).
    yolo: dict[str, Any] = field(default_factory=dict)
    # Progress reports (ADR 0014 §10), kept as written: ``stiracore.progress.settings``
    # reads them leniently (an environment variable wins per value). Off by default.
    progress: dict[str, Any] = field(default_factory=dict)
    # ``delegate`` (#486, ADR 0016 phase 1): ``enabled`` (default on) and the per-child caps
    # (``stiracore.tools.delegate_tool.DelegationSettings``). Empty = the defaults; the
    # object is written only when it holds a value, so files without it stay as they were.
    delegation: dict[str, Any] = field(default_factory=dict)
    # ADR 0021 S2 (#239): the gated Agent-layer jobs, ``enabled`` (default **off**) and the
    # pool ``agent_daily_token_budget`` (``stiracore.config.agent_jobs``). Read at start.
    agent_jobs: dict[str, Any] = field(default_factory=dict)
    # ``run_checks`` (ADR 0019 phases 0-1, #167): absent or {} = off (no tool registered);
    # ``{"phase": 1}`` turns it on at the next start. Kept as written and read leniently by
    # ``stiracore.tools.run_checks.settings_from`` (anything else is off).
    run_checks: dict[str, Any] = field(default_factory=dict)
    # ADR 0011 W4 (#296): ``{"stage_c": true}`` turns the grant control plane on at the next
    # start (default off: no route, no file, no thread); ``run_checks_unattended`` (default
    # true) only counts on a verified os_sandbox host. Kept as written and read leniently by
    # ``stiracore.grants.service.settings_from``; written only when present.
    autonomy: dict[str, Any] = field(default_factory=dict)
    # Token unit prices per model id (#451), USD per million tokens, kept as written and read
    # leniently by ``stiracore.providers.token_prices`` at start-up. Empty (the default):
    # no price, so no cost is shown. Stira ships no price data.
    token_prices: dict[str, Any] = field(default_factory=dict)
    # Handoff notes (wave 23, ``docs/handoff.md``): the handoff_write / handoff_read tools and
    # the newest note (at most ``handoff_max_tokens``, 100-1500, and ``handoff_max_age_hours``
    # old, 1-720) in the recalled part. No note = no section. STIRA_HANDOFF_INJECT=0 turns it
    # off at start-up. Written to the file only when it differs from these defaults.
    handoff_inject: bool = True
    handoff_max_tokens: int = 400
    handoff_max_age_hours: int = 72
    # Per-task model routing (``docs/task-models.md``): ``{"<task>": "<model>"}``, tasks in
    # ``stiracore.agent.task_models.TASKS``. Empty (the default) = every task on the chat
    # model. STIRA_TASK_MODEL_<TASK> wins per task. Written only when it holds a value.
    task_models: dict[str, Any] = field(default_factory=dict)
    path: Path = field(default=DEFAULT_GATEWAY_CONFIG_PATH, repr=False)
    lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)
    # The file's heartbeat bounds had floor > ceil, so ``load`` used the defaults for both
    # (#512) and the heartbeat refuses to start. ``hb_intervals_file``: the interval keys as
    # the file holds them; ``save`` writes those back, so only a save of new bounds (a
    # config without the flag) ends the reset (review r568 M-2).
    hb_intervals_reset: bool = field(default=False, init=False, repr=False, compare=False)
    hb_intervals_file: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _require_nonempty_string(self.model_id, "model_id")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer or null")
        _require_nonempty_string(self.region, "region")
        self.provider = _validate_provider(self.provider, "provider")
        self.base_url = _validate_optional(self.base_url, validate_base_url, "base_url")
        self.api_key_env = _validate_optional(self.api_key_env, validate_env_name, "api_key_env")
        self.organization_env = _validate_optional(
            self.organization_env, validate_env_name, "organization_env"
        )
        _require_nonempty_string(self.state_base_path, "state_base_path")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer in [1, 65535]")
        _require_finite_nonnegative(self.hb_interval_floor_sec, "hb_interval_floor_sec")
        _require_finite_nonnegative(self.hb_interval_ceil_sec, "hb_interval_ceil_sec")
        if type(self.hb_enabled) is not bool:
            raise ValueError("hb_enabled must be a boolean")
        if type(self.prompt_cache) is not bool:
            raise ValueError("prompt_cache must be a boolean")
        if self.prompt_cache_ttl not in ("5m", "1h"):
            raise ValueError("prompt_cache_ttl must be '5m' or '1h'")
        if type(self.context_management) is not bool:
            raise ValueError("context_management must be a boolean")
        if not isinstance(self.context_management_options, dict):
            raise ValueError("context_management_options must be an object")
        from stiracore.agent.context_management import ContextManagementSettings

        ContextManagementSettings.from_options(False, self.context_management_options)
        self.context_management_options = dict(self.context_management_options)
        if type(self.memory_map) is not bool:
            raise ValueError("memory_map must be a boolean")
        if type(self.memory_map_max_tokens) is not int or not (
            100 <= self.memory_map_max_tokens <= 1000
        ):
            raise ValueError("memory_map_max_tokens must be an integer in [100, 1000]")
        from stiracore.agent.context_profile import get_profile

        get_profile(self.context_profile)  # ValueError for an unknown name
        if type(self.tools_send_allowed_only) is not bool:
            raise ValueError("tools_send_allowed_only must be a boolean")
        if type(self.tools_short_schemas) is not bool:
            raise ValueError("tools_short_schemas must be a boolean")
        if type(self.jev_enabled) is not bool:
            raise ValueError("jev_enabled must be a boolean")
        if self.jev_classifier not in ("rule", "sokudan"):
            raise ValueError("jev_classifier must be 'rule' or 'sokudan'")
        if not isinstance(self.notify_targets, list) or any(
            not isinstance(target, str) or not target.strip() for target in self.notify_targets
        ):
            raise ValueError("notify_targets must be an array of nonempty strings")
        self.hb_interval_floor_sec = float(self.hb_interval_floor_sec)
        self.hb_interval_ceil_sec = float(self.hb_interval_ceil_sec)
        if self.hb_interval_floor_sec > self.hb_interval_ceil_sec:
            raise ValueError("hb_interval_floor_sec must be <= hb_interval_ceil_sec")
        self.notify_targets = list(self.notify_targets)
        self.ui = _validate_ui(self.ui)
        # Never refuses (a hand-edited section must not stop the server): read leniently by
        # stiracore.yolo.config_file.clean; a non-object is kept so that it is reported.
        self.yolo = copy.deepcopy(self.yolo) if isinstance(self.yolo, dict) else self.yolo
        self.progress = (
            copy.deepcopy(self.progress) if isinstance(self.progress, dict) else self.progress
        )
        from stiracore.tools.delegate_tool import DelegationSettings

        DelegationSettings.from_options(self.delegation)  # ValueError for a bad section
        self.delegation = dict(self.delegation)
        from stiracore.config.agent_jobs import AgentJobSettings

        AgentJobSettings.from_options(self.agent_jobs)  # ValueError for a bad section
        self.agent_jobs = dict(self.agent_jobs)
        self.run_checks = (
            copy.deepcopy(self.run_checks) if isinstance(self.run_checks, dict) else self.run_checks
        )
        self.autonomy = (
            copy.deepcopy(self.autonomy) if isinstance(self.autonomy, dict) else self.autonomy
        )
        self.token_prices = (
            copy.deepcopy(self.token_prices)
            if isinstance(self.token_prices, dict)
            else self.token_prices
        )
        from stiracore.tools.handoff_tools import check_settings as check_handoff

        check_handoff(self.handoff_inject, self.handoff_max_tokens, self.handoff_max_age_hours)
        from stiracore.agent.task_models import check_config as check_task_models

        self.task_models = check_task_models(self.task_models)  # ValueError: unknown task
        self.path = Path(self.path)

    @classmethod
    def load(cls, path: Path = DEFAULT_GATEWAY_CONFIG_PATH) -> GatewayConfig:
        path = Path(path)
        allowed = {
            "model_id",
            "max_output_tokens",
            "region",
            "provider",
            "base_url",
            "api_key_env",
            "organization_env",
            "port",
            "state_base_path",
            "hb_interval_floor_sec",
            "hb_interval_ceil_sec",
            "hb_enabled",
            "prompt_cache",
            "prompt_cache_ttl",
            "context_management",
            "context_management_options",
            "memory_map",
            "memory_map_max_tokens",
            "context_profile",
            "tools_send_allowed_only",
            "tools_short_schemas",
            "jev_enabled",
            "jev_classifier",
            "notify_targets",
            "ui",
            "yolo",
            "progress",
            "delegation",
            "agent_jobs",
            "run_checks",
            "autonomy",
            "token_prices",
            "handoff_inject",
            "handoff_max_tokens",
            "handoff_max_age_hours",
            "task_models",
        }
        data = _load_object(path, allowed, "gateway")
        if data is None:
            return cls(path=path)
        data, kept = _reset_inverted_hb_intervals(data)
        config = cls(**data, path=path)
        if kept is not None:
            config.keep_reset_intervals(kept)
        return config

    def keep_reset_intervals(self, kept: dict[str, Any]) -> None:
        """Mark the bounds as reset on load (#512); ``kept``: the file's interval keys."""
        self.hb_intervals_reset = True
        self.hb_intervals_file = dict(kept)

    def shown_intervals(self) -> tuple[float, float]:
        """The bounds the settings page shows: the file's (an omitted one at its default)
        while reset, else the values in use."""
        if not self.hb_intervals_reset:
            return float(self.hb_interval_floor_sec), float(self.hb_interval_ceil_sec)
        defaults = GatewayConfig.__dataclass_fields__
        floor, ceil = (
            float(self.hb_intervals_file.get(name, defaults[name].default))
            for name in _HB_INTERVAL_FIELDS
        )
        return floor, ceil

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "model_id": self.model_id,
                "max_output_tokens": self.max_output_tokens,
                "region": self.region,
                "provider": self.provider,
                "base_url": self.base_url,
                "api_key_env": self.api_key_env,
                "organization_env": self.organization_env,
                "port": self.port,
                "state_base_path": self.state_base_path,
                "hb_interval_floor_sec": self.hb_interval_floor_sec,
                "hb_interval_ceil_sec": self.hb_interval_ceil_sec,
                "hb_enabled": self.hb_enabled,
                "prompt_cache": self.prompt_cache,
                "prompt_cache_ttl": self.prompt_cache_ttl,
                "context_management": self.context_management,
                "context_management_options": dict(self.context_management_options),
                "memory_map": self.memory_map,
                "memory_map_max_tokens": self.memory_map_max_tokens,
                # #368: only when not the default (files without them stay as they were).
                **(
                    {"context_profile": self.context_profile}
                    if self.context_profile != "default"
                    else {}
                ),
                **({"tools_send_allowed_only": True} if self.tools_send_allowed_only else {}),
                **({"tools_short_schemas": True} if self.tools_short_schemas else {}),
                "jev_enabled": self.jev_enabled,
                "jev_classifier": self.jev_classifier,
                "notify_targets": list(self.notify_targets),
                "ui": dict(self.ui),
                # Only when present (phase 7c): files without a YOLO section stay as they were.
                **({"yolo": copy.deepcopy(self.yolo)} if self.yolo else {}),
                **({"progress": copy.deepcopy(self.progress)} if self.progress else {}),
                # #486: only when it holds a value (the defaults otherwise).
                **({"delegation": dict(self.delegation)} if self.delegation else {}),
                # ADR 0021 S2: only when it holds a value (off otherwise).
                **({"agent_jobs": dict(self.agent_jobs)} if self.agent_jobs else {}),
                # ADR 0019: only when present (files without it stay as they were).
                **({"run_checks": copy.deepcopy(self.run_checks)} if self.run_checks else {}),
                # ADR 0011 W4: only when present (files without it stay as they were).
                **({"autonomy": copy.deepcopy(self.autonomy)} if self.autonomy else {}),
                # #451: only when present (files without it stay as they were).
                **({"token_prices": copy.deepcopy(self.token_prices)} if self.token_prices else {}),
                # Task models: only when it holds a value (files without it stay as they were).
                **({"task_models": dict(self.task_models)} if self.task_models else {}),
                # Handoff notes: only a value that differs from the default is written.
                **{
                    name: getattr(self, name)
                    for name, default in _HANDOFF_DEFAULTS.items()
                    if getattr(self, name) != default
                },
            }

    def save(self) -> None:
        with self.lock:
            data = self.as_dict()
            if self.hb_intervals_reset:
                # Reset on load (#512): the file's bounds stay as they were (an omitted one
                # stays omitted); only a save of new bounds changes them.
                for name in _HB_INTERVAL_FIELDS:
                    data.pop(name, None)
                data.update(self.hb_intervals_file)
            _atomic_save(self.path, data, ".gateway-config-")


_HANDOFF_DEFAULTS = {
    "handoff_inject": True,
    "handoff_max_tokens": 400,
    "handoff_max_age_hours": 72,
}


def _default_identity_anchors() -> list[str]:
    return list(StiraConfig().identity_anchors)


def _default_autonomy_boundary() -> dict[str, dict[str, bool]]:
    return {action: {"require_confirmation": True} for action in _AUTONOMY_ACTIONS}


def _normalise_stira_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    normalised = copy.deepcopy(overrides)
    for name in ("protected_layers", "blocked_tool_names"):
        if name in normalised and isinstance(normalised[name], list):
            normalised[name] = frozenset(normalised[name])
    return normalised


def _bc_window_is_bad(size: Any) -> bool:
    return type(size) is not int or size < BC_MIN_SAMPLES


_HB_INTERVAL_FIELDS = ("hb_interval_floor_sec", "hb_interval_ceil_sec")


def _reset_inverted_hb_intervals(
    values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use the default heartbeat bounds when the file's pair is floor > ceil (#512).

    Only the load path does this, like the agent file's roundings (#337): one inverted pair
    in a hand-edited file must not make every settings route answer 503 (they share this
    loader) nor stop the server start. A save or a direct construction still refuses the
    pair. The heartbeat itself does not start on the reset bounds (``HeartbeatGateway``,
    fail-closed).

    An omitted bound counts as its default (what the model fills in), so ``{"floor": 20000}``
    is inverted too; a written bound that is not a finite nonnegative number (``null``, a
    string, a boolean) is left for the normal checks, never replaced. Returns the values to
    build with and, when reset, the interval keys exactly as the file holds them (an omitted
    one absent) so that a save of anything else writes them back unchanged (review r568).
    Once-logged, never with the values.
    """
    global _HB_RESET_WARNED
    defaults = {
        name: GatewayConfig.__dataclass_fields__[name].default for name in _HB_INTERVAL_FIELDS
    }
    numbers = [values.get(name, defaults[name]) for name in _HB_INTERVAL_FIELDS]
    if not all(
        type(number) in (int, float) and math.isfinite(number) and number >= 0 for number in numbers
    ):
        return values, None
    floor, ceil = numbers
    if floor <= ceil:
        return values, None
    if not _HB_RESET_WARNED:
        _HB_RESET_WARNED = True
        logger.warning(
            "hb_interval_floor_sec in the gateway configuration was above "
            "hb_interval_ceil_sec; the heartbeat does not start and runs nothing on other "
            "bounds [hb_intervals_reset]. Save valid bounds in the settings to fix the file."
        )
    kept = {name: values[name] for name in _HB_INTERVAL_FIELDS if name in values}
    return {k: v for k, v in values.items() if k not in _HB_INTERVAL_FIELDS}, kept


def bc_window_clamped_in(path: Path) -> bool:
    """Whether the agent file at ``path`` holds a ``bc_window_size`` that ``load`` rounds (#337).

    A cheap, read-only look at the file as it is now (no validation, no warning, no write):
    False when the file is absent, unreadable, not an object, or the value is fine. The chat
    server's status uses it so that it agrees with ``/api/config`` and the settings hub, which
    both re-read the file: a settings save writes the rounded 5 and all three go to False.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    overrides = data.get("stira_config_overrides") if isinstance(data, dict) else None
    return (
        isinstance(overrides, dict)
        and "bc_window_size" in overrides
        and _bc_window_is_bad(overrides["bc_window_size"])
    )


def _clamp_bc_window_size(values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Round a bad ``bc_window_size`` read from a file up to ``BC_MIN_SAMPLES`` (#337).

    Only the load (start-up) path does this: the file is hand-edited, the value is not
    editable in the settings UI, and refusing it would stop the start. Everywhere else
    (``StiraConfig``, ``AgentConfig(...)``, a save) a bad value is still refused. The
    warning is logged once per process and never carries the value.
    """
    global _BC_CLAMP_WARNED
    overrides = values.get("stira_config_overrides")
    if not isinstance(overrides, dict) or "bc_window_size" not in overrides:
        return values, False
    if not _bc_window_is_bad(overrides["bc_window_size"]):
        return values, False
    if not _BC_CLAMP_WARNED:
        _BC_CLAMP_WARNED = True
        logger.warning(
            "bc_window_size in the agent configuration was not an integer >= %d; "
            "rounded to %d (otherwise the coherence window never measures and COHERENCE "
            "cannot fire) [bc_window_clamped]. Fix the file to silence this.",
            BC_MIN_SAMPLES,
            BC_MIN_SAMPLES,
        )
    return {
        **values,
        "stira_config_overrides": {**overrides, "bc_window_size": BC_MIN_SAMPLES},
    }, True


# The breaker thresholds with their exits (``stira_delivery.NEXT_TURN_FIELDS``): one set.
_BREAKER_PAIR_FIELDS = NEXT_TURN_FIELDS


def _breaker_pairs_inverted(overrides: dict[str, Any]) -> bool:
    """Whether the six thresholds break only a pair rule (#512 requirement 4).

    An omitted threshold counts as its default (what ``StiraConfig`` fills in). Only a set
    of finite numbers in [0, 1] can be "inverted": a written ``null``, string, boolean or an
    out-of-range number is left for the normal checks (refused, never replaced).
    """
    defaults = StiraConfig()
    numbers = {name: overrides.get(name, getattr(defaults, name)) for name in _BREAKER_PAIR_FIELDS}
    if not all(
        type(number) in (int, float) and math.isfinite(number) and 0.0 <= number <= 1.0
        for number in numbers.values()
    ):
        return False
    try:
        StiraConfig(**numbers)
    except ValueError:
        return True  # in range, so only a pair (or critical/warning order) rule failed
    return False


def _reset_inverted_breaker_pairs(
    values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use the default breaker thresholds when the file's set breaks a pair rule (#512).

    Only the load path does this, like the heartbeat bounds (#568): one inverted pair in a
    hand-edited agent file must not make every settings route answer 503 nor stop the
    server start. All six thresholds take their defaults (never looser than the defaults;
    the running engine keeps the stricter of its values and the defaults, see
    ``settings_hub.reset_breaker_thresholds``). A save or a direct construction still
    refuses the set. Returns the values to build with and, when reset, the six keys exactly
    as the file holds them (an omitted one absent), which ``AgentConfig.save`` writes back
    so a save of anything else never rewrites them. Once-logged, never with the values.
    """
    overrides = values.get("stira_config_overrides")
    if not isinstance(overrides, dict) or not _breaker_pairs_inverted(overrides):
        return values, None
    kept = {name: overrides[name] for name in _BREAKER_PAIR_FIELDS if name in overrides}
    rest = {k: v for k, v in overrides.items() if k not in _BREAKER_PAIR_FIELDS}
    return {**values, "stira_config_overrides": rest}, kept


def _warn_breaker_pairs_reset() -> None:
    """Once per process, never with the values (#512)."""
    global _BP_RESET_WARNED
    if not _BP_RESET_WARNED:
        _BP_RESET_WARNED = True
        logger.warning(
            "A breaker threshold in the agent configuration was not below its exit (or the "
            "critical line not below the warning line); the default thresholds are used "
            "[breaker_pairs_reset]. Save valid thresholds in the settings to fix the file."
        )


def protected_layers_clamped_in(path: Path) -> bool:
    """Whether the agent file at ``path`` holds a ``protected_layers`` that ``load`` completes.

    The read-only look ``bc_window_clamped_in`` takes (no validation, warning or write), so
    The chat server's status agrees with ``/api/config`` and the settings hub (#272).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    overrides = data.get("stira_config_overrides") if isinstance(data, dict) else None
    return (
        isinstance(overrides, dict)
        and completed_protected_layers(overrides.get("protected_layers")) is not None
    )


def _complete_protected_layers(values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Add the missing L3-L5 to a hand-edited ``protected_layers`` read from a file (#272).

    Only the load (start-up) path does this, the way ``_clamp_bc_window_size`` rounds the
    window: a hand-edited file must not stop the start, and the protection of layers 3-5 is
    never dropped. A settings save (``AgentConfig(...)``) still refuses such a value. The
    warning is logged once per process and carries no value.
    """
    global _PL_CLAMP_WARNED
    overrides = values.get("stira_config_overrides")
    if not isinstance(overrides, dict):
        return values, False
    completed = completed_protected_layers(overrides.get("protected_layers"))
    if completed is None:
        return values, False
    if not _PL_CLAMP_WARNED:
        _PL_CLAMP_WARNED = True
        logger.warning(
            "protected_layers in the agent configuration did not hold layers 3-5; "
            "they were added [protected_layers_clamped]. Fix the file to silence this."
        )
    return {**values, "stira_config_overrides": {**overrides, "protected_layers": completed}}, True


def hitl_timeout_clamped_in(path: Path) -> bool:
    """Whether the agent file at ``path`` holds a ``hitl_timeout_sec`` that ``load`` bounds."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    overrides = data.get("stira_config_overrides") if isinstance(data, dict) else None
    return (
        isinstance(overrides, dict)
        and "hitl_timeout_sec" in overrides
        and bounded_hitl_timeout(overrides["hitl_timeout_sec"]) is not None
    )


def _bound_hitl_timeout(values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Cap a too-long ``hitl_timeout_sec`` read from a file, or use the default for one that
    is not a positive finite number (#272, #427 review).

    Like the other load-time roundings, only the load path does this; a save or direct
    construction still refuses such a value. Once-logged, never with the value.
    """
    global _HT_CLAMP_WARNED
    overrides = values.get("stira_config_overrides")
    if not isinstance(overrides, dict) or "hitl_timeout_sec" not in overrides:
        return values, False
    bounded = bounded_hitl_timeout(overrides["hitl_timeout_sec"])
    if bounded is None:
        return values, False
    if not _HT_CLAMP_WARNED:
        _HT_CLAMP_WARNED = True
        logger.warning(
            "hitl_timeout_sec in the agent configuration was not a positive number of at most "
            "24 hours; the default or the maximum is used [hitl_timeout_clamped]. "
            "Fix the file to silence this."
        )
    return {**values, "stira_config_overrides": {**overrides, "hitl_timeout_sec": bounded}}, True


@dataclass
class AgentConfig:
    """Per-agent identity, tool, heartbeat, and autonomy policy."""

    identity_anchors: list[str] = field(default_factory=_default_identity_anchors)
    soul_path: str | None = None
    read_only_paths: list[str] = field(default_factory=lambda: sorted(DEFAULT_READ_ONLY_PATHS))
    # Where agent-side file tools may write (#243). "@workspace" is the agent's
    # working root: ``workspace_dir`` when set, else the server's
    # ``<data dir>/workspace/`` (the chat server: .local/stira/workspace/; the rest of
    # the data directory is on the floor, #271). Outside every root: denied
    # (fail-closed); "ask" (HITL card) is reserved, not implemented.
    workspace_dir: str | None = None
    write_roots: list[str] = field(default_factory=lambda: [WORKSPACE_ROOT])
    write_outside_action: str = "deny"
    # Explicit opt-in for a root that is "/", a drive root, the home directory or
    # an ancestor of it (otherwise refused at start-up).
    allow_broad_write_roots: bool = False
    # Where agent-side file tools may read (#457): file_read / view_image / image_reader /
    # editor view / lsp_*. Same tokens as write_roots ("@workspace"); default: the
    # workspace only. "/" is allowed (a person widens it on the settings page, confirmed
    # and recorded); the secret-path floor (identity_paths.SECRET_*) applies whatever the
    # roots, and read_secret_paths adds entries to it (a path, or a name pattern).
    read_roots: list[str] = field(default_factory=lambda: [WORKSPACE_ROOT])
    read_secret_paths: list[str] = field(default_factory=list)
    model_override: str | None = None
    # Per-agent provider (#205). When set, model_override is required and the
    # gateway's base_url/*_env do not apply; only these overrides do.
    provider_override: str | None = None
    base_url_override: str | None = None
    api_key_env_override: str | None = None
    # TODO(#131): expose these fields through the shared settings API/UI after it merges.
    distiller_mode: Literal["rule", "llm", "jev"] = "rule"
    distiller_model_id: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    # LLM distiller provider (#207); only used when distiller_mode == "llm".
    # Credentials by reference, as for the chat model (#205).
    distiller_provider: str = "bedrock"
    distiller_base_url: str | None = None
    distiller_api_key_env: str | None = None
    # Harvest stages the automatic band (0.4-0.7) for human review too (#293), until the
    # staging review UI (#193) exists. False = write it to semantic/knowledge/ directly.
    harvest_stage_auto: bool = True
    # Episodes one scheduled ``memory_harvest`` run scores (#251; rule distiller only, the
    # job itself is created by a human, ADR 0008); the rest wait for the next run.
    harvest_job_max_episodes: int = 100
    # Retention limits per store (#294), read by `stira memory retention` only. Empty =
    # no limits; nothing is removed without `--apply` (archived, undo with `restore`).
    memory_retention: dict[str, Any] = field(default_factory=dict)
    # Record the post-turn emotion snapshot of each new episode (#364; on the data-directory
    # floor, never in the prompt). False = episodes stay episodic-summary/v1.
    # STIRA_EPISODIC_EMOTION (non-empty) overrides it.
    episodic_emotion_snapshot: bool = True
    # Opt-in associative recall (#170): {} = off. Keys enabled / max_items / max_chars /
    # window; the chat server reads it at start-up. No model call when on.
    associative_recall: dict[str, Any] = field(default_factory=dict)
    # ADR 0018 (phase 0, observe only): {} = off — no memory_rate tool, no memory job kind,
    # nothing bound. STIRA_MEMORY_AUTONOMY=0 forces it off. Read at start-up.
    memory_autonomy: dict[str, Any] = field(default_factory=dict)
    # ADR 0023 phase 0 (#460-0, observe only): {} = off. {"phase": 0} counts containment
    # signals and reports the stage a session would be in; nothing is enforced.
    # STIRA_CONTAINMENT_OBSERVE=0 forces it off. Read at start-up.
    containment: dict[str, Any] = field(default_factory=dict)
    # ADR 0022 phase 0 (#458, observe only): {} = off. {"phase": 0} labels each turn's
    # context and counts what a flow layer would hold; nothing is enforced.
    # STIRA_TAINT_OBSERVE=0 forces it off. Read at start-up.
    taint: dict[str, Any] = field(default_factory=dict)
    tool_config: ToolConfig = field(default_factory=ToolConfig)
    hb_session_mode: Literal["continue", "fresh"] = "fresh"
    hb_task_policy: str | dict[str, Any] = field(default_factory=dict)
    autonomy_boundary: dict[str, Any] = field(default_factory=_default_autonomy_boundary)
    stira_config_overrides: dict[str, Any] | None = None
    path: Path = field(default=DEFAULT_AGENT_CONFIG_PATH, repr=False)
    lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)
    # Set by ``load`` when a file held a bc_window_size that had to be rounded up to
    # BC_MIN_SAMPLES (#337). Read-only status; never saved, so the next save is clean.
    bc_window_clamped: bool = field(default=False, init=False, repr=False, compare=False)
    # Same for protected_layers completed with L3-L5 on load (#272).
    protected_layers_clamped: bool = field(default=False, init=False, repr=False, compare=False)
    # Same for a hitl_timeout_sec that was capped or replaced by the default on load (#272).
    hitl_timeout_clamped: bool = field(default=False, init=False, repr=False, compare=False)
    # The file's breaker thresholds broke a pair rule, so ``load`` used the defaults for all
    # six (#512). ``breaker_pairs_file``: those keys as the file holds them; ``save`` writes
    # them back, so only a save of new thresholds (a config without the flag) ends it.
    breaker_pairs_reset: bool = field(default=False, init=False, repr=False, compare=False)
    breaker_pairs_file: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity_anchors, list)
            or not self.identity_anchors
            or any(
                not isinstance(anchor, str) or not anchor.strip()
                for anchor in self.identity_anchors
            )
        ):
            raise ValueError("identity_anchors must be a nonempty array of nonempty strings")
        _require_optional_string(self.soul_path, "soul_path")
        if not isinstance(self.read_only_paths, list) or any(
            not isinstance(path, str) or not path.strip() or "\0" in path
            for path in self.read_only_paths
        ):
            raise ValueError("read_only_paths must be an array of nonempty paths")
        # Additions never remove the mandatory defaults or the actual SOUL path.
        self.read_only_paths = sorted(
            DEFAULT_READ_ONLY_PATHS
            | set(self.read_only_paths)
            | ({self.soul_path} if self.soul_path else set())
        )
        _require_optional_string(self.workspace_dir, "workspace_dir")
        if not isinstance(self.write_roots, list) or any(
            not isinstance(root, str) or not root.strip() or "\0" in root
            for root in self.write_roots
        ):
            raise ValueError("write_roots must be an array of nonempty paths")
        self.write_roots = list(dict.fromkeys(self.write_roots))
        for name in ("read_roots", "read_secret_paths"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(
                not isinstance(entry, str) or not entry.strip() or "\0" in entry for entry in value
            ):
                raise ValueError(f"{name} must be an array of nonempty paths")
            setattr(self, name, list(dict.fromkeys(value)))
        if type(self.allow_broad_write_roots) is not bool:
            raise ValueError("allow_broad_write_roots must be a boolean")
        if self.write_outside_action not in OUTSIDE_ACTIONS:
            raise ValueError("write_outside_action must be one of: " + ", ".join(OUTSIDE_ACTIONS))
        _require_optional_string(self.model_override, "model_override")
        if self.provider_override is not None:
            self.provider_override = _validate_provider(self.provider_override, "provider_override")
            if self.model_override is None:
                raise ValueError("provider_override requires model_override")
        elif self.base_url_override is not None or self.api_key_env_override is not None:
            raise ValueError("base_url_override/api_key_env_override require provider_override")
        self.base_url_override = _validate_optional(
            self.base_url_override, validate_base_url, "base_url_override"
        )
        self.api_key_env_override = _validate_optional(
            self.api_key_env_override, validate_env_name, "api_key_env_override"
        )
        if self.distiller_mode not in ("rule", "llm", "jev"):
            raise ValueError("distiller_mode must be 'rule', 'llm', or 'jev'")
        _require_nonempty_string(self.distiller_model_id, "distiller_model_id")
        self.distiller_provider = _validate_provider(self.distiller_provider, "distiller_provider")
        self.distiller_base_url = _validate_optional(
            self.distiller_base_url, validate_base_url, "distiller_base_url"
        )
        self.distiller_api_key_env = _validate_optional(
            self.distiller_api_key_env, validate_env_name, "distiller_api_key_env"
        )
        if type(self.harvest_stage_auto) is not bool:
            raise ValueError("harvest_stage_auto must be a boolean")
        if type(self.episodic_emotion_snapshot) is not bool:
            raise ValueError("episodic_emotion_snapshot must be a boolean")
        value = self.harvest_job_max_episodes
        if type(value) is not int or not 1 <= value <= HARVEST_JOB_LIMIT_MAX:
            raise ValueError(
                f"harvest_job_max_episodes must be an integer from 1 to {HARVEST_JOB_LIMIT_MAX}"
            )
        from stiracore.control.memory_retention import RetentionPolicy

        self.memory_retention = RetentionPolicy.from_options(self.memory_retention).values
        from stiracore.agent.associative_recall import clean_options

        self.associative_recall = clean_options(self.associative_recall)
        from stiracore.memory_autonomy import clean_options as clean_memory_autonomy

        self.memory_autonomy = clean_memory_autonomy(self.memory_autonomy)
        from stiracore.core.containment import clean_options as clean_containment

        self.containment = clean_containment(self.containment)
        from stiracore.labels import clean_options as clean_taint

        self.taint = clean_taint(self.taint)
        if not isinstance(self.tool_config, ToolConfig):
            raise ValueError("tool_config must be a ToolConfig")
        if self.hb_session_mode not in ("continue", "fresh"):
            raise ValueError("hb_session_mode must be 'continue' or 'fresh'")
        if not isinstance(self.hb_task_policy, (str, dict)):
            raise ValueError("hb_task_policy must be a string or object")
        _require_json_value(self.hb_task_policy, "hb_task_policy")
        if not isinstance(self.autonomy_boundary, dict) or any(
            not isinstance(key, str) for key in self.autonomy_boundary
        ):
            raise ValueError("autonomy_boundary must be an object with string keys")

        boundary = copy.deepcopy(self.autonomy_boundary)
        defaults = _default_autonomy_boundary()
        for action, default_policy in defaults.items():
            policy = boundary.setdefault(action, default_policy)
            if not isinstance(policy, dict):
                raise ValueError(f"autonomy_boundary.{action} must be an object")
            policy.setdefault("require_confirmation", True)
            if type(policy["require_confirmation"]) is not bool:
                raise ValueError(
                    f"autonomy_boundary.{action}.require_confirmation must be a boolean"
                )
        _require_json_value(boundary, "autonomy_boundary")

        if self.stira_config_overrides is not None:
            if not isinstance(self.stira_config_overrides, dict):
                raise ValueError("stira_config_overrides must be an object or null")
            unknown = set(self.stira_config_overrides) - _STIRA_OVERRIDE_FIELDS
            if unknown:
                raise ValueError("Unknown StiraConfig override keys: " + ", ".join(sorted(unknown)))
            _require_json_value(self.stira_config_overrides, "stira_config_overrides")
            StiraConfig(**_normalise_stira_overrides(self.stira_config_overrides))
            timeout = self.stira_config_overrides.get("hitl_timeout_sec")
            if timeout is not None and timeout > HITL_TIMEOUT_MAX_SEC:
                raise ValueError(f"hitl_timeout_sec must be at most {HITL_TIMEOUT_MAX_SEC:g}")
            if "protected_layers" in self.stira_config_overrides:
                check_protected_layers(self.stira_config_overrides["protected_layers"])
            self.stira_config_overrides = copy.deepcopy(self.stira_config_overrides)

        self.identity_anchors = list(self.identity_anchors)
        self.hb_task_policy = copy.deepcopy(self.hb_task_policy)
        self.autonomy_boundary = boundary
        self.path = Path(self.path)

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_AGENT_CONFIG_PATH,
        *,
        tool_config_path: Path = DEFAULT_TOOL_CONFIG_PATH,
    ) -> AgentConfig:
        path = Path(path)
        tool_config_path = Path(tool_config_path)
        allowed = {
            "identity_anchors",
            "soul_path",
            "read_only_paths",
            "workspace_dir",
            "write_roots",
            "write_outside_action",
            "allow_broad_write_roots",
            "read_roots",
            "read_secret_paths",
            "model_override",
            "provider_override",
            "base_url_override",
            "api_key_env_override",
            "distiller_mode",
            "distiller_model_id",
            "distiller_provider",
            "distiller_base_url",
            "distiller_api_key_env",
            "harvest_stage_auto",
            "harvest_job_max_episodes",
            "memory_retention",
            "episodic_emotion_snapshot",
            "associative_recall",
            "memory_autonomy",
            "containment",
            "taint",
            "tool_config",
            "hb_session_mode",
            "hb_task_policy",
            "autonomy_boundary",
            "stira_config_overrides",
        }
        data = _load_object(path, allowed, "agent")
        if data is None:
            return cls(path=path, tool_config=ToolConfig.load(tool_config_path, readonly=True))

        values = dict(data)
        tool_data = values.pop("tool_config", None)
        if tool_data is None:
            tool_config = ToolConfig.load(tool_config_path, readonly=True)
        else:
            if not isinstance(tool_data, dict) or set(tool_data) - _TOOL_CONFIG_KEYS:
                raise ValueError("Invalid embedded tool configuration")
            tool_config = ToolConfig(**tool_data, path=tool_config_path)
        values, clamped = _clamp_bc_window_size(values)
        values, layers_completed = _complete_protected_layers(values)
        values, timeout_bounded = _bound_hitl_timeout(values)
        values, pairs_kept = _reset_inverted_breaker_pairs(values)
        config = cls(**values, tool_config=tool_config, path=path)
        config.bc_window_clamped = clamped
        config.protected_layers_clamped = layers_completed
        config.hitl_timeout_clamped = timeout_bounded
        if pairs_kept is not None:
            config.keep_reset_breaker_pairs(pairs_kept)
            _warn_breaker_pairs_reset()  # after the build (review r591 m-2): it loaded
        return config

    def keep_reset_breaker_pairs(self, kept: dict[str, Any]) -> None:
        """Mark the thresholds as reset on load (#512); ``kept``: the file's six keys."""
        self.breaker_pairs_reset = True
        self.breaker_pairs_file = copy.deepcopy(kept)

    def shown_breaker_values(self, stira_config) -> dict[str, float]:
        """The thresholds the settings page shows: the file's (an omitted one at its
        default) while reset, else ``stira_config``'s (the values in use)."""
        shown = {name: float(getattr(stira_config, name)) for name in _BREAKER_PAIR_FIELDS}
        if self.breaker_pairs_reset:
            defaults = StiraConfig()
            for name in _BREAKER_PAIR_FIELDS:
                shown[name] = float(self.breaker_pairs_file.get(name, getattr(defaults, name)))
        return shown

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "identity_anchors": list(self.identity_anchors),
                "soul_path": self.soul_path,
                "read_only_paths": list(self.read_only_paths),
                "workspace_dir": self.workspace_dir,
                "write_roots": list(self.write_roots),
                "write_outside_action": self.write_outside_action,
                "allow_broad_write_roots": self.allow_broad_write_roots,
                "read_roots": list(self.read_roots),
                "read_secret_paths": list(self.read_secret_paths),
                "model_override": self.model_override,
                "provider_override": self.provider_override,
                "base_url_override": self.base_url_override,
                "api_key_env_override": self.api_key_env_override,
                "distiller_mode": self.distiller_mode,
                "distiller_model_id": self.distiller_model_id,
                "distiller_provider": self.distiller_provider,
                "distiller_base_url": self.distiller_base_url,
                "distiller_api_key_env": self.distiller_api_key_env,
                "harvest_stage_auto": self.harvest_stage_auto,
                "harvest_job_max_episodes": self.harvest_job_max_episodes,
                "memory_retention": copy.deepcopy(self.memory_retention),
                "episodic_emotion_snapshot": self.episodic_emotion_snapshot,
                "associative_recall": copy.deepcopy(self.associative_recall),
                "memory_autonomy": copy.deepcopy(self.memory_autonomy),
                "containment": copy.deepcopy(self.containment),
                "taint": copy.deepcopy(self.taint),
                "tool_config": self.tool_config.as_dict(),
                "hb_session_mode": self.hb_session_mode,
                "hb_task_policy": copy.deepcopy(self.hb_task_policy),
                "autonomy_boundary": copy.deepcopy(self.autonomy_boundary),
                "stira_config_overrides": copy.deepcopy(self.stira_config_overrides),
            }

    def save(self) -> None:
        with self.lock:
            data = self.as_dict()
            if self.breaker_pairs_reset:
                # Reset on load (#512): the file's thresholds stay as they were (an omitted
                # one stays omitted); only a save of new thresholds changes them.
                kept = {
                    k: v
                    for k, v in (data["stira_config_overrides"] or {}).items()
                    if k not in _BREAKER_PAIR_FIELDS
                }
                kept.update(copy.deepcopy(self.breaker_pairs_file))
                data["stira_config_overrides"] = kept
            _publish_roots_change(self.path, data)
            _atomic_save(self.path, data, ".agent-config-")


# ``workspace_dir`` is what "@workspace" points at (#601): moving it moves the roots.
_ROOT_FIELDS = (
    "write_roots",
    "read_roots",
    "allow_broad_write_roots",
    "read_secret_paths",
    "workspace_dir",
)


def _publish_roots_change(path, data: dict) -> None:
    """ADR 0025 §5: a save that changes write roots or read roots ends every session
    allowance (roots counter + 1) before the file is replaced. An unreadable previous file
    counts as a change (fail closed: an end is a narrowing). Never raises."""
    try:
        try:
            with open(path, encoding="utf-8") as handle:
                before = json.load(handle)
        except FileNotFoundError:
            before = {}
        changed = not isinstance(before, dict) or any(
            before.get(name) != data.get(name) for name in _ROOT_FIELDS
        )
    except Exception:  # noqa: BLE001 - unreadable: treat as changed
        changed = True
    if not changed:
        return
    from stiracore.yolo.session_allow import table

    with contextlib.suppress(Exception):  # in-memory only; never fails the save
        table().bump_roots()
