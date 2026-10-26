"""Promote episodic memories through one auditable distillation pipeline."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, RLock

from stiracore.core.amygdala import AmygdalaPass
from stiracore.memory_importance import (
    BINDING_VERIFIED,
    bind_retention,
    binding_state,
    retention_binding,
)
from stiracore.memory_origin import record_origin, verified_origin
from stiracore.memory_rating import rebind_rating, scoring_metadata
from stiracore.tools.memory_store import MemoryEntry, MemoryLayer

from .distillers import Distiller, RuleBasedDistiller
from .staging_review import STAGING_PREFIX, is_rejected, is_staged_key

logger = logging.getLogger(__name__)


@dataclass
class HarvestResult:
    semantic: int = 0
    knowledge: int = 0
    discard: int = 0
    skipped: int = 0
    errors: int = 0
    # Entries waiting in semantic/staging/ after the sweep (all of them, not only new ones).
    staging_pending: int = 0
    # Not scored in this sweep because ``admit`` said stop (a scheduled run's limits, #251);
    # left unmarked for the next sweep, not counted as errors.
    deferred: int = 0

    @property
    def processed(self) -> int:
        return self.semantic + self.knowledge + self.discard


_STORE_LOCKS: dict[str, RLock] = {}
_STORE_LOCKS_GUARD = Lock()


def store_lock(memory_store) -> RLock:
    """The one Harvest lock of this process for ``memory_store``'s memory directory.

    Every :class:`HarvestScheduler` on the same directory shares it, so sweeps of one
    store never overlap inside a process (the scheduled job, a manual run, another
    instance). Reentrant: the job holds it around its own sweep. Other processes are
    excluded by ``server.lock`` (#285; the CLI with ``--owner-lock``), not by this.
    """
    directory = getattr(memory_store, "memory_dir", None)
    if directory is None:  # a store without a directory: per-instance, as before
        return RLock()
    key = os.path.normcase(os.path.realpath(directory))
    with _STORE_LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, RLock())


def write_semantic_index(memory_store) -> None:
    """Regenerate ``semantic/index.md`` from the promoted (non-staged) entries."""
    entries = [
        entry
        for entry in memory_store.list_entries(MemoryLayer.SEMANTIC, limit=100_000)
        if entry.key != "index" and not is_staged_key(entry.key)
    ]
    lines = [
        "# Semantic Memory Index",
        "",
        "| title | tags | score | source_id | path |",
        "|---|---|---:|---|---|",
    ]
    for entry in sorted(entries, key=lambda item: item.key):
        metadata = entry.metadata
        tags = metadata.get("tags", [])
        tags_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
        path = entry.path.relative_to(memory_store.memory_dir).as_posix()
        values = (
            str(metadata.get("title", entry.key)),
            tags_text,
            str(metadata.get("score", "")),
            str(metadata.get("source_id", "")),
            path,
        )
        escaped = [value.replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    memory_store.write(
        MemoryLayer.SEMANTIC,
        "index",
        "\n".join(lines) + "\n",
        {"generated": True, "entry_count": len(entries)},
    )


class HarvestScheduler:
    """Run one synchronous, additive distillation sweep.

    ``MemoryStore`` remains the canonical backend contract. Episodic Markdown
    is never deleted: every successful decision only adds harvest metadata to
    its existing frontmatter, keeping its modification time so that Episodic
    order (and the prompt's recent episodes) is unchanged (#292). Routing thresholds come exclusively from the
    wrapped :class:`AmygdalaPass`.

    ``stage_auto`` (default on, #293): until the staging review UI (#193)
    exists, the automatic band is staged for human review too instead of
    being written to ``knowledge/`` directly. Its staged key is
    ``staging/knowledge/<domain>/...`` so approval lands where it would
    otherwise have gone.
    """

    def __init__(
        self,
        memory_store,
        amygdala: AmygdalaPass | None,
        agent_id: str,
        *,
        distiller: Distiller | None = None,
        stage_auto: bool = True,
    ) -> None:
        if type(stage_auto) is not bool:
            raise ValueError("stage_auto must be a boolean")
        self.memory_store = memory_store
        self.agent_id = agent_id
        self.distiller = distiller or RuleBasedDistiller(amygdala)
        self.stage_auto = stage_auto
        self._lock = store_lock(memory_store)  # shared per store (#251)

    @staticmethod
    def _fingerprint(content: str) -> str:
        normalized = " ".join(content.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _slug(value: str, fallback: str = "general") -> str:
        slug = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-_")
        return slug[:64] or fallback

    @classmethod
    def _domain(cls, entry: MemoryEntry) -> str:
        domain = entry.metadata.get("domain")
        if isinstance(domain, str) and domain.strip():
            return cls._slug(domain)
        tags = entry.metadata.get("tags")
        if isinstance(tags, (list, tuple)):
            for tag in tags:
                if isinstance(tag, str) and tag.strip():
                    return cls._slug(tag)
        return "general"

    @staticmethod
    def _title(entry: MemoryEntry) -> str:
        title = entry.metadata.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:120]
        for line in entry.content.splitlines():
            candidate = line.strip().lstrip("#").strip()
            if candidate:
                return candidate[:120]
        return entry.key

    def _is_duplicate(self, entry: MemoryEntry, fingerprint: str) -> bool:
        probes = [fingerprint, entry.key]
        normalized = " ".join(entry.content.casefold().split())
        if normalized:
            probes.append(normalized[:160])
        seen: set[tuple[str, str]] = set()
        for probe in probes:
            for candidate in self.memory_store.search(
                probe,
                layers=[MemoryLayer.SEMANTIC],
                limit=20,
            ):
                identity = (candidate.layer.value, candidate.key)
                if identity in seen or candidate.key == "index":
                    continue
                seen.add(identity)
                candidate_fingerprint = candidate.metadata.get("source_fingerprint")
                candidate_source = candidate.metadata.get("source_id")
                candidate_normalized = " ".join(candidate.content.casefold().split())
                if (
                    candidate_fingerprint == fingerprint
                    or candidate_source == entry.key
                    or candidate_normalized == normalized
                ):
                    return True
        return False

    def _mark_episode(
        self,
        entry: MemoryEntry,
        *,
        score: float,
        route: str,
        reason: str,
        band: str | None = None,
    ) -> None:
        # The marks are metadata: keep the episode's time so that marking does
        # not reorder Episodic memory (``list_entries`` and the prompt, #292).
        verified = verified_origin(self.memory_store, MemoryLayer.EPISODIC, entry.key)
        # A human's retention tag (#173) follows this rewrite only when the file is
        # exactly what was tagged *and* what this sweep parsed (otherwise the record
        # keeps its old digest: the entry stays held and is reported for a human look).
        store = self.memory_store
        before = store.read_text(MemoryLayer.EPISODIC, entry.key)
        tag = retention_binding(store, MemoryLayer.EPISODIC, entry.key)
        follow = (
            tag is not None
            and before is not None
            and binding_state(tag, before) == BINDING_VERIFIED
            and store._parse(before) == (entry.content, entry.metadata)
        )
        metadata = {
            **entry.metadata,
            "harvested": True,
            "harvest_score": score,
            "harvest_route": route,
            "harvest_band": band or route,
            "harvest_reason": reason,
            "harvest_version": self.distiller.version,
        }
        self.memory_store.write(
            MemoryLayer.EPISODIC,
            entry.key,
            entry.content,
            metadata,
            preserve_mtime=True,
        )
        if verified:  # this rewrite is the server's own: the binding moves with it (#294)
            record_origin(
                self.memory_store,
                MemoryLayer.EPISODIC,
                entry.key,
                verified["origin"],
                verified["source"],
            )
        expected = store.render(entry.content, metadata)
        if follow:
            # Bound to the text this rewrite produced, never to a re-read of the file: a
            # change by another writer after the write stays a mismatch.
            bind_retention(store, MemoryLayer.EPISODIC, entry.key, tag[0], expected)
        if before is not None and store._parse(before) == (entry.content, entry.metadata):
            # ADR 0018 §1a: an agent rating that verified before this rewrite moves with it
            # (stored only; nothing reads it in phase 0).
            rebind_rating(store, MemoryLayer.EPISODIC, entry.key, before, expected)

    def _write_semantic(
        self,
        entry: MemoryEntry,
        *,
        score: float,
        route: str,
        band: str,
        reason: str,
        fingerprint: str,
    ) -> None:
        source_digest = hashlib.sha256(entry.key.encode("utf-8")).hexdigest()
        readable_slug = self._slug(entry.key, "memory")[:24]
        key_base = f"harvest-{readable_slug}-{source_digest[:16]}"
        # The score band picks the final home; the route decides whether a human sees it first.
        home = key_base if band == "staging" else f"knowledge/{self._domain(entry)}/{key_base}"
        key = f"{STAGING_PREFIX}{home}" if route == "staging" else home
        # Slugging and digest truncation are lossy. Never overwrite another
        # source that happens to map to the same shortened destination key.
        existing = self.memory_store.read(MemoryLayer.SEMANTIC, key)
        if existing is not None and existing.metadata.get("source_id") != entry.key:
            key = f"{key}-{source_digest[16:32]}"
            existing = self.memory_store.read(MemoryLayer.SEMANTIC, key)
            if existing is not None and existing.metadata.get("source_id") != entry.key:
                raise ValueError("Semantic destination belongs to another source")
        self.memory_store.write(
            MemoryLayer.SEMANTIC,
            key,
            entry.content,
            {
                "title": self._title(entry),
                "tags": list(entry.metadata.get("tags", []))
                if isinstance(entry.metadata.get("tags"), (list, tuple))
                else [],
                "score": score,
                "source_id": entry.key,
                "source_type": "episodic",
                "source_fingerprint": fingerprint,
                "harvest_route": route,
                "harvest_band": band,
                "harvest_reason": reason,
                "harvest_version": self.distiller.version,
                "domain": self._domain(entry),
                # Provenance (#294): who wrote the text and how it came in — only what
                # a server writer recorded for this exact episode text (the episode's
                # front matter is agent-writable); otherwise nothing = unknown.
                **(verified_origin(self.memory_store, MemoryLayer.EPISODIC, entry.key) or {}),
            },
        )

    def _write_index(self) -> None:
        write_semantic_index(self.memory_store)

    def run_distillation(
        self, *, admit: Callable[[MemoryEntry], bool] | None = None
    ) -> HarvestResult:
        """Process a snapshot and leave failed episodes retryable.

        ``admit`` (optional, #251) is asked before each episode that would be scored;
        ``False`` leaves it untouched for a later sweep (counted in ``deferred``).
        """
        result = HarvestResult()
        with self._lock:
            episodes = list(self.memory_store.list_entries(MemoryLayer.EPISODIC, limit=100_000))
            for entry in episodes:
                try:
                    if (
                        entry.metadata.get("harvested")
                        and entry.metadata.get("harvest_version") == self.distiller.version
                    ):
                        result.skipped += 1
                        continue
                    if admit is not None and not admit(entry):
                        result.deferred += 1
                        continue
                    # #506: a front-matter importance counts only while the server's
                    # episode writer is still the author of the exact text.
                    decision = self.distiller.distill(
                        entry.content, scoring_metadata(self.memory_store, entry)
                    )
                    fingerprint = self._fingerprint(entry.content)
                    if decision.route != "discard" and is_rejected(
                        self.memory_store, fingerprint, entry.key
                    ):
                        self._mark_episode(
                            entry,
                            score=decision.score,
                            route="rejected",
                            reason="Previously rejected in staging review",
                            band=decision.route,
                        )
                        result.skipped += 1
                        continue
                    if decision.route != "discard" and self._is_duplicate(entry, fingerprint):
                        self._mark_episode(
                            entry,
                            score=decision.score,
                            route="duplicate",
                            reason="Existing semantic memory matched source or content",
                            band=decision.route,
                        )
                        result.skipped += 1
                        continue
                    route = decision.route
                    if route == "auto" and self.stage_auto:
                        route = "staging"
                    if route != "discard":
                        self._write_semantic(
                            entry,
                            score=decision.score,
                            route=route,
                            band=decision.route,
                            reason=decision.reason,
                            fingerprint=fingerprint,
                        )
                    self._mark_episode(
                        entry,
                        score=decision.score,
                        route=route,
                        reason=decision.reason,
                        band=decision.route,
                    )
                    bucket = (
                        "semantic"
                        if route == "staging"
                        else "knowledge"
                        if route == "auto"
                        else "discard"
                    )
                    setattr(result, bucket, getattr(result, bucket) + 1)
                except Exception as error:  # noqa: BLE001 - one bad episode must not stop the sweep
                    result.errors += 1
                    # Code and type only: a message or traceback could carry the episode text.
                    logger.warning(
                        "Harvest episode failed; left retryable (E_HARVEST_EPISODE, %s)",
                        type(error).__name__,
                    )
            if result.semantic or result.knowledge:
                self._write_index()
            result.staging_pending = sum(
                1
                for entry in self.memory_store.list_entries(MemoryLayer.SEMANTIC, limit=100_000)
                if is_staged_key(entry.key)
            )
        return result
