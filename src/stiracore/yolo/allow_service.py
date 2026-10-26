"""Evidence, audit rows and receipts around the allowance CAS (ADR 0025 §4, §7; PR 1).

:class:`AllowanceService` runs the I/O half of every step of §4 — the eligibility
function, the rule / roots reads under the settings store's save lock, the live breaker,
the audit rows and the read-back of their receipts — and calls the compare-and-set
methods of :class:`.session_allow.AllowanceTable` for the other half.

* §4a :meth:`press` — reserve (CAS) → ``audit.allow.decided`` + the card's store decision
  + ``audit.allow.issued`` (evidence) → commit (CAS).
* §4b :meth:`answer` — the approver at step 5 (``RULE_ASK`` only).
* §4c :meth:`consume` — ``audit.tool.started`` and the receipts (evidence) → consume (CAS).
* :meth:`record_press_call` / :meth:`outcome` — the press's own once-approved call and the
  terminal result Make Always waits for (§9, ``call_not_finished``).

Audit failure refuses the effect (§7): an unhealthy writer, a row that cannot be written
or a receipt that does not verify ends every allowance for the rest of the process and
refuses the step. Test seams: ``hooks[point]()`` runs at the named boundaries
(``after_reserve``, ``after_decided``, ``before_commit``, ``before_consume_cas``) so a
race test can stop a step there and publish an end from another thread.

The chat server wires it to the decision route, the hub save and the gate (PR 3):
``enabled`` defaults to :data:`.session_allow.ENABLED`.
"""

from __future__ import annotations

import contextlib
import hashlib
import queue
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .allow_class import (
    CLASS_VERSION,
    TargetClass,
    allow_again_eligibility,
)
from .audit_schema import SCHEMA
from .authority import Receipt
from .session_allow import (
    ENABLED,
    NOT_ENABLED,
    AllowanceTable,
    EndNotice,
    Snapshot,
    UseBinding,
    keyed_ref,
)

__all__ = ["REFUSAL_TEXT", "AllowanceService", "Evidence", "PressResult"]

REFUSAL_TEXT = {
    "ja": "継続許可が終了したため実行しませんでした。もう一度確認してください",
    "en": "Not run: the allowance ended. Ask again.",
}
_STORE_LOCK_TIMEOUT = 5.0
_SYSTEM_ACTOR = "system:stira"


@dataclass(frozen=True)
class Evidence:
    snapshot: Snapshot
    rule: str
    rule_generation: Any
    roots_digest: str
    breaker: str


@dataclass(frozen=True)
class PressResult:
    """``allowed`` (approved once + an active allowance), ``approved_once`` (the commit
    lost to a stop / change, §4a step 3) or ``refused`` (nothing approved)."""

    status: str
    code: str | None = None
    allow_id: str | None = None
    decision_ref: str | None = None


