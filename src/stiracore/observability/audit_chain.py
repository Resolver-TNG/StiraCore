"""Audit adapters onto the hash-chained EventStore (#171, ADR 0009 §8).

Approval decisions (#146 / ADR 0006), tool executions (#167, ledger phases) and
procedural-memory writes (#144) are recorded as ``audit.*`` events on a chained
:class:`SQLiteEventStore`. The adapters are *evidence only*:

* They never replace the approval store, the scheduler log or the operation ledger,
  and nothing reads a permission back from these events.
* They accept identifiers and hashes only, never arguments, content or free text.
  Identifiers must match ``[A-Za-z0-9._:@-]{1,64}`` and pass ``mask_secrets``
  unchanged, otherwise they are recorded as ``unknown``; hashes must be 64 lowercase
  hex characters, otherwise ``invalid``.
* Event kinds come from fixed sets; anything else raises ``ValueError``.

:meth:`AuditChain.mirror_approval_store` copies the approval store's own chain (only
after it verified) and cross-checks what was mirrored before: a source event whose hash
changed, or that disappeared, is recorded as ``audit.approval.source_diverged``. That
is the external anchor ADR 0006 lists as missing (an attacker who rewrites the approval
db and its head consistently is still visible here, unless they also rewrite this
chain and its head).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .event_store import SQLiteEventStore
from .hooks import ObservabilityEvent
from .masking import mask_secrets

__all__ = [
    "APPROVAL_KINDS",
    "ARTIFACT_OUTCOMES",
    "CHECKS_OUTCOMES",
    "MEMORY_OUTCOMES",
    "PROCEDURAL_OUTCOMES",
    "PROCEDURAL_REVIEW_OUTCOMES",
    "READ_REFUSAL_CODES",
    "TOOL_PHASES",
    "WEB_FAILURE_CODES",
    "WRITE_REFUSAL_CODES",
    "AuditChain",
    "MirrorResult",
]

# ADR 0006 event kinds, plus the adapter's own source-check kinds.
APPROVAL_KINDS = frozenset(
    {
        "requested",
        "decided",
        "expired",
        "cancelled",
        "consumed",
        "resume_claimed",
        "resume_refused",
        "binding_mismatch",
    }
)
_SOURCE_KINDS = frozenset({"source_unverified", "source_diverged"})
# #146 ledger phases, plus the gate's refusal (no approval / breaker).
TOOL_PHASES = frozenset({"started", "committed", "failed", "denied"})
PROCEDURAL_OUTCOMES = frozenset({"requested", "written", "denied"})
# #463: the proposal -> review flow (the agent's propose_procedure, the review screen).
PROCEDURAL_REVIEW_OUTCOMES = frozenset({"proposed", "refused", "approved", "rejected"})
# ADR 0019 phase 1 (#167): one row per run_checks call, by its status.
CHECKS_OUTCOMES = frozenset({"passed", "failed", "error", "refused", "limited"})
_CHECKS_KINDS = frozenset({"pytest", "unittest", "node_test", "ruff", "compileall"})
_CHECKS_COUNTS = frozenset({"passed", "failed", "errors", "skipped", "findings"})
_CHECKS_CONTAINMENT = frozenset({"os_sandbox", "limits_only"})
_CHECKS_TARGET = re.compile(r"workspace:[0-9a-f]{64}")
# #457: a file tool's refused read (the read floor, read roots, the secret-path floor).
READ_REFUSAL_CODES = frozenset({"E_READ_FLOOR", "E_READ_OUTSIDE_ROOTS", "E_READ_SECRET_PATH"})
# #536: a file_write / editor refused on the secret-path floor.
WRITE_REFUSAL_CODES = frozenset({"E_WRITE_SECRET_PATH"})
# #584: a failed web_fetch, by its code (``tools.web_tools.FETCH_CODES``; kept in sync by a test).
WEB_FAILURE_CODES = frozenset(
    {
        "invalid_url",
        "redirect",
        "http_error",
        "network_timeout",
        "network_connect",
        "network_error",
        "too_large",
        "unsupported_type",
        "decode_error",
        # #585: PDF extraction and the summary on request.
        "pdf_unavailable",
        "pdf_broken",
        "pdf_encrypted",
        "pdf_no_text",
        "pdf_timeout",
        "pdf_limits_unavailable",
        "summary_unavailable",
        "summary_limit",
        "summary_failed",
    }
)
# docs/task-models.md: one row per side-task model call (task, model, tokens; never text).
_TASK_MODEL_STATUSES = frozenset({"success", "error", "refused"})
_TASK_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
_MODEL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}")
_REASON = re.compile(r"[a-z][a-z_]{0,39}")
_WEB_TOOLS = frozenset({"web_fetch"})
_READ_TARGET = re.compile(r"path:[0-9a-f]{64}")
# ADR 0018 phase 0: in-turn ratings and memory-job plans (ids, codes and counts only).
MEMORY_OUTCOMES = frozenset({"rated", "refused", "planned"})
_MEMORY_LAYERS = frozenset({"episodic", "semantic", "procedural"})
_MEMORY_RATED_BY = frozenset({"agent_turn", "agent_job", "human"})
_MEMORY_TRUSTS = frozenset({"trusted", "untrusted", "unknown"})
_MEMORY_CAPPED = frozenset({"clamped", "quota", "untrusted"})
_MEMORY_KINDS = frozenset({"memory_rate_backfill", "memory_consolidate", "memory_tidy"})
_MEMORY_COUNT = re.compile(r"[a-z][a-z_]{0,39}")
# #454 (ADR 0020 §7): a person's label change on a workspace artifact (id and label codes).
ARTIFACT_OUTCOMES = frozenset({"labeled"})
_ARTIFACT_ID = re.compile(r"a[0-9a-f]{20}")
_ARTIFACT_LABELS = frozenset({"stable", "experimental", "latest"})
_ID = re.compile(r"[A-Za-z0-9._:@-]{1,64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ACTIONS = frozenset({"approve", "edit", "reject", "respond"})


def audit_id(value: Any) -> str | None:
    """A short identifier, or ``unknown`` (never a secret-looking or free-text value)."""
    if value is None:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value) or mask_secrets(value) != value:
        return "unknown"
    return value


def audit_principal(value: Any) -> str | None:
    """Keep the principal kind (``local``/``remote``/``system``); sanitize the name."""
    if isinstance(value, str):
        kind, sep, name = value.partition(":")
        if sep and kind in ("local", "remote", "system"):
            return f"{kind}:{audit_id(name) or 'unknown'}"
    return audit_id(value)


def audit_digest(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and _DIGEST.fullmatch(value) else "invalid"


@dataclass(frozen=True)
class MirrorResult:
    ok: bool
    mirrored: int
    error: str | None = None


class AuditChain:
    """Write ``audit.*`` events onto a chained store (raises if the store is unhealthy)."""

    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def _emit(self, family: str, kind: str, attributes: dict[str, Any], **columns) -> str:
        event = ObservabilityEvent(
            event_type=f"audit.{family}.{kind}",
            attributes={k: v for k, v in attributes.items() if v is not None},
            **columns,
        )
        self.store.emit(event)
        return event.event_id

    def record_approval(
        self,
        kind: str,
        *,
        request_id: str,
        tool_name: str | None = None,
        args_hash: str | None = None,
        action: str | None = None,
        principal: str | None = None,
        source_seq: int | None = None,
        source_hash: str | None = None,
        event_id: str | None = None,
    ) -> str:
        if kind not in APPROVAL_KINDS:
            raise ValueError(f"unknown approval event kind {kind!r}")
        attributes = {
            "request_id": audit_id(request_id),
            "args_hash": audit_digest(args_hash),
            "action": action if action in _ACTIONS else None if action is None else "unknown",
            "principal": audit_principal(principal),
            "source_seq": source_seq if type(source_seq) is int else None,
            "source_hash": audit_digest(source_hash),
        }
        extra = {} if event_id is None else {"event_id": event_id}
        return self._emit("approval", kind, attributes, tool_name=audit_id(tool_name), **extra)

    def record_tool_execution(
        self,
        phase: str,
        *,
        tool_name: str,
        tool_use_id: str | None,
        args_hash: str | None,
        request_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        if phase not in TOOL_PHASES:
            raise ValueError(f"unknown tool execution phase {phase!r}")
        attributes = {"args_hash": audit_digest(args_hash), "request_id": audit_id(request_id)}
        return self._emit(
            "tool",
            phase,
            attributes,
            tool_name=audit_id(tool_name),
            tool_use_id=audit_id(tool_use_id),
            turn_id=audit_id(turn_id),
        )

    def record_procedural_write(
        self,
        outcome: str,
        *,
        entry_key: str | None,
        content_hash: str | None,
        source_turn: str | None,
        approved_by: str | None = None,
        request_id: str | None = None,
        code: str | None = None,
    ) -> str:
        if outcome not in PROCEDURAL_OUTCOMES | PROCEDURAL_REVIEW_OUTCOMES:
            raise ValueError(f"unknown procedural write outcome {outcome!r}")
        attributes = {
            "entry_key": audit_id(entry_key),
            "content_hash": audit_digest(content_hash),
            "source_turn": audit_id(source_turn),
            "approved_by": audit_principal(approved_by),
            "request_id": audit_id(request_id),
            # A fixed refusal code (``E_*``), never a message.
            "code": audit_id(code),
        }
        return self._emit("procedural", outcome, attributes, turn_id=audit_id(source_turn))

    def record_memory(
        self,
        outcome: str,
        *,
        layer: str | None = None,
        target: str | None = None,
        kind: str | None = None,
        job_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        rated_by: str | None = None,
        trust: str | None = None,
        capped: str | None = None,
        rating: float | None = None,
        reason_code: str | None = None,
        counts: dict[str, Any] | None = None,
    ) -> str:
        """One ``audit.memory.*`` row (ADR 0018 §2f): codes, ids, a keyed hash of the key, a
        rating rounded to 0.05 and integer counts — never text, a key in clear or a path."""
        if outcome not in MEMORY_OUTCOMES:
            raise ValueError(f"unknown memory outcome {outcome!r}")

        def one_of(value: Any, allowed: frozenset[str]) -> str | None:
            if value is None:
                return None
            return value if isinstance(value, str) and value in allowed else "unknown"

        capped_codes = None
        if capped is not None:
            parts = capped.split(",") if isinstance(capped, str) else []
            capped_codes = ",".join(parts) if parts and set(parts) <= _MEMORY_CAPPED else "unknown"
        value = None
        if rating is not None:
            if isinstance(rating, bool) or not isinstance(rating, (int, float)):
                value = None
            elif 0.0 <= float(rating) <= 1.0:
                value = round(round(float(rating) * 20) / 20, 2)
        clean_counts = None
        if counts is not None:
            clean_counts = {
                name: number
                for name, number in sorted(counts.items())
                if isinstance(name, str)
                and _MEMORY_COUNT.fullmatch(name)
                and type(number) is int
                and 0 <= number <= 10**12
            }
        attributes = {
            "layer": one_of(layer, _MEMORY_LAYERS),
            "target": audit_digest(target),
            "kind": one_of(kind, _MEMORY_KINDS),
            "job_id": audit_id(job_id),
            "run_id": audit_id(run_id),
            "rated_by": one_of(rated_by, _MEMORY_RATED_BY),
            "trust": one_of(trust, _MEMORY_TRUSTS),
            "capped": capped_codes,
            "rating": value,
            "reason_code": audit_id(reason_code),
            "counts": clean_counts,
        }
        return self._emit("memory", outcome, attributes, turn_id=audit_id(turn_id))

    def record_read_refusal(
        self,
        *,
        tool_name: str | None,
        target: str | None,
        reason_code: str | None,
    ) -> str:
        """One ``audit.read.denied`` row (#457): the tool, a keyed hash of the path
        (``path:<hex>``, ``file_tools.path_ref``) and the refusal code — never the path,
        a file name or content."""
        attributes = {
            "target": target
            if isinstance(target, str) and _READ_TARGET.fullmatch(target)
            else "invalid",
            "reason_code": reason_code if reason_code in READ_REFUSAL_CODES else "unknown",
        }
        return self._emit("read", "denied", attributes, tool_name=audit_id(tool_name))

    def record_write_refusal(
        self,
        *,
        tool_name: str | None,
        target: str | None,
        reason_code: str | None,
    ) -> str:
        """One ``audit.write.denied`` row (#536): the tool, a keyed hash of the path
        (``path:<hex>``, ``file_tools.path_ref``) and the refusal code — never the path,
        a file name, content or which floor entry matched."""
        attributes = {
            "target": target
            if isinstance(target, str) and _READ_TARGET.fullmatch(target)
            else "invalid",
            "reason_code": reason_code if reason_code in WRITE_REFUSAL_CODES else "unknown",
        }
        return self._emit("write", "denied", attributes, tool_name=audit_id(tool_name))

    def record_web_failure(
        self,
        *,
        tool_name: str | None,
        reason_code: str | None,
        http_status: int | None = None,
    ) -> str:
        """One ``audit.web.failed`` row (#584): the tool, the failure code and the HTTP
        status — never the URL, a host, a redirect target or an exception's text."""
        attributes = {
            "reason_code": reason_code if reason_code in WEB_FAILURE_CODES else "unknown",
            "http_status": http_status
            if type(http_status) is int and 100 <= http_status <= 599
            else None,
        }
        tool = tool_name if tool_name in _WEB_TOOLS else "unknown"
        return self._emit("web", "failed", attributes, tool_name=tool)

    def record_task_model(
        self,
        *,
        task: Any,
        provider: Any = None,
        model: Any = None,
        status: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        reason: Any = None,
        duration_ms: Any = None,
    ) -> str:
        """One ``audit.task_model.call`` row: the side task, its provider and model id, the
        tokens, the status and a reason code — never a prompt, a summary or a URL."""

        def code(value, pattern):
            return value if isinstance(value, str) and pattern.fullmatch(value) else None

        def count(value):
            return value if type(value) is int and value >= 0 else None

        attributes = {
            "task": code(task, _TASK_NAME) or "unknown",
            "provider": code(provider, _TASK_NAME),
            "model": code(model, _MODEL_REF),
            "status": status if status in _TASK_MODEL_STATUSES else "unknown",
            "input_tokens": count(input_tokens),
            "output_tokens": count(output_tokens),
            "reason": code(reason, _REASON),
            "duration_ms": count(duration_ms),
        }
        return self._emit("task_model", "call", attributes)

    def record_checks(
        self,
        outcome: str,
        *,
        target: str | None = None,
        kinds: Any = None,
        counts: Any = None,
        containment: str | None = None,
        reason_code: str | None = None,
        tree_changes: int | None = None,
        real_tree_changed: bool | None = None,
    ) -> str:
        """One ``audit.checks.*`` row (ADR 0019 §5): kinds, counts, the containment mode,
        a reason code and a keyed hash of the folder — never a test id, a message, output,
        a file name or a path."""
        from stiracore.tools.run_checks import REASON_CODES

        if outcome not in CHECKS_OUTCOMES:
            raise ValueError(f"unknown checks outcome {outcome!r}")
        clean_kinds = None
        if isinstance(kinds, list):
            clean_kinds = [k if k in _CHECKS_KINDS else "unknown" for k in kinds][:8]
        clean_counts = None
        if isinstance(counts, dict):
            clean_counts = {
                k: v
                for k, v in sorted(counts.items())
                if k in _CHECKS_COUNTS and type(v) is int and 0 <= v <= 10**9
            }
        attributes = {
            "target": (
                target if isinstance(target, str) and _CHECKS_TARGET.fullmatch(target) else None
            )
            if target is not None
            else None,
            "kinds": clean_kinds,
            "counts": clean_counts,
            "containment": (
                containment
                if containment in _CHECKS_CONTAINMENT
                else (None if containment is None else "unknown")
            ),
            "reason_code": (
                reason_code
                if reason_code in REASON_CODES
                else (None if reason_code is None else "unknown")
            ),
            "tree_changes": tree_changes
            if type(tree_changes) is int and tree_changes >= 0
            else None,
            "real_tree_changed": real_tree_changed if type(real_tree_changed) is bool else None,
        }
        if target is not None and attributes["target"] is None:
            attributes["target"] = "invalid"
        return self._emit("checks", outcome, attributes)

    def record_artifact(
        self,
        outcome: str,
        *,
        artifact_id: str,
        labels: Any,
        previous: Any,
        version: int | None,
        principal: str | None,
    ) -> str:
        """One ``audit.artifact.*`` row (ADR 0020 §5): the artifact's opaque id, the label
        codes before and after, the version it was set at and who — never a path or content."""
        if outcome not in ARTIFACT_OUTCOMES:
            raise ValueError(f"unknown artifact outcome {outcome!r}")

        def codes(value: Any) -> str | None:
            if not isinstance(value, list):
                return None
            return ",".join(v if v in _ARTIFACT_LABELS else "unknown" for v in value[:3])

        attributes = {
            "artifact_id": artifact_id
            if isinstance(artifact_id, str) and _ARTIFACT_ID.fullmatch(artifact_id)
            else "unknown",
            "labels": codes(labels),
            "previous": codes(previous),
            "version": version if type(version) is int and version >= 0 else None,
            "principal": audit_principal(principal),
        }
        return self._emit("artifact", outcome, attributes)

    def record_yolo(self, event_type: str, **fields: Any) -> str:
        """Write one ``audit.yolo.*`` row after the allow-list check (ADR 0012 §11).

        ``stiracore.yolo.audit_schema.build`` validates every field first; a row
        that breaks the contract raises ``AuditContractError`` and nothing is written.
        The event id is the row's deterministic ``op:<operation_id>:<phase>``, so the
        store's unique column refuses a second row for the same step.
        """
        from stiracore.yolo.audit_schema import build

        row = build(event_type, **fields)
        event = ObservabilityEvent(
            event_type=row["event_type"], event_id=row["event_id"], attributes=row["attributes"]
        )
        self.store.emit(event)
        return event.event_id

    # --- YOLO read side (ADR 0012 §6a (2), §11 latch judgement) -----------------

    _YOLO_HISTORY = ("audit.yolo.started", "audit.yolo.ended", "audit.yolo.unlatched")

    def yolo_receipt(self, event_id: str) -> dict[str, Any] | None:
        """Read back one row (seq, hash, type, attributes) after writing it."""
        return self.store.receipt(event_id)

    def yolo_healthy(self) -> bool:
        """The writer's sticky health flag (ADR 0013 §2b (B) step 1)."""
        return bool(self.store.healthy)

    def yolo_head_seq(self) -> int:
        seq = self.store.chain_status().get("seq")
        return seq if type(seq) is int else 0

    def yolo_history(self) -> list[tuple[str, dict[str, Any]]]:
        """Every ``started`` / ``ended`` / ``unlatched`` row (by type), after the whole
        chain verified. Raises ``RuntimeError`` when it does not (the caller latches)."""
        ok, _error = self.store.verify_chain()
        if not ok:
            raise RuntimeError("event chain did not verify")
        rows: list[tuple[str, dict[str, Any]]] = []
        for event_type in self._YOLO_HISTORY:
            since = None
            while True:
                batch = self.store.get_events(since_id=since, limit=500, event_type=event_type)
                for event in batch:
                    attributes = event.attributes if isinstance(event.attributes, dict) else {}
                    rows.append((event_type, attributes))
                if len(batch) < 500:
                    break
                since = batch[-1].event_id
        return rows

    # --- mirroring the approval store's own chain ------------------------------

    def _mirrored_sources(self) -> dict[int, str]:
        """``source_seq -> source_hash`` of every approval event mirrored so far."""
        found: dict[int, str] = {}
        for kind in APPROVAL_KINDS:
            since = None
            while True:
                batch = self.store.get_events(
                    since_id=since, limit=500, event_type=f"audit.approval.{kind}"
                )
                for event in batch:
                    attributes = event.attributes or {}
                    if type(attributes.get("source_seq")) is int:
                        found[attributes["source_seq"]] = attributes.get("source_hash")
                if len(batch) < 500:
                    break
                since = batch[-1].event_id
        return found

    def _source_problem(self, kind: str, reason: str) -> MirrorResult:
        self._emit("approval", kind, {"reason": reason})
        return MirrorResult(False, 0, reason)

    def mirror_approval_store(self, approval_store: Any) -> MirrorResult:
        """Copy new approval-chain events (ids, hashes, action, principal only).

        Nothing is mirrored from a store that does not verify. Previously mirrored
        events must still be in the source with the same hash.
        """
        ok, _error = approval_store.verify_chain()
        if not ok:
            return self._source_problem("source_unverified", "approval store did not verify")
        # audit_events is newest first; ask for more than there are so the read is
        # complete even if events were appended since status() was read.
        limit = int(approval_store.status().get("events") or 0) + 1000
        events = approval_store.audit_events(limit=limit)
        complete = len(events) < limit
        source = {event["seq"]: event for event in events}
        oldest = min(source, default=None)
        mirrored = self._mirrored_sources()
        for seq, digest in sorted(mirrored.items()):
            current = source.get(seq)
            if current is None and not complete and (oldest is None or seq < oldest):
                continue  # outside the window that was read; checked next time
            if current is None or current.get("hash") != digest:
                return self._source_problem(
                    "source_diverged", f"approval event {seq} changed after it was mirrored"
                )
        count = 0
        for seq in sorted(source):
            if seq in mirrored:
                continue
            event = source[seq]
            body = event.get("body") if isinstance(event.get("body"), dict) else {}
            self.record_approval(
                event["kind"],
                request_id=event.get("request_id"),
                tool_name=body.get("tool_name"),
                args_hash=body.get("args_hash"),
                action=body.get("action"),
                principal=body.get("approver") or body.get("by"),
                source_seq=seq,
                source_hash=event.get("hash"),
                event_id=f"approval:{seq}:{event.get('hash')}",
            )
            count += 1
        return MirrorResult(True, count)
