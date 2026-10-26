"""SDK tool-batch snapshots, before any tool in the batch has side effects."""

from __future__ import annotations

import contextlib
import logging
from copy import deepcopy
from threading import Lock
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    AfterToolsEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    BeforeToolsEvent,
    HookProvider,
    HookRegistry,
)

from stiracore.core import (
    BreakAction,
    BreakLevel,
    HITLAction,
    HITLRequest,
    StiraEngine,
    hitl_reasons,
)
from stiracore.core.approval import (
    HITL_BINDINGS_KEY,
    HITL_IDEMPOTENCY_KEYS_KEY,
    LEDGER_COMMITTED,
    LEDGER_STARTED,
    ApprovalBinding,
    args_hash,
    operation_key,
)
from stiracore.core.engine import HITL_APPROVED_TOOL_IDS_KEY, HITLDecision
from stiracore.observability.hooks import PROGRESS_DECLARED_KEY

# toolUseId -> (rule, generation) an approval was given under (#190, ADR 0010 §6).
HITL_POLICY_GENERATIONS_KEY = "stira.hitl.policy_generations"
# toolUseId -> the YOLO automatic approval of that call (ADR 0012 §6a, phase 5a): consumed
# once by ApprovalBindingVerifier at the execution boundary, voided if the call is refused.
YOLO_GRANTS_KEY = "stira.yolo.grants"
# #448 (only with the YOLO approver connected): toolUseId -> how a person answered its card
# ("manual" / "respond"); toolUseId -> the window an automatic approval was consumed in
# (counted there by ``consume``); toolUseId -> the window a call that runs was counted in
# (an error result adds one ``failed``). Counts only, never an argument.
YOLO_DECIDED_KEY = "stira.yolo.decided"
YOLO_COUNTED_KEY = "stira.yolo.counted"
YOLO_RAN_IN_KEY = "stira.yolo.ran_in"
# ADR 0025: toolUseId -> the session allowance's answer (``UseBinding``), consumed once by
# ApprovalBindingVerifier (§4c); toolUseId -> (intent row, card request id or None) of a call
# whose ``audit.tool.*`` outcome row this gate writes after it ran (an allowance-authorized
# call, or the once-approved call of a card pressed with one of the two buttons).
ALLOW_GRANTS_KEY = "stira.allow.grants"
ALLOW_INTENTS_KEY = "stira.allow.intents"
ALLOW_CLASS_KEY = "stira.allow.class"
_POLICIES = ("always", "ask", "never")
# What the model reads when it calls a Never tool (#508). The first sentence is unchanged
# (failure classification matches it, ``diagnostics.write_errors``).
NEVER_REFUSAL = (
    "Tool policy is Never for this tool; not executed. Reason: rule_never. Only a person "
    "can enable it (Settings → Tools); you may ask the user to."
)
_LEVEL_RANK = {BreakLevel.NORMAL: 0, BreakLevel.COHERENCE: 0, BreakLevel.WARNING: 1}


def read_policy(policy_check: Any, tool_name: str) -> tuple[str, Any] | None:
    """``(rule, generation)`` from a reader returning a rule or ``(rule, generation)``.

    None without a reader. An error, or anything but a known rule, is Never.
    """
    if policy_check is None:
        return None
    try:
        value = policy_check(tool_name)
    except Exception:  # noqa: BLE001 - an unreadable rule fails closed
        return ("never", None)
    policy, generation = value if isinstance(value, tuple) and len(value) == 2 else (value, None)
    return (policy, generation) if policy in _POLICIES else ("never", None)


def fresh_breaker_rank(engine: StiraEngine) -> int:
    """Breaker strictness from the live state, ignoring the turn snapshot (read-only):
    0 NORMAL, 1 WARNING, 2 CRITICAL (stability thresholds only)."""
    return _fresh_rank(engine)


def _fresh_rank(engine: StiraEngine) -> int:
    """Breaker strictness from the live state, ignoring the turn snapshot (read-only for
    the breaker; a level other than NORMAL advances the allowance departure counter,
    ADR 0025 — in-memory only)."""
    state, config = engine.state, engine.config
    stability = getattr(state, "stability", 1.0)
    if stability < config.stability_critical:
        rank = 2
    elif stability < config.stability_warning:
        rank = 1
    else:
        return 0
    publish = getattr(engine, "_publish_departure", None)
    if callable(publish):
        publish()
    return rank


from stiracore.tools.tool_consent import (
    GATE_ISSUED_KEY,
    mark_gate_passed,
    modifying_http_call,
    needs_gate_mark,
    needs_person,
    release_gate_mark,
    revoke_gate_mark,
)


def _write_capable(engine: StiraEngine, tool_name: str) -> bool:
    from stiracore.tools.tool_registry import WRITE_TOOLS

    return tool_name in engine.config.blocked_tool_names or tool_name in WRITE_TOOLS


def _is_delegate(tool_name: str) -> bool:
    from stiracore.tools.delegate_tool import DELEGATE_TOOL_NAME

    return tool_name == DELEGATE_TOOL_NAME


def _ledgered(engine: StiraEngine, tool_name: str) -> bool:
    """ADR 0024 §5: calls that get an operation-ledger entry before they run, approved or
    not — every write-capable call and ``delegate`` (a child: new model calls, tokens).
    A replayed batch answers them from the ledger instead of running them again."""
    return _write_capable(engine, tool_name) or _is_delegate(tool_name)


