"""Per-task model routing (#258, #585): which model a side task such as a summary uses.

Every side task Stira runs on a model is named in :data:`TASKS`. Each one uses the chat
model (``inherit``) unless the operator routes it, in this order:

1. the environment variable ``STIRA_TASK_MODEL_<TASK>`` (upper case, e.g.
   ``STIRA_TASK_MODEL_TOOL_SUMMARY``),
2. the gateway config ``task_models: {"<task>": "<model>"}``,
3. ``inherit`` (the chat model; nothing changes).

``<model>`` is ``inherit``, a model id of the chat's provider (the chat's endpoint, key
variable and region are reused), or ``provider:model`` for another provider whose
credentials are already set up (nothing new is looked for).

Invariants (fail closed, the cost and the destination of the data never change silently):

* An unknown task name, an unknown model id (not in the capability table), a provider whose
  SDK is missing, or another provider without configured credentials refuses the start
  (:class:`TaskModelError`, a fixed code; never the value).
* A routed model that fails is an error of that task: there is no fallback to another model.
* A task that takes no tools (all of them today) refuses a call that passes tool specs.
* Each routed call leaves one record: task, provider, model id, input / output tokens, the
  status and a failure reason code. Never the text, never a URL.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from strands.models import Model

from stiracore.providers.capabilities import capabilities_for
from stiracore.providers.credentials import credential_status, require_credentials
from stiracore.providers.spec import ModelSpec, normalize_provider

logger = logging.getLogger(__name__)

INHERIT = "inherit"
ENV_PREFIX = "STIRA_TASK_MODEL_"
SOURCES = ("env", "config", "inherit")
_TASK_NAME = re.compile(r"\A[a-z][a-z0-9_]{0,40}\Z")
_MAX_REF = 200


@dataclass(frozen=True)
class TaskInfo:
    """One side task that calls a model. ``tools``: whether the call may carry tools."""

    name: str
    description: str
    default: str = INHERIT
    tools: bool = False
    # Record (and fail closed on) the call also when it inherits the chat model: a new task
    # whose cost must be visible either way. False keeps an existing caller as it was.
    audit_inherit: bool = False


TASKS: Mapping[str, TaskInfo] = MappingProxyType(
    {
        info.name: info
        for info in (
            TaskInfo(
                "tool_summary",
                "Summary of a long tool result on request (web_fetch summarize=True, #585). "
                "No tools; the output is untrusted data.",
                audit_inherit=True,
            ),
            TaskInfo(
                "context_summary",
                "The conversation summary of context management (#258, opt-in). No tools.",
            ),
        )
    }
)

# Failure codes (stable; the settings page and the start-up message use them).
CODES = (
    "unknown_task",
    "invalid_value",
    "unknown_model",
    "provider_unavailable",
    "credentials_not_configured",
    "tools_not_allowed",
    "model_failed",
)


class TaskModelError(ValueError):
    """A routing problem or a failed routed call. The message never holds a value."""

    def __init__(self, code: str, task: str | None, detail: str = "", reason: str | None = None):
        self.code = code if code in CODES else "invalid_value"
        self.task = task if isinstance(task, str) and _TASK_NAME.fullmatch(task) else None
        self.reason = reason
        where = f" for task '{self.task}'" if self.task else ""
        super().__init__(f"task model {self.code}{where}" + (f": {detail}" if detail else ""))


def env_name(task: str) -> str:
    return ENV_PREFIX + task.upper()


def check_config(value: Any) -> dict[str, str]:
    """Shape of gateway ``task_models``: known task names -> nonempty strings."""
    if not isinstance(value, dict):
        raise ValueError("task_models must be an object")  # noqa: TRY004 - config contract
    for task, ref in value.items():
        if task not in TASKS:
            raise ValueError(f"task_models: unknown task (known: {', '.join(sorted(TASKS))})")
        if not isinstance(ref, str) or not ref.strip() or len(ref) > _MAX_REF:
            raise ValueError(f"task_models.{task} must be a nonempty model reference")
    return dict(value)


@dataclass(frozen=True)
class TaskRoute:
    """Where one task's calls go. ``spec`` is None for ``inherit``."""

    task: str
    source: str
    spec: ModelSpec | None

    @property
    def inherit(self) -> bool:
        return self.spec is None

    def describe(self, main: ModelSpec | None) -> dict[str, Any]:
        spec = self.spec or main
        return {
            "task": self.task,
            "description": TASKS[self.task].description,
            "source": self.source,
            "inherit": self.inherit,
            "provider": spec.provider if spec else None,
            "model_id": spec.model_id if spec else None,
            "env": env_name(self.task),
            "error": None,
        }


