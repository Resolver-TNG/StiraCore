"""A minimal Strands Agent wrapper with Stira turn-level safeguards.

Tool restrictions are name-based. Applications must configure every
write-capable tool name, including aliases, in ``blocked_tool_names``.
This wrapper does not sandbox tools or implement per-tool confirmation.
Until a confirmation gateway exists, WARNING also refuses write tools at the call
(#259: they stay in the tool list so the cached prefix does not change).
"""

import copy
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from strands import Agent
from strands.models import Model
from strands.models.bedrock import BedrockModel
from strands.types.exceptions import MaxTokensReachedException

from stiracore.agent import context_management as _context
from stiracore.agent import prompt_cache as _cache
from stiracore.agent.checkpoint_hooks import CheckpointHooks, read_policy
from stiracore.agent.context_management import ContextManagementSettings, StashStorage
from stiracore.agent.prompt_cache import PromptCacheSettings, PromptParts
from stiracore.agent.repeated_failure import RepeatedFailureHook
from stiracore.agent.soul_compaction import SoulAgentState, SoulCompactionGuard
from stiracore.agent.stimulus_classifier import NO_UPDATE_REASONS
from stiracore.core import (
    EMOTION_MATRIX,
    BreakAction,
    HITLDecision,
    HITLRequest,
    NeuralState,
    StiraConfig,
    StiraEngine,
)
from stiracore.core.approval import OperationLedger
from stiracore.core.checkpoint import Checkpoint, CheckpointStore
from stiracore.core.repeated_failure import RepeatedFailureCounter, RepeatedFailureSettings
from stiracore.identity_paths import WritePathPolicy
from stiracore.middleware.identity_guard import (
    IdentityGuard,
    breaker_withholds_writes,
    breaker_write_names,
)
from stiracore.providers import ModelSpec, create_model, provider_of

# The stash directory is context_stash_dir/<agent_id>; nothing else is accepted.
_SAFE_DIR_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# ``callback_handler`` not given: the SDK's own default (it prints the streamed text).
_SDK_DEFAULT_CALLBACK = object()


