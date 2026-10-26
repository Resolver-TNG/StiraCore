"""StiraCore chat server — ブラウザでリゾルバと対話."""

from __future__ import annotations

import atexit
import copy
import errno
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from flask import Flask, redirect, render_template

from stiracore import (
    ContextAssembler,
    MemoryLayer,
    MemoryStore,
    NeuralState,
    NXPAgent,
    SQLiteEventStore,
    StatePersistence,
    StiraConfig,
    authority,
)
from stiracore.agent import task_models
from stiracore.agent.associative_recall import AssociativeRecall, recall_settings
from stiracore.agent.checkpoint_hooks import read_policy
from stiracore.agent.context_assembler import (
    ProtectedContextOverflow,
    tool_definition_tokens,
)
from stiracore.agent.context_management import (
    ContextManagementSettings,
    resolve_context_window_source,
)
from stiracore.agent.context_profile import assembler_budget
from stiracore.agent.hitl_resume import replay_preview, resume_approved_request
from stiracore.agent.model_switch import apply_model_selection
from stiracore.agent.nxp_agent import prompt_text
from stiracore.agent.prompt_cache import PromptCacheSettings
from stiracore.agent.stimulus_classifier import NO_UPDATE_REASONS, classifier_mode
from stiracore.authority.outbox import LifecycleRecorder
from stiracore.authority.routes import create_authority_blueprint
from stiracore.authority.watchdog import Watchdog
from stiracore.config import AgentConfig, GatewayConfig
from stiracore.config.agent_jobs import AgentJobSettings
from stiracore.config.data_dir import (
    DataDirSettingError,
    explicit_data_dir,
    shown_readable,
    shown_writable,
    writable_names,
)
from stiracore.config.data_dir import build_policy as data_dir_floor_policy
from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
from stiracore.config.models import (
    bc_window_clamped_in,
    hitl_timeout_clamped_in,
    protected_layers_clamped_in,
    ui_board_svg,
)
from stiracore.config.resolver import model_kwargs, model_spec_from
from stiracore.config.stira_delivery import (
    NEXT_TURN_FIELDS,
    SERVER_CHOSEN,
    never_applied,
    plain_values,
    startup_values,
)
from stiracore.control.agent_jobs import GatedJobExecutor, stira_child_builder
from stiracore.control.harvest_job import ACTION_NAME as HARVEST_ACTION
from stiracore.control.harvest_job import (
    HarvestJobStatus,
    harvest_job_precondition,
    memory_harvest_action,
)
from stiracore.control.job_approvals import JobApprovalWaits
from stiracore.control.memory_jobs import (
    MemoryJobExecutor,
    MemoryJobStatus,
    distiller_model_factory,
    plan_counts,
)
from stiracore.control.procedural_proposals import REVIEW_LOG_NAME, ProcedureProposals
from stiracore.core import breaker_reasons, hitl_reasons
from stiracore.core.approval import OperationLedger
from stiracore.core.approval_store import ApprovalStore
from stiracore.core.checkpoint import FileCheckpointStore
from stiracore.core.coherence import bc_measurement
from stiracore.core.containment import ContainmentObserver
from stiracore.core.containment import settings_from as containment_settings_from
from stiracore.core.engine import DEFAULT_IDENTITY_ANCHORS
from stiracore.core.neural_state import DIMS
from stiracore.core.turn_lock import TurnLock
from stiracore.diagnostics.doctor import run_doctor, summarize
from stiracore.diagnostics.write_errors import WriteFailureBoard, WriteFailureHook
from stiracore.heartbeat import HeartbeatDaemon, HeartbeatGateway, HeartbeatLockError
from stiracore.integrations.webhook import WebhookReceiver
from stiracore.labels import settings_from as taint_settings_from
from stiracore.memory_autonomy import RatingSession
from stiracore.memory_autonomy import settings_from as memory_autonomy_settings
from stiracore.memory_rating import rating_counts
from stiracore.middleware.containment_observer import (
    ContainmentObserverHook,
    tee_read_refusals,
)
from stiracore.middleware.taint_observer import TaintObserver, TaintObserverHook
from stiracore.observability import security as _security
from stiracore.observability import settings_hub
from stiracore.observability.access_log import install_polling_filter
from stiracore.observability.artifact_api import create_artifact_blueprint
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.chat_api import (
    HITLApprovalBroker,
    LockBusy,
    create_chat_blueprint,
    timed_lock,
)
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.observability.config_api import _tool_rows as config_tool_rows
from stiracore.observability.containment_api import (
    containment_view,
    create_containment_blueprint,
)
from stiracore.observability.diagnostics_api import create_diagnostics_blueprint
from stiracore.observability.hooks import ObservabilityEvent, create_dashboard_hooks
from stiracore.observability.i18n import install_i18n
from stiracore.observability.lifecycle import ServerLifecycle
from stiracore.observability.masking import (
    MaskingFilter,
    MaskingFormatter,
    install_record_masking,
)
from stiracore.observability.neural_history import (
    NeuralHistory,
    create_neural_history_blueprint,
    history_payload,
)
from stiracore.observability.procedural_api import create_procedural_review_blueprint
from stiracore.observability.scheduler_api import create_scheduler_blueprint
from stiracore.observability.security import install_security, resolve_bind_host
from stiracore.observability.settings_hub import (
    apply_breaker_thresholds,
    classifier_choice,
    filter_tools_by_config,
    reset_breaker_thresholds,
    startup_classifier,
)
from stiracore.observability.shell import STIRA_NAV, install_shell
from stiracore.observability.stage import register_stage, stage_enabled
from stiracore.observability.taint_api import create_taint_blueprint, taint_view
from stiracore.observability.turn_events import (
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    capture_payload,
)
from stiracore.observability.turns_view import create_turns_observability_blueprint
from stiracore.owner_lock import UnsafeLockFileError
from stiracore.procedural_provenance import STAGING_PREFIX, is_staged_key
from stiracore.progress import ProgressTracker
from stiracore.progress import parse_settings as parse_progress_settings
from stiracore.providers import resolve_max_output_tokens
from stiracore.providers.credentials import MissingCredentialError, credential_status
from stiracore.providers.failure import model_banner, status_model
from stiracore.providers.token_prices import parse_token_prices
from stiracore.scheduler import Scheduler, SchedulerLockError, SchedulerLogError
from stiracore.scheduler.executors import health_check_action
from stiracore.scheduler.scheduler import SchedulerRefused
from stiracore.tools import (
    compact_specs,
    create_memory_tools,
    tool_availability,
    tool_policy,
    tool_registry,
)
from stiracore.tools import file_tools as file_tools_module
from stiracore.tools import page_check as page_check_tool
from stiracore.tools import run_checks as run_checks_tool
from stiracore.tools import tool_summary as tool_summary_module
from stiracore.tools import web_tools as web_tools_module
from stiracore.tools.delegate_tool import (
    DELEGATE_TOOL_NAME,
    ChildToolSink,
    DelegateTool,
    DelegationSettings,
)
from stiracore.tools.episodic_emotion import (
    capture_emotion_snapshot,
    emotion_snapshot_enabled,
)
from stiracore.tools.episodic_formatter import EpisodicFormatter, record_episode
from stiracore.tools.handoff_tools import (
    HANDOFF_WRITE_TOOL_NAME,
    HandoffNote,
    create_handoff_tools,
    handoff_settings,
)
from stiracore.tools.memory_index import MemoryMap
from stiracore.tools.memory_rate_tool import create_memory_rate_tool
from stiracore.tools.procedure_tool import (
    PROPOSE_PROCEDURE_TOOL_NAME,
    create_propose_procedure_tool,
)
from stiracore.tools.tool_config import ToolConfig, register_data_root
from stiracore.tools.workspace_artifacts import ArtifactHook, WorkspaceArtifacts
from stiracore.yolo import origin as yolo_origin
from stiracore.yolo import own_dashboard as yolo_own_dashboard
from stiracore.yolo import routes as yolo_routes
from stiracore.yolo import runtime as yolo_runtime
from stiracore.yolo import settings_api as yolo_settings_api
from stiracore.yolo.allowance import YoloSettingError
from stiracore.yolo.config_file import DEFAULTS as YOLO_CONFIG_DEFAULTS
from stiracore.yolo.config_file import clean as yolo_clean_section

ROOT = Path(__file__).resolve().parent.parent
SOUL_PATH = ROOT / "examples" / "personas" / "resolver" / "SOUL.md"
DEFAULT_DATA_DIR = ROOT / ".local" / "stira"
# STIRA_DATA_DIR (#272): an explicit data directory; then the configuration files are
# the ones inside it too. Unset: the default above and the working-directory configs,
# as before (nothing moves). An unusable value refuses the start (create_app).
try:
    _EXPLICIT_DATA_DIR = explicit_data_dir(os.environ)
    _DATA_DIR_ERROR: DataDirSettingError | None = None
except DataDirSettingError as _error:
    _EXPLICIT_DATA_DIR, _DATA_DIR_ERROR = None, _error
DATA_DIR = _EXPLICIT_DATA_DIR or DEFAULT_DATA_DIR
DATA_DIR_FROM_ENV = _EXPLICIT_DATA_DIR is not None
# A resumed turn waits at most this long for a running chat turn to finish (#240).
RESUME_LOCK_TIMEOUT_SEC = 300.0
# Speaker labels of the chat server's episodes (#77); also never used as recall terms (#170).
USER_LABEL = "マスター"
AGENT_LABEL = "リゾルバ"
GATEWAY_CONFIG = GatewayConfig.load() if not DATA_DIR_FROM_ENV else None
if GATEWAY_CONFIG is None:
    try:
        GATEWAY_CONFIG = GatewayConfig.load(DATA_DIR / "gateway-config.json")
    except OSError as _error:
        # Refused in create_app (exit 2, no path); defaults only to finish the import.
        _DATA_DIR_ERROR = DataDirSettingError(
            f"STIRA_DATA_DIR cannot be read ({type(_error).__name__})"
        )
        GATEWAY_CONFIG = GatewayConfig()
MODEL_ID = os.environ.get("STIRA_MODEL", GATEWAY_CONFIG.model_id)
REGION = os.environ.get("AWS_REGION", GATEWAY_CONFIG.region)
PORT = int(os.environ.get("STIRA_PORT", str(GATEWAY_CONFIG.port)))
# Entries per layer that /api/memory/tree reads (the read cap of #289); the response also
# carries each layer's total (#574).
MEMORY_TREE_LIMIT = 20


def _provider_config() -> GatewayConfig:
    """Gateway provider settings with STIRA_PROVIDER/STIRA_BASE_URL/STIRA_API_KEY_ENV applied.

    Only environment-variable NAMES are configured; values are read when the
    model client is built and never reach status, SSE, logs, or config files.
    """
    env = os.environ
    values = {}
    for key, name in (
        ("provider", "STIRA_PROVIDER"),
        ("base_url", "STIRA_BASE_URL"),
        ("api_key_env", "STIRA_API_KEY_ENV"),
    ):
        if name in env:
            values[key] = env[name] or (None if key != "provider" else "bedrock")
    return replace(GATEWAY_CONFIG, **values) if values else GATEWAY_CONFIG


DIMENSIONS = (
    "trust",
    "curiosity",
    "vigilance",
    "attachment",
    "stability",
    "satisfaction",
)


def _registered_tool_name(tool) -> str | None:
    name = getattr(tool, "tool_name", None)
    if isinstance(name, str) and name:
        return name
    spec = getattr(tool, "tool_spec", None)
    if isinstance(spec, dict):
        name = spec.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _stira_tools(
    bound_memory_tools,
    bound_memory_write_names,
    breaker_level=None,
    *,
    hitl_enabled=False,
    read_only_paths=(),
    protected_dirs=(),
    write_policy=None,
    delegate_tool=None,
):
    """Compose tools, retaining writes at CRITICAL when the HITL gate owns them.

    ``delegate_tool`` (#486, ADR 0016) is offered only while ``writes_allowed`` holds for
    the breaker level (NORMAL or COHERENCE: the same criterion the child's write check
    uses), delegation is switched on, **and** the turn is a chat / Stage turn a person's
    message started (``yolo_origin.started_by_person()``, #493 review M4 and round 2
    minor 3). A webhook, bot, approval-resume, a window's automatic continue or other
    unattended turn never gets it: its call would be a card nobody may be watching, and its
    definition would cost tokens on every such turn. (The heartbeat and the scheduler call
    no model, ADR 0008.)

    read_only_paths/protected_dirs (and the write roots of write_policy, #243)
    reach the file tools' execution-boundary wrapper, which also holds inside
    hook-less inner agents (#144/#145).
    """
    normalized = str(getattr(breaker_level, "value", breaker_level)).lower()
    hitl_boundary = hitl_enabled and normalized == "critical"
    effective_level = None if hitl_boundary else breaker_level
    tools = list(
        tool_registry.get_tools(
            effective_level,
            read_only_paths=read_only_paths,
            protected_dirs=protected_dirs,
            write_policy=write_policy,
        )
    )
    if tool_registry.writes_allowed(effective_level):
        tools.extend(bound_memory_tools)
    else:
        tools.extend(
            tool
            for tool in bound_memory_tools
            if _registered_tool_name(tool) not in bound_memory_write_names
        )
    if (
        delegate_tool is not None
        and tool_registry.writes_allowed(breaker_level)
        and yolo_origin.started_by_person()
        and delegate_tool.offered()
    ):
        tools.append(delegate_tool)
    return tools


def _delegation_settings(config_store) -> DelegationSettings:
    """``gateway-config.json`` ``delegation`` (#486), read at every call; unreadable = off."""
    try:
        return DelegationSettings.from_options(config_store.load()[0].delegation)
    except Exception:  # noqa: BLE001 - a bad file switches delegation off (fail closed)
        return DelegationSettings(enabled=False)


def _yolo_breaker_level(engine):
    """The breaker level YOLO samples (ADR 0012 §6): the engine's level, made stricter by
    the live stability (a turn snapshot can lag behind it). Anything but NORMAL pauses."""
    from stiracore.agent.checkpoint_hooks import fresh_breaker_rank

    level = engine.breaker_status().level
    rank = fresh_breaker_rank(engine)
    if rank >= 2:
        return "critical"
    if rank == 1 and str(getattr(level, "value", level)).lower() in ("normal", "coherence"):
        return "warning"
    return level


def _autonomy_settings(path):
    """ADR 0011 W4: the ``autonomy`` section of ``gateway-config.json``, read once at
    start-up; an unreadable file or section is off (fail closed)."""
    from stiracore.grants.service import settings_from

    try:
        section = GatewayConfig.load(path).autonomy
    except (OSError, ValueError, TypeError):
        section = {}
    settings = settings_from(section)
    if settings.warnings:
        logging.getLogger(__name__).warning(
            "gateway-config autonomy: %s (read as off / false)", ", ".join(settings.warnings)
        )
    return settings


def _install_grants(
    app,
    settings,
    authority_service,
    recorder,
    agent,
    tool_paths,
    *,
    hitl_broker=None,
    tool_provider=None,
    turn_busy=None,
    scheduler=None,
    heartbeat_status=None,
):
    """ADR 0011 W4 (#296): the grant control plane, or ``None`` with ``autonomy.stage_c``
    off — then nothing is registered, no file is created and no thread starts. On: the
    service (its store ``grants.json``), the routes, the panel on the settings page, the
    breaker hook (with the authority's) and the containment floor for ``run_checks``.

    W5: with ``autonomy.stage_c_exec`` too, the goal iteration executor
    (``app.extensions["grant_executor"]``) and its route ``POST /api/grants/<id>/step``;
    without it there is no executor object (and no route, no thread).

    W6d: with ``autonomy.heartbeat_start`` too **and** a running scheduler, the
    ``grant_tick`` fire kind (``app.extensions["grant_tick"]``) attached to that scheduler:
    a grant issued with its own ``heartbeat_start`` flag may then be started by the
    scheduler's wake (model-free admission, the executor's same ``step``). Without a
    running scheduler (or heartbeat) the ticker is built but nothing wakes it (or every wake
    is skipped): one structured warning, and a flagged grant shows ``unavailable:
    scheduler_off`` / ``heartbeat_off``."""
    if not settings.stage_c:
        return None
    from stiracore.grants import routes as grants_routes
    from stiracore.grants.service import GrantService

    desk = yolo_runtime.desk()

    def contained() -> bool:
        # The safety floor: run_checks registered and a verified os_sandbox host.
        return run_checks_tool.is_enabled() and run_checks_tool.containment().verified

    grants = GrantService(
        data_dir=DATA_DIR,
        authority=authority_service,
        recorder=recorder,
        settings=settings,
        breaker=lambda: _yolo_breaker_level(agent.engine),
        containment_verified=contained,
        write_roots=lambda: tool_paths["write_policy"].write_roots or (),
        write_policy=lambda: tool_paths["write_policy"],
        # The YOLO desk's operations when it exists: its effective channel (a host command
        # or dialog refuses the browser confirmation); without YOLO, browser-channel ones.
        operations=desk.operations if desk is not None else None,
        # Registered as a tool hook whenever stage C is on (track_tool_calls).
        tool_call_running=lambda: yolo_runtime.TRACKER.running() > 0,
    )
    grants.start()

    def breaker_hook(before, after):
        authority_service.breaker_transition(before, after)
        grants.on_breaker_transition(before, after)

    agent.engine.on_breaker_transition = breaker_hook
    app.extensions["grants_breaker_hook"] = breaker_hook  # read-only, for tests
    executor = None
    if settings.stage_c_exec:
        from stiracore.grants.executor import GoalIterationExecutor

        executor = GoalIterationExecutor(
            grants,
            authority_service,
            parent=lambda: agent,
            tool_provider=tool_provider or (lambda _names: []),
            broker=hitl_broker,
            recorder=recorder,
            turn_busy=turn_busy,
            containment=run_checks_tool.containment,
            run_checks_enabled=run_checks_tool.is_enabled,
        )
    ticker = None
    if executor is not None and settings.heartbeat_start:
        from stiracore.scheduler.grant_tick import GrantTicker

        try:
            heartbeat = dict(heartbeat_status() or {}) if heartbeat_status is not None else {}
        except Exception:  # noqa: BLE001 - unreadable: counted as off
            heartbeat = {}
        unavailable = None
        if scheduler is None or not scheduler.running:
            unavailable = "scheduler_off"  # nothing wakes the ticker on this server
        elif heartbeat.get("enabled") is not True or heartbeat.get("running") is not True:
            unavailable = "heartbeat_off"  # every wake would be skipped heartbeat_unhealthy
        if unavailable is not None:
            # W6d decision (1): one structured warning (codes only), and the code on a
            # flagged grant's tick block (settings panel and Stage board).
            logging.getLogger(__name__).warning(
                "grants: heartbeat_start unavailable (reason=%s)", unavailable
            )
        ticker = GrantTicker(
            grants,
            executor,
            settings=settings,
            authority=authority_service,
            recorder=recorder,
            unavailable=unavailable,
        )
        if unavailable != "scheduler_off":
            scheduler.attach_grant_tick(ticker)
    grants_routes.install(
        app,
        grants,
        browser_ok=lambda: yolo_own_dashboard.armed() is not None,
        executor=executor,
        **({"ticker": ticker} if ticker is not None else {}),
    )
    layer = {
        "host": f"host confirmation ({grants.channel})",
        "browser_weak": "browser confirmation (weaker, autonomy.allow_browser_confirm)",
        "unavailable": "issue / resume / verify refused: no host confirmation channel "
        "(STIRA_YOLO_HOST_CONFIRM, or autonomy.allow_browser_confirm)",
    }[grants.confirm_layer]
    if executor is None:
        print(f"   grants: on (autonomy.stage_c; nothing runs: no executor); {layer}")
    else:
        print(
            "   grants: on with the goal iteration executor (autonomy.stage_c_exec: a person's "
            f"press runs one iteration; wall time {settings.iteration_wall_sec} s); {layer}"
        )
    if ticker is not None:
        print(
            "   grants: heartbeat start on (autonomy.heartbeat_start: a grant issued with it "
            "may start one iteration per scheduler wake; spacing "
            f"{settings.tick_min_spacing_sec} s)"
        )
    if grants.confirm_layer == "browser_weak":
        logging.getLogger(__name__).warning(
            "grants: browser confirmation chosen (autonomy.allow_browser_confirm): a same-user "
            "process can drive it; host confirmation is the stronger layer"
        )
    return grants


