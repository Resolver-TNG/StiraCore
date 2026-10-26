"""The ``authority-epoch`` store (ADR 0011 §5): one small file in the data directory.

Format (JSON, ``v`` = 1): ``{"v": 1, "records": [...]}`` with the latest
:data:`KEEP_RECORDS` records, oldest first. A record is ``{"seq", "epoch", "prev",
"hash"}``: ``seq`` grows by one per write, ``epoch`` is the authority epoch written (it
only grows), ``prev`` is the hash of the previous record (64 zeros for ``seq`` 1) and
``hash`` is ``sha256("stiracore-authority-epoch:v1:<seq>:<epoch>:<prev>")``. No
incarnation is ever written (it is never read back). W2 records also carry the latch
store **commitment** ``latch_gen`` / ``latch_head`` / ``latch_store`` (the latch store's
newest generation, record hash and random store id; hashed as
``...:v2:<seq>:<epoch>:<prev>:<latch_gen>:<latch_head>:<latch_store>``; ``latch_gen``
never decreases along the chain): a
latch store that is missing, older or different from it at the next start is refused
(:mod:`.latches`). Once a record carries it, every later one must; a record that only
moves the commitment may repeat the epoch.

**Write**: a new temporary file in the same directory (``O_EXCL``, ``O_NOFOLLOW``, mode
``0600``), written, flushed (``F_FULLFSYNC`` on macOS), renamed over the file, then the
directory flushed (POSIX). Any failure raises ``OSError``.

**Read** (:func:`load`): ``missing`` (a first start) or ``ok`` or a reason code — the
file is not a regular, singly linked file of this user with mode ``0600`` or tighter, by
the path or by the opened descriptor (``epoch_store_unsafe``), cannot be read (``epoch_store_unreadable``), is not the format
(``epoch_store_malformed``), a hash or a ``prev`` link does not match
(``epoch_store_hash_mismatch``), a ``seq`` is skipped or repeated
(``epoch_store_sequence_gap``) or an epoch does not grow (``epoch_store_not_monotonic``).
Nothing here repairs a file: a person looks at it (``docs/authority-service.md``).

**Limits.** The hash chain detects corruption and a broken sequence, **not tampering**:
it is an unkeyed SHA-256, so anything running as the same user can write a new valid
chain. A process of the same user that can write the data directory can also swap the
file or the directory between two checks; the checks narrow that race (``lstat`` →
``open(O_NOFOLLOW | O_NONBLOCK)`` → ``fstat`` + inode comparison, and a re-read of the
current file before every replace), they do not close it. A missing file is a first
start (epoch 1). A rollback of the whole file to an older **valid** state is not
detectable here; that is why identity across starts is ``(incarnation, epoch)`` (ADR 0011
§5, §6, Open (6)).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from stiracore.durable_dir import fsync_dir

__all__ = [
    "EPOCH_FILE",
    "KEEP_RECORDS",
    "EpochRecord",
    "LoadResult",
    "load",
    "next_record",
    "write",
]

EPOCH_FILE = "authority-epoch"
KEEP_RECORDS = 16
_MAX_BYTES = 16384
_ZERO = "0" * 64
_HEX64 = re.compile(r"[0-9a-f]{64}")
_KEYS = frozenset({"seq", "epoch", "prev", "hash"})
_KEYS_V2 = _KEYS | {"latch_gen", "latch_head", "latch_store"}
_HEX32 = re.compile(r"[0-9a-f]{32}")
_MAX_INT = 1 << 53
_F_FULLFSYNC = 51  # macOS <sys/fcntl.h>
_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        errno.EINVAL,
        errno.ENOTTY,
    )
    if code is not None
)


@dataclass(frozen=True)
class EpochRecord:
    seq: int
    epoch: int
    prev: str
    hash: str
    # W2: the latch store commitment (its newest generation and record hash) — ``None``
    # in a W1 record. Once a record carries it, every later record must.
    latch_gen: int | None = None
    latch_head: str | None = None
    latch_store: str | None = None

    def as_dict(self) -> dict:
        out = {"seq": self.seq, "epoch": self.epoch, "prev": self.prev, "hash": self.hash}
        if self.latch_gen is not None:
            out["latch_gen"] = self.latch_gen
            out["latch_head"] = self.latch_head
            out["latch_store"] = self.latch_store
        return out

    @property
    def commitment(self) -> tuple[int, str, str] | None:
        if self.latch_gen is None:
            return None
        return (self.latch_gen, self.latch_head, self.latch_store)


@dataclass(frozen=True)
class LoadResult:
    status: str  # missing | ok | bad
    records: tuple[EpochRecord, ...] = ()
    reason: str | None = None
    identity: tuple[int, int] | None = None  # (st_dev, st_ino) of the file that was read

    @property
    def last(self) -> EpochRecord | None:
        return self.records[-1] if self.records else None


def _digest(seq: int, epoch: int, prev: str, latch=None) -> str:
    if latch is None:
        text = f"stiracore-authority-epoch:v1:{seq}:{epoch}:{prev}"
    else:
        text = f"stiracore-authority-epoch:v2:{seq}:{epoch}:{prev}:{latch[0]}:{latch[1]}:{latch[2]}"
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def next_record(
    last: EpochRecord | None, epoch: int, latch: tuple[int, str, str] | None = None
) -> EpochRecord:
    """The record after ``last``; ``latch`` = ``(latch_gen, latch_head, latch_store)``."""
    seq = 1 if last is None else last.seq + 1
    prev = _ZERO if last is None else last.hash
    digest = _digest(seq, epoch, prev, latch)
    if latch is None:
        return EpochRecord(seq, epoch, prev, digest)
    return EpochRecord(seq, epoch, prev, digest, latch[0], latch[1], latch[2])


def _positive(value) -> bool:
    return type(value) is int and 1 <= value < _MAX_INT


def _parse(raw: bytes) -> LoadResult:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return LoadResult("bad", reason="epoch_store_malformed")
    if (
        type(data) is not dict
        or set(data) != {"v", "records"}
        or type(data["v"]) is not int
        or data["v"] != 1
        or type(data["records"]) is not list
        or not 1 <= len(data["records"]) <= KEEP_RECORDS
    ):
        return LoadResult("bad", reason="epoch_store_malformed")
    records: list[EpochRecord] = []
    for item in data["records"]:
        if type(item) is not dict or set(item) not in (_KEYS, _KEYS_V2):
            return LoadResult("bad", reason="epoch_store_malformed")
        seq, epoch, prev, digest = item["seq"], item["epoch"], item["prev"], item["hash"]
        if not (_positive(seq) and _positive(epoch)):
            return LoadResult("bad", reason="epoch_store_malformed")
        if not all(type(v) is str and _HEX64.fullmatch(v) for v in (prev, digest)):
            return LoadResult("bad", reason="epoch_store_malformed")
        latch = None
        if "latch_gen" in item:
            gen, head, store = item["latch_gen"], item["latch_head"], item["latch_store"]
            if type(gen) is not int or not 0 <= gen < _MAX_INT:
                return LoadResult("bad", reason="epoch_store_malformed")
            if type(head) is not str or not _HEX64.fullmatch(head):
                return LoadResult("bad", reason="epoch_store_malformed")
            if type(store) is not str or not _HEX32.fullmatch(store):
                return LoadResult("bad", reason="epoch_store_malformed")
            latch = (gen, head, store)
        if digest != _digest(seq, epoch, prev, latch):
            return LoadResult("bad", reason="epoch_store_hash_mismatch")
        if records:
            before = records[-1]
            if seq != before.seq + 1:
                return LoadResult("bad", reason="epoch_store_sequence_gap")
            if prev != before.hash:
                return LoadResult("bad", reason="epoch_store_hash_mismatch")
            if before.commitment is not None and latch is None:
                return LoadResult("bad", reason="epoch_store_malformed")  # no way back to v1
            if before.commitment is not None and latch[0] < before.commitment[0]:
                return LoadResult("bad", reason="epoch_store_not_monotonic")  # never restarts
            # A record that only moves the latch commitment may repeat the epoch.
            same_epoch_ok = (
                epoch == before.epoch and latch is not None and latch != before.commitment
            )
            if epoch < before.epoch or (epoch == before.epoch and not same_epoch_ok):
                return LoadResult("bad", reason="epoch_store_not_monotonic")
        elif seq == 1 and prev != _ZERO:
            return LoadResult("bad", reason="epoch_store_hash_mismatch")
        records.append(
            EpochRecord(seq, epoch, prev, digest)
            if latch is None
            else EpochRecord(seq, epoch, prev, digest, latch[0], latch[1], latch[2])
        )
    first = records[0]
    # A trimmed file keeps the newest records; a short file starts at seq 1.
    if len(records) < KEEP_RECORDS and first.seq != 1:
        return LoadResult("bad", reason="epoch_store_sequence_gap")
    return LoadResult("ok", tuple(records))


def _unsafe(info: os.stat_result) -> bool:
    """Not a regular, singly linked file of this user with mode ``0600`` or tighter."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return True
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return True
    return os.name == "posix" and bool(info.st_mode & 0o077)