class CheckpointHooks(HookProvider):
    """Checkpoint, HITL gate and replay deduplication for one engine.

    ``binding_verifier`` must be registered *after* every other hook: it is the
    last check before a HITL-approved tool runs (#146).
    """

    def __init__(
        self,
        engine: StiraEngine,
        path_check: Any = None,
        policy_check: Any = None,
        scope_policy: Any = None,
    ) -> None:
        self.engine = engine
        # ``IdentityGuard.path_denial``: the same path decision as #5/#6 (#243).
        # Never ask a human for a write no approval could make allowed.
        self.path_check = path_check
        # Live Always/Ask/Never rule, ``name -> "always"|"ask"|"never"`` (#190, ADR
        # 0010). Read at every call; an error or an unknown value counts as Never.
        self.policy_check = policy_check
        self._response_text: dict[str, str] = {}
        self._response_lock = Lock()
        self.binding_verifier = ApprovalBindingVerifier(engine, policy_check=policy_check)
        # ADR 0012 phase 5a: the YOLO approver (``yolo.runtime.APPROVER``), set only by a
        # server started with ``STIRA_YOLO_ALLOW``. ``None`` (the default): the gate is
        # exactly as before — every Ask goes to ``request_hitl``.
        self.auto_approver: Any = None
        # ADR 0025: the session allowance approver (``yolo.allow_service.AllowanceService``),
        # set by the chat server. ``None``: no allowance ever answers (the gate as before).
        self.allow_approver: Any = None
        # The agent's ``WritePathPolicy`` (its workspace and write roots): YOLO answers an
        # internal call only inside them (``yolo.tiers.scope_refusal``). ``None``: every
        # call that takes a path goes to a person.
        self.scope_policy = scope_policy
        # ADR 0014 phase 1 (#451): the progress tracker (``progress.ProgressTracker``), set
        # only by a server started with ``STIRA_YOLO_ALLOW``. ``None``: no checkpoint.
        self.progress: Any = None
        # ADR 0019 §5 (#167): ``tool name -> fn(input) -> input`` run first at the gate. The
        # function adds what the harness will run (``run_checks``: its resolved argv,
        # folder, containment and limits under ``plan``), so the card shows it and the
        # approval's argument hash binds it. A function that raises cancels the call.
        self.input_preparers: dict[str, Any] = {}
        # ADR 0024 §7: toolUseIds a resume replays on a person's approval. Such a call
        # always goes through the gate (answered from the store), whatever the rule and
        # the breaker say at replay time, so an approved EDIT never runs the original.
        self._resume_gated: set[str] = set()
        self._resume_lock = Lock()
        # ADR 0024 §5: toolUseIds of the batch a restored checkpoint replays. An ungated
        # ledger entry answers a call only while it is replayed; outside a replay the same
        # key means a provider repeated a toolUseId, and the call is a new one.
        self._replay_ids: frozenset[str] = frozenset()
        self.binding_verifier.replaying = self.replaying

    def begin_replay(self, tool_use_ids) -> None:
        """A checkpoint was restored: its pending calls are replays (``NXPAgent``)."""
        with self._resume_lock:
            self._replay_ids = frozenset(i for i in tool_use_ids if i)

    def end_replay(self) -> None:
        with self._resume_lock:
            self._replay_ids = frozenset()

    def replaying(self, tool_use_id: str) -> bool:
        with self._resume_lock:
            return bool(tool_use_id) and tool_use_id in self._replay_ids

    @contextlib.contextmanager
    def gate_on_resume(self, tool_use_id: str | None):
        """Within the block, the call ``tool_use_id`` is asked like an Ask-rule call."""
        if not tool_use_id:
            yield
            return
        with self._resume_lock:
            self._resume_gated.add(tool_use_id)
        try:
            yield
        finally:
            with self._resume_lock:
                self._resume_gated.discard(tool_use_id)

    def _gated_by_resume(self, tool_use_id: str) -> bool:
        with self._resume_lock:
            return bool(tool_use_id) and tool_use_id in self._resume_gated

    def set_auto_approver(self, approver: Any) -> None:
        """Connect (or, with ``None``, disconnect) the YOLO approver at the rule layer."""
        self.auto_approver = approver
        self.binding_verifier.auto_approver = approver

    def set_allow_approver(self, approver: Any) -> None:
        """Connect (or disconnect) the session allowance approver (ADR 0025 §2)."""
        self.allow_approver = approver
        self.binding_verifier.allow_approver = approver
        self.binding_verifier.scope_policy = self.scope_policy

    def _allow_decision(
        self, event: BeforeToolCallEvent, tool_name: str, tool_use_id: str
    ) -> HITLDecision | None:
        """ADR 0025 §2 step 2 / §4b: after YOLO did not answer, an ``active`` allowance may
        answer this ``RULE_ASK`` call — only in a person's chat / Stage turn of this
        agent. ``None`` → the person's card (any failure too). The answer is an APPROVE
        bound like a person's and goes through every later check; it is consumed in the
        verifier (§4c) and is never a human approval (IdentityGuard, exec marks)."""
        tool_input = event.tool_use.get("input", {})
        origin = _allow_origin(self.engine.agent_id)
        if origin is None:
            return None
        try:
            digest = args_hash(tool_input)
            binding = self.allow_approver.answer(
                tool=tool_name,
                tool_input=tool_input,
                policy=self.scope_policy,
                origin=origin,
                args_hash=digest,
            )
        except Exception:  # noqa: BLE001 - the approver failing sends the call to a person
            return None
        if binding is None:
            return None
        grants = event.invocation_state.setdefault(ALLOW_GRANTS_KEY, {})
        previous = grants.get(tool_use_id)
        if previous is not None:
            _void_allow(self.allow_approver, previous)
        grants[tool_use_id] = binding
        return HITLDecision(
            HITLAction.APPROVE,
            request_id="allow-" + binding.nonce[:16],
            args_hash=digest,
            expires_at=self.engine._now_fn() + self.engine.config.hitl_timeout_sec,
        )

    def _press_void(self, decision: HITLDecision | None) -> None:
        """A card pressed with one of the two buttons whose call is refused at this gate
        reached a terminal result (§9: it did not run)."""
        approver = self.allow_approver
        request_id = getattr(decision, "request_id", None)
        if approver is None or not request_id:
            return
        with contextlib.suppress(Exception):
            approver.terminal(request_id, "void")

    def _auto_decision(
        self, event: BeforeToolCallEvent, tool_name: str, tool_use_id: str, level: Any, read
    ) -> HITLDecision | None:
        """ADR 0012 §2 step 5: the YOLO approver may answer this ``RULE_ASK`` request
        instead of a person. ``None`` → the person's card, as without YOLO (any failure
        here too). An answer is an APPROVE bound like a human one (argument hash, short
        expiry) and goes through every later check of this gate and the verifier."""
        from stiracore.yolo.tiers import destination, scope_refusal

        tool_input = event.tool_use.get("input", {})
        try:
            refusal = scope_refusal(tool_name, tool_input, self.scope_policy)
        except Exception:  # noqa: BLE001 - a check that cannot run: a person's card
            return None
        if refusal is not None:
            # Outside the workspace / write roots, or not decidable; an MCP tool; an
            # external call with secret-like arguments (phase 6, §10): a person's card.
            return None
        try:
            digest = args_hash(tool_input)
        except (TypeError, ValueError):
            return None
        try:
            grant, _code = self.auto_approver.approve(
                tool_name=tool_name,
                args_digest=digest,
                tool_use_id=tool_use_id,
                level=level,
                rule_generation=read[1] if read is not None else None,
                agent_id=self.engine.agent_id,
                destination=destination(tool_name, tool_input),
            )
        except Exception:  # noqa: BLE001 - the approver failing sends the call to a person
            return None
        if grant is None:
            return None
        grants = event.invocation_state.setdefault(YOLO_GRANTS_KEY, {})
        previous = grants.get(tool_use_id)
        if previous is not None:
            # A retry of the same call: the earlier, unused approval returns its slot.
            _void_grant(self.auto_approver, previous, "binding_mismatch")
        grants[tool_use_id] = grant
        return HITLDecision(
            HITLAction.APPROVE,
            request_id="yolo-" + grant.intent,
            args_hash=digest,
            expires_at=self.engine._now_fn() + self.engine.config.hitl_timeout_sec,
        )

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeToolsEvent, self.before_tools)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        """#448: one turn of this agent started (counted only in an open YOLO window)."""
        _yolo_note(self.auto_approver, "record_turn")

    def before_tools(self, event: BeforeToolsEvent) -> None:
        pending = [block["toolUse"] for block in event.message["content"] if "toolUse" in block]
        self.engine.save_checkpoint(
            conversation_history=event.agent.messages,
            pending_tool_calls=pending,
        )
        if self.progress is not None:
            self.progress.batch()

    def after_tools(self, event: AfterToolsEvent) -> None:
        """ADR 0014 §1 hard end: a tool call after the checkpoint's nudge ends the turn
        after this batch with one fixed assistant line (the history keeps alternating)."""
        if self.progress is None:
            return
        try:
            text = self.progress.end_turn_text()
        except Exception:  # noqa: BLE001 - the calls were already cancelled
            return
        if text is not None:
            event.end_turn = text

    def _progress_cancel(self, event: BeforeToolCallEvent, tool_use_id: str) -> bool:
        """ADR 0014 §11: a checkpoint cancels the call **before** the rule, the card and
        the YOLO approver (no card, no cap slot). A check that fails cancels nothing."""
        if self.progress is None:
            return False
        declaration = self._declaration(event)
        try:
            text = self.progress.check(tool_use_id, self.engine.agent_id, declaration=declaration)
        except Exception:  # noqa: BLE001 - a report is never needed to run safely
            return False
        if text is None:
            return False
        event.cancel_tool = text
        if declaration is not None and tool_use_id:
            # #545: the declaration itself, cancelled on purpose (not a refusal)
            event.invocation_state.setdefault(PROGRESS_DECLARED_KEY, set()).add(tool_use_id)
        return True

    def _declaration(self, event: BeforeToolCallEvent) -> str | None:
        """ADR 0014 phase 3: the ``message`` of a ``handoff_to_user`` call with
        ``breakout_of_loop`` exactly ``true`` (``""`` without one), else ``None``. A tool
        whose rule is Never is not offered, so a call of it declares nothing."""
        if event.tool_use.get("name") != "handoff_to_user":
            return None
        tool_input = event.tool_use.get("input")
        if not isinstance(tool_input, dict) or tool_input.get("breakout_of_loop") is not True:
            return None
        read = read_policy(self.policy_check, "handoff_to_user")
        if read is not None and read[0] == "never":
            return None
        message = tool_input.get("message")
        return message if type(message) is str else ""

    def _respond(self, event: BeforeToolCallEvent, tool_use_id: str, text: str, why: str) -> None:
        with self._response_lock:
            self._response_text[tool_use_id] = text
        event.cancel_tool = why

    def _replay_guard(self, event: BeforeToolCallEvent, tool_name: str, tool_use_id: str) -> bool:
        """Refuse to repeat a HITL-approved side effect recorded by the ledger.

        Returns True when the call was handled (skipped or refused).
        """
        tool_input = event.tool_use.get("input", {})
        try:
            digest = args_hash(tool_input)
        except (TypeError, ValueError):
            return False
        key = operation_key(self.engine.agent_id, tool_name, digest, tool_use_id)
        ledger = self.engine.operation_ledger
        record = ledger.get(key)
        if record is None:
            return False
        if record.ungated and not self.replaying(tool_use_id):
            return False  # not a replay: a new call (the verifier records it afresh)
        delegate = _is_delegate(tool_name)
        if record.state != LEDGER_COMMITTED:
            applied = None if delegate else ledger.reconcile(record, tool_input)
            if applied is False:
                return False  # proven not applied: normal gating (fresh approval) applies
            if applied is None:
                logger.info("replay refused: operation %s outcome unknown", key)
                event.cancel_tool = (
                    (
                        "delegate was started before the interruption and may already have "
                        f"run (idempotency key {key}); not repeated after an interruption. "
                        "Call it again (a new call) if it is still needed."
                    )
                    if delegate
                    else "HITL operation outcome is unknown after an interruption "
                    f"(idempotency key {key}); not executed again. Reconcile manually."
                )
                return True
        logger.info("replay skipped: operation %s already applied", key)
        if delegate:
            self._respond(
                event,
                tool_use_id,
                f"delegate already ran before the interruption (idempotency key {key}); not "
                "repeated after an interruption. Call it again (a new call) if it is still "
                "needed.",
                "delegate already ran; replay skipped.",
            )
            return True
        self._respond(
            event,
            tool_use_id,
            f"Already applied before the interruption (idempotency key {key}); not executed again.",
            "HITL operation already applied; replay skipped.",
        )
        return True

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        """Gate write-capable tools at a CRITICAL HALT boundary, and Ask-rule tools always."""
        tool_name = event.tool_use["name"]
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        preparer = self.input_preparers.get(tool_name)
        if preparer is not None:
            try:
                event.tool_use = {
                    **event.tool_use,
                    "input": preparer(deepcopy(event.tool_use.get("input", {}))),
                }
            except Exception:  # noqa: BLE001 - fail closed: nothing runs unprepared
                event.cancel_tool = "The call could not be prepared for approval; not executed."
                return
        if self._progress_cancel(event, tool_use_id):
            return
        read = read_policy(self.policy_check, tool_name)
        policy = read[0] if read is not None else None
        if policy == "never":
            # Defense in depth: a Never tool is also filtered out of the tool list.
            # #508: the reason code and who can enable it; the refusal itself is unchanged.
            event.cancel_tool = NEVER_REFUSAL
            return
        resumed = self._gated_by_resume(tool_use_id)
        ask = policy == "ask" or resumed
        write = _write_capable(self.engine, tool_name)
        if not ask and not _ledgered(self.engine, tool_name):
            return  # read-only: it runs again on a replay (no side effect, ADR 0024 §5)
        if tool_use_id and self._replay_guard(event, tool_name, tool_use_id):
            return
        if not write and not ask:
            return  # ``delegate`` outside the write class: ungated (ledger entry at the end)

        breaker = self.engine.breaker_status()
        halt = breaker.level == BreakLevel.CRITICAL and bool(breaker.actions & BreakAction.HALT)
        if not halt and not ask:
            return
        if (
            not halt
            and write
            and (
                self.engine.is_write_blocked()
                or breaker.actions & (BreakAction.REQUIRE_CONFIRMATION | BreakAction.BLOCK_WRITES)
            )
        ):
            # The Ask rule's approval answers the rule layer only: it never lifts the
            # breaker's write block (monotonic stack, ADR 0010 §2).
            event.cancel_tool = "Writes are blocked by the circuit breaker; not sent for approval."
            return
        if self.path_check is not None:
            denial = self.path_check(tool_name, event.tool_use.get("input", {}))
            if denial is not None:
                event.cancel_tool = f"{denial.reason} (not sent for approval: it cannot lift this)"
                return

        decision = None
        if not halt and not resumed and self.auto_approver is not None:
            # Reason RULE_ASK only: CRITICAL_HALT, Never, the breaker and the floors
            # were decided above and are never answered by YOLO (ADR 0012 §2, §5).
            decision = self._auto_decision(event, tool_name, tool_use_id, breaker.level, read)
        if decision is None and not halt and not resumed and self.allow_approver is not None:
            # ADR 0025 §2: YOLO first, then the session allowance, then the person's card.
            decision = self._allow_decision(event, tool_name, tool_use_id)
            if decision is not None and self.auto_approver is not None:
                event.invocation_state.setdefault(YOLO_DECIDED_KEY, {})[tool_use_id] = (
                    "allowed_again"
                )
        automatic = decision is not None
        if decision is None:
            decision = self.engine.request_hitl(
                HITLRequest(
                    tool_name=tool_name,
                    tool_input=deepcopy(event.tool_use.get("input", {})),
                    break_level=breaker.level,
                    reason=(hitl_reasons.CRITICAL_HALT if halt else hitl_reasons.RULE_ASK),
                    tool_use_id=tool_use_id or None,
                    agent_id=self.engine.agent_id,
                )
            )
            if self.auto_approver is not None and decision.action in _PERSON_ANSWERS:
                # #448: a person answered the card (a later refusal still counts denied).
                event.invocation_state.setdefault(YOLO_DECIDED_KEY, {})[tool_use_id] = (
                    "respond" if decision.action == HITLAction.RESPOND else "manual"
                )
        if decision.action in (HITLAction.APPROVE, HITLAction.EDIT) and read is not None:
            # Generation check (#190): the approval is bound to the rule *generation* it
            # was asked under. Any save in between (even Ask -> Never -> Ask) voids it.
            current = read_policy(self.policy_check, tool_name)
            if current[0] == "never" or current != read:
                event.cancel_tool = (
                    "Tool policy changed while waiting for approval; not executed. "
                    "Ask again under the current policy."
                )
                self._press_void(None if automatic else decision)
                return
            event.invocation_state.setdefault(HITL_POLICY_GENERATIONS_KEY, {})[tool_use_id] = read
        if decision.action in (HITLAction.APPROVE, HITLAction.EDIT) and _fresh_rank(
            self.engine
        ) > _LEVEL_RANK.get(breaker.level, 2):
            # The breaker got stricter while the human was deciding: ask again under it.
            event.cancel_tool = (
                "The circuit breaker changed while waiting for approval; not executed."
            )
            self._press_void(None if automatic else decision)
            return
        if decision.action in (HITLAction.APPROVE, HITLAction.EDIT):
            if decision.action == HITLAction.EDIT:
                event.tool_use = {
                    **event.tool_use,
                    "input": deepcopy(decision.edited_input),
                }
            if decision.args_hash is not None:
                binding = ApprovalBinding(
                    request_id=decision.request_id or "",
                    agent_id=self.engine.agent_id,
                    tool_name=tool_name,
                    args_hash=decision.args_hash,
                    expires_at=decision.expires_at or float("inf"),
                )
                refusal = binding.check(
                    agent_id=self.engine.agent_id,
                    tool_name=tool_name,
                    tool_input=event.tool_use.get("input", {}),
                    now=self.engine._now_fn(),
                )
                if refusal is not None:
                    event.cancel_tool = f"HITL rejected this write operation: {refusal}."
                    self._press_void(None if automatic else decision)
                    return
                event.invocation_state.setdefault(HITL_BINDINGS_KEY, {})[tool_use_id] = binding
            if automatic:
                # Only the rule layer's Ask is answered: a YOLO approval is never a
                # human approval for the breaker's write block (IdentityGuard).
                return
            approved = event.invocation_state.setdefault(HITL_APPROVED_TOOL_IDS_KEY, set())
            approved.add(tool_use_id)
            return

        if decision.action == HITLAction.RESPOND:
            self._respond(
                event,
                tool_use_id,
                decision.response or "",
                "HITL operator supplied a response instead of running the tool.",
            )
            return
        reason = f": {decision.reason}" if decision.reason else ""
        event.cancel_tool = f"HITL rejected this write operation{reason}."

    def after_tool(self, event: AfterToolCallEvent) -> None:
        """Turn RESPOND cancellation into a successful operator-authored tool result."""
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        approved = event.invocation_state.get(HITL_APPROVED_TOOL_IDS_KEY, set())
        approved.discard(tool_use_id)
        event.invocation_state.get(HITL_BINDINGS_KEY, {}).pop(tool_use_id, None)
        with self._response_lock:
            response = self._response_text.pop(tool_use_id, None)
        if response is not None:
            event.result = {
                "toolUseId": tool_use_id,
                "status": "success",
                "content": [{"text": response}],
            }


