"""Register in dashboard/app.py:
    app.register_blueprint(create_chat_blueprint(nxp_agent))
Pass the existing NXPAgent instance; protect these routes with dashboard auth.
Optional memory_store: get_recent_turns(limit=50), append_turn(turn).
status_provider() overrides status fields for project-specific state adapters.
runner(message) may return sync/async chunks or a final Strands AgentResult.
token_rates: (input, output) USD per million tokens; None means unknown cost.
token_prices: model id -> Pricing (gateway-config ``token_prices``, #451); each turn is
priced by its own model, and a model without an entry is never priced (nor free).
Defaults are process-local; use shared adapters for multi-worker deployments.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from copy import deepcopy
from enum import Enum
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
)
from strands.types.exceptions import MaxTokensReachedException
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from stiracore.core import (
    HITLAction,
    HITLDecision,
    HITLRequest,
    breaker_reasons,
    containment,
    hitl_reasons,
)
from stiracore.core.approval import args_hash, canonical_json
from stiracore.core.approval_store import (
    APPROVED,
    CANCELLED,
    EDITED,
    EXPIRED,
    MAX_CHAIN_LINKS,
    PENDING,
    ApprovalStoreError,
    StoreOutcome,
    normalize_origin,
    stricter_than_requested,
)
from stiracore.core.hitl_reasons import reason_code, reason_params, resolution_code
from stiracore.core.turn_lock import TurnLock
from stiracore.observability import activity
from stiracore.observability.chat_images import MAX_CHAT_BYTES, decode_images
from stiracore.observability.masking import mask_secrets
from stiracore.observability.security import DUPLICATE_ERROR, idempotency_release
from stiracore.observability.turn_events import (
    USAGE_CANCELLED,
    USAGE_INTERRUPTED,
    USAGE_NOT_REPORTED,
)
from stiracore.providers.failure import describe_model_failure
from stiracore.providers.usage import normalize_strands_usage
from stiracore.yolo import allow_runtime
from stiracore.yolo import origin as yolo_origin
from stiracore.yolo import runtime as yolo_runtime
from stiracore.yolo.session_allow import publish_end as publish_allow_end


class DecideOutcome(Enum):
    """Result of ``HITLApprovalBroker.decide``; only ACCEPTED is truthy."""

    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"
    BINDING_MISMATCH = "binding_mismatch"
    EXPIRED = "expired"
    # Durable store (#240): unreadable/tampered store or clock anomaly -> nothing recorded.
    STORE_UNAVAILABLE = "store_unavailable"
    # A request restored after a restart accepts approve / edit (unmasked) / reject only.
    UNSUPPORTED = "unsupported"
    # The shared reset/decision lock (#350) was not free in time: nothing was recorded.
    BUSY = "busy"
    # Approvals are refused while open ones could not be revoked after a rule save.
    SUSPENDED = "suspended"
    # ADR 0024 §3: the breaker is stricter than when the request was raised; approve /
    # edit wait for it to recover (nothing recorded, the request stays pending).
    BREAKER = "breaker"

    def __bool__(self) -> bool:
        return self is DecideOutcome.ACCEPTED


_OUTCOME_STATUS = {
    DecideOutcome.ACCEPTED: 202,
    DecideOutcome.NOT_FOUND: 404,
    DecideOutcome.ALREADY_DECIDED: 409,
    DecideOutcome.BINDING_MISMATCH: 409,
    DecideOutcome.EXPIRED: 410,
    DecideOutcome.STORE_UNAVAILABLE: 503,
    DecideOutcome.UNSUPPORTED: 409,
    DecideOutcome.SUSPENDED: 503,
    DecideOutcome.BUSY: 503,
    DecideOutcome.BREAKER: 409,
}
_OUTCOME_ERROR = {
    DecideOutcome.NOT_FOUND: "approval request not found",
    DecideOutcome.ALREADY_DECIDED: "approval request already decided",
    DecideOutcome.BINDING_MISMATCH: "approval does not match the pending request; reload it",
    DecideOutcome.EXPIRED: "approval request expired; it cannot be approved any more",
    DecideOutcome.STORE_UNAVAILABLE: "approval store unavailable; nothing was recorded",
    DecideOutcome.UNSUPPORTED: "this request accepts approve, reject, or an edit of "
    "unmasked arguments only (a restored request cannot be answered)",
    DecideOutcome.BUSY: "a new conversation is being started; nothing was recorded, the request "
    "is still pending: try again",
    DecideOutcome.SUSPENDED: "approvals are refused: the tool rules changed and the open "
    "approvals could not be revoked; fix the approval store and save the rules again",
    DecideOutcome.BREAKER: "the circuit breaker is stricter than when this was requested; "
    "approve or edit waits until it recovers (reject is possible; the expiry is not extended)",
}
_STORE_OUTCOME = {
    StoreOutcome.ACCEPTED: DecideOutcome.ACCEPTED,
    StoreOutcome.NOT_FOUND: DecideOutcome.NOT_FOUND,
    StoreOutcome.ALREADY_DECIDED: DecideOutcome.ALREADY_DECIDED,
    StoreOutcome.BINDING_MISMATCH: DecideOutcome.BINDING_MISMATCH,
    StoreOutcome.EXPIRED: DecideOutcome.EXPIRED,
    StoreOutcome.REFUSED: DecideOutcome.UNSUPPORTED,
}
_RESOLUTION = {
    "approve": "approved",
    "edit": "edited",
    "reject": "rejected",
    "respond": "responded",
}
HISTORY_INPUT_LIMIT = 2000
logger = logging.getLogger(__name__)


class HITLApprovalBroker:
    """Process-local synchronous handoff between a tool worker and chat HTTP UI.

    Each request is bound (#146) to agent, tool, normalized-argument hash and an
    expiry. ``decide`` accepts exactly one decision per request, refuses one whose
    ``args_hash`` differs from what is pending, and refuses anything after expiry.
    An unanswered request (UI never reached, tab closed, server shutting down)
    resolves as a rejection: fail-closed. Resolved requests are kept in a bounded
    in-memory history.

    With a durable ``store`` (#240, ADR 0006) every request and decision is also
    recorded in a hash-chained :class:`ApprovalStore`. Shutdown still rejects the
    waiting worker, but the request stays pending in the store; after a restart it
    is listed with ``restored: true``. Approving it calls ``on_resume(request_id)``
    (the host replays the paused tool batch from its checkpoint), and the replayed
    gate is answered from the store, consuming the approval once. A store that is
    unreadable/tampered, or a clock anomaly, rejects everything (fail-closed).
    """

    def __init__(
        self,
        on_event=None,
        *,
        ttl_sec: float = 120.0,
        now_fn=None,
        turn_id_provider=None,
        history_limit: int = 200,
        store=None,
        checkpoint_id_provider=None,
        on_resume=None,
        on_reject=None,
        decision_guard=None,
        breaker_level=None,
        replay_preview=None,
    ) -> None:
        self._condition = threading.Condition()
        self._pending: dict[str, dict] = {}
        self._decisions: dict[str, HITLDecision] = {}
        self._history: deque[dict] = deque(maxlen=history_limit)
        self._resolved_ids: dict[str, str] = {}
        self._on_event = on_event
        self._ttl = float(ttl_sec)
        self._now = now_fn or time.time
        self._turn_id_provider = turn_id_provider
        self._closed = False
        # ADR 0011 W5: live cards of a grant iteration withdrawn by its stop (bounded).
        self._withdrawn: deque[str] = deque(maxlen=64)
        # Saves that passed the closed check and are still writing (#285): close()
        # waits for them, so nothing reaches the store after close() returned.
        self._saving = 0
        # Ids of those saves (#400): already in the store, not yet in ``_pending``. They
        # are this process's live requests, never listed or decided as restored ones.
        self._saving_ids: set[str] = set()
        self._store = store
        # Set while open approvals could not be revoked after a rule save (#330).
        self._suspended: str | None = None
        self._checkpoint_id_provider = checkpoint_id_provider
        self._on_resume = on_resume
        # ``(request_id) -> None`` after a recorded rejection (a scheduler wake-up only).
        self._on_reject = on_reject
        # Held around a whole decision, from the store transition to scheduling the resume
        # (#350): a conversation reset holding the same lock never falls between the two.
        self._decision_guard = (
            decision_guard if decision_guard is not None else contextlib.nullcontext
        )
        # ADR 0024 §3: ``() -> level`` of the breaker now (``engine.breaker_status().level``).
        # With it, approve / edit wait while the breaker ranks above the request's level.
        # None: no breaker to compare with (a host without an engine), nothing is frozen.
        self._breaker_level = breaker_level
        # ADR 0024 §5: ``record -> {"from_ledger", "unknown"}`` for a restored card (the
        # other calls of its paused batch); None: the card says nothing about them.
        self._replay_preview = replay_preview
        # Counts per restored request (its batch does not run while it waits): one
        # checkpoint read per card, not one per poll. Bounded.
        self._replay_counts: OrderedDict[str, dict | None] = OrderedDict()
        if store is not None:
            with contextlib.suppress(ApprovalStoreError):
                for record in store.pending():
                    if str(record.agent_id or "").startswith("grant:"):
                        # ADR 0011 W5: a grant iteration ends with its process.
                        store.cancel(record.request_id, reason=hitl_reasons.GRANT_STOPPED)
                    elif record.live_only:
                        # Its worker is gone and its arguments were never stored.
                        store.cancel(
                            record.request_id,
                            reason=hitl_reasons.LIVE_ONLY,
                        )
                    elif (record.origin or {}).get("kind") == "job":
                        # ADR 0024 §4: job runs do not survive a restart; their cards
                        # are never restored (no job resume exists, phase 2).
                        store.cancel(record.request_id, reason=hitl_reasons.JOB_RESTART)
                for record in reversed(store.history(history_limit)):
                    self._history.append(_record_entry(record))
                    self._resolved_ids[record.request_id] = record.status

    def suspend_approvals(self, reason: str) -> None:
        """Refuse every approve/edit/respond until :meth:`resume_approvals` (#330).

        Rejections stay possible. Used when a saved tool rule could not void the open
        approvals: none of them may be approved meanwhile (fail-closed).
        """
        with self._condition:
            self._suspended = str(reason)

    def resume_approvals(self) -> None:
        """The open approvals are reconciled with the rules again."""
        with self._condition:
            self._suspended = None

    def approvals_suspended(self) -> str | None:
        with self._condition:
            return self._suspended

    def current_breaker_level(self) -> str | None:
        """The breaker level now, as a name; ``"unknown"`` when it cannot be read (counts
        as critical); None without a breaker reader."""
        if self._breaker_level is None:
            return None
        try:
            level = self._breaker_level()
            level = getattr(level, "value", level)
        except Exception:  # noqa: BLE001 - unreadable: the strictest reading
            return "unknown"
        return level if isinstance(level, str) else "unknown"

    def frozen(self, break_level, current: str | None = None) -> bool:
        """ADR 0024 §3: approve / edit of a request raised at ``break_level`` wait now."""
        if self._breaker_level is None:
            return False
        level = self.current_breaker_level() if current is None else current
        return stricter_than_requested(level, break_level)

    def store_status(self) -> dict | None:
        """Durable store health for the UI (None: process-local broker)."""
        return None if self._store is None else self._store.status()

    def _notify(self, event_type: str, data: dict) -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(event_type, data)

    def _turn_id(self):
        if self._turn_id_provider is None:
            return None
        with contextlib.suppress(Exception):
            return self._turn_id_provider()
        return None

    def request(
        self, hitl_request: HITLRequest, *, durable_display: dict | None = None
    ) -> HITLDecision:
        """``durable_display`` (ADR 0011 W5, a grant iteration's card): the codes-only
        summary the durable record, the history and every log keep instead of the
        arguments; the arguments stay in memory for the live card only."""
        tool_input = deepcopy(getattr(hitl_request, "tool_input", {}))
        try:
            normalized = canonical_json(tool_input)
            digest = args_hash(tool_input)
        except (TypeError, ValueError):
            return HITLDecision(HITLAction.REJECT, reason="tool input cannot be normalized")
        supplied = getattr(hitl_request, "args_hash", None)
        if supplied is not None and supplied != digest:
            return HITLDecision(HITLAction.REJECT, reason="request hash does not match its input")
        if self._store is not None:
            durable = self._durable_decision(hitl_request, digest)
            if durable is not None:
                return durable
        now = self._now()
        request_id = getattr(hitl_request, "request_id", None) or uuid4().hex
        break_level = getattr(hitl_request, "break_level", None)
        # ADR 0011 W5: a grant iteration's card belongs to no chat turn and never survives
        # a restart (live only: no checkpoint to resume, cancelled at the next start).
        grant_card = str(getattr(hitl_request, "agent_id", None) or "").startswith("grant:")
        durable = canonical_json(_grant_card_display(durable_display)) if grant_card else None
        pending = {
            "id": request_id,
            "agent_id": getattr(hitl_request, "agent_id", None),
            "tool_name": hitl_request.tool_name,
            "tool_input": tool_input,
            "normalized_input": normalized,
            "args_hash": digest,
            "break_level": getattr(break_level, "value", break_level),
            "reason": hitl_request.reason,
            "reason_code": reason_code(hitl_request.reason),
            "reason_params": reason_params(hitl_request.reason, break_level),
            # ADR 0011 W5: a grant card's id is the harness's own (the model's toolUseId is
            # model-chosen text); nothing durable or evented carries the model's one.
            "tool_use_id": ("grant-" + uuid4().hex[:16])
            if grant_card
            else hitl_request.tool_use_id,
            "supersedes": getattr(hitl_request, "supersedes", None),
            # Shown only when the operation itself names a working directory (#189).
            "cwd": None if grant_card else _text_field(tool_input, "cwd"),
            "turn_id": None if grant_card else self._turn_id(),
            **({"grant_card": True, "durable_display": durable} if grant_card else {}),
            "created_at": now,
            "expires_at": getattr(hitl_request, "expires_at", None) or now + self._ttl,
            "status": "pending",
            # ADR 0025 §2/§3: a person's chat / Stage turn of this session (server only;
            # the card's view never shows it). Anything else is never offered the buttons.
            "allow_origin": allow_runtime.card_origin(getattr(hitl_request, "agent_id", None)),
        }
        resolved = []
        with self._condition:
            if self._closed:
                # Shutting down (#285): never store a request nobody can answer, or the
                # next start would restore it as if a turn were waiting for it.
                return HITLDecision(HITLAction.REJECT, reason="approval closed")
            if (
                request_id in self._pending
                or request_id in self._saving_ids
                or request_id in self._resolved_ids
            ):
                return HITLDecision(HITLAction.REJECT, reason="duplicate approval request id")
            # Checked and registered under one lock: close() cannot complete in between.
            self._saving += 1
            self._saving_ids.add(request_id)
        try:
            saved = self._store is None or self._persist(pending)
        except BaseException:
            with self._condition:
                self._end_saving_locked(request_id)
            raise
        with self._condition:
            # Leaving the saving set and entering ``_pending`` is one step under the lock
            # (#400): pending()/decide() never see the stored request as restored.
            self._end_saving_locked(request_id)
            if not saved:
                return HITLDecision(HITLAction.REJECT, reason="approval store unavailable")
            if request_id in self._pending or request_id in self._resolved_ids:
                return HITLDecision(HITLAction.REJECT, reason="duplicate approval request id")
            self._pending[request_id] = pending
            if pending["supersedes"]:
                for entry in self._history:
                    if entry["id"] == pending["supersedes"]:
                        entry["superseded_by"] = request_id
            self._condition.notify_all()
        if not grant_card:
            # A grant card belongs to no chat turn: no turn event, no activity row.
            self._notify("approval.requested", _approval_data(pending))
        with self._condition:
            while request_id not in self._decisions:
                remaining = pending["expires_at"] - self._now()
                if request_id in self._withdrawn:
                    # ADR 0011 W5: the iteration that asked stopped (kill, revoke, a
                    # person's chat turn, the wall time): nothing may run on this card.
                    resolved.append(
                        self._resolve_locked(
                            request_id,
                            HITLDecision(HITLAction.REJECT, reason=hitl_reasons.GRANT_STOPPED),
                            status="cancelled",
                            resolver="system",
                        )
                    )
                    break
                if self._closed or remaining <= 0:
                    outcome = "closed" if self._closed else "expired"
                    closing = hitl_reasons.CLOSED if self._closed else hitl_reasons.EXPIRED
                    resolved.append(
                        self._resolve_locked(
                            request_id,
                            HITLDecision(HITLAction.REJECT, reason=closing),
                            status=outcome,
                            resolver="system",
                        )
                    )
                    break
                self._condition.wait(timeout=min(remaining, 0.25))
            decision = self._decisions.pop(request_id)
        for entry in resolved:
            if self._store is not None and entry["status"] == "expired":
                with contextlib.suppress(ApprovalStoreError):
                    self._store.expire(request_id)
            elif self._store is not None and entry["status"] == "cancelled":
                with contextlib.suppress(ApprovalStoreError):
                    self._store.cancel(request_id, reason=hitl_reasons.GRANT_STOPPED)
            self._notify_resolved(entry)
        if self._store is not None and decision.action == HITLAction.APPROVE:
            # Used now by this live worker: never offered again after a restart.
            try:
                consumed = self._store.consume(request_id, by_request_id=request_id)
            except ApprovalStoreError:
                consumed = False
            if not consumed:
                return HITLDecision(
                    HITLAction.REJECT,
                    request_id=request_id,
                    reason="approval could not be recorded as used",
                )
        return decision

    def _end_saving_locked(self, request_id: str) -> None:
        self._saving -= 1
        self._saving_ids.discard(request_id)
        self._condition.notify_all()

    def _persist(self, pending: dict) -> bool:
        checkpoint_id = None
        if self._checkpoint_id_provider is not None and not pending.get("grant_card"):
            with contextlib.suppress(Exception):
                checkpoint_id = self._checkpoint_id_provider()
        origin = {"kind": "turn", "id": pending["turn_id"]} if pending["turn_id"] else {}
        try:
            origin = normalize_origin(origin)  # ADR 0024 §2: the closed form only
        except ValueError:
            origin = {}  # a turn id that is not a short identifier is not kept as origin
        edited = pending["supersedes"] is not None
        try:
            # An EDIT replacement exists nowhere else; keep it to resume (ADR 0006 §1),
            # unless it contains a secret: then it is approved live only (never stored).
            live_only = pending.get("grant_card") is True or (
                edited and self._store.would_mask(pending["tool_input"])
            )
            self._store.submit(
                request_id=pending["id"],
                agent_id=pending["agent_id"] or "",
                tool_name=pending["tool_name"],
                tool_input=pending["tool_input"],
                expires_at=pending["expires_at"],
                tool_use_id=pending["tool_use_id"],
                correlation_id=pending["turn_id"],
                origin=origin,
                checkpoint_id=checkpoint_id if isinstance(checkpoint_id, str) else None,
                supersedes=pending["supersedes"],
                reason=pending["reason"],
                break_level=pending["break_level"],
                store_payload=edited and not live_only,
                live_only=live_only,
                display=pending.get("durable_display"),
            )
        except (ApprovalStoreError, TypeError, ValueError):
            return False
        return True

    def _durable_decision(self, hitl_request, digest):
        """Answer a replayed call from an approval recorded before a restart.

        A stored approval is handed out only inside the claimed, in-process resume
        of that very request (``ApprovalStore.resume_context``, entered by
        ``resume_approved_request`` after checking its checkpoint), for the same
        agent / tool / toolUseId / argument hash / correlation id, and at most once.
        Any other turn with the same call identity asks a human as usual.

        None: ask a human. Otherwise the decision to return: APPROVE consumes the
        stored approval; EDIT hands back the approved replacement (the engine
        re-reviews it, which consumes the replacement's approval); anything
        ambiguous is a rejection.
        """
        tool_use_id = getattr(hitl_request, "tool_use_id", None)
        if not tool_use_id:
            return None
        agent_id = getattr(hitl_request, "agent_id", None) or ""
        store = self._store
        try:
            records = store.for_tool_use(agent_id, hitl_request.tool_name, tool_use_id)
            resumed = [r for r in records if store.resuming(r.request_id)]
            if not resumed:
                with self._condition:
                    live_ids = set(self._pending) | self._saving_ids
                for record in records:
                    if (
                        record.args_hash == digest
                        and record.status == PENDING
                        and record.request_id not in live_ids
                    ):
                        # A replay without a decision: the restored entry is replaced.
                        store.cancel(record.request_id, reason=hitl_reasons.REPLACED)
                return None
            same = [r for r in resumed if r.args_hash == digest]
            if not same:
                for record in resumed:
                    if record.status == APPROVED and not record.consumed:
                        store.note_binding_mismatch(record.request_id, digest)
                        store.cancel(record.request_id, reason=hitl_reasons.BINDING_MISMATCH)
                return HITLDecision(
                    HITLAction.REJECT, reason="the approval is bound to different arguments"
                )
            record = same[-1]
            if record.status == APPROVED:
                if store.consume(
                    record.request_id,
                    by_request_id=getattr(hitl_request, "request_id", None),
                    resumed=True,
                ):
                    return HITLDecision(
                        HITLAction.APPROVE, reason="approved while the turn was paused"
                    )
                return HITLDecision(HITLAction.REJECT, reason="approval expired or already used")
            if record.status == EDITED:
                successor = _edit_chain_end(store, record)
                if (
                    successor is None
                    or not store.resuming(successor.request_id)
                    or successor.status != APPROVED
                    or not successor.resume_claimed
                    or successor.consumed
                    or self._now() > successor.expires_at
                ):
                    return HITLDecision(
                        HITLAction.REJECT, reason="the edited request is not approved"
                    )
                replacement = store.payload(successor.request_id)
                if replacement is None:
                    return HITLDecision(HITLAction.REJECT, reason="the edited input is unavailable")
                return HITLDecision(HITLAction.EDIT, edited_input=replacement)
            return HITLDecision(HITLAction.REJECT, reason="the paused request is not approved")
        except ApprovalStoreError:
            return HITLDecision(HITLAction.REJECT, reason="approval store unavailable")

    def _resolve_locked(self, request_id, decision, *, status, resolver, client_id=None) -> dict:
        pending = self._pending.pop(request_id)
        self._decisions[request_id] = decision
        entry = {
            **{
                key: value
                for key, value in pending.items()
                if key not in ("tool_input", "allow_origin")
            },
            # History is display only: masked like the queue (#185). A grant iteration's
            # card keeps only its codes-only summary (ADR 0011 W5).
            "normalized_input": (
                pending["durable_display"]
                if pending.get("grant_card")
                else canonical_json(mask_secrets(pending["tool_input"]))
            )[:HISTORY_INPUT_LIMIT],
            # Free text is display only too: cwd, the request's and the operator's reason.
            "cwd": _masked_text(pending.get("cwd")),
            "reason": _masked_text(pending.get("reason")),
            "status": status,
            "action": decision.action.value,
            "resolution_reason": _masked_text(decision.reason),
            "resolution_reason_code": resolution_code(decision.reason, resolver),
            "resolver": resolver,
            "client_id": client_id,
            "resolved_at": self._now(),
            "superseded_by": None,
        }
        self._history.append(entry)
        # ADR 0025: the card ended — its view token is dead; a card a button was pressed
        # on that ends without running is a terminal result for Make Always (§9).
        allow_runtime.card_ended(request_id, status)
        self._resolved_ids[request_id] = status
        while len(self._resolved_ids) > max(self._history.maxlen or 0, 1) * 4:
            self._resolved_ids.pop(next(iter(self._resolved_ids)))
        self._condition.notify_all()
        return deepcopy(entry)

    def _notify_resolved(self, entry: dict) -> None:
        if entry.get("grant_card"):
            return  # ADR 0011 W5: never on a chat turn's event stream (the grant's own path)
        action = entry["action"]
        self._notify(
            "approval.resolved",
            {
                **_approval_data(entry),
                "decision": "deny" if action == "reject" else action,
                "action": action,
                "status": entry["status"],
                "resolver": entry["resolver"],
                "resolution_reason": _masked_text(entry["resolution_reason"]),
                "resolution_reason_code": entry["resolution_reason_code"],
            },
        )

    def pending_entry(self, request_id: str) -> dict | None:
        """ADR 0025: a copy of one **live** pending request as stored (unmasked arguments,
        its origin) — the server recomputes the allow-again class from it, never from the
        page. A restored request (no live worker) is ``None``."""
        with self._condition:
            entry = self._pending.get(request_id)
            return deepcopy(entry) if entry is not None else None

    def pending(self, *, strict: bool = False) -> list[dict]:
        """Live requests, then requests restored from the durable store (``restored``).

        ``strict`` (#601, the /new pre-check): a store that cannot be read raises
        :class:`ApprovalStoreError` instead of listing only the live requests."""
        if self._store is None:
            with self._condition:
                return self._with_hold(deepcopy(list(self._pending.values())))
        # A live request the store already ended (a saved rule revoked it, #287) is
        # rejected now, not listed until its worker times out.
        self.sync_from_store()
        with self._condition:
            live = deepcopy(list(self._pending.values()))
        try:
            for expired_id in self._store.expire_due():
                self._record_resolved(expired_id)
            stored = self._store.pending()
        except ApprovalStoreError:
            if strict:
                raise
            return live  # an untrusted store lists nothing it could resume
        known = {item["id"] for item in live}
        with self._condition:
            # Checked after the store read (#400): a live request saved meanwhile is still
            # being saved or registered by now; it is listed live next time, never restored.
            known |= set(self._pending) | self._saving_ids
        restored = []
        for record in stored:
            if record.request_id in known or record.live_only:
                continue
            entry = _restored_pending(record)
            if self._replay_preview is not None:
                entry["replay"] = self._replay_counts_of(record)
            restored.append(entry)
        return self._with_hold(live + restored)

    def _replay_counts_of(self, record) -> dict | None:
        with self._condition:
            if record.request_id in self._replay_counts:
                return deepcopy(self._replay_counts[record.request_id])
        try:
            counts = self._replay_preview(record)
        except Exception:  # noqa: BLE001 - display only
            counts = None
        counts = dict(counts) if isinstance(counts, dict) else None
        with self._condition:
            self._replay_counts[record.request_id] = counts
            while len(self._replay_counts) > 64:
                self._replay_counts.popitem(last=False)
        return deepcopy(counts)

    def _with_hold(self, items: list[dict]) -> list[dict]:
        """Mark entries whose approve / edit wait for the breaker (ADR 0024 §3)."""
        if self._breaker_level is None:
            return items
        current = self.current_breaker_level()
        for item in items:
            item["breaker_hold"] = self.frozen(item.get("break_level"), current)
        return items

    def history(self, limit: int = 50) -> list[dict]:
        """Newest-first resolved requests (approved, rejected, edited, expired...).

        With a store, an entry shows the store's final state: an approval this
        process recorded and the store later cancelled or expired (a saved rule,
        a refused resume) reads ``cancelled``/``expired``, not ``approved`` (#287).
        One entry per request; requests the store ended without this process (a
        restored card voided by a saved rule) are listed as after a restart.
        """
        with self._condition:
            newest_first = deepcopy(list(self._history)[::-1])
        entries, seen = [], set()
        for entry in newest_first:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                entries.append(entry)
        if self._store is None:
            return entries[:limit]
        records = self._store_records([entry["id"] for entry in entries[:limit]])
        for entry in entries[:limit]:
            record = records.get(entry["id"])
            ended = record is not None and record.status in (CANCELLED, EXPIRED)
            if ended and entry["status"] != record.status:
                entry.update(
                    status=record.status,
                    action="reject",
                    resolution_reason=record.resolution_reason,
                )
        try:
            stored = self._store.history(limit)
        except ApprovalStoreError:
            stored = []
        entries.extend(_record_entry(r) for r in stored if r.request_id not in seen)
        entries.sort(key=lambda e: e.get("resolved_at") or e.get("created_at") or 0, reverse=True)
        return entries[:limit]

    def _store_records(self, request_ids) -> dict:
        if self._store is None or not request_ids:
            return {}
        try:
            return self._store.get_many(request_ids)
        except ApprovalStoreError:
            return {}

    def sync_from_store(self) -> None:
        """Reject live requests the durable store already ended (#287).

        A saved tool rule revokes every open approval in the store (#190); the
        worker waiting on one of them gets a rejection now (fail-closed) and its
        card leaves the queue, instead of staying approvable until it times out.
        """
        with self._condition:
            live_ids = list(self._pending)
        records = self._store_records(live_ids)
        resolved = []
        with self._condition:
            for request_id in live_ids:
                record = records.get(request_id)
                if record is None or record.status not in (CANCELLED, EXPIRED):
                    continue
                if request_id not in self._pending:
                    continue
                reason = record.resolution_reason or f"approval {record.status}"
                resolved.append(
                    self._resolve_locked(
                        request_id,
                        HITLDecision(HITLAction.REJECT, reason=reason),
                        status=record.status,
                        resolver="system",
                    )
                )
        for entry in resolved:
            self._notify_resolved(entry)

    def decide(
        self,
        request_id: str,
        decision: HITLDecision,
        *,
        args_hash: str | None = None,
        resolver: str = "operator",
        client_id: str | None = None,
    ) -> DecideOutcome:
        """Resolve one pending request, exactly once, only for what was displayed.

        Anything but a rejection must quote the pending ``args_hash``. A refused
        decision leaves the request pending (except after expiry). While approvals
        are suspended only a rejection is accepted.
        """
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(self._decision_guard())
            except TimeoutError:  # LockBusy, or a host guard's own timeout (#601)
                # Only the acquisition: nothing was decided, the request stays as it was
                # (a retry decides it). An exception from _decide below is never turned
                # into "busy" — it may come after something was recorded.
                return DecideOutcome.BUSY
            return self._decide(
                request_id,
                decision,
                args_hash=args_hash,
                resolver=resolver,
                client_id=client_id,
            )

    def _decide(self, request_id, decision, *, args_hash, resolver, client_id) -> DecideOutcome:
        if _decision_action(decision) != HITLAction.REJECT.value and self.approvals_suspended():
            return DecideOutcome.SUSPENDED
        # Read once, outside the broker's lock (the engine has its own); the replay's claim
        # repeats the comparison under the store's write lock (ADR 0024 §3).
        current = (
            self.current_breaker_level() if _decision_action(decision) in _HELD_ACTIONS else None
        )
        with self._condition:
            pending = self._pending.get(request_id)
            if pending is None and request_id in self._saving_ids:
                # Still being saved by its live worker (#400): not listed yet, and never
                # the restored path (that would schedule a resume of a running turn).
                return DecideOutcome.NOT_FOUND
            if pending is None and self._store is not None:
                restored = True
            elif pending is None:
                previous = self._resolved_ids.get(request_id)
                if previous is None:
                    return DecideOutcome.NOT_FOUND
                if previous == "expired":
                    return DecideOutcome.EXPIRED
                return DecideOutcome.ALREADY_DECIDED
            else:
                restored = False
        if restored:
            return self._decide_restored(
                request_id,
                decision,
                args_hash=args_hash,
                resolver=resolver,
                client_id=client_id,
                current=current,
            )
        with self._condition:
            pending = self._pending.get(request_id)
            if pending is None:
                previous = self._resolved_ids.get(request_id)
                return (
                    DecideOutcome.EXPIRED
                    if previous == "expired"
                    else (DecideOutcome.ALREADY_DECIDED if previous else DecideOutcome.NOT_FOUND)
                )
            if self._now() > pending["expires_at"]:
                entry = self._resolve_locked(
                    request_id,
                    HITLDecision(HITLAction.REJECT, reason=hitl_reasons.EXPIRED),
                    status="expired",
                    resolver="system",
                )
                if self._store is not None:
                    with contextlib.suppress(ApprovalStoreError):
                        self._store.expire(request_id)
                outcome = DecideOutcome.EXPIRED
            else:
                action = _decision_action(decision)
                if action != HITLAction.REJECT.value and self._suspended:
                    return DecideOutcome.SUSPENDED  # re-checked under the lock
                if action in _HELD_ACTIONS and self.frozen(pending["break_level"], current):
                    _log_hold(request_id, pending["args_hash"], pending["break_level"], current)
                    return DecideOutcome.BREAKER
                if action != HITLAction.REJECT.value and args_hash != pending["args_hash"]:
                    return DecideOutcome.BINDING_MISMATCH
                if action == HITLAction.EDIT.value and _is_masked(pending["tool_input"]):
                    # The queue shows these arguments masked (#185): an edit would be
                    # made blind and could send the mask back as a value.
                    return DecideOutcome.UNSUPPORTED
                if self._store is not None:
                    # Durable first: an unrecorded decision is never delivered (#240).
                    try:
                        stored = self._store.decide(
                            request_id,
                            action,
                            args_hash=args_hash,
                            approver=_principal(resolver),
                            client_id=client_id,
                            reason=decision.reason,
                        )
                    except (ApprovalStoreError, ValueError):
                        return DecideOutcome.STORE_UNAVAILABLE
                    if not stored:
                        return _STORE_OUTCOME[stored]
                bound = HITLDecision(
                    decision.action,
                    edited_input=deepcopy(decision.edited_input),
                    response=decision.response,
                    request_id=request_id,
                    args_hash=pending["args_hash"],
                    expires_at=pending["expires_at"],
                    reason=decision.reason,
                )
                entry = self._resolve_locked(
                    request_id,
                    bound,
                    status=_RESOLUTION.get(action, action),
                    resolver=resolver,
                    client_id=client_id,
                )
                outcome = DecideOutcome.ACCEPTED
        self._notify_resolved(entry)
        return outcome

    def _decide_restored(
        self, request_id, decision, *, args_hash, resolver, client_id, current=None
    ):
        """Decide a request restored from the store (its worker is gone)."""
        store = self._store
        try:
            for expired_id in store.expire_due():
                self._record_resolved(expired_id)
            record = store.get(request_id)
        except ApprovalStoreError:
            return DecideOutcome.STORE_UNAVAILABLE
        if record is None:
            return DecideOutcome.NOT_FOUND
        if record.status != PENDING:
            return (
                DecideOutcome.EXPIRED
                if record.status == EXPIRED
                else (DecideOutcome.ALREADY_DECIDED)
            )
        action = _decision_action(decision)
        edited = None
        if action == HITLAction.RESPOND.value:
            return DecideOutcome.UNSUPPORTED  # no live turn to answer
        if action == HITLAction.EDIT.value:
            edited = deepcopy(decision.edited_input)
            if record.masked or not isinstance(edited, dict):
                return DecideOutcome.UNSUPPORTED  # the human cannot see what they edit
            try:
                canonical_json(edited)
                if store.would_mask(edited):
                    # A secret in a restored edit would have to be stored to resume.
                    return DecideOutcome.UNSUPPORTED
            except (TypeError, ValueError):
                return DecideOutcome.UNSUPPORTED
            except ApprovalStoreError:
                return DecideOutcome.STORE_UNAVAILABLE
        if action != HITLAction.REJECT.value and self.approvals_suspended():
            return DecideOutcome.SUSPENDED
        if action in _HELD_ACTIONS and self.frozen(record.break_level, current):
            _log_hold(request_id, record.args_hash, record.break_level, current)
            return DecideOutcome.BREAKER
        try:
            stored = store.decide(
                request_id,
                action,
                args_hash=args_hash,
                approver=_principal(resolver),
                client_id=client_id,
                reason=decision.reason,
            )
        except (ApprovalStoreError, ValueError):
            return DecideOutcome.STORE_UNAVAILABLE
        if stored is StoreOutcome.EXPIRED:
            self._record_resolved(request_id)
        if not stored:
            return _STORE_OUTCOME[stored]
        successor = None
        if edited is not None:
            # EDIT is a new request that must itself be approved (#146).
            successor = uuid4().hex
            try:
                store.submit(
                    request_id=successor,
                    agent_id=record.agent_id,
                    tool_name=record.tool_name,
                    tool_input=edited,
                    expires_at=self._now() + self._ttl,
                    tool_use_id=record.tool_use_id,
                    correlation_id=record.correlation_id,
                    origin=record.origin,
                    checkpoint_id=record.checkpoint_id,
                    supersedes=record.request_id,
                    reason=record.reason,
                    break_level=record.break_level,
                    store_payload=True,
                )
            except (ApprovalStoreError, TypeError, ValueError):
                successor = None  # nothing to approve: nothing resumes (fail-closed)
        self._record_resolved(request_id)
        if successor is not None:
            with contextlib.suppress(ApprovalStoreError):
                fresh = store.get(successor)
                if fresh is not None:
                    self._notify("approval.requested", _approval_data(_restored_pending(fresh)))
        if action == HITLAction.APPROVE.value and self._on_resume is not None:
            try:
                self._on_resume(request_id)
            except Exception:
                logger.exception("HITL resume could not be scheduled")
        if action == HITLAction.REJECT.value and self._on_reject is not None:
            try:
                self._on_reject(request_id)
            except Exception:
                logger.exception("HITL rejection could not be passed on")
        return DecideOutcome.ACCEPTED

    def _record_resolved(self, request_id: str) -> None:
        """Mirror a store-side resolution into the in-memory history and events."""
        try:
            record = self._store.get(request_id)
        except ApprovalStoreError:
            return
        if record is None or record.status == PENDING:
            return
        entry = _record_entry(record)
        with self._condition:
            if self._resolved_ids.get(request_id) == record.status:
                return
            self._history.append(entry)
            self._resolved_ids[request_id] = record.status
            for previous in self._history:
                if previous["id"] == record.supersedes:
                    previous["superseded_by"] = request_id
        self._notify_resolved(deepcopy(entry))

    def withdraw(self, request_id: str) -> None:
        """ADR 0011 W5: a grant iteration stopped while its card waits — the live worker
        gets a rejection at once (memory only here: the waiting worker records it).
        Also before the card is registered (the id is remembered, bounded)."""
        with self._condition:
            if request_id not in self._withdrawn:
                self._withdrawn.append(request_id)
            self._condition.notify_all()

    def close(self, timeout: float = 2.0) -> bool:
        """Reject everything still pending (shutdown/disconnect is never approval).

        New requests are refused without being stored. A save that had already
        passed that check is waited for (at most ``timeout`` seconds), so the store
        does not change after ``close`` returned ``True``; such a request is pending
        like any other waiting one and restored after a restart. ``False``: a save
        was still running at the deadline. Idempotent.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._saving:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


MAX_EDIT_CHAIN = MAX_CHAIN_LINKS
# ADR 0024 §3: what the breaker freeze holds back (reject and respond are never held).
_HELD_ACTIONS = frozenset({HITLAction.APPROVE.value, HITLAction.EDIT.value})


def _log_hold(request_id, digest, requested, current) -> None:
    """Audit line of a held decision: ids, levels and a hash prefix only (no arguments)."""
    logger.warning(
        "approval %s held by the breaker (requested at %s, now %s, args %s)",
        str(request_id)[:32],
        requested if isinstance(requested, str) else "unknown",
        current if isinstance(current, str) else "unknown",
        str(digest)[:12],
    )


def _edit_chain_end(store, record):
    """Follow EDIT successors to the newest request (cycle- and depth-bounded).

    A -> B -> C (each edited, C approved) answers A's replay with C's input; None
    when the chain is broken, cyclic or longer than ``MAX_EDIT_CHAIN``.
    """
    seen = {record.request_id}
    node = record
    for _ in range(MAX_EDIT_CHAIN):
        node = store.get(node.superseded_by or "")
        if node is None or node.request_id in seen:
            return None
        if node.status != EDITED:
            return node
        seen.add(node.request_id)
    return None


def _principal(resolver) -> str:
    """Store principal for a dashboard decision (ADR 0006 §4): always ``local:``."""
    name = resolver if isinstance(resolver, str) and resolver.strip() else "operator"
    return f"local:{name.strip()}"


def _display_input(record):
    try:
        value = json.loads(record.display)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _record_turn(record):
    origin = record.origin or {}
    return origin.get("id") if origin.get("kind") == "turn" else None


def _masked_text(value):
    return mask_secrets(value) if isinstance(value, str) else value


def _masked_turn(turn):
    """A history turn with its texts masked (#291); usage and ids are left as they are."""
    if not isinstance(turn, Mapping):
        return turn
    return {**turn, **{k: _masked_text(turn[k]) for k in ("message", "response") if k in turn}}


def _is_masked(tool_input) -> bool:
    return mask_secrets(tool_input) != tool_input


_ALLOW_ERROR = {
    "stale_view": "Something changed after this card was shown; nothing was approved.",
    "class_changed": "Something changed after this card was shown; nothing was approved.",
    "op_token_used": "This button was already used; nothing more was approved.",
    "list_full": "Up to 20 allowances; nothing was approved.",
    "audit_failed": "The audit log cannot be written; nothing was approved.",
    "card_not_pending": "The request is no longer pending.",
    "not_enabled": "This option is not available.",
}


def _with_allow_view(item: dict) -> dict:
    """A queue entry for the page with its ``allow_again`` view (ADR 0025 §3, §4a): the
    offer is computed from the **unmasked** stored request (a masked one is never
    offered: ``secret_in_args``); the page gets only the view."""
    shown = display_pending(item)
    try:
        view = allow_runtime.card_view(item)
    except Exception:  # noqa: BLE001 - a view that cannot be computed offers nothing
        view = {"offered": False, "reason": "read_failed"}
    return {**shown, "allow_again": view}


def display_pending(item: dict) -> dict:
    """A queue entry as the approval UI may see it (#185).

    Secrets in a live request's arguments are masked with ``mask_secrets`` (like the
    store's display copy of a restored request). Such a request is not editable: the
    server refuses the edit too. The binding (``args_hash``) still names the real
    arguments, so approve/reject work unchanged.
    """
    if item.get("restored"):
        return item  # already the store's masked display
    item = {k: v for k, v in item.items() if k != "allow_origin"}  # server only (ADR 0025)
    raw = item.get("tool_input")
    shown = mask_secrets(raw)
    # Free text beside the arguments (the request's reason) is masked as well.
    item = {**item, "reason": _masked_text(item.get("reason"))}
    if shown == raw:
        return item
    return {
        **item,
        "tool_input": shown,
        "normalized_input": canonical_json(shown),
        "cwd": _text_field(shown, "cwd"),
        "editable": False,
        "masked": True,
    }


def _restored_pending(record) -> dict:
    """Queue entry for a request whose worker is gone (masked display only)."""
    shown = _display_input(record)
    return {
        "id": record.request_id,
        "agent_id": record.agent_id or None,
        "tool_name": record.tool_name,
        "tool_input": shown,
        "normalized_input": record.display,
        "args_hash": record.args_hash,
        "break_level": record.break_level,
        "reason": record.reason,
        "reason_code": reason_code(record.reason),
        "reason_params": reason_params(record.reason, record.break_level),
        "tool_use_id": record.tool_use_id,
        "supersedes": record.supersedes,
        "cwd": _text_field(shown, "cwd"),
        "turn_id": _record_turn(record),
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "status": "pending",
        "restored": True,
        "resumable": bool(record.checkpoint_id),
        "editable": not record.masked,
        "origin": dict(record.origin or {}),
    }


def _record_entry(record) -> dict:
    """History entry (same shape as ``_resolve_locked``) from a store record."""
    shown = _display_input(record)
    resolver = (record.approver or "system").removeprefix("local:")
    status = _RESOLUTION.get(record.action, record.status) if record.action else record.status
    if record.status in ("expired", "cancelled"):
        status = record.status
    action = record.action if record.status not in ("expired", "cancelled") else "reject"
    return {
        "id": record.request_id,
        "agent_id": record.agent_id or None,
        "tool_name": record.tool_name,
        "normalized_input": record.display[:HISTORY_INPUT_LIMIT],
        "args_hash": record.args_hash,
        "break_level": record.break_level,
        "reason": record.reason,
        "reason_code": reason_code(record.reason),
        "reason_params": reason_params(record.reason, record.break_level),
        "tool_use_id": record.tool_use_id,
        "supersedes": record.supersedes,
        "cwd": _text_field(shown, "cwd"),
        "turn_id": _record_turn(record),
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "status": status,
        "action": action or "reject",
        "resolution_reason": record.resolution_reason,
        "resolution_reason_code": resolution_code(record.resolution_reason, resolver),
        "resolver": resolver,
        "client_id": record.client_id,
        "resolved_at": record.decided_at,
        "superseded_by": record.superseded_by,
        "restored": True,
    }


def _text_field(mapping, key):
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, str) else None


# W5b: ``/api/activity`` without stage C has no ``grant`` key (the answer is as before).
_NO_GRANTS = object()
_HEX = re.compile(r"[0-9a-f]+")
_CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,47}")


def _grant_card_display(summary) -> dict:
    """ADR 0011 W5: the durable display of a grant card, built from fixed fields only —
    whatever else the caller passes (any model-chosen string) is dropped."""
    summary = summary if isinstance(summary, dict) else {}
    out: dict = {"grant_card": True}

    def hexed(key: str, length: int) -> None:
        value = summary.get(key)
        if isinstance(value, str) and len(value) <= length and _HEX.fullmatch(value or "0"):
            out[key] = value

    hexed("grant_id", 32)
    hexed("path_sha256", 12)
    hexed("sha256", 16)
    tool = summary.get("tool")
    if isinstance(tool, str) and _CODE_WORD.fullmatch(tool):
        out["tool"] = tool
    if type(summary.get("inside")) is bool:
        out["inside"] = summary["inside"]
    if type(summary.get("bytes")) is int and summary["bytes"] >= 0:
        out["bytes"] = summary["bytes"]
    return out


def _approval_data(pending: dict) -> dict:
    raw_reason = pending.get("reason")
    return {
        "request_id": pending["id"],
        "tool_use_id": pending.get("tool_use_id"),
        "tool_name": pending.get("tool_name"),
        "break_level": pending.get("break_level"),
        "reason": _masked_text(raw_reason),
        "reason_code": pending["reason_code"]
        if "reason_code" in pending
        else reason_code(raw_reason),
        "agent_id": pending.get("agent_id"),
        "args_hash": pending.get("args_hash"),
        "expires_at": pending.get("expires_at"),
        "supersedes": pending.get("supersedes"),
    }


def _decision_action(decision):
    action = getattr(decision, "action", None)
    if not isinstance(action, HITLAction):
        values = getattr(decision, "__dict__", {}).values()
        action = next((v for v in values if isinstance(v, HITLAction)), action)
    return getattr(action, "value", action)


def _transition_event(previous: str, level: str, summary: Mapping) -> dict:
    """``{"from", "to"}`` plus, when known, why (#455): the engine's own record of the
    change into ``level``, else the reasons of the level now. Codes and numbers only."""
    data: dict = {"from": previous, "to": level}
    record = summary.get("breaker_transition")
    if isinstance(record, Mapping) and record.get("to") == level and record.get("reasons"):
        data["reasons"], data["inputs"] = list(record["reasons"]), dict(record["inputs"])
    elif summary.get("breaker_reasons"):
        data["reasons"] = list(summary["breaker_reasons"])
        data["inputs"] = dict(summary.get("breaker_inputs") or {})
    return data


def _breaker_level(breaker):
    level = field(breaker, "level", breaker) if isinstance(breaker, Mapping) else breaker
    level = field(level, "value", level)
    return None if level is None else str(level)


DIMENSIONS = (
    "trust",
    "curiosity",
    "vigilance",
    "attachment",
    "stability",
    "satisfaction",
)


def field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def complete_chunk(result):
    """Normalize a final result while preserving its token-usage metadata."""
    message = field(result, "message")
    content = field(message, "content", []) or []
    text = "".join(value for block in content if isinstance(value := field(block, "text"), str))
    if not text:
        text = str(result)
    return {"data": text, "result": result}


def iterate(source):
    """Yield streaming chunks, or one chunk for a complete response."""
    if source is None:
        return

    if isinstance(source, str):
        yield source
        return

    # A mapping is one event/result, not a stream of its keys.
    if isinstance(source, Mapping):
        yield source
        return

    # Recognize final AgentResult objects before attempting iteration.
    if hasattr(source, "message") and hasattr(source, "metrics"):
        yield complete_chunk(source)
        return

    if hasattr(source, "__aiter__"):
        loop = asyncio.new_event_loop()
        iterator = None
        try:
            iterator = source.__aiter__()
            while True:
                try:
                    yield loop.run_until_complete(iterator.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            try:
                if iterator is not None and hasattr(iterator, "aclose"):
                    loop.run_until_complete(iterator.aclose())
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
        return

    try:
        iterator = iter(source)
    except TypeError:
        # NXPAgent returns a complete result, not necessarily an iterable.
        yield complete_chunk(source)
        return

    try:
        yield from iterator
    finally:
        close = getattr(iterator, "close", None)
        if not callable(close):
            close = getattr(source, "close", None)
        if callable(close):
            close()


def _rejects_images(agent) -> bool:
    """Reject images only for models *known* to be text-only (#206).

    Unknown models keep today's behaviour; the settings UI can warn about them (#208).
    """
    from stiracore.providers.capabilities import ModelCapabilities

    capabilities = getattr(agent, "capabilities", None)
    return (
        isinstance(capabilities, ModelCapabilities)
        and capabilities.known
        and not capabilities.vision
    )


# A turn that ends without a token count carries a reason code (turn_events.USAGE_REASONS,
# #363) instead of a bare null, so a gap in cost accounting is explained, not silent.
MAX_TOKENS_CODE = "max_tokens"
MAX_TOKENS_MESSAGE = (
    "出力の上限に達して途中で切れました。上限を上げるか、分けて依頼してください"
    " / The output reached its limit and was cut off. Raise the limit or ask in smaller parts"
)
# #416: the cut call had started neither a reply nor a tool call (thinking used the cap).
CUT_BEFORE_REPLY = "before_reply"
MAX_TOKENS_BEFORE_REPLY_MESSAGE = (
    "返答を書き始める前に出力の上限に達しました（思考も出力の上限に含まれるモデルでは、"
    "思考だけで使い切ることがあります）。上限を上げてください"
    " / The output limit was reached before the reply began (with models whose thinking"
    " counts toward the limit, thinking alone can use it up). Raise the limit"
)


def _cut_off_details(agent) -> dict:
    """``cut`` / ``limit`` of a ``max_tokens`` turn for the page (#416); {} when unknown."""
    details: dict = {}
    cut = field(agent, "last_cut_off")
    if isinstance(cut, Mapping) and cut.get("before_reply") is True:
        details["cut"] = CUT_BEFORE_REPLY
    limit = field(agent, "max_output_tokens")
    if type(limit) is int and limit > 0:
        details["limit"] = limit
    return details


def _merge_reported_usage(usage: dict, reported: object) -> None:
    """Copy the token counts a provider/SDK reported into ``usage`` (ints only)."""
    for key, camel in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
        value = field(reported, key, field(reported, camel))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value
    # Cache buckets only when the provider reports them (#206); their relation to
    # input_tokens is provider-specific, see stiracore.providers.usage.
    for key, camel in (
        ("cache_read_tokens", "cacheReadInputTokens"),
        ("cache_write_tokens", "cacheWriteInputTokens"),
    ):
        value = field(reported, camel)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value


def _usage_reason(usage: Mapping, stop: str) -> str | None:
    """Why a turn has no token count (None when it has one)."""
    if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
        return None
    if stop == "cancelled":
        return USAGE_CANCELLED
    return USAGE_INTERRUPTED if stop == "interrupted" else USAGE_NOT_REPORTED


# The turn runs on its own worker thread (#452); the POST /api/chat stream only reads it.
# A reader that stops reading (a stalled connection) is dropped once this many frames wait
# for it: the turn goes on, its events stay in turns.db and its reply in the chat history.
STREAM_FRAME_LIMIT = 10_000
# An idle stream sends an SSE comment this often: a closed page is noticed (and only
# detached) and an idle connection is not dropped by a proxy or the browser.
KEEPALIVE_SEC = 15.0
# How often an explicit stop repeats agent.cancel() until the turn has ended (a stop that
# lands before the SDK's agent exists, or between two of its invocations, is not lost).
CANCEL_REPEAT_SEC = 0.2
BUSY_MESSAGE = "処理中です / Agent is busy"
# ADR 0014 §4 (operator decision 2026-10-19): the message of an automatic continue, in the
# language of the person's last message (the model answers in the conversation's language).
AUTO_CONTINUE_MESSAGE = {
    "ja": "続けてください。（返答がなかったため、自動で続けます）",
    "en": "Please continue. (No reply came, so this continues automatically.)",
}
# ADR 0014 §5: Redirect's one line (≤ 300 characters after the page's prefix). The page's
# ``maxLength`` is a convenience; the server holds the limit (PR #489 review).
REDIRECT_MAX_CHARS = 300
REDIRECT_PREFIXES = ("方向修正:", "Change of direction:")  # catalog: progress.redirect_prefix
STREAM_DROPPED_MESSAGE = (
    "表示が追いつかないため、このページでの表示を止めました。ターンは続いています"
    " / This page fell behind and stopped showing the turn; the turn goes on"
)
INTERRUPTED_MESSAGE = (
    "サーバーが停止したため、ターンを中断しました / The server stopped; the turn was interrupted"
)
CANCEL_REFUSALS = {
    "no_turn": "実行中のターンはありません / No turn is running",
    "turn_mismatch": "そのターンは実行中ではありません / That turn is not the running one",
}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class TurnStream:
    """The SSE frames of one running chat turn (#452).

    The turn's worker writes; the POST /api/chat response that started the turn reads. A
    closed connection only detaches the reader — the turn goes on, nothing is queued for
    it any more — and a reader that stops reading is detached once ``limit`` frames wait
    for it (memory stays bounded). The turn's events and reply are kept elsewhere
    (turns.db, the chat history); this is only the live view of one page.
    """

    def __init__(self, limit: int = STREAM_FRAME_LIMIT) -> None:
        self._cond = threading.Condition()
        self._frames: deque[str] = deque()
        self._limit = max(1, int(limit))
        self._attached = True
        self._closed = False
        self.overflowed = False

    @property
    def attached(self) -> bool:
        with self._cond:
            return self._attached

    def put(self, frame: str) -> bool:
        with self._cond:
            if not self._attached or self._closed:
                return False
            if len(self._frames) >= self._limit:
                self._attached, self.overflowed = False, True
                self._cond.notify_all()
                return False
            self._frames.append(frame)
            self._cond.notify_all()
            return True

    def detach(self) -> None:
        with self._cond:
            self._attached = False
            self._frames.clear()
            self._cond.notify_all()

    def close(self) -> None:
        """The turn has ended (and released the turn lock): the reader drains and stops."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def read(self, keepalive: float = KEEPALIVE_SEC):
        """The response body: frames as the worker writes them, a comment when idle."""
        try:
            yield ": connected\n\n"
            while True:
                with self._cond:
                    if not self._frames and not self._closed and self._attached:
                        self._cond.wait(keepalive)
                    frame = self._frames.popleft() if self._frames else None
                    ended = self._closed or not self._attached
                    overflowed = self.overflowed
                if frame is not None:
                    yield frame
                elif ended:
                    if overflowed:
                        yield _sse(
                            "error", {"error": STREAM_DROPPED_MESSAGE, "code": "stream_dropped"}
                        )
                    return
                else:
                    yield ": keep-alive\n\n"
        finally:
            # The page went away (or fell behind): only this view ends, never the turn.
            self.detach()


def _cache_hit_ratio(usage: Mapping, provider: object) -> float | None:
    """Cache reads / whole prompt, with the provider's input semantics (#206/#254)."""
    normalized = normalize_strands_usage(
        {
            "inputTokens": usage.get("input_tokens"),
            "cacheReadInputTokens": usage.get("cache_read_tokens"),
            "cacheWriteInputTokens": usage.get("cache_write_tokens"),
        },
        provider if isinstance(provider, str) else "custom",
    )
    prompt = normalized.prompt_tokens
    if not prompt or normalized.cache_read_tokens is None:
        return None
    return round(normalized.cache_read_tokens / prompt, 4)


def _last_call_prompt_tokens(metrics, provider: object) -> int | None:
    """The prompt of the turn's *last* model call (uncached + cache read + cache write).

    ``accumulated_usage`` sums every model call of the turn, so a turn with tool calls
    reports more than the context holds. Strands keeps each call's usage per event-loop
    cycle (``agent_invocations[-1].cycles[-1].usage``, the value its own
    ``latest_context_size`` reads); it is normalized with the provider's input semantics
    like the other counts (#206). None when the SDK or the provider did not report it.
    """
    try:
        invocations = field(metrics, "agent_invocations") or []
        cycles = (field(invocations[-1], "cycles") or []) if invocations else []
        usage = field(cycles[-1], "usage") if cycles else None
    except Exception:  # noqa: BLE001 - telemetry must never break chat
        return None
    if not isinstance(usage, Mapping):
        return None
    raw = usage.get("inputTokens")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return None
    normalized = normalize_strands_usage(usage, provider if isinstance(provider, str) else "custom")
    return normalized.prompt_tokens


# POST /api/chat/session/new (#350): refusal reason codes (409 unless noted).
NEW_SESSION_REFUSALS = {
    "turn_running": "ターンの実行中です / A turn is running",
    "approval_pending": "承認待ちの操作があります / An approval is pending",
    "resume_pending": "承認された操作の再開を待っています / An approved operation is waiting to resume",
    "approvals_unavailable": "承認の状態を確認できません / The approval state cannot be read",
    "audit_unavailable": "監査ログに書けません / The audit log cannot be written",
    "decision_in_progress": "承認の判断を処理中です / An approval decision is being processed",
}
# Reset keys this process completed (digest -> session id), newest last. Kept for the process
# lifetime (not the 300 s of the generic idempotency cache): a repeat after any delay is a
# duplicate and names the session that key started. Bounded: the oldest are forgotten.
NEW_SESSION_KEYS_KEPT = 256
NEW_SESSION_LOCK_TIMEOUT_SEC = 5.0
_KEY_CHARS = re.compile(r"[\x21-\x7e]{1,128}")


class LockBusy(TimeoutError):
    """A bounded acquisition of the shared reset/decision lock timed out (#350)."""


@contextlib.contextmanager
def timed_lock(lock, timeout: float):
    """Hold ``lock`` (re-entrant if it is an RLock) or raise :class:`LockBusy` after ``timeout``."""
    if not lock.acquire(timeout=timeout):
        raise LockBusy(f"lock not free within {timeout:.1f} s")
    try:
        yield
    finally:
        lock.release()


def request_digest(key: str) -> str:
    """The digest that names a reset request in the history (never the key itself)."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _new_session_body_error() -> str | None:
    """An empty body or ``{}``; anything else is refused before anything runs."""
    raw = request.get_data(cache=True)
    if not raw.strip():
        return None
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "body must be empty or a JSON object"
    if not isinstance(body, dict):
        return "body must be empty or a JSON object"
    if body:
        return f"unexpected field: {min(body)[:40]}"
    return None


def create_chat_blueprint(
    agent,
    memory_store=None,
    status_provider=None,
    runner=None,
    token_rates=None,
    turn_lock=None,
    hitl_broker=None,
    turn_summary_provider=None,
    turn_recorder=None,
    new_session=None,
    progress=None,
    yolo_stop=None,
    token_prices=None,
    artifact_counts=None,
    session_reset=None,
):
    """``turn_lock`` (ADR 0011 W6b): the server's :class:`~stiracore.core.turn_lock.TurnLock`
    (a plain lock or None: one without a pre-emption signal). Every acquirer here — the
    chat route, the automatic continue, ``/new`` — takes it only through it, so a goal
    iteration is pre-empted before the lock is tried (it yields at its next effect
    boundary).

    ``session_reset()`` (ADR 0011 W4): called when ``/new`` resets the conversation
    (with the allowances' end, before the reset is judged): the grants are revoked
    (``revoked(session_reset)``) in memory; never raises. None: nothing.

    ``artifact_counts(turn_ids) -> {turn id: n}`` (#535): how many workspace artifacts
    each turn wrote (counts only), for the board's and the report's links to
    ``/artifacts?turn=``; None: no links.

    ``progress`` (ADR 0014 phase 1, #451): a ``progress.ProgressTracker`` (only with
    ``STIRA_YOLO_ALLOW`` and turn events), or None — no report, no route answers one.
    ``yolo_stop()``: the report's Stop ends the YOLO window (``yolo.runtime.stop``).

    ``new_session`` (#350): the host's conversation reset, or None (no /new route).

    The host provides ``exclusive(timeout)`` (a context manager; the same lock the approval
    broker holds around a decision and the host holds while scheduling a resume, so a
    decision or a resume reservation never falls between the checks and the reset; raises
    ``TimeoutError``), ``blocker() -> str | None`` (a refusal code from
    ``NEW_SESSION_REFUSALS``), ``audit_ready() -> bool``, ``audit(stage, record)`` (raises
    when the record cannot be written) and ``reset() -> int`` (messages dropped). All but
    ``exclusive`` run inside it, with the turn lock held.
    """
    bp = Blueprint("chat_api", __name__)

    @bp.before_request
    def chat_body_limit():
        if request.endpoint == "chat_api.chat":
            # Override small observer/server defaults only for this endpoint.
            request.max_content_length = MAX_CHAT_BYTES

    @bp.errorhandler(RequestEntityTooLarge)
    def too_large(error):
        return jsonify(error=error.description), 413

    started, turns = time.monotonic(), deque(maxlen=50)
    started_wall = time.time()  # #446: the board's totals count from here
    generation_lock = TurnLock.of(turn_lock)
    state_lock = threading.Lock()
    totals = {"total_input_tokens": 0, "total_output_tokens": 0}
    last_breaker = {"level": None}
    # The conversation this process serves (#350): a restart or /new starts a new one.
    session = {"id": uuid4().hex, "started_at": time.time(), "reason": "start", "cleared": 0}
    # The chat turn running now (#452): its worker, stream and stop signal; None when idle.
    running = {"run": None}
    # ADR 0014 (#451): the reports of this conversation (newest last; ids and turn ids only —
    # the record is the turn events), and the app the automatic continue runs its turn in.
    if turn_recorder is None:
        progress = None
    reports = deque(maxlen=20)
    holder = {"app": None}

    @bp.record_once
    def _remember_app(state):
        holder["app"] = state.app

    if runner is None:
        runner = next(
            (
                getattr(agent, name)
                for name in ("stream_async", "stream", "run")
                if callable(getattr(agent, name, None))
            ),
            agent,
        )
    if not callable(runner):
        raise TypeError("Supply an NXPAgent runner(message) adapter")
    if turn_summary_provider is None:
        turn_summary_provider = lambda: field(agent, "last_turn_summary")
    if not callable(turn_summary_provider):
        raise TypeError("turn_summary_provider must be callable")

    def turn_summary():
        candidate = turn_summary_provider()
        if not isinstance(candidate, Mapping):
            return None
        deltas = field(candidate, "deltas", {})
        if not isinstance(deltas, Mapping):
            deltas = {}
        return {
            "category": field(candidate, "category"),
            "deltas": deepcopy(dict(deltas)),
            "emotion_transcription": field(candidate, "emotion_transcription"),
            "breaker": field(candidate, "breaker"),
        }

    ui_before = {"state": None}

    def neural_snapshot():
        try:
            state = status().get("neural_state")
        except Exception:  # noqa: BLE001 - UI telemetry must never break chat
            return None
        if not isinstance(state, Mapping):
            return None
        return {dim: field(state, dim) for dim in DIMENSIONS}

    def ui_summary():
        try:
            candidate = turn_summary_provider()
        except Exception:  # noqa: BLE001 - UI telemetry must never break chat
            return None
        if not isinstance(candidate, Mapping):
            return None
        data = {
            key: field(candidate, key)
            for key in (
                "category",
                "intensity",
                "confidence",
                "classifier",
                "emotion_transcription",
                "deltas",
                "breaker",
            )
        }
        # #455: why the breaker is at this level (allow-listed codes and numbers only).
        reasons, inputs = breaker_reasons.clean(
            field(candidate, "breaker_reasons"), field(candidate, "breaker_inputs")
        )
        if reasons:
            data["breaker_reasons"], data["breaker_inputs"] = reasons, inputs
        transition = breaker_reasons.clean_transition(field(candidate, "breaker_transition"))
        if transition is not None:
            data["breaker_transition"] = transition
        # ADR 0023 phase 0 (#460-0): the stage this session would be in (observe only).
        held = containment.clean_summary(field(candidate, "containment"))
        if held is not None:
            data["containment"] = held
        # ADR 0022 phase 0 (#458): the context label and would-hold counts (observe only).
        from stiracore.observability.taint_api import clean_summary as clean_taint

        taint = clean_taint(field(candidate, "taint"))
        if taint is not None:
            data["taint"] = taint
        for key in ("before", "after"):
            value = field(candidate, key)
            data[key] = (
                {dim: field(value, dim) for dim in DIMENSIONS}
                if isinstance(value, Mapping)
                else None
            )
        # The agent summary carries deltas only: fall back to the status snapshots
        # taken around the turn (before = at turn.started, after = now).
        if data["before"] is None:
            data["before"] = ui_before.get("state")
        if data["after"] is None:
            data["after"] = neural_snapshot()
        return data

    def _model_in_use():
        model_id = field(agent, "model_id")
        return model_id if isinstance(model_id, str) and model_id else None

    def _priced_session_cost():
        """#451: the chat page's estimated cost with ``token_prices`` — the same sum as the
        board's totals, shown only when every turn with tokens is priced (else None)."""
        if turn_recorder is None:
            return None
        try:
            priced = activity.totals_from(
                turn_recorder.store.completed_since(started_wall),
                token_prices,
                model=_model_in_use(),
            )
        except Exception:  # noqa: BLE001 - an unknown cost is shown as unknown
            return None
        return None if priced.get("cost_partial") else priced.get("cost")

    def status():
        neural, breaker, soul = (
            field(agent, key, {}) for key in ("neural_state", "breaker", "soul")
        )
        memory = memory_store if memory_store is not None else field(agent, "memory_store", {})
        with state_lock:
            session = dict(totals)
        session["estimated_cost"] = (
            None
            if token_rates is None
            else (
                session["total_input_tokens"] * token_rates[0]
                + session["total_output_tokens"] * token_rates[1]
            )
            / 1_000_000
        )
        if token_prices:
            session["estimated_cost"] = _priced_session_cost()

        level = field(breaker, "level", "unknown")
        changed = field(breaker, "last_changed")
        model = field(agent, "model")
        model_id = model if isinstance(model, str) else field(model, "model_id")
        if model_id is None:
            model_id = field(field(model, "config", {}), "model_id")

        result = {
            "agent_id": field(agent, "agent_id"),
            "model": model_id,
            "neural_state": {key: field(neural, key) for key in DIMENSIONS},
            "breaker": {
                "level": field(level, "value", level),
                "last_changed": (changed.isoformat() if hasattr(changed, "isoformat") else changed),
            },
            "memory": {
                f"{key}_count": field(memory, f"{key}_count")
                for key in ("episodic", "semantic", "procedural")
            },
            "soul": {
                "loaded": field(soul, "loaded"),
                "path": (str(field(soul, "path")) if field(soul, "path") is not None else None),
            },
            "bc_score": field(agent, "bc_score"),
            "session": session,
            "uptime_seconds": int(time.monotonic() - started),
        }
        if status_provider:
            result.update(status_provider())
        return result

    @bp.get("/chat")
    def page():
        return render_template("chat.html")

    @bp.get("/api/gateway/status")
    def gateway_status():
        return jsonify(status()), 200, {"Cache-Control": "no-store"}

    @bp.get("/api/chat/history")
    def history():
        try:
            with state_lock:
                # One snapshot: a reset between the two copies would pair old turns
                # with the new session.
                recent = list(turns)
                current = dict(session)
            if memory_store is not None:
                recent = list(memory_store.get_recent_turns(limit=50))
            # Masked on the way out as well: turns an external store held from before.
            return (
                jsonify(turns=[_masked_turn(t) for t in recent[-50:]], session=current),
                200,
                {"Cache-Control": "no-store"},
            )
        except Exception:
            current_app.logger.exception("Chat history failed")
            return jsonify(error="履歴を取得できません / History unavailable"), 503

    if new_session is not None:
        if memory_store is not None:
            raise TypeError("new_session cannot reset an external memory_store history")
        # Idempotency of /new is handled here, not by the generic cache: a repeat names the
        # session its key started (also after 300 s), and two requests with one key at the
        # same time run once (the second is "in progress").
        completed_keys: OrderedDict = OrderedDict()
        inflight_keys: set = set()

        def refuse(code, status=409):
            return jsonify(error=NEW_SESSION_REFUSALS[code], code=code), status

        def duplicate(digest):
            with state_lock:
                done = completed_keys.get(digest)
                current_id = session["id"]
            if done is None:  # the same key is being processed right now
                return (
                    jsonify(error=DUPLICATE_ERROR, code="duplicate_request", in_progress=True),
                    409,
                )
            return (
                jsonify(
                    error=DUPLICATE_ERROR,
                    code="duplicate_request",
                    session={"id": done, "current": done == current_id},
                ),
                409,
            )

        @bp.post("/api/chat/session/new")
        def new_chat_session():
            """Start a new conversation (#350): only the history sent to the model goes.

            Fail-closed: refused (nothing changes) while a turn runs or streams, while an
            approval is pending, being decided or an approved one waits to resume, or when
            the audit cannot be written. The "reset" audit row is persisted *before* the
            reset; if it cannot be written nothing is reset (500 ``audit_failed``).
            Same-origin + CSRF (install_security) and a required Idempotency-Key: a repeat
            of a reset that succeeded is ``duplicate_request`` with the session that key
            started, and clears nothing a second time. No conversation text is audited.
            """
            if progress is not None:
                # ADR 0014 §4 ⑤: /new cancels a scheduled continue whatever happens next.
                progress.cancel_auto("new_session")
            problem = _new_session_body_error()
            if problem is not None:
                return jsonify(error=problem, code="invalid_body"), 400
            key = request.headers.get("Idempotency-Key")
            if not key:
                return (
                    jsonify(error="Idempotency-Key is required", code="idempotency_key_required"),
                    400,
                )
            if not _KEY_CHARS.fullmatch(key):
                return (
                    jsonify(
                        error="Idempotency-Key must be 1-128 printable ASCII characters",
                        code="invalid_idempotency_key",
                    ),
                    400,
                )
            digest = request_digest(key)
            with state_lock:
                if digest in completed_keys or digest in inflight_keys:
                    busy = True
                else:
                    busy = False
                    inflight_keys.add(digest)
            if busy:
                return duplicate(digest)
            try:
                return run_reset(digest)
            finally:
                with state_lock:
                    inflight_keys.discard(digest)

        def run_reset(digest):
            """Lock order: turn lock -> (audit row, no shared lock) -> shared lock -> broker /
            state_lock. Nothing that can stall (the event store) runs under the shared lock."""
            if not generation_lock.acquire(blocking=False):
                return refuse("turn_running")
            try:
                return reset_with_turn_lock(digest)
            finally:
                generation_lock.release()

        def outcome_row(stage, record):
            """The outcome row after the durable "reset" row: one retry, then an ERROR log."""
            for attempt in (1, 2):
                try:
                    new_session.audit(stage, record)
                    return "ok"
                except Exception:
                    current_app.logger.exception(
                        "new session %s row failed (attempt %d of 2)", stage, attempt
                    )
            current_app.logger.error(
                "new session %s row not written; the reset row %s stays the record",
                stage,
                record["session_id"],
            )
            return "failed"

        def reset_with_turn_lock(digest):
            try:
                ready = new_session.audit_ready()
            except Exception:  # noqa: BLE001 - unknown health: refuse, change nothing
                ready = False
            if not ready:
                return refuse("audit_unavailable", 503)
            # No turn can change the history while the turn lock is held.
            with state_lock:
                previous = {"id": session["id"], "turns": len(turns)}
            record = {
                "session_id": uuid4().hex,
                "previous_session_id": previous["id"],
                "request": digest,
                "principal": "local:operator",
                "remote": request.remote_addr,
                "previous_turns": previous["turns"],
            }
            # 1. The durable "reset" row, written *outside* the shared lock: a stalled or
            #    failing event store never holds approval decisions up. Not written: nothing
            #    is reset.
            try:
                new_session.audit("reset", record)
            except Exception:
                current_app.logger.exception("new session audit failed; nothing was reset")
                return (
                    jsonify(
                        error="監査ログに書けなかったため何も変えていません / "
                        "The audit could not be written; nothing was reset",
                        code="audit_failed",
                    ),
                    500,
                )
            # ADR 0025 §5: every session allowance ends before the conversation is reset
            # (fence + 1, in memory; a refused reset only ended them early — a narrowing).
            publish_allow_end("new_session")
            if session_reset is not None:
                # ADR 0011 W4: /new revokes every grant (a narrowing, like the allowances).
                session_reset()
            # 2. Judge and reset under the shared lock (approval-store reads and in-memory
            #    assignments only).
            verdict, status, dropped, current = None, 409, 0, None
            try:
                with new_session.exclusive(NEW_SESSION_LOCK_TIMEOUT_SEC):
                    verdict, status, dropped, current = judge_and_reset(digest, record, previous)
            except TimeoutError:
                verdict = "decision_in_progress"
            except Exception:
                current_app.logger.exception("new session reset failed after its audit row")
                audited = outcome_row("failed", {**record, "error": "reset_failed"})
                return (
                    jsonify(
                        error="会話をリセットできませんでした / The reset failed",
                        code="reset_failed",
                        outcome_audit=audited,
                    ),
                    500,
                )
            if current is None:
                # Refused: nothing changed; the reset row is closed with the reason.
                audited = outcome_row("refused", {**record, "refused": verdict})
                return (
                    jsonify(
                        error=NEW_SESSION_REFUSALS[verdict], code=verdict, outcome_audit=audited
                    ),
                    status,
                )
            # 3. The outcome row (retried once). The reset is already traceable by its
            #    durable reset row, so a failed outcome row never makes it run again.
            audited = outcome_row("started", {**record, "previous_messages": dropped})
            current_app.logger.info(
                "conversation reset (/new): %d messages, %d turns", dropped, previous["turns"]
            )
            return jsonify(status="started", session=current, outcome_audit=audited), 200

        def judge_and_reset(digest, record, previous):
            """Under the shared lock. -> (refusal code or None, status, dropped, session)."""
            try:
                # strict (#601): an unreadable store is "cannot tell", not "no approvals".
                pending = hitl_broker.pending(strict=True) if hitl_broker is not None else []
                blocker = new_session.blocker()
            except Exception:  # cannot tell: change nothing
                current_app.logger.exception("new session pre-check failed")
                return "approvals_unavailable", 503, 0, None
            if pending:
                return "approval_pending", 409, 0, None
            if blocker is not None:
                code = blocker if blocker in NEW_SESSION_REFUSALS else "resume_pending"
                return code, 409, 0, None
            dropped = new_session.reset()  # in-memory assignments only (NXPAgent)
            # Only with STIRA_YOLO_ALLOW (ADR 0012 §6, revision 6): /new also ends every
            # open confirmed operation and fences the YOLO request (a narrowing).
            yolo_runtime.revoke_operations("new_session")
            if progress is not None:
                progress.reset()
            with state_lock:
                turns.clear()
                reports.clear()
                totals.update(total_input_tokens=0, total_output_tokens=0)
                session.clear()
                session.update(
                    id=record["session_id"],
                    started_at=time.time(),
                    reason="new",
                    cleared=previous["turns"],
                    request=digest,
                )
                current = dict(session)
                completed_keys[digest] = current["id"]
                while len(completed_keys) > NEW_SESSION_KEYS_KEPT:
                    completed_keys.popitem(last=False)
            return None, 200, dropped, current

    @bp.get("/api/hitl/pending")
    def hitl_pending():
        requests = (
            [_with_allow_view(item) for item in hitl_broker.pending()]
            if hitl_broker is not None
            else []
        )
        store = getattr(hitl_broker, "store_status", None)
        extra = {"store": store()} if callable(store) and store() is not None else {}
        suspended = getattr(hitl_broker, "approvals_suspended", None)
        if callable(suspended) and suspended():
            extra["approvals_suspended"] = True
        return (
            jsonify(requests=requests, server_time=time.time(), **extra),
            200,
            {"Cache-Control": "no-store"},
        )

    @bp.get("/api/hitl/history")
    def hitl_history():
        if hitl_broker is None:
            return jsonify(history=[], server_time=time.time()), 200, {"Cache-Control": "no-store"}
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return jsonify(error="limit must be an integer"), 400
        if not 1 <= limit <= 200:
            return jsonify(error="limit must be between 1 and 200"), 400
        return (
            jsonify(history=hitl_broker.history(limit), server_time=time.time()),
            200,
            {"Cache-Control": "no-store"},
        )

    @bp.post("/api/hitl/<request_id>/decision")
    def hitl_decision(request_id):
        if hitl_broker is None:
            return jsonify(error="HITL is not configured"), 404
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="JSON body is required"), 400
        try:
            action = HITLAction(payload.get("action"))
        except (TypeError, ValueError):
            return jsonify(error="action must be approve, edit, reject, or respond"), 400
        allow_kind = None
        if "allow_again" in payload or "view_token" in payload:
            # ADR 0025 §4a / §9: one of the two card buttons. ``session_allow.ENABLED`` off:
            # refused, nothing decided.
            if not allow_runtime.enabled():
                return jsonify(error="This option is not available.", code="not_enabled"), 409
            allow_kind = payload.get("allow_again")
            if (
                action != HITLAction.APPROVE
                or allow_kind not in ("session", "always")
                or not isinstance(payload.get("view_token"), str)
            ):
                return (
                    jsonify(
                        error="allow_again needs action approve, session or always, and the "
                        "card's view_token",
                        code="allow_invalid",
                    ),
                    400,
                )
        if action in (HITLAction.APPROVE, HITLAction.EDIT):
            # Only with STIRA_YOLO_ALLOW: not while an agent tool call runs (ADR 0012 §8b,
            # one layer; never a proof of a person). Reject / respond are never refused.
            running = yolo_runtime.tool_call_refusal()
            if running is not None:
                return jsonify(error=running["error"], code=running["code"]), 409
        edited_input = payload.get("edited_input")
        response_text = payload.get("response")
        bound_hash = payload.get("args_hash")
        reason = payload.get("reason")
        client_id = payload.get("client_id")
        if action == HITLAction.EDIT and not isinstance(edited_input, dict):
            return jsonify(error="edit requires edited_input object"), 400
        if action == HITLAction.RESPOND and not isinstance(response_text, str):
            return jsonify(error="respond requires response string"), 400
        if action != HITLAction.REJECT and not isinstance(bound_hash, str):
            return jsonify(error="args_hash of the displayed request is required"), 400
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
            return jsonify(error="reason must be a string of at most 2000 characters"), 400
        if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 64):
            return jsonify(error="client_id must be a short string"), 400
        # ADR 0012 §8b (phase 2c, operator decision 2026-10-19): approve / edit of a card is
        # one press with or without STIRA_YOLO_ALLOW (a single decision bound to its
        # args_hash, not a loosening of what may run); only the running-tool-call refusal
        # above applies to it while the allowance is set.
        if progress is not None:
            progress.note_contact()  # ADR 0014 §2: a person deciding a card is present
        decision = HITLDecision(
            action,
            edited_input=edited_input,
            response=response_text,
            reason=(reason.strip() or None) if isinstance(reason, str) else None,
        )
        if allow_kind is not None:
            return _allow_press(request_id, decision, allow_kind, payload, bound_hash, client_id)
        outcome = hitl_broker.decide(
            request_id, decision, args_hash=bound_hash, client_id=client_id
        )
        if not outcome:
            return jsonify(error=_OUTCOME_ERROR[outcome], code=outcome.value), _OUTCOME_STATUS[
                outcome
            ]
        return jsonify(status="accepted"), 202

    def _allow_press(request_id, decision, kind, payload, bound_hash, client_id):
        """ADR 0025 §4a (button 1) / §9 (button 2): the class is recomputed from the
        **stored** request; the card's single-use store transition runs inside the press,
        after the view token's CAS and the decision row — a stale view approves nothing."""
        service = allow_runtime.service()
        item = getattr(hitl_broker, "pending_entry", lambda _rid: None)(request_id)
        if service is None or item is None:
            return jsonify(error=_OUTCOME_ERROR[DecideOutcome.NOT_FOUND], code="not_found"), 404
        if bound_hash != item.get("args_hash"):
            return (
                jsonify(
                    error=_OUTCOME_ERROR[DecideOutcome.BINDING_MISMATCH], code="binding_mismatch"
                ),
                409,
            )
        decided = {}

        def decide() -> bool:
            outcome = hitl_broker.decide(
                request_id, decision, args_hash=bound_hash, client_id=client_id
            )
            decided["outcome"] = outcome
            return bool(outcome)

        press = service.press if kind == "session" else service.press_always
        result = press(
            payload["view_token"],
            request_id=request_id,
            args_hash=item["args_hash"],
            tool=item.get("tool_name"),
            tool_input=item.get("tool_input"),
            policy=allow_runtime.policy(),
            origin=allow_runtime.origin_of(item),
            decide=decide,
        )
        if result.status == "refused":
            outcome = decided.get("outcome")
            if outcome is not None and not outcome:
                return jsonify(error=_OUTCOME_ERROR[outcome], code=outcome.value), _OUTCOME_STATUS[
                    outcome
                ]
            status = 503 if result.code == "audit_failed" else 409
            return jsonify(
                error=_ALLOW_ERROR.get(result.code, "Not approved."), code=result.code
            ), (status)
        return (
            jsonify(
                status="accepted",
                allow={"status": result.status, "code": result.code, "button": kind},
            ),
            202,
        )

    @bp.get("/api/turns")
    def turn_list():
        if turn_recorder is None:
            return jsonify(error="turn events are not configured"), 404
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            return jsonify(error="limit must be an integer"), 400
        if not 1 <= limit <= 200:
            return jsonify(error="limit must be between 1 and 200"), 400
        with state_lock:
            run = running.get("run")
        live = run["turn_id"] if run is not None and not run["done"].is_set() else None
        # ``stoppable``: the running chat turn, the one POST /api/chat/cancel can stop (#452);
        # a turn resumed after an approval is not (the chat page offers no ■ Stop for it).
        turns_out = [
            {**turn, "stoppable": live is not None and turn.get("turn_id") == live}
            for turn in turn_recorder.store.recent_turns(limit)
        ]
        return jsonify(turns=turns_out), 200, {"Cache-Control": "no-store"}

    @bp.get("/api/turns/<turn_id>/events")
    def turn_event_list(turn_id):
        if turn_recorder is None:
            return jsonify(error="turn events are not configured"), 404
        try:
            after_seq = int(request.args.get("after_seq", "0"))
        except ValueError:
            return jsonify(error="after_seq must be an integer"), 400
        if after_seq < 0:
            return jsonify(error="after_seq must be >= 0"), 400
        events = turn_recorder.store.events(turn_id, after_seq=after_seq)
        return jsonify(turn_id=turn_id, events=events), 200, {"Cache-Control": "no-store"}

    @bp.get("/api/activity")
    def activity_board():
        """#446: the Stage board's activity mode in one read (state, recent rows after the
        cursor ``after_id``, the YOLO window, totals since the server started, the stall
        hint). Allow-listed codes, counts, times and tool names only (``activity.py``)."""
        if turn_recorder is None:
            return jsonify(error="turn events are not configured"), 404
        raw = request.args.get("after_id", "0")
        if len(raw) > 19 or not raw.isascii() or not raw.isdigit():
            return jsonify(error="after_id must be a non-negative integer"), 400
        after_id = int(raw)
        store = turn_recorder.store
        if after_id and after_id > store.counts()["latest_id"]:
            after_id = 0  # a cursor from another store (a reset data dir): start over
        if after_id == 0:
            # The cursor first: a row written after it comes with the next diff, not lost.
            cursor = store.counts()["latest_id"]
            # The listed types only: a long model call writes many model.progress rows.
            rows = [
                row
                for row in store.tail_types(activity.LISTED_TYPES, activity.RECENT_LIMIT * 3)
                if row[0] <= cursor
            ]
        else:
            rows = store.events_after(after_id, activity.DIFF_LIMIT)
            cursor = rows[-1][0] if rows else after_id
        items = [item for row in rows if (item := activity.activity_item(*row)) is not None]
        if after_id == 0:
            items = items[-activity.RECENT_LIMIT :]
        _attach_artifacts(items)
        with state_lock:
            run = running.get("run")
        live = run["turn_id"] if run is not None and not run["done"].is_set() else None
        pending = []
        if hitl_broker is not None:
            try:
                pending = hitl_broker.pending()
            except Exception:  # noqa: BLE001 - the board shows what it can read
                pending = []
        try:
            yolo = yolo_runtime.status()
        except Exception:  # noqa: BLE001
            yolo = None
        window = activity.window_view(yolo)
        breaker = field(field(agent, "breaker", {}), "level")
        state = activity.live_state(
            store.events(live) if live is not None else None,
            pending=pending,
            breaker=field(breaker, "value", breaker),
            window=window,
            latched=bool(isinstance(yolo, Mapping) and yolo.get("latched") is True),
            report=_activity_report(),
        )
        if live is not None:
            state["turn"] = live
        if token_prices:
            totals = activity.totals_from(
                store.completed_since(started_wall), token_prices, model=_model_in_use()
            )
        else:
            totals = activity.totals_from(store.completed_since(started_wall), token_rates)
        extra = {}
        if status_provider:
            try:
                extra = status_provider() or {}
            except Exception:  # noqa: BLE001
                extra = {}
        context_window = field(extra.get("context_window") or {}, "tokens")
        if type(context_window) is int and context_window > 0:
            totals["context_window"] = context_window
        body = {
            "server_time": time.time(),
            "started_at": started_wall,
            "cursor": cursor,
            "reset": after_id == 0,
            "more": len(rows) >= activity.DIFF_LIMIT and after_id != 0,
            "items": items,
            "state": state,
            "yolo": window,
            "totals": totals,
            "stall_sec": activity.STALL_SEC,
        }
        grant = _grant_block(pending)
        if grant is not _NO_GRANTS:
            body["grant"] = grant  # ADR 0011 stage C only (W5b): off, the answer is as before
        return jsonify(body), 200, {"Cache-Control": "no-store"}

    def _grant_block(pending):
        """W5b (#296): the newest goal grant for the board — the grant service's and the
        executor's in-memory reads (no look, no I/O, no lock of this API), allow-listed by
        ``activity.grant_view``. Without stage C: :data:`_NO_GRANTS` (no key at all). A
        failed read is a fixed code, never a 500 of the whole answer."""
        grants = current_app.extensions.get("grants")
        if grants is None:
            return _NO_GRANTS
        try:
            # One consistent version (W5b fix round 2): the newest record (immutable),
            # then the executor's pieces (its own write check), then the record again —
            # moved in between means a change was written meanwhile: busy, never a mix.
            # The executor's own read is a seqlock read (its version once, the pieces, the
            # version again); the two record reads bracket it.
            record = grants.board_record()
            executor = current_app.extensions.get("grant_executor")
            board = None
            if executor is not None and record is not None:
                board = executor.board(record.grant_id)
            # W6d: the heartbeat start's counters (only with its switch on and for a grant
            # issued with it; the ticker's own non-blocking read, inside the bracket).
            ticker = current_app.extensions.get("grant_tick")
            tick = None
            if ticker is not None and record is not None and record.heartbeat_start is True:
                tick = ticker.board(record.grant_id)
            if grants.board_record() is not record:
                return {"error": "grant_busy"}
            view = grants.board_view(record)
            if tick is None:
                return activity.grant_view(view, board, pending=pending, since=started_wall)
            return activity.grant_view(view, board, pending=pending, since=started_wall, tick=tick)
        except Exception as error:  # noqa: BLE001 - the board shows what it can read
            # a lock on the read path was held (grants.schema.BoardBusy): never waited for
            busy = getattr(error, "board_busy", False) is True
            return {"error": "grant_busy" if busy else "grant_unreadable"}

    @bp.get("/api/turns/<turn_id>/tools/<tool_use_id>/<kind>")
    def tool_payload(turn_id, tool_use_id, kind):
        """Paged, masked tool input/output (#188): <= 200 lines / 100 KiB per page."""
        if turn_recorder is None:
            return jsonify(error="turn events are not configured"), 404
        if kind not in ("input", "output"):
            return jsonify(error="kind must be input or output"), 400
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            return jsonify(error="offset must be an integer"), 400
        if offset < 0:
            return jsonify(error="offset must be >= 0"), 400
        page = turn_recorder.store.payload_page(turn_id, tool_use_id, kind, offset)
        if page is None:
            return jsonify(error="payload not recorded"), 404
        return jsonify(page), 200, {"Cache-Control": "no-store"}

    @bp.post("/api/chat")
    def chat():
        payload = request.get_json(silent=True)
        message = payload.get("message", "") if isinstance(payload, dict) else None
        if not isinstance(message, str) or len(message) > 16000:
            return jsonify(error="1〜16000文字で入力してください / Enter 1–16000 characters"), 400
        try:
            images = decode_images(payload.get("images", []))
        except (BadRequest, RequestEntityTooLarge) as error:
            return jsonify(error=error.description), error.code
        if not message.strip() and not images:
            return jsonify(error="メッセージまたは画像が必要です / Message or image required"), 400
        if images and _rejects_images(agent):
            return (
                jsonify(
                    error="このモデルは画像入力に対応していません / "
                    "The selected model does not accept images",
                    capability="vision",
                ),
                400,
            )
        prompt = message.strip()
        if images:
            prompt = ([{"text": prompt}] if prompt else []) + images
        reply = payload.get("progress_reply") if isinstance(payload, dict) else None
        if reply is not None and (
            not isinstance(reply, dict)
            or type(reply.get("report_id")) is not str
            or not 1 <= len(reply["report_id"]) <= 64
            or reply.get("answer") not in ("continue", "redirect")
        ):
            return jsonify(error="progress_reply: report_id and answer", code="invalid_body"), 400
        if reply is not None and reply["answer"] == "redirect":
            line = message.strip()
            for prefix in REDIRECT_PREFIXES:
                if line.startswith(prefix):
                    line = line[len(prefix) :].strip()
                    break
            if len(line) > REDIRECT_MAX_CHARS:
                return (
                    jsonify(
                        error=f"方向修正は {REDIRECT_MAX_CHARS} 文字までです / "
                        f"A change of direction is at most {REDIRECT_MAX_CHARS} characters",
                        code="redirect_too_long",
                    ),
                    400,
                )

        # "Busy" runs nothing: its Idempotency-Key is released so the same message can
        # be sent again with the same key (#185).
        release_key = idempotency_release()
        # ADR 0011 W5/W6b: the turn lock pre-empts a goal iteration before it is tried.
        if not generation_lock.acquire(blocking=False):
            release_key()
            return _stream_response(
                iter([_sse("error", {"error": BUSY_MESSAGE, "code": "turn_running"})])
            )
        app = current_app._get_current_object()
        stream = TurnStream(STREAM_FRAME_LIMIT)
        run = {
            "turn_id": uuid4().hex if turn_recorder is not None else None,
            "stream": stream,
            "cancel": threading.Event(),
            "done": threading.Event(),
            # Held around each repeated cancel() and around setting "done" (#482 M1).
            "cancel_lock": threading.Lock(),
        }
        answer = None
        try:
            if progress is not None:
                # A person's message answers the pending report (the button's answer, or
                # ``message``) and cancels its automatic continue (ADR 0014 §4). The
                # answer is recorded only once its turn has started (PR #489 review).
                answer = _take_reply(reply, run["turn_id"])
            worker = threading.Thread(
                target=run_turn,
                args=(app, run, prompt, message, images),
                name=f"chat-turn-{(run['turn_id'] or 'x')[:8]}",
                daemon=True,
            )
            with state_lock:
                running["run"] = run
            worker.start()
        except BaseException:
            # Nothing ran: the lock and the slot are free again.
            with state_lock:
                if running.get("run") is run:
                    running["run"] = None
            generation_lock.release()
            raise
        if answer is not None:
            _append_report_event(*answer)  # never raises (best effort, logged)
        return _stream_response(stream.read(KEEPALIVE_SEC))

    def _stream_response(body):
        return Response(
            body,
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    def drain_ui(ui_queue, out):
        while True:
            try:
                event = ui_queue.get_nowait()
            except queue.Empty:
                return
            out(_sse("ui_event", event))

    def finish_ui(ui, usage, *, error, stop, reason=None, cut=None, failure=None):
        """``turn.completed`` exactly once per turn (the worker and a server stop may race)."""
        if turn_recorder is None or ui["turn_id"] is None:
            return False
        with ui["lock"]:
            if ui["done"]:
                return False
            ui["done"] = True
        if stop != "interrupted":
            summary = ui_summary()
            if summary is not None:
                turn_recorder.emit("turn.summary", summary)
                level = _breaker_level(summary.get("breaker"))
                with state_lock:
                    previous = last_breaker["level"]
                    if level is not None:
                        last_breaker["level"] = level
                if previous is not None and level is not None and level != previous:
                    turn_recorder.emit(
                        "breaker.transition", _transition_event(previous, level, summary)
                    )
        completed = {"usage": dict(usage), "stop": stop, "error": error}
        if reason is not None:
            completed["reason"] = reason
        if cut is not None:
            completed["cut"] = cut
        if failure is not None:
            # Why the model call failed: a fixed code (and status/provider/host), no text.
            completed["failure"] = failure["reason"]
            for key in ("http_status", "provider", "host"):
                if key in failure:
                    completed[f"failure_{key}"] = failure[key]
        usage_reason = _usage_reason(usage, stop)
        if usage_reason is not None:
            completed["usage_reason"] = usage_reason
        # Which model the usage belongs to: /observability's by-model table (#286).
        model_id = field(agent, "model_id")
        if isinstance(model_id, str) and model_id:
            completed["model"] = model_id
        # Prompt-prefix hashes and what changed since the last turn (#254).
        prefix = field(agent, "last_prompt_cache")
        if isinstance(prefix, Mapping):
            completed["prompt_cache"] = dict(prefix)
        turn_recorder.emit("turn.completed", completed)
        return True

    def bill_failed_turn(usage):
        """Take the counts of a call that raised (Bedrock bills it) into usage and totals."""
        _merge_reported_usage(usage, field(agent, "last_failed_usage"))
        with state_lock:
            totals["total_input_tokens"] += usage["input_tokens"] or 0
            totals["total_output_tokens"] += usage["output_tokens"] or 0

    def run_turn(app, run, prompt, message, images, auto=None):
        """The turn, on its own thread (#452): it holds the turn lock from the request that
        started it to its end. The response stream is one reader of ``run["stream"]``;
        closing it never stops the turn. Only an explicit stop (POST /api/chat/cancel) or
        the server's stop ends it early."""
        out = run["stream"].put
        parts = []
        usage = {"input_tokens": None, "output_tokens": None}
        chunks = None
        ui_queue = queue.SimpleQueue()
        ui = {"turn_id": run["turn_id"], "done": False, "lock": threading.Lock()}
        run["ui"], run["usage"] = ui, usage
        unsubscribe = None
        try:
            with app.app_context():
                try:
                    if turn_recorder is not None:
                        turn_recorder.active.set(ui["turn_id"])

                        # #445: model.progress is written by a ticker while the model call
                        # runs and no chunk wakes this loop: it goes to the page at once.
                        def on_event(event):
                            if event.get("type") == "model.progress":
                                out(_sse("ui_event", event))
                                return
                            ui_queue.put(event)
                            if progress is not None:
                                # ADR 0014 phase 4 (#451): the token count of the turn.
                                progress.note_event(event)

                        unsubscribe = turn_recorder.subscribe(on_event)
                        ui_before["state"] = neural_snapshot()
                        started = {"prompt_chars": len(message.strip()), "image_count": len(images)}
                        if auto is not None:
                            started["auto_continue"] = auto["report_id"]
                        turn_recorder.emit("turn.started", started)
                        drain_ui(ui_queue, out)
                    if progress is not None:
                        progress.turn_started(ui["turn_id"], auto=auto is not None)
                    # ADR 0012 §3: a human chat / Stage turn — the only origin YOLO covers.
                    # (A runner that streams lazily runs its tools outside this mark and
                    # is not covered: fail closed.) Bound to this agent and this turn; set
                    # on this worker thread, so the SDK's threads copied from it carry it
                    # and nothing else does (#452).
                    with yolo_origin.human_turn(
                        getattr(agent, "agent_id", None), automatic=auto is not None
                    ):
                        chunks = iterate(runner(prompt))

                    for chunk in chunks:
                        result = field(chunk, "result", chunk)
                        metrics = field(result, "metrics", {})
                        reported = field(
                            metrics,
                            "accumulated_usage",
                            field(result, "usage", {}),
                        )
                        _merge_reported_usage(usage, reported)
                        # The last model call's prompt: what the context holds now (#350).
                        last_call = _last_call_prompt_tokens(metrics, field(agent, "provider"))
                        if last_call is not None:
                            usage["context_tokens"] = last_call

                        delta = field(
                            chunk,
                            "delta",
                            field(field(chunk, "contentBlockDelta", {}), "delta", {}),
                        )
                        text = (
                            chunk
                            if isinstance(chunk, str)
                            else field(chunk, "data", field(delta, "text", ""))
                        )

                        # Final events may repeat the entire streamed message.
                        # Only use that message when no text has been emitted.
                        if not text and not parts and field(result, "message"):
                            content = field(field(result, "message"), "content", []) or []
                            text = "".join(
                                value
                                for block in content
                                if isinstance(value := field(block, "text"), str)
                            )

                        if isinstance(text, str) and text:
                            parts.append(text)
                            out(_sse("delta", {"text": text}))
                        drain_ui(ui_queue, out)
                        if run["cancel"].is_set():
                            # An explicit stop: what came so far is kept (and its usage);
                            # a runner without agent.cancel() is closed here.
                            break

                    stop = "cancelled" if run["cancel"].is_set() else "completed"
                    checkpoint = (
                        progress.turn_finished(ui["turn_id"]) if progress is not None else None
                    )
                    if checkpoint is not None and stop == "completed":
                        stop = "checkpoint"  # ADR 0014 §6: not an error
                        note = checkpoint.get("note")
                        if isinstance(note, str) and note:
                            # Phase 3: a declared checkpoint's note (the handoff message,
                            # cleaned) is the turn's last line; shown unless it already
                            # is the streamed text (the SDK's end-turn message).
                            shown = "".join(parts)
                            if not shown.rstrip().endswith(note):
                                extra = ("\n\n" if shown.strip() else "") + note
                                parts.append(extra)
                                out(_sse("delta", {"text": extra}))
                    if "cache_read_tokens" in usage or "cache_write_tokens" in usage:
                        usage["cache_hit_ratio"] = _cache_hit_ratio(usage, field(agent, "provider"))
                    summary = turn_summary()
                    # The kept/stored turn is masked (#291); the live delta above is not.
                    # Kept whether or not a page is still reading (#452).
                    turn = {
                        "message": _masked_text(message.strip()),
                        "response": _masked_text("".join(parts)),
                        **usage,
                    }
                    if stop != "completed":
                        turn["stop"] = stop
                    if auto is not None:
                        turn["auto_continue"] = True
                    if summary is not None:
                        turn["turn_summary"] = summary
                    if ui["turn_id"] is not None:
                        # Lets the chat UI reattach replayable turn events after reload (#187).
                        turn["turn_id"] = ui["turn_id"]
                    if images:
                        turn["image_count"] = len(images)
                    with state_lock:
                        turns.append(turn)
                        totals["total_input_tokens"] += usage["input_tokens"] or 0
                        totals["total_output_tokens"] += usage["output_tokens"] or 0

                    if memory_store is not None:
                        memory_store.append_turn(turn)

                    turn_event = {**usage, "status": status()}
                    if stop != "completed":
                        turn_event["stop"] = stop
                    if summary is not None:
                        turn_event["turn_summary"] = summary
                    if stop == "checkpoint":
                        # Before turn.completed: the report is part of this turn (§6).
                        try:
                            turn_event["progress"] = _make_report(
                                ui["turn_id"], checkpoint, usage, auto
                            )
                        except Exception:  # noqa: BLE001 - the turn itself has ended well
                            app.logger.warning("progress: the report could not be made")
                    if ui["turn_id"] is not None:
                        finish_ui(
                            ui, usage, error=stop not in ("completed", "checkpoint"), stop=stop
                        )
                        drain_ui(ui_queue, out)
                        turn_event["turn_id"] = ui["turn_id"]
                    out(_sse("turn", turn_event))
                except MaxTokensReachedException as exc:
                    # The exception type and a fixed code only: its message and traceback are
                    # not logged (a provider message can carry request text).
                    app.logger.warning(
                        "NXPAgent output reached max tokens: %s code=%s",
                        type(exc).__name__,
                        "E_MAX_TOKENS",
                    )
                    bill_failed_turn(usage)
                    cut_off = _cut_off_details(agent)
                    finish_ui(ui, usage, error=True, stop="max_tokens", cut=cut_off.get("cut"))
                    drain_ui(ui_queue, out)
                    # The model call was cut off before its tool call(s) were complete; the
                    # SDK raises before any tool runs, and the partial message stays in
                    # history with its tool uses replaced by text, so nothing truncated can
                    # execute.
                    message = (
                        MAX_TOKENS_BEFORE_REPLY_MESSAGE
                        if cut_off.get("cut") == CUT_BEFORE_REPLY
                        else MAX_TOKENS_MESSAGE
                    )
                    out(_sse("error", {"error": message, "code": MAX_TOKENS_CODE, **cut_off}))
                except Exception as exc:  # noqa: BLE001 - logged by type only, a failed turn
                    app.logger.error(
                        "NXPAgent chat failed: %s code=%s", type(exc).__name__, "E_CHAT_FAILED"
                    )
                    bill_failed_turn(usage)
                    stop = "cancelled" if run["cancel"].is_set() else "error"
                    # #model-failure: a fixed reason, status, provider and host only (no text).
                    failure = None if stop == "cancelled" else describe_model_failure(exc, agent)
                    finish_ui(
                        ui,
                        usage,
                        error=True,
                        stop=stop,
                        failure=failure,
                    )
                    drain_ui(ui_queue, out)
                    out(
                        _sse(
                            "error",
                            failure
                            or {
                                "error": "応答処理に失敗しました / Response processing failed",
                            },
                        )
                    )
        finally:
            try:
                if chunks is not None:
                    chunks.close()
            finally:
                try:
                    if progress is not None:
                        progress.turn_finished(ui["turn_id"])  # no-op unless still open
                    if turn_recorder is not None and ui["turn_id"] is not None:
                        # Only a worker that ended without either path above (an exception
                        # that is not an Exception) reaches this unrecorded.
                        finish_ui(
                            ui, usage, error=True, stop="interrupted", reason="worker_stopped"
                        )
                        turn_recorder.active.clear()
                        if unsubscribe is not None:
                            unsubscribe()
                finally:
                    try:
                        with state_lock:
                            if running.get("run") is run:
                                running["run"] = None
                        # With the stop's lock: no cancel() of this turn runs after this,
                        # so none can reach the next turn (this lock is released next).
                        with run["cancel_lock"]:
                            run["done"].set()
                        generation_lock.release()
                    finally:
                        # Last: a reader that saw the end may send the next message at once.
                        run["stream"].close()

    def interrupt_turn(reason="server_stopped"):
        """The server's stop (#452): record the running turn as ``interrupted``.

        Its worker is a daemon thread that ends with the process; if it still finishes, its
        own ``turn.completed`` is not written a second time. -> "interrupted" / "off".
        """
        with state_lock:
            run = running.get("run")
        ui = run.get("ui") if run is not None else None
        if ui is None:
            return "off"
        if finish_ui(ui, run.get("usage") or {}, error=True, stop="interrupted", reason=reason):
            run["stream"].put(_sse("error", {"error": INTERRUPTED_MESSAGE, "code": "interrupted"}))
        return "interrupted"

    @bp.post("/api/chat/cancel")
    def cancel_turn():
        """Stop the running turn (#452): the one explicit stop besides the server's own.

        The SDK stops at its next safe point (during the model's stream, before a tool, after
        a tool); a card of this turn that waits for a person is rejected so the stop does not
        wait for it. The turn is recorded ``stop: "cancelled"`` with what it produced so far.
        Closing the page never does this. Body: empty or ``{"turn_id": ...}`` (refused when
        it names another turn). Same-origin + CSRF like every POST.
        """
        payload = request.get_json(silent=True)
        wanted = payload.get("turn_id") if isinstance(payload, dict) else None
        if wanted is not None and not isinstance(wanted, str):
            return jsonify(error="turn_id must be a string", code="invalid_body"), 400
        with state_lock:
            run = running.get("run")
        if run is None or run["done"].is_set():
            return jsonify(error=CANCEL_REFUSALS["no_turn"], code="no_turn"), 409
        if wanted is not None and wanted != run["turn_id"]:
            return jsonify(error=CANCEL_REFUSALS["turn_mismatch"], code="turn_mismatch"), 409
        first = not run["cancel"].is_set()
        publish_allow_end("stop")  # ADR 0025 §5 (Q5 A): a person's stop ends allowances
        run["cancel"].set()
        rejected = _reject_cards_of(run["turn_id"]) if first else 0
        if first:
            current_app.logger.info("chat turn stop requested (turn %s)", run["turn_id"])
            threading.Thread(
                target=_keep_cancelling, args=(run,), name="chat-turn-cancel", daemon=True
            ).start()
        return jsonify(status="cancelling", turn_id=run["turn_id"], rejected=rejected), 202

    def _keep_cancelling(run):
        """Until the stopped turn has ended: repeat agent.cancel() and reject the turn's cards.

        A card raised after the stop (its tool was already asking) is rejected too, so the
        stop never waits for hitl_timeout_sec. Each cancel() runs under the lock the worker
        sets "done" with: it never lands after the turn ended, on the next turn's agent."""
        cancel = getattr(agent, "cancel", None)
        cancel = cancel if callable(cancel) else None
        while True:
            with run["cancel_lock"]:
                if run["done"].is_set():
                    return
                if cancel is not None:
                    try:
                        cancel()
                    except Exception:  # noqa: BLE001 - best effort; still ends as cancelled
                        logger.info("chat turn stop: agent.cancel failed")
                        cancel = None
            _reject_cards_of(run["turn_id"])
            run["done"].wait(CANCEL_REPEAT_SEC)

    def _reject_cards_of(turn_id):
        """Reject the live approval cards of the stopped turn (a rejection is never refused)."""
        if hitl_broker is None or turn_id is None:
            return 0
        count = 0
        try:
            pending = hitl_broker.pending()
        except Exception:  # noqa: BLE001 - the stop itself still holds
            return 0
        for item in pending:
            if item.get("turn_id") != turn_id or item.get("restored"):
                continue
            outcome = hitl_broker.decide(
                item["id"],
                HITLDecision(HITLAction.REJECT, reason="the turn was stopped"),
                resolver="operator",
            )
            count += bool(outcome)
        return count

    # -- ADR 0014 phase 1 (#451): progress reports -----------------------------------------

    def _latest_report():
        with state_lock:
            return dict(reports[-1]) if reports else None

    def _report_events(entry):
        """The report's own events from turns.db (the record): -> (report data, answers)."""
        report, answers = None, []
        for event in turn_recorder.store.events(entry["turn_id"]):
            data = event.get("data") or {}
            if data.get("report_id") != entry["report_id"]:
                continue
            if event["type"] == "progress.report":
                report = dict(data)
                report["ts"] = event.get("ts")
            elif event["type"] in ("progress.reply", "progress.auto_continue"):
                answers.append({**data, "type": event["type"], "ts": event.get("ts")})
        return report, answers

    def _artifact_counts(turn_ids):
        """#535: ``{turn id: n > 0}``; ``{}`` without the store or when it cannot be read."""
        if artifact_counts is None or not turn_ids:
            return {}
        try:
            counts = artifact_counts(list(turn_ids))
        except Exception:  # noqa: BLE001 - a link is never needed to show the row
            return {}
        if not isinstance(counts, Mapping):
            return {}
        return {k: v for k, v in counts.items() if type(k) is str and type(v) is int and v > 0}

    def _attach_artifacts(items):
        """A turn's end row (and a done file_write / editor row) gets ``artifacts``: the
        number of artifacts that turn wrote. Nothing when it wrote none."""
        turns = {i["turn"] for i in items if i.get("turn")}
        counts = _artifact_counts(sorted(turns))
        for item in items:
            n = counts.get(item.get("turn"))
            if n is None:
                continue
            ended = item["kind"] == "turn" and item["what"] == "completed"
            wrote = item["kind"] == "tool" and item.get("ok") is True
            if ended or (wrote and item.get("name") in ("file_write", "editor")):
                item["artifacts"] = n

    def _activity_report():
        """#446 / ADR 0014: the pending report's own event data (trigger, time), or None."""
        if progress is None:
            return None
        try:
            entry = _pending_report()
            return _report_events(entry)[0] if entry is not None else None
        except Exception:  # noqa: BLE001 - the board shows what it can read
            return None

    def _pending_report():
        entry = _latest_report()
        if entry is None:
            return None
        _report, answers = _report_events(entry)
        return None if answers else entry

    def _append_report_event(entry, event_type, data):
        try:
            turn_recorder.store.append(entry["turn_id"], event_type, data)
            return True
        except Exception:  # noqa: BLE001 - the answer itself still holds
            logger.warning("progress: %s not recorded", event_type)
            return False

    def _take_reply(reply, next_turn_id):
        """In chat(), with the turn lock held: the person's message answers the pending
        report (in memory now: the continue is cancelled). -> the ``progress.reply`` event
        to record once the turn has started, or None. A reply that names another report
        (stale page) is an ordinary message."""
        progress.answered(human=True)
        entry = _pending_report()
        if entry is None:
            return None
        answer = "message"
        if reply is not None and reply["report_id"] == entry["report_id"]:
            answer = reply["answer"]
        data = {"report_id": entry["report_id"], "answer": answer}
        if next_turn_id is not None:
            data["next_turn_id"] = next_turn_id
        return entry, "progress.reply", data

    def _make_report(turn_id, checkpoint, usage, auto):
        """``progress.report`` on the ending turn (codes, counts, ids), then the automatic
        continue is scheduled or not. -> the report view for the turn's SSE frame."""
        from stiracore.progress import facts as progress_facts
        from stiracore.progress import new_report_id

        report_id = new_report_id()
        events = turn_recorder.store.events(turn_id)
        data = {
            "report_id": report_id,
            "trigger": checkpoint.get("trigger", "time"),
            "end": checkpoint["end"],
            "elapsed_sec": checkpoint["elapsed_sec"],
            "tools": progress_facts.tool_counts(events, checkpoint["cancelled"]),
            "usage": progress_facts.usage_facts(usage),
        }
        if type(usage.get("context_tokens")) is int:
            data["context_tokens"] = usage["context_tokens"]
        if checkpoint.get("count") in ("tools", "tokens"):
            data["count"] = checkpoint["count"]
        window = progress.window()
        yolo = progress_facts.yolo_facts(window)
        if yolo is not None:
            data["yolo"] = yolo
        if auto is not None:
            data["after_auto"] = auto["n"]
        turn_recorder.emit("progress.report", data)
        entry = {"report_id": report_id, "turn_id": turn_id}
        if isinstance(checkpoint.get("note"), str):
            entry["note"] = checkpoint["note"]  # phase 3: in memory only, like the list
        with state_lock:
            reports.append(entry)
        progress.report_made(report_id, turn_id)
        return {"report_id": report_id, "end": checkpoint["end"]}

    def _auto_message(entry):
        with state_lock:
            original = next(
                (t.get("message") for t in reversed(turns) if not t.get("auto_continue")), ""
            )
        ja = isinstance(original, str) and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", original)
        return AUTO_CONTINUE_MESSAGE["ja" if ja else "en"]

    def start_auto(auto):
        """The tracker's timer, under its start lock (not its state lock): start the one
        continuation turn (§4). Its ``progress.auto_continue`` event is written once the
        turn has started.

        Only for the newest report, still unanswered, and only if no turn runs (a person's
        turn holds the lock: that is an answer). The turn is a chat turn like the person's
        own (its mark, the gates, the caps, the stop) — nothing about it is looser.
        """
        app = holder["app"]
        entry = _latest_report()
        if app is None or entry is None or entry["report_id"] != auto["report_id"]:
            return False
        if _pending_report() is None:
            return False
        if not generation_lock.acquire(blocking=False):
            return False
        stream = TurnStream(STREAM_FRAME_LIMIT)
        stream.detach()  # nobody reads it: the pages poll the turn and its events
        run = {
            "turn_id": uuid4().hex,
            "stream": stream,
            "cancel": threading.Event(),
            "done": threading.Event(),
            "cancel_lock": threading.Lock(),
            "auto": True,
        }
        message = _auto_message(entry)
        try:
            worker = threading.Thread(
                target=run_turn,
                args=(app, run, message, message, []),
                kwargs={"auto": auto},
                name=f"chat-turn-{run['turn_id'][:8]}",
                daemon=True,
            )
            with state_lock:
                running["run"] = run
            worker.start()
        except BaseException:
            with state_lock:
                if running.get("run") is run:
                    running["run"] = None
            generation_lock.release()
            raise
        _append_report_event(
            entry,
            "progress.auto_continue",
            {"report_id": entry["report_id"], "n": auto["n"], "next_turn_id": run["turn_id"]},
        )
        logger.info("progress: automatic continue %d started (turn %s)", auto["n"], run["turn_id"])
        return True

    if progress is not None:
        progress.set_starter(start_auto)

        @bp.get("/api/progress/current")
        def progress_current():
            """The newest report of this conversation, from its turn events (§6): facts,
            the note (the turn's masked text, cleaned), its answer, the continue timer."""
            from stiracore.progress import facts as progress_facts

            body = {
                "report": None,
                **progress.window_state(),
                "settings": progress.settings.view(),
                "consecutive_auto": progress.consecutive(),
            }
            entry = _latest_report()
            if entry is not None:
                report, answers = _report_events(entry)
                if report is not None:
                    with state_lock:
                        turn = next(
                            (t for t in turns if t.get("turn_id") == entry["turn_id"]), None
                        )
                    note = entry.get("note")
                    if note is None and report.get("end") == "soft" and turn is not None:
                        note = progress_facts.clean_note(_masked_text(turn.get("response")))
                    body["report"] = {
                        **report,
                        "turn_id": entry["turn_id"],
                        "note": note,
                        "pending": not answers,
                        "answer": answers[0] if answers else None,
                        "auto": progress.auto_view(entry["report_id"]) if not answers else None,
                    }
                    count = _artifact_counts([entry["turn_id"]]).get(entry["turn_id"])
                    if count is not None:
                        body["report"]["artifacts"] = count  # #535: the link's count
            return jsonify(body), 200, {"Cache-Control": "no-store"}

        @bp.post("/api/progress/<report_id>/stop")
        def progress_stop(report_id):
            """「ここで止める」: never refused. The scheduled continue goes first (in memory),
            then the YOLO window ends (``revoked(stop)``) and a continuation turn of this
            report that already runs is stopped; the answer is recorded last (best effort)."""
            progress.cancel_auto("report_stop")
            stopped = False
            if yolo_stop is not None:
                try:
                    yolo_stop()
                    stopped = True
                except Exception:  # noqa: BLE001 - reported to the page; the rest holds
                    logger.warning("progress: the YOLO stop failed")
            with state_lock:
                run = running.get("run")
            if run is not None and run.get("auto") and not run["done"].is_set():
                run["cancel"].set()
                threading.Thread(
                    target=_keep_cancelling, args=(run,), name="chat-turn-cancel", daemon=True
                ).start()
            recorded = False
            entry = _pending_report()
            if entry is not None and entry["report_id"] == report_id:
                progress.answered(human=True)
                recorded = _append_report_event(
                    entry,
                    "progress.reply",
                    {"report_id": report_id, "answer": "stop", "yolo_stopped": stopped},
                )
            return jsonify(status="stopped", yolo_stopped=stopped, recorded=recorded), 200

        @bp.post("/api/progress/<report_id>/hold")
        def progress_hold(report_id):
            """「自動で続けない」: the scheduled continue of this report is cancelled; the
            report stays pending (nothing else changes)."""
            entry = _pending_report()
            held = False
            if entry is not None and entry["report_id"] == report_id:
                held = progress.cancel_auto("hold")
                progress.note_contact()
            return jsonify(status="held", held=held), 200

    # The server's stop calls this (stira_server registers it as a lifecycle step).
    bp.interrupt_turn = interrupt_turn

    return bp