class AllowanceService:
    def __init__(
        self,
        table: AllowanceTable,
        *,
        record: Callable[..., str] | None,
        ledger: Any,
        actor: str,
        breaker: Callable[[], str],
        rules: Callable[[str], tuple[str, Any]],
        roots: Callable[[], str],
        store_lock: Any = None,
        containment: Callable[[], str] | None = None,
        loosening: Callable[[], list[str]] | None = None,
        enabled: bool = ENABLED,
        hooks: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        self.table = table
        self.enabled = enabled is True
        self._record = record
        self._ledger = ledger
        self._actor = actor
        self._breaker = breaker
        self._rules = rules
        self._roots = roots
        self._store_lock = store_lock
        self._containment = containment
        self._loosening = loosening or _default_loosening
        self.hooks = dict(hooks or {})
        self.store_lock_timeout = _STORE_LOCK_TIMEOUT
        self._key = secrets.token_bytes(32)  # keyed hashes of this process only
        self._key_id = "allow-" + hashlib.sha256(self._key).hexdigest()[:12]
        self._incarnation_ref = keyed_ref(self._key, "incarnation:" + table.incarnation)
        self._lock = threading.Lock()  # the per-request call bookkeeping (§9)
        # Card request id -> {tool, args_ref, decision_ref, intent, terminal}: the server's
        # own record of the card a button was pressed on (§7, §9). The save target tool and
        # every receipt check are derived from it, never from the caller. Boundary: the
        # approval store's ``pending → decided`` transition makes the *card decision*
        # single-use (ADR 0006); this record makes each ``audit.allow.decided`` receipt
        # authorize exactly one call row, of that card, tool and arguments.
        self._calls: dict[str, dict] = {}
        self._used_decisions: set[str] = set()
        # Review M4: a bump only hands its notice over (memory); the ``audit.allow.ended``
        # rows are written by one writer thread, never in the publisher's call — so no
        # ``engine.state_lock`` / ``ConfigStore.lock`` of the caller is held over row I/O.
        self._ended: queue.Queue = queue.Queue()
        self._ended_thread: threading.Thread | None = None
        table.on_end = self._enqueue_ended
        table.writer_probe = self._writer_ok  # read inside every grant transition

    # -- helpers ----------------------------------------------------------------------

    def _hook(self, point: str) -> None:
        hook = self.hooks.get(point)
        if hook is not None:
            hook()

    def _ref(self, text: str) -> str:
        return keyed_ref(self._key, text)

    def _target_ref(self, target: TargetClass) -> str:
        if target.class_kind == "layer":
            return "layer:" + target.class_key
        if target.class_kind == "dir_subtree":
            return "dir:" + self._ref("dir:" + target.class_key)
        if target.class_kind in ("host", "host_read"):
            return "host:" + self._ref("host:" + target.class_key)
        return "tool:" + self._ref("tool:" + target.class_key)

    def _common(self, phase: str, delta: str, human: bool, surface: str, g0: int, g1: int):
        op = secrets.token_hex(16)
        return {
            "schema": SCHEMA,
            "operation_id": op,
            "phase": phase,
            "event_id": f"op:{op}:{phase}",
            "actor_kind": "human" if human else "system",
            "actor": self._actor if human else _SYSTEM_ACTOR,
            "surface": surface,
            "incarnation_ref": self._incarnation_ref,
            "key_id": self._key_id,
            "delta_class": delta,
            "generation_from": g0,
            "generation_to": g1,
            "authority_epoch": self.table.snapshot().fence,
        }

    def _audit_ok(self) -> bool:
        """**The one place** the audit's health is judged (review round 3). ``False`` when
        there is no writer, the writer reports unhealthy (or cannot say), or the table's
        sticky failure is set — and any unhealthy writer observed here is published to the
        table at once (``mark_audit_failed``: health epoch + 1, fence + 1, until restart).
        Every row write, best-effort rows included, and every final confirmation goes
        through it; nothing else reads the writer's health."""
        if not self._writer_ok():
            self.table.mark_audit_failed()
            return False
        return not self.table.audit_failed

    def _writer_ok(self) -> bool:
        """The writer's current health — a flag read, no I/O and no lock, so the table may
        call it inside its grant gate (``AllowanceTable.writer_probe``)."""
        try:
            return (
                self._record is not None
                and self._ledger is not None
                and self._ledger.yolo_healthy() is True
            )
        except Exception:  # noqa: BLE001 - unknown health is unhealthy
            return False

    def _audit_failed_now(self) -> None:
        """A row that could not be written: published like an unhealthy writer."""
        with contextlib.suppress(Exception):
            self.table.mark_audit_failed()

    def _confirm(self, epoch: int | None) -> bool:
        """The final confirmation of a step that wrote and verified its rows: the writer
        itself is still healthy (read now, published if not) **and**, under the allowance
        lock, the audit has not failed and its health epoch has not moved since ``epoch``."""
        return self._audit_ok() and self.table.audit_still_healthy(epoch)

    def _write(self, event_type: str, fields: dict) -> str | None:
        """Write one row and verify its receipt; ``None`` (and every allowance ends) on
        any failure — an unhealthy writer included, and the table's sticky audit failure
        (review round 2: a failed ``ended`` row counts until restart)."""
        if not self._audit_ok():
            return None
        try:
            event_id = self._record(event_type, **fields)
        except Exception:  # noqa: BLE001 - a row that cannot be written refuses the step
            self._audit_failed_now()
            return None
        if self.verify(event_id, event_type) is None:
            self._audit_failed_now()
            return None
        return event_id

    def verify(self, event_id: Any, event_type: str, expect: dict | None = None) -> Receipt | None:
        """ADR 0013 §2b (A): read the row back; its receipt only if it is this process's
        row of the expected type **and** every ``expect`` attribute equals the stored one
        (the card, the tool, the arguments, the allowance it must belong to)."""
        if self._ledger is None or type(event_id) is not str:
            return None
        try:
            found = self._ledger.yolo_receipt(event_id)
        except Exception:  # noqa: BLE001 - an unreadable row has no receipt
            return None
        if type(found) is not dict or type(found.get("attributes")) is not dict:
            return None
        receipt = Receipt(event_id, found.get("seq"), found.get("hash"))
        if not (
            receipt.valid()
            and found.get("event_type") == event_type
            and found["attributes"].get("incarnation_ref") == self._incarnation_ref
            and all(found["attributes"].get(k) == v for k, v in (expect or {}).items())
        ):
            return None
        return receipt

    def _enqueue_ended(self, notice: EndNotice) -> None:
        """The table's ``on_end``: memory only (a queue put), never row I/O."""
        self._ended.put(notice)
        with self._lock:
            if self._ended_thread is None or not self._ended_thread.is_alive():
                self._ended_thread = threading.Thread(
                    target=self._drain_ended, name="allow-ended-rows", daemon=True
                )
                self._ended_thread.start()

    def _drain_ended(self) -> None:
        while True:
            try:
                notice = self._ended.get(timeout=1.0)
            except queue.Empty:
                with self._lock:
                    if self._ended.empty():
                        self._ended_thread = None
                        return
                continue
            try:
                self._write_ended(notice)
            finally:
                self._ended.task_done()

    def flush_ended(self) -> None:
        """Wait until every queued ``audit.allow.ended`` row was attempted (tests, stop)."""
        self._ended.join()

    def _write_ended(self, notice: EndNotice) -> None:
        """``audit.allow.ended`` rows of a bump, after the lock (best-effort: the bump
        already ended everything; a failed row only marks the writer unhealthy)."""
        if not self._audit_ok():
            return  # published; the bump already ended everything
        human = notice.reason in ("stop", "revoked")
        for ended in notice.ended:
            try:
                fields = self._common(
                    "transition",
                    "narrowing",
                    human,
                    "admin" if human else "system",
                    ended.generation_from,
                    ended.generation_to,
                )
                fields.update(
                    allow_ref=ended.allow_id,
                    end_reason=notice.reason,
                    uses=ended.uses,
                    fence=notice.fence_to,
                )
                self._record("audit.allow.ended", **fields)
            except Exception:  # noqa: BLE001
                self._audit_failed_now()
                return

    def _locked_store(self):
        lock = self._store_lock
        if lock is None:
            return contextlib.nullcontext(True)
        return _Bounded(lock, self.store_lock_timeout)

    def evidence(self, tool: str) -> Evidence | None:
        """§4.0 evidence: the snapshot, the rule and its generation and the roots digest,
        read together under the settings store's save lock; then the live breaker (no
        engine lock under the store lock). A failed read ends every allowance."""
        try:
            with self._locked_store() as held:
                if not held:
                    raise TimeoutError("settings store lock")
                snapshot = self.table.snapshot()
                rule, generation = self._rules(tool)
                digest = self._roots()
            if type(digest) is not str or not digest:
                raise ValueError("roots digest")
        except Exception:  # noqa: BLE001 - a failed read ends every allowance
            self.table.bump_fence("read_failed")
            return None
        self.table.observe_roots(digest)  # a changed digest bumps (the snapshot is stale)
        try:
            level = self._breaker()
        except Exception:  # noqa: BLE001 - an unreadable breaker is a departure
            level = "unknown"
        if level != "normal":
            self.table.bump_departure()
        if self._containment is not None:
            try:
                stage = self._containment()
            except Exception:  # noqa: BLE001
                stage = "unknown"
            if stage != "none":
                self.table.bump_fence("containment")
        return Evidence(snapshot, rule, generation, digest, level)

    # -- §4a: the offer and the press -------------------------------------------------

    def offer(
        self,
        *,
        request_id: str,
        args_hash: str,
        tool: str,
        tool_input: Any,
        policy: Any,
        origin: Any,
    ) -> dict:
        """The card's view: ``{"offered": True, "class_kind", "class_key", "whole_root",
        "token"}`` or ``{"offered": False, "reason"}``. The clear class text goes to the
        page only (never to a row)."""
        if not self.enabled:
            return {"offered": False, "reason": NOT_ENABLED}
        target = allow_again_eligibility(tool, tool_input, policy, origin)
        if isinstance(target, str):
            return {"offered": False, "reason": target}
        evidence = self.evidence(tool)
        if evidence is None:
            return {"offered": False, "reason": "read_failed"}
        if evidence.breaker != "normal":
            return {"offered": False, "reason": "breaker"}
        if evidence.rule != "ask":
            return {"offered": False, "reason": "rule_not_ask"}
        token = self.table.issue_view_token(
            request_id=request_id,
            args_hash=args_hash,
            target=target,
            rule_generation=evidence.rule_generation,
        )
        if token is None:
            return {"offered": False, "reason": "audit_failed"}
        return {
            "offered": True,
            "class_kind": target.class_kind,
            "class_key": target.class_key,
            "whole_root": target.whole_root,
            "token": token,
        }

    def press(
        self,
        token: Any,
        *,
        request_id: str,
        args_hash: str,
        tool: str,
        tool_input: Any,
        policy: Any,
        origin: Any,
        decide: Callable[[], bool],
    ) -> PressResult:
        """Button 1. ``decide`` is the card's existing single-use store transition
        (``pending → decided(approved)``); it runs only after the reserve CAS and the
        decision row, so a stale view or a failed audit approves nothing."""
        if not self.enabled:
            return PressResult("refused", NOT_ENABLED)
        if not self._audit_ok():
            return PressResult("refused", "audit_failed")
        target = allow_again_eligibility(tool, tool_input, policy, origin)
        if isinstance(target, str):
            return PressResult("refused", "class_changed")
        evidence = self.evidence(tool)
        if evidence is None:
            return PressResult("refused", "stale_view")
        if evidence.rule != "ask" or "allow_not_settable" in self._loosening():
            return PressResult("refused", "stale_view")
        record, code = self.table.reserve(
            token,
            request_id=request_id,
            args_hash=args_hash,
            target=target,
            rule_generation=evidence.rule_generation,
            roots_digest_value=evidence.roots_digest,
        )
        if record is None:
            return PressResult("refused", code)
        self._hook("after_reserve")
        session_ref = self._ref(f"session:{record.session_epoch}")
        source_ref = self._ref("request:" + request_id)
        expected = "pending:" + target.class_kind
        fields = self._common("decision", "loosening", True, "chat", 0, 0)
        fields.update(
            source_request_ref=source_ref,
            operation=tool,
            args_ref=self._ref("args:" + args_hash),
            button="allow_again",
            session_ref=session_ref,
            expected=expected,
            allow_id=record.allow_id,
            op_token_ref=self._ref("token:" + str(token)),
        )
        decision_ref = self._write("audit.allow.decided", fields)
        if decision_ref is None:
            self.table.abort(record.allow_id)
            return PressResult("refused", "audit_failed")
        self._hook("before_decide")
        if not self._confirm(record.health_epoch):
            # An audit failure published after the decision row: nothing is approved (the
            # press's own call is refused at its consume point as well, §7).
            self.table.abort(record.allow_id)
            return PressResult("refused", "audit_failed")
        # The card's record first: the approved call may reach its consume point (and look
        # for this record, §7) before ``decide`` returns here.
        self._remember_card(request_id, tool, args_hash, decision_ref)
        try:
            approved = decide() is True
        except Exception:  # noqa: BLE001 - the store refused: nothing approved
            approved = False
        if not approved:
            self._forget_card(request_id, decision_ref)
            self.table.abort(record.allow_id)
            return PressResult("refused", "card_not_pending")
        self._hook("after_decided")
        fields = self._common("decision", "loosening", True, "chat", 0, 1)
        fields.update(
            allow_id=record.allow_id,
            source_request_ref=source_ref,
            decision_receipt_ref=decision_ref,
            operation=tool,
            class_kind=target.class_kind,
            class_version=CLASS_VERSION,
            target=self._target_ref(target),
            policy={
                "rules": evidence.rule_generation if type(evidence.rule_generation) is int else 0,
                "breaker": "normal",
            },
            fence=record.fence,
            expires_at=record.expires_at,
            session_ref=session_ref,
            expected=expected,
            op_token_ref=self._ref("token:" + str(token)),
        )
        issued_ref = self._write("audit.allow.issued", fields)
        self._hook("before_commit")
        try:
            level = self._breaker()
        except Exception:  # noqa: BLE001
            level = "unknown"
        if level != "normal":
            self.table.bump_departure()
        cause = self.table.commit(
            record.allow_id,
            breaker_normal=level == "normal",
            issued_ref=issued_ref or "",
            decision_ref=decision_ref,
        )
        if cause is None and issued_ref is None:  # unreachable: a failed row bumped
            cause = "issue_aborted_audit"
        if cause is not None:
            self._write_abort(record.allow_id, cause)
            return PressResult("approved_once", cause, None, decision_ref)
        return PressResult("allowed", None, record.allow_id, decision_ref)

    def press_always(
        self,
        token: Any,
        *,
        request_id: str,
        args_hash: str,
        tool: str,
        tool_input: Any,
        policy: Any,
        origin: Any,
        decide: Callable[[], bool],
    ) -> PressResult:
        """Button 2 (§9): approve this call once — bound to the card's view token like
        button 1, with its own decision row — and remember the card so the page may save
        "Always" through the hub after the call's terminal result (``save_token``). No
        allowance is created; nothing is saved here."""
        if not self.enabled:
            return PressResult("refused", NOT_ENABLED)
        if not self._audit_ok():
            return PressResult("refused", "audit_failed")
        target = allow_again_eligibility(tool, tool_input, policy, origin)
        if isinstance(target, str):
            return PressResult("refused", "class_changed")
        evidence = self.evidence(tool)
        if evidence is None or evidence.rule != "ask" or evidence.breaker != "normal":
            return PressResult("refused", "stale_view")
        epoch = self.table.audit_epoch()
        code = self.table.spend_view(
            token,
            request_id=request_id,
            args_hash=args_hash,
            target=target,
            rule_generation=evidence.rule_generation,
        )
        if code is not None:
            return PressResult("refused", code)
        fields = self._common("decision", "loosening", True, "chat", 0, 0)
        fields.update(
            source_request_ref=self._ref("request:" + request_id),
            operation=tool,
            args_ref=self._ref("args:" + args_hash),
            button="make_always",
            session_ref=self._ref(f"session:{self.table.session_epoch}"),
            expected="pending:" + target.class_kind,
            op_token_ref=self._ref("token:" + str(token)),
        )
        decision_ref = self._write("audit.allow.decided", fields)
        if decision_ref is None:
            return PressResult("refused", "audit_failed")
        if not self._confirm(epoch):
            return PressResult("refused", "audit_failed")
        self._remember_card(request_id, tool, args_hash, decision_ref)
        try:
            approved = decide() is True
        except Exception:  # noqa: BLE001 - the store refused: nothing approved
            approved = False
        if not approved:
            self._forget_card(request_id, decision_ref)
            return PressResult("refused", "card_not_pending")
        return PressResult("approved_once", None, None, decision_ref)

    def _remember_card(self, request_id: str, tool: str, args_hash: str, decision_ref: str):
        with self._lock:
            self._calls[request_id] = {
                "tool": tool,
                "args_ref": self._ref("args:" + args_hash),
                "decision_ref": decision_ref,
                "intent": None,
                "terminal": None,
            }
            while len(self._calls) > 500:  # memory only; the oldest cards go first
                self._calls.pop(next(iter(self._calls)))

    def _forget_card(self, request_id: str, decision_ref: str) -> None:
        with self._lock:
            entry = self._calls.get(request_id)
            if entry is not None and entry["decision_ref"] == decision_ref:
                del self._calls[request_id]

    def pressed(self, request_id: Any) -> str | None:
        """The decision row of a card pressed with one of the two buttons (its call must
        then be audited at its consume point, §7), else ``None`` (today's path)."""
        if type(request_id) is not str:
            return None
        with self._lock:
            entry = self._calls.get(request_id)
            return None if entry is None or entry.get("intent") else entry["decision_ref"]

    def card_tool(self, request_id: Any) -> str | None:
        with self._lock:
            entry = self._calls.get(request_id) if type(request_id) is str else None
            return entry["tool"] if entry else None

    def card_terminal(self, request_id: Any) -> str | None:
        with self._lock:
            entry = self._calls.get(request_id) if type(request_id) is str else None
            return entry["terminal"] if entry else None

    def _write_abort(self, allow_id: str, cause: str) -> None:
        if not self._audit_ok():
            return
        try:
            fields = self._common("transition", "narrowing", False, "system", 0, 1)
            fields.update(allow_ref=allow_id, end_reason=cause, uses=0)
            self._record("audit.allow.ended", **fields)
        except Exception:  # noqa: BLE001 - best-effort row; the failure is published
            self._audit_failed_now()

    # -- §4b: the approver -------------------------------------------------------------

    def answer(
        self, *, tool: str, tool_input: Any, policy: Any, origin: Any, args_hash: str
    ) -> UseBinding | None:
        """An automatic answer for a ``RULE_ASK`` call, or ``None`` (a person's card)."""
        if not self.enabled:
            return None
        call = allow_again_eligibility(tool, tool_input, policy, origin)
        if isinstance(call, str):
            return None
        evidence = self.evidence(tool)
        if evidence is None or evidence.rule != "ask" or evidence.breaker != "normal":
            return None
        if self.table.check_clock() is None:
            return None
        record = self.table.find(tool, call, rule_generation=evidence.rule_generation)
        if record is None or record.roots_digest != evidence.roots_digest:
            return None
        binding, _code = self.table.use(
            record.allow_id, snapshot=evidence.snapshot, call=call, args_hash=args_hash
        )
        return binding

    # -- §4c: consumption and outcomes -------------------------------------------------

    def consume(
        self,
        binding: Any,
        *,
        tool: str,
        tool_input: Any,
        policy: Any,
        origin: Any,
        args_hash: str,
    ) -> tuple[bool, str | None, str | None]:
        """``(run, code, intent_ref)``. ``run`` False: the call is not run; the model gets
        :data:`REFUSAL_TEXT` and its retry raises a card."""
        if not self.enabled or not isinstance(binding, UseBinding):
            return False, NOT_ENABLED if not self.enabled else "binding_invalid", None
        evidence = self.evidence(tool)
        if evidence is None:
            self.table.void(binding)
            return False, "read_failed", None
        call = allow_again_eligibility(tool, tool_input, policy, origin)
        if (
            isinstance(call, str)
            or call.class_key != binding.target_key
            or args_hash != binding.args_hash
            or tool != binding.tool
        ):
            self.table.void(binding)
            return False, "class_mismatch", None
        record = self.table.get(binding.allow_id)
        if record is None or not record.issued_ref:
            self.table.void(binding)
            return False, "ended", None
        fields = self._common("intent", "loosening", False, "chat", 1, 1)
        fields.update(
            operation=tool,
            args_ref=self._ref("args:" + args_hash),
            decision="auto_approved",
            authorizer_kind="session_allow",
            allow_id=record.allow_id,
            issued_ref=record.issued_ref,
        )
        intent = self._write("audit.tool.started", fields)
        if (
            intent is None
            or self.verify(record.issued_ref, "audit.allow.issued", {"allow_id": record.allow_id})
            is None
        ):
            self._audit_failed_now()
            self.table.void(binding)
            return False, "audit_failed", intent
        self._hook("before_consume_cas")
        if self.table.check_clock() is None:
            self.table.void(binding)
            self._void_row(intent, "time")
            return False, "clock_anomaly", intent
        self._hook("after_clock")
        if not self._audit_ok():
            # The writer itself is read again (published if unhealthy); the CAS below then
            # compares the health epoch under the lock (review round 3).
            self.table.void(binding)
            return False, "audit_failed", intent
        # The deadline is decided by a clock read inside the CAS (review M3).
        consumed, code = self.table.consume(binding, snapshot=evidence.snapshot)
        if consumed is None:
            self._void_row(intent, code if code in _VOID_CODES else "refused")
            return False, code, intent
        return True, None, intent

    def _void_row(self, intent: str | None, reason: str) -> None:
        if intent is None or not self._audit_ok():
            return
        try:
            fields = self._common("outcome", "narrowing", False, "chat", 1, 1)
            fields.update(intent_ref=intent, reason_code=reason)
            self._record("audit.tool.void", **fields)
        except Exception:  # noqa: BLE001 - best-effort row; the failure is published
            self._audit_failed_now()

    def record_press_call(
        self, *, request_id: str, tool: str, args_hash: str, decision_ref: str
    ) -> tuple[bool, str | None]:
        """The press's own once-approved call (§7): it runs only if its decision row's
        receipt and its call row verify here, at its consume point, **and** the audit has
        not failed — at the start and at the final confirmation under the allowance lock
        (an audit failure published in between wins; the row I/O stays outside the lock)."""
        epoch = self.table.audit_epoch()
        if epoch is None:
            return False, None
        args_ref = self._ref("args:" + args_hash)
        with self._lock:
            card = self._calls.get(request_id)
            fresh = (
                card is not None
                and card["decision_ref"] == decision_ref
                and card["tool"] == tool
                and card["args_ref"] == args_ref
                and decision_ref not in self._used_decisions
            )
            if fresh:
                self._used_decisions.add(decision_ref)  # one call row per receipt
        if not fresh:
            return False, "receipt_mismatch"
        expect = {
            "source_request_ref": self._ref("request:" + request_id),
            "operation": tool,
            "args_ref": args_ref,
        }
        if self.verify(decision_ref, "audit.allow.decided", expect) is None:
            self._audit_failed_now()
            return False, None
        fields = self._common("intent", "loosening", False, "chat", 0, 0)
        fields.update(
            operation=tool,
            args_ref=self._ref("args:" + args_hash),
            decision="approved_once",
            authorizer_kind="human_card",
            decision_ref=decision_ref,
        )
        intent = self._write("audit.tool.started", fields)
        if intent is None:
            return False, None
        self._hook("before_press_confirm")
        if not self._confirm(epoch):
            self._void_row(intent, "audit_failed")
            return False, None
        with self._lock:
            self._calls[request_id]["intent"] = intent
        return True, intent

    def outcome(self, intent: str, result: str, *, request_id: str | None = None) -> bool:
        """``committed`` / ``failed`` / ``void`` of a call row (best-effort row; the
        terminal result of a card's call is recorded for Make Always either way)."""
        if result not in ("committed", "failed", "void"):
            result = "failed"
        written = False
        if self._audit_ok():
            try:
                fields = self._common(
                    "outcome", "narrowing" if result == "void" else "loosening", False, "chat", 0, 0
                )
                fields.update(intent_ref=intent)
                if result == "void":
                    fields["reason_code"] = "refused"
                self._record(f"audit.tool.{result}", **fields)
                written = True
            except Exception:  # noqa: BLE001
                self._audit_failed_now()
        if request_id is not None:
            self.terminal(request_id, result)
        return written

    def terminal(self, request_id: str, result: str) -> bool:
        """The card's call reached a terminal result — also an approval that ended
        without running (``expired`` / ``cancelled`` / ``revoked``). Only for a card a
        button was pressed on (a server-side record); any other id is ignored."""
        with self._lock:
            entry = self._calls.get(request_id)
            if entry is None:
                return False
            entry["terminal"] = result
            return True

    # -- §9: the Make Always save token ------------------------------------------------

    def save_token(
        self,
        *,
        request_id: str,
        hub_version: str,
        rule_generation: Any,
        tool: str | None = None,
    ) -> tuple[str | None, str | None]:
        """``(token, None)`` once the card's call has a terminal result, else
        ``(None, "call_not_finished")``. The tool to make Always is the **card's** tool
        (the server's record); a caller naming another one gets ``tool_mismatch``."""
        if not self.enabled:
            return None, NOT_ENABLED
        with self._lock:
            entry = self._calls.get(request_id)
            terminal = entry.get("terminal") if entry else None
            card_tool = entry.get("tool") if entry else None
        if terminal is None or card_tool is None:
            return None, "call_not_finished"
        if tool is not None and tool != card_tool:
            return None, "tool_mismatch"
        token = self.table.issue_save_token(
            request_id=request_id,
            tool=card_tool,
            hub_version=hub_version,
            rule_generation=rule_generation,
        )
        return (token, None) if token is not None else (None, "audit_failed")


_VOID_CODES = frozenset({"ended", "moved", "time", "binding_spent", "binding_invalid"})


def _default_loosening() -> list[str]:
    """The one loosening judgement (ADR 0025 §8): issuance in ``card_allow_again``."""
    from stiracore.tools.tool_config import rules_loosening

    return rules_loosening(
        {"session_allow": "none"}, {"session_allow": "active"}, context="card_allow_again"
    )


class _Bounded:
    """Acquire a lock with a timeout (a save stopped between its bump and its replace
    keeps the lock: an evidence step waits, bounded; a timeout is a failed read)."""

    def __init__(self, lock: Any, timeout: float) -> None:
        self._lock = lock
        self._timeout = timeout
        self._held = False

    def __enter__(self) -> bool:
        self._held = bool(self._lock.acquire(timeout=self._timeout))
        return self._held

    def __exit__(self, *exc: object) -> None:
        if self._held:
            self._lock.release()