def load(data_dir: Path) -> LoadResult:
    """Read and verify the file. The path is checked with ``lstat``, then opened without
    following a symlink and without blocking (a FIFO), and the **descriptor** is checked
    again (``fstat``: the same metadata rules and the same device / inode as the path),
    so a file swapped between the two steps is refused, not read."""
    path = Path(data_dir) / EPOCH_FILE
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return LoadResult("missing")
    except OSError:
        return LoadResult("bad", reason="epoch_store_unreadable")
    if stat.S_ISLNK(info.st_mode) or _unsafe(info):
        return LoadResult("bad", reason="epoch_store_unsafe")
    if info.st_size > _MAX_BYTES:
        return LoadResult("bad", reason="epoch_store_malformed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno == getattr(errno, "ELOOP", None):
            return LoadResult("bad", reason="epoch_store_unsafe")  # swapped for a symlink
        return LoadResult("bad", reason="epoch_store_unreadable")
    try:
        try:
            opened = os.fstat(fd)
        except OSError:
            return LoadResult("bad", reason="epoch_store_unreadable")
        if _unsafe(opened) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return LoadResult("bad", reason="epoch_store_unsafe")
        if opened.st_size > _MAX_BYTES:
            return LoadResult("bad", reason="epoch_store_malformed")
        try:
            raw = os.read(fd, _MAX_BYTES + 1)
        except OSError:
            return LoadResult("bad", reason="epoch_store_unreadable")
    finally:
        os.close(fd)
    result = _parse(raw)
    if result.status != "ok":
        return result
    return LoadResult("ok", result.records, identity=(opened.st_dev, opened.st_ino))


def _flush(fd: int) -> None:
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError as error:
            if error.errno not in _UNSUPPORTED:
                raise
    os.fsync(fd)


def write(data_dir: Path, records: tuple[EpochRecord, ...]) -> tuple[int, int]:
    """Replace the file with ``records`` (the newest :data:`KEEP_RECORDS`), durably; the
    new file's ``(st_dev, st_ino)``. The caller checks the current file first
    (:func:`load`): this function replaces whatever is there."""
    directory = Path(data_dir)
    path = directory / EPOCH_FILE
    kept = records[-KEEP_RECORDS:]
    body = json.dumps(
        {"v": 1, "records": [record.as_dict() for record in kept]}, sort_keys=True
    ).encode("ascii")
    tmp = directory / f".{EPOCH_FILE}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            _flush(fd)
            written = os.fstat(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(directory, _flush)
    return (written.st_dev, written.st_ino)
