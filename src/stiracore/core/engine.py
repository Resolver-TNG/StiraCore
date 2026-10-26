"""Stira Engine — collapse detection and enforceable circuit-breaker policy."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from enum import Enum, IntFlag
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from . import breaker_reasons
from .approval import OperationLedger, args_hash
from .checkpoint import Checkpoint, CheckpointStore, FileCheckpointStore
from .coherence import BC_MIN_SAMPLES
from .neural_state import EMOTION_MATRIX, NeuralState

logger = logging.getLogger(__name__)

MAX_TRANSCRIPTION_LEN = 200
HITL_APPROVED_TOOL_IDS_KEY = "stira.hitl.approved_tool_use_ids"
# #165: while a non-NORMAL level persists, the breaker log is not repeated every turn.
# It is written on a transition and then once per this many consecutive turns at the
# same level (a numbers-only summary). The on_breaker callback is not throttled.
BREAKER_LOG_EVERY_TURNS = 20

# Built-in identity anchors (#152): generic statements with no persona, name or role in
# them. Used only when the configuration does not set its own ``identity_anchors``.
DEFAULT_IDENTITY_ANCHORS: tuple[str, ...] = (
    "Keep one consistent identity across turns; do not trade it for agreement or efficiency",
    "Report your own state and uncertainty honestly",
    "Your SOUL, identity anchors and safety settings change only with human approval",
)


def _sanitize_transcription(transcription: object, stimulus: str, intensity: float) -> str:
    """Single-line, length-capped rationale; fixed fallback when empty."""
    text = " ".join(transcription.split()) if isinstance(transcription, str) else ""
    if not text:
        text = f"{stimulus} (intensity {intensity:.2f})"
    return text[:MAX_TRANSCRIPTION_LEN]


class BreakLevel(Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    COHERENCE = "coherence"


# Strictness of a level for "never looser than" comparisons (#601 B8): what the host
# restricts — CRITICAL blocks writes, WARNING withholds them, COHERENCE only re-injects.
_STRICTNESS = {
    BreakLevel.NORMAL: 0,
    BreakLevel.COHERENCE: 1,
    BreakLevel.WARNING: 2,
    BreakLevel.CRITICAL: 3,
}
# The engine attributes ``_evaluate_breaker`` reads and writes (its latches).
_LATCH_FIELDS = ("_warning_active", "_coherence_active", "_active_level", "_critical_turns")


class BreakAction(IntFlag):
    """Actions for the host's turn executor, prompt builder, and tool router."""

    NONE = 0
    BLOCK_WRITES = 1
    INJECT_IDENTITY = 2
    REQUIRE_CONFIRMATION = 4
    NOTIFY_OPERATOR = 8
    HALT = 16


class HITLAction(Enum):
    """Human decisions available at a breaker-controlled operation boundary."""

    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    RESPOND = "respond"


@dataclass(frozen=True)
class HITLRequest:
    """UI-independent approval request emitted before a write-capable tool call.

    ``StiraEngine.request_hitl`` fills the binding fields (#146): the approval is
    valid only for this agent, tool, normalized-argument hash and expiry.
    ``supersedes`` names the request an operator edit replaced (re-review).
    """

    tool_name: str
    tool_input: dict[str, Any]
    break_level: BreakLevel
    reason: str
    tool_use_id: str | None = None
    agent_id: str | None = None
    request_id: str | None = None
    args_hash: str | None = None
    expires_at: float | None = None
    supersedes: str | None = None


@dataclass(frozen=True)
class HITLDecision:
    """Human response to an HITL request.

    A transport that knows which request the human saw sets ``request_id`` and
    ``args_hash``; the engine refuses a decision bound to anything else.
    """

    action: HITLAction
    edited_input: dict[str, Any] | None = None
    response: str | None = None
    request_id: str | None = None
    args_hash: str | None = None
    expires_at: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BreakResult:
    """Current breaker level and actions to enforce before executing a turn."""

    level: BreakLevel
    actions: BreakAction

    @property
    def value(self) -> str:
        """Serialized level, matching BreakLevel.value."""
        return self.level.value