class ApprovalBindingVerifier(HookProvider):
    """Last BeforeToolCall check for HITL-approved calls, and their ledger entries.

    Re-verifies the approval binding after all other hooks (a later hook must not
    be able to swap arguments), claims the operation in the ledger right before
    execution and records its outcome right after (first AfterToolCall callback).
    """

    def __init__(self, engine: StiraEngine, policy_check: Any = None) -> None:
        self.engine = engine
        self.policy_check = policy_check
        self._claimed: dict[str, str] = {}
        self._lock = Lock()
        self.auto_approver: Any = None  # ADR 0012 phase 5a, see CheckpointHooks
        self.allow_approver: Any = None  # ADR 0025, see CheckpointHooks
        self.scope_policy: Any = None
        # ``toolUseId -> bool``: the call is replayed from a restored checkpoint (set by
        # CheckpointHooks). Without it every call counts as a replay (strictest reading).
        self.replaying: Any = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        exec_mark_allowed = self._verify_binding(event)
        self._issue_gate_mark(event, exec_mark_allowed=exec_mark_allowed)
        self._bind_class_policy(event)
        self._claim_ungated(event)
        self._count_call(event)

    def _bind_class_policy(self, event: BeforeToolCallEvent) -> None:
        """ADR 0025 §3: a write an allowance authorized opens its file with the class as one
        more policy (``open_checked`` asks it again on the opened descriptor's path): a
        parent swapped for a symlink after the check is a path outside the class."""
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        class_key = _pop(event.invocation_state, ALLOW_CLASS_KEY, tool_use_id)
        if class_key is None or event.cancel_tool:
            return
        owned = event.invocation_state.get(GATE_ISSUED_KEY)
        record = owned.get(id(event.tool_use)) if isinstance(owned, dict) else None
        if record is None:
            return  # no write mark (not a file write): nothing to bind
        from stiracore.tools.tool_consent import bind_mark_policy
        from stiracore.yolo.allow_class import ClassPolicy

        bind_mark_policy(record[1], ClassPolicy(class_key))

    def _claim_ungated(self, event: BeforeToolCallEvent) -> None:
        """ADR 0024 §5: a write-capable call (or ``delegate``) that runs without an approval
        (rule Always) gets its ledger entry here, after every other check, right before it
        runs; ``request_id`` stays empty (``OperationRecord.ungated``). A replayed batch then
        answers it from the ledger (``_replay_guard``) instead of running it again.

        Nothing is recorded for a refused call. A call whose identity cannot be recorded
        (input that cannot be normalized) is refused; a call without ``toolUseId`` cannot
        be replayed by id and is left as before.
        """
        if event.cancel_tool:
            return
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        tool_name = event.tool_use.get("name")
        if not tool_use_id or not isinstance(tool_name, str):
            return
        with self._lock:
            if tool_use_id in self._claimed:
                return  # the approved path claimed it already (``_verify_binding``)
        try:
            ledgered = _ledgered(self.engine, tool_name)
        except (TypeError, AttributeError):
            # An engine without a tool-class configuration (a test double): nothing to
            # classify, the call is left as before ADR 0024. A StiraEngine always has one.
            return
        if not ledgered:
            return
        try:
            digest = args_hash(event.tool_use.get("input", {}))
        except (TypeError, ValueError):
            self._refuse_unrecorded(
                event,
                "Stira could not record this call in its operation ledger (input that cannot "
                "be normalized); not executed.",
            )
            return
        key = operation_key(self.engine.agent_id, tool_name, digest, tool_use_id)
        try:
            claimed = self.engine.operation_ledger.mark_started(
                key,
                agent_id=self.engine.agent_id,
                tool_name=tool_name,
                digest=digest,
                tool_use_id=tool_use_id,
                request_id=None,
            )
        except Exception:  # noqa: BLE001 - a ledger that cannot record refuses the call
            self._refuse_unrecorded(
                event, "Stira could not record this call in its operation ledger; not executed."
            )
            return
        if not claimed:
            claimed = self._renew_ungated(key, tool_name, digest, tool_use_id)
        if not claimed:
            self._refuse_unrecorded(
                event, f"Operation {key} is already recorded; refusing a duplicate execution."
            )
            return
        event.invocation_state.setdefault(HITL_IDEMPOTENCY_KEYS_KEY, {})[tool_use_id] = key
        with self._lock:
            self._claimed[tool_use_id] = key

    def _count_call(self, event: BeforeToolCallEvent) -> None:
        """#448: the last BeforeToolCall check has decided this call; count it in the open
        YOLO window (if any) by decision. Without the approver: nothing (no state either).

        Read here, after every other hook (IdentityGuard, the binding, the ledger, the
        exec mark), so a refusal by any of them counts ``denied`` and a call with no card
        ``always``. A declared checkpoint's own call (#545) is not counted. An automatic approval was counted by ``consume`` (with its slot)."""
        approver = self.auto_approver
        if approver is None:
            return
        state = event.invocation_state
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        decided = _pop(state, YOLO_DECIDED_KEY, tool_use_id)
        window_id = _pop(state, YOLO_COUNTED_KEY, tool_use_id)
        if window_id is None:
            if event.cancel_tool and tool_use_id in state.get(PROGRESS_DECLARED_KEY, ()):
                return  # #545: a declared checkpoint is not a refused call (not counted)
            if event.cancel_tool:
                decision = "manual" if decided == "respond" else "denied"
            elif decided in ("manual", "allowed_again"):
                decision = decided  # ADR 0025 §7: an allowance's answer is never manual
            else:
                decision = "always"
            window_id = _yolo_note(approver, "record_call", event.tool_use.get("name"), decision)
        if type(window_id) is str and not event.cancel_tool:
            state.setdefault(YOLO_RAN_IN_KEY, {})[tool_use_id] = window_id

    def _issue_gate_mark(
        self, event: BeforeToolCallEvent, *, exec_mark_allowed: bool = False
    ) -> None:
        """Last check passed: give this exact file/exec tool call a single-use mark.

        The upstream consent is granted for it and nothing else (#242). An exec tool
        (``HUMAN_APPROVAL_TOOLS``), and an ``http_request`` with a modifying method (#547),
        gets one only when ``exec_mark_allowed`` — this call's
        HITL binding was verified and its ledger entry claimed, and it was a human
        approval or (ADR 0012 phase 7) an exec automatic approval of this very tool
        (#362); otherwise it is cancelled, whatever the rule reader said.

        ``exec_mark_allowed`` means exactly that and nothing more: it opens the exec
        gate mark of this one call. It is **not** a human approval — it is read nowhere
        else; IdentityGuard's breaker exception, the protected-write floor and the
        file tools' marks (``file_write`` / ``editor``) never see it (they read
        ``HITL_APPROVED_TOOL_IDS_KEY``, which only :class:`CheckpointHooks` sets, and
        only for a person's answer). The mark is owned by this call (see
        :meth:`_release_owned`): a cancelled call drops only a mark it was issued
        itself, e.g. on an earlier attempt (#255).
        """
        # A previous attempt of this very call (a retry, or a resume after an
        # interrupt that skipped AfterToolCall) never leaves its mark behind. This
        # runs before the name check, so a record is dropped even if the name changed.
        self._release_owned(event)
        if not needs_gate_mark(event.tool_use):
            return
        if event.cancel_tool:
            return
        name = event.tool_use.get("name")
        if needs_person(event.tool_use) and exec_mark_allowed is not True:
            revoke_gate_mark(event.invocation_state, event.tool_use.get("toolUseId"))
            if modifying_http_call(event.tool_use):
                # #547: the upstream asks its terminal for these; only a person answers.
                method = str(event.tool_use["input"]["method"]).upper()
                event.cancel_tool = (
                    f"{name} was not run: a {method} request may change data where it is "
                    "sent, and runs only after a person approved this exact call (Ask rule, "
                    "approval card); no approval was given for it. GET requests are not "
                    "affected. Ask the person to set http_request to Ask and approve the "
                    "card (#547)."
                )
                return
            event.cancel_tool = (
                f"{name} was not run: exec-class tools run only after a human approved "
                "this exact call (Ask rule, approval card), and no approval was given for "
                "it. Set the tool's rule to Ask and approve the card (#362)."
            )
            return
        mark = mark_gate_passed(event.invocation_state, event.tool_use)
        if mark is None:
            # Fail closed: an empty id or unhashable input, or the same toolUseId
            # already in flight (both calls are refused; the id is ambiguous).
            revoke_gate_mark(event.invocation_state, event.tool_use.get("toolUseId"))
            event.cancel_tool = (
                "Stira could not bind its approval to this call (empty toolUseId, "
                "input that cannot be normalized, or the same toolUseId already in "
                "flight); not executed."
            )
            return
        with self._lock:
            owned = event.invocation_state.setdefault(GATE_ISSUED_KEY, {})
            owned[id(event.tool_use)] = (event.tool_use, mark)

    def _renew_ungated(self, key: str, tool_name: str, digest: str, tool_use_id: str) -> bool:
        """Outside a replay, an ungated entry with this key belongs to an earlier call that
        repeated the provider's ``toolUseId`` (and the arguments); it finished. Record this
        call in its place. Anything else (a replay, an approved entry, an entry still
        ``started`` — the same id in flight) stays refused."""
        replaying = self.replaying
        if replaying is None or replaying(tool_use_id):
            return False
        ledger = self.engine.operation_ledger
        try:
            record = ledger.get(key)
            if record is None or not record.ungated or record.state == LEDGER_STARTED:
                return False
            ledger.forget(key)
            return ledger.mark_started(
                key,
                agent_id=self.engine.agent_id,
                tool_name=tool_name,
                digest=digest,
                tool_use_id=tool_use_id,
                request_id=None,
            )
        except Exception:  # noqa: BLE001 - refuse
            return False

    def _refuse_unrecorded(self, event: BeforeToolCallEvent, text: str) -> None:
        """Cancel a call the ledger could not record, with the gate mark it was just given."""
        self._release_owned(event)
        revoke_gate_mark(event.invocation_state, event.tool_use.get("toolUseId"))
        event.cancel_tool = text

    def _release_owned(self, event: BeforeToolCallEvent | AfterToolCallEvent) -> None:
        """Drop the mark issued to *this* call, if it is still unclaimed (#255).

        Ownership is the identity of the ``tool_use`` dict: the SDK (1.57-1.59)
        hands AfterToolCallEvent the same object its last BeforeToolCallEvent
        callback saw (``before_event.tool_use``; a retry re-enters
        BeforeToolCall with it). Concurrent calls, even with the same
        ``toolUseId``, are different objects. The record keeps the object alive,
        so its ``id`` cannot be reused while the record exists, and it lives in
        ``invocation_state`` so a call that never reaches AfterToolCall leaks
        nothing beyond that invocation. A call this verifier never marked (a
        cancelled one, another tool) finds no record and consumes nothing.
        Stira does not enable the SDK's background tasks, which would hand
        AfterToolCall a copy: such a call would find no record either, and its
        unclaimed mark would stay single-use and bound to its id and arguments.
        """
        state = event.invocation_state
        with self._lock:
            owned = state.get(GATE_ISSUED_KEY) if isinstance(state, dict) else None
            record = owned.get(id(event.tool_use)) if isinstance(owned, dict) else None
            if record is None or record[0] is not event.tool_use:
                return
            del owned[id(event.tool_use)]
        release_gate_mark(state, record[1])
        from stiracore.tools.tool_consent import release_mark_policy

        release_mark_policy(record[1])  # ADR 0025: its class policy ends with it

    def _take_grant(self, event: BeforeToolCallEvent, tool_use_id: str) -> Any:
        grants = event.invocation_state.get(YOLO_GRANTS_KEY)
        return grants.pop(tool_use_id, None) if isinstance(grants, dict) else None

    def _allow_consume(self, event: BeforeToolCallEvent, allow: Any, binding: Any) -> bool:
        """ADR 0025 §4c: the allowance's answer is consumed exactly once here, after every
        other check; refused → not run (the model's retry raises a card)."""
        from stiracore.yolo.allow_runtime import note_use
        from stiracore.yolo.allow_service import REFUSAL_TEXT

        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        tool_name = event.tool_use["name"]
        approver = self.allow_approver
        try:
            run, _code, intent = approver.consume(
                allow,
                tool=tool_name,
                tool_input=event.tool_use.get("input", {}),
                policy=self.scope_policy,
                origin=_allow_origin(self.engine.agent_id),
                args_hash=binding.args_hash,
            )
        except Exception:  # noqa: BLE001 - a consume that fails refuses
            run, intent = False, None
        if not run:
            event.cancel_tool = f"{REFUSAL_TEXT['en']} ({REFUSAL_TEXT['ja']})"
            return False
        event.invocation_state.setdefault(ALLOW_INTENTS_KEY, {})[tool_use_id] = (intent, None)
        record = None
        with contextlib.suppress(Exception):
            record = approver.table.get(allow.allow_id)
        if record is not None:
            note_use(record)
            if record.target.class_kind == "dir_subtree":
                event.invocation_state.setdefault(ALLOW_CLASS_KEY, {})[tool_use_id] = (
                    record.target.class_key
                )
        return True

    def _press_call(self, event: BeforeToolCallEvent, binding: Any) -> bool:
        """ADR 0025 §7: the once-approved call of a card pressed with one of the two buttons
        runs only if its decision row's receipt and its own call row verify here."""
        approver = self.allow_approver
        request_id = binding.request_id or ""
        decision_ref = None
        with contextlib.suppress(Exception):
            decision_ref = approver.pressed(request_id)
        if decision_ref is None:
            return True  # an ordinary "Approve once": today's path
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        try:
            run, intent = approver.record_press_call(
                request_id=request_id,
                tool=event.tool_use["name"],
                args_hash=binding.args_hash,
                decision_ref=decision_ref,
            )
        except Exception:  # noqa: BLE001
            run, intent = False, None
        if not run:
            event.cancel_tool = (
                "Not run: Stira could not record this approval in its audit log "
                "(an audit failure refuses the call)."
            )
            with contextlib.suppress(Exception):
                approver.terminal(request_id, "void")
            return False
        event.invocation_state.setdefault(ALLOW_INTENTS_KEY, {})[tool_use_id] = (
            intent,
            request_id,
        )
        return True

    def _verify_binding(self, event: BeforeToolCallEvent) -> bool:
        """Re-check this call's HITL binding. The answer is ``exec_mark_allowed`` (see
        :meth:`_issue_gate_mark`): True only if the binding holds, the ledger claimed it
        and it was a **human** approval — or (ADR 0012 phase 7, §9 option C) a YOLO
        approval of this exact exec call in a window whose person chose "Include exec".
        Any other YOLO approval is consumed here (§6a) and never counts for an exec-class
        call. Either way the exec gate mark stays bound to this call's arguments and is
        claimed once (``ExecGatedTool``, #362)."""
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        binding = event.invocation_state.get(HITL_BINDINGS_KEY, {}).get(tool_use_id)
        grant = self._take_grant(event, tool_use_id)
        allow = _pop(event.invocation_state, ALLOW_GRANTS_KEY, tool_use_id)
        if binding is None or event.cancel_tool:
            if grant is not None:
                _void_grant(self.auto_approver, grant, "binding_mismatch")
            if allow is not None:
                _void_allow(self.allow_approver, allow)
            if binding is not None and allow is None and self.allow_approver is not None:
                with contextlib.suppress(Exception):
                    if self.allow_approver.pressed(binding.request_id or ""):
                        self.allow_approver.terminal(binding.request_id, "void")
            return False
        tool_name = event.tool_use["name"]
        tool_input = event.tool_use.get("input", {})
        refusal = binding.check(
            agent_id=self.engine.agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            now=self.engine._now_fn(),
        )
        if refusal is not None:
            event.cancel_tool = f"HITL rejected this write operation: {refusal}."
            if grant is not None:
                _void_grant(self.auto_approver, grant, "binding_mismatch")
            if allow is not None:
                _void_allow(self.allow_approver, allow)
            return False
        bound = event.invocation_state.get(HITL_POLICY_GENERATIONS_KEY, {}).get(tool_use_id)
        if self.policy_check is not None and (
            bound is None or read_policy(self.policy_check, tool_name) != bound
        ):
            # Execution boundary: the rule generation must still be the approved one.
            event.cancel_tool = "Tool policy changed after approval; not executed."
            if grant is not None:
                _void_grant(self.auto_approver, grant, "binding_mismatch")
            if allow is not None:
                _void_allow(self.allow_approver, allow)
            return False
        if self.allow_approver is not None and not (
            self._allow_consume(event, allow, binding)
            if allow is not None
            else (grant is not None or self._press_call(event, binding))
        ):
            return False
        if grant is not None:
            # ADR 0012 §6a consume: incarnation, generation, epoch, clocks, ``active``;
            # exactly once. Refused → not executed (the slot went back, ``void``).
            try:
                consumed, _code = self.auto_approver.consume(grant)
            except Exception:  # noqa: BLE001 - a consume that fails refuses
                consumed = False
            if not consumed:
                event.cancel_tool = (
                    "The YOLO automatic approval is no longer valid (the window paused, "
                    "ended or changed); not executed."
                )
                return False
            # #448: ``consume`` counted this call in its window (with the slot).
            event.invocation_state.setdefault(YOLO_COUNTED_KEY, {})[tool_use_id] = getattr(
                grant, "window_id", None
            )
        key = operation_key(self.engine.agent_id, tool_name, binding.args_hash, tool_use_id)
        try:
            claimed = self.engine.operation_ledger.mark_started(
                key,
                agent_id=self.engine.agent_id,
                tool_name=tool_name,
                digest=binding.args_hash,
                tool_use_id=tool_use_id,
                request_id=binding.request_id or None,
            )
        except Exception:  # noqa: BLE001 - #601: a ledger that cannot record refuses the call
            event.cancel_tool = (
                "Stira could not record this call in its operation ledger; not executed."
            )
            return False
        if not claimed:
            event.cancel_tool = (
                f"HITL operation {key} is already recorded; refusing a duplicate execution."
            )
            return False
        event.invocation_state.setdefault(HITL_IDEMPOTENCY_KEYS_KEY, {})[tool_use_id] = key
        with self._lock:
            self._claimed[tool_use_id] = key
        if allow is not None:
            return False  # an allowance's answer is never a human approval (no exec mark)
        if grant is None:
            return True
        # Phase 7: only an exec approval (``AutoGrant.exec_class``, issued for ``shell`` /
        # ``batch`` in a window with the exec flag) for this very tool.
        return getattr(grant, "exec_class", False) is True and getattr(grant, "tool", None) == (
            tool_name
        )

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        self._release_owned(event)
        allowed = _pop(event.invocation_state, ALLOW_INTENTS_KEY, tool_use_id)
        if allowed is not None and self.allow_approver is not None:
            # ADR 0025 §7 / §9: the outcome row of an audited call, and the card's terminal
            # result Make Always waits for.
            failed = (
                getattr(event, "exception", None) is not None
                or (getattr(event, "result", None) or {}).get("status") != "success"
            )
            intent, request_id = allowed
            with contextlib.suppress(Exception):
                self.allow_approver.outcome(
                    intent, "failed" if failed else "committed", request_id=request_id
                )
        ran_in = _pop(event.invocation_state, YOLO_RAN_IN_KEY, tool_use_id)
        if ran_in is not None and (
            getattr(event, "exception", None) is not None
            or (getattr(event, "result", None) or {}).get("status") == "error"
        ):
            # #448: it ran (it was not refused) and returned an error.
            _yolo_note(self.auto_approver, "record_failure", ran_in)
        with self._lock:
            key = self._claimed.pop(tool_use_id, None)
        if key is None:
            return
        event.invocation_state.get(HITL_IDEMPOTENCY_KEYS_KEY, {}).pop(tool_use_id, None)
        status = (event.result or {}).get("status")
        self.engine.operation_ledger.finish(key, success=status == "success")


