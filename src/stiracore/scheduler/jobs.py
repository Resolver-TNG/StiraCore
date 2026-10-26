"""Versioned job definitions for the scheduler (#239, ADR 0008 §1, ADR 0021 §2).

``parse_job`` is strict: unknown keys, tools outside the read-only set and secrets in
the prompt are refused. The read-only set is a constant; no environment variable
(``STIRA_ENABLE_EXEC_TOOLS``, ``BYPASS_TOOL_CONSENT``, ...) widens it.

Schema version 2 (ADR 0021 §2, phase S1) is additive: a version 1 definition stays valid
and means the same thing. Version 2 adds ``unattended_approval`` (agent jobs; ``wait`` is
refused until ADR 0024 phase 2), ``approval_wait_max_sec`` (only with ``wait``) and
``requires_heartbeat``. ``missed_policy: "catch_up"`` is refused for model-calling jobs.
These refusals apply when a person creates or updates a job; a stored definition is
replayed as it was written (``stored=True``) and the scheduler applies the rules at run time.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Any

from .schedule import Schedule, ScheduleError, parse_schedule

__all__ = [
    "APPROVAL_MODES",
    "JOB_READ_ONLY_TOOLS",
    "MEMORY_JOB_CAPS",
    "MEMORY_JOB_KINDS",
    "MODEL_CALLING_KINDS",
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "Budget",
    "JobDefinition",
    "JobError",
    "is_model_calling",
    "parse_job",
]

SCHEMA_VERSION = 2
SCHEMA_VERSIONS = frozenset({1, 2})
# Tools an unattended job may call. Everything else needs a human approval, which an
# unattended job cannot get (ADR 0008 §8): it is refused here and again at run time.
JOB_READ_ONLY_TOOLS = frozenset(
    {"current_time", "memory_read", "memory_search", "memory_list", "file_read"}
)
LAYERS = frozenset({"gateway", "agent", "memory"})
# ADR 0018 §2b/§2c: memory jobs are *fixed kinds* — the kind fixes the prompt (in code), the
# job has no prompt and no tools. ``MODEL_CALLING_KINDS`` is the one list of job kinds that
# may build a model (the memory executor refuses any other kind); every other job kind,
# Gateway actions included, is model-free.
MEMORY_JOB_KINDS = ("memory_rate_backfill", "memory_consolidate", "memory_tidy")
MODEL_CALLING_KINDS = frozenset({"memory_rate_backfill", "memory_consolidate"})
# Per-kind ceilings (ADR 0018 §2d, commander decision Q5): a definition may ask for less,
# never more; a missing value takes the ceiling.
MEMORY_JOB_CAPS: dict[str, dict[str, int | None]] = {
    "memory_rate_backfill": {
        "max_runs_per_day": 6,
        "max_tokens_per_run": 15_000,
        "max_tokens_per_day": 60_000,
    },
    "memory_consolidate": {
        "max_runs_per_day": 1,
        "max_tokens_per_run": 30_000,
        "max_tokens_per_day": 30_000,
    },
    "memory_tidy": {"max_runs_per_day": 6, "max_tokens_per_run": None, "max_tokens_per_day": None},
}
MISSED_POLICIES = frozenset({"skip", "catch_up", "latest"})
# ADR 0021 §2 / Q4: after an outage a model-calling job runs at most the newest missed fire.
MODEL_MISSED_POLICIES = frozenset({"skip", "latest"})
# ADR 0021 §6: ``wait`` belongs to ADR 0024 phase 2 (S3) and is refused until then.
APPROVAL_MODES = frozenset({"deny", "wait"})
# ADR 0024 phase 2 (go 2026-10-21): ``wait`` is parsed; the scheduler still refuses it
# unless a durable approval store is wired (``Scheduler(approval_waits=...)``).
ENABLED_APPROVAL_MODES = frozenset({"deny", "wait"})
APPROVAL_WAIT_DEFAULT = 3600
OVERLAP_POLICIES = frozenset({"skip"})
JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "job_id",
        "name",
        "layer",
        "action",
        "prompt",
        "tools",
        "schedule",
        "missed_policy",
        "max_catch_up",
        "overlap_policy",
        "hold_max_sec",
        "budget",
        # schema version 2 (ADR 0021 §2)
        "unattended_approval",
        "approval_wait_max_sec",
        "requires_heartbeat",
    }
)
_V2_KEYS = ("unattended_approval", "approval_wait_max_sec", "requires_heartbeat")
_BUDGET_KEYS = ("max_runs_per_day", "max_tokens_per_run", "max_tokens_per_day")
PROMPT_LIMIT = 4000


class JobError(ValueError):
    """The job definition is invalid (``code`` = ``invalid`` or ``scope``)."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Budget:
    max_runs_per_day: int | None = None
    max_tokens_per_run: int | None = None
    max_tokens_per_day: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {key: getattr(self, key) for key in _BUDGET_KEYS}


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    name: str
    schedule: Schedule
    layer: str = "gateway"
    action: str | None = None
    prompt: str | None = None
    tools: tuple[str, ...] = ()
    missed_policy: str = "skip"
    max_catch_up: int = 3
    overlap_policy: str = "skip"
    hold_max_sec: int = 3600
    budget: Budget = field(default_factory=Budget)
    schema_version: int = SCHEMA_VERSION
    # Schema version 2 (ADR 0021 §2). ``None`` = not given: ``unattended_approval`` is
    # ``"deny"`` for agent jobs (and not applicable otherwise), ``approval_wait_max_sec``
    # only exists with ``wait``, ``requires_heartbeat`` defaults to true for model-calling
    # jobs and is ignored for every other job.
    unattended_approval: str | None = None
    approval_wait_max_sec: int | None = None
    requires_heartbeat: bool | None = None

    @property
    def model_calling(self) -> bool:
        return is_model_calling(self.layer, self.action)

    @property
    def needs_heartbeat(self) -> bool:
        """ADR 0021 §8: a model-calling run needs a healthy heartbeat unless the job says
        ``requires_heartbeat: false``. Version 1 definitions take the default."""
        return self.model_calling and self.requires_heartbeat is not False

    @property
    def effective_missed_policy(self) -> str:
        """``catch_up`` never fires a model-calling job in bulk (ADR 0021 Q4): a stored
        definition that still says ``catch_up`` runs as ``latest``."""
        if self.model_calling and self.missed_policy == "catch_up":
            return "latest"
        return self.missed_policy

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "name": self.name,
            "layer": self.layer,
            "action": self.action,
            "prompt": self.prompt,
            "tools": list(self.tools),
            "schedule": self.schedule.to_dict(),
            "missed_policy": self.missed_policy,
            "max_catch_up": self.max_catch_up,
            "overlap_policy": self.overlap_policy,
            "hold_max_sec": self.hold_max_sec,
            "budget": self.budget.to_dict(),
        }
        if self.schema_version >= 2:
            for key in _V2_KEYS:
                data[key] = getattr(self, key)
        return data


