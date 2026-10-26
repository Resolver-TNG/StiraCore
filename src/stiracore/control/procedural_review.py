"""Proposal -> human approval -> write, for the procedural layer (#144).

Procedural entries become system-prompt rules, so they are never written straight
from a turn. A proposal waits under ``procedural/staging/<key>`` (inside the
protected procedural subtree, which the agent's file tools cannot write); only
:meth:`ProceduralReview.approve`, run by the operator's CLI (``procedural_cli``) or the
review screen (``procedural_proposals`` / ``observability/procedural_api``, #463), moves it to ``procedural/<key>`` and adds the provenance
the context assembler requires (``source_turn`` / ``approved_by`` /
``approved_at`` / ``review``). The approver is decided by the host (the CLI uses
the OS account of the process), never taken from the proposal: a proposal that carries a reserved
approval field is refused, at proposal time and again at approval time.

No agent tool approves or rejects: the agent's ``propose_procedure`` (#463) reaches
:meth:`ProceduralReview.propose` only. Works on the files through ``MemoryStore``
(key validation, no symbolic links, atomic writes) and calls no model.

**Revisions (#463).** A host caller may stage a proposal for a key that already holds an
approved rule by passing ``revises=<SHA-256 of that rule>`` (the agent's
``propose_procedure`` does, through ``control.procedural_proposals``). Approving it
replaces the rule only with ``replace=True`` (the review screen's explicit choice) and only
while the rule is still the one the proposal was made against; the CLI never passes
``replace`` and keeps refusing an existing target (``E_TARGET_EXISTS``).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from stiracore.observability.masking import mask_secrets
from stiracore.procedural_provenance import (
    RESERVED_APPROVAL_KEYS,
    RESERVED_PROPOSAL_KEYS,
    REVIEW_APPROVED,
    STAGING_PREFIX,
    is_staged_key,
    provenance_problem,
    valid_field,
)
from stiracore.tools.memory_store import MemoryEntry, MemoryLayer, MemoryStore

LEGACY_SOURCE_TURN = "legacy-pre-144"
_LAYER = MemoryLayer.PROCEDURAL
# Fields only this pipeline writes. Code before #144 wrote none of them, so an entry
# that has one is not a pre-#144 entry and ``stage_legacy`` does not normalize it.
_PIPELINE_KEYS = RESERVED_PROPOSAL_KEYS | {"proposed_at", "legacy"}
E_STAGING_CLEANUP = "E_STAGING_CLEANUP"


@dataclass(frozen=True)
class ApprovalResult:
    """``written``: this call created the rule (False: it existed, only staging was cleaned)."""

    entry: MemoryEntry
    written: bool


class CleanupError(Exception):
    """The rule (or legacy proposal) was written, but the source entry was not removed.

    Not a refusal: ``entry`` exists. Repeating the same command removes the leftover.
    """

    def __init__(self, entry: MemoryEntry, leftover: str, cause: BaseException) -> None:
        super().__init__(
            f"{E_STAGING_CLEANUP} ({type(cause).__name__}): {leftover} was not removed"
        )
        self.entry = entry
        self.leftover = leftover
        self.code = E_STAGING_CLEANUP


def _refuse_secrets(content: str, source_turn: Any, metadata: dict[str, Any]) -> None:
    """Refuse text that ``mask_secrets`` would change (as for scheduled goals, ADR 0011).

    The rule would be put into every system prompt; the message names no value.
    """
    for value in (content, source_turn, metadata):
        if mask_secrets(value) != value:
            raise ValueError("E_SECRET_IN_CONTENT: *** looks like it contains a secret; refused")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def content_sha256(content: str) -> str:
    """The SHA-256 of an entry's content (what the CLI's ``list`` prints)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _target_key(key: str) -> str:
    """The final key of a proposal (``staging/`` prefix optional)."""
    target = key[len(STAGING_PREFIX) :] if is_staged_key(key) else key
    if not isinstance(target, str) or not target or is_staged_key(target):
        raise ValueError("E_INVALID_KEY: a procedural key must not be under staging/")
    MemoryStore._validate_key(target)
    return target