def _install_allowances(app, agent, config_store, tool_policy_reader, audit) -> None:
    """ADR 0025: one :class:`AllowanceService` per process, its routes and the gate's
    approver slot (the page's list, revoke and "Revoke all" work with it from PR 2)."""
    from stiracore.agent.checkpoint_hooks import read_policy
    from stiracore.yolo import allow_routes, allow_runtime
    from stiracore.yolo.allow_service import AllowanceService
    from stiracore.yolo.host_channel import principal_for
    from stiracore.yolo.session_allow import roots_digest, table

    write_policy = getattr(agent, "write_policy", None)

    def breaker():
        level = _yolo_breaker_level(agent.engine)
        return str(getattr(level, "value", level)).lower()

    def rules(tool):
        read = read_policy(tool_policy_reader, tool)
        return read if read is not None else ("never", None)

    service = AllowanceService(
        table(),
        record=audit.record_yolo,
        ledger=audit,
        actor=principal_for(os.getuid()) if hasattr(os, "getuid") else "local:unknown",
        breaker=breaker,
        rules=rules,
        roots=lambda: roots_digest(write_policy),
        store_lock=config_store.lock,
    )
    allow_runtime.install(service, policy=write_policy)
    allow_routes.install(app)
    hooks = getattr(agent, "checkpoint_hooks", None)
    if hooks is not None and hasattr(hooks, "set_allow_approver"):
        hooks.set_allow_approver(service)


def _policy_files(config_store, data_dir: Path) -> set[str]:
    """Files that decide what the agent may do (#190): part of the write floor."""
    files = set(config_store.protected_files())
    for name in (
        "tool-policy-approvals.json",
        "tool-policy-approvals.json.tmp",
        "hitl-approvals.db",
        "hitl-approvals.db-wal",
        "hitl-approvals.db-shm",
        "hitl-operations.db",
        "hitl-operations.db-wal",
        "hitl-operations.db-shm",
        "neural-state.json",
    ):
        files.add(str((Path(data_dir) / name).expanduser().absolute()))
    return files


def _config_location(config_store, data_dir: Path) -> dict:
    """Whether the configuration files sit in the data directory (#272).

    The configuration paths default to ``.local/stira/`` under the *working
    directory*, the data directory to the one under the repository (both are the
    ``STIRA_DATA_DIR`` directory when that is set). Started from
    elsewhere, the two differ: saved settings and rules are not the ones next to the
    data. Reported (status, start-up warning) without any path.
    """
    data = Path(data_dir).expanduser().resolve()
    paths = (config_store.gateway_path, config_store.agent_path, config_store.tool_path)
    matches = all(Path(path).expanduser().resolve().parent == data for path in paths)
    # Where the data directory came from (no path): STIRA_DATA_DIR or the default.
    source = "STIRA_DATA_DIR" if DATA_DIR_FROM_ENV else "default"
    return {"matches_data_dir": matches, "data_dir": source}


_YOLO_READ_WARNED: list[bool] = []  # one warning per process (main and the app read it)


def _yolo_config_notice(settings) -> str | None:
    """The start-up line when values from the config file loosen YOLO (PR #444 B1 (3)).

    Field names only, never a value: the allowance above ``off``, the browser as the
    confirmation channel, or a cap / sub-cap above its default, taken from the file (not a
    variable). A shell command in an exec window can edit the file like the tool rules
    (ADR 0012 §9); this line is how the next start shows where the values came from.
    """
    if not settings.enabled:
        return None  # nothing is in effect without the allowance
    sources = getattr(settings, "sources", None) or {}
    current = {
        "allow": settings.allowance,
        "host_confirm": settings.host_confirm,
        "max_auto_approvals": settings.cap,
        "max_external": settings.external_cap,
        "max_exec": settings.exec_cap,
    }
    looser = {
        "allow": settings.allowance != "off",
        "host_confirm": settings.host_confirm == "browser",
        **{
            name: current[name] > YOLO_CONFIG_DEFAULTS[name]
            for name in ("max_auto_approvals", "max_external", "max_exec")
        },
    }
    names = [name for name in current if sources.get(name) == "config" and looser[name]]
    if not names:
        return None
    return (
        "INFO: YOLO settings from gateway-config.json are in effect ("
        + ", ".join(names)
        + "; the settings page shows each value and its source)"
    )


def _progress_settings(path):
    """ADR 0014 §10: the ``progress`` section of ``gateway-config.json`` and its variables
    (environment > config > default; off by default). Never refuses; codes only."""
    try:
        section = GatewayConfig.load(path).progress
    except (OSError, ValueError, TypeError):
        section = {}
    settings = parse_progress_settings(os.environ, section)
    if settings.warnings:
        logging.getLogger(__name__).warning(
            "gateway-config progress: %s (rounded or left out)", ", ".join(settings.warnings)
        )
    return settings


def _progress_may_continue(engine) -> bool:
    """ADR 0014 §4 (PR #489 review, major 2): an automatic continue starts only while the
    breaker YOLO samples is NORMAL and nothing is latched. The window pauses on a breaker
    sample too; this is read directly so the continue never depends on that order."""
    level = _yolo_breaker_level(engine)
    if str(getattr(level, "value", level)).lower() != "normal":
        return False
    return yolo_runtime.status().get("latched") is False


def _run_checks_settings(path):
    """``gateway-config.json`` ``run_checks`` (ADR 0019), read once at start-up; an
    unreadable file or section is off (fail closed)."""
    try:
        section = GatewayConfig.load(path).run_checks
    except (OSError, ValueError, TypeError):
        section = None
    return run_checks_tool.settings_from(section)


def _token_prices(path) -> dict:
    """``gateway-config.json`` ``token_prices`` (#451): model id -> ``Pricing``, read once
    at start-up. Stira ships no price data: without an entry for a model its cost is not
    shown. Never refuses; one warning with codes only (never a value)."""
    try:
        section = GatewayConfig.load(path).token_prices
    except (OSError, ValueError, TypeError):
        section = None
    prices, warnings = parse_token_prices(section)
    if warnings:
        logging.getLogger(__name__).warning(
            "gateway-config token_prices: %s (left out)", ", ".join(warnings)
        )
    return prices


def _yolo_config_section(path) -> dict:
    """The cleaned ``yolo`` section of ``gateway-config.json`` (ADR 0012 phase 7c).

    Read once at start-up; never refuses: an unreadable file reads as no section (the
    server reports the file itself elsewhere), an invalid value is rounded or left out,
    an unknown allowance is ``off`` — one warning naming the codes, never a value.
    """
    try:
        raw = GatewayConfig.load(path).yolo
    except (OSError, ValueError, TypeError) as error:
        # Off, as before — but said once, so "I set it and nothing happens" is visible
        # (PR #444 m2). The exception's type only, never its text (it carries the path).
        if not _YOLO_READ_WARNED:
            _YOLO_READ_WARNED.append(True)
            logging.getLogger(__name__).warning(
                "gateway-config yolo: the file could not be read (%s); YOLO values from it "
                "are not used (off unless a variable sets them)",
                type(error).__name__,
            )
        return {}
    section, warnings = yolo_clean_section(raw)
    if warnings:
        logging.getLogger(__name__).warning(
            "gateway-config yolo: %s (rounded or left out; an unknown allowance is off)",
            ", ".join(warnings),
        )
    return section


def _default_config_store() -> ConfigStore:
    """With STIRA_DATA_DIR the configuration files are the ones in it (#272).

    Without it, the long-standing defaults (relative to the working directory) stay:
    an existing install keeps reading the files it always read.
    """
    if not DATA_DIR_FROM_ENV:
        return ConfigStore()
    return ConfigStore(
        DATA_DIR / "gateway-config.json",
        DATA_DIR / "agent-config.json",
        DATA_DIR / "tool-config.json",
    )


FLASK_LOG_FORMAT = "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"


def _install_log_masking(stream=None) -> list[logging.Handler]:
    """Server logs (stderr, i.e. ``stira.out.log``) go through ``mask_secrets`` (#291).

    Every log record is masked when it is created (``install_record_masking``), so
    handlers that were already registered (``logging.basicConfig``, a library's) or
    are added later never see plain text; every handler that already exists on any
    logger also gets a :class:`MaskingFilter` (records made outside the factory).
    Then, before the app exists, the root logger gets a WARNING handler in Flask's
    format (it stands in for Python's last-resort handler, and Flask then logs
    through it instead of adding its own); werkzeug's access log gets its handler
    before it would add its own (kept off the root handler, so no line is written
    twice). Process-wide, so only :func:`main` calls it. Returns the handlers it
    added.
    """
    from flask.logging import default_handler

    install_record_masking()
    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    for existing in {default_handler, *(h for lg in loggers for h in getattr(lg, "handlers", ()))}:
        if not any(isinstance(f, MaskingFilter) for f in existing.filters):
            existing.addFilter(MaskingFilter())
    default_handler.setFormatter(MaskingFormatter(FLASK_LOG_FORMAT))
    root = logging.StreamHandler(stream)
    root.setLevel(logging.WARNING)
    root.setFormatter(MaskingFormatter(FLASK_LOG_FORMAT))
    root.addFilter(lambda record: not record.name.startswith("werkzeug"))
    logging.getLogger().addHandler(root)
    access = logging.StreamHandler(stream)
    access.setFormatter(MaskingFormatter("%(message)s"))
    werkzeug = logging.getLogger("werkzeug")
    werkzeug.addHandler(access)
    install_polling_filter()  # #369: successful page polls stay out of the access log
    if werkzeug.level == logging.NOTSET:
        werkzeug.setLevel(logging.INFO)
    return [root, access]


ANCHORS_DEFAULT_CODE = "identity_anchors_default"
ANCHORS_DEFAULT_WARNING = (
    "WARNING: identity_anchors is not set in agent-config.json; the built-in neutral "
    f"anchors are in use [{ANCHORS_DEFAULT_CODE}]. Versions before #152 had a different "
    "built-in list: to keep the anchors you had, write them into identity_anchors "
    "(docs/configuration.md, Identity anchors)."
)


