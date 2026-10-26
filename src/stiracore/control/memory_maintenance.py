"""Manual-only semantic forgetting and duplicate maintenance.

Forgetting compresses a Semantic entry to its head and tail. Since #294 it is
reversible and audited: the original Markdown is archived (``forgotten/``, see
:mod:`stiracore.control.forgetting_archive`) before the entry is overwritten,
and the overwrite is refused when the archive cannot be written. Undo is explicit
only (:meth:`MemoryMaintenance.undo_forgetting`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from stiracore.control.forgetting_archive import (
    ArchiveRecord,
    ForgettingArchive,
    ForgettingArchiveError,
    sha256_text,
)
from stiracore.control.staging_review import is_staged_key
from stiracore.memory_importance import REFERENCE_HALF_LIFE_DAYS, Retention, retention_of
from stiracore.tools.memory_store import MemoryEntry, MemoryLayer, MemoryStore

# What the selection is based on; shown with every preview (#294). Semantic memory is
# not injected into the prompt, so last_accessed only moves when the memory tools read
# or search an entry: a useful entry that is never searched is compressed first.
SELECTION_BASIS = (
    "last_accessed only: Semantic memory is not part of the prompt, so only "
    "memory_read/memory_search (and Harvest's duplicate search) refresh it; "
    "scaled by the entry's half-life (retention_half_life_days, default "
    f"{REFERENCE_HALF_LIFE_DAYS}); retention: invariant is never selected (#173)"
)
INVARIANT_REASON = "retention: invariant (never forgotten)"


def public_entry(key: str) -> dict[str, str]:
    """How the new outputs of #173 name a Semantic entry: a fixed area label and the
    SHA-256 of ``semantic/<key>`` (as ``stira memory tag --id`` prints it), never the key,
    which can carry a memory's words."""
    return {
        "area": "staging" if is_staged_key(key) else "semantic",
        "key_sha256": hashlib.sha256(f"semantic/{key}".encode()).hexdigest(),
    }


@dataclass(frozen=True)
class ForgettingCandidate:
    key: str
    age_days: int
    stage: str
    current_stage: str = "none"
    last_accessed: str | None = None
    threshold_days: int | None = None
    chars: int | None = None
    limit: int | None = None
    archive_id: str | None = None
    refused: str | None = None
    # Set when the entry *was* compressed but its audit line could not be written.
    audit_error: str | None = None
    # The entry's half-life (#173): the stage thresholds are for 30 days and scale with it.
    half_life_days: int = REFERENCE_HALF_LIFE_DAYS
    # 2 ** (-age / half_life), rounded for display.
    strength: float | None = None
    # The front matter held a tag outside the contract; it was ignored (default used).
    retention_invalid: bool = False

    @property
    def reason(self) -> str:
        threshold = self.threshold_days or 0
        if self.half_life_days == REFERENCE_HALF_LIFE_DAYS:
            limit = f"{threshold} days"
        else:
            limit = f"{threshold * self.half_life_days / REFERENCE_HALF_LIFE_DAYS:g} days"
        ignored = ", invalid retention tag ignored" if self.retention_invalid else ""
        return (
            f"last_accessed {self.age_days} days ago >= {limit} "
            f"(half-life {self.half_life_days} days, strength {self.strength}{ignored}; "
            f"{self.current_stage} -> {self.stage}, {self.chars} chars -> at most {self.limit})"
        )

    def as_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["reason"] = self.reason
        return data

    def public_dict(self) -> dict[str, Any]:
        """The dry run's view of a candidate (#173): an allow-list of fields, each a fixed
        label, a digest or a number — nothing copied from the front matter.

        ``current_stage`` comes from the entry's ``forgetting_stage``, which can hold any
        string: only the known stage names are shown, anything else is ``unknown``, and
        the reason is built from the normalised value.
        """
        known = MemoryMaintenance._STAGE_ORDER
        current = self.current_stage if self.current_stage in known else "unknown"
        stage = self.stage if self.stage in known else "unknown"
        shown = replace(self, current_stage=current, stage=stage)
        return {
            **public_entry(self.key),
            "stage": stage,
            "current_stage": current,
            "age_days": int(self.age_days),
            # Rendered from the index timestamp (a float), never from the front matter.
            "last_accessed": self.last_accessed,
            "threshold_days": int(self.threshold_days or 0),
            "chars": int(self.chars or 0),
            "limit": int(self.limit or 0),
            "half_life_days": int(self.half_life_days),
            "strength": float(self.strength or 0.0),
            "retention_invalid": bool(self.retention_invalid),
            "reason": shown.reason,
        }