class ProceduralReview:
    """Propose, approve and reject procedural entries (host side only)."""

    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    # --- reading -----------------------------------------------------------------

    def _all(self) -> list[MemoryEntry]:
        return self.memory_store.list_entries(_LAYER, limit=100_000)

    def list_staged(self) -> list[MemoryEntry]:
        return sorted((e for e in self._all() if is_staged_key(e.key)), key=lambda e: e.key)

    def unapproved(self) -> list[MemoryEntry]:
        """Entries outside staging that the prompt does not use (no valid provenance)."""
        return sorted(
            (
                entry
                for entry in self._all()
                if not is_staged_key(entry.key) and provenance_problem(entry.metadata)
            ),
            key=lambda e: e.key,
        )

    def staged(self, key: str) -> MemoryEntry:
        entry = self.memory_store.read(_LAYER, STAGING_PREFIX + _target_key(key))
        if entry is None:
            raise KeyError(f"No staged procedural entry: {key}")
        return entry

    # --- writing -----------------------------------------------------------------

    def _remove(self, key: str) -> None:
        store = self.memory_store
        path = store._path(_LAYER, key)
        store._require_writable(path)
        path.unlink(missing_ok=True)
        with store._connection() as connection:
            connection.execute(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?", (_LAYER.value, key)
            )

    def _require_free(self, target: str) -> None:
        if self.memory_store.read(_LAYER, STAGING_PREFIX + target) is not None:
            raise ValueError(f"E_PROPOSAL_EXISTS: staging/{target} is already proposed")
        if self.memory_store.read(_LAYER, target) is not None:
            raise ValueError(f"E_TARGET_EXISTS: {target} already exists; not overwritten")

    def propose(
        self,
        key: str,
        content: str,
        *,
        source_turn: str,
        metadata: dict[str, Any] | None = None,
        revises: str | None = None,
    ) -> MemoryEntry:
        """Stage a proposal. Nothing reaches the prompt until :meth:`approve`.

        ``revises``: the SHA-256 of the approved rule at ``key`` this proposal replaces
        (host callers only, #463). Without it an existing target is refused.
        """
        target = _target_key(key)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("E_EMPTY_CONTENT: a procedural entry needs content")
        if not valid_field(source_turn):
            raise ValueError("E_SOURCE_TURN_REQUIRED: source_turn must be a single-line id")
        metadata = dict(metadata or {})
        if any(not isinstance(name, str) for name in metadata):
            raise ValueError("metadata must be a string-keyed mapping.")
        reserved = sorted(RESERVED_PROPOSAL_KEYS.intersection(metadata))
        if reserved:
            raise ValueError(f"E_RESERVED_KEY: a proposal cannot set {', '.join(reserved)}")
        _refuse_secrets(content, source_turn, metadata)
        extra: dict[str, Any] = {}
        if revises is None:
            self._require_free(target)
        else:
            self._require_revisable(target, revises)
            extra["revises_sha256"] = revises
        try:
            return self.memory_store.write(
                _LAYER,
                STAGING_PREFIX + target,
                content,
                {**metadata, "source_turn": source_turn, "proposed_at": _now(), **extra},
                exclusive=True,
            )
        except FileExistsError:
            raise ValueError(f"E_PROPOSAL_EXISTS: staging/{target} is already proposed") from None

    def _require_revisable(self, target: str, revises: str) -> None:
        """A revision needs an approved rule at ``target`` whose content hash is ``revises``."""
        if self.memory_store.read(_LAYER, STAGING_PREFIX + target) is not None:
            raise ValueError(f"E_PROPOSAL_EXISTS: staging/{target} is already proposed")
        existing = self.memory_store.read(_LAYER, target)
        if existing is None or provenance_problem(existing.metadata) is not None:
            raise ValueError(f"E_NOT_REVISABLE: {target} is not an approved rule")
        if content_sha256(existing.content) != revises:
            raise ValueError(f"E_REVISION_STALE: {target} changed; not proposed")

    @staticmethod
    def _same_approval(existing: MemoryEntry, staged: MemoryEntry) -> bool:
        """``existing`` is the approval of ``staged`` (an earlier run that left staging)."""
        return (
            provenance_problem(existing.metadata) is None
            and existing.content == staged.content
            and existing.metadata.get("source_turn") == staged.metadata.get("source_turn")
        )

    def _cleanup(self, entry: MemoryEntry, leftover: str) -> None:
        try:
            self._remove(leftover)
        except (OSError, sqlite3.Error, ValueError) as error:
            raise CleanupError(entry, leftover, error) from None

    def approve(self, key: str, *, approved_by: str, replace: bool = False) -> ApprovalResult:
        """Create ``procedural/<key>`` from a proposal with host-decided provenance.

        The rule is created exclusively (never overwrites, also against a
        concurrent approval). If the proposal cannot be removed afterwards,
        :class:`CleanupError` reports the approved entry; approving again removes
        the leftover proposal (``written=False``).

        ``replace`` (#463, the review screen only): a proposal staged with ``revises``
        replaces the approved rule it was made against — refused (``E_REVISION_STALE``)
        once that rule changed. Without ``replace`` an existing rule is never touched.
        """
        if not valid_field(approved_by):
            raise ValueError("E_APPROVER_REQUIRED: approved_by must be a single-line id")
        target = _target_key(key)
        staged_key = STAGING_PREFIX + target
        staged = self.staged(target)
        forged = sorted(RESERVED_APPROVAL_KEYS.intersection(staged.metadata))
        if forged:
            # Only the approval writes these; a proposal carrying one was not made by
            # propose() (or was edited afterwards). Refused: reject it and re-propose.
            raise ValueError(
                f"E_FORGED_RESERVED_KEY: *** proposal already sets {', '.join(forged)}"
            )
        source_turn = staged.metadata.get("source_turn")
        if not valid_field(source_turn):
            raise ValueError("E_SOURCE_TURN_REQUIRED: the proposal has no valid source_turn")
        if not staged.content.strip():
            raise ValueError("E_EMPTY_CONTENT: a procedural entry needs content")
        _refuse_secrets(staged.content, source_turn, staged.metadata)
        existing = self.memory_store.read(_LAYER, target)
        revises = staged.metadata.get("revises_sha256")
        revision = replace and existing is not None and revises is not None
        if existing is not None and not revision:
            if self._same_approval(existing, staged):
                self._cleanup(existing, staged_key)
                return ApprovalResult(existing, written=False)
            raise ValueError(f"E_TARGET_EXISTS: {target} already exists; not overwritten")
        if revision and self._same_approval(existing, staged):
            # An earlier replace wrote it and could not remove the proposal.
            self._cleanup(existing, staged_key)
            return ApprovalResult(existing, written=False)
        if revision and (
            provenance_problem(existing.metadata) is not None
            or content_sha256(existing.content) != revises
        ):
            raise ValueError(f"E_REVISION_STALE: {target} changed since the proposal")
        metadata = {
            **{k: v for k, v in staged.metadata.items() if k != "revises_sha256"},
            "source_turn": source_turn,
            "approved_by": approved_by,
            "approved_at": _now(),
            "review": REVIEW_APPROVED,
        }
        if revision:
            metadata["previous_sha256"] = revises
        try:
            approved = self.memory_store.write(
                _LAYER, target, staged.content, metadata, exclusive=not revision
            )
        except FileExistsError:
            # Another approval created it between the check and the write.
            raise ValueError(
                f"E_TARGET_EXISTS: {target} was created concurrently; not overwritten"
            ) from None
        self._cleanup(approved, staged_key)
        return ApprovalResult(approved, written=True)

    def reject(self, key: str) -> str:
        """Delete a proposal; nothing is written to the procedural layer."""
        target = _target_key(key)
        self.staged(target)
        self._remove(STAGING_PREFIX + target)
        return STAGING_PREFIX + target

    def stage_legacy(self, key: str) -> MemoryEntry:
        """Move an entry written before #144 back to staging, to be approved again.

        Only an entry with none of this pipeline's fields qualifies (code before
        #144 wrote none): an entry carrying ``source_turn`` / ``approved_*`` /
        ``review`` / ``proposed_at`` / ``legacy`` without valid provenance was
        written or edited by hand after #144 and is refused (``E_NOT_LEGACY``;
        inspect and remove it by hand). The staged copy records
        ``source_turn: legacy-pre-144`` and ``legacy: true``; it is a proposal
        like any other, so a human still reads it (``show``) and approves it.
        Running the command again after a :class:`CleanupError` removes the
        leftover original.
        """
        target = _target_key(key)
        staged_key = STAGING_PREFIX + target
        entry = self.memory_store.read(_LAYER, target)
        if entry is None:
            raise KeyError(f"No procedural entry: {target}")
        if provenance_problem(entry.metadata) is None:
            raise ValueError(f"E_ALREADY_APPROVED: {target} already has provenance")
        staged = self.memory_store.read(_LAYER, staged_key)
        if staged is not None:
            if staged.metadata.get("legacy") is True and staged.content == entry.content:
                self._cleanup(staged, target)
                return staged
            raise ValueError(f"E_PROPOSAL_EXISTS: staging/{target} is already proposed")
        pipeline = sorted(_PIPELINE_KEYS.intersection(entry.metadata))
        if pipeline:
            raise ValueError(
                f"E_NOT_LEGACY: *** {target} has {', '.join(pipeline)} (not a pre-#144 entry); "
                "inspect and remove it by hand"
            )
        if not entry.content.strip():
            raise ValueError("E_EMPTY_CONTENT: a procedural entry needs content")
        metadata = dict(entry.metadata)
        _refuse_secrets(entry.content, LEGACY_SOURCE_TURN, metadata)
        try:
            staged = self.memory_store.write(
                _LAYER,
                staged_key,
                entry.content,
                {
                    **metadata,
                    "source_turn": LEGACY_SOURCE_TURN,
                    "proposed_at": _now(),
                    "legacy": True,
                },
                exclusive=True,
            )
        except FileExistsError:
            raise ValueError(f"E_PROPOSAL_EXISTS: staging/{target} is already proposed") from None
        self._cleanup(staged, target)
        return staged


__all__ = [
    "E_STAGING_CLEANUP",
    "LEGACY_SOURCE_TURN",
    "ApprovalResult",
    "CleanupError",
    "ProceduralReview",
    "content_sha256",
]