def parse_ref(task: str, ref: str, main: ModelSpec, region: str | None = None) -> ModelSpec | None:
    """``inherit`` -> None; a bare id -> the chat's provider and options; ``p:model`` ->
    provider ``p`` (the chat's options only when it is the chat's provider; another
    provider gets none of the chat's endpoint / key settings, Bedrock only the region)."""
    text = ref.strip()
    if not text or len(text) > _MAX_REF:
        raise TaskModelError("invalid_value", task, "empty or too long model reference")
    if text.lower() == INHERIT:
        return None
    head, sep, rest = text.partition(":")
    provider = None
    if sep and rest.strip():
        try:
            provider = normalize_provider(head)
        except ValueError:
            provider = None  # a Bedrock id such as "...-v1:0"
    model_id = rest.strip() if provider else text
    provider = provider or main.provider
    if provider == main.provider:
        options = {k: v for k, v in main.options.items() if k != "capabilities"}
    elif provider == "bedrock" and (region or main.region_name):
        options = {"region_name": region or main.region_name}
    else:
        options = {}
    return ModelSpec(provider, model_id, options)


def _check_route(task: str, spec: ModelSpec, main: ModelSpec, environ: Mapping[str, str]):
    if not capabilities_for(spec).known:
        raise TaskModelError("unknown_model", task, "not in the model capability table")
    from stiracore.providers.factory import provider_available

    if not provider_available(spec.provider):
        raise TaskModelError("provider_unavailable", task, f"install the {spec.provider} extra")
    if spec.provider == main.provider:
        try:
            require_credentials(
                spec.provider,
                api_key_env=spec.options.get("api_key_env"),
                base_url=spec.options.get("base_url"),
                environ=environ,
            )
        except ValueError:
            raise TaskModelError(
                "credentials_not_configured", task, f"provider {spec.provider}"
            ) from None
        return
    if credential_status(spec.provider, environ=environ).get("configured") is not True:
        raise TaskModelError("credentials_not_configured", task, f"provider {spec.provider}")


def _route(
    task: str,
    config: Mapping[str, str],
    env: Mapping[str, str],
    main: ModelSpec,
    region: str | None,
    check: bool,
) -> TaskRoute:
    raw = env.get(f"STIRA_TASK_MODEL_{task.upper()}")  # env_name(task)
    if raw is not None and raw.strip():
        source, ref = "env", raw
    elif task in config:
        source, ref = "config", config[task]
    else:
        source, ref = "inherit", TASKS[task].default
    spec = parse_ref(task, ref, main, region)
    if spec is not None and check:
        _check_route(task, spec, main, env)
    return TaskRoute(task, source, spec)


def resolve_routes(
    config: Mapping[str, str] | None,
    main: ModelSpec,
    environ: Mapping[str, str] | None = None,
    *,
    region: str | None = None,
    check: bool = True,
) -> dict[str, TaskRoute]:
    """One route per task (env > config > inherit). ``check``: refuse a bad route."""
    env = os.environ if environ is None else environ
    # Exact names only: a name in another case (STIRA_TASK_MODEL_tool_summary) is never read,
    # so it is refused rather than silently ignored (review r588).
    allowed = {env_name(task) for task in TASKS}
    unknown = sorted(
        name for name in env if name.upper().startswith(ENV_PREFIX) and name not in allowed
    )
    if unknown:
        raise TaskModelError(
            "unknown_task", None, "unknown or mis-cased STIRA_TASK_MODEL_ variable"
        )
    try:
        config = check_config(dict(config or {}))
    except ValueError as error:
        raise TaskModelError("unknown_task", None, str(error)) from None
    return {task: _route(task, config, env, main, region, check) for task in TASKS}


def describe_routes(
    config: Mapping[str, str] | None,
    main: ModelSpec,
    environ: Mapping[str, str] | None = None,
    *,
    region: str | None = None,
) -> list[dict[str, Any]]:
    """Read-only rows for the settings page; a bad route shows its code, never raises."""
    env = os.environ if environ is None else environ
    config = dict(config) if isinstance(config, Mapping) else {}
    rows = []
    for task in TASKS:
        try:
            row = _route(task, config, env, main, region, True).describe(main)
        except (TaskModelError, ValueError) as error:
            row = TaskRoute(task, "inherit", None).describe(main)
            row.update(error=getattr(error, "code", "invalid_value"), provider=None, model_id=None)
        rows.append(row)
    return rows


def _usage_of(event: Any) -> Mapping[str, Any] | None:
    if isinstance(event, Mapping):
        metadata = event.get("metadata")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("usage"), Mapping):
            return metadata["usage"]
    return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


