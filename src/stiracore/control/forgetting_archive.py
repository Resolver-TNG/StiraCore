"""Archive and audit log that make Semantic forgetting reversible (#294).

``--apply-forgetting`` used to overwrite a Semantic entry with its head and tail and
keep nothing else; the episode it came from carries a harvest mark, so it was never
distilled again. Now every overwrite is preceded by an archive record and is refused
when the record cannot be written and read back (fail-closed). The only way back is
the explicit undo (``distillation_cli --undo-forgetting <id>``); there is no automatic
restore.

Where it lives: ``<memory_dir>/forgotten/`` next to the layer directories, *not* inside
``semantic/``. So the memory tools (``memory_read`` / ``memory_search``), Harvest and the
Semantic index never see it, and it is inside the data directory, which is on the read
and write floor of the file tools (#271/#290, #312/#316: only ``workspace/``, the
episodic layer and ``context-stash/`` are reachable). The directory is created ``0700``
and every file ``0600`` (POSIX; on Windows the ACL of the data directory applies).

Every file in the archive is opened without following symbolic links (``O_NOFOLLOW``,
relative to a descriptor of the directory on POSIX) and must be a regular file owned by
this user with no group/other permission bits; anything else is refused. The directory
is fsynced after a record is created or removed and after the log is created. On Windows
(no ``O_NOFOLLOW``/``dir_fd``) a link is refused by an ``lstat`` check before the open
(best effort) and permissions are left to the data directory's ACL.

An archive record keeps the **whole original Markdown** (frontmatter included), so it
can contain whatever the memory contained, including secrets. Deleting a memory for
good therefore means deleting its archive records too (see docs/operations.md §12).

The audit log (``forgetting-log.jsonl``) is a hash-chained JSONL of ids, keys, stages
and SHA-256 digests, never content. It is evidence only: a broken chain refuses further
forgetting and undo, and nothing reads a permission back from it. Without a head anchor
a removed *last* line is not detectable (unlike the scheduler log).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from stiracore.core.approval import canonical_json
from stiracore.durable_dir import fsync_dir
from stiracore.observability.masking import mask_secrets
from stiracore.tools.memory_store import MemoryLayer

__all__ = [
    "ARCHIVE_DIRNAME",
    "AUDIT_LOG_NAME",
    "ArchiveRecord",
    "ForgettingArchive",
    "ForgettingArchiveError",
    "sha256_text",
]

ARCHIVE_DIRNAME = "forgotten"
AUDIT_LOG_NAME = "forgetting-log.jsonl"
_FORMAT = 1
_GENESIS = "0" * 64
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_STAGES = frozenset({"none", "light", "compact", "archive"})
_LINE_KEYS = frozenset({"seq", "ts", "kind", "body", "prev", "hash"})
_KINDS = frozenset(
    {
        "forget.started",
        "forget.applied",
        "forget.refused",
        "forget.recovered",
        "forget.abandoned",
        "undo.started",
        "undo.applied",
        "undo.refused",
        "undo.recovered",
        # Removal records of retention and delete requests (removal_archive, #294).
        "remove.started",
        "remove.applied",
        "remove.refused",
        "restore.started",
        "restore.applied",
        "restore.refused",
        "purge.applied",
        # Delete requests (memory_forget, #294): a resumable job, irreversible.
        "delete.started",
        "delete.completed",
        "delete.failed",
        # Importance tags set by a human (``stira memory tag --apply``, #173).
        "retention.tagged",
    }
)
_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Opening a FIFO/device must not block before fstat can refuse it.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_POSIX = os.name == "posix"


class ForgettingArchiveError(RuntimeError):
    """The archive or its audit log cannot be trusted or written; nothing is changed."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shown(value: Any) -> str | None:
    """A key/reason as the audit log may hold it: masked as a whole, cut to 200."""
    if value is None:
        return None
    text = str(value)
    if mask_secrets(text) != text:
        return "[masked]"
    return text[:200]


