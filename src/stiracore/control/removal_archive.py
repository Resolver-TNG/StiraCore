"""Removal records: what a retention run took out, kept for undo (#294); delete-job state.

``stira memory retention --apply`` never deletes without a copy: every removed item
(an Episodic entry, a checkpoint, ``turns.db`` rows...) is first written to one record
``forgotten/removed-<id>.json``, read back and verified, and only then removed.
``stira memory restore <id>`` puts the items back (explicit, audited); no automatic
restore exists. A delete request (``stira memory forget --apply``) is the opposite: it
keeps no copy and rewrites or drops the records that hold what it deletes (audited by
ids and digests). Its job state lives here too (``delete-<id>.json``: digests only —
of the query, the requested ids and each step's location — in every state).

The records live in the same directory as the forgetting archive of #320
(``memory/agents/<agent>/memory/forgotten/``: outside the layer directories, on the
read and write floor of the file tools, ``0700``/``0600``) and share its hash-chained
audit log ``forgetting-log.jsonl`` (one writer at a time: the data directory's owner
lock, #285). The names never collide: #320 records are ``<id>.json``, these are
``removed-<id>.json``. A record holds the **full text** it removed (may hold secrets);
the audit log holds ids, counts, keys and SHA-256 digests only, never content or the
query of a delete request (its digest only).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from stiracore.control.forgetting_archive import (
    _ID,
    ForgettingArchiveError,
    _AuditLog,
    _Directory,
    _open_error,
    _read_all,
    _shown,
    _write_all,
    sha256_text,
)
from stiracore.control.memory_layout import DataLayout
from stiracore.core.approval import canonical_json

__all__ = ["JOB_PREFIX", "REMOVAL_PREFIX", "RemovalArchive", "RemovalRecord", "items_digest"]

REMOVAL_PREFIX = "removed-"
_FORMAT = 1
_REMOVAL_NAME = re.compile(rf"{REMOVAL_PREFIX}([0-9]{{8}}T[0-9]{{6}}Z-[0-9a-f]{{16}})\.json")
JOB_PREFIX = "delete-"
_JOB_NAME = re.compile(rf"{JOB_PREFIX}([0-9]{{8}}T[0-9]{{6}}Z-[0-9a-f]{{16}})\.json")
_ITEM_STORES = frozenset(
    {
        "memory",
        "emotion_record",
        "checkpoint",
        "context_stash",
        "memory_plan",
        "turn_event",
        "tool_payload",
        "staging_rejection",
    }
)


def items_digest(items: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_json(items))


@dataclass(frozen=True)
class RemovalRecord:
    id: str
    purpose: str  # "retention" | "forget"
    created_at: str
    agent_id: str
    reason: str
    items: tuple[dict[str, Any], ...]
    items_sha256: str
    format: int = _FORMAT

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "items": list(self.items),
            "items_sha256": self.items_sha256,
            "format": self.format,
        }

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for item in self.items:
            found[item["store"]] = found.get(item["store"], 0) + 1
        return found

    def summary(self) -> dict[str, Any]:
        """Printable: ids, counts, keys; no removed text."""
        return {
            "id": self.id,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "reason": _shown(self.reason),
            "counts": self.counts(),
            "items_sha256": self.items_sha256,
        }


class RemovalArchive:
    """``forgotten/removed-<id>.json`` plus the shared audit log."""

    def __init__(self, layout: DataLayout) -> None:
        self.layout = layout
        self.directory = layout.archive_dir
        self._dir: _Directory | None = None
        self._log: _AuditLog | None = None

    # -- preparation (apply / restore / delete jobs only) ----------------------

    def prepare(self) -> None:
        self.close()
        try:
            self.layout.check_inside(self.directory)
        except ValueError:
            # Fixed text: the path itself may carry what a delete request removes.
            raise ForgettingArchiveError("archive directory path has a symbolic link") from None
        if not self.layout.memory_dir.is_dir():
            raise ForgettingArchiveError("memory directory missing (nothing to archive into)")
        directory = _Directory(self.directory, create=True)
        try:
            log = _AuditLog(directory)
            log.open()
        except BaseException:
            directory.close()
            raise
        self._dir, self._log = directory, log

    def close(self) -> None:
        if self._dir is not None:
            self._dir.close()
        self._dir = self._log = None

    def _require(self) -> tuple[_Directory, _AuditLog]:
        if self._dir is None or self._log is None:
            self.prepare()
        assert self._dir is not None and self._log is not None
        return self._dir, self._log

    def audit(self, kind: str, **body: Any) -> dict[str, Any]:
        clean = {
            name: _shown(value) if isinstance(value, (str, BaseException)) else value
            for name, value in body.items()
        }
        return self._require()[1].append(kind, clean)

    def events(self) -> list[dict[str, Any]]:
        return list(self._require()[1].events)

    # -- reading (also without prepare: nothing is created) --------------------

    def _reader(self) -> _Directory | None:
        if self._dir is not None:
            return self._dir
        if not self.directory.is_dir() or self.directory.is_symlink():
            return None
        return _Directory(self.directory, create=False)

    def _names(self) -> list[str]:
        reader = self._reader()
        if reader is None:
            return []
        try:
            return reader.names()
        finally:
            if reader is not self._dir:
                reader.close()

    def ids(self) -> list[str]:
        found = []
        for name in self._names():
            match = _REMOVAL_NAME.fullmatch(name)
            if match:
                found.append(match.group(1))
        return found

    def forgetting_ids(self) -> list[str]:
        """Ids of the #320 forgetting records in the same directory."""
        return [n[:-5] for n in self._names() if n.endswith(".json") and _ID.fullmatch(n[:-5])]

    def _read_json(self, name: str) -> dict[str, Any]:
        reader = self._reader()
        if reader is None:
            raise ForgettingArchiveError("no archive directory")
        try:
            try:
                fd = reader.open(name, os.O_RDONLY)
            except FileNotFoundError:
                raise ForgettingArchiveError("no record with this id") from None
            except OSError as error:
                raise _open_error(error, "archive record") from None
            try:
                raw = json.loads(_read_all(fd).decode("utf-8"))
            except (OSError, ValueError) as error:
                raise ForgettingArchiveError(
                    f"record unreadable ({type(error).__name__})"
                ) from None
            finally:
                os.close(fd)
        finally:
            if reader is not self._dir:
                reader.close()
        if not isinstance(raw, dict):
            raise ForgettingArchiveError("record is not an object")
        return raw

    def load(self, removal_id: str) -> RemovalRecord:
        if not isinstance(removal_id, str) or not _ID.fullmatch(removal_id):
            raise ForgettingArchiveError("not a removal record id")
        raw = self._read_json(f"{REMOVAL_PREFIX}{removal_id}.json")
        try:
            record = RemovalRecord(
                id=raw["id"],
                purpose=raw["purpose"],
                created_at=raw["created_at"],
                agent_id=raw["agent_id"],
                reason=raw["reason"],
                items=tuple(raw["items"]),
                items_sha256=raw["items_sha256"],
                format=raw["format"],
            )
        except (KeyError, TypeError):
            raise ForgettingArchiveError("record is incomplete") from None
        if (
            record.id != removal_id
            or record.format != _FORMAT
            or record.agent_id != self.layout.agent_id
            or any(
                not isinstance(item, dict) or item.get("store") not in _ITEM_STORES
                for item in record.items
            )
            or items_digest(list(record.items)) != record.items_sha256
        ):
            raise ForgettingArchiveError("record failed its integrity check")
        return record

    def load_forgetting(self, archive_id: str) -> dict[str, Any]:
        """A #320 record as stored (read only; integrity is checked by its own loader)."""
        if not isinstance(archive_id, str) or not _ID.fullmatch(archive_id):
            raise ForgettingArchiveError("not a forgetting archive id")
        return self._read_json(f"{archive_id}.json")

    def summaries(self) -> list[dict[str, Any]]:
        found = []
        for removal_id in self.ids():
            try:
                found.append(self.load(removal_id).summary())
            except ForgettingArchiveError as error:
                found.append({"id": removal_id, "error": str(error)})
        return found

    # -- writing --------------------------------------------------------------

    @staticmethod
    def new_id(at: datetime) -> str:
        return f"{at:%Y%m%dT%H%M%SZ}-{secrets.token_hex(8)}"

    def save(
        self, purpose: str, reason: str, items: list[dict[str, Any]], at: datetime
    ) -> RemovalRecord:
        """Write a record (never over another) and prove it reads back intact."""
        directory, _log = self._require()
        record = RemovalRecord(
            id=self.new_id(at),
            purpose=purpose,
            created_at=at.isoformat(),
            agent_id=self.layout.agent_id,
            reason=reason,
            items=tuple(items),
            items_sha256=items_digest(items),
        )
        name = f"{REMOVAL_PREFIX}{record.id}.json"
        data = (canonical_json(record.as_json()) + "\n").encode("utf-8")
        try:
            fd = directory.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except OSError as error:
            raise ForgettingArchiveError(
                f"removal record not written ({type(error).__name__})"
            ) from None
        try:
            _write_all(fd, data)
            os.fsync(fd)
        except OSError as error:
            os.close(fd)
            directory.unlink(name)
            raise ForgettingArchiveError(
                f"removal record not written ({type(error).__name__})"
            ) from None
        os.close(fd)
        directory.sync()
        try:
            intact = self.load(record.id) == record
        except ForgettingArchiveError:
            intact = False
        if not intact:
            directory.unlink(name)
            raise ForgettingArchiveError("removal record did not read back intact")
        return record

    def remove(self, removal_id: str) -> None:
        if not _ID.fullmatch(str(removal_id)):
            raise ForgettingArchiveError("not a removal record id")
        directory, _log = self._require()
        directory.unlink(f"{REMOVAL_PREFIX}{removal_id}.json")

    def remove_forgetting(self, archive_id: str) -> None:
        if not _ID.fullmatch(str(archive_id)):
            raise ForgettingArchiveError("not a forgetting archive id")
        directory, _log = self._require()
        directory.unlink(f"{archive_id}.json")

    # -- delete jobs (memory_forget): small JSON state files, replaced atomically --

    def job_ids(self) -> list[str]:
        found = []
        for name in self._names():
            match = _JOB_NAME.fullmatch(name)
            if match:
                found.append(match.group(1))
        return found

    def load_job(self, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ForgettingArchiveError("not a delete job id")
        return self._read_json(f"{JOB_PREFIX}{job_id}.json")

    def save_job(self, job: dict[str, Any]) -> None:
        """Replace ``delete-<id>.json`` atomically (temporary file, fsync, rename)."""
        job_id = job["id"]
        if not _ID.fullmatch(str(job_id)):
            raise ForgettingArchiveError("not a delete job id")
        directory, _log = self._require()
        name = f"{JOB_PREFIX}{job_id}.json"
        temporary = f".{name}.{secrets.token_hex(4)}.tmp"
        data = (canonical_json(job) + "\n").encode("utf-8")
        try:
            fd = directory.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except OSError as error:
            raise _open_error(error, "delete job") from None
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if directory.fd is not None:
                os.rename(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
            else:
                os.replace(Path(self.directory) / temporary, Path(self.directory) / name)
        except OSError as error:
            directory.unlink(temporary)
            raise ForgettingArchiveError(f"delete job not saved ({type(error).__name__})") from None
        directory.sync()

    def size(self) -> int:
        total = 0
        for name in self._names():
            try:
                total += os.lstat(Path(self.directory) / name).st_size
            except OSError:
                continue
        return total