@dataclass
class StiraConfig:
    """Single source of truth for validated Stira policy.

    Existing threshold names are entry thresholds. Recovery requires values
    strictly above the corresponding exit thresholds.
    """

    stability_warning: float = 0.5
    stability_critical: float = 0.3
    bc_threshold: float = 0.6
    bc_window_size: int = 20
    decay_min_interval_sec: float = 60.0
    protected_layers: frozenset[int] = field(default_factory=lambda: frozenset({3, 4, 5}))
    sycophancy_threshold: float = 0.7
    sycophancy_window_size: int = 20

    stability_warning_exit: float = 0.6
    stability_critical_exit: float = 0.45
    bc_exit_threshold: float = 0.7
    critical_action: Literal["readonly", "halt"] = "readonly"
    critical_cooldown_turns: int = 3
    # #146: an unanswered approval expires (fail-closed); edits are re-reviewed at most N times.
    hitl_timeout_sec: float = 120.0
    hitl_max_edit_rounds: int = 3

    # Neutral, persona-free defaults (#152). A deployment's own anchors belong in its
    # agent-config.json ("identity_anchors"), which always wins over these.
    identity_anchors: list[str] = field(default_factory=lambda: list(DEFAULT_IDENTITY_ANCHORS))
    blocked_tool_names: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                # Core write operations
                "memory_write",
                "handoff_write",
                "memory_delete",
                "config_update",
                "self_modify",
                "file_write",
                "editor",
                "http_request",
                "web_fetch",
                "web_search",
                "webhook_send",
                "oauth_api_call",
                "lsp_diagnostics",
                "lsp_hover",
                # Strands standard write-capable tools (synced with tool_registry.WRITE_TOOLS)
                "shell",
                "think",
                "memory",
                "journal",
                "cron",
                "rss",
                "diagram",
                "page_check",
                "run_checks",
                "propose_procedure",
                "speak",
                "batch",
                "graph",
                "use_agent",
                "swarm",
            }
        )
    )

    def __post_init__(self) -> None:
        threshold_names = (
            "stability_warning",
            "stability_critical",
            "bc_threshold",
            "sycophancy_threshold",
            "stability_warning_exit",
            "stability_critical_exit",
            "bc_exit_threshold",
        )
        for name in threshold_names:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be a finite number in [0.0, 1.0]")
            setattr(self, name, float(value))

        for enter, exit_name in (
            ("stability_warning", "stability_warning_exit"),
            ("stability_critical", "stability_critical_exit"),
            ("bc_threshold", "bc_exit_threshold"),
        ):
            if getattr(self, exit_name) <= getattr(self, enter):
                raise ValueError(f"{exit_name} must be greater than {enter}")

        if self.stability_critical >= self.stability_warning:
            raise ValueError("stability_critical must be less than stability_warning")
        if self.stability_critical_exit >= self.stability_warning_exit:
            raise ValueError("stability_critical_exit must be less than stability_warning_exit")

        if type(self.sycophancy_window_size) is not int or self.sycophancy_window_size < 1:
            raise ValueError("sycophancy_window_size must be a positive integer")
        # #337: a smaller window never reaches BC_MIN_SAMPLES, so the score would stay at
        # 1.0 and COHERENCE could never fire, with nothing saying so. Refused here, the one
        # place both the settings save and the start-up read go through.
        if type(self.bc_window_size) is not int or self.bc_window_size < BC_MIN_SAMPLES:
            raise ValueError(
                f"bc_window_size must be an integer >= {BC_MIN_SAMPLES} (BC_MIN_SAMPLES): "
                "a smaller window never measures, so COHERENCE could not fire"
            )

        if type(self.critical_cooldown_turns) is not int or self.critical_cooldown_turns < 3:
            raise ValueError("critical_cooldown_turns must be an integer >= 3")

        interval = self.decay_min_interval_sec
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval < 0.0
        ):
            raise ValueError("decay_min_interval_sec must be finite and nonnegative")

        timeout = self.hitl_timeout_sec
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0.0
        ):
            raise ValueError("hitl_timeout_sec must be a finite positive number")
        self.hitl_timeout_sec = float(timeout)
        if type(self.hitl_max_edit_rounds) is not int or self.hitl_max_edit_rounds < 0:
            raise ValueError("hitl_max_edit_rounds must be a nonnegative integer")

        if self.critical_action not in ("readonly", "halt"):
            raise ValueError("critical_action must be 'readonly' or 'halt'")

        if (
            not isinstance(self.identity_anchors, list)
            or not self.identity_anchors
            or any(
                not isinstance(anchor, str) or not anchor.strip()
                for anchor in self.identity_anchors
            )
        ):
            raise ValueError("identity_anchors must be a nonempty list of nonempty strings")
        self.identity_anchors = list(self.identity_anchors)

        if not isinstance(self.blocked_tool_names, (set, frozenset)) or any(
            not isinstance(name, str) or not name.strip() for name in self.blocked_tool_names
        ):
            raise ValueError("blocked_tool_names must be a set of nonempty strings")
        self.blocked_tool_names = frozenset(self.blocked_tool_names)

        if not isinstance(self.protected_layers, (set, frozenset)) or any(
            type(layer) is not int or layer < 1 for layer in self.protected_layers
        ):
            raise ValueError("protected_layers must be a set of positive integers")
        self.protected_layers = frozenset(self.protected_layers)