_PERSON_ANSWERS = (HITLAction.APPROVE, HITLAction.EDIT, HITLAction.RESPOND)
logger = logging.getLogger(__name__)


def _pop(state: Any, key: str, tool_use_id: str) -> Any:
    entries = state.get(key) if isinstance(state, dict) else None
    return entries.pop(tool_use_id, None) if isinstance(entries, dict) else None


def _yolo_note(approver: Any, method: str, *args: Any) -> Any:
    """#448: count something in the YOLO window (best effort: a count never changes or
    breaks a tool call; an approver without the method counts nothing)."""
    note = getattr(approver, method, None) if approver is not None else None
    if note is None:
        return None
    try:
        return note(*args)
    except Exception:  # noqa: BLE001 - observation only
        logging.getLogger(__name__).warning("YOLO: counting a tool call failed")
        return None


def _allow_origin(agent_id: Any) -> str | None:
    """ADR 0025 §2: ``ORIGIN_CHAT`` inside a person's chat / Stage turn of ``agent_id``
    (not a window's automatic continue), else ``None`` — never answered."""
    from stiracore.yolo import origin
    from stiracore.yolo.allow_class import ORIGIN_CHAT

    try:
        return ORIGIN_CHAT if origin.covered(agent_id) and origin.started_by_person() else None
    except Exception:  # noqa: BLE001
        return None


def _void_allow(approver: Any, binding: Any) -> None:
    """Drop an unconsumed allowance answer (the call was refused by a later check)."""
    if approver is None:
        return
    with contextlib.suppress(Exception):
        approver.table.void(binding)


def _void_grant(approver: Any, grant: Any, reason: str) -> None:
    """Return an unused YOLO approval's slot (best effort: a void that fails leaves
    the slot reserved until the window ends — never a use)."""
    if approver is None:
        return
    try:
        approver.void(grant, reason)
    except Exception:  # noqa: BLE001 - the call is refused either way
        logging.getLogger(__name__).warning("YOLO: returning an unused approval failed")