def is_model_calling(layer: str, action: str | None) -> bool:
    """Agent jobs and the model-calling memory kinds (ADR 0018 §2d); nothing else."""
    return layer == "agent" or (layer == "memory" and action in MODEL_CALLING_KINDS)


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise JobError(f"{name} must be an integer between {low} and {high}")
    return value


def _budget(value: Any) -> Budget:
    if value is None:
        return Budget()
    if not isinstance(value, dict):
        raise JobError("budget must be an object")
    unknown = set(value) - set(_BUDGET_KEYS)
    if unknown:
        raise JobError(f"unknown budget field(s): {', '.join(sorted(unknown))}")
    parsed = {}
    for key in _BUDGET_KEYS:
        raw = value.get(key)
        parsed[key] = None if raw is None else _int(raw, f"budget.{key}", 1, 10_000_000)
    return Budget(**parsed)


def _contains_secret(text: str) -> bool:
    from stiracore.observability.masking import mask_secrets

    return mask_secrets(text) != text


def parse_job(
    data: Any,
    *,
    now: float,
    gateway_actions: frozenset[str] | set[str] | None,
    agent_jobs: bool,
    existing: JobDefinition | None = None,
    memory_kinds: frozenset[str] | set[str] | None = frozenset(),
    stored: bool = False,
) -> JobDefinition:
    """Validate ``data``; with ``existing`` it is an update (layer/action immutable).

    ``memory_kinds``: the memory job kinds this server accepts (ADR 0018; empty = none,
    ``None`` = any known kind — used when replaying the stored log). ``stored``: replaying
    a definition that was accepted when it was written; the refusals a person meets at
    create / update (``catch_up`` for model-calling jobs, a field of a phase not enabled
    here) do not drop it — the scheduler applies the safe meaning at run time."""
    try:
        return _parse_job(
            data,
            now=now,
            gateway_actions=gateway_actions,
            agent_jobs=agent_jobs,
            existing=existing,
            memory_kinds=memory_kinds,
            stored=stored,
        )
    except (TypeError, AttributeError) as error:
        # e.g. {"layer": []}: a wrong type is an invalid definition, never a 500.
        raise JobError(f"invalid field type ({type(error).__name__})") from None


