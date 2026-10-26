"""Review queue for harvest entries routed to ``semantic/staging/``."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from stiracore.tools.memory_store import MemoryEntry, MemoryLayer

STAGING_PREFIX = "staging/"
_LEDGER_NAME = "staging_rejections.jsonl"


def is_staged_key(key: str) -> bool:
    return key.startswith(STAGING_PREFIX)


def _ledger_path(memory_store) -> Path:
    return Path(memory_store.memory_dir) / _LEDGER_NAME


def load_rejections(memory_store) -> list[dict[str, Any]]:
    path = _ledger_path(memory_store)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def is_rejected(memory_store, fingerprint: str, source_id: str) -> bool:
    for record in load_rejections(memory_store):
        if record.get("source_fingerprint") == fingerprint or record.get("source_id") == source_id:
            return True
    return False


class StagingReview:
    """Approve or reject staged semantic entries."""

    def __init__(self, memory_store) -> None:
        self.memory_store = memory_store

    @staticmethod
    def _staged_key(key: str) -> str:
        return key if is_staged_key(key) else f"{STAGING_PREFIX}{key}"

    def list_staged(self) -> list[MemoryEntry]:
        return [
            entry
            for entry in self.memory_store.list_entries(MemoryLayer.SEMANTIC, limit=100_000)
            if is_staged_key(entry.key)
        ]

    def _remove(self, key: str) -> None:
        store = self.memory_store
        path = store._path(MemoryLayer.SEMANTIC, key)
        store._require_writable(path)
        path.unlink(missing_ok=True)
        with store._connection() as connection:
            connection.execute(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?",
                (MemoryLayer.SEMANTIC.value, key),
            )

    def _require(self, key: str) -> MemoryEntry:
        entry = self.memory_store.read(MemoryLayer.SEMANTIC, key)
        if entry is None:
            raise KeyError(f"No staged entry: {key}")
        return entry

    def approve(self, key: str) -> MemoryEntry:
        staged_key = self._staged_key(key)
        entry = self._require(staged_key)
        target = staged_key[len(STAGING_PREFIX) :]
        existing = self.memory_store.read(MemoryLayer.SEMANTIC, target)
        if existing is not None and existing.metadata.get("source_id") != entry.metadata.get(
            "source_id"
        ):
            raise ValueError("Semantic destination belongs to another source")
        promoted = self.memory_store.write(
            MemoryLayer.SEMANTIC,
            target,
            entry.content,
            {**entry.metadata, "review": "approved", "reviewed_at": time.time()},
        )
        self._remove(staged_key)
        return promoted

    def reject(self, key: str, reason: str = "") -> dict[str, Any]:
        staged_key = self._staged_key(key)
        entry = self._require(staged_key)
        record = {
            "key": staged_key,
            "source_id": entry.metadata.get("source_id"),
            "source_fingerprint": entry.metadata.get("source_fingerprint"),
            "reason": reason,
            "rejected_at": time.time(),
        }
        with _ledger_path(self.memory_store).open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._remove(staged_key)
        return record