@dataclass(frozen=True)
class ForgettingResult:
    candidates: tuple[ForgettingCandidate, ...]
    applied: int = 0
    refused: int = 0
    error: str | None = None
    # Entries compressed whose ``forget.applied`` line is missing (action needed).
    audit_incomplete: int = 0
    # Audit lines completed (or orphan records cleaned) by this run, see ``reconcile``.
    reconciled: tuple[dict[str, str], ...] = ()
    # Entries old enough to be selected but tagged ``retention: invariant`` (#173):
    # ``{"area", "key_sha256", "code", "reason"}`` (no key), never touched.
    protected: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class UndoResult:
    archive_id: str
    restored: bool
    key: str | None = None
    refused: str | None = None
    # Set when the entry *was* restored but its ``undo.applied`` line is missing; the
    # record is then kept so the next apply/undo run can complete the log.
    audit_error: str | None = None
    reconciled: tuple[dict[str, str], ...] = ()
    # Restored and logged, but the record could not be removed (retried next run).
    cleanup_error: str | None = None


class _ChangedButNotAudited(Exception):
    """The entry was changed; only the audit line after the change failed."""

    def __init__(self, candidate: ForgettingCandidate, error: Exception) -> None:
        super().__init__(str(error))
        self.candidate = candidate


@dataclass(frozen=True)
class DeletionCandidate:
    key: str
    reason: str
    duplicate_of: str | None = None
    age_days: int | None = None