def prompt_text(prompt: Any) -> str:
    """Text-only projection for classifiers and episodic memory (never image bytes)."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(
            block["text"]
            for block in prompt
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        )
    return ""


def _breaker_tool_notice(breaker: Any) -> str:
    """#259: one fixed line for the runtime (volatile) block while the breaker refuses
    write tools; "" otherwise (NORMAL, COHERENCE, CRITICAL with ``halt``)."""
    if not breaker_withholds_writes(breaker.actions):
        return ""
    level = "WARNING" if breaker.actions & BreakAction.REQUIRE_CONFIRMATION else "CRITICAL"
    return (
        f"Circuit breaker {level}: write and exec tool calls are refused "
        "(breaker_write_refused) until stability recovers; read tools keep working."
    )


def _tool_name(tool: Any) -> str | None:
    """Resolve names used by Strands functions, tool objects, and tool specs."""
    name = getattr(tool, "tool_name", None)
    if isinstance(name, str) and name:
        return name

    for attribute in ("tool_spec", "TOOL_SPEC"):
        spec = getattr(tool, attribute, None)
        if isinstance(spec, Mapping):
            name = spec.get("name")
            if isinstance(name, str) and name:
                return name

    if isinstance(tool, Mapping):
        spec = tool.get("toolSpec", tool)
        if isinstance(spec, Mapping):
            name = spec.get("name")
            if isinstance(name, str) and name:
                return name

    if callable(tool):
        name = getattr(tool, "__name__", None)
        if isinstance(name, str) and name:
            return name

    return None


# #745: after a call cut off by ``max_tokens`` the SDK replaces each half-written tool use
# with one sentence ("... tool use was incomplete due to maximum token limits ..."). That
# does not tell the model that the call was not run, nor what to do, so the same oversized
# call came again. This fixed text (and the cap, a number) is appended to those blocks;
# nothing of the cut arguments is copied.
_SDK_CUT_TOOL_USE = "incomplete due to maximum token limits"
CUT_OFF_NOTE_MARKER = "[stira:cut_off]"


def cut_off_note(limit: Any = None) -> str:
    cap = f" ({limit} tokens)" if type(limit) is int and limit > 0 else ""
    return (
        f"{CUT_OFF_NOTE_MARKER} This tool call was cut off by the output limit{cap} before"
        " its arguments were complete, so it was not run and nothing was written. Sending"
        " the same call again will be cut off again. Redo it in smaller pieces: for a long"
        " file, write a first part and add the rest in further, shorter calls."
    )


def annotate_cut_off_note(message: Any, limit: Any = None) -> bool:
    """Append :func:`cut_off_note` to the SDK's cut-tool-use blocks of an assistant message.

    In place; True when a block was changed. Any other message, and a block that already
    carries the note, is left as it is.
    """
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return False
    changed = False
    for block in message.get("content") or []:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        text = block["text"]
        if _SDK_CUT_TOOL_USE in text and CUT_OFF_NOTE_MARKER not in text:
            block["text"] = f"{text}\n{cut_off_note(limit)}"
            changed = True
    return changed


class NXPAgent:
    """Wrap a Strands Agent with Stira pre/post-turn hooks.

    The model is Bedrock by default (``model_id`` + ``region_name``, unchanged
    since the first release). Pass ``model=`` with a :class:`ModelSpec` (built
    through ``stiracore.providers.create_model``) or any Strands ``Model``
    instance to use another provider (#204).

    The inner Agent is created lazily. Its tool registry is rebuilt each turn
    using public constructor arguments, while conversation messages are
    preserved. This avoids stale write restrictions and accumulated identity
    injections without mutating Strands registry internals.

    Instances are intended for sequential calls, not concurrent turns.
    """

    def __init__(
        self,
        model_id: str | None = None,
        region_name: str | None = None,
        state: NeuralState | None = None,
        config: StiraConfig | None = None,
        on_breaker: Callable[..., None] | None = None,
        on_hitl_request: Callable[[HITLRequest], HITLDecision] | None = None,
        tools: Iterable[Any] | None = None,
        system_prompt: str = "",
        stimulus_classifier: Any | None = None,
        system_prompt_provider: Callable[[], str] | None = None,
        max_output_tokens: int = 8192,
        session_manager: Any | None = None,
        agent_id: str = "nxp-agent",
        read_only_paths: Iterable[str] = (),
        checkpoint_store: CheckpointStore | None = None,
        extra_hooks: Iterable[Any] | None = None,
        model: Model | ModelSpec | None = None,
        operation_ledger: OperationLedger | None = None,
        write_policy: WritePathPolicy | None = None,
        prompt_cache: PromptCacheSettings | None = None,
        prompt_parts_provider: Callable[[], PromptParts] | None = None,
        context_management: ContextManagementSettings | None = None,
        context_stash_dir: str | Path | None = None,
        tool_policy: Callable[[str], str] | None = None,
        callback_handler: Any = _SDK_DEFAULT_CALLBACK,
        send_allowed_tools_only: bool = False,
        repeated_failure: RepeatedFailureSettings | None = None,
    ) -> None:
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        if model is None and model_id is None:
            raise ValueError("model_id is required unless model= is given")
        if model is not None and not isinstance(model, (Model, ModelSpec)):
            raise TypeError("model must be a strands Model or a ModelSpec")
        if model is not None and region_name is not None:
            raise ValueError(
                "region_name only applies to the Bedrock shorthand; with model=, "
                "set it in ModelSpec.options['region_name']"
            )
        if isinstance(model, ModelSpec) and model_id is not None and model_id != model.model_id:
            raise ValueError("model_id conflicts with ModelSpec.model_id")
        self.session_manager = session_manager
        self.agent_id = agent_id
        self._session_initialized = False
        self.max_output_tokens = max_output_tokens
        self.state = state if state is not None else NeuralState()
        persistence = getattr(session_manager, "state_persistence", None)
        if persistence is not None and persistence.state is not self.state:
            raise ValueError("Session persistence must reference this agent's NeuralState")
        self.config = config if config is not None else StiraConfig()
        self.engine = StiraEngine(
            state=self.state,
            config=self.config,
            on_breaker=on_breaker,
            on_hitl_request=on_hitl_request,
            agent_id=agent_id,
            checkpoint_store=checkpoint_store,
            operation_ledger=operation_ledger,
        )
        self._restored_messages: list[dict[str, Any]] | None = None
        self.read_only_paths = frozenset(read_only_paths)
        self.identity_guard = IdentityGuard(
            self.engine, read_only_paths=self.read_only_paths, path_policy=write_policy
        )
        # Floor + write roots shared by IdentityGuard and the HITL gate (#243).
        self.write_policy = self.identity_guard.path_policy
        # ``tool_policy``: live Always/Ask/Never rule per tool name (#190, ADR 0010).
        self.checkpoint_hooks = CheckpointHooks(
            self.engine,
            path_check=self.identity_guard.path_denial,
            policy_check=tool_policy,
            # YOLO's scope (ADR 0012 phase 5a): only an explicit policy names a workspace.
            scope_policy=self.write_policy if write_policy is not None else None,
        )
        # An explicit policy also reaches the execution boundary: every file
        # tool this agent holds is wrapped with it (#243, like delegates).
        self._wrap_file_tools = write_policy is not None
        # #368 (off by default): each turn, send only the tools the live rule allows
        # (Always / Ask) and whose name resolves. Removal only; the call-time gate is unchanged.
        self.send_allowed_tools_only = send_allowed_tools_only is True
        # Per-task model routing (docs/task-models.md): a TaskModelRouter, or None (every
        # side task on ``self.model``). Set by the server after construction.
        self.task_models: Any = None
        self.tools = tuple(tools) if tools is not None else ()
        self.system_prompt = system_prompt
        # Optional per-turn base prompt, called after pre_turn so that any
        # breaker snapshot it reads (engine.breaker_status()) is this turn's
        # cached restriction snapshot — the same one used to filter tools.
        self.system_prompt_provider = system_prompt_provider
        # model_spec is None only for an injected Model instance; provider and
        # model_id are display/routing metadata and never hold credentials.
        self.model_spec: ModelSpec | None
        self._legacy_bedrock = model is None
        if model is None:
            try:
                self.model_spec = ModelSpec.bedrock(model_id, region_name=region_name)
            except ValueError:
                # Legacy callers were never validated here; keep accepting them.
                self.model_spec = None
            self.provider = "bedrock"
            self.model_id = model_id
            self.region_name = region_name
            self.model = BedrockModel(
                model_id=model_id,
                region_name=region_name,
                max_tokens=max_output_tokens,
            )
        elif isinstance(model, ModelSpec):
            self.model_spec = model
            self.provider = model.provider
            self.model_id = model.model_id
            self.region_name = model.region_name
            self.model = create_model(model, max_tokens=max_output_tokens)
        else:
            self.model_spec = None
            self.provider = provider_of(model)
            config = model.get_config()
            configured_id = config.get("model_id") if isinstance(config, Mapping) else None
            self.model_id = model_id or (
                configured_id if isinstance(configured_id, str) else type(model).__name__
            )
            self.region_name = None
            self.model = model
        self.agent: Agent | None = None
        self.compaction_guard = SoulCompactionGuard()
        self._compaction_state: SoulAgentState | None = None
        # Stimulus classifier drives per-turn Neural State updates (#63/#65).
        # None = resolve lazily on first use (sokudan if available, else the
        # rule-based fallback), then reuse — avoids reloading the model each turn.
        self._stimulus_classifier = stimulus_classifier
        self._classifier_resolved = stimulus_classifier is not None
        self._last_turn_summary: dict[str, Any] | None = None
        # Turns since construction that moved / did not move Neural State, by reason
        # (#367). Display only: fixed codes, read by the status view.
        self._emotion_updates: dict[str, Any] = {
            "turns": 0,
            "updated": 0,
            "reasons": dict.fromkeys(NO_UPDATE_REASONS, 0),
            "last_reason": None,
        }
        self._last_failed_usage: dict[str, int] | None = None
        self._last_cut_off: dict[str, bool] | None = None
        self._turn_agent: Agent | None = None
        # Optional additional Strands HookProviders (e.g. dashboard/UI event hooks).
        self._extra_hooks = list(extra_hooks or ())
        # Repeated-failure indicator (#151): off unless settings with enabled=True are
        # given. On, a hook counts consecutive tool errors and a fixed notice is prepended
        # with the identity notice at the next turn's start while the count is at the
        # threshold. Observe + prompt only: no gate, approval or breaker input changes.
        if repeated_failure is not None and not isinstance(
            repeated_failure, RepeatedFailureSettings
        ):
            raise TypeError("repeated_failure must be RepeatedFailureSettings")
        self._repeated_failure: RepeatedFailureCounter | None = None
        if repeated_failure is not None and repeated_failure.enabled:
            self._repeated_failure = RepeatedFailureCounter(repeated_failure.threshold)
            self._extra_hooks.append(RepeatedFailureHook(self._repeated_failure))
        # Prompt caching (#254): opt-in. prompt_parts_provider (normally
        # ContextAssembler.build_prompt_parts) supplies the static/volatile split;
        # it is called after pre_turn, like system_prompt_provider.
        self.prompt_cache = prompt_cache if prompt_cache is not None else PromptCacheSettings()
        self.prompt_parts_provider = prompt_parts_provider
        # ADR 0026 B1: called with the tools this turn sends and the identity notice this
        # turn prepends ("" when none), before the request is built; may raise to fail the
        # turn (ContextAssembler.check_protected_fits). None = no check.
        self.send_boundary_check: Callable[[list[Any], str], None] | None = None
        self._last_prompt_cache: dict[str, Any] | None = None
        self._cached_prefix_tokens = 0
        self._refusal_warned = False
        # Context management (#258): opt-in offload of large tool results to a
        # durable stash under context_stash_dir/<agent_id> + threshold summarization.
        self.context_management = (
            context_management if context_management is not None else ContextManagementSettings()
        )
        self.context_stash_dir = Path(context_stash_dir) if context_stash_dir else None
        self._stash: StashStorage | None = None
        self._stash_purged_at = 0.0
        self._context_warned: set[str] = set()
        self._context_refusal: str | None = None
        self._last_context_management: dict[str, Any] | None = None
        # Passed to every SDK Agent when given. The SDK default prints the model's
        # streamed text and tool names to stdout (unmasked, e.g. into a server log);
        # servers pass None (#185 integration review, 2026-10-15).
        self._callback_kwargs = (
            {}
            if callback_handler is _SDK_DEFAULT_CALLBACK
            else {"callback_handler": callback_handler}
        )

    @property
    def tools(self) -> tuple[Any, ...]:
        return self._tools

    @tools.setter
    def tools(self, tools: Iterable[Any]) -> None:
        """Assigning tools (also per turn) wraps file tools with ``write_policy``.

        Wrapping nests: an already wrapped tool keeps its own floor/roots and
        gains these, so nothing is ever weakened. Tools already wrapped with
        this agent's policy are not wrapped twice.
        """
        tools = tuple(tools)
        if getattr(self, "_wrap_file_tools", False):
            from stiracore.tools.identity_tools import PATH_WRITE_TOOLS, IdentityProtectedTool

            tools = tuple(
                IdentityProtectedTool(tool, policy=self.write_policy)
                if getattr(tool, "tool_name", None) in PATH_WRITE_TOOLS
                and not (
                    isinstance(tool, IdentityProtectedTool) and tool.policy is self.write_policy
                )
                else tool
                for tool in tools
            )
        self._tools = tools

    @property
    def capabilities(self) -> Any:
        """``ModelCapabilities`` for the configured model (#206; unknown = conservative)."""
        from stiracore.providers.capabilities import UNKNOWN, capabilities_for

        try:
            if self.model_spec is not None:
                return capabilities_for(self.model_spec)
            if self.provider != "custom":
                return capabilities_for(self.provider, str(self.model_id))
        except (TypeError, ValueError):
            pass
        return UNKNOWN

    @property
    def last_turn_summary(self) -> dict[str, Any] | None:
        """Return a detached summary for the most recently finished turn."""
        return deepcopy(self._last_turn_summary)

    @property
    def emotion_updates(self) -> dict[str, Any]:
        """Detached counts of finished turns with / without a Neural State update (#367).

        ``reasons`` counts the turns without one by ``NO_UPDATE_REASONS`` code;
        ``last_reason`` is the newest turn's code (None = it updated, or no turn yet).
        """
        return deepcopy(self._emotion_updates)

    @property
    def last_failed_usage(self) -> dict[str, int] | None:
        """Usage the provider reported for a turn that then raised (#363), else None.

        A model call that ends in ``max_tokens`` is billed, but the exception carries no
        result. The SDK has already added that call's metadata usage to the agent's
        metrics, so the counts survive here (camelCase keys, as the SDK reports them).
        """
        return dict(self._last_failed_usage) if self._last_failed_usage else None

    @property
    def last_cut_off(self) -> dict[str, bool] | None:
        """What the call cut off by ``max_tokens`` had produced (#416), else None.

        ``before_reply``: neither text nor a tool call was started — the cap went on thinking
        (``reasoning``: a reasoning block was streamed) or on output the provider does not
        show (an empty message). Read from the partial message the SDK keeps in the history.
        """
        return dict(self._last_cut_off) if self._last_cut_off else None

    def _remember_cut_off(self, exc: BaseException) -> None:
        self._last_cut_off = None
        if not isinstance(exc, MaxTokensReachedException):
            return
        try:
            turn_agent = self._turn_agent
            messages = turn_agent.messages if turn_agent is not None else []
            last = messages[-1] if messages else None
            if not isinstance(last, Mapping) or last.get("role") != "assistant":
                return
            blocks = [b for b in last.get("content") or [] if isinstance(b, Mapping)]
            # A cut tool call is already replaced by a text note (SDK recovery): it counts as
            # started output, like text.
            started = any(
                "toolUse" in b or (isinstance(b.get("text"), str) and b["text"].strip())
                for b in blocks
            )
            reasoning = any("reasoningContent" in b for b in blocks)
            # #745: tell the model why its call did not run and what to do instead.
            annotate_cut_off_note(last, self.max_output_tokens)
        except Exception:  # noqa: BLE001 - telemetry must never mask the turn's own error
            return
        self._last_cut_off = {"before_reply": not started, "reasoning": reasoning}

    def _remember_failed_usage(self) -> None:
        self._last_failed_usage = None
        try:
            # Only this turn's Agent: before it exists, self.agent is the previous turn's
            # and its metrics were counted then.
            turn_agent = self._turn_agent
            usage = turn_agent.event_loop_metrics.accumulated_usage if turn_agent else None
            kept = {
                key: usage[key]
                for key in (
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                    "cacheWriteInputTokens",
                )
                if usage is not None and type(usage.get(key)) is int and usage[key] >= 0  # type: ignore[index]
            }
        except Exception:  # noqa: BLE001 - telemetry must never mask the turn's own error
            return
        # All-zero = the provider reported nothing before failing: not a usage record.
        if kept.get("inputTokens") or kept.get("outputTokens"):
            self._last_failed_usage = kept

    @property
    def last_prompt_cache(self) -> dict[str, Any] | None:
        """Prompt-prefix record of the latest turn (#254): mode, hashes, what changed."""
        return deepcopy(self._last_prompt_cache)

    @property
    def last_context_management(self) -> dict[str, Any] | None:
        """Context-management record of the latest turn (#258): mode, offloads, summaries."""
        return deepcopy(self._last_context_management)

    @property
    def context_stash(self) -> StashStorage | None:
        """This agent's offload stash (``context_stash_dir/<agent_id>``), once opened."""
        return self._stash

    @property
    def context_refusal_reason(self) -> str | None:
        """Why context management was last refused: a ``REFUSAL_REASONS`` code, or ``None``."""
        return self._context_refusal

    def server_context_window(self, *, cached_only: bool = False) -> int | None:
        """The window a local OpenAI-compatible server reports for this model (#746).

        ``None`` for every other provider or address and when nothing is reported.
        ``cached_only`` never sends a request (status views).
        """
        from stiracore.providers.server_window import reported_context_window

        return reported_context_window(self.model_spec, cached_only=cached_only)

    def _context_refused(self, reason: str) -> str:
        """Refuse with a fixed code (#746): the log names the fix, never a path."""
        self._context_refusal = reason
        if reason not in self._context_warned:
            self._context_warned.add(reason)
            logging.getLogger(__name__).warning(
                "context_management is enabled but refused (%s); history is managed as before: "
                "nothing is offloaded or summarized. To fix: %s",
                reason,
                _context.REFUSAL_REASONS[reason],
            )
        return "refused"

    def _context_mode(self) -> str:
        """``off`` | ``unsupported`` | ``refused`` | ``on``.

        ``refused`` (fail-closed, warned once, ``context_refusal_reason``): no stash
        directory, or one on the identity floor, or one that is neither inside the write
        roots (#243) nor the server's ``<data dir>/context-stash/`` (#746). Offloading
        without a durable stash would drop content the model cannot read back.
        ``unsupported``: a stateful model keeps its history server-side.
        """
        settings = self.context_management
        self._context_refusal = None
        if not settings.enabled:
            return "off"
        if getattr(self.model, "stateful", False):
            return "unsupported"
        if self.context_stash_dir is None:
            return self._context_refused("no_stash_dir")
        if not _SAFE_DIR_NAME.fullmatch(self.agent_id) or self.agent_id in (".", ".."):
            return self._context_refused("agent_id_not_plain")
        directory = self.context_stash_dir / self.agent_id
        policy = self.write_policy
        within = getattr(policy, "within_roots", None)
        try:
            # Never on the floor. Inside the write roots (#243), or the server's own
            # <data dir>/context-stash/ (#746): the offloader writes there itself, so a
            # workspace_dir outside the data directory does not switch the feature off,
            # and the agent's file tools gain no write root.
            if (
                within is None
                or policy.is_read_only(directory)
                or not (within(directory) or _context.internal_stash(policy, directory))
            ):
                return self._context_refused("stash_outside_write_roots")
            if self._stash is None or self._stash.directory != directory:
                self._stash = StashStorage(directory)
        except (OSError, RuntimeError, ValueError):
            return self._context_refused("stash_unusable")
        now = time.time()
        if now - self._stash_purged_at >= 60:
            self._stash_purged_at = now
            self._stash.purge_expired(settings.retention_hours, now=now)
        return "on"

    def _cache_mode(self) -> str:
        """``off`` | ``unsupported`` | ``split`` | ``static`` | ``refused``.

        Only an eligible model (a Bedrock model whose capabilities include
        ``prompt_cache``: Claude) gets anything new; ``unsupported`` models get
        the legacy prompt, exactly as with the setting off. ``static``: a fixed
        system prompt that may be cached as is. ``refused``: the system prompt
        is rebuilt every turn without a static/volatile split, which would only
        write cache entries and never read them (#254 P1); a warning is logged
        once and the legacy prompt is used.
        """
        if not self.prompt_cache.enabled:
            return "off"
        if not _cache.split_eligible(self.prompt_cache, self.model, self.capabilities):
            return "unsupported"
        if self.prompt_parts_provider is not None:
            return "split"
        dynamic = self.system_prompt_provider is not None or (
            getattr(self.session_manager, "system_prompt_provider", None) is not None
        )
        if not dynamic:
            return "static"
        if not self._refusal_warned:
            self._refusal_warned = True
            logging.getLogger(__name__).warning(
                "prompt_cache is enabled but the system prompt is rebuilt every turn without a "
                "static/volatile split (set prompt_parts_provider); prompt caching stays off"
            )
        return "refused"

    def _turn_model(self, cached: bool, context_window: int | None = None) -> Any:
        """The model for this turn's Agent: ``self.model``, or a configured copy.

        The configured model is never mutated, so nothing has to be restored
        after a model switch or when caching is turned off, and a model shared
        with other wrappers never sees this agent's cache settings (#254 M2).
        The copy shares the client and differs only in its own config dict:
        ``cache_config`` (#254) and/or ``context_window_limit`` (#258, the
        window context management measures utilization against).
        """
        updates: dict[str, Any] = {}
        if cached:
            config = _cache.cache_config_for(self.prompt_cache, self.model, self.capabilities)
            if config is not None:
                updates["cache_config"] = config
        if (
            context_window is not None
            and isinstance(getattr(self.model, "config", None), dict)
            and getattr(self.model, "context_window_limit", None) != context_window
        ):
            updates["context_window_limit"] = context_window
        if not updates:
            return self.model
        model = copy.copy(self.model)
        model.config = {**self.model.config, **updates}
        return model

    def _record_prefix(
        self,
        mode: str,
        active: bool,
        system_prompt: str,
        volatile: str,
        messages: list[Any],
    ) -> None:
        """Fingerprint the request prefix so a cache miss can be explained per turn.

        Parts, in Bedrock cache order: ``tools``, ``system`` (static system),
        ``messages`` (the history must extend the previous turn's history), and
        ``volatile`` (the runtime block; expected to change, it sits behind the
        last cache point). ``changed`` lists the cached parts that differ from
        the previous turn.
        """
        try:
            tool_specs = self.agent.tool_registry.get_all_tool_specs()
            tools_hash: str | None = _cache.digest(tool_specs)
            tool_count: int | None = len(tool_specs)
        except Exception:  # noqa: BLE001 - telemetry must never fail a turn
            tools_hash, tool_count = None, None
        previous = self._last_prompt_cache or {}
        history_hash = _cache.digest(messages)
        now = time.time()
        record = {
            "mode": mode,
            "active": active,
            "ttl": self.prompt_cache.ttl if active else None,
            "tools_hash": tools_hash,
            "tool_count": tool_count,
            "system_hash": _cache.digest(system_prompt or ""),
            "system_chars": len(system_prompt or ""),
            "volatile_hash": _cache.digest(volatile) if volatile else None,
            "volatile_chars": len(volatile),
            "messages_hash": history_hash,
            "elapsed_sec": (
                round(now - previous["started_at"], 1) if "started_at" in previous else None
            ),
            "started_at": now,
        }
        changed = [
            part
            for part in ("tools", "system")
            if f"{part}_hash" in previous and previous[f"{part}_hash"] != record[f"{part}_hash"]
        ]
        # The history must start with exactly what the previous turn ended with.
        expected_history = previous.get("history_after_hash")
        if expected_history is not None and expected_history != history_hash:
            changed.append("messages")
        if "volatile_hash" in previous and previous["volatile_hash"] != record["volatile_hash"]:
            changed.append("volatile")  # informational: behind the last cache point
        record["changed"] = changed
        self._last_prompt_cache = record

    def _observe_cache(self, result: Any) -> None:
        """Record this turn's cache usage and log a cache break with its likely cause."""
        record = self._last_prompt_cache
        if record is None:
            return
        try:
            record["history_after_hash"] = _cache.digest(self.agent.messages)
            usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
            read = usage.get("cacheReadInputTokens")
            write = usage.get("cacheWriteInputTokens")
        except Exception:  # noqa: BLE001 - telemetry must never fail a turn
            return
        if not isinstance(read, int) or not isinstance(write, int):
            return
        record["cache_read_tokens"], record["cache_write_tokens"] = read, write
        expected = self._cached_prefix_tokens
        self._cached_prefix_tokens = read + write
        if not record.get("active") or not expected:
            return
        # A warm turn re-reads (at least) what the previous turn read + wrote.
        if read >= expected * 0.5:
            record["cache_break"] = False
            return
        reasons = [part for part in record["changed"] if part != "volatile"]
        ttl_sec = 3600 if record.get("ttl") == "1h" else 300
        if record.get("elapsed_sec") is not None and record["elapsed_sec"] > ttl_sec:
            reasons.append("ttl_expired")
        record["cache_break"] = True
        record["cache_break_reasons"] = reasons or ["unknown"]
        logging.getLogger(__name__).warning(
            "prompt cache break: read %d of ~%d cached tokens; changed=%s",
            read,
            expected,
            ",".join(record["cache_break_reasons"]),
        )

    def _observe_context(self, mode: str, history_before: int) -> None:
        """Record what context management did this turn (telemetry; never fails a turn)."""
        record: dict[str, Any] = {"mode": mode}
        try:
            if mode == "on":
                manager = self.agent.conversation_manager
                record.update(_context.history_markers(self.agent.messages))
                record["summarized_messages"] = int(getattr(manager, "removed_message_count", 0))
                record["history_messages"] = len(self.agent.messages)
                record["history_before"] = history_before
                record["stash_entries"] = self._stash.entry_count() if self._stash else 0
        except Exception:
            logging.getLogger(__name__).debug("context telemetry failed", exc_info=True)
        self._last_context_management = record

    def _prepare_split_turn(self, breaker: Any) -> tuple[list[Any], str, str, str]:
        """Split-mode turn policy: name-sorted tools, static system, control, recalled."""
        parts = self.prompt_parts_provider()
        turn_tools = _cache.sort_tools(self._turn_tools(breaker), _tool_name)
        system_prompt = self._with_identity(breaker, _cache.static_system_prompt(parts.static))
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        volatile = "\n\n".join(
            part
            for part in (parts.volatile.strip(), _breaker_tool_notice(breaker), f"## Time\n{now}")
            if part
        )
        return turn_tools, system_prompt, volatile, parts.recalled.strip()

    def _prepare_turn(self, breaker: Any) -> tuple[list[Any], str]:
        """Apply the existing turn policy for sync and SDK streaming execution."""
        base_prompt = (
            self.system_prompt_provider()
            if self.system_prompt_provider is not None
            else self.system_prompt
        )
        return self._turn_tools(breaker), self._with_identity(breaker, base_prompt)

    def _turn_tools(self, breaker: Any) -> list[Any]:
        """This turn's tools. #259: the breaker no longer removes write tools here (the
        list, and so the cached prefix, is the same at every level); ``IdentityGuard``
        refuses their calls with ``breaker_write_refused``. While the breaker withholds
        writes, a tool whose name cannot be resolved is still left out (fail closed: the
        call-time check decides by name)."""
        turn_tools = list(self.tools)
        # The names refused by the breaker as of this turn's start (IdentityGuard and the
        # gate's breaker refusal both read it, so a tool closed mid-turn stays refused).
        names = frozenset(breaker_write_names(self.engine))
        self.identity_guard.turn_write_names = names
        self.checkpoint_hooks.turn_write_names = names
        if breaker_withholds_writes(breaker.actions):
            turn_tools = [tool for tool in turn_tools if _tool_name(tool) is not None]
        if getattr(self, "send_allowed_tools_only", False):
            turn_tools = self._allowed_tools(turn_tools)
        return turn_tools

    def _allowed_tools(self, turn_tools: list[Any]) -> list[Any]:
        """#368: drop what the model could not call: a tool whose live rule is Never (an
        unreadable rule counts as Never, ``read_policy``) or whose name does not resolve.
        Read once per tool at the turn's start, so a saved rule applies from the next turn.
        Without a rule reader every resolvable tool stays. Only removes; the kept tools are
        the same objects (same schema), and every call still goes through the gate."""
        policy_check = self.checkpoint_hooks.policy_check
        kept = []
        for tool in turn_tools:
            name = _tool_name(tool)
            if name is None:
                continue
            read = read_policy(policy_check, name)
            if read is not None and read[0] == "never":
                continue
            kept.append(tool)
        return kept

    def _identity_notice(self, breaker: Any) -> str:
        """The live identity notice this turn prepends, or "" when the breaker does not."""
        if breaker.actions & BreakAction.INJECT_IDENTITY:
            return self.engine.identity_injection() or ""
        return ""

    def _calm_notice(self) -> str:
        """The repeated-failure notice (#151) this turn prepends; "" when off or below."""
        counter = getattr(self, "_repeated_failure", None)
        return counter.calm_notice() if counter is not None else ""

    def _turn_notice(self, breaker: Any) -> str:
        """Everything this turn prepends to the base prompt: identity, then calm."""
        return "\n\n".join(
            part for part in (self._identity_notice(breaker), self._calm_notice()) if part
        )

    def _with_identity(self, breaker: Any, base_prompt: str) -> str:
        """Prepend the live identity notice while the breaker injects identity."""
        notice = self._turn_notice(breaker)
        if notice:
            return f"{notice}\n\n{base_prompt}" if base_prompt else notice
        return base_prompt

    def _snapshot_context(self, agent: Agent, system_prompt: str) -> None:
        """Capture the trusted prepared prompt, not restored conversation content."""
        self._compaction_state = SoulAgentState(system_prompt, self.engine, agent)
        self.compaction_guard.snapshot(self._compaction_state)

    def _finish_turn(self, prompt: Any, result: Any, breaker: Any, completed: bool) -> None:
        """Close a turn exactly once, including failed and cancelled invocations."""
        self._last_turn_summary = None
        # Derive stimulus from conversation via the pluggable classifier
        # (#63 rule-based / #65 sokudan). Resolve once, then reuse.
        _stim = None
        classify_failed = False
        if completed:
            try:
                from stiracore.agent.stimulus_classifier import coerce_stimulus_result

                classifier = self._get_classifier()
                _stim = coerce_stimulus_result(
                    classifier.classify(
                        user_message=prompt_text(prompt),
                        agent_response=str(result) if result is not None else "",
                        breaker_result=breaker,
                    ),
                    classifier_name=str(
                        getattr(classifier, "name", "") or type(classifier).__name__
                    ),
                )
            except Exception:
                # Any non-fatal classifier failure (ImportError, RuntimeError,
                # ConnectionError, ...) is treated as abstain so post_turn always
                # runs once and the original turn outcome is never masked.
                # KeyboardInterrupt/SystemExit (BaseException) still propagate.
                logging.getLogger(__name__).warning("Stimulus detection failed", exc_info=True)
                _stim = None
                classify_failed = True
        summary = None
        if completed:
            if _stim is not None:
                summary = self.engine.post_turn(
                    stimulus=_stim.category,
                    intensity=_stim.intensity,
                    transcription=_stim.transcription,
                )
            else:
                summary = self.engine.post_turn(stimulus=None, intensity=0.0)
        else:
            summary = self.engine.post_turn(stimulus=None)
        if isinstance(summary, Mapping):
            self._last_turn_summary = deepcopy(dict(summary))
            self._last_turn_summary["category"] = _stim.category if _stim is not None else None
            # Classifier verdict details for the turn ribbon (#187); None = not recorded.
            self._last_turn_summary["intensity"] = _stim.intensity if _stim is not None else None
            self._last_turn_summary["confidence"] = _stim.confidence if _stim is not None else None
            self._last_turn_summary["classifier"] = (
                (_stim.classifier_name or None) if _stim is not None else None
            )
            if not self._last_turn_summary.get("deltas"):
                # post_turn intentionally retains the last applied rationale in state.
                # A turn-scoped summary must not attribute that stale rationale to abstain.
                self._last_turn_summary["emotion_transcription"] = None
            reason = self._no_update_reason(
                self._last_turn_summary.get("deltas"), completed, classify_failed, _stim
            )
            self._last_turn_summary["no_update_reason"] = reason
            self._record_emotion_update(reason)
            counter = getattr(self, "_repeated_failure", None)
            if counter is not None:
                self._last_turn_summary["repeated_failure"] = counter.snapshot()
        else:
            self._last_turn_summary = None
        if self._compaction_state is not None:
            context = self._compaction_state
            self._compaction_state = None
            self.compaction_guard.check_and_reinject(context, context.agent.messages)

    @staticmethod
    def _no_update_reason(
        deltas: Any, completed: bool, classify_failed: bool, stim: Any
    ) -> str | None:
        """The fixed ``NO_UPDATE_REASONS`` code for a turn that moved nothing (#367).

        "Moved" means a non-zero delta, not merely a key in ``deltas``.
        """
        moved = isinstance(deltas, Mapping) and any(
            isinstance(value, int | float) and value != 0 for value in deltas.values()
        )
        if moved:
            return None
        if not completed:
            return "turn_incomplete"
        if classify_failed:
            return "classifier_error"
        if stim is None:
            return "no_stimulus"
        if stim.category not in EMOTION_MATRIX:
            return "unknown_stimulus"
        return "no_change"

    def _record_emotion_update(self, reason: str | None) -> None:
        counts = self._emotion_updates
        counts["turns"] += 1
        if reason is None:
            counts["updated"] += 1
        else:
            counts["reasons"][reason] += 1
        counts["last_reason"] = reason

    def resume_from_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        """Explicitly restore a generation; call this agent with None to replay.

        Tools execute again under current breaker/identity policy. This is not
        exactly-once execution or an approval override. Review side effects first.
        HITL-approved operations are the exception (#146): the operation ledger
        skips ones already committed and refuses ones whose outcome is unknown. So are
        write-capable calls and ``delegate`` that ran without an approval (ADR 0024 §5);
        read-only calls run again.
        """
        checkpoint = self.engine.resume_from_checkpoint(checkpoint_id)
        # ADR 0024 §5: these calls are a replay until the next turn that is not one.
        self.checkpoint_hooks.begin_replay(
            str(call.get("toolUseId", "")) for call in checkpoint.pending_tool_calls
        )
        self._restored_messages = deepcopy(checkpoint.conversation_history)
        if self.agent is not None:
            self.agent.messages = deepcopy(checkpoint.conversation_history)
        # An explicit checkpoint wins over automatic latest-session loading.
        self._session_initialized = True
        return checkpoint

    def start_new_conversation(self) -> int:
        """Drop the conversation sent to the model, as a server restart does (#350).

        Only the in-memory history goes: the SDK delegate (rebuilt per turn anyway),
        a restored checkpoint's messages, the engine's copy of the history and the
        per-conversation prompt telemetry. Neural State, the breaker (level, hysteresis,
        cooldown), memory, SOUL, approvals, the operation ledger, checkpoints on disk
        and the tool rules are not touched. The caller serializes this with turns.
        Returns how many messages were dropped. A session manager persists the
        conversation itself; resetting that is not supported (refused, nothing changes).
        """
        if self.session_manager is not None:
            raise RuntimeError("start_new_conversation does not reset a session manager")
        messages = (
            self._restored_messages
            if self._restored_messages is not None
            else self.agent.messages
            if self.agent is not None
            else []
        )
        dropped = len(messages)
        self.agent = None
        self._restored_messages = None
        self.engine.conversation_history = []
        # A fresh process has a fresh guard (it tracks this conversation's history size).
        self.compaction_guard = SoulCompactionGuard()
        self._compaction_state = None
        counter = getattr(self, "_repeated_failure", None)
        if counter is not None:
            counter.reset()  # another task (#151)
        self._last_prompt_cache = None
        self._cached_prefix_tokens = 0
        self._last_context_management = None
        return dropped

    def _checkpoint_turn_history(self, prompt: Any) -> list[dict[str, Any]]:
        messages = deepcopy(
            self._restored_messages
            if self._restored_messages is not None
            else self.agent.messages
            if self.agent is not None
            else []
        )
        if prompt is not None:
            content = [{"text": prompt}] if isinstance(prompt, str) else prompt
            messages.append({"role": "user", "content": deepcopy(content)})
        return messages

    def cancel(self) -> bool:
        """Stop the running turn at the SDK's next safe point (#452, thread-safe).

        ``Agent.cancel()`` of this turn's Strands agent: it stops during the model's stream,
        before a tool or after one, and the turn returns with ``stop_reason="cancelled"``.
        False when this turn has no agent yet (the caller repeats until the turn ends).
        """
        turn_agent = self._turn_agent
        if turn_agent is None:
            return False
        turn_agent.cancel()
        return True

    def __call__(self, prompt: str | list[dict[str, Any]] | None = None) -> Any:
        """Run one guarded turn and return the underlying Strands result."""
        completed = False
        result = None
        breaker = None
        self._last_failed_usage = None
        self._last_cut_off = None
        self._turn_agent = None
        if self._restored_messages is None:
            # A turn that replays no checkpoint: no call of it is a replay (ADR 0024 §5).
            self.checkpoint_hooks.end_replay()
        try:
            if self.session_manager is not None and not self._session_initialized:
                # Restore before pre_turn: breaker evaluation must see latest disk
                # state, not constructor defaults or an old conversation snapshot.
                self.agent = Agent(
                    model=self.model,
                    tools=[],
                    agent_id=self.agent_id,
                    # Accepts a snapshot saved with or without context management (#258).
                    conversation_manager=_context.restore_conversation_manager(),
                    **self._callback_kwargs,
                )
                self.session_manager.initialize(self.agent)
                self._session_initialized = True
            history = self._checkpoint_turn_history(prompt)
            pending = (
                [block["toolUse"] for block in history[-1].get("content", []) if "toolUse" in block]
                if history and history[-1].get("role") == "assistant"
                else []
            )
            breaker = self.engine.pre_turn(conversation_history=history, pending_tool_calls=pending)
            messages = (
                self._restored_messages
                if self._restored_messages is not None
                else self.agent.messages
                if self.agent is not None
                else []
            )
            if self.session_manager is not None:
                # Snapshot managers include system_prompt; refresh it before the
                # turn and then reapply identity injection from current policy.
                provider = self.session_manager.system_prompt_provider
                if provider is not None:
                    self.system_prompt = provider()
            mode = self._cache_mode()
            volatile = recalled = ""
            if mode == "split":
                turn_tools, turn_system_prompt, volatile, recalled = self._prepare_split_turn(
                    breaker
                )
            else:
                turn_tools, turn_system_prompt = self._prepare_turn(breaker)
                if mode == "static":
                    turn_tools = _cache.sort_tools(turn_tools, _tool_name)
            if self.send_boundary_check is not None:
                self.send_boundary_check(turn_tools, self._turn_notice(breaker))
            cache_active = mode in ("split", "static")
            context_mode = self._context_mode()
            turn_model = self._turn_model(
                cache_active,
                _context.resolve_context_window(
                    self.context_management,
                    self.model,
                    self.capabilities,
                    server_window=self.server_context_window,
                )
                if context_mode == "on"
                else None,
            )
            agent_kwargs = {}
            context_hooks: list[Any] = []
            if mode == "split":
                # User text cannot open or close the injected blocks (#254).
                prompt = _cache.neutralize_prompt(prompt)
                from strands.vended_plugins.context_injector import ContextInjector

                # Recalled memory first, control state last; both on every model
                # call of the turn (tool loop too), never persisted into history.
                blocks = (
                    ("stira:recalled-memory", recalled and _cache.render_recalled_memory(recalled)),
                    ("stira:runtime-context", volatile and _cache.render_runtime_context(volatile)),
                )
                agent_kwargs["plugins"] = [
                    ContextInjector(
                        lambda _context, text=text: text, name=name, trigger="everyTurn"
                    )
                    for name, text in blocks
                    if text
                ]
            if context_mode == "on":
                # Fresh components per turn; the stash itself is durable (#258).
                # The summary call uses the configured model, without cache points: the chat
                # model, or the one routed to ``context_summary`` (no fallback when it fails).
                summary_model = (
                    self.task_models.model_for("context_summary", self.model)
                    if self.task_models is not None
                    else self.model
                )
                components = _context.build_turn_components(
                    self.context_management, self._stash, summary_model=summary_model
                )
                agent_kwargs["plugins"] = [
                    *agent_kwargs.get("plugins", []),
                    *components["plugins"],
                ]
                agent_kwargs["conversation_manager"] = components["conversation_manager"]
                context_hooks = components["hooks"]
            if self.session_manager is not None:
                agent_kwargs.update(
                    agent_id=self.agent_id,
                    state=self.agent.state.get() if self.agent is not None else {},
                )
            self.agent = Agent(
                model=turn_model,
                tools=turn_tools,
                system_prompt=turn_system_prompt,
                messages=messages,
                hooks=[
                    self.checkpoint_hooks,
                    self.identity_guard,
                    *self._extra_hooks,
                    *context_hooks,
                    # Last: re-verify HITL approval bindings and claim the ledger (#146).
                    self.checkpoint_hooks.binding_verifier,
                ],
                **agent_kwargs,
                **self._callback_kwargs,
            )
            self._turn_agent = self.agent
            self._restored_messages = None
            self._record_prefix(
                mode,
                cache_active,
                turn_system_prompt,
                "\n".join(part for part in (recalled, volatile) if part),
                messages,
            )
            self._snapshot_context(self.agent, turn_system_prompt)
            history_before = len(messages)
            self._last_context_management = {"mode": context_mode}
            result = self.agent(prompt)
            self._observe_cache(result)
            self._observe_context(context_mode, history_before)
            completed = getattr(result, "stop_reason", None) not in ("interrupt", "cancelled")
            return result
        except Exception as exc:
            self._remember_failed_usage()
            self._remember_cut_off(exc)
            raise
        finally:
            self._finish_turn(prompt, result, breaker, completed)
            if (
                self.session_manager is not None
                and self._session_initialized
                and self.agent is not None
            ):
                persistence = self.session_manager.state_persistence
                if persistence is not None:
                    persistence.save_session(self.agent, self.session_manager)
                else:
                    self.session_manager.sync_agent(self.agent)

    def _get_classifier(self) -> Any:
        """Resolve the stimulus classifier once and cache it.

        A classifier injected at construction is used as-is. Otherwise it is
        resolved lazily (sokudan when available, else the rule-based fallback)
        so the model is loaded at most once, on first use.
        """
        if not self._classifier_resolved:
            from stiracore.agent.stimulus_classifier import get_classifier

            self._stimulus_classifier = get_classifier()
            self._classifier_resolved = True
        return self._stimulus_classifier


def create_nxp_agent(
    model_id: str | None = None,
    region_name: str | None = None,
    state: NeuralState | None = None,
    config: StiraConfig | None = None,
    on_breaker: Callable[..., None] | None = None,
    on_hitl_request: Callable[[HITLRequest], HITLDecision] | None = None,
    tools: Iterable[Any] | None = None,
    system_prompt: str = "",
    stimulus_classifier: Any | None = None,
    system_prompt_provider: Callable[[], str] | None = None,
    max_output_tokens: int = 8192,
    session_manager: Any | None = None,
    agent_id: str = "nxp-agent",
    read_only_paths: Iterable[str] = (),
    checkpoint_store: CheckpointStore | None = None,
    extra_hooks: Iterable[Any] | None = None,
    model: Model | ModelSpec | None = None,
    write_policy: WritePathPolicy | None = None,
    prompt_cache: PromptCacheSettings | None = None,
    prompt_parts_provider: Callable[[], PromptParts] | None = None,
    context_management: ContextManagementSettings | None = None,
    context_stash_dir: str | Path | None = None,
) -> NXPAgent:
    """Create an NXPAgent without invoking the model."""
    return NXPAgent(
        model_id=model_id,
        region_name=region_name,
        state=state,
        config=config,
        on_breaker=on_breaker,
        on_hitl_request=on_hitl_request,
        tools=tools,
        system_prompt=system_prompt,
        stimulus_classifier=stimulus_classifier,
        system_prompt_provider=system_prompt_provider,
        max_output_tokens=max_output_tokens,
        session_manager=session_manager,
        agent_id=agent_id,
        read_only_paths=read_only_paths,
        checkpoint_store=checkpoint_store,
        extra_hooks=extra_hooks,
        model=model,
        write_policy=write_policy,
        prompt_cache=prompt_cache,
        prompt_parts_provider=prompt_parts_provider,
        context_management=context_management,
        context_stash_dir=context_stash_dir,
    )