class RoutedModel(Model):
    """The boundary of every task call: refuses tools when the task takes none. A routed
    model (``record`` set) also records each call and turns a failure into
    :class:`TaskModelError` (no fallback); ``inherit`` (``record`` None) passes the chat
    model's events and errors through unchanged."""

    def __init__(
        self, task: str, inner: Model, provider: str | None, model_id: str | None, record
    ) -> None:
        self.task = task
        self.inner = inner
        self.provider = provider
        self.model_id = model_id
        self._record = record

    @property
    def stateful(self) -> bool:
        return bool(getattr(self.inner, "stateful", False))

    def update_config(self, **model_config: Any) -> None:
        self.inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self.inner.get_config()

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise TaskModelError("tools_not_allowed", self.task, "structured output is not routed")

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, **kwargs: Any
    ) -> AsyncIterable[Any]:
        if tool_specs and not TASKS[self.task].tools:
            if self._record is not None:
                self._record(self, "refused", None, "tools_not_allowed")
            raise TaskModelError("tools_not_allowed", self.task, "this task takes no tools")
        if self._record is None:  # inherit: the chat model, its own errors, no record
            async for event in self.inner.stream(messages, tool_specs, system_prompt, **kwargs):
                yield event
            return
        usage: Mapping[str, Any] | None = None
        started = time.monotonic()
        reason = None
        try:
            async for event in self.inner.stream(messages, tool_specs, system_prompt, **kwargs):
                usage = _usage_of(event) or usage
                yield event
        except TaskModelError:
            raise
        except Exception as error:  # noqa: BLE001 - classified, never echoed
            from stiracore.providers.connection_test import classify_error

            reason = classify_error(error)[0] or "unknown"
        if reason is not None:
            # Raised outside the handler: no __cause__ / __context__, so the provider's
            # message (it can hold a key, a signed URL, page text) never reaches a traceback.
            self._record(self, "error", usage, reason, started)
            raise TaskModelError("model_failed", self.task, reason=reason)
        self._record(self, "success", usage, None, started)


class TaskModelRouter:
    """Built once at start-up from the resolved routes; models are built on first use."""

    def __init__(
        self,
        routes: Mapping[str, TaskRoute],
        main: ModelSpec | None = None,
        *,
        build: Callable[[ModelSpec], Model] | None = None,
        on_record: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.routes = dict(routes)
        self.main = main
        self._build = build
        self._models: dict[str, RoutedModel] = {}
        self.on_record = on_record
        self.records: list[dict[str, Any]] = []  # the last 50, for the settings page

    def model_for(self, task: str, default: Any = None) -> Any:
        """The routed model, or ``default`` (the chat model) for ``inherit``."""
        if task not in TASKS:
            raise TaskModelError("unknown_task", None)
        route = self.routes.get(task)
        if route is None or route.spec is None:
            # inherit: the chat model behind the same boundary (no tools for a task that takes
            # none), passed through otherwise: the same request, its own errors, no record —
            # unless the task records inherited calls too (``audit_inherit``).
            if default is None:
                return None
            config = default.get_config() if hasattr(default, "get_config") else None
            model_id = config.get("model_id") if isinstance(config, Mapping) else None
            if TASKS[task].audit_inherit:
                provider = self.main.provider if self.main is not None else None
                return RoutedModel(task, default, provider, model_id, self._add)
            return RoutedModel(task, default, None, model_id, None)
        model = self._models.get(task)
        if model is None:
            failed = False
            try:
                inner = self._build(route.spec) if self._build else _default_build(route.spec)
            except Exception:  # noqa: BLE001 - no fallback to another model
                failed = True
            if failed:  # raised outside the handler: the build error never chains
                raise TaskModelError("provider_unavailable", task)
            model = RoutedModel(task, inner, route.spec.provider, route.spec.model_id, self._add)
            self._models[task] = model
        return model

    def _add(self, model: RoutedModel, status, usage, reason, started=None) -> None:
        record = {
            "task": model.task,
            "provider": model.provider,
            "model": model.model_id,
            "status": status,
            "input_tokens": _count((usage or {}).get("inputTokens")),
            "output_tokens": _count((usage or {}).get("outputTokens")),
        }
        if reason:
            record["reason"] = reason
        if started is not None:
            record["duration_ms"] = int((time.monotonic() - started) * 1000)
        self.records = [*self.records[-49:], record]
        logger.info(
            "task model call: task=%s provider=%s model=%s status=%s in=%s out=%s",
            record["task"],
            record["provider"],
            record["model"],
            status,
            record["input_tokens"],
            record["output_tokens"],
        )
        if self.on_record is not None:
            try:
                self.on_record(dict(record))
            except Exception:  # noqa: BLE001 - telemetry never breaks the task
                logger.warning("task model record dropped")

    def describe(self) -> list[dict[str, Any]]:
        return [self.routes[task].describe(self.main) for task in TASKS if task in self.routes]

    def routed(self) -> bool:
        return any(route.spec is not None for route in self.routes.values())


def _default_build(spec: ModelSpec) -> Model:
    from stiracore.providers import create_model, resolve_max_output_tokens

    return create_model(spec, max_tokens=resolve_max_output_tokens(None, spec))


def inherit_router(main: ModelSpec | None = None) -> TaskModelRouter:
    """Every task on the chat model (nothing configured)."""
    return TaskModelRouter({t: TaskRoute(t, "inherit", None) for t in TASKS}, main)


__all__ = [
    "CODES",
    "ENV_PREFIX",
    "INHERIT",
    "SOURCES",
    "TASKS",
    "RoutedModel",
    "TaskInfo",
    "TaskModelError",
    "TaskModelRouter",
    "TaskRoute",
    "check_config",
    "describe_routes",
    "env_name",
    "inherit_router",
    "parse_ref",
    "resolve_routes",
]