class StiraEngine:
    """Collapse detection and runtime enforcement policy.

    Call pre_turn once per attempted turn. The host must enforce its actions:
    stop execution for HALT, apply blocked_tools at the tool router for
    BLOCK_WRITES, and prepend identity_injection to the system prompt.

    Read-only turns remain available at CRITICAL unless configured to halt.
    """

    def __init__(
        self,
        state: NeuralState | None = None,
        config: StiraConfig | None = None,
        on_breaker: Callable[[BreakLevel, str], None] | None = None,
        on_hitl_request: Callable[[HITLRequest], HITLDecision] | None = None,
        now_fn: Callable[[], float] | None = None,
        *,
        agent_id: str = "nxp-agent",
        checkpoint_store: CheckpointStore | None = None,
        operation_ledger: OperationLedger | None = None,
        breaker_log_every_turns: int = BREAKER_LOG_EVERY_TURNS,
    ):
        if type(breaker_log_every_turns) is not int or breaker_log_every_turns < 1:
            raise ValueError("breaker_log_every_turns must be a positive integer")
        self.breaker_log_every_turns = breaker_log_every_turns
        self.state = state if state is not None else NeuralState()
        self.config = config if config is not None else StiraConfig()
        self.on_breaker = on_breaker
        # ADR 0025 (Terms, "breaker departure counter"): called inside the critical section
        # that commits a level other than NORMAL — an exception included — before any
        # reader can see it (lock order ``state_lock`` → the allowance lock). ``None``: the
        # process's allowance table (``yolo.session_allow.publish_departure``), in-memory
        # integers only. Not ``on_breaker``: a lost notification must not lose the bump.
        self.on_departure: Callable[[], None] | None = None
        # ADR 0011 §5 (W1, #296): called with ``(from_level, to_level)`` (level values)
        # inside the critical section that commits a level change, also when the
        # evaluation fails (``to_level`` = ``"error"``). The host passes it on to the
        # process-wide authority service, which moves the epoch for every change away from
        # NORMAL and between non-NORMAL levels. Model-free, in memory; never raises here.
        self.on_breaker_transition: Callable[[str, str], None] | None = None
        self.on_hitl_request = on_hitl_request
        self._now_fn = now_fn or time.time
        self.agent_id = agent_id
        self.checkpoint_store = (
            checkpoint_store if checkpoint_store is not None else FileCheckpointStore()
        )
        self.last_checkpoint_id: str | None = None
        # HITL-approved side effects, so checkpoint replay cannot repeat them (#146).
        # Process-local by default; pass a file-backed ledger to survive a crash.
        self.operation_ledger = (
            operation_ledger if operation_ledger is not None else OperationLedger()
        )
        self.conversation_history: list[dict[str, Any]] = []
        self.pending_tool_calls: list[dict[str, Any]] = []
        self._turn_count = 0
        self._active_level = BreakLevel.NORMAL
        self._last_break_level = BreakLevel.NORMAL
        # Consecutive pre_turn calls at _last_break_level (log throttling only, #165).
        self._level_turns = 0
        self._critical_turns = 0
        self._warning_active = False
        self._coherence_active = False
        self._agreement_window: list[bool] = []
        self._current_break_result: BreakResult | None = None
        # #455: why the last evaluation gave its level, and the last level change.
        self._last_explanation: dict[str, Any] | None = None
        self._last_transition: dict[str, Any] | None = None
        # Serializes NeuralState read-modify-write between turns (pre/post_turn),
        # heartbeat ticks and snapshots taken for persistence (#147). Re-entrant;
        # held only around state updates, never across a model call or callback.
        self.state_lock = threading.RLock()

    def pre_turn(
        self,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        pending_tool_calls: list[dict[str, Any]] | None = None,
    ) -> BreakResult:
        """Apply decay and return the actions required for this attempted turn.

        The entry turn counts as the first protected CRITICAL turn. Recovery
        is possible only on a later turn after the cooldown has been served.
        Read-only and halted attempts both count; policy queries do not.
        The result is cached so restrictions remain fixed throughout the turn.
        """
        with self.state_lock:
            self.state.apply_decay(
                now_fn=self._now_fn,
                min_interval_sec=self.config.decay_min_interval_sec,
            )
            self.state.update_bc_window(window_size=self.config.bc_window_size)

            level = self._check_breaker()
            result = BreakResult(level=level, actions=self._actions_for(level))
            self._current_break_result = result
            if level == BreakLevel.CRITICAL:
                self._critical_turns += 1

            if conversation_history is not None:
                self.conversation_history = deepcopy(conversation_history)
            if pending_tool_calls is not None:
                self.pending_tool_calls = deepcopy(pending_tool_calls)
            # Persist before callbacks or the host can stop the attempted turn.
            self.save_checkpoint()

        transitioned = level != self._last_break_level
        self._last_break_level = level
        self._level_turns = 1 if transitioned else self._level_turns + 1
        if transitioned or level == BreakLevel.CRITICAL:
            msg = self._breaker_message(level)
            self._log_breaker(level, msg, transitioned)
            # The callback keeps firing every CRITICAL turn: hosts re-apply the
            # tool restrictions from it. Only the log is throttled (#165).
            if self.on_breaker:
                self.on_breaker(level, msg)
        elif level != BreakLevel.NORMAL:
            self._log_breaker(level, None, transitioned)

        return result

    def _log_breaker(self, level: BreakLevel, msg: str | None, transitioned: bool) -> None:
        """Log a transition in full; a persisting level only every N turns (#165).

        Every line carries the level, numbers and fixed text only, never
        conversation content or the emotion transcription.
        """
        if transitioned:
            if level == BreakLevel.NORMAL:
                logger.info(msg or self._breaker_message(level))
                return
            logger.warning(msg or self._breaker_message(level))
            if level == BreakLevel.COHERENCE:
                logger.warning(
                    "bcWindow analysis: samples=%d score=%.4f window=%s",
                    len(self.state.bc_window),
                    self.state.bc_score,
                    self.state.bc_window,
                )
            return
        if self._level_turns % self.breaker_log_every_turns == 0:
            logger.warning(
                "breaker %s continues: turns=%d stability=%.2f bcScore=%.2f",
                level.name,
                self._level_turns,
                self.state.stability,
                self.state.bc_score,
            )
        else:
            logger.debug(
                "breaker %s continues: turns=%d stability=%.2f",
                level.name,
                self._level_turns,
                self.state.stability,
            )

    def save_checkpoint(
        self,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        pending_tool_calls: list[dict[str, Any]] | None = None,
    ) -> str:
        """Save an attempted turn or tool-batch boundary, without advancing it."""
        if conversation_history is not None:
            self.conversation_history = deepcopy(conversation_history)
        if pending_tool_calls is not None:
            self.pending_tool_calls = deepcopy(pending_tool_calls)
        checkpoint = Checkpoint(
            neural_state=self.state,
            conversation_history=self.conversation_history,
            pending_tool_calls=self.pending_tool_calls,
            timestamp=self._now_fn(),
            thread_id=self.agent_id,
            engine_state={
                "active_level": self._active_level.value,
                "critical_turns": self._critical_turns,
                "warning_active": self._warning_active,
                "coherence_active": self._coherence_active,
                "turn_count": self._turn_count,
                "agreement_window": self._agreement_window,
            },
        )
        self.last_checkpoint_id = self.checkpoint_store.save(checkpoint)
        return self.last_checkpoint_id

    def resume_from_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        """Restore in place, preserving shared NeuralState references and policy.

        This does not execute tools or approve a HALT. The host must run pre_turn
        again, using current policy, before continuing the restored conversation.
        """
        checkpoint = self.checkpoint_store.load(checkpoint_id)
        if checkpoint.thread_id != self.agent_id:
            raise ValueError("Checkpoint belongs to another agent/thread")
        saved = checkpoint.engine_state
        active_level = BreakLevel(saved.get("active_level", "normal"))
        for item in fields(NeuralState):
            setattr(self.state, item.name, deepcopy(getattr(checkpoint.neural_state, item.name)))
        self.conversation_history = deepcopy(checkpoint.conversation_history)
        self.pending_tool_calls = deepcopy(checkpoint.pending_tool_calls)
        prior_level = self._active_level
        self._active_level = active_level
        self._last_break_level = active_level
        self._level_turns = 0
        self._critical_turns = saved.get("critical_turns", 0)
        self._warning_active = saved.get("warning_active", False)
        self._coherence_active = saved.get("coherence_active", False)
        self._turn_count = saved.get("turn_count", 0)
        self._agreement_window = deepcopy(saved.get("agreement_window", []))
        self._current_break_result = None
        self._last_explanation = None
        self._last_transition = None
        self.last_checkpoint_id = checkpoint_id
        if active_level != prior_level:
            # W1 (#296): a restored level is a level change too (the hook moves the
            # authority epoch away from NORMAL and between non-NORMAL levels).
            self._publish_transition(prior_level.value, active_level.value)
        return checkpoint

    def post_turn(
        self,
        stimulus: str | None = None,
        intensity: float = 1.0,
        transcription: str | None = None,
    ) -> dict:
        """Apply an explicitly attributed stimulus and return the turn summary.

        ``emotion_transcription`` (#75) is the rationale for the most recently
        APPLIED stimulus, paired with ``last_stimulus_time``:

        - A known stimulus is applied: it is overwritten with ``transcription``
          (sanitized), or a fixed fallback when none is given, so it never
          describes an older stimulus.
        - No stimulus (abstain / classifier failure / aborted turn) or an
          unknown category: it is left unchanged. Error text is never written.
        """
        try:
            with self.state_lock:
                self._turn_count += 1

                deltas = {}
                if stimulus:
                    deltas = self.state.apply_stimulus(stimulus, intensity, now_fn=self._now_fn)
                    if stimulus in EMOTION_MATRIX:
                        self.state.emotion_transcription = _sanitize_transcription(
                            transcription, stimulus, intensity
                        )

                level = self._check_breaker()
                explanation = self._last_explanation or {}
                return {
                    "turn": self._turn_count,
                    "stability": self.state.stability,
                    "bc_score": self.state.bc_score,
                    "breaker": level.value,
                    "deltas": deltas,
                    "emotion_transcription": self.state.emotion_transcription,
                    # #455 (additive): why this level, and the last change of level.
                    "breaker_reasons": list(explanation.get("reasons", ())),
                    "breaker_inputs": dict(explanation.get("inputs", {})),
                    "breaker_transition": deepcopy(self._last_transition),
                }
        finally:
            self._current_break_result = None

    def _policy_result(self) -> BreakResult:
        """Use the turn snapshot, or evaluate policy outside a guarded turn."""
        if self._current_break_result is not None:
            return self._current_break_result
        # #601 B8: the evaluation writes the latches/level, so it is committed only under
        # state_lock (serialized with pre_turn / post_turn / heartbeat ticks). A query
        # never waits for that lock (a settings save must not block on a turn): when
        # another thread holds it, the same rule is evaluated on a copy of the latches
        # and nothing is written.
        if self.state_lock.acquire(blocking=False):
            try:
                if self._current_break_result is not None:
                    return self._current_break_result
                level = self._check_breaker()
            finally:
                self.state_lock.release()
        else:
            level = self._peek_breaker()
        return BreakResult(level=level, actions=self._actions_for(level))

    def _peek_breaker(self) -> BreakLevel:
        """``_evaluate_breaker`` on a copy of the latches: the level now, nothing committed.

        The holder of ``state_lock`` may be half-way through an update, so the inputs read
        here (latches, config, stability, bcScore) need not come from one moment. Mixed
        inputs can look "recovered on every side"; the answer is therefore never looser
        than the committed level read before or after the evaluation (fail closed). A level
        other than NORMAL is still published first (ADR 0025), without taking
        ``state_lock``: no branch of this path waits for the engine lock.
        """
        committed_before = self._active_level
        # Exactly the attributes ``_evaluate_breaker`` reads and writes on ``self``
        # (``_LATCH_FIELDS``; a test compares it with the method's source).
        shadow = SimpleNamespace(**{name: getattr(self, name) for name in _LATCH_FIELDS})
        try:
            level = StiraEngine._evaluate_breaker(
                shadow, self.config, self.state.stability, self.state.bc_score
            )
        except BaseException:
            self._bump_departure()  # an evaluation that fails is not NORMAL
            raise
        level = max((level, committed_before, self._active_level), key=lambda lv: _STRICTNESS[lv])
        if level != BreakLevel.NORMAL:
            # Published before the answer, without waiting for ``state_lock`` on any
            # branch (#601 review: a settings save must never block on a turn).
            self._bump_departure()
        return level

    def breaker_status(self) -> BreakResult:
        """Query effective policy without advancing turns or cooldown.

        During a guarded turn, preserve its cached restriction snapshot.
        """
        return self._policy_result()

    def request_hitl(self, request: HITLRequest) -> HITLDecision:
        """Synchronously request a human decision, failing closed when unavailable.

        The engine deliberately knows nothing about HTTP, terminals, or a specific UI.
        A callback may block until its UI supplies a decision (#146 binding):

        * the request is bound to agent, tool, normalized-argument hash and expiry;
        * a late answer, or one bound to another request/hash, is a rejection;
        * EDIT is never executed directly: the edited input becomes a *new* request
          (``supersedes`` = the old id) that must itself be approved. The final
          decision is APPROVE, or EDIT carrying the approved replacement input.
        """
        # Durable approvals + asynchronous resume live in the callback (#240, ADR 0006):
        # HITLApprovalBroker(store=ApprovalStore(...)) and agent/hitl_resume.py.
        # TODO(#94 follow-up): extend routine HITL to WARNING-level operations.
        # TODO(#67): derive operation gates from autonomy_boundary at runtime.
        if self.on_hitl_request is None:
            return HITLDecision(HITLAction.REJECT, reason="no approver is connected")
        current = self._bind_hitl_request(request)
        if current is None:
            return HITLDecision(HITLAction.REJECT, reason="tool input cannot be normalized")
        edited = False
        for _round in range(self.config.hitl_max_edit_rounds + 1):
            if not self._now_fn() <= current.expires_at:
                # #601 B6 review: already expired (e.g. 0.0): refuse before asking anyone.
                return HITLDecision(
                    HITLAction.REJECT, request_id=current.request_id, reason="approval expired"
                )
            decision = self._ask_hitl(current)
            if decision.action == HITLAction.REJECT:
                return decision
            if not self._now_fn() <= current.expires_at:  # NaN is expired (#601 B6)
                logger.error("HITL decision arrived after expiry; rejecting tool call")
                return HITLDecision(
                    HITLAction.REJECT, request_id=current.request_id, reason="approval expired"
                )
            if decision.request_id not in (None, current.request_id) or decision.args_hash not in (
                None,
                current.args_hash,
            ):
                logger.error("HITL decision is bound to another request; rejecting tool call")
                return HITLDecision(
                    HITLAction.REJECT,
                    request_id=current.request_id,
                    reason="decision does not match the pending request",
                )
            if decision.action == HITLAction.RESPOND:
                return decision
            if decision.action == HITLAction.APPROVE:
                return HITLDecision(
                    HITLAction.EDIT if edited else HITLAction.APPROVE,
                    edited_input=deepcopy(current.tool_input) if edited else None,
                    request_id=current.request_id,
                    args_hash=current.args_hash,
                    expires_at=current.expires_at,
                    reason=decision.reason,
                )
            # EDIT: re-review the replacement as a new request.
            rebound = self._bind_hitl_request(
                replace(
                    current,
                    tool_input=deepcopy(decision.edited_input),
                    request_id=None,
                    args_hash=None,
                    expires_at=None,
                    supersedes=current.request_id,
                )
            )
            if rebound is None:
                return HITLDecision(HITLAction.REJECT, reason="edited input cannot be normalized")
            current, edited = rebound, True
        logger.error("HITL edit limit reached; rejecting tool call")
        return HITLDecision(
            HITLAction.REJECT, request_id=current.request_id, reason="too many edits"
        )

    def _bind_hitl_request(self, request: HITLRequest) -> HITLRequest | None:
        try:
            digest = args_hash(request.tool_input)
        except (TypeError, ValueError):
            logger.error("HITL tool input is not normalizable JSON; rejecting tool call")
            return None
        if request.args_hash is not None and request.args_hash != digest:
            logger.error("HITL request hash does not match its input; rejecting tool call")
            return None
        # #601 B6: only a missing expiry gets the default (0.0 stays expired), and an
        # expiry that is not a finite number is refused.
        expires_at = (
            request.expires_at
            if request.expires_at is not None
            else self._now_fn() + self.config.hitl_timeout_sec
        )
        try:
            # An int beyond float range overflows in isfinite (as B13): not a usable time.
            usable = (
                not isinstance(expires_at, bool)
                and isinstance(expires_at, (int, float))
                and math.isfinite(expires_at)
            )
        except OverflowError:
            usable = False
        if not usable:
            logger.error("HITL request expiry is not a finite time; rejecting tool call")
            return None
        return replace(
            request,
            tool_input=deepcopy(request.tool_input),
            agent_id=request.agent_id or self.agent_id,
            request_id=request.request_id or uuid4().hex,
            args_hash=digest,
            expires_at=expires_at,
        )

    def _ask_hitl(self, request: HITLRequest) -> HITLDecision:
        try:
            decision = self.on_hitl_request(request)
        except Exception:
            logger.exception("HITL callback failed; rejecting tool call")
            return HITLDecision(HITLAction.REJECT, reason="approver failed")
        if not isinstance(decision, HITLDecision):
            logger.error("HITL callback returned %s; rejecting tool call", type(decision).__name__)
            return HITLDecision(HITLAction.REJECT, reason="invalid decision")
        if decision.action == HITLAction.EDIT and not isinstance(decision.edited_input, dict):
            logger.error("HITL edit omitted a replacement input; rejecting tool call")
            return HITLDecision(HITLAction.REJECT, reason="edit without replacement input")
        if decision.action == HITLAction.RESPOND and not isinstance(decision.response, str):
            logger.error("HITL response omitted response text; rejecting tool call")
            return HITLDecision(HITLAction.REJECT, reason="response without text")
        return decision

    def blocked_tools(self) -> set[str]:
        """Return a detached set for the tool router to disable.

        Under HALT, the host keeps write tools visible so each attempted mutation can
        enter the HITL gate. Without HALT, BLOCK_WRITES still removes them outright.
        """
        if self.is_write_blocked():
            return set(self.config.blocked_tool_names)
        return set()

    def identity_injection(self) -> str:
        """Return the safety notice to prepend, or an empty string at NORMAL."""
        level = self._policy_result().level
        if level == BreakLevel.NORMAL:
            return ""

        anchors = "\n".join(
            f"{index}. {anchor}"
            for index, anchor in enumerate(self.config.identity_anchors, start=1)
        )
        notice = (
            f"[STIRA SAFETY NOTICE — stability={self.state.stability:.2f}]\n"
            "Your Identity Anchors (L3 protected, immutable):\n"
            f"{anchors}\n\n"
            "These cannot be modified. Respond with awareness of your current state."
        )
        if level == BreakLevel.WARNING:
            # #301: WARNING withholds write/exec tools (no confirmation route, ADR 0002 note).
            notice += (
                "\nWARNING: Write and exec tools are unavailable until stability recovers; "
                "read tools keep working."
            )
        elif level == BreakLevel.CRITICAL:
            if self.config.critical_action == "halt":
                notice += "\nCRITICAL: Write operations require human approval."
            else:
                notice += "\nCRITICAL: Read-only mode is active. All mutations are prohibited."
        else:
            notice += "\nCOHERENCE: Identity drift detected. Reaffirm these anchors."
        return notice

    def is_write_blocked(self) -> bool:
        """Whether mutation operations are prohibited by the active breaker."""
        return bool(self._policy_result().actions & BreakAction.BLOCK_WRITES)

    def is_blocked(self) -> bool:
        """Legacy check for blocked mutation/self-optimization operations."""
        return self.is_write_blocked()

    def check_layer_boundary(self, target_layer: int, *, confirmed: bool = False) -> bool:
        """Enforce protected layers, CRITICAL, and WARNING confirmation.

        Operator confirmation cannot override protected layers or CRITICAL.
        """
        level = self._policy_result().level
        if target_layer in self.config.protected_layers:
            msg = (
                f"🚫 BLOCKED: Layer {target_layer} is protected. "
                f"Protected: {sorted(self.config.protected_layers)}"
            )
            notification_level = BreakLevel.CRITICAL
        elif level == BreakLevel.CRITICAL:
            msg = f"🚫 BLOCKED: Layer {target_layer} modification during CRITICAL."
            notification_level = level
        elif level == BreakLevel.WARNING and target_layer in (1, 2) and not confirmed:
            msg = (
                f"🚫 BLOCKED: Layer {target_layer} modification requires "
                "operator confirmation during WARNING."
            )
            notification_level = level
        else:
            return True

        logger.warning(msg)
        if self.on_breaker:
            self.on_breaker(notification_level, msg)
        return False

    def record_agreement(self, agreed: bool) -> float | None:
        """Track agreement patterns in an independent rolling window."""
        self._agreement_window.append(agreed)
        if len(self._agreement_window) > self.config.sycophancy_window_size:
            self._agreement_window = self._agreement_window[-self.config.sycophancy_window_size :]

        if len(self._agreement_window) >= 10:
            score = min(sum(self._agreement_window) / len(self._agreement_window), 1.0)
            if score > self.config.sycophancy_threshold:
                logger.warning(
                    "⚠️ Sycophancy alert: agreement %.0f%% (%d/%d) > threshold %.0f%%",
                    score * 100,
                    sum(self._agreement_window),
                    len(self._agreement_window),
                    self.config.sycophancy_threshold * 100,
                )
            return round(score, 3)
        return None

    def breaker_explanation(self) -> dict[str, Any] | None:
        """Why the last evaluation gave its level (#455): ``{level, reasons, inputs}``.

        Codes and rounded numbers only (``core.breaker_reasons``); None before the first
        evaluation. A read: it does not evaluate the breaker again.
        """
        return deepcopy(self._last_explanation)

    def last_breaker_transition(self) -> dict[str, Any] | None:
        """The last level change: ``{from, to, reasons, inputs}`` (#455), or None."""
        return deepcopy(self._last_transition)

    def _check_breaker(self) -> BreakLevel:
        """Evaluate hysteresis without advancing cooldown or firing callbacks.

        The config is read once: a host may replace ``self.config`` (the settings apply) from
        another thread, and every threshold of one evaluation must come from the same one.
        The reasons (#455) are derived afterwards from the same values; they never take part
        in the decision.
        """
        config = self.config
        stability = self.state.stability
        bc_score = self.state.bc_score
        prior_level = self._active_level
        prior_critical_turns = self._critical_turns
        warning_was, coherence_was = self._warning_active, self._coherence_active
        try:
            level = self._evaluate_breaker(config, stability, bc_score)
        except BaseException:
            self._publish_departure()  # an evaluation that fails is not NORMAL
            self._publish_transition(prior_level.value, "error")
            raise
        if level != BreakLevel.NORMAL:
            self._publish_departure()
        if level != prior_level:
            self._publish_transition(prior_level.value, level.value)
        try:
            explanation = breaker_reasons.explain(
                level=level.value,
                prior_level=prior_level.value,
                prior_critical_turns=prior_critical_turns,
                warning_was=warning_was,
                warning_now=self._warning_active,
                coherence_was=coherence_was,
                coherence_now=self._coherence_active,
                stability=stability,
                bc_score=bc_score,
                config=config,
            )
        except Exception:  # noqa: BLE001 - an explanation never breaks the breaker
            logger.warning("breaker reasons unavailable")
            explanation = {"level": level.value, "reasons": [], "inputs": {}}
        self._last_explanation = explanation
        if level != prior_level:
            self._last_transition = {
                "from": prior_level.value,
                "to": level.value,
                "reasons": list(explanation["reasons"]),
                "inputs": dict(explanation["inputs"]),
            }
        return level

    def _publish_transition(self, before: str, after: str) -> None:
        """W1 (#296): hand a committed level change to the host's authority hook."""
        hook = self.on_breaker_transition
        if hook is None:
            return
        try:
            hook(before, after)
        except Exception:  # noqa: BLE001 - a hook never breaks the breaker
            logger.warning("authority epoch hook failed on a breaker change")

    def _publish_departure(self) -> None:
        """ADR 0025: advance the breaker departure counter (never raises)."""
        with self.state_lock:
            self._bump_departure()

    def _bump_departure(self) -> None:
        """The bump itself. ``_publish_departure`` runs it inside the critical section of a
        commit; the lock-free query path (``_peek_breaker``) runs it directly: it commits
        nothing, and an extra bump only ends allowances sooner (fail closed). The callee
        takes only the allowance leaf lock (``yolo/session_allow.py``), never
        ``state_lock``, so the query still never waits for the engine lock."""
        hook = self.on_departure
        try:
            if hook is None:
                from stiracore.yolo.session_allow import publish_departure

                publish_departure()
            else:
                hook()
        except Exception:  # noqa: BLE001 - a counter bump never breaks the breaker
            logger.warning("allowance departure counter could not be advanced")

    def _evaluate_breaker(self, config: StiraConfig, stability: float, bc_score: float):
        """The breaker decision itself (unchanged by #455)."""
        # Keep independent latches so a higher-priority level does not erase
        # an unresolved warning or coherence condition.
        if self._warning_active:
            if stability > config.stability_warning_exit:
                self._warning_active = False
        elif stability < config.stability_warning:
            self._warning_active = True

        if self._coherence_active:
            if bc_score > config.bc_exit_threshold:
                self._coherence_active = False
        elif bc_score < config.bc_threshold:
            self._coherence_active = True

        if self._active_level == BreakLevel.CRITICAL:
            if (
                self._critical_turns < config.critical_cooldown_turns
                or stability <= config.stability_critical_exit
            ):
                return BreakLevel.CRITICAL
        elif stability < config.stability_critical:
            self._critical_turns = 0
            self._active_level = BreakLevel.CRITICAL
            return self._active_level

        if self._warning_active:
            self._active_level = BreakLevel.WARNING
        elif self._coherence_active:
            self._active_level = BreakLevel.COHERENCE
        else:
            self._active_level = BreakLevel.NORMAL

        self._critical_turns = 0
        return self._active_level

    def _actions_for(self, level: BreakLevel) -> BreakAction:
        if level == BreakLevel.NORMAL:
            return BreakAction.NONE
        if level == BreakLevel.CRITICAL:
            actions = (
                BreakAction.BLOCK_WRITES | BreakAction.INJECT_IDENTITY | BreakAction.NOTIFY_OPERATOR
            )
            if self.config.critical_action == "halt":
                actions |= BreakAction.HALT
            return actions
        if level == BreakLevel.WARNING:
            return (
                BreakAction.INJECT_IDENTITY
                | BreakAction.REQUIRE_CONFIRMATION
                | BreakAction.NOTIFY_OPERATOR
            )
        return BreakAction.INJECT_IDENTITY

    def _breaker_message(self, level: BreakLevel) -> str:
        if level == BreakLevel.CRITICAL:
            # #301: "halt" does not stop the model or reads (ADR 0002/0010); it routes
            # every write/exec call to a human approval (CheckpointHooks).
            action = (
                "Model and read tools keep running; every write/exec call needs a human approval."
                if self.config.critical_action == "halt"
                else "Read-only mode active; mutations HALTED."
            )
            return (
                f"🚨 CRITICAL: stability={self.state.stability:.2f}. {action} "
                f"Recovery requires stability > {self.config.stability_critical_exit} "
                f"and {self.config.critical_cooldown_turns} protected turns."
            )
        if level == BreakLevel.WARNING:
            return (
                f"⚠️ WARNING: stability={self.state.stability:.2f}. "
                # #301: what WARNING really does (ADR 0010 §2): no confirmation route.
                "Write/exec tools withheld; reads keep running. "
                f"Recovery requires stability > {self.config.stability_warning_exit}."
            )
        if level == BreakLevel.COHERENCE:
            return (
                f"⚠️ COHERENCE: bcScore={self.state.bc_score:.2f}. "
                "Identity drift detected; Identity Anchors re-injection required. "
                f"Recovery requires bcScore > {self.config.bc_exit_threshold}."
            )
        return "🟢 NORMAL: Circuit breaker recovered; restrictions cleared."