def _parse_job(
    data: Any,
    *,
    now: float,
    gateway_actions: frozenset[str] | set[str] | None,
    agent_jobs: bool,
    existing: JobDefinition | None,
    memory_kinds: frozenset[str] | set[str] | None = frozenset(),
    stored: bool = False,
) -> JobDefinition:
    if not isinstance(data, dict):
        raise JobError("job must be an object")
    unknown = set(data) - _TOP_LEVEL
    if unknown:
        raise JobError(f"unknown field(s): {', '.join(sorted(unknown))}", "scope")
    if existing is not None:
        for key in ("layer", "action", "job_id"):
            if key in data and data[key] != getattr(existing, key):
                raise JobError(f"{key} cannot be changed; create another job", "scope")
        merged = existing.to_dict()
        # An update is written in the current version unless it names one (v2 is additive).
        merged.pop("schema_version", None)
        merged.update(data)
        data = merged
    version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or version not in SCHEMA_VERSIONS:
        raise JobError(f"unsupported schema_version {version!r}")
    if version < 2:
        given = sorted(key for key in _V2_KEYS if data.get(key) is not None)
        if given:
            raise JobError(f"{', '.join(given)} need schema_version 2")
    job_id = data.get("job_id") or f"job-{secrets.token_hex(6)}"
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise JobError("job_id must match [a-z0-9][a-z0-9_-]{0,47}")
    if _contains_secret(job_id):
        # Identifiers are persisted and logged verbatim: refuse, never mask.
        raise JobError("job_id must not look like a secret")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name.strip() or "\x00"):
        raise JobError("name must be 1-80 printable characters")
    if _contains_secret(name):
        raise JobError("name must not contain a secret")
    layer = data.get("layer", "gateway")
    if not isinstance(layer, str) or layer not in LAYERS:
        raise JobError("layer must be gateway, agent or memory")
    if layer == "agent" and not agent_jobs:
        raise JobError("agent (model-calling) jobs are not enabled on this server", "scope")
    if layer == "memory" and memory_kinds is not None and not memory_kinds:
        raise JobError("memory jobs are not enabled on this server", "scope")
    try:
        schedule = parse_schedule(data.get("schedule"), default_anchor=now)
    except ScheduleError as error:
        raise JobError(str(error)) from None
    tools_raw = data.get("tools") or []
    if not isinstance(tools_raw, list) or not all(isinstance(t, str) for t in tools_raw):
        raise JobError("tools must be a list of tool names")
    outside = sorted(set(tools_raw) - JOB_READ_ONLY_TOOLS)
    if outside:
        raise JobError(
            "only read-only tools are allowed in jobs (approval-gated tools cannot run "
            f"unattended): {', '.join(outside)}",
            "scope",
        )
    tools = tuple(sorted(set(tools_raw)))
    budget = _budget(data.get("budget"))
    action = data.get("action")
    prompt = data.get("prompt")
    if layer == "memory":
        budget = _memory_job(action, prompt, tools, budget, memory_kinds)
    elif layer == "gateway":
        if (
            not isinstance(action, str)
            or (gateway_actions is not None and action not in gateway_actions)
            or _contains_secret(action)
        ):
            raise JobError(f"unknown gateway action {action!r}", "scope")
        if prompt is not None or tools:
            raise JobError("gateway jobs take no prompt or tools (they never call a model)")
    else:
        if action is not None:
            raise JobError("agent jobs take a prompt, not an action")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT:
            raise JobError(f"prompt must be 1-{PROMPT_LIMIT} characters")
        if _contains_secret(prompt):
            raise JobError("prompt must not contain a secret (it is stored in the audit log)")
        if budget.max_tokens_per_run is None or budget.max_tokens_per_day is None:
            raise JobError("agent jobs need budget.max_tokens_per_run and max_tokens_per_day")
    missed = data.get("missed_policy", "skip")
    if not isinstance(missed, str) or missed not in MISSED_POLICIES:
        raise JobError("missed_policy must be skip, catch_up or latest")
    model_calling = is_model_calling(layer, action)
    if model_calling and missed not in MODEL_MISSED_POLICIES and not stored:
        # ADR 0021 §2 / Q4: no bulk model calls after an outage.
        raise JobError(
            "missed_policy catch_up is not allowed for model-calling jobs (use skip or latest)",
            "scope",
        )
    approval, wait_max = _approval_fields(data, layer, stored)
    requires_heartbeat = data.get("requires_heartbeat")
    if requires_heartbeat is not None and not isinstance(requires_heartbeat, bool):
        raise JobError("requires_heartbeat must be true or false")
    overlap = data.get("overlap_policy", "skip")
    if not isinstance(overlap, str) or overlap not in OVERLAP_POLICIES:
        raise JobError("overlap_policy must be skip")
    job = JobDefinition(
        job_id=job_id,
        name=name.strip(),
        schedule=schedule,
        layer=layer,
        action=action if layer in ("gateway", "memory") else None,
        prompt=prompt if layer == "agent" else None,
        tools=tools,
        missed_policy=missed,
        max_catch_up=_int(data.get("max_catch_up", 3), "max_catch_up", 1, 10),
        overlap_policy=overlap,
        hold_max_sec=_int(data.get("hold_max_sec", 3600), "hold_max_sec", 0, 86400),
        budget=budget,
        schema_version=version,
        unattended_approval=approval,
        approval_wait_max_sec=wait_max,
        requires_heartbeat=requires_heartbeat,
    )
    return job if existing is None else replace(job, job_id=existing.job_id)


