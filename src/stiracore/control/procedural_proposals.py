"""Agent proposals and the operator's review of procedural rules (#463).

The procedural layer becomes system-prompt rules, so only a person may approve one
(#144). This module adds the two missing entrances around the existing
:class:`~stiracore.control.procedural_review.ProceduralReview`:

* :meth:`ProcedureProposals.propose` — what the agent's ``propose_procedure`` tool
  calls. It writes ``procedural/staging/<key>`` **only** (through ``ProceduralReview.
  propose``); ``source_turn`` is the harness's turn id (the tool has no such argument),
  and the proposer fields (``proposed_via``, ``origin``) are set here. Refused (nothing
  written): an invalid key, an empty or oversized body/rationale, anything
  ``mask_secrets`` would change, more than :data:`LIMITS` allow (per turn, per hour,
  pending in total). A key that already holds an approved rule is staged as a
  **revision** (``revises_sha256`` = the rule's current hash).
* :meth:`overview` / :meth:`approve` / :meth:`reject` — the review screen (operator
  side, ``observability/procedural_api.py``). The approver is the OS account of the
  server process, ``local:<account>``, decided exactly like the CLI
  (``procedural_cli.operator_principal``); a request cannot name another one. Both
  are bound to the SHA-256 of the proposal the person saw (``E_STALE_PROPOSAL``).
  **Approve needs a NORMAL breaker** (operator decision 2026-10-20, like the other
  loosenings): with ``breaker_level`` set (the server sets it), any other level — or a
  level that cannot be read — refuses the approval before anything is read or written
  (``E_BREAKER_NOT_NORMAL`` / ``E_BREAKER_UNAVAILABLE``, one ``refused`` audit row with
  the key and the code). Reject only narrows and is allowed at every level. The CLI has
  no live breaker (it is another process; the level lives in the server's engine) and
  builds no :class:`ProcedureProposals`: its approval applies at the next start.
  A rejection keeps its reason in the review log (``procedural-reviews.jsonl`` in the
  data directory — outside every agent write root).

Audit: each proposal, refusal, approval and rejection is one ``audit.procedural.*`` row
(``AuditChain.record_procedural_write``): key, ``source_turn``, approver, the body's
SHA-256 and a fixed code — never the body, the rationale or a reason.

Nothing here calls a model. No agent tool can approve or reject: only the review routes
(CSRF, ``confirm``, refused while a turn runs) and the CLI call :meth:`approve`.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stiracore.memory_origin import ORIGIN_MODEL
from stiracore.observability.masking import mask_secrets
from stiracore.procedural_provenance import (
    STAGING_PREFIX,
    is_staged_key,
    provenance_problem,
    valid_field,
)
from stiracore.tools.memory_store import MemoryEntry, MemoryLayer, MemoryStore

from .procedural_review import ApprovalResult, CleanupError, ProceduralReview, content_sha256

_LOG = logging.getLogger(__name__)
_LAYER = MemoryLayer.PROCEDURAL

PROPOSE_TOOL_NAME = "propose_procedure"
# Lowercase, one path component: a proposal key never names a sub-directory.
KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_REASON_CHARS = 500
REVIEW_LOG_NAME = "procedural-reviews.jsonl"
REVIEW_LOG_KEPT = 200
DIFF_MAX_LINES = 400
# The only breaker level at which a proposal can be approved (operator decision 2026-10-20).
APPROVE_LEVEL = "normal"
_HOUR = 3600.0


@dataclass(frozen=True)
class ProposalLimits:
    """Bounds on what the agent can stage (#463 requirement 1)."""

    max_body_chars: int = 4000
    max_rationale_chars: int = 1000
    max_pending: int = 20
    max_per_turn: int = 2
    max_per_hour: int = 10


LIMITS = ProposalLimits()


class ProposalRefused(ValueError):
    """A refused proposal or review; ``code`` is a fixed ``E_*`` code (no value)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _code_of(error: BaseException) -> str:
    text = error.args[0] if error.args and isinstance(error.args[0], str) else ""
    head = text.split(":", 1)[0].split(" ", 1)[0]
    return head if head.startswith("E_") else type(error).__name__


def _target(key: Any) -> str:
    """The rule key of a review request (``staging/`` optional)."""
    if not isinstance(key, str):
        raise ProposalRefused("E_INVALID_KEY", "the key must be a string")
    target = key[len(STAGING_PREFIX) :] if is_staged_key(key) else key
    if not KEY_PATTERN.fullmatch(target):
        raise ProposalRefused("E_INVALID_KEY", "use a-z, 0-9, '_' or '-' (at most 64)")
    return target


def breaker_name(level: Any) -> str | None:
    """``BreakLevel.NORMAL`` / ``"normal"`` / ``"NORMAL"`` -> ``"normal"``; else None."""
    value = getattr(level, "value", level)
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def unified_diff(before: str, after: str) -> list[dict[str, str]]:
    """``[{"op": " "|"-"|"+"|"@", "text": line}]`` from ``before`` to ``after``."""
    lines: list[dict[str, str]] = []
    for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), "rule", "proposal", lineterm="", n=3
    ):
        if line.startswith(("---", "+++")):
            continue
        op = "@" if line.startswith("@@") else line[:1]
        lines.append({"op": op, "text": line if op == "@" else line[1:]})
        if len(lines) >= DIFF_MAX_LINES:
            lines.append({"op": "@", "text": "…"})
            break
    return lines


def _prompt_limit() -> int | None:
    """How many approved rules the prompt reads (the assembler's default, newest first)."""
    import inspect

    from stiracore.agent.context_assembler import ContextAssembler

    value = inspect.signature(ContextAssembler).parameters["procedural_limit"].default
    return value if isinstance(value, int) else None


class ProcedureProposals:
    """The agent's staging entrance and the review screen's operations (host side)."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        review_log: Path,
        audit: Callable[..., Any] | None = None,
        approver: Callable[[], str] | None = None,
        limits: ProposalLimits = LIMITS,
        clock: Callable[[], float] = time.time,
        reflect: Callable[[], Any] | None = None,
        breaker_level: Callable[[], Any] | None = None,
    ) -> None:
        if approver is None:
            from .procedural_cli import operator_principal as approver
        self.review = ProceduralReview(store)
        self.store = store
        self.review_log = Path(review_log)
        # ``audit(outcome, **fields)`` — ``AuditChain.record_procedural_write``; may be
        # set after construction (the event store opens later at start-up).
        self.audit = audit
        self.approver = approver
        self.limits = limits
        self.clock = clock
        # Called after an approval, under the caller's turn lock: re-reads the static
        # prompt (SOUL, anchors, Procedural Rules) so the rule applies from the next turn.
        self.reflect = reflect
        # The live breaker level (a ``BreakLevel`` or its name); approve needs NORMAL.
        # ``None``: this process has no breaker (unit tests); the review routes refuse
        # to approve then (``E_BREAKER_UNAVAILABLE``), so the panel is always gated.
        self.breaker_level = breaker_level
        self.started_at = _now()
        self._lock = threading.RLock()
        self._per_turn: dict[str, int] = {}
        self._recent: deque[float] = deque()

    # --- audit ---------------------------------------------------------------------

    def _audit(self, outcome: str, **fields: Any) -> bool:
        if self.audit is None:
            return False
        try:
            self.audit(outcome, **fields)
        except Exception:  # noqa: BLE001 - evidence only; never breaks the operation
            _LOG.warning("procedural audit row not written (%s)", outcome)
            return False
        return True

    # --- the agent's entrance ------------------------------------------------------

    def _refuse(self, code: str, message: str, *, key: Any, source_turn: Any) -> None:
        self._audit(
            "refused",
            entry_key=key if isinstance(key, str) else None,
            content_hash=None,
            source_turn=source_turn if isinstance(source_turn, str) else None,
            code=code,
        )
        raise ProposalRefused(code, message)

    def _check_rate(self, source_turn: str) -> str | None:
        now = self.clock()
        while self._recent and now - self._recent[0] >= _HOUR:
            self._recent.popleft()
        if self._per_turn.get(source_turn, 0) >= self.limits.max_per_turn:
            return "E_RATE_TURN"
        if len(self._recent) >= self.limits.max_per_hour:
            return "E_RATE_HOUR"
        if len(self.review.list_staged()) >= self.limits.max_pending:
            return "E_TOO_MANY_PENDING"
        return None

    def _count(self, source_turn: str) -> None:
        self._recent.append(self.clock())
        if source_turn not in self._per_turn and len(self._per_turn) >= 64:
            self._per_turn.pop(next(iter(self._per_turn)))
        self._per_turn[source_turn] = self._per_turn.get(source_turn, 0) + 1

    def propose(self, key: Any, body: Any, rationale: Any, *, source_turn: Any) -> dict:
        """Stage the agent's proposal. ``source_turn`` comes from the harness."""
        limits = self.limits
        if not valid_field(source_turn):
            self._refuse("E_NO_TURN", "no running turn", key=key, source_turn=None)
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            self._refuse(
                "E_INVALID_KEY",
                "use a-z, 0-9, '_' or '-' (at most 64)",
                key=None,
                source_turn=source_turn,
            )
        if not isinstance(body, str) or not body.strip():
            self._refuse("E_EMPTY_CONTENT", "the body is empty", key=key, source_turn=source_turn)
        if len(body) > limits.max_body_chars:
            self._refuse(
                "E_BODY_TOO_LARGE",
                f"at most {limits.max_body_chars} characters",
                key=key,
                source_turn=source_turn,
            )
        if not isinstance(rationale, str):
            rationale = ""
        if len(rationale) > limits.max_rationale_chars:
            self._refuse(
                "E_RATIONALE_TOO_LARGE",
                f"at most {limits.max_rationale_chars} characters",
                key=key,
                source_turn=source_turn,
            )
        for value in (key, body, rationale):
            if mask_secrets(value) != value:
                self._refuse(
                    "E_SECRET_IN_CONTENT",
                    "*** looks like it contains a secret; nothing staged",
                    key=key,
                    source_turn=source_turn,
                )
        with self._lock:
            code = self._check_rate(source_turn)
            if code is not None:
                self._refuse(
                    code, "proposal limit reached; try later", key=key, source_turn=source_turn
                )
            existing = self.store.read(_LAYER, key)
            revises = None
            if existing is not None and provenance_problem(existing.metadata) is None:
                revises = content_sha256(existing.content)
            metadata = {
                "proposed_via": PROPOSE_TOOL_NAME,
                "origin": ORIGIN_MODEL,
                "rationale": rationale,
            }
            try:
                entry = self.review.propose(
                    key, body, source_turn=source_turn, metadata=metadata, revises=revises
                )
            except ValueError as error:
                self._refuse(_code_of(error), "not staged", key=key, source_turn=source_turn)
            self._count(source_turn)
        digest = content_sha256(entry.content)
        self._audit("proposed", entry_key=key, content_hash=digest, source_turn=source_turn)
        return {
            "staged": entry.key,
            "sha256": digest,
            "revision": revises is not None,
            "source_turn": source_turn,
        }

    # --- the review screen ---------------------------------------------------------

    def _proposal_view(self, entry: MemoryEntry) -> dict:
        target = entry.key[len(STAGING_PREFIX) :]
        metadata = entry.metadata
        existing = self.store.read(_LAYER, target)
        revises = metadata.get("revises_sha256")
        view: dict[str, Any] = {
            "key": target,
            "body": entry.content,
            "sha256": content_sha256(entry.content),
            "length": len(entry.content),
            "rationale": metadata.get("rationale") if isinstance(metadata, dict) else None,
            "source_turn": metadata.get("source_turn"),
            "proposed_at": metadata.get("proposed_at"),
            "proposed_via": metadata.get("proposed_via") or "operator",
            "legacy": metadata.get("legacy") is True,
            "revision": isinstance(revises, str),
            "existing": None,
            "diff": [],
            "approvable": True,
            "problem": None,
        }
        if existing is not None:
            current = content_sha256(existing.content)
            view["existing"] = {
                "sha256": current,
                "approved_by": existing.metadata.get("approved_by"),
                "approved_at": existing.metadata.get("approved_at"),
                "approved": provenance_problem(existing.metadata) is None,
            }
            view["diff"] = unified_diff(existing.content, entry.content)
            if not isinstance(revises, str):
                view.update(approvable=False, problem="E_TARGET_EXISTS")
            elif revises != current or provenance_problem(existing.metadata) is not None:
                view.update(approvable=False, problem="E_REVISION_STALE")
        if not valid_field(view["source_turn"]):
            view.update(approvable=False, problem="E_SOURCE_TURN_REQUIRED")
        return view

    def _rules_view(self) -> list[dict]:
        rules = []
        for entry in self.review._all():
            if is_staged_key(entry.key):
                continue
            metadata = entry.metadata
            approved = provenance_problem(metadata) is None
            approved_at = metadata.get("approved_at") if approved else None
            rules.append(
                {
                    "key": entry.key,
                    "sha256": content_sha256(entry.content),
                    "length": len(entry.content),
                    "approved": approved,
                    "approved_by": metadata.get("approved_by") if approved else None,
                    "approved_at": str(approved_at) if approved_at else None,
                    "source_turn": metadata.get("source_turn") if approved else None,
                }
            )
        return sorted(rules, key=lambda rule: rule["key"])

    def overview(self) -> dict:
        """Everything the review screen shows; proposal bodies included (operator view)."""
        with self._lock:
            proposals = [self._proposal_view(e) for e in self.review.list_staged()]
            return {
                "proposals": proposals,
                "rules": self._rules_view(),
                "reviews": self.review_history(),
                "limits": asdict(self.limits),
                "prompt_limit": _prompt_limit(),
                "effective": "next_turn" if self.reflect is not None else "next_start",
                "breaker": self.breaker_view(),
            }

    def _staged_for(self, key: Any, sha256: Any) -> tuple[str, MemoryEntry]:
        target = _target(key)
        try:
            staged = self.review.staged(target)
        except KeyError:
            raise ProposalRefused("E_NO_PROPOSAL", "no such proposal") from None
        if not isinstance(sha256, str) or content_sha256(staged.content) != sha256:
            raise ProposalRefused("E_STALE_PROPOSAL", "the proposal changed; reload it")
        return target, staged

    def _principal(self) -> str:
        try:
            return self.approver()
        except ValueError as error:
            raise ProposalRefused("E_APPROVER_UNAVAILABLE", str(error)) from None

    def breaker_view(self) -> dict:
        """``{"level": name | None, "approve_allowed": bool}`` for the review screen."""
        if self.breaker_level is None:
            return {"level": None, "approve_allowed": False}
        try:
            level = breaker_name(self.breaker_level())
        except Exception:  # noqa: BLE001 - an unreadable level fails closed
            return {"level": None, "approve_allowed": False}
        return {"level": level, "approve_allowed": level == APPROVE_LEVEL}

    def _check_breaker(self, target: str) -> None:
        """Approve only at a NORMAL breaker (a loosening); refused rows carry the code."""
        if self.breaker_level is None:
            return
        try:
            level = breaker_name(self.breaker_level())
        except Exception:  # noqa: BLE001 - an unreadable level fails closed
            level = None
        if level == APPROVE_LEVEL:
            return
        code = "E_BREAKER_UNAVAILABLE" if level is None else "E_BREAKER_NOT_NORMAL"
        self._refuse(code, "approving needs the breaker at NORMAL", key=target, source_turn=None)

    def approve(self, key: Any, *, sha256: Any, replace: bool = False) -> dict:
        """Approve the proposal the person saw (``sha256``) as ``local:<account>``.

        Refused unless the breaker is NORMAL (when ``breaker_level`` is set), checked
        first, so a refused approval neither writes nor refreshes the prompt.
        """
        with self._lock:
            self._check_breaker(_target(key))
            approver = self._principal()  # before anything is read or written
            target, _staged = self._staged_for(key, sha256)
            previous = self.store.read(_LAYER, target)
            leftover = None
            try:
                result = self.review.approve(target, approved_by=approver, replace=replace)
            except CleanupError as error:
                # Approved; only the proposal file stayed (approving again removes it).
                result = ApprovalResult(error.entry, written=True)
                leftover = error.leftover
            except (KeyError, ValueError) as error:
                raise ProposalRefused(_code_of(error), "not approved") from None
            entry = result.entry
            record = {
                "action": "approved",
                "key": target,
                "sha256": content_sha256(entry.content),
                "source_turn": entry.metadata.get("source_turn"),
                "by": approver,
                "at": str(entry.metadata.get("approved_at")),
            }
            if result.written and previous is not None and replace:
                record["previous_sha256"] = content_sha256(previous.content)
                # Kept so a replaced rule can be restored by hand (it was approved text).
                record["previous_body"] = previous.content
            logged = self._append_review(record)
            reflected = False
            if result.written and self.reflect is not None:
                try:
                    self.reflect()
                    reflected = True
                except Exception:  # noqa: BLE001 - the rule is approved; restart applies it
                    _LOG.warning("procedural rule approved; prompt not refreshed (restart)")
        audited = self._audit(
            "approved",
            entry_key=target,
            content_hash=record["sha256"],
            source_turn=record["source_turn"],
            approved_by=approver,
        )
        return {
            "approved": target,
            "written": result.written,
            "sha256": record["sha256"],
            "approved_by": approver,
            "approved_at": record["at"],
            "source_turn": record["source_turn"],
            "effective": "next_turn" if reflected else "next_start",
            "review_log": "ok" if logged else "failed",
            "audit": "ok" if audited else "failed",
            "leftover": leftover,
        }

    def reject(self, key: Any, *, sha256: Any, reason: Any = "") -> dict:
        """Delete the proposal the person saw and keep the (masked) reason."""
        if reason is None:
            reason = ""
        if not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS:
            raise ProposalRefused("E_REASON_INVALID", f"at most {MAX_REASON_CHARS} characters")
        with self._lock:
            reviewer = self._principal()
            target, staged = self._staged_for(key, sha256)
            record = {
                "action": "rejected",
                "key": target,
                "sha256": sha256,
                "source_turn": staged.metadata.get("source_turn"),
                "by": reviewer,
                "at": _now(),
                "reason": mask_secrets(reason.strip()),
            }
            # The reason is kept before the proposal goes (a failed write keeps both).
            if not self._append_review(record):
                raise ProposalRefused("E_REVIEW_LOG", "the review log could not be written")
            try:
                self.review.reject(target)
            except (KeyError, ValueError, OSError) as error:
                raise ProposalRefused(_code_of(error), "not rejected") from None
        audited = self._audit(
            "rejected",
            entry_key=target,
            content_hash=sha256,
            source_turn=record["source_turn"],
            approved_by=reviewer,
        )
        return {
            "rejected": target,
            "by": reviewer,
            "at": record["at"],
            "audit": "ok" if audited else "failed",
        }

    # --- the review log --------------------------------------------------------------

    def review_history(self) -> list[dict]:
        """The newest reviews first (without a replaced rule's body)."""
        try:
            raw = self.review_log.read_bytes().splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        records = []
        for data in raw:
            try:
                # Strict per line: a line with a damaged byte is skipped (also one that
                # would still be valid JSON with a replacement character in it).
                record = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(record, dict):
                record.pop("previous_body", None)
                records.append(record)
        return list(reversed(records[-50:]))

    def _append_review(self, record: dict) -> bool:
        """Append one line, keeping the last :data:`REVIEW_LOG_KEPT` (atomic replace)."""
        try:
            try:
                # surrogateescape: a damaged byte is kept as it is (not replaced, and
                # the append does not fail after an approval was already written).
                lines = self.review_log.read_text(
                    encoding="utf-8", errors="surrogateescape"
                ).splitlines()
            except FileNotFoundError:
                lines = []
            lines = [*lines[-(REVIEW_LOG_KEPT - 1) :], json.dumps(record, ensure_ascii=False)]
            temporary = self.review_log.with_name(self.review_log.name + ".tmp")
            temporary.write_text(
                "\n".join(lines) + "\n", encoding="utf-8", errors="surrogateescape"
            )
            os.replace(temporary, self.review_log)
        except (OSError, UnicodeError):
            _LOG.warning("procedural review log not written")
            return False
        return True


__all__ = [
    "KEY_PATTERN",
    "LIMITS",
    "MAX_REASON_CHARS",
    "PROPOSE_TOOL_NAME",
    "REVIEW_LOG_NAME",
    "ProcedureProposals",
    "ProposalLimits",
    "ProposalRefused",
    "unified_diff",
]