class MemoryMaintenance:
    """Inspect and optionally compress Semantic memory; Episodic is untouched."""

    STAGES = ((180, "archive", 800), (90, "compact", 2_000), (30, "light", 4_000))
    STALE_DAYS = 365
    _STAGE_ORDER: ClassVar[dict[str, int]] = {
        "none": 0,
        "light": 1,
        "compact": 2,
        "archive": 3,
    }

    def __init__(self, store: MemoryStore, archive: ForgettingArchive | None = None) -> None:
        self.store = store
        self.archive = archive or ForgettingArchive(store)

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        value = now or datetime.now(tz=UTC)
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _semantic_entries(store: MemoryStore) -> list[MemoryEntry]:
        return [
            entry
            for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=100_000)
            if entry.key != "index"
        ]

    @classmethod
    def _target_stage(
        cls, age_days: int, retention: Retention | None = None
    ) -> tuple[int, str, int] | None:
        """The deepest stage ``age_days`` reached; thresholds scale with the half-life.

        Untagged (half-life 30): ``age >= threshold``, exactly as before #173.
        """
        retention = retention or Retention()
        for threshold, stage, limit in cls.STAGES:
            if retention.reaches(age_days, threshold):
                return threshold, stage, limit
        return None

    @staticmethod
    def _compress(content: str, limit: int) -> str:
        normalized = re.sub(r"[ \t]+", " ", content).strip()
        if len(normalized) <= limit:
            return normalized + ("\n" if normalized else "")
        head_limit = max(1, int(limit * 0.75))
        tail_limit = max(1, limit - head_limit - len("\n\n…\n\n"))
        return normalized[:head_limit].rstrip() + "\n\n…\n\n" + normalized[-tail_limit:].lstrip()

    def run_forgetting(
        self,
        *,
        now: datetime | None = None,
        apply: bool = False,
    ) -> ForgettingResult:
        """Find stage transitions; with ``apply``, archive each original, then compress it.

        The preview (default) changes nothing and lists every candidate with the reason
        it was selected. ``apply`` refuses everything when the archive is unusable and
        stops at the first entry whose archive record cannot be written and read back:
        no entry is ever overwritten without an intact record of its original.
        """
        current_time = self._now(now)
        candidates: list[ForgettingCandidate] = []
        protected: list[dict[str, str]] = []
        for entry in self._semantic_entries(self.store):
            age_days = max(0, (current_time - entry.last_accessed).days)
            retention = retention_of(entry.metadata)
            target = self._target_stage(age_days, retention)
            if target is None:
                continue
            threshold, stage, limit = target
            current_stage = str(entry.metadata.get("forgetting_stage", "none"))
            if self._STAGE_ORDER.get(current_stage, 0) >= self._STAGE_ORDER[stage]:
                continue
            if retention.invariant:
                protected.append(
                    {**public_entry(entry.key), "code": "retention_invariant",
                     "reason": INVARIANT_REASON}
                )  # fmt: skip
                continue
            candidates.append(
                ForgettingCandidate(
                    entry.key,
                    age_days,
                    stage,
                    current_stage=current_stage,
                    last_accessed=entry.last_accessed.isoformat(),
                    threshold_days=threshold,
                    chars=len(entry.content),
                    limit=limit,
                    half_life_days=retention.half_life_days,
                    strength=round(retention.strength(age_days), 4),
                    retention_invalid=retention.invalid,
                )
            )
        if not apply or (not candidates and not self.archive.directory.exists()):
            return ForgettingResult(tuple(candidates), protected=tuple(protected))
        try:
            return replace(self._apply(candidates, current_time), protected=tuple(protected))
        finally:
            self.archive.close()

    def _apply(self, candidates: list[ForgettingCandidate], at: datetime) -> ForgettingResult:
        try:
            self.archive.prepare()
            reconciled = self.reconcile()
        except ForgettingArchiveError as error:
            refused = tuple(replace(item, refused=str(error)) for item in candidates)
            return ForgettingResult(refused, 0, len(refused), str(error))

        done: list[ForgettingCandidate] = []
        for position, candidate in enumerate(candidates):
            try:
                done.append(self._apply_one(candidate, at))
            except _ChangedButNotAudited as changed:
                # Compressed, archived, but not logged: report it as applied and stop.
                rest = [
                    replace(item, refused=f"stopped: audit log not writable ({changed})")
                    for item in candidates[position + 1 :]
                ]
                return ForgettingResult(
                    (*done, changed.candidate, *rest),
                    len(done) + 1,
                    len(rest),
                    str(changed),
                    audit_incomplete=1,
                    reconciled=reconciled,
                )
            except ForgettingArchiveError as error:
                # Fail closed: this entry and every later one stay as they are.
                rest = [replace(item, refused=str(error)) for item in candidates[position:]]
                with _suppressed():
                    self.archive.audit(
                        "forget.refused", key=candidate.key, stage=candidate.stage, reason=error
                    )
                return ForgettingResult(
                    (*done, *rest), len(done), len(rest), str(error), reconciled=reconciled
                )
        return ForgettingResult(tuple(done), len(done), reconciled=reconciled)

    def _apply_one(self, candidate: ForgettingCandidate, at: datetime) -> ForgettingCandidate:
        store = self.store
        original = store.read_text(MemoryLayer.SEMANTIC, candidate.key)
        if original is None:
            raise ForgettingArchiveError("entry disappeared before it was forgotten")
        content, metadata = store._parse(original)
        if retention_of(metadata).invariant:  # tagged after the selection (#173)
            raise ForgettingArchiveError("entry was tagged retention: invariant; not forgotten")
        original_sha = sha256_text(original)
        archive_id = self.archive.new_id(candidate.key, original_sha, candidate.stage, at)
        compressed = store.render(
            self._compress(content, candidate.limit or 0),
            {
                **metadata,
                "forgetting_stage": candidate.stage,
                "forgetting_age_days": candidate.age_days,
                "forgetting_applied_at": at.isoformat(),
                "forgetting_archive_id": archive_id,
            },
        )
        record = ArchiveRecord(
            id=archive_id,
            key=candidate.key,
            stage=candidate.stage,
            previous_stage=candidate.current_stage,
            applied_at=at.isoformat(),
            age_days=candidate.age_days,
            last_accessed=candidate.last_accessed or "",
            reason=candidate.reason,
            source_id=str(metadata["source_id"]) if metadata.get("source_id") else None,
            original_sha256=original_sha,
            compressed_sha256=sha256_text(compressed),
            original_text=original,
            agent_id=store.agent_id,
        )
        self.archive.save(record)
        audit = {
            "id": archive_id,
            "key": candidate.key,
            "stage": candidate.stage,
            "previous_stage": candidate.current_stage,
            "original_sha256": record.original_sha256,
            "compressed_sha256": record.compressed_sha256,
        }
        try:
            self.archive.audit("forget.started", **audit)
            current = store.read_text(MemoryLayer.SEMANTIC, candidate.key)
            if current is None or sha256_text(current) != original_sha:
                raise ForgettingArchiveError("entry changed while it was being forgotten")
        except ForgettingArchiveError:
            # Nothing was overwritten: the record is only a spare copy, drop it.
            with _suppressed():
                self.archive.remove(archive_id)
            raise
        store.write_text(MemoryLayer.SEMANTIC, candidate.key, compressed)
        applied = replace(candidate, archive_id=archive_id)
        try:
            self.archive.audit("forget.applied", **audit)
        except ForgettingArchiveError as error:
            raise _ChangedButNotAudited(replace(applied, audit_error=str(error)), error) from None
        return applied

    # Terminal audit kinds per archive id: once logged, never logged again (#320).
    _APPLIED = frozenset({"forget.applied", "forget.recovered"})
    _FINISHED = frozenset({"undo.applied", "undo.recovered", "forget.abandoned"})

    def reconcile(self) -> tuple[dict[str, str], ...]:
        """Complete the audit log after an interrupted step (needs a prepared archive).

        Runs at the start of every apply and undo and is idempotent. Per record, from the
        log and the entry's current digest:

        * already finished (``undo.applied`` / ``undo.recovered`` / ``forget.abandoned``
          logged): only the clean-up (removing the record) is resumed, nothing is logged;
        * an undo that restored the entry but did not log it: ``undo.recovered``, then
          clean-up;
        * a compression that was written but not logged: ``forget.recovered`` (the
          record stays: it is what undo needs);
        * a record whose entry was never overwritten: ``forget.abandoned``, then clean-up;
        * anything else (applied and logged, an undo that did not restore): left alone.

        Logging and clean-up are separate steps: a failed or interrupted removal is
        reported (``cleanup_error``) and resumed by the next run without a second line.
        Damaged records are skipped (``--list-forgotten`` shows them).
        """
        kinds: dict[str, set[str]] = {}
        for event in self.archive.logged():
            identifier = event["body"].get("id")
            if isinstance(identifier, str):
                kinds.setdefault(identifier, set()).add(event["kind"])
        done: list[dict[str, str]] = []
        for archive_id in self.archive.ids():
            try:
                record = self.archive.load(archive_id)
            except ForgettingArchiveError:
                continue
            seen = kinds.get(archive_id, set())
            if seen & self._FINISHED:
                action = "cleanup"
            else:
                current = self.store.read_text(MemoryLayer.SEMANTIC, record.key)
                digest = None if current is None else sha256_text(current)
                body = {
                    "id": archive_id,
                    "key": record.key,
                    "stage": record.stage,
                    "original_sha256": record.original_sha256,
                    "compressed_sha256": record.compressed_sha256,
                }
                if "undo.started" in seen and digest == record.original_sha256:
                    action = "undo.recovered"
                elif seen & self._APPLIED or "undo.started" in seen:
                    continue
                elif digest == record.compressed_sha256 and "forget.started" in seen:
                    self.archive.audit("forget.recovered", **body)
                    done.append({"id": archive_id, "action": "forget.recovered"})
                    continue
                elif digest == record.original_sha256:
                    action = "forget.abandoned"
                else:
                    continue
                self.archive.audit(action, **body)
            entry = {"id": archive_id, "action": action}
            cleanup_error = self._cleanup(archive_id)
            if cleanup_error:
                entry["cleanup_error"] = cleanup_error
            done.append(entry)
        return tuple(done)

    def _cleanup(self, archive_id: str) -> str | None:
        """Remove a record that is no longer needed; the error text, or ``None``."""
        try:
            self.archive.remove(archive_id)
        except (OSError, ForgettingArchiveError) as error:
            return f"record not removed ({type(error).__name__}); the next run retries"
        return None

    def undo_forgetting(self, archive_id: str) -> UndoResult:
        """Restore the archived original of one forgetting step (explicit, audited).

        Refused when the record is missing or damaged, names a key outside this store's
        Semantic layer, or when the entry is no longer exactly what this step wrote
        (edited since, or a later step compressed it again: undo that one first). The
        restored entry counts as read now (``last_accessed``), so the next preview does
        not select it again at once. The record is removed after the restore and its
        ``undo.applied`` line; if only that line fails, the result says ``restored`` with
        ``audit_error`` and the record stays until :meth:`reconcile` completes the log.
        """
        try:
            return self._undo(archive_id)
        finally:
            self.archive.close()

    def _undo(self, archive_id: str) -> UndoResult:
        try:
            self.archive.prepare()
            reconciled = self.reconcile()
        except ForgettingArchiveError as error:
            return UndoResult(str(archive_id), False, refused=str(error))
        try:
            record = self.archive.load(archive_id)
        except ForgettingArchiveError as error:
            with _suppressed():
                self.archive.audit("undo.refused", id=str(archive_id), reason=error)
            return UndoResult(str(archive_id), False, refused=str(error), reconciled=reconciled)
        audit = {
            "id": record.id,
            "key": record.key,
            "stage": record.stage,
            "original_sha256": record.original_sha256,
            "compressed_sha256": record.compressed_sha256,
        }
        current = self.store.read_text(MemoryLayer.SEMANTIC, record.key)
        if current is None or sha256_text(current) != record.compressed_sha256:
            reason = (
                "the entry is no longer what this forgetting step wrote "
                "(edited since, or compressed again: undo the later step first)"
            )
            with _suppressed():
                self.archive.audit("undo.refused", **audit, reason=reason)
            return UndoResult(record.id, False, record.key, reason, reconciled=reconciled)
        try:
            self.archive.audit("undo.started", **audit)
        except ForgettingArchiveError as error:  # nothing changed yet
            return UndoResult(record.id, False, record.key, str(error), reconciled=reconciled)
        self.store.write_text(MemoryLayer.SEMANTIC, record.key, record.original_text)
        restored = self.store.read_text(MemoryLayer.SEMANTIC, record.key)
        if restored is None or sha256_text(restored) != record.original_sha256:
            return UndoResult(
                record.id,
                False,
                record.key,
                "restored entry does not match the archive (record kept)",
                reconciled=reconciled,
            )
        self.store.read(MemoryLayer.SEMANTIC, record.key)  # access-touch
        try:
            self.archive.audit("undo.applied", **audit)
        except ForgettingArchiveError as error:
            return UndoResult(
                record.id, True, record.key, audit_error=str(error), reconciled=reconciled
            )
        cleanup_error = self._cleanup(record.id)
        return UndoResult(
            record.id, True, record.key, reconciled=reconciled, cleanup_error=cleanup_error
        )

    def find_deletion_candidates(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[DeletionCandidate, ...]:
        """Return duplicate/stale Semantic candidates without deleting anything.

        ``retention: invariant`` entries are never candidates (#173); in a duplicate
        group the copy kept is an invariant one when there is one.
        """
        current_time = self._now(now)
        entries = self._semantic_entries(self.store)
        retentions = {entry.key: retention_of(entry.metadata) for entry in entries}
        groups: dict[str, list[MemoryEntry]] = {}
        for entry in entries:
            normalized = " ".join(entry.content.casefold().split())
            groups.setdefault(normalized, []).append(entry)

        candidates: dict[str, DeletionCandidate] = {}
        for group in groups.values():
            if not group or not group[0].content.strip() or len(group) < 2:
                continue
            keep = max(
                group,
                key=lambda item: (
                    retentions[item.key].invariant,
                    item.last_accessed,
                    item.modified_at,
                    item.key,
                ),
            )
            for entry in group:
                if entry.key != keep.key and not retentions[entry.key].invariant:
                    candidates[entry.key] = DeletionCandidate(
                        entry.key,
                        "duplicate semantic content",
                        duplicate_of=keep.key,
                    )

        for entry in entries:
            retention = retentions[entry.key]
            if entry.key in candidates or retention.invariant:
                continue
            age_days = max(0, (current_time - entry.last_accessed).days)
            if retention.reaches(age_days, self.STALE_DAYS):
                candidates[entry.key] = DeletionCandidate(
                    entry.key,
                    "semantic memory has not been accessed recently",
                    age_days=age_days,
                )
        return tuple(candidates[key] for key in sorted(candidates))


class _suppressed:
    """Best-effort audit of a refusal: a broken log must not hide the refusal itself."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, value, traceback) -> bool:
        return kind is not None and issubclass(kind, (ForgettingArchiveError, OSError))


__all__ = [
    "SELECTION_BASIS",
    "DeletionCandidate",
    "ForgettingCandidate",
    "ForgettingResult",
    "MemoryMaintenance",
    "UndoResult",
]