@dataclass(frozen=True)
class ArchiveRecord:
    id: str
    key: str
    stage: str
    previous_stage: str
    applied_at: str
    age_days: int
    last_accessed: str
    reason: str
    source_id: str | None
    original_sha256: str
    compressed_sha256: str
    original_text: str = field(repr=False)
    agent_id: str = ""
    layer: str = "semantic"
    format: int = _FORMAT

    def summary(self) -> dict[str, Any]:
        """Everything except the archived text (safe to print)."""
        data = asdict(self)
        data.pop("original_text")
        data["key"] = _shown(self.key)
        data["source_id"] = _shown(self.source_id)
        return data


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


class _Directory:
    """The archive directory, held open on POSIX; files are opened relative to it."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        self.fd: int | None = None
        if path.is_symlink():
            raise ForgettingArchiveError("archive directory is a symbolic link")
        if path.exists() and not path.is_dir():
            raise ForgettingArchiveError("archive path exists and is not a directory")
        if create and not path.exists():
            try:
                path.mkdir(mode=0o700)
                _fsync_directory(path.parent)  # the new entry in memory_dir survives a crash
            except FileExistsError:
                pass
            except OSError as error:
                raise ForgettingArchiveError(
                    f"archive directory not usable ({type(error).__name__})"
                ) from None
        if not _POSIX:
            if path.is_symlink() or not path.is_dir():
                raise ForgettingArchiveError("archive directory is not a real directory")
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
        try:
            self.fd = os.open(path, flags)
        except OSError as error:
            raise ForgettingArchiveError(
                f"archive directory not usable ({type(error).__name__})"
            ) from None
        info = os.fstat(self.fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            self.close()
            raise ForgettingArchiveError("archive directory is not a directory owned by this user")
        if create and stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(self.fd, 0o700)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def open(self, name: str, flags: int) -> int:
        """Open ``name`` without following a link; the result is a private regular file."""
        flags |= _NOFOLLOW | _NONBLOCK | _BINARY
        if self.fd is not None:
            fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        else:
            target = self.path / name
            if target.is_symlink():
                raise ForgettingArchiveError(f"{name} is a symbolic link")
            fd = os.open(target, flags, 0o600)
        try:
            self.check(fd, name)
            if _NONBLOCK:
                os.set_blocking(fd, True)  # a regular file: back to normal I/O
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def check(fd: int, name: str) -> os.stat_result:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ForgettingArchiveError(f"{name} is not a regular file")
        if _POSIX and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ForgettingArchiveError(f"{name} is not private (owner-only 0600 expected)")
        return info

    def unlink(self, name: str) -> None:
        try:
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                (self.path / name).unlink()
        except FileNotFoundError:
            return
        self.sync()

    def names(self) -> list[str]:
        return sorted(os.listdir(self.fd if self.fd is not None else self.path))

    def sync(self) -> None:
        """fsync the directory so a created/removed entry survives a crash (POSIX)."""
        if self.fd is not None:
            os.fsync(self.fd)


def _fsync_directory(path: Path) -> None:
    """fsync a directory (POSIX); a no-op where directories cannot be opened."""
    fsync_dir(path)


def _open_error(error: OSError, name: str) -> ForgettingArchiveError:
    # O_NOFOLLOW on a link: ELOOP (Linux, macOS) or EMLINK (FreeBSD).
    if error.errno in (errno.ELOOP, errno.EMLINK):
        return ForgettingArchiveError(f"{name} is a symbolic link")
    if error.errno == errno.ENXIO:  # O_WRONLY|O_NONBLOCK on a FIFO without a reader
        return ForgettingArchiveError(f"{name} is not a regular file")
    return ForgettingArchiveError(f"{name} not usable ({type(error).__name__})")


class _AuditLog:
    """Append-only JSONL, ``hash = sha256(prev || canonical([seq, ts, kind, body]))``."""

    def __init__(self, directory: _Directory) -> None:
        self.directory = directory
        self._seq = 0
        self._prev = _GENESIS
        self._size = 0
        self._identity: tuple[int, int] | None = None
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def _hash(prev: str, seq: int, ts: float, kind: str, body: dict[str, Any]) -> str:
        material = prev + "\n" + canonical_json([seq, ts, kind, body])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def open(self) -> list[dict[str, Any]]:
        try:
            fd = self.directory.open(AUDIT_LOG_NAME, os.O_RDONLY)
        except FileNotFoundError:
            data = b""
        except OSError as error:
            raise _open_error(error, "audit log") from None
        else:
            try:
                info = os.fstat(fd)
                self._identity = (info.st_dev, info.st_ino)
                data = _read_all(fd)
            finally:
                os.close(fd)
        if data and not data.endswith(b"\n"):
            raise ForgettingArchiveError("audit log ends with a partial line")
        events: list[dict[str, Any]] = []
        prev, seq = _GENESIS, 0
        for raw in data.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                raise ForgettingArchiveError(f"audit log line {seq + 1} is not JSON") from None
            if (
                not isinstance(line, dict)
                or set(line) != _LINE_KEYS
                or line["seq"] != seq + 1
                or line["prev"] != prev
                or not isinstance(line["body"], dict)
                or not isinstance(line["kind"], str)
                or not isinstance(line["ts"], (int, float))
            ):
                raise ForgettingArchiveError(f"audit log chain broken at line {seq + 1}")
            expected = self._hash(prev, line["seq"], line["ts"], line["kind"], line["body"])
            if line["hash"] != expected:
                raise ForgettingArchiveError(f"audit log hash mismatch at line {seq + 1}")
            prev, seq = expected, line["seq"]
            events.append(line)
        self._seq, self._prev, self._size = seq, prev, len(data)
        self.events = events
        return events

    def append(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind not in _KINDS:
            raise ValueError(f"unknown forgetting audit kind: {kind}")
        seq, ts = self._seq + 1, round(time.time(), 6)
        digest = self._hash(self._prev, seq, ts, kind, body)
        line = {"seq": seq, "ts": ts, "kind": kind, "body": body, "prev": self._prev}
        line["hash"] = digest
        encoded = (canonical_json(line) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        try:
            fd = self.directory.open(AUDIT_LOG_NAME, flags)
        except OSError as error:
            raise _open_error(error, "audit log") from None
        try:
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if self._identity is not None and identity != self._identity:
                raise ForgettingArchiveError("audit log was replaced outside this process")
            if info.st_size != self._size:
                raise ForgettingArchiveError("audit log changed outside this process")
            _write_all(fd, encoded)
            os.fsync(fd)
        except OSError as error:
            raise ForgettingArchiveError(
                f"audit log not writable ({type(error).__name__})"
            ) from None
        finally:
            os.close(fd)
        if self._identity is None:
            self._identity = identity
            self.directory.sync()
        self._seq, self._prev, self._size = seq, digest, self._size + len(encoded)
        self.events.append(line)
        return line


class ForgettingArchive:
    """``<memory_dir>/forgotten/<id>.json`` records plus the audit log."""

    def __init__(self, store) -> None:
        self.store = store
        self.memory_dir = Path(store.memory_dir)
        self.directory = self.memory_dir / ARCHIVE_DIRNAME
        self._dir: _Directory | None = None
        self._log: _AuditLog | None = None

    # -- preparation ---------------------------------------------------------

    def prepare(self) -> None:
        """Create/check the directory and verify the log; raise when unusable."""
        self.close()
        try:
            self.store._reject_symlinks(self.directory)
        except ValueError:
            raise ForgettingArchiveError("archive directory is a symbolic link") from None
        directory = _Directory(self.directory, create=True)
        try:
            if not self.directory.resolve().is_relative_to(self.memory_dir.resolve()):
                raise ForgettingArchiveError("archive directory leaves the memory directory")
            if not os.access(self.directory, os.W_OK | os.X_OK):
                raise ForgettingArchiveError("archive directory is not writable")
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

    def _reader(self) -> _Directory:
        """The prepared directory, or a read-only view of it (listing, loading)."""
        if self._dir is not None:
            return self._dir
        self._dir = _Directory(self.directory, create=False)
        return self._dir

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

    def logged(self) -> list[dict[str, Any]]:
        """Events of the prepared log (as verified at ``prepare`` plus own appends)."""
        return list(self._require()[1].events)

    def events(self) -> list[dict[str, Any]]:
        """Read and verify the log without creating anything (operator tooling, tests)."""
        if not self.directory.is_dir():
            return []
        directory = _Directory(self.directory, create=False)
        try:
            return _AuditLog(directory).open()
        finally:
            directory.close()

    # -- records -------------------------------------------------------------

    @staticmethod
    def new_id(key: str, original_sha256: str, stage: str, at: datetime) -> str:
        material = "\0".join((key, original_sha256, stage, at.isoformat(), str(time.time_ns())))
        return f"{at:%Y%m%dT%H%M%SZ}-{sha256_text(material)[:16]}"

    @staticmethod
    def _name(archive_id: str) -> str:
        if not isinstance(archive_id, str) or not _ID.fullmatch(archive_id):
            raise ForgettingArchiveError("not a forgetting archive id")
        return f"{archive_id}.json"

    def save(self, record: ArchiveRecord) -> ArchiveRecord:
        """Write the record (never over another one) and prove it reads back intact."""
        directory, _log = self._require()
        if sha256_text(record.original_text) != record.original_sha256:
            raise ForgettingArchiveError("archive record does not match its digest")
        name = self._name(record.id)
        data = (json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        try:
            fd = directory.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except OSError as error:  # FileExistsError: never touch another record
            raise ForgettingArchiveError(
                f"archive record not written ({type(error).__name__})"
            ) from None
        try:
            _write_all(fd, data)
            os.fsync(fd)
        except OSError as error:
            os.close(fd)
            directory.unlink(name)
            raise ForgettingArchiveError(
                f"archive record not written ({type(error).__name__})"
            ) from None
        os.close(fd)
        directory.sync()
        try:
            intact = self.load(record.id) == record
        except ForgettingArchiveError:
            intact = False
        if not intact:
            directory.unlink(name)  # a damaged copy is no archive
            raise ForgettingArchiveError("archive record did not read back intact")
        return record

    def load(self, archive_id: str) -> ArchiveRecord:
        directory = self._reader()
        name = self._name(archive_id)
        try:
            fd = directory.open(name, os.O_RDONLY)
        except FileNotFoundError:
            raise ForgettingArchiveError("no archive record with this id") from None
        except OSError as error:
            raise _open_error(error, "archive record") from None
        try:
            raw = json.loads(_read_all(fd).decode("utf-8"))
            record = ArchiveRecord(**raw)
        except (OSError, ValueError, TypeError) as error:
            raise ForgettingArchiveError(
                f"archive record unreadable ({type(error).__name__})"
            ) from None
        finally:
            os.close(fd)
        if (
            record.id != archive_id
            or record.format != _FORMAT
            or not _DIGEST.fullmatch(str(record.original_sha256))
            or not _DIGEST.fullmatch(str(record.compressed_sha256))
            or not isinstance(record.original_text, str)
            or sha256_text(record.original_text) != record.original_sha256
            or record.stage not in _STAGES
            or record.previous_stage not in _STAGES
        ):
            raise ForgettingArchiveError("archive record failed its integrity check")
        if record.layer != "semantic" or record.agent_id != self.store.agent_id:
            raise ForgettingArchiveError("archive record belongs to another layer or agent")
        try:
            # The restore target must be a Semantic entry of *this* store: no "..",
            # absolute path, separator tricks or symbolic link (MemoryStore._path).
            self.store._path(MemoryLayer.SEMANTIC, record.key)
        except (TypeError, ValueError):
            raise ForgettingArchiveError(
                "archive record names a key outside the Semantic memory of this store"
            ) from None
        return record

    def remove(self, archive_id: str) -> None:
        directory, _log = self._require()
        directory.unlink(self._name(archive_id))

    def ids(self) -> list[str]:
        names = self._reader().names()
        return [n[:-5] for n in names if n.endswith(".json") and _ID.fullmatch(n[:-5])]

    def summaries(self) -> list[dict[str, Any]]:
        """One printable summary per record (no text); a damaged one shows its error."""
        if not self.directory.is_dir() or self.directory.is_symlink():
            return []
        found: list[dict[str, Any]] = []
        try:
            ids = self.ids()
        except ForgettingArchiveError as error:
            return [{"id": None, "error": str(error)}]
        for archive_id in ids:
            try:
                found.append(self.load(archive_id).summary())
            except ForgettingArchiveError as error:
                found.append({"id": archive_id, "error": str(error)})
        return found