def _anchors_configured(agent_path) -> bool:
    """Whether agent-config.json itself sets ``identity_anchors`` (#152).

    By the key, not by the value: a file that repeats the built-in list is configured.
    Called after ``AgentConfig.load`` succeeded, so the file is absent or a JSON object.
    """
    try:
        data = json.loads(Path(agent_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    return isinstance(data, dict) and "identity_anchors" in data


def _anchors_banner(anchors, *, configured: bool) -> str:
    """Origin and count of the identity anchors in force (#152). Never their text."""
    if anchors is None:
        return "unavailable"
    if not configured:
        return f"default ({len(anchors)})"
    if tuple(anchors) == DEFAULT_IDENTITY_ANCHORS:
        return f"configured ({len(anchors)}, same as built-in)"
    return f"configured ({len(anchors)})"


SOUL_STATES = ("loaded", "empty", "missing", "unreadable")


def _soul_state(soul_path: Path) -> tuple[str, str | None]:
    """The real read state of the SOUL file (#291): one of ``SOUL_STATES``, and the error
    type when unreadable.

    Read like ``ContextAssembler.load_soul`` does (UTF-8 text): ``loaded`` only when
    the text reads and is not empty.
    """
    try:
        text = Path(soul_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeDecodeError) as error:
        return "unreadable", type(error).__name__
    return ("loaded" if text.strip() else "empty"), None


def _soul_banner(soul_path: Path, *, configured: bool) -> str:
    """``configured``/``default`` and the real read state of the SOUL file (#291). No path."""
    origin = "configured" if configured else "default"
    state, error = _soul_state(soul_path)
    if state == "missing":
        return f"{origin} (❌ missing)"
    if state == "unreadable":
        return f"{origin} (❌ unreadable: {error})"
    return f"{origin} ({'✅ loaded' if state == 'loaded' else '⚠️ empty'})"


def _soul_status(soul_path: Path, *, configured: bool) -> dict:
    """``/api/gateway/status`` view of the SOUL (#291): the read state and where it is set
    (``configured`` in agent-config.json or the ``default``), never the path."""
    state, _error = _soul_state(soul_path)
    return {
        "loaded": state == "loaded",
        "state": state,
        "source": "configured" if configured else "default",
    }


def _revoke_open_approvals(approval_store) -> None:
    """Cancel every pending or approved-but-unused approval; raise if any one fails."""
    # The store's unbounded query: nothing is missed however long the history is.
    open_records = list(approval_store.open_records())
    for record in open_records:
        approval_store.cancel(record.request_id, reason=hitl_reasons.RULES_CHANGED)
        current = approval_store.get(record.request_id)
        if current is not None and (
            current.status == "pending"
            or (current.status in ("approved", "edited") and not current.consumed)
        ):
            raise RuntimeError("an approval could not be revoked")


REVOKE_FAILED = "the tool rules changed and the open approvals could not be revoked"


def _tool_rules_saved_hook(tool_policy_reader, hitl_broker):
    """After a committed tool-rule save: void the open approvals now (#287, #330).

    The revocation failing suspends approvals in the broker (fail-closed: no open
    card can be approved) and re-raises, so the save answers with an error; the
    broker's live queue is reconciled with the store in every case.
    """

    def saved() -> None:
        # ADR 0011 §5 / ADR 0012 §6b: a rules save moves the authority epoch (the
        # process-wide service, also without YOLO; W1 #296); an issuing YOLO request is
        # fenced. A failing epoch move never skips the revocation below, and any failure
        # suspends approvals (so the error answer's "approvals are refused" is true).
        try:
            try:
                yolo_runtime.advance_epoch()
            finally:
                tool_policy_reader.sync()
        except BaseException:
            hitl_broker.suspend_approvals(REVOKE_FAILED)
            raise
        finally:
            try:
                hitl_broker.sync_from_store()
            except Exception:  # noqa: BLE001 - never hides the revocation's own error
                print("WARNING: the approval queue could not be reconciled with the store")

    return saved


def _tool_policy_reader(
    config_store,
    approval_store=None,
    marker_path=None,
    on_reconciled=None,
    delegation_enabled=None,
):
    """Live ``(rule, generation)`` for the HITL gate (#190, ADR 0010).

    Read from tool-config.json at every call, so a saved change applies to the next
    call and voids an approval still waiting (the gate compares generations). When
    the generation differs from the one the open approvals were asked under
    (``marker_path``, kept across restarts), every open approval in the durable store
    is revoked first, so a rule saved while the server was down never revives one.
    ``on_reconciled`` runs after every sync that completed (the open approvals match
    the rules): it lifts an approval suspension set by a failed revocation (#330).
    Any failure raises, and the gate treats it as Never (fail closed). Tools the
    registry does not govern (the server's own memory tools) are left to the
    breaker/HITL. ``delegate`` (#486, ADR 0016) is Ask while delegation is on (every call
    is a person's card: it spends tokens; YOLO never answers it before phase 3) and Never
    while it is off (``delegation_enabled``; none given = off).
    """
    lock = threading.Lock()

    def generation_of_open_approvals():
        try:
            data = json.loads(Path(marker_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return data.get("generation") if isinstance(data, dict) else "invalid"

    def sync(generation) -> None:
        if approval_store is None or marker_path is None:
            return
        with lock:
            if generation_of_open_approvals() != generation:
                _revoke_open_approvals(approval_store)
                target = Path(marker_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_text(json.dumps({"generation": generation}), encoding="utf-8")
                os.replace(temporary, target)
        if on_reconciled is not None:
            on_reconciled()

    def policy(name: str):
        config = config_store.load_tools()
        generation = (config.generation, config.integrity_error)
        sync(list(generation))
        if config.integrity_error:
            return ("never", generation)
        if name == DELEGATE_TOOL_NAME:
            on = delegation_enabled is not None and delegation_enabled()
            return ("ask" if on else "never", generation)
        if name not in tool_registry.READ_TOOLS and name not in tool_registry.WRITE_TOOLS:
            return ("always", generation)
        return (config.policy_of(name), generation)

    policy.sync = lambda: sync(list(_generation(config_store)))
    return policy


def _generation(config_store):
    config = config_store.load_tools()
    return (config.generation, config.integrity_error)


def _breaker_view(engine, previous: dict) -> dict:
    """Live breaker level from the engine; keep last_changed stable until it changes (#161)."""
    status = engine.breaker_status()
    level = getattr(getattr(status, "level", None), "value", None) or "unknown"
    if level == previous.get("level"):
        view = {"level": level, "last_changed": previous.get("last_changed")}
    else:
        view = {"level": level, "last_changed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    view.update(_breaker_reason_fields(engine, level))
    return view


def _breaker_reason_fields(engine, level: str) -> dict:
    """#455 (additive to level / last_changed): why the level is what it is, and the last
    change. Codes and rounded numbers only; empty when the engine's explanation is of
    another level (a turn snapshot) or unreadable."""
    try:
        explanation = engine.breaker_explanation() or {}
        transition = breaker_reasons.clean_transition(engine.last_breaker_transition())
    except Exception:  # noqa: BLE001 - status never fails on an explanation
        explanation, transition = {}, None
    if explanation.get("level") == level:
        reasons, inputs = breaker_reasons.clean(
            explanation.get("reasons"), explanation.get("inputs")
        )
    else:
        reasons, inputs = [], {}
    return {"reasons": reasons, "inputs": inputs, "last_transition": transition}


def _with_containment(summary, observer):
    """ADR 0023 phase 0: add the window's ``would_be`` and score to the turn summary."""
    if observer is None or not isinstance(summary, dict):
        return summary
    try:
        window = observer.snapshot()
    except Exception:  # noqa: BLE001 - telemetry never breaks a turn
        return summary
    return {**summary, "containment": {"would_be": window["would_be"], "score": window["score"]}}


def _with_taint(summary, observer):
    """ADR 0022 phase 0: add the last turn's context label and would-hold counts."""
    if observer is None or not isinstance(summary, dict) or observer.last is None:
        return summary
    return {**summary, "taint": dict(observer.last)}


def _yolo_breaker_reasons(engine) -> dict:
    """The codes and numbers behind the level YOLO samples (#455): the engine's own, or,
    when the live stability made the level stricter, the stability comparison itself."""
    from stiracore.agent.checkpoint_hooks import fresh_breaker_rank

    sampled = _yolo_breaker_level(engine)
    level = str(getattr(sampled, "value", sampled)).lower()
    fields = _breaker_reason_fields(engine, level)
    if fields["reasons"]:
        return {"reasons": fields["reasons"], "inputs": fields["inputs"]}
    rank = fresh_breaker_rank(engine)
    config, stability = engine.config, engine.state.stability
    if rank >= 2:
        code, values = "stability_below_critical", {"stability_critical": config.stability_critical}
    elif rank == 1:
        code, values = "stability_below_warning", {"stability_warning": config.stability_warning}
    else:
        return {"reasons": [], "inputs": {}}
    reasons, inputs = breaker_reasons.clean([code], {"stability": stability, **values})
    return {"reasons": reasons, "inputs": inputs}


def _doctor_classifier_choice(config_store) -> str:
    """The saved classifier choice for the doctor's check (read-only; ``auto`` when unreadable)."""
    try:
        return classifier_choice(config_store.load()[0])
    except (OSError, ValueError, TypeError):
        return "auto"


def _classifier_view(agent) -> dict:
    """Describe the stimulus classifier actually in use (shared by status and config)."""
    resolved = getattr(agent, "_stimulus_classifier", None)
    name = "unresolved" if resolved is None else type(resolved).__name__
    return {
        "name": name,
        "is_jev": name == "SokudanClassifier",
        "model": getattr(resolved, "model_id", None),
        # #367: rule_only / sokudan / unknown (unknown = not resolved yet, or third-party).
        "mode": classifier_mode(resolved),
    }


def _sokudan_installed() -> bool:
    """Whether ``sokudan`` can be imported, without importing it (start-up notice, #367)."""
    import importlib.util

    try:
        return importlib.util.find_spec("sokudan") is not None
    except (ImportError, ValueError):
        return False


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _emotion_updates_view(agent) -> dict | None:
    """Turns since start-up that moved Neural State, and why the others did not (#367).

    Allow-listed: only ``NO_UPDATE_REASONS`` codes leave the server; any other last
    reason reads ``unknown``. ``last`` is null before the first turn. Read-only.
    """
    counts = getattr(agent, "emotion_updates", None)
    if not isinstance(counts, dict):
        return None
    raw = counts.get("reasons")
    raw = raw if isinstance(raw, dict) else {}
    reasons = {code: _count(raw.get(code)) for code in NO_UPDATE_REASONS}
    turns, updated = _count(counts.get("turns")), _count(counts.get("updated"))
    reason = counts.get("last_reason")
    last = None
    if turns:
        last = {
            "updated": reason is None,
            "reason": None
            if reason is None
            else (reason if reason in NO_UPDATE_REASONS else "unknown"),
        }
    return {
        "turns": turns,
        "updated": updated,
        "not_updated": max(0, turns - updated),
        "reasons": reasons,
        "last": last,
    }


def _context_window_view(agent) -> dict | None:
    """Read-only context window for the gauge (#350): the window summarization uses.

    Same resolution as context management (``resolve_context_window_source``; the
    setting, the model's own limit, the capability row, else 200,000 = ``default``).
    ``compression_threshold`` matters only while context management is enabled.
    """
    try:
        settings = agent.context_management
        tokens, source = resolve_context_window_source(settings, agent.model, agent.capabilities)
        return {
            "tokens": tokens,
            "source": source,
            "compression_threshold": settings.compression_threshold,
        }
    except Exception:  # noqa: BLE001 - status telemetry: unknown, never a failure
        return None


_NEW_SESSION_EVENTS = {
    "reset": "session.reset",
    "started": "session.started",
    "refused": "session.reset_refused",
    "failed": "session.reset_failed",
}
# Bounded waits for the shared reset/decision lock (#350). Its holders do only approval-store
# reads/writes (SQLite, 5 s busy timeout) and in-memory assignments, never the event store.
DECISION_LOCK_TIMEOUT_SEC = 5.0


class _NewSessionHost:
    """The chat server's side of POST /api/chat/session/new (#350).

    One re-entrant lock (``lock``) is shared with the approval broker, which holds it around
    every decision (from the store transition to scheduling the resume), and is taken here to
    reserve a resume. The reset holds it only to judge and reset (approval-store reads and
    in-memory assignments); its audit rows are written outside it. Every acquisition is
    bounded (``timed_lock``): a decision that cannot get it in time is "busy" (503, nothing
    recorded), a reset is ``decision_in_progress`` (409, nothing reset). A decision or a
    reservation that started first makes the reset refuse (``approval_pending`` /
    ``resume_pending``); one that comes later finds nothing left to decide (a pending
    approval would have refused the reset). A resume replays its checkpoint's *old*
    conversation, which is why the two must never interleave.

    audit: rows in the hash-chained events.db (ADR 0009; ``emit`` returns after COMMIT,
    WAL with SQLite's default ``synchronous=FULL``, errors propagate): ``session.reset`` before
    the reset (no row, no reset), then ``session.started`` / ``session.reset_refused`` /
    ``session.reset_failed``. Counts, ids, the principal and the key's digest; never text.
    """

    def __init__(self, agent, event_store, lock) -> None:
        self._agent = agent
        self._events = event_store
        self.lock = lock
        self._resumes = 0

    def exclusive(self, timeout: float):
        return timed_lock(self.lock, timeout)

    def resume_scheduled(self) -> None:
        # Called inside the broker's decision (the lock is already held: re-entrant).
        with timed_lock(self.lock, DECISION_LOCK_TIMEOUT_SEC):
            self._resumes += 1

    def resume_finished(self) -> None:
        # Background thread, after the turn lock was released: wait (bounded steps) until
        # the count is down, so a finished resume never keeps /new refused.
        while True:
            try:
                with timed_lock(self.lock, DECISION_LOCK_TIMEOUT_SEC):
                    self._resumes = max(0, self._resumes - 1)
                    return
            except LockBusy:
                logging.getLogger(__name__).warning("resume_finished: lock busy, retrying")

    def blocker(self) -> str | None:
        with timed_lock(self.lock, DECISION_LOCK_TIMEOUT_SEC):
            return "resume_pending" if self._resumes else None

    def audit_ready(self) -> bool:
        """False only for a *sticky* event-store failure (``error`` set: a database or
        chain error, kept until the process reopens the store). A store that was busy
        at its open (``healthy`` False, ``error`` None) is retried by the write itself,
        and a transient busy/locked write fails that one request (500 ``audit_failed``)
        without refusing the next one."""
        return self._events.error is None

    def reset(self) -> int:
        return self._agent.start_new_conversation()

    def audit(self, stage: str, record: dict) -> None:
        self._events.emit(
            ObservabilityEvent(
                event_type=_NEW_SESSION_EVENTS[stage],
                status="error" if stage in ("failed", "refused") else "success",
                message="new conversation (/new)",
                attributes=dict(record),
            )
        )


HEARTBEAT_DISABLED = {"enabled": False, "running": False}

UI_HOME_STAGE_OFF_WARNING = (
    'WARNING: gateway-config "ui.home" is "stage" but the Stage is off (STIRA_STAGE is not set);'
    " / opens the chat"
)


def _ui_home(config_store, stage_on: bool) -> str:
    """The page ``/`` opens (#350): gateway-config ``ui.home``, read once at start-up.

    "stage" only takes effect when the Stage page exists (STIRA_STAGE=1); anything else,
    including an unreadable configuration, falls back to the chat.
    """
    try:
        gateway_config, _agent_config = config_store.load()
        home = gateway_config.ui.get("home", "chat")
    except (OSError, ValueError, TypeError):
        return "chat"
    if home == "stage" and not stage_on:
        print(UI_HOME_STAGE_OFF_WARNING)
        return "chat"
    return "stage" if home == "stage" else "chat"


def _heartbeat_enabled(gateway_config: GatewayConfig) -> bool:
    """Opt-in only (#238): STIRA_HEARTBEAT=1/0 overrides gateway-config ``hb_enabled``."""
    # One rule with the settings page, which shows the switch as pinned (#472).
    return settings_hub.effective_heartbeat_enabled(gateway_config)


def _heartbeat_runtime(daemon, status: dict, config_store) -> dict:
    """What the heartbeat runs with (#472), for the settings page's "applied": the switch
    after STIRA_HEARTBEAT and the bounds the daemon was built with (read once at start-up).
    """
    if daemon is not None:
        gateway_config = daemon.gateway.gateway_config
    else:
        try:
            gateway_config, _agent_config = config_store.load()
        except (OSError, ValueError, TypeError):
            gateway_config = GATEWAY_CONFIG
    error = status.get("last_error") if daemon is None else None
    return {
        "enabled": status.get("enabled") is True,
        "interval_floor_sec": float(gateway_config.hb_interval_floor_sec),
        "interval_ceil_sec": float(gateway_config.hb_interval_ceil_sec),
        # A class name when it was turned on but did not start (never a message).
        "error": error if isinstance(error, str) else None,
    }


def _start_heartbeat(
    config_store,
    engine,
    state_dir: Path,
    *,
    on_tick,
    snapshot_sink,
    persistence=None,
    lifecycle: ServerLifecycle | None = None,
):
    """Start the zero-model tick loop when opted in; never fails the server start.

    Returns ``(daemon_or_None, status_provider)``. The status is read-only:
    there is deliberately no route that starts, stops or triggers a tick.
    Every dependency (the shared save lock) is wired before the thread starts,
    because the first tick runs immediately.
    """
    try:
        gateway_config, _agent_config = config_store.load()
    except (OSError, ValueError, TypeError):
        gateway_config = GATEWAY_CONFIG
    if not _heartbeat_enabled(gateway_config):
        return None, lambda: dict(HEARTBEAT_DISABLED)
    try:
        if persistence is not None:
            # Saves now race with ticks: snapshot them under the shared lock (#147).
            persistence.state_lock = engine.state_lock
        gateway = HeartbeatGateway(engine, gateway_config, snapshot_sink=snapshot_sink)
        daemon = HeartbeatDaemon(gateway, state_dir=state_dir, on_tick=on_tick)
        if lifecycle is not None:
            # Registered before the thread starts: a stop during start-up finds it (#285).
            lifecycle.add("heartbeat", lambda t: _stop_label(daemon.stop(t)), order=30)
        daemon.start()
    except UnsafeLockFileError as error:
        # A symlink / hard link / foreign file named heartbeat.lock (#322): untouched.
        # Before HeartbeatLockError: the unsafe error is also one (compatibility).
        print(f"   Heartbeat: ⚠️ heartbeat.lock is unsafe ({error.reason}); not started")
        unsafe = {"enabled": True, "running": False, "last_error": "UnsafeLockFileError"}
        return None, lambda: dict(unsafe)
    except HeartbeatLockError:
        print("   Heartbeat: ⚠️ another process owns this state directory; not started")
        refused = {"enabled": True, "running": False, "last_error": "HeartbeatLockError"}
        return None, lambda: dict(refused)
    except Exception as error:  # noqa: BLE001 - the heartbeat is optional; serve without it
        # Class name only: the message may carry paths.
        print(f"   Heartbeat: ⚠️ not started ({type(error).__name__})", file=sys.stderr)
        failed = {"enabled": True, "running": False, "last_error": type(error).__name__}
        return None, lambda: dict(failed)
    if lifecycle is None:
        atexit.register(daemon.stop)
    return daemon, daemon.status


SCHEDULER_DISABLED = {"enabled": False, "running": False}


def _scheduler_enabled() -> bool:
    """Opt-in only (#239, ADR 0008): STIRA_SCHEDULER=1 enables operator-defined jobs."""
    raw = os.environ.get("STIRA_SCHEDULER", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _start_scheduler(
    engine,
    state_dir: Path,
    *,
    heartbeat_status,
    lifecycle: ServerLifecycle | None = None,
    extra_actions: dict | None = None,
    action_checks: dict | None = None,
    memory_executor=None,
    memory_kinds=frozenset(),
    memory_daily_token_budget=None,
    agent_executor=None,
    agent_daily_token_budget=None,
    approval_waits=None,
):
    """Start the scheduler when opted in; never fails the server start.

    Only model-free Gateway actions are registered (``health_check``, and the rule-only
    ``memory_harvest`` of #251). Agent-layer jobs (an operator's prompt) run only with
    ``agent_executor`` (gateway ``agent_jobs.enabled``, default off; ADR 0021 S2). With
    ``memory_autonomy`` on (ADR 0018), the fixed memory job kinds it enables run in their own
    executor; nothing runs until a person creates such a job (``confirm: true``).
    Returns ``(scheduler_or_None, status_provider)``.
    """
    if not _scheduler_enabled():
        return None, lambda: dict(SCHEDULER_DISABLED)

    def breaker_view():
        return {"level": getattr(engine.breaker_status().level, "value", "unknown")}

    scheduler = Scheduler(
        state_dir,
        breaker_level=lambda: engine.breaker_status().level,
        gateway_actions={
            "health_check": health_check_action(
                {"heartbeat": lambda: heartbeat_status(), "breaker": breaker_view}
            ),
            **(extra_actions or {}),
        },
        health_source=lambda: heartbeat_status(),
        action_checks=action_checks,
        memory_executor=memory_executor,
        memory_kinds=memory_kinds,
        memory_daily_token_budget=memory_daily_token_budget,
        # ADR 0021 S2: the gated Agent-layer executor, only with gateway agent_jobs on.
        agent_executor=agent_executor,
        approval_waits=approval_waits,
        **(
            {"agent_daily_token_budget": agent_daily_token_budget}
            if agent_daily_token_budget is not None
            else {}
        ),
    )
    if lifecycle is not None:
        # Registered before start (#285); stop() of a scheduler that never ran is a no-op.
        lifecycle.add("scheduler", lambda t: _stop_label(scheduler.stop(t)), order=20)
    try:
        scheduler.start()
    except UnsafeLockFileError as error:
        # A symlink / hard link / foreign file named scheduler.lock (#322): untouched.
        # Before SchedulerLockError: the unsafe error is also one (compatibility).
        print(f"   Scheduler: ⚠️ scheduler.lock is unsafe ({error.reason}); not started")
    except SchedulerLockError:
        print("   Scheduler: ⚠️ another process owns this state directory; not started")
    except SchedulerLogError:
        # The log is kept for inspection; nothing runs and every change is refused.
        print("   Scheduler: ⚠️ event log failed verification; not started", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - optional; serve without it
        print(f"   Scheduler: ⚠️ not started ({type(error).__name__})", file=sys.stderr)
    if scheduler.running and lifecycle is None:
        atexit.register(scheduler.stop)
    return scheduler, scheduler.status


def _agent_job_settings(config_store) -> AgentJobSettings:
    """``gateway-config.json`` ``agent_jobs`` (ADR 0021 S2), read at start; unreadable = off."""
    try:
        return AgentJobSettings.from_options(config_store.load()[0].agent_jobs)
    except Exception:  # noqa: BLE001 - a bad file keeps the Agent layer off (fail closed)
        return AgentJobSettings()


def _agent_job_executor(settings, parent, tool_provider, approval_waits=None):
    """The gated Agent-layer executor (ADR 0021 §5), or None while ``agent_jobs`` is off.

    The child is built like ``delegate``'s (ADR 0016): the server agent's rule reader,
    floors and write policy reach it; the server's breaker is read before every call."""
    if not settings.enabled:
        return None

    def rule_of(name):
        found = read_policy(parent().checkpoint_hooks.policy_check, name)
        return None if found is None else found[0]

    return GatedJobExecutor(
        stira_child_builder(parent, tool_provider),
        breaker_level=lambda: parent().engine.breaker_status().level,
        rule_of=rule_of,
        # ADR 0024 phase 2: ``unattended_approval: wait`` jobs pause on an Ask (opt-in).
        waits=approval_waits,
    )


def _associative_recall(config_store, store, engine, ignore_terms=()):
    """The opt-in associative recall (#170), or None when off. Read at start-up; never fails.

    ``associative_recall.enabled`` in ``agent-config.json`` switches it on (restart to apply);
    a non-empty ``STIRA_ASSOCIATIVE_RECALL`` overrides ``enabled`` (``1`` = on with the
    configured limits, any other value = off). An unreadable config counts as off. It reads Neural State under the engine's lock and
    never writes it; the stability band is the breaker's warning / critical thresholds and
    the self-protective terms come from the engine's Identity Anchors (read only).
    ``associative_recall.emotion_proximity`` (or ``STIRA_RECALL_EMOTION_PROXIMITY``) adds the
    emotion-proximity signal (#364, ADR 0017): it is given the breaker level
    (``breaker_status()``, which never advances turns) and the number of the turn being
    answered; the snapshots it reads are the protected records, read by the recall itself.
    """
    try:
        _gateway, agent_config = config_store.load()
        settings = recall_settings(agent_config.associative_recall)
        if not settings.enabled:
            return None

        def state_view():
            with engine.state_lock:
                return {dim: getattr(engine.state, dim) for dim in DIMS}

        band = (engine.config.stability_warning, engine.config.stability_critical)
        gates = {}
        if settings.emotion_proximity:

            def turn_number():
                # The number post_turn will give this turn (the episode's snapshot "turn").
                with engine.state_lock:
                    return int(getattr(engine, "_turn_count", 0)) + 1

            gates = {
                "breaker_level": lambda: engine.breaker_status().level,
                "turn_number": turn_number,
            }
        return AssociativeRecall(
            store,
            state_view,
            settings,
            ignore_terms=ignore_terms,
            identity_anchors=tuple(engine.config.identity_anchors),
            guard_band=band,
            **gates,
        )
    except Exception as error:  # noqa: BLE001 - optional; serve without it
        logging.getLogger(__name__).warning(
            "Associative recall off: %s (%s)", "config unreadable", type(error).__name__
        )
        return None


def _memory_autonomy_view(settings, store, job_status, executor):
    if not settings.enabled:
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return {
        **settings.status(),
        "ratings": rating_counts(store, day),
        "jobs": job_status.status() if executor is not None else None,
        "plans": plan_counts(executor.plans_dir) if executor is not None else None,
    }


def _conversation(agent):
    """The conversation in the agent's context (for the turn's trust, ADR 0018 §1e)."""
    inner = getattr(agent, "agent", None)
    messages = getattr(inner, "messages", None)
    return messages if isinstance(messages, list) else None


def _model_ref(agent) -> str | None:
    """``provider:model_id`` of the chat model, for a rating's provenance (no secrets)."""
    model = getattr(getattr(agent, "agent", None), "model", None)
    try:
        config = model.get_config() if hasattr(model, "get_config") else {}
    except Exception:  # noqa: BLE001 - provenance is best effort
        return None
    model_id = (config or {}).get("model_id")
    if not isinstance(model_id, str) or not model_id:
        return None
    return f"{getattr(agent, 'provider', None) or 'unknown'}:{model_id}"


def _record_task_model(turn_recorder, audit, record) -> None:
    """A side-task model call (docs/task-models.md): one turn event and one audit row."""
    turn_recorder.emit("task_model.call", record)
    if audit.get("record") is not None:
        try:
            audit["record"](**record)
        except Exception as error:  # noqa: BLE001 - the audit never breaks the task
            # The type name only, never the error's text or chain.
            print(f"WARNING: task model audit row not written ({type(error).__name__})")


def _finish_recall(recall, turn_recorder) -> None:
    """After a turn: record why memories were used/dropped (memory.injected), then clear."""
    if recall is None:
        return
    result = recall.take_result()  # read and clear under one lock
    if result is None:
        return
    try:
        turn_recorder.emit("memory.injected", result.as_event_data())
    except Exception as error:  # noqa: BLE001 - telemetry never fails a turn
        logging.getLogger(__name__).warning(
            "memory.injected not recorded (%s)", type(error).__name__
        )


def _tool_availability_provider(config_store, applied_tool_config, *, memory_rate_bound: bool):
    """The agent's Tool Availability section (#508), rebuilt per turn from what is saved.

    A pure function of the rules this turn applies (``applied_tool_config``, else the file),
    the start-up switches and the YOLO window's fixed facts: the same bytes until a person
    saves the rules or a window opens or ends (#254). The rows are the ``/api/config/tools``
    rows, so the section names exactly the Never tools the settings page shows. Never raises.
    """

    def build() -> str:
        try:
            tool_config = applied_tool_config.get("config")
            if tool_config is None:
                with config_store.lock:
                    tool_config = config_store.load_tools()
        except Exception:  # noqa: BLE001 - unreadable rules lock (every tool Never, #190)
            tool_config = ToolConfig.locked(config_store.tool_path, "unreadable")
        try:
            # The rule and the exec exposure do not depend on the breaker level.
            rows, _level = config_tool_rows(tool_config, "normal")
            registered = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
            absent: dict[str, str] = {}
            if "page_check" not in registered:
                code = tool_registry.PAGE_CHECK_STATUS.code
                absent["page_check"] = (
                    tool_availability.DEPENDENCY_MISSING
                    if code == "page_check_no_browser"
                    else tool_availability.UNSUPPORTED_HOST
                )
            if "run_checks" not in registered:
                absent["run_checks"] = (
                    tool_availability.NOT_CONFIGURED
                    if tool_registry.RUN_CHECKS_STATE.get("code") == "run_checks_off"
                    else tool_availability.UNSUPPORTED_HOST
                )
            unbound = {}
            if not memory_rate_bound and "memory_rate" in registered:
                unbound["memory_rate"] = tool_availability.NOT_CONFIGURED
            # #583: web_search without STIRA_WEB_SEARCH_ENDPOINT is not offered.
            unbound.update(
                {
                    name: tool_availability.NOT_CONFIGURED
                    for name in tool_registry.unconfigured_tools()
                }
            )
            known = {
                **tool_registry.READ_TOOLS,
                **tool_registry.WRITE_TOOLS,
                "page_check": page_check_tool.page_check,
                "run_checks": run_checks_tool.run_checks,
            }
            return tool_availability.section(
                rows,
                tools=known,
                absent=absent,
                unbound=unbound,
                yolo_allowed=yolo_runtime.enabled(),
                yolo_window=yolo_runtime.window_facts(),
            )
        except Exception as error:  # noqa: BLE001 - a missing section never fails a turn
            logging.getLogger(__name__).warning(
                "Tool Availability section not built (%s)", type(error).__name__
            )
            return ""

    return build


# The assembled system prompt's budget (ContextAssembler ``max_tokens``) without a profile.
PROMPT_MAX_TOKENS = 4000


def _wire_system_prompt(
    agent: NXPAgent,
    store,
    soul_path: Path,
    max_tokens: int | None = None,
    associative_recall=None,
    tools_section=None,
    memory_map=None,
    profile: str = "default",
    handoff_note=None,
):
    """Rebuild the chat server's system prompt every turn with a live Breaker section (#78).

    The breaker provider reads engine.breaker_status(), which never advances
    turns/cooldown and, inside a guarded turn, returns pre_turn's cached
    snapshot. NXPAgent calls system_prompt_provider after pre_turn, so the
    Breaker section always matches the restrictions applied to that turn's tools.
    The static Identity section is dropped: NXPAgent already prepends the live
    identity notice on INJECT_IDENTITY, and a startup-frozen copy would both
    duplicate it and go stale.
    """
    engine = agent.engine
    # #368: the profile sets the default budgets; an explicit max_tokens wins. "default"
    # sets none, so the assembler is built exactly as before.
    budget = {"max_tokens": PROMPT_MAX_TOKENS, **assembler_budget(profile, max_tokens=max_tokens)}
    assembler = ContextAssembler(
        agent_id="stira",
        memory_store=store,
        engine=engine,
        soul_path=soul_path,
        **budget,
        breaker_state_provider=engine.breaker_status,
        include_identity=False,
        associative_recall=associative_recall,
        memory_map=memory_map,
        handoff_note=handoff_note,
        context_window_tokens=lambda: _known_context_window(agent),
    )
    agent.system_prompt_provider = assembler.build_system_prompt

    def send_boundary_check(turn_tools, identity_notice: str = "") -> None:
        # B1: SOUL + the Identity Anchors notice NXPAgent prepends (this assembler leaves
        # identity out) + this turn's tool definitions (after any filtering).
        assembler.check_protected_fits(
            _known_context_window(agent), tool_definition_tokens(turn_tools), identity_notice
        )

    agent.send_boundary_check = send_boundary_check
    # Static/volatile split, used only while prompt caching is on (#254).
    agent.prompt_parts_provider = assembler.build_prompt_parts
    if tools_section is not None:
        # #508: after the assembled prompt; in the cached split, at the end of the static
        # part (it changes only on a rules save or a YOLO window's start/end).
        def system_prompt() -> str:
            return tool_availability.with_section(assembler.build_system_prompt(), tools_section())

        def prompt_parts():
            parts = assembler.build_prompt_parts()
            return replace(
                parts, static=tool_availability.with_section(parts.static, tools_section())
            )

        agent.system_prompt_provider = system_prompt
        agent.prompt_parts_provider = prompt_parts
    # Startup snapshot only, for readers of agent.system_prompt; turns use the provider.
    try:
        agent.system_prompt = agent.system_prompt_provider()
    except ProtectedContextOverflow as error:
        # Each turn fails with context_window_too_small; the server itself still starts.
        logging.getLogger(__name__).warning("%s", error)
    return assembler


def _memory_map(config_store, store) -> MemoryMap | None:
    """The Memory Map section (wave 23): on by default, ``memory_map: false`` in
    ``gateway-config.json`` or ``STIRA_MEMORY_MAP=0`` turns it off (read at start-up).
    Off = None: the prompt is byte-identical to before. An unreadable config = defaults."""
    try:
        gateway_config = config_store.load()[0]
    except (OSError, ValueError, TypeError):
        gateway_config = GATEWAY_CONFIG
    if not settings_hub.effective_memory_map(gateway_config):
        return None
    return MemoryMap(store, max_tokens=gateway_config.memory_map_max_tokens)


def _known_context_window(agent) -> int | None:
    """The window ContextAssembler guards (ADR 0026 B1), or None when only the default is known.

    Same resolution as context management and the gauge; the 200k fallback is a guess,
    so it is not used to fail a turn.
    """
    try:
        tokens, source = resolve_context_window_source(
            agent.context_management, agent.model, agent.capabilities
        )
    except Exception:  # noqa: BLE001 - unknown window: the over-budget warning only
        return None
    return None if source == "default" else tokens


def _prompt_cache_settings(gateway_config: GatewayConfig) -> PromptCacheSettings:
    """Opt-in (#254): STIRA_PROMPT_CACHE=1/0 and STIRA_PROMPT_CACHE_TTL override the config."""
    # One rule with the settings page, which shows these switches as pinned (#470).
    enabled, ttl = settings_hub.effective_prompt_cache(gateway_config)
    return PromptCacheSettings(enabled=enabled, ttl=ttl)


def _context_management_settings(gateway_config: GatewayConfig) -> ContextManagementSettings:
    """Opt-in (#258): STIRA_CONTEXT_MANAGEMENT=1/0 overrides the configured switch."""
    enabled = settings_hub.effective_context_enabled(gateway_config)
    return ContextManagementSettings.from_options(
        enabled, gateway_config.context_management_options
    )


def _acquire_data_dir_lock(
    data_dir: Path, *, allow_same_process: bool = False
) -> DataDirOwnerLock | None:
    """Own ``data_dir`` for this server or raise :class:`DataDirLockError` (#285).

    The lock is never released while the process lives: the kernel releases it at
    exit, after every thread that could still write here has gone.
    ``allow_same_process`` is for tests only (``create_app`` called again in one
    process for the same directory): it tolerates a lock this very process holds
    and returns ``None``; another process is refused either way.
    """
    lock = DataDirOwnerLock(data_dir)
    try:
        lock.acquire()
    except DataDirLockError as error:
        if allow_same_process and error.same_process:
            return None
        raise
    return lock


def create_app(
    config_store: ConfigStore | None = None,
    *,
    lifecycle: ServerLifecycle | None = None,
    _tests_share_data_dir: bool = False,
) -> Flask:
    """Build the chat server app; refuses a data directory another server owns (#285).

    Every resource is registered on ``lifecycle`` as it is created (a new one when
    omitted; ``app.extensions["lifecycle"]``). If building fails — an error, or
    ``SystemExit`` from SIGTERM in the middle of start-up — what exists is stopped
    and closed, and the owner lock released when that finished cleanly; then the
    exception propagates. ``_tests_share_data_dir`` exists for tests that build a
    second app on the same directory in one process. The server entry point never
    sets it, and it never lets a second *process* in.
    """
    lifecycle = lifecycle if lifecycle is not None else ServerLifecycle()
    try:
        app = _build_app(config_store, lifecycle, _tests_share_data_dir)
    except BaseException:
        lifecycle.abort()
        raise
    # One idempotent stop for both paths: the entry point's and the interpreter's.
    atexit.register(lifecycle.shutdown)
    return app


def _build_app(
    config_store: ConfigStore | None, lifecycle: ServerLifecycle, tests_share_data_dir: bool
) -> Flask:
    if _DATA_DIR_ERROR is not None:
        # Before anything is created: a relative/too broad value never becomes a directory.
        raise _DATA_DIR_ERROR
    config_store = config_store or _default_config_store()
    # ADR 0015 Addendum rev 4 (#449): page_check reads the localhost ports a person listed
    # from tool-config.json at every call (locked rules: none).
    page_check_tool.set_local_ports_provider(
        lambda: (
            ()
            if (tools := config_store.load_tools()).integrity_error
            else tools.page_check_ports or ()
        )
    )
    # ADR 0012 §1: read once; an invalid variable refuses start-up (YoloSettingError).
    # Phase 7c: over the config's ``yolo`` section (a variable wins per value).
    yolo_settings = yolo_runtime.settings(config=_yolo_config_section(config_store.gateway_path))
    # ADR 0011 W4: read once; with stage C the tool-call tracker is registered with or
    # without YOLO (every grant operation step is refused while an agent tool call runs).
    autonomy_settings = _autonomy_settings(config_store.gateway_path)
    track_tool_calls = yolo_settings.enabled or autonomy_settings.stage_c
    notice = _yolo_config_notice(yolo_settings)
    if notice is not None:
        print(notice)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if not DATA_DIR_FROM_ENV:
            raise
        # The operator's own value: exit 2 with the variable's name, never the path.
        raise DataDirSettingError(
            f"STIRA_DATA_DIR cannot be created ({type(error).__name__})"
        ) from None
    register_data_root(DATA_DIR)  # an opt-in rule anchor inside it locks (#273)
    # ADR 0019 phases 0-1 (#167): run_checks is registered only when gateway-config.json
    # says run_checks: {"phase": 1} — before the tool rules and the breaker see the names.
    run_checks_code = tool_registry.enable_run_checks(
        _run_checks_settings(config_store.gateway_path)
    )
    if run_checks_code == "run_checks_on":
        host = run_checks_tool.containment()
        print(f"   run_checks: on ({host.mode}: {host.detail})")
    elif run_checks_code != "run_checks_off":
        print(f"   run_checks: not registered ({run_checks_code})")
    config_location = _config_location(config_store, DATA_DIR)
    if not config_location["matches_data_dir"]:
        # No path in the log (#291): the working directory is the usual cause.
        print(
            "WARNING: the configuration files are not in the data directory: the server "
            "reads settings and tool rules relative to the working directory. Start it "
            "from the repository root (scripts/stira_ctl.sh does), or set STIRA_DATA_DIR "
            "(#272)."
        )
    # One server per data directory (#285), before anything in it is opened: a second
    # server would serve the first one's live approvals as restored. Fail-closed.
    owner_lock = _acquire_data_dir_lock(DATA_DIR, allow_same_process=tests_share_data_dir)
    lifecycle.own_lock(owner_lock)
    # ADR 0011 §5 (W1, #296): the one authority service of this process — the persisted
    # epoch (stored + 1 at start), the kill flag and the in-flight set. It exists with or
    # without YOLO; it starts no work. Unusable epoch file: unhealthy (permits refused).
    authority_service = authority.install(DATA_DIR)
    if authority_service.unhealthy_reason is not None:
        print(f"WARNING: authority epoch store unhealthy ({authority_service.unhealthy_reason})")
    # W2 (#296): the lifecycle recorder (hash-chained log + outbox). A failing record
    # latches the service unhealthy; the watchdog (below) writes what is queued.
    authority_recorder = LifecycleRecorder(DATA_DIR, on_failure=authority_service.report_unhealthy)
    authority_service.recorder = authority_recorder
    if authority_recorder.unhealthy_reason is not None:
        authority_service.report_unhealthy(authority_recorder.unhealthy_reason)
        print(f"WARNING: authority lifecycle log unhealthy ({authority_recorder.unhealthy_reason})")
    if authority_service.killed:
        print("WARNING: the authority kill latch is set (stira authority unlatch kill)")
    (DATA_DIR / "memory").mkdir(exist_ok=True)

    state = NeuralState()
    persistence = StatePersistence(state, DATA_DIR / "neural-state.json")
    persistence.load()

    # The same files the settings API edits (not the cwd defaults), #190.
    identity_config = AgentConfig.load(
        config_store.agent_path, tool_config_path=config_store.tool_path
    )
    anchors_configured = _anchors_configured(config_store.agent_path)
    if not anchors_configured:
        # Once per start, no path: an existing install without the key changed anchors.
        print(ANCHORS_DEFAULT_WARNING)
    soul_path = Path(identity_config.soul_path) if identity_config.soul_path else SOUL_PATH
    read_only_paths = {
        str(Path(path).expanduser().resolve()) for path in identity_config.read_only_paths
    }
    read_only_paths.add(str(soul_path.resolve()))
    # The tool rules, their head/lock marker, the settings history, the configs and the
    # approval state are never writable by agent tools, approved or not (#190).
    read_only_paths |= _policy_files(config_store, DATA_DIR)
    store = MemoryStore(DATA_DIR / "memory", "stira", read_only_paths=read_only_paths)
    # #463: the agent's propose_procedure stages procedural/staging/<key> only; the turn
    # id is the server's (bound below, once the turn recorder exists).
    turn_source: dict = {}
    procedure_proposals = ProcedureProposals(store, review_log=DATA_DIR / REVIEW_LOG_NAME)
    propose_tool = create_propose_procedure_tool(
        procedure_proposals,
        lambda: turn_source["recorder"].active.current() if "recorder" in turn_source else None,
    )
    bound_memory_tools = (*create_memory_tools(store), propose_tool)
    # Handoff notes (wave 23, docs/handoff.md): on by default; STIRA_HANDOFF_INJECT=0 or
    # gateway handoff_inject false = no tools and no section. An unreadable file = the
    # values the server started with.
    try:
        handoff = handoff_settings(config_store.load()[0])
    except (OSError, ValueError, TypeError):
        handoff = handoff_settings(GATEWAY_CONFIG)
    handoff_note = HandoffNote(store, handoff) if handoff.inject else None
    if handoff_note is not None:
        bound_memory_tools = (*bound_memory_tools, *create_handoff_tools(store, handoff))
    # ADR 0018 phase 0 (observe only): memory_autonomy absent = off — no memory_rate tool,
    # no memory job kind, nothing bound. The rating waits for the turn's episode.
    memory_settings = memory_autonomy_settings(identity_config.memory_autonomy)
    # ADR 0023 phase 0 (#460-0, observe only): containment absent = off — no hook, no read
    # tee, no rows. On, it counts the gate's refusals and holds and reports the stage the
    # session would be in; nothing is enforced (no tool removed, no card, no level moved).
    containment_settings = containment_settings_from(identity_config.containment)
    containment_observer = (
        ContainmentObserver(window_seconds=containment_settings.window_seconds)
        if containment_settings.enabled
        else None
    )
    # ADR 0022 phase 0 (#458, observe only): taint absent = off — no hook, no rows. On, it
    # labels the context per turn and counts what a flow layer would hold; nothing is
    # enforced and no prompt, tool result or gate decision changes.
    taint_settings = taint_settings_from(identity_config.taint)
    taint_observer = TaintObserver() if taint_settings.enabled else None
    memory_hooks: dict = {}
    rating_session = None
    if memory_settings.rating_enabled:
        rating_session = RatingSession(
            store,
            turn_id=lambda: (
                turn_source["recorder"].active.current() if "recorder" in turn_source else None
            ),
            breaker_level=lambda: (
                memory_hooks["breaker"]() if "breaker" in memory_hooks else "unknown"
            ),
            started_by_person=yolo_origin.started_by_person,
            model_ref=lambda: memory_hooks["model_ref"]() if "model_ref" in memory_hooks else None,
        )
        bound_memory_tools = (*bound_memory_tools, create_memory_rate_tool(rating_session))
    # Agent-side file tools may not write the procedural layer (#144); approved
    # host-side MemoryStore writes are unaffected.
    tool_paths = {
        "read_only_paths": tuple(sorted(read_only_paths)),
        "protected_dirs": (str(store.memory_dir / MemoryLayer.PROCEDURAL.value),),
    }
    # Write roots (#243): "@workspace" = workspace_dir if configured, else the
    # data directory's own workspace/ subdirectory (#271). Outside every root is
    # denied (fail-closed). The whole data directory is on the floor; only the
    # fixed names workspace/, the episodic layer and context-stash/ stay writable,
    # each only if it is a real directory (a symlink is never followed into an
    # exception, also one that was there before start-up).
    context_stash_dir = DATA_DIR / "context-stash"
    for directory in (DATA_DIR / "workspace", context_stash_dir):
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            # A dangling symlink, a read-only file system, a permission error: the
            # check below sees what is really there and disables the root with a
            # warning (never a crash at start-up, never a resolved symlink target).
            pass
    tool_paths["write_policy"], data_dir_floor = data_dir_floor_policy(
        identity_config,
        DATA_DIR,
        read_only_paths=tool_paths["read_only_paths"],
        protected_dirs=tool_paths["protected_dirs"],
        names=writable_names(store.agent_id),
    )
    for problem in data_dir_floor.problems:
        print(f"WARNING: not writable for the agent (stays on the floor, #271): {problem}")
    if not data_dir_floor.workspace_enabled:
        print(
            "WARNING: @workspace is disabled (the default workspace failed its check); "
            "set workspace_dir in agent-config.json to choose one (#271)"
        )
    # Write-failure diagnosis + permission cards, and the start-up doctor (#244).
    # Read-only checks (one temporary file per root); TCC paths are only warned about.
    write_failures = WriteFailureBoard()
    doctor_state = {"report": None}

    def run_write_doctor():
        try:
            doctor_state["report"] = run_doctor(
                # The roots the policy enforces (#271), not only the configured ones.
                data_dir_floor.roots,
                workspace=tool_paths["write_policy"].workspace,
                policy=tool_paths["write_policy"],
                classifier_choice=_doctor_classifier_choice(config_store),
                run_checks_code=tool_registry.RUN_CHECKS_STATE["code"],
            )
        except Exception as error:  # noqa: BLE001 - the doctor must never stop the server
            doctor_state["report"] = {
                "version": 1,
                "ok": False,
                "error": type(error).__name__,
                "roots": [],
                "counts": {"ok": 0, "warning": 0, "error": 0},
            }
        return doctor_state["report"]

    run_write_doctor()
    bound_memory_write_names = {
        name
        for tool in bound_memory_tools
        if (name := _registered_tool_name(tool))
        in ("memory_write", PROPOSE_PROCEDURE_TOOL_NAME, HANDOFF_WRITE_TOOL_NAME)
    }
    if "memory_write" not in bound_memory_write_names:
        raise RuntimeError("chat server memory tools did not register memory_write.")

    provider_config = _provider_config()
    agent_model_kwargs = model_kwargs(
        provider_config.provider,
        MODEL_ID,
        region=REGION,
        base_url=provider_config.base_url,
        api_key_env=provider_config.api_key_env,
        organization_env=provider_config.organization_env,
    )
    # Per-task model routing (docs/task-models.md): checked before anything starts; a bad
    # route (unknown task / model, missing SDK or credentials) refuses the start.
    main_model_spec = model_spec_from(
        provider_config.provider,
        MODEL_ID,
        region=REGION,
        base_url=provider_config.base_url,
        api_key_env=provider_config.api_key_env,
        organization_env=provider_config.organization_env,
    )
    task_routes = task_models.resolve_routes(
        GATEWAY_CONFIG.task_models, main_model_spec, region=REGION
    )
    # The anchors saved in agent-config.json win over the neutral built-in ones (#152);
    # before, the server always used the built-in list whatever the file said.
    # The overrides in agent-config.json reach the engine only as stira_delivery says (#272):
    # everything but critical_action / blocked_tool_names at start-up; the thresholds and
    # their exits are re-applied each turn (sync_runtime_settings).
    config = StiraConfig(
        critical_action=SERVER_CHOSEN["critical_action"],
        identity_anchors=list(identity_config.identity_anchors),
        **startup_values(identity_config.stira_config_overrides),
    )
    ignored = never_applied(identity_config.stira_config_overrides)
    if ignored:
        # Field names only, no values, no path.
        print(
            "WARNING: stira_config_overrides holds fields the chat server does not apply "
            f"(saved and shown only, #272): {', '.join(ignored)}"
        )
    turn_store = TurnEventStore(DATA_DIR / "turns.db")
    lifecycle.add("turns", lambda _t: _close_label(turn_store), order=90, abort_only=True)
    # #452: a turn the previous process left without an end (a crash, a kill) is recorded
    # as interrupted, not left looking like it still runs. Nothing runs yet (owner lock).
    interrupted = turn_store.close_unfinished("server_restarted")
    if interrupted:
        logging.getLogger(__name__).warning(
            "%d turn(s) from before the restart recorded as interrupted", len(interrupted)
        )
    turn_recorder = TurnEventRecorder(turn_store)
    turn_source["recorder"] = turn_recorder
    # #454 (ADR 0020): what file_write / editor wrote into the workspace, as versions. The
    # store is on the data-directory floor (agent tools neither read nor write it).
    artifacts = _workspace_artifacts(DATA_DIR / "artifacts")
    if artifacts is not None:
        lifecycle.add("artifacts", lambda _t: _close_label(artifacts), order=90, abort_only=True)
    artifact_hooks = (
        [
            ArtifactHook(
                artifacts,
                policy_provider=lambda: tool_paths["write_policy"],
                turn_id_provider=turn_recorder.active.current,
            )
        ]
        if artifacts is not None
        else []
    )
    # Approvals are bound to agent/tool/args-hash/expiry and remember their origin turn (#146).
    # Durable + hash-chained (#240, ADR 0006): a pending approval survives a restart and
    # approving it resumes the paused tool batch. Unreadable/tampered store = reject all.
    approval_store = ApprovalStore(DATA_DIR / "hitl-approvals.db")
    # Closed only when start-up fails: at a normal stop a woken worker may still use it.
    lifecycle.add(
        "approval_store", lambda _t: _close_label(approval_store), order=90, abort_only=True
    )
    resume_scheduler = {"schedule": None}
    new_session_lock = threading.RLock()
    hitl_broker = HITLApprovalBroker(
        on_event=turn_recorder.emit,
        turn_id_provider=turn_recorder.active.current,
        store=approval_store,
        checkpoint_id_provider=lambda: agent.engine.last_checkpoint_id if agent else None,
        on_resume=lambda request_id: resume_scheduler["schedule"](request_id),
        on_reject=lambda request_id: (resume_scheduler.get("rejected") or (lambda _r: None))(
            request_id
        ),
        # Shared with the conversation reset (#350): a decision and a reset never interleave.
        # Bounded: a decision that cannot get it in time is "busy" (503, nothing recorded).
        decision_guard=lambda: timed_lock(new_session_lock, DECISION_LOCK_TIMEOUT_SEC),
        # ADR 0024 §3: approve / edit wait while the breaker is stricter than at request
        # time (reject always works; the expiry is not extended). No agent yet: unknown,
        # which counts as critical (fail-closed).
        breaker_level=lambda: agent.engine.breaker_status().level,
        # ADR 0024 §5: a restored card counts the other calls of its batch that a resume
        # answers from the operation ledger (counts only).
        replay_preview=lambda record: replay_preview(agent, record),
    )
    lifecycle.add("approvals", lambda t: "closed" if hitl_broker.close(t) else "closing", order=10)
    # Durable ledger: a restored checkpoint never repeats an approved side effect.
    operation_ledger = OperationLedger(DATA_DIR / "hitl-operations.db")
    lifecycle.add("ledger", lambda _t: _close_label(operation_ledger), order=90, abort_only=True)
    # Register every loaded write-capable name with the existing breaker.
    config.blocked_tool_names = (
        set(config.blocked_tool_names)
        | set(tool_registry.WRITE_TOOLS)
        | bound_memory_write_names
        # ``delegate`` (#486): a child may write, so the breaker treats it as write class.
        | {DELEGATE_TOOL_NAME}
    )
    # ADR 0016 phase 1 (#486): Stira's gated child agent. On by default (gateway
    # ``delegation.enabled``); each call is Ask (a person's card), YOLO never answers it.
    # The child's tool rows reach this turn's UI as names only (no arguments or results).
    delegate_tool = DelegateTool(
        settings=lambda: _delegation_settings(config_store),
        child_hooks=lambda label: [
            create_dashboard_hooks(ChildToolSink(UIEventSink(turn_recorder), label)),
            *artifact_hooks,
            *([yolo_runtime.TRACKER] if track_tool_calls else []),
        ],
        window_probe=yolo_runtime.current_window_id if yolo_settings.enabled else None,
    )

    # Display metadata belongs here, never on agent.model.
    _status = {
        "agent_id": "stira",
        "model_id": MODEL_ID,
        "provider": provider_config.provider,
        # Readiness only ("configured" / env NAME); never a secret value (#205).
        "credentials": credential_status(
            provider_config.provider,
            api_key_env=provider_config.api_key_env,
            base_url=provider_config.base_url,
        ),
        "neural_state": {},
        "breaker": {"level": "unknown", "last_changed": None},
        "bc_score": None,
        "bc_measurement": bc_measurement(0),
        # #291: the real read state and its source, never the path (it names users/dirs).
        "soul": _soul_status(soul_path, configured=bool(identity_config.soul_path)),
        "memory": {},
    }

    agent: NXPAgent | None = None
    # The tool switches applied by the last sync (#227); a breaker transition rebuilds
    # the tool list mid-turn and must not bring back a tool set to Never (#190).
    applied_tool_config = {"config": None}

    def on_breaker(*args, **kwargs):
        # Accept level-bearing callback payloads without assuming that the
        # callback fires on every turn. Do not invoke pre_turn to read status.
        for payload in (*args, kwargs, *kwargs.values()):
            level = (
                payload.get("level")
                if isinstance(payload, dict)
                else getattr(payload, "level", None)
            )
            if level is None:
                continue
            # Apply transitions during pre_turn too, restoring tools immediately
            # on recovery instead of retaining the previous turn's restrictions.
            if agent is not None:
                rebuilt = _stira_tools(
                    bound_memory_tools,
                    bound_memory_write_names,
                    level,
                    hitl_enabled=True,
                    delegate_tool=delegate_tool,
                    **tool_paths,
                )
                if applied_tool_config["config"] is not None:
                    rebuilt = filter_tools_by_config(
                        rebuilt, applied_tool_config["config"], _registered_tool_name
                    )
                agent.tools = tuple(rebuilt)
            level = getattr(level, "value", level)
            if level != _status["breaker"]["level"]:
                _status["breaker"] = {
                    "level": level,
                    "last_changed": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            if agent is not None:
                _status["breaker"].update(_breaker_reason_fields(agent.engine, str(level)))
            break

    # The settings hub's classifier choice applies at start-up (#227). "auto"
    # (the default) keeps the agent's own pick: Sokudan when installed, else rules.
    try:
        startup_gateway, _startup_agent = config_store.load()
        runtime_classifier_choice = classifier_choice(startup_gateway)
        chosen_classifier = startup_classifier(startup_gateway)
    except (OSError, ValueError, TypeError):
        runtime_classifier_choice, chosen_classifier = "auto", None
    if runtime_classifier_choice != "rule" and not _sokudan_installed():
        # #367: say it once at start-up; the status and the chat page say it too.
        print(
            "WARNING: emotion classification is rule-only: sokudan is not installed "
            '(pip install -e ".[jev]" on Python 3.11). See docs/neural-saturation.md (#367).'
        )
    tool_policy_reader = _tool_policy_reader(
        config_store,
        approval_store,
        DATA_DIR / "tool-policy-approvals.json",
        on_reconciled=hitl_broker.resume_approvals,
        delegation_enabled=delegate_tool.offered,
    )
    try:
        # ADR 0015 phase 1: a saved Always for page_check is read as Ask; say so once.
        tool_policy.warn_saved_always(config_store.load_tools())
    except (OSError, ValueError, TypeError):
        pass  # unreadable rules: the gate fails closed per call
    try:
        # Rules saved while the server was down void the approvals still open (#190).
        tool_policy_reader.sync()
    except Exception:  # noqa: BLE001 - the gate re-tries and fails closed per call
        print("WARNING: could not reconcile open approvals with the tool rules")
    agent = NXPAgent(
        **agent_model_kwargs,
        state=state,
        config=config,
        stimulus_classifier=chosen_classifier,
        on_breaker=on_breaker,
        on_hitl_request=hitl_broker.request,
        operation_ledger=operation_ledger,
        tool_policy=tool_policy_reader,
        # Next to the approval store, not relative to the working directory (#240).
        checkpoint_store=FileCheckpointStore(DATA_DIR / "checkpoints"),
        tools=_stira_tools(
            bound_memory_tools,
            bound_memory_write_names,
            delegate_tool=delegate_tool,
            **tool_paths,
        ),
        # Explicit gateway value, else the selected model's documented limit bounded at
        # 32000, else the old 8192 (#363).
        max_output_tokens=resolve_max_output_tokens(
            GATEWAY_CONFIG.max_output_tokens, provider_config.provider, MODEL_ID
        ),
        # No SDK printing handler: streamed model text would land unmasked in the
        # server's stdout log (stira.out.log); the UI gets turns through the hooks.
        callback_handler=None,
        read_only_paths=read_only_paths,
        write_policy=tool_paths["write_policy"],
        # Dedicated provider: masked/capped tool payloads reach only the turn UI (#188).
        extra_hooks=[
            create_dashboard_hooks(UIEventSink(turn_recorder), payload_capture=capture_payload),
            # Classify failed file_write/editor results; post permission cards (#244).
            WriteFailureHook(
                write_failures.add, base_provider=lambda: tool_paths["write_policy"].workspace
            ),
            *artifact_hooks,
            # Only with STIRA_YOLO_ALLOW (or stage C): running tool calls, for the admin-API
            # refusal of ADR 0012 §8b. Without them the hook list is unchanged.
            *([yolo_runtime.TRACKER] if track_tool_calls else []),
            # ADR 0023 phase 0: reads the tool events only. Off: the hook list is unchanged.
            *(
                [ContainmentObserverHook(containment_observer)]
                if containment_observer is not None
                else []
            ),
            # ADR 0022 phase 0: reads the events only. Off: the hook list is unchanged.
            *(
                [TaintObserverHook(taint_observer, yolo_origin.started_by_person)]
                if taint_observer is not None
                else []
            ),
        ],
    )

    delegate_tool.parent = agent  # bound after the agent exists (its turn agent is the caller)
    # W1 (#296): every breaker change away from NORMAL (and between non-NORMAL levels)
    # moves the authority epoch, with or without YOLO.
    agent.engine.on_breaker_transition = authority_service.breaker_transition
    # Filled once the audit chain exists (below); a call before that has no audit row.
    task_model_audit: dict = {"record": None}
    # Routed side-task calls leave one turn event each: task, model, tokens, status (no text).
    agent.task_models = task_models.TaskModelRouter(
        task_routes,
        main_model_spec,
        on_record=lambda record: _record_task_model(turn_recorder, task_model_audit, record),
    )
    # #585: web_fetch(summarize=True) runs the tool_summary task (no tools, framed result,
    # at most MAX_SUMMARIES_PER_TURN per turn).
    summarizer = tool_summary_module.ToolSummarizer(
        agent.task_models,
        lambda: agent.model,
        turn_recorder.active.current,
        # The turn store is the authority on finished turns (review r592b B2).
        is_finished=lambda turn: any(
            event.get("type") == "turn.completed" for event in turn_store.events(turn)
        ),
    )
    tool_summary_module.set_summarizer(summarizer)
    # A finished turn's counter goes; a late call from it is refused (review r592 #2).
    turn_recorder.subscribe(
        lambda event: (
            summarizer.finish(event.get("turn_id"))
            if event.get("type") == "turn.completed"
            else None
        )
    )
    for route in task_routes.values():
        if not route.inherit:
            print(f"   Task model: {route.task} -> {route.spec.ref()} ({route.source})")
    if tool_registry.RUN_CHECKS_STATE["registered"]:
        # ADR 0019 §5: the plan (argv, folder, containment, limits) goes into the call's
        # input before the gate, so the card shows it and the approval binds it.
        agent.checkpoint_hooks.input_preparers["run_checks"] = lambda tool_input: (
            run_checks_tool.prepare_input(tool_input, tool_paths["write_policy"])
        )
        run_checks_tool.set_turn_provider(turn_recorder.active.current)

    # Opt-in associative recall (#170); None (the default) leaves the prompt unchanged.
    associative_recall = _associative_recall(
        config_store, store, agent.engine, ignore_terms=(USER_LABEL, AGENT_LABEL)
    )
    if associative_recall is not None:
        print(
            "   Associative recall: on "
            f"(max {associative_recall.settings.max_items} memories, "
            f"{associative_recall.settings.max_chars} chars"
            f"{', emotion proximity on' if associative_recall.emotion_proximity else ''})"
        )
    # Use the wrapper's engine for context assembly as well; rebuilt per turn (#78).
    availability_section = _tool_availability_provider(
        config_store, applied_tool_config, memory_rate_bound=rating_session is not None
    )
    # ADR 0014 phase 3 (#451): the progress tracker (wired below with the YOLO allowance)
    # adds its fixed guidance after the Tool Availability section, at the end of the static
    # part: present only with ``declared`` on and while a window with reports on is open.
    static_progress: dict = {}

    def tools_section() -> str:
        tracker = static_progress.get("tracker")
        try:
            guidance = tracker.guidance() if tracker is not None else ""
        except Exception:  # noqa: BLE001 - a missing paragraph never fails a turn
            guidance = ""
        return tool_availability.with_section(availability_section(), guidance)

    memory_map = _memory_map(config_store, store)
    if memory_map is not None:
        print(f"   Memory Map: on (max {memory_map.max_tokens} tokens)")
    # #368: read at start-up (a change applies at the next start). "default" changes nothing.
    try:
        profile_gateway = config_store.load()[0]
    except (OSError, ValueError, TypeError):
        profile_gateway = GATEWAY_CONFIG
    context_profile_facts = settings_hub.effective_context_profile(profile_gateway)
    agent.send_allowed_tools_only = context_profile_facts["send_allowed_tools_only"]
    # Process-wide, before the first turn: the registry's long upstream schemas are sent
    # with shorter property descriptions (names, types and required unchanged).
    compact_specs.set_short_schemas(context_profile_facts["short_tool_schemas"])
    if (
        context_profile_facts["name"] != "default"
        or agent.send_allowed_tools_only
        or context_profile_facts["short_tool_schemas"]
    ):
        print(
            f"   Context profile: {context_profile_facts['name']}"
            f"{' (only usable tool definitions sent)' if agent.send_allowed_tools_only else ''}"
            f"{' (short tool schemas)' if context_profile_facts['short_tool_schemas'] else ''}"
        )
    _wire_system_prompt(
        agent,
        store,
        soul_path,
        associative_recall=associative_recall,
        tools_section=tools_section,
        memory_map=memory_map,
        profile=context_profile_facts["name"],
        handoff_note=handoff_note,
    )
    # #463: an approval from the review screen re-reads the static prompt (SOUL, anchors,
    # Procedural Rules) under the turn lock, so the rule applies from the next turn's start.
    procedure_proposals.reflect = lambda: _wire_system_prompt(
        agent,
        store,
        soul_path,
        associative_recall=associative_recall,
        tools_section=tools_section,
        memory_map=memory_map,
        profile=context_profile_facts["name"],
        handoff_note=handoff_note,
    )
    # Approving is a loosening: only at a NORMAL breaker (operator decision 2026-10-20).
    # The level YOLO samples (the engine's, made stricter by the live stability).
    procedure_proposals.breaker_level = lambda: _yolo_breaker_level(agent.engine)
    memory_hooks["breaker"] = lambda: _yolo_breaker_level(agent.engine)
    memory_hooks["model_ref"] = lambda: _model_ref(agent)
    try:
        cache_gateway = config_store.load()[0]
    except (OSError, ValueError, TypeError):
        cache_gateway = GATEWAY_CONFIG
    agent.prompt_cache = _prompt_cache_settings(cache_gateway)
    # Offloaded tool results live under the data directory (a write root while
    # "@workspace" is the default; refused when outside the roots, #243/#271).
    agent.context_management = _context_management_settings(cache_gateway)
    agent.context_stash_dir = context_stash_dir

    def refresh_status():
        _status.update(
            {
                "breaker": _breaker_view(agent.engine, _status["breaker"]),
                "neural_state": {key: getattr(state, key, None) for key in DIMENSIONS},
                "bc_score": getattr(state, "bc_score", 1.0),
                "bc_measurement": bc_measurement(len(getattr(state, "bc_window", ()))),
                # Counted without reading the entries: refresh_status runs every turn
                # and every heartbeat tick (integration review, 2026-10-15).
                "memory": {
                    "episodic_count": store.count_entries(MemoryLayer.EPISODIC),
                    "semantic_count": store.count_entries(MemoryLayer.SEMANTIC),
                    "procedural_count": store.count_entries(MemoryLayer.PROCEDURAL),
                },
            }
        )

    def status_provider():
        # The gateway's public field is "model"; internal metadata uses model_id.
        # Report which stimulus classifier is live so the UI can show whether the
        # Jev (sokudan) backend is actually driving Neural State, or the rule fallback.
        classifier_info = _classifier_view(agent)
        # #367: rules running although Sokudan was asked for (auto / sokudan) = fallback.
        # null while unresolved (before the first turn under "auto").
        classifier_info["fallback"] = (
            None
            if classifier_info["mode"] == "unknown"
            else classifier_info["mode"] == "rule_only" and runtime_classifier_choice != "rule"
        )
        # #558 follow-up: the provider/model really in use (not the Bedrock default id).
        in_use = status_model(agent, _status["model_id"], _status["provider"])
        return {
            "agent_id": _status["agent_id"],
            "model": in_use["model"],
            "model_host": in_use["host"],
            "classifier": classifier_info,
            # #367: how many turns moved Neural State, and why the last one did not.
            "emotion_updates": _emotion_updates_view(agent),
            "neural_state": dict(_status["neural_state"]),
            "breaker": dict(_status["breaker"]),
            "bc_score": _status["bc_score"],
            # #337: tells "not measured yet" from "measured, no movement" (both 1.0).
            **_status["bc_measurement"],
            # The file's bc_window_size is rounded up to the minimum on load (#337). Read from
            # the file like /api/config, so a settings save (which writes the 5) clears both.
            "bc_window_clamped": bc_window_clamped_in(config_store.agent_path),
            # Same for protected_layers completed with L3-L5 on load (#272).
            "protected_layers_clamped": protected_layers_clamped_in(config_store.agent_path),
            # Same for a hitl_timeout_sec capped (24 h) or defaulted on load (#272).
            "hitl_timeout_clamped": hitl_timeout_clamped_in(config_store.agent_path),
            "soul": dict(_status["soul"]),
            "memory": dict(_status["memory"]),
            # False: the settings are read away from the data directory (#272).
            "config_location": dict(config_location),
            "provider": _status["provider"],
            "credentials": copy.deepcopy(_status["credentials"]),
            "heartbeat": heartbeat_status(),
            # The newest scheduled Harvest run (#251), counts only; null when the
            # scheduler is off (no job can exist).
            "harvest_job": harvest_job_status.status() if scheduler is not None else None,
            # ADR 0018 phase 0: counts only (no keys, no values); null when off.
            "memory_autonomy": _memory_autonomy_view(
                memory_settings, store, memory_job_status, memory_executor
            ),
            "prompt_cache": {
                "enabled": agent.prompt_cache.enabled,
                "ttl": agent.prompt_cache.ttl,
                "active": bool((agent.last_prompt_cache or {}).get("active")),
            },
            "context_management": {
                "enabled": agent.context_management.enabled,
                "mode": (agent.last_context_management or {}).get("mode"),
            },
            # Read-only window for the context gauge (#350); null when it cannot be resolved.
            "context_window": _context_window_view(agent),
            # Doctor summary + open permission cards (#244); masked, no full paths.
            "diagnostics": {
                "doctor": summarize(doctor_state["report"]),
                "write_failures": write_failures.summary(),
            },
        }

    refresh_status()
    neural_history = NeuralHistory()
    neural_history.record(state)
    history_lock = threading.RLock()
    history_state = copy.deepcopy(state)

    heartbeat_status = HEARTBEAT_DISABLED.copy

    def neural_history_provider(limit):
        with history_lock:
            payload = history_payload(
                history_state, agent.engine.config, neural_history.snapshot(), limit
            )
        # Read-only heartbeat summary next to the trajectory it feeds (#238).
        payload["heartbeat"] = heartbeat_status()
        return payload

    episodic_formatter = EpisodicFormatter(user_label=USER_LABEL, agent_label=AGENT_LABEL)

    def sync_model_selection():
        """Apply the model saved from /settings before this turn (#208)."""
        try:
            resolved = config_store.resolved()
            if apply_model_selection(
                agent, resolved.nxp_model_kwargs(), max_output_tokens=resolved.max_output_tokens
            ):
                _status["model_id"] = agent.model_id
                _status["provider"] = agent.provider
                _status["credentials"] = resolved.credential_status()
            _status["model_selection_error"] = None
            # #470: the cap in use, for the settings page's "applied" (next turn).
            runtime_settings["max_output_tokens"] = getattr(agent, "max_output_tokens", None)
        except Exception as error:  # noqa: BLE001 - a bad selection must not break the turn
            # Keep the current model; report the class name only (never a value).
            _status["model_selection_error"] = type(error).__name__

    # What the running agent has applied from the settings hub (#227); the hub
    # reports "applied" only when these equal the saved values.
    runtime_settings = {
        "tools": None,
        "breaker": None,
        # Every StiraConfig field of the running engine (#272); refreshed each turn.
        "engine": plain_values(config),
        "classifier_choice": runtime_classifier_choice,
        # Write roots this process enforces (#243); a hub change applies at restart.
        "write": {"roots": list(identity_config.write_roots)},
        "read": {"roots": list(identity_config.read_roots)},
        "workspace": (
            str(tool_paths["write_policy"].workspace) if data_dir_floor.workspace_enabled else None
        ),
        # The data directory is on the file tools' floor except these (#271).
        "data_directory": {
            # Floor exceptions that are also inside the effective roots.
            "writable": shown_writable(tool_paths["write_policy"], data_dir_floor),
            # What the read floor leaves readable in the data directory (#312).
            "readable": shown_readable(tool_paths["write_policy"], data_dir_floor),
        },
        "error": None,
        # #470: the output cap (re-resolved every turn) and the start-up cost switches.
        "max_output_tokens": getattr(agent, "max_output_tokens", None),
        "prompt_cache": {"enabled": agent.prompt_cache.enabled, "ttl": agent.prompt_cache.ttl},
        "context_management": agent.context_management.as_dict(),
        # #368: the start-up context profile (read-only on the settings page).
        "context_profile": context_profile_facts,
        # Per-task model routing, read-only on the settings page (docs/task-models.md).
        "task_models": agent.task_models.describe(),
        # #554: registry tools this server did not bind (fixed at start-up): the settings
        # page shows them as not configured, not as their saved rule. The same fact the
        # agent's Tool Availability section states.
        "unbound_tools": (
            {"memory_rate": tool_availability.NOT_CONFIGURED}
            if rating_session is None and "memory_rate" in tool_registry.READ_TOOLS
            else {}
        ),
    }
    last_tool_config = None

    def sync_runtime_settings():
        """Apply saved tool switches and breaker thresholds before this turn (#227).

        A load/validation failure keeps the last breaker values; the tool rules then
        lock (every governed tool Never, #190), like a rule that fails its check.
        """
        nonlocal last_tool_config
        try:
            tool_config = config_store.load_tools()
        except Exception as error:  # noqa: BLE001 - a bad file must not break the turn
            runtime_settings["error"] = type(error).__name__
            locked = ToolConfig.locked(config_store.tool_path, "unreadable")
            applied_tool_config["config"] = locked
            return locked
        try:
            resolved = config_store.resolved()
            thresholds = {name: getattr(resolved.stira_config, name) for name in NEXT_TURN_FIELDS}
            if resolved.breaker_pairs_reset:
                # #512: the file's set is unusable: never looser than the defaults nor than
                # what runs (fail-closed), until valid thresholds are saved.
                thresholds = reset_breaker_thresholds(agent.engine.config)
            breaker = apply_breaker_thresholds(agent.engine, thresholds)
            agent.config = agent.engine.config  # same object (blocked tools etc.)
        except Exception as error:  # noqa: BLE001 - a bad file must not break the turn
            runtime_settings["error"] = type(error).__name__
            applied_tool_config["config"] = tool_config
            return tool_config
        last_tool_config = tool_config
        applied_tool_config["config"] = tool_config
        runtime_settings.update(
            tools=tool_config.as_dict(),
            breaker=breaker,
            engine=plain_values(agent.engine.config),
            error=None,
        )
        return tool_config

    sync_runtime_settings()

    def record_heartbeat_snapshot(snapshot):
        # Trajectory sink: the bounded NeuralHistory (500 rows), not a new store.
        # Records the tick's own snapshot (same instant as its breaker result).
        # HeartbeatGateway calls the sink after releasing engine.state_lock, and
        # the two locks below are taken one after the other, never nested, so
        # there is no lock-order inversion with turns (#147).
        nonlocal history_state
        with agent.engine.state_lock:
            current = copy.deepcopy(state)
        with history_lock:
            neural_history.record_snapshot(snapshot)
            history_state = current

    # W6d: the scheduler's event-driven grant_tick wake (set only with
    # autonomy.heartbeat_start and a running scheduler; None: nothing).
    grant_tick_wake = {"wake": None}

    def after_heartbeat_tick(_result):
        # Persist last_decay_check so a restart neither re-applies nor skips decay.
        persistence.save()
        refresh_status()
        wake = grant_tick_wake["wake"]
        if wake is not None:
            wake()  # memory only: the scheduler's own thread runs the tick

    heartbeat, heartbeat_status = _start_heartbeat(
        config_store,
        agent.engine,
        DATA_DIR,
        on_tick=after_heartbeat_tick,
        snapshot_sink=record_heartbeat_snapshot,
        persistence=persistence,
        lifecycle=lifecycle,
    )
    runtime_settings["heartbeat"] = _heartbeat_runtime(heartbeat, heartbeat_status(), config_store)

    # Harvest as a human-defined scheduler job (#251): registered only with the scheduler
    # (STIRA_SCHEDULER=1); nothing runs until a human creates a memory_harvest job.
    harvest_job_status = HarvestJobStatus()

    def load_harvest_config():
        return AgentConfig.load(config_store.agent_path, tool_config_path=config_store.tool_path)

    # ADR 0018 phase 0: the fixed memory job kinds memory_autonomy enables, in their own
    # executor (plan files only). Registered only with the scheduler (STIRA_SCHEDULER=1).
    memory_job_status = MemoryJobStatus()
    memory_executor = None
    if memory_settings.enabled and memory_settings.jobs:
        memory_executor = MemoryJobExecutor(
            store,
            settings=lambda: memory_settings,
            model_factory=distiller_model_factory(
                load_harvest_config,
                region=os.environ.get("AWS_REGION") or "ap-northeast-1",
            ),
            breaker_level=lambda: agent.engine.breaker_status().level,
            owner_held=lambda: owner_lock is not None and owner_lock.held,
            status=memory_job_status,
        )
        lifecycle.add("memory_jobs", lambda _t: memory_executor.close() or "closed", order=25)
    harvest_action = memory_harvest_action(
        store,
        agent_id="stira",
        load_config=load_harvest_config,
        breaker_level=lambda: agent.engine.breaker_status().level,
        owner_held=lambda: owner_lock is not None and owner_lock.held,
        status=harvest_job_status,
        on_finish=lambda _report: refresh_status(),
    )
    agent_job_settings = _agent_job_settings(config_store)
    # ADR 0024 phase 2 / ADR 0021 S3: runs of ``wait`` jobs pause in the durable store
    # (``kind: "job"``), within this process only; only with agent_jobs on.
    job_approval_waits = (
        JobApprovalWaits(
            approval_store, hitl_timeout_sec=lambda: agent.engine.config.hitl_timeout_sec
        )
        if agent_job_settings.enabled
        else None
    )
    agent_executor = _agent_job_executor(
        agent_job_settings,
        lambda: agent,
        lambda _names: _stira_tools(bound_memory_tools, bound_memory_write_names, **tool_paths),
        approval_waits=job_approval_waits,
    )
    scheduler, scheduler_status = _start_scheduler(
        agent.engine,
        DATA_DIR,
        heartbeat_status=lambda: heartbeat_status(),
        lifecycle=lifecycle,
        extra_actions={HARVEST_ACTION: harvest_action},
        # Rule distiller only: an llm/jev setting refuses create/update/resume (#251).
        action_checks={HARVEST_ACTION: harvest_job_precondition(load_harvest_config)},
        memory_executor=memory_executor,
        memory_kinds=memory_settings.jobs if memory_executor is not None else frozenset(),
        memory_daily_token_budget=memory_settings.daily_token_budget,
        agent_executor=agent_executor,
        agent_daily_token_budget=agent_job_settings.agent_daily_token_budget,
        approval_waits=job_approval_waits if agent_executor is not None else None,
    )

    def resume_job_after_approval(request_id) -> bool:
        """A dashboard approval of a job request continues its run through the scheduler
        (ADR 0024 §6.2), never directly. True: it was a job request (handled here)."""
        try:
            record = approval_store.get(request_id)
        except Exception:  # noqa: BLE001 - unreadable: nothing resumes (fail closed)
            return True
        if record is None or (record.origin or {}).get("kind") != "job":
            return False
        try:
            if scheduler is None:
                raise SchedulerRefused("not_found", "the scheduler is not running")
            scheduler.resume_waiting_run(request_id, by=record.approver or "local:unknown")
        except SchedulerRefused:
            approval_store.cancel(request_id, reason="no run waits for this approval")
        return True

    def emotion_snapshot():
        """The post-turn snapshot (#364), or None when switched off. Never raises.

        Read when the episode is written, so a change applies to the next one; an unreadable
        agent config counts as off (no collection while the setting is unknown)."""
        try:
            _gateway, agent_config = config_store.load()
            if not emotion_snapshot_enabled(agent_config.episodic_emotion_snapshot):
                return None
        except Exception as error:  # noqa: BLE001 - a bad file must not break the turn
            logging.getLogger(__name__).warning(
                "Emotion snapshot off: config unreadable (%s)", type(error).__name__
            )
            return None
        return capture_emotion_snapshot(agent, turn_id=turn_recorder.active.current)

    def runner(message):
        nonlocal history_state
        previous_stimulus_time = state.last_stimulus_time
        sync_model_selection()
        tool_config = sync_runtime_settings()
        # NXPAgent exclusively owns pre_turn/post_turn, including failure cleanup.
        try:
            tools = _stira_tools(
                bound_memory_tools,
                bound_memory_write_names,
                _status["breaker"]["level"],
                hitl_enabled=True,
                delegate_tool=delegate_tool,
                **tool_paths,
            )
            if tool_config is not None:
                # Saved switches only ever remove tools (#227).
                tools = filter_tools_by_config(tools, tool_config, _registered_tool_name)
            agent.tools = tuple(tools)
            if associative_recall is not None:
                associative_recall.set_query(prompt_text(message))
            try:
                response = agent(message)
            finally:
                _finish_recall(associative_recall, turn_recorder)
            # Deterministic, model-free summary (#77). record_episode never
            # raises, so a memory failure cannot fail the chat turn. The post-turn
            # emotion snapshot (#364) is read-only and goes to the floor, not the prompt.
            episode = record_episode(
                store,
                prompt_text(message),
                response,
                formatter=episodic_formatter,
                emotion=emotion_snapshot(),
            )
            if rating_session is not None:
                # ADR 0018 phase 0: bind the turn's rating (if any) to the episode just
                # written; stored only. Never raises.
                rating_session.bind_turn(
                    turn_recorder.active.current(), episode, _conversation(agent)
                )
            return response
        finally:
            if rating_session is not None:
                rating_session.discard(turn_recorder.active.current())
            # Refresh even after a failed/halted invocation.
            try:
                persistence.save()
            finally:
                refresh_status()
                with history_lock:
                    # Record actual stimulus occurrence, never classify twice.
                    stimulus = (
                        "stimulus" if state.last_stimulus_time != previous_stimulus_time else None
                    )
                    neural_history.record(state, stimulus=stimulus)
                    history_state = copy.deepcopy(state)

    app = Flask(
        __name__,
        template_folder=str(ROOT / "dashboard" / "templates"),
        static_folder=str(ROOT / "dashboard" / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    # Operator tooling/tests reach the live approval queue here (scripts/hitl_harness.py).
    app.extensions["hitl_broker"] = hitl_broker
    # Held until the process exits (#285); None only for _tests_share_data_dir.
    app.extensions["data_dir_lock"] = owner_lock
    app.extensions["authority"] = authority_service  # W1 (#296): read-only for tests / status
    app.extensions["authority_recorder"] = authority_recorder  # W2: lifecycle log + outbox
    # W4: whether the tool-call tracker is on the agent's hooks (YOLO or stage C); tests.
    app.extensions["tool_calls_tracked"] = track_tool_calls
    # Shutdown is never an approval: anything still pending resolves as rejected
    # (the "approvals" step; the entry point runs it before interpreter shutdown).
    app.extensions["lifecycle"] = lifecycle
    install_i18n(app)
    # Stage (#341) is opt-in (STIRA_STAGE=1): without it there is no route and no nav item.
    stage_on = stage_enabled()
    # ui.home (#350): "stage" opens / on the Stage and lists it first in the menu.
    ui_home = _ui_home(config_store, stage_on)
    if not stage_on:
        nav = STIRA_NAV
    elif ui_home == "stage":
        nav = ("stage", *STIRA_NAV)
    else:
        nav = (*STIRA_NAV, "stage")
    install_shell(app, nav)
    app.extensions["ui_home"] = ui_home
    install_security(
        app,
        csrf_exempt_endpoints=("webhooks.receive",),
        idempotent_endpoints=(
            "/api/chat",
            # /api/chat/session/new keeps its own reset keys (chat_api: process lifetime,
            # a repeat names the session it started), so it is not in this cache.
            "/api/hitl/*/decision",
            "/api/config/models*",
            "/api/scheduler/*",
        ),
    )

    if yolo_settings.enabled:
        # ADR 0012 phase 1: the status route and the reserved-key check (after the
        # security checks above). Without the allowance: no route, no check.
        # #450: the status names the tools each scope could answer (saved rules, read
        # only: no record, no latch from this poll).
        yolo_routes.install(
            app, tool_rules=lambda: ToolConfig.load(config_store.tool_path, readonly=True)
        )
    # Phase 7c: the YOLO values on the settings page, with or without the allowance
    # (that is where it is enabled; a person's save, used at the next start). No button,
    # badge or poll comes with it.
    yolo_settings_api.install(
        app,
        config_store=config_store,
        turn_running=lambda: bool(
            app.extensions.get("turn_lock") and app.extensions["turn_lock"].locked()
        ),
        wildcard_bind=_listens_on_wildcard,
        # The audit chain is built below (events.db); resolved at save time.
        record=lambda event_type, **fields: app.extensions["yolo_settings_record"](
            event_type, **fields
        ),
    )
    app.extensions["config_store"] = config_store
    # Operator tooling/tests only; no HTTP route controls the heartbeat (#238).
    app.extensions["heartbeat"] = heartbeat
    # Operator-defined jobs (#239): status/changes only through the authenticated API.
    app.extensions["scheduler"] = scheduler
    app.register_blueprint(create_scheduler_blueprint(lambda: scheduler, scheduler_status))
    # W2 (#296): the authority's read routes, the kill switch and the outbox (no latch is
    # cleared over HTTP: `stira authority unlatch`, server stopped).
    app.register_blueprint(
        create_authority_blueprint(
            lambda: authority_service,
            lambda: authority_recorder,
            lambda: app.extensions.get("authority_watchdog"),
        )
    )
    # #463: the review screen of the memory browser's procedural tab.
    app.register_blueprint(
        create_procedural_review_blueprint(
            procedure_proposals,
            turn_lock=lambda: app.extensions.get("turn_lock"),
            tool_call_running=(
                yolo_runtime.TRACKER.running if yolo_settings.enabled else (lambda: False)
            ),
        )
    )
    app.extensions["procedure_proposals"] = procedure_proposals
    # Pass the BreakLevel enum (not the BreakResult wrapper) so the shared
    # blueprint's level_name() resolves the same value app.py feeds it via
    # _check_breaker(); handing it the BreakResult would stringify to garbage
    # and silently break tool-permission gating on this server only.
    app.register_blueprint(
        create_config_blueprint(
            config_store,
            lambda: agent.engine.breaker_status().level,
            effective_classifier_provider=lambda: _classifier_view(agent),
            running_model_provider=lambda: {
                "provider": agent.provider,
                "model_id": agent.model_id,
                "apply_error": _status.get("model_selection_error"),
            },
            runtime_settings_provider=lambda: dict(runtime_settings),
            # The effective stack shows CRITICAL as the engine enforces it (#190).
            critical_action_provider=lambda: agent.engine.config.critical_action,
            # A saved rule voids the open approvals now, not at the next gate call:
            # restored cards leave the queue, waiting workers are rejected (#287).
            # Revocation failing is a failed save and suspends approvals (#330).
            on_tool_rules_saved=_tool_rules_saved_hook(tool_policy_reader, hitl_broker),
            # The settings hub's read-only location card (#469): no path, a flag and a name.
            config_location_provider=lambda: dict(config_location),
            # The heartbeat card's read-only status (#472).
            heartbeat_status_provider=lambda: heartbeat_status(),
        )
    )

    event_store = SQLiteEventStore(DATA_DIR / "events.db")
    lifecycle.add("events", lambda _t: _close_label(event_store), order=90, abort_only=True)
    # PR #444 M1: a loosening save of the YOLO settings is one audit row (with or without
    # the allowance; nothing is written at start-up).
    app.extensions["yolo_settings_record"] = AuditChain(event_store).record_yolo
    # #463: proposals, refusals, approvals and rejections of procedural rules (hashes only).
    procedure_proposals.audit = AuditChain(event_store).record_procedural_write
    # ADR 0019 §5: one audit.checks.* row per run_checks call (kinds, counts, mode, hash).
    run_checks_tool.set_audit(
        AuditChain(event_store).record_checks
        if tool_registry.RUN_CHECKS_STATE["registered"]
        else None
    )
    # #457: one audit.read.denied row per refused read (the tool, a keyed path hash, a code).
    file_tools_module.set_read_audit(
        AuditChain(event_store).record_read_refusal
        if containment_observer is None
        # ADR 0023 phase 0: the same row, counted on the way (the row is unchanged).
        else tee_read_refusals(containment_observer, AuditChain(event_store).record_read_refusal)
    )
    # #536: one audit.write.denied row per write refused on the secret-path floor.
    file_tools_module.set_write_audit(AuditChain(event_store).record_write_refusal)
    # #584: one audit.web.failed row per failed web_fetch (the code and HTTP status only).
    web_tools_module.set_fetch_audit(AuditChain(event_store).record_web_failure)
    # docs/task-models.md: one audit.task_model.call row per side-task call (tokens, codes).
    task_model_audit["record"] = AuditChain(event_store).record_task_model
    if containment_observer is not None:
        # One containment.observed row per signal: codes and numbers only (#460-0).
        containment_observer.record = lambda row: event_store.emit(
            ObservabilityEvent(event_type="containment.observed", attributes=row)
        )
    if taint_observer is not None:
        # One taint.observed row per turn: codes and numbers only (#458).
        taint_observer.record = lambda row: event_store.emit(
            ObservabilityEvent(event_type="taint.observed", attributes=row)
        )
    # ADR 0018 phase 0: ratings and memory-job plans (ids, codes, counts only).
    if rating_session is not None:
        rating_session.audit = AuditChain(event_store).record_memory
    if memory_executor is not None:
        memory_executor._audit = AuditChain(event_store).record_memory
    # ADR 0025: the session allowance service (memory only, audit rows in events.db). On
    # every chat server; nothing is offered before PR 3 (``session_allow.ENABLED``).
    _install_allowances(app, agent, config_store, tool_policy_reader, AuditChain(event_store))
    if yolo_settings.enabled:
        # ADR 0012 §8b (phase 2): host-confirmed operations, the YOLO request and the host
        # channel (command socket or desktop dialog); phase 3: the window, its marker in
        # the data directory and the latch judgement. Without the allowance: nothing.
        yolo_audit = AuditChain(event_store)
        yolo_runtime.install_host(
            data_dir=DATA_DIR,
            # W2: the window's pause / end rows are observed (after they were written)
            # as lifecycle events; YOLO's behaviour is unchanged.
            record=_observe_yolo_rows(yolo_audit.record_yolo, authority_service),
            ledger=yolo_audit,
            breaker=lambda: _yolo_breaker_level(agent.engine),
            breaker_reasons=lambda: _yolo_breaker_reasons(agent.engine),
            authority=authority_service,
        )
        # Phase 5a: the approver at the rule layer (§2 step 5). Without the allowance
        # the gate has none and every Ask goes to a person, exactly as before.
        agent.checkpoint_hooks.set_auto_approver(yolo_runtime.APPROVER)
        # ADR 0014 phase 1 (#451): progress reports inside a YOLO window (off unless the
        # dialog's tick or the settings turn them on). Without the allowance: none.
        progress_tracker = ProgressTracker(
            _progress_settings(config_store.gateway_path),
            window_reader=lambda: yolo_runtime.status().get("window"),
            continue_gate=lambda: _progress_may_continue(agent.engine),
        )
        agent.checkpoint_hooks.progress = progress_tracker
        app.extensions["progress"] = progress_tracker
        static_progress["tracker"] = progress_tracker

        def _stop_progress(_timeout):
            progress_tracker.cancel_auto("server_stopped")  # before the turn is interrupted
            return "stopped"

        lifecycle.add("progress", _stop_progress, order=4)

        def _stop_yolo_host(_timeout):
            # The server's stop ends the window normally (revoked(stop), marker closed).
            yolo_runtime.shutdown_host()
            return "stopped"

        lifecycle.add("yolo_host", _stop_yolo_host, order=80)

    def _flush_authority(timeout):
        # W1 (#296): every settlement (worker, a synchronous narrowing, unowned work) is
        # drained within the step's budget. Only "drained and durable" is "stopped"; work
        # left at the deadline is "pending", a drained but non-durable (unhealthy) service
        # "unhealthy" — never repaired here.
        return _AUTHORITY_STOP_LABELS.get(authority_service.flush_result(timeout=timeout), "error")

    lifecycle.add("authority", _flush_authority, order=85)

    # ADR 0011 W4 (#296): the grant control plane, only with gateway autonomy.stage_c
    # (default off: no route, no file, no thread). It runs nothing (no model, tool or
    # iteration); its periodic look is a step of the watchdog's pass below.
    # W5: the turn lock is created below; the executor reads it late (admission only).
    turn_holder = {}
    grants = _install_grants(
        app,
        autonomy_settings,
        authority_service,
        authority_recorder,
        agent,
        tool_paths,
        hitl_broker=hitl_broker,
        tool_provider=lambda _names: _stira_tools(
            bound_memory_tools, bound_memory_write_names, **tool_paths
        ),
        turn_busy=lambda: turn_holder["lock"].locked(),
        scheduler=scheduler,
        heartbeat_status=lambda: heartbeat_status(),
    )
    if grants is not None:
        lifecycle.add("grants", grants.stop, order=84)
    if scheduler is not None and scheduler._grant_tick is not None:
        grant_tick_wake["wake"] = scheduler.wake_grant_tick
    grant_executor = app.extensions.get("grant_executor")
    if grant_executor is not None:
        # W5: before the approvals close (10): the running iteration is stopped and joined.
        lifecycle.add("grant_executor", grant_executor.stop, order=8)

    # W2 (#296): the model-free watchdog (store / writer health, heartbeat staleness, its
    # own last pass and the gap since, outbox repair). With or without YOLO. Its passes
    # end first at a stop (no "heartbeat stale" while the stops run); the queued events
    # are written last, after YOLO's end and the authority drain.
    authority_watchdog = Watchdog(
        authority_service,
        authority_recorder,
        data_dir=DATA_DIR,
        heartbeat_status=lambda: heartbeat_status(),
        looks=()
        if grants is None
        else (grants.sync,) + (() if grant_executor is None else (grant_executor.poll,)),
    )
    lifecycle.add("authority_watchdog_passes", authority_watchdog.stop_passes, order=1)
    lifecycle.add("authority_watchdog", authority_watchdog.stop, order=86)
    authority_watchdog.start()
    app.extensions["authority_watchdog"] = authority_watchdog

    # ADR 0011 W6b: every holder pre-empts a goal iteration (the signal before the lock).
    preempt = None if grant_executor is None else grant_executor.preempt_signal
    turn_lock = TurnLock(threading.Lock(), preempt)
    turn_holder["lock"] = turn_lock

    def resume_paused_turn(request_id):
        """Replay the batch paused on an approval restored after a restart (#240).

        Runs in the background, serialized with chat turns. Busy for too long, or any
        mismatch found by resume_approved_request, means nothing runs (fail-closed).
        """

        def work():
            try:
                run()
            finally:
                new_session_host.resume_finished()

        def run():
            nonlocal history_state
            if not turn_lock.acquire(timeout=RESUME_LOCK_TIMEOUT_SEC):
                approval_store.refuse_resume(request_id, "the agent stayed busy")
                return
            started = False

            def on_replay():
                nonlocal started
                started = True
                turn_recorder.active.set(uuid4().hex)
                turn_recorder.emit(
                    "turn.started",
                    {"prompt_chars": 0, "image_count": 0, "resumed_request_id": request_id},
                )

            error = None
            outcome = None
            try:
                resume_tool_config = sync_runtime_settings()
                resume_tools = _stira_tools(
                    bound_memory_tools,
                    bound_memory_write_names,
                    _status["breaker"]["level"],
                    hitl_enabled=True,
                    delegate_tool=delegate_tool,
                    **tool_paths,
                )
                if resume_tool_config is not None:
                    # A resumed batch never runs a tool the saved rules set to Never (#190).
                    resume_tools = filter_tools_by_config(
                        resume_tools, resume_tool_config, _registered_tool_name
                    )
                agent.tools = tuple(resume_tools)
                outcome = resume_approved_request(
                    agent, approval_store, request_id, on_replay=on_replay
                )
            except Exception as exc:
                app.logger.exception("HITL resume failed")
                error = type(exc).__name__
            finally:
                try:
                    if started:
                        text = str(outcome.result) if outcome and outcome.resumed else ""
                        turn_recorder.emit(
                            "turn.completed",
                            {
                                "usage": {},
                                "stop": "resumed",
                                "error": error,
                                "output_preview": text,
                            },
                        )
                        turn_recorder.active.clear()
                        persistence.save()
                        with history_lock:
                            neural_history.record(state, stimulus=None)
                            history_state = copy.deepcopy(state)
                    refresh_status()
                finally:
                    turn_lock.release()
            if outcome is not None and not outcome.resumed:
                app.logger.warning("HITL resume refused: %s", outcome.reason)

        try:
            new_session_host.resume_scheduled()
        except LockBusy:
            # Not reserved: nothing may replay unseen by /new. Revoke (fail-closed).
            approval_store.refuse_resume(request_id, "the agent stayed busy")
            return
        try:
            threading.Thread(target=work, name=f"hitl-resume-{request_id[:8]}", daemon=True).start()
        except BaseException:
            new_session_host.resume_finished()
            raise

    new_session_host = _NewSessionHost(agent, event_store, new_session_lock)

    resume_scheduler["schedule"] = lambda request_id: (
        resume_job_after_approval(request_id) or resume_paused_turn(request_id)
    )
    # A rejected job request ends its waiting run now, not at the next tick (#556).
    resume_scheduler["rejected"] = lambda request_id: (
        scheduler.note_approval_rejected(request_id) if scheduler is not None else None
    )
    app.extensions["approval_store"] = approval_store
    # Operator tooling/tests: the host side of POST /api/chat/session/new (#350).
    app.extensions["new_session"] = new_session_host
    # The one lock every turn holds (chat stream, webhook, resume); tests/tooling only.
    app.extensions["turn_lock"] = turn_lock
    app.register_blueprint(WebhookReceiver(agent, runner=runner, turn_lock=turn_lock))
    chat_blueprint = create_chat_blueprint(
        agent,
        runner=runner,
        turn_lock=turn_lock,
        status_provider=status_provider,
        hitl_broker=hitl_broker,
        turn_summary_provider=lambda: _with_taint(
            _with_containment(agent.last_turn_summary, containment_observer), taint_observer
        ),
        turn_recorder=turn_recorder,
        # POST /api/chat/session/new (#350): the conversation only, fail-closed.
        new_session=new_session_host,
        progress=app.extensions.get("progress"),
        yolo_stop=yolo_runtime.stop,
        # #451: the activity board's (and the chat page's) estimated cost, per model.
        token_prices=_token_prices(config_store.gateway_path),
        # ADR 0011 W4: /new revokes every grant (only with autonomy.stage_c).
        session_reset=None if grants is None else grants.session_reset,
        # #535: the board's and the report's links to /artifacts?turn= (counts only).
        artifact_counts=(
            (lambda ids: artifacts.turn_counts(tool_paths["write_policy"], ids))
            if artifacts is not None
            else None
        ),
    )
    app.register_blueprint(chat_blueprint)

    # #452: a chat turn runs on its own thread, not on a page's stream. The server's stop
    # records it as interrupted first (before the approvals close and reject its card).
    def stop_chat_turn(_timeout):
        # #486: a running ``delegate`` child is stopped first (it never waits for the child).
        delegate_tool.cancel_active("cancelled")
        return chat_blueprint.interrupt_turn("server_stopped")

    lifecycle.add("chat_turn", stop_chat_turn, order=5)
    # /api/turns and /api/turns/<id>/events come from the chat API (turns.db); #286.
    # /observability and its snapshot/stream read the same turns.db (one source of truth).
    app.register_blueprint(create_turns_observability_blueprint(turn_store))
    app.register_blueprint(
        create_diagnostics_blueprint(
            write_failures,
            doctor_report=lambda: doctor_state["report"],
            run_doctor=run_write_doctor,
        )
    )
    app.extensions["write_failures"] = write_failures
    app.register_blueprint(
        create_artifact_blueprint(
            artifacts,
            policy_provider=lambda: tool_paths["write_policy"],
            # ADR 0020 §7: a person's label change, one audit row before it is saved.
            audit=AuditChain(event_store).record_artifact,
            principal=_label_principal,
            tool_call_running=(
                yolo_runtime.TRACKER.running if yolo_settings.enabled else (lambda: False)
            ),
        )
    )
    app.extensions["artifacts"] = artifacts
    app.register_blueprint(create_neural_history_blueprint(neural_history_provider))
    # ADR 0023 phase 0: read only (the switch, the tables, the window when on).
    app.register_blueprint(
        create_containment_blueprint(
            lambda: containment_view(containment_settings, containment_observer)
        )
    )
    app.extensions["containment"] = containment_observer
    # ADR 0022 phase 0: read only (the switch, the vocabulary, the counts when on).
    app.register_blueprint(
        create_taint_blueprint(lambda: taint_view(taint_settings, taint_observer))
    )
    app.extensions["taint"] = taint_observer

    if stage_on:
        # Read-only view on the same masked APIs; approvals via the existing decision API.
        # ui.board_svg (#453): read on each page load (the settings page saves it); on
        # unless the file says false (ADR 0012 addendum 2026-10-24)
        register_stage(
            app,
            ROOT / "dashboard" / "stage",
            board_svg=lambda: ui_board_svg(config_store.load()[0].ui),
        )

    @app.get("/approvals")
    def approvals_page():
        # Pending queue + decision history; the same /api/hitl state as chat cards (#189).
        return render_template("approvals.html")

    @app.get("/schedules")
    def schedules_page():
        # Jobs, recent runs and health notifications (#239); changes need the CSRF API.
        return render_template("schedules.html")

    @app.get("/observability")
    def observability_page():
        # #286: turns.db (TurnEventRecorder) is the source of truth for turn events; the
        # stream and snapshot behind this page read it. events.db keeps the /new audit chain.
        return render_template("observability.html", event_source="turns")

    @app.get("/artifacts")
    def artifacts_page():
        # #454 (ADR 0020): the workspace artifacts behind /api/artifacts.
        return render_template("artifacts.html")

    @app.get("/memory")
    def memory_page():
        # Read-only three-layer view backed by /api/memory/tree below.
        return render_template("forest.html")

    @app.get("/")
    def index():
        # ui.home (#350); /chat always stays, as the fallback and the detailed view.
        return redirect("/stage/" if ui_home == "stage" else "/chat")

    @app.get("/api/memory/tree")
    def memory_tree():
        tree = {}
        totals = {}
        for layer in MemoryLayer:
            entries = list(store.list_entries(layer, limit=MEMORY_TREE_LIMIT))
            # The list is the newest MEMORY_TREE_LIMIT; the total is counted without reading
            # (#574: the page showed the list length as the count, so a layer of 80 read "(20)").
            # Staged procedural proposals are not rules, so they are not counted either.
            totals[layer.value] = store.count_entries(
                layer, exclude_prefix=STAGING_PREFIX if layer is MemoryLayer.PROCEDURAL else None
            )
            if layer is MemoryLayer.PROCEDURAL:
                # Proposals are not rules: the review panel shows them (#463).
                entries = [e for e in entries if not is_staged_key(getattr(e, "key", ""))]
            tree[layer.value] = [
                {
                    "id": getattr(entry, "key", ""),
                    "content": getattr(entry, "content", str(entry)),
                }
                for entry in entries
            ]
        tree["totals"] = totals
        tree["limit"] = MEMORY_TREE_LIMIT
        return tree

    print("🧪 StiraCore Chat Server")
    # Which SOUL (agent-config soul_path or the default) and whether it reads; never
    # its path, which names users and directories in the log (#291).
    print(f"   SOUL: {_soul_banner(soul_path, configured=bool(identity_config.soul_path))}")
    anchors_banner = _anchors_banner(
        identity_config.identity_anchors, configured=anchors_configured
    )
    print(f"   Identity anchors: {anchors_banner}")
    print(f"   Model: {model_banner(agent, MODEL_ID, provider_config.provider)}")
    print(f"   State: stability={state.stability:.2f}")
    print(f"   Heartbeat: {_heartbeat_banner(heartbeat, heartbeat_status())}")
    print(f"   Scheduler: {_scheduler_banner(scheduler, scheduler_status())}")
    print(f"   URL: http://127.0.0.1:{PORT}/chat")
    print()

    return app


def _heartbeat_banner(daemon, status: dict) -> str:
    """The banner line from the same status /api/gateway/status serves (#285)."""
    if daemon is not None:
        return "on (zero-model tick)"
    if status.get("enabled"):
        return f"enabled, not running ({status.get('last_error') or 'unknown'})"
    return "off"


def _scheduler_banner(scheduler, status: dict) -> str:
    if scheduler is None:
        return "off"
    if scheduler.running:
        return str(status.get("state") or "running")
    return f"enabled, not running ({status.get('state') or 'stopped'})"


def _stop_label(stopped) -> str:
    return "stopping" if stopped is False else "stopped"


def _label_principal() -> str:
    """Who set an artifact label (#454): the server's OS account, as the review screen."""
    from stiracore.control.procedural_cli import operator_principal

    try:
        return operator_principal()
    except ValueError:
        return "local:unknown"


def _workspace_artifacts(root: Path) -> WorkspaceArtifacts | None:
    """The artifact registry (#454), or ``None`` with a warning (never a crash, no path)."""
    try:
        return WorkspaceArtifacts(root)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"WARNING: workspace artifacts are off ({type(error).__name__}, #454)")
        return None


def _close_label(resource) -> str:
    resource.close()
    return "closed"


def shutdown_app(app: Flask) -> dict:
    """Run the app's bounded stops (#285, ADR 0008); returns what each step did.

    Order: close the approval broker (a waiting worker is rejected, its request
    stays pending in the store and is restored after a restart), then stop the
    scheduler and the heartbeat — 4 s per step, 9 s in total, inside the 10 s
    ``stira_ctl.sh`` waits before SIGKILL. Idempotent, also against ``atexit``
    and concurrent callers (see :class:`ServerLifecycle`). The data-directory
    lock is left to the kernel, which releases it when the process has exited.

    The entry point calls this before interpreter shutdown on purpose: that first
    joins the SDK's worker threads, and a worker waiting on an approval would
    hold the process until the approval expires, so ``atexit`` alone never got to
    close the broker.
    """
    return app.extensions["lifecycle"].shutdown()


def _exit_on_sigterm(signum, frame) -> None:
    """SIGTERM → ``SystemExit(0)`` in the main thread, so the stop path runs (#285).

    Python's default SIGTERM ends the process without ``finally`` or ``atexit``.
    Later SIGTERMs are ignored so they cannot cut the bounded stops short
    (``stira_ctl.sh stop`` escalates to SIGKILL after 10 s).
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


class PortUnavailableError(Exception):
    """The server cannot listen on its port (#160); the message names the port only."""


def _bind_failed_in_use(error: OSError) -> bool:
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
    return error.errno in in_use or getattr(error, "winerror", None) == 10048


def listen_socket(host: str, port: int) -> socket.socket:
    """The server's listening socket, or :class:`PortUnavailableError` (#160).

    This is the server's only bind (the socket is handed to the web server), so a
    port taken at any moment before serving, on any address, ends the same way:
    a ``Stira:`` line and exit code 4. Socket options follow the web server's own
    bind: ``SO_REUSEADDR`` on POSIX (a port in TIME_WAIT is not "in use"), and
    ``SO_EXCLUSIVEADDRUSE`` on Windows, where ``SO_REUSEADDR`` would let a second
    socket share a port that is in use.
    """
    from werkzeug.serving import LISTEN_QUEUE, get_sockaddr, select_address_family

    family = select_address_family(host, port)
    try:
        address = get_sockaddr(host, int(port), family)
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as error:
        raise PortUnavailableError(
            f"cannot listen on port {port} ({error.strerror or type(error).__name__}); "
            "not starting. Check STIRA_HOST, or start on another port with STIRA_PORT."
        ) from None
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(LISTEN_QUEUE)
    except OSError as error:
        sock.close()
        if _bind_failed_in_use(error):
            raise PortUnavailableError(
                f"port {port} is already in use (by another program or another Stira "
                "server); not starting. Stop it, or start on another port with STIRA_PORT "
                f"(for example STIRA_PORT={port + 1})."
            ) from None
        raise PortUnavailableError(
            f"cannot listen on port {port} ({error.strerror or type(error).__name__}); "
            "not starting. Check STIRA_HOST, or start on another port with STIRA_PORT."
        ) from None
    return sock


def serve(application, host: str, port: int) -> None:
    """Serve ``application`` threaded, as ``Flask.run`` did, on :func:`listen_socket`."""
    from werkzeug.serving import make_server

    sock = listen_socket(host, port)
    try:
        server = make_server(host, port, application, threaded=True, fd=sock.fileno())
    finally:
        sock.close()  # the server holds its own duplicate
    try:
        server.log_startup()
        print("Press CTRL+C to quit", file=sys.stderr, flush=True)
        server.serve_forever()
    finally:
        server.server_close()


# ADR 0012 §8b revision 6: where the person confirms (the start-up line only).
_CHANNEL_TEXT = {
    "browser": "in the dashboard (STIRA_YOLO_HOST_CONFIRM to require the host)",
    "command": "on the host, stira approvals decide / stira yolo confirm",
    "dialog": "on the host, the desktop dialog",
}


def _listens_on_wildcard() -> bool:
    """Whether the next start's listen address is a wildcard (YOLO would refuse to start).

    The same reading as :func:`main` (``security.bind_host_setting``, which
    ``resolve_bind_host`` uses), never a second implementation (PR #444 M2).
    """
    return yolo_own_dashboard.is_wildcard(_security.bind_host_setting())


def main(app_factory=None) -> None:
    """Server entry point: refuse a data directory another server owns, stop cleanly.

    The lifecycle exists before the app does, so a SIGTERM at any point — while
    ``create_app`` is still starting things, or while serving — runs the same
    bounded stops on what exists by then. ``app_factory(lifecycle)`` is for
    offline harnesses (a scripted model); the default is :func:`create_app` with
    every safety default.
    """
    # #185: refuse a non-loopback bind before anything starts (no lock, store or thread).
    host = resolve_bind_host()
    try:
        # ADR 0012 §1: read once, before anything starts; names the variable, not the value.
        # Phase 7c: over the config's ``yolo`` section (a variable wins per value).
        # ADR 0012 addendum 2026-10-24: unset = external (offered, nothing approved), but
        # never on a wildcard listener — there the default is off and the start goes on.
        wildcard = yolo_own_dashboard.is_wildcard(host)
        yolo_settings = yolo_runtime.settings(
            config=_yolo_config_section(_default_config_store().gateway_path),
            default_allow="off" if wildcard else None,
        )
    except YoloSettingError as error:
        print(f"Stira: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    if yolo_settings.enabled and wildcard:
        # ADR 0012 §8b: the own-dashboard refusal cannot cover every interface. Fixed
        # message, no address; refused before anything (lock, data directory) exists.
        print(
            (
                "Stira: STIRA_YOLO_ALLOW needs a listener on one local address; "
                if yolo_settings.sources.get("allow") != "config"
                else "Stira: gateway-config yolo.allow needs a listener on one local address; "
            )
            + "set STIRA_HOST to a loopback or a single local address (not a wildcard)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    lifecycle = ServerLifecycle()
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    _install_log_masking()
    try:
        factory = app_factory or (lambda lc: create_app(lifecycle=lc))
        try:
            application = factory(lifecycle)
        except MissingCredentialError as error:
            # The message names the environment variable only, never its value.
            print(f"Stira: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        except task_models.TaskModelError as error:
            # A fixed code and the task name, never the configured value.
            print(f"Stira: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        except DataDirSettingError as error:
            # Names the variable only, never its value (#291).
            print(f"Stira: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        except DataDirLockError as error:
            print(f"Stira: {error}", file=sys.stderr)
            raise SystemExit(3) from None
        # ADR 0012 §8b, #495: from here on this process (its tools, redirects, in-process
        # clients) cannot connect to its own dashboard — at every start, with or without
        # the YOLO allowance (which refused a wildcard listener above) — nor to the observer
        # dashboard of this checkout, which reads the same config files (#499).
        yolo_own_dashboard.arm(
            host,
            PORT,
            wildcard=not yolo_settings.enabled,
            also_ports=(yolo_own_dashboard.OBSERVER_PORT,),
        )
        if yolo_settings.enabled and yolo_settings.sources.get("allow") == "default":
            # ADR 0012 addendum 2026-10-24: the default allowance is one short line per
            # start, not the operator's long notice (nothing was chosen, nothing opens).
            print(
                "YOLO: the Start YOLO button is offered (default; nothing is approved until "
                "a person presses and confirms it; STIRA_YOLO_ALLOW=off hides it)",
                flush=True,
            )
        elif wildcard and yolo_settings.sources.get("allow") == "default":
            print(
                "YOLO: not offered on a wildcard listener (STIRA_HOST set to one local "
                "address offers the button)",
                flush=True,
            )
        elif yolo_settings.enabled:
            host_desk = yolo_runtime.desk()
            channel = host_desk.channel if host_desk is not None and host_desk.available else None
            print(
                f"YOLO allowance: {yolo_settings.allowance} (a confirmed request opens a "
                "six-hour window in which internal tools' Ask is approved automatically in "
                "chat turns; with tier external also the listed external tools' Ask (at most "
                f"{yolo_settings.external_cap} per window, a card for secret-like arguments); "
                + (
                    "the press may include exec: shell / batch in that window (at most "
                    f"{yolo_settings.exec_cap}; cron and child-agent tools still ask; commands "
                    "can bypass Stira's protections); MCP still asks; "
                    if yolo_settings.exec_offered
                    else "MCP and exec still ask; "
                )
                + "the button is on the chat header and the "
                "Stage dialogue box; "
                "connections from this process to its own dashboard are refused; loosening "
                "saves, RECOVER and scheduler create / resume need a confirmation: "
                f"{_CHANNEL_TEXT.get(channel, 'unavailable')})",
                flush=True,
            )
        # Bound after the factory: a second server of this checkout keeps reporting
        # the data-directory owner (exit 3), whatever its port. The host was resolved
        # (and a non-loopback bind refused) before anything started (#185).
        try:
            serve(application, host, PORT)
        except PortUnavailableError as error:
            print(f"Stira: {error}", file=sys.stderr)
            raise SystemExit(4) from None
    finally:
        stopped = lifecycle.shutdown()
        # After the bounded stops (no tool call is left running): an in-process caller of
        # main() (tests, harnesses) is not held to a dashboard that is gone.
        yolo_own_dashboard.disarm()
        line = _stopped_line(stopped)
        if line is not None:
            print(line, flush=True)


# W1 (#296): the authority step's label per ``AuthorityService.flush_result`` (fixed
# words; only "clean" is "stopped").
_AUTHORITY_STOP_LABELS = {
    "clean": "stopped",
    "pending": "pending",
    "unhealthy": "unhealthy",
    "settle_unavailable": "pending (no settle worker)",
    "reentrant": "error",
}

# Stop steps named in the stop line only when they did not finish cleanly (W1, #296):
# the authority step's drain is "stopped" on every normal stop and would only lengthen
# the documented line; "pending" (work left at its deadline) or an error is shown.
# W4: the grants step (stage C only) is named only when it is not "stopped" (pending /
# unhealthy: drained is not durable). W5: the executor step likewise (pending: a thread
# still in a provider call at its deadline).
_QUIET_WHEN_CLEAN = frozenset(
    {"authority", "authority_watchdog_passes", "authority_watchdog", "grants", "grant_executor"}
)


def _observe_yolo_rows(record, service):
    """W2 (#296): YOLO's audit writer, unchanged, plus a read-only observation — after a
    successful ``audit.yolo.paused`` / ``audit.yolo.ended`` row one lifecycle event is
    queued (no I/O; a refused event is reported by the recorder). The row's result and
    errors are YOLO's own."""
    from stiracore.authority.lifecycle_events import YOLO_REASONS, make_event

    def recording(event_type, **fields):
        event_id = record(event_type, **fields)
        try:
            if event_type == "audit.yolo.paused":
                before, after, reason = "active", "paused", fields.get("reason_code")
            elif event_type == "audit.yolo.ended":
                before, after, reason = "unknown", "ended", fields.get("end_reason")
            else:
                return event_id
            recorder = service.recorder
            if recorder is not None:
                recorder.post(
                    make_event(
                        "yolo_window",
                        fields.get("window_id"),
                        before,
                        after,
                        reason if reason in YOLO_REASONS else "reason_not_recorded",
                        incarnation=service.incarnation,
                        epoch=service.epoch,
                        ts_wall=time.time(),
                    )
                )
        except Exception:  # noqa: BLE001 - observation never changes YOLO's result
            service.report_unhealthy("lifecycle_event_invalid")
        return event_id

    return recording


def _stopped_line(stopped: dict[str, str]) -> str | None:
    """The ``StiraCore: stopped (...)`` line for a lifecycle result, or ``None``."""
    if not stopped:
        return None
    shown = {
        name: label
        for name, label in stopped.items()
        if not (name in _QUIET_WHEN_CLEAN and label == "stopped")
    }
    return "StiraCore: stopped (" + ", ".join(f"{k}: {v}" for k, v in shown.items()) + ")"


if __name__ == "__main__":
    main()