def _approval_fields(data: dict, layer: str, stored: bool) -> tuple[str | None, int | None]:
    """``unattended_approval`` / ``approval_wait_max_sec`` (ADR 0021 §2, §6).

    Agent jobs only. ``deny`` (default) is ADR 0008 §8 as implemented; ``wait`` is ADR 0024
    phase 2 and refused (``scope``) until that phase is enabled; the wait limit only exists
    with ``wait``."""
    mode = data.get("unattended_approval")
    wait_max = data.get("approval_wait_max_sec")
    if layer != "agent":
        if mode is not None or wait_max is not None:
            raise JobError("unattended_approval and approval_wait_max_sec apply to agent jobs")
        return None, None
    if mode is None:
        mode = "deny"
    if not isinstance(mode, str) or mode not in APPROVAL_MODES:
        raise JobError("unattended_approval must be deny or wait")
    if mode not in ENABLED_APPROVAL_MODES and not stored:
        raise JobError(
            "unattended_approval wait needs ADR 0024 phase 2, which is not enabled here",
            "scope",
        )
    if wait_max is not None:
        if mode != "wait":
            raise JobError("approval_wait_max_sec only applies with unattended_approval wait")
        wait_max = _int(wait_max, "approval_wait_max_sec", 60, 86400)
    elif mode == "wait":
        wait_max = APPROVAL_WAIT_DEFAULT
    return mode, wait_max


def _memory_job(
    kind: Any,
    prompt: Any,
    tools: tuple[str, ...],
    budget: Budget,
    memory_kinds: frozenset[str] | set[str] | None,
) -> Budget:
    """A fixed-kind memory job (ADR 0018 §2b): the kind names it, nothing else is free."""
    if not isinstance(kind, str) or kind not in MEMORY_JOB_KINDS:
        raise JobError(f"unknown memory job kind {kind!r}", "scope")
    if memory_kinds is not None and kind not in memory_kinds:
        raise JobError(f"memory job kind {kind} is not enabled on this server", "scope")
    if prompt is not None or tools:
        raise JobError("memory jobs take no prompt or tools (the kind fixes them)")
    caps = MEMORY_JOB_CAPS[kind]
    values = {}
    for key, ceiling in caps.items():
        asked = getattr(budget, key)
        if ceiling is None:
            if key != "max_runs_per_day" and asked is not None:
                raise JobError(f"{kind} calls no model: budget.{key} does not apply")
            values[key] = asked
            continue
        if asked is not None and asked > ceiling:
            raise JobError(f"budget.{key} of {kind} must be at most {ceiling}", "scope")
        values[key] = ceiling if asked is None else asked
    return Budget(**values)
