"""Resume a tool batch that was paused on a HITL approval before a restart (#240).

ADR 0006 §3: an approval of a restored request claims a single resume, and the paused
batch is replayed from its checkpoint (#95) only when everything still matches what the
human approved. The replayed CheckpointHooks gate asks for HITL again under current
breaker policy and the broker answers it from the store (consuming the approval once);
the operation ledger (#146) still guarantees at most one execution. Any doubt refuses the
resume and revokes the approval: nothing runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stiracore.core.approval import LEDGER_COMMITTED, args_hash, operation_key
from stiracore.core.approval_store import (
    APPROVED,
    BREAKER_REFUSAL,
    ApprovalStore,
    ApprovalStoreError,
)

__all__ = ["ResumeOutcome", "check_resumable", "replay_preview", "resume_approved_request"]

BREAKER_STRICTER = (
    "the circuit breaker is stricter than when this was requested; the approval was revoked"
)


@dataclass(frozen=True)
class ResumeOutcome:
    resumed: bool
    reason: str | None = None
    result: Any = None


def check_resumable(agent: Any, store: ApprovalStore, request_id: str) -> str | None:
    """Return why the paused batch of ``request_id`` must not be replayed, or None."""
    root = store.chain_root(request_id)
    if root is None:
        if store.get(request_id) is not None:
            return "the edit chain is too long to resume"  # ADR 0024 §7
        return "approval request not found"
    if not root.checkpoint_id:
        return "no checkpoint was recorded for this request"
    if root.agent_id != agent.agent_id:
        return "the request belongs to another agent"
    checkpoints = agent.engine.checkpoint_store
    try:
        checkpoint = checkpoints.load(root.checkpoint_id)
        newest = checkpoints.list(agent.agent_id)
    except Exception:  # noqa: BLE001 - missing/corrupt checkpoint: fail closed
        return "the paused checkpoint is unavailable"
    if checkpoint.thread_id != agent.agent_id:
        return "the paused checkpoint belongs to another agent"
    if (
        not newest
        or newest[0] != root.checkpoint_id
        or (agent.engine.last_checkpoint_id not in (None, root.checkpoint_id))
    ):
        return "the conversation has moved on since this request"
    calls = [
        call
        for call in checkpoint.pending_tool_calls
        if str(call.get("toolUseId", "")) == (root.tool_use_id or "")
    ]
    if len(calls) != 1 or calls[0].get("name") != root.tool_name:
        return "the paused call is not in the checkpoint"
    try:
        digest = args_hash(calls[0].get("input", {}))
    except (TypeError, ValueError):
        return "the paused call cannot be normalized"
    if digest != root.args_hash:
        with contextlib.suppress(ApprovalStoreError):
            store.note_binding_mismatch(root.request_id, digest)
        return "the paused call's arguments differ from the approved ones"
    return None


def replay_preview(agent: Any, record: Any) -> dict[str, int] | None:
    """What a resume of ``record`` would do with the *other* calls of its paused batch
    (ADR 0024 §5, the card's wording): counts only, never a name or an argument.

    ``from_ledger``: recorded as done, answered from the ledger (not repeated);
    ``unknown``: recorded as started or failed — it may already have run, it is refused,
    not repeated. Read-only calls run again and are not counted. None: nothing to say
    (no checkpoint, or it cannot be read).
    """
    from stiracore.agent.checkpoint_hooks import _ledgered

    if not getattr(record, "checkpoint_id", None):
        return None
    engine = agent.engine
    try:
        checkpoint = engine.checkpoint_store.load(record.checkpoint_id)
    except Exception:  # noqa: BLE001 - the resume itself refuses it; nothing to show
        return None
    counts = {"from_ledger": 0, "unknown": 0}
    for call in checkpoint.pending_tool_calls:
        tool_use_id = str(call.get("toolUseId", ""))
        name = call.get("name")
        if not tool_use_id or tool_use_id == (record.tool_use_id or ""):
            continue
        if not isinstance(name, str) or not _ledgered(engine, name):
            continue
        try:
            key = operation_key(
                engine.agent_id, name, args_hash(call.get("input", {})), tool_use_id
            )
            entry = engine.operation_ledger.get(key)
        except Exception:  # noqa: BLE001 - unreadable: "may already have run"
            counts["unknown"] += 1
            continue
        if entry is None:
            continue
        counts["from_ledger" if entry.state == LEDGER_COMMITTED else "unknown"] += 1
    return counts


def resume_approved_request(
    agent: Any,
    store: ApprovalStore,
    request_id: str,
    *,
    on_replay: Callable[[], None] | None = None,
) -> ResumeOutcome:
    """Replay the batch paused on ``request_id`` once, if it is approved and still bound.

    ``agent`` is an :class:`~stiracore.agent.nxp_agent.NXPAgent` whose broker uses
    ``store``. The caller must serialize this with ordinary turns (the chat server: turn lock).
    ``on_replay`` runs right before the replay (e.g. to open a turn record).
    """
    try:
        record = store.get(request_id)
        if record is None or record.status != APPROVED:
            return ResumeOutcome(False, "the request is not approved")
        if record.resume_claimed or record.consumed:
            return ResumeOutcome(False, "already resumed or used")
        refusal = check_resumable(agent, store, request_id)
        if refusal is None and not store.claim_resume(
            request_id, breaker_level=lambda: agent.engine.breaker_status().level
        ):
            # ADR 0024 §3: a breaker stricter than at request time refused the claim
            # (recorded and revoked inside the claim, under the store's write lock).
            after = store.get(request_id)
            if after is not None and after.resume_refused == BREAKER_REFUSAL:
                return ResumeOutcome(False, BREAKER_STRICTER)
            return ResumeOutcome(False, "already resumed, used or expired")
    except ApprovalStoreError as error:
        return ResumeOutcome(False, f"approval store unavailable: {error}")
    if refusal is not None:
        with contextlib.suppress(ApprovalStoreError):
            store.refuse_resume(request_id, refusal)  # not claimed: nothing to lend
        return ResumeOutcome(False, refusal)
    # Claimed from here on: every exit either uses the approval or revokes it.
    try:
        root = store.chain_root(request_id)
        if root is None or not root.checkpoint_id:
            raise ApprovalStoreError("the claimed request has no checkpoint")
        if on_replay is not None:
            on_replay()
        hooks = getattr(agent, "checkpoint_hooks", None)
        gate = getattr(hooks, "gate_on_resume", None)
        # ADR 0024 §7: the paused call is asked again (and answered from the store) even
        # when the rule or a recovered breaker would let it run ungated: an approved EDIT
        # must never run the original input. A host without the hooks: nothing to force.
        forced = gate(root.tool_use_id) if callable(gate) else contextlib.nullcontext()
        with store.resume_context(request_id), forced:
            agent.resume_from_checkpoint(root.checkpoint_id)
            result = agent(None)
    except Exception as error:  # noqa: BLE001 - recorded; never left claimed-but-unused
        reason = f"resume failed: {type(error).__name__}"
        _revoke(store, request_id, reason)
        return ResumeOutcome(False, reason)
    try:
        record = store.get(request_id)
        unused = record is None or not record.consumed
    except ApprovalStoreError:
        unused = True
    if unused:
        # The replay never reached the gate (e.g. the breaker recovered and the write
        # ran under normal policy, or it was refused): the approval is spent.
        _revoke(store, request_id, "the replay did not use the approval")
    return ResumeOutcome(True, result=result)


def _revoke(store: ApprovalStore, request_id: str, reason: str) -> None:
    """Revoke a claimed approval; if that cannot be recorded, quarantine the store.

    A claimed, unconsumed approval that stays APPROVED must never be lendable again,
    so a failed revocation (clock anomaly, I/O error...) stops the whole instance.
    """
    try:
        store.refuse_resume(request_id, reason)
    except Exception as error:  # noqa: BLE001 - any failure: fail closed for good
        store.quarantine(f"could not revoke claimed approval {request_id}: {error}")
