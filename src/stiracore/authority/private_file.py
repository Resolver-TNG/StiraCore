"""Safe open / read / replace / append for the authority's small files (ADR 0011 §5, §12).

The same rules as the ``authority-epoch`` store (:mod:`.epoch_store`), shared by the latch
store, the lifecycle log, the outbox and the watchdog's last-pass record (W2):

* **read**: the path is checked with ``lstat``, opened without following a symlink and
  without blocking (a FIFO), and the opened **descriptor** is checked again (regular file,
  this user, mode ``0600`` or tighter, one link, the same device / inode as the path);
  anything else is ``unsafe`` and the file is not read (and never written);
* **replace**: a new temporary file in the same directory (``O_EXCL``, ``O_NOFOLLOW``,
  ``0600``), written, flushed (``F_FULLFSYNC`` on macOS), renamed over the file, then the
  directory flushed;
* **append** (the lifecycle log): the file is opened ``O_APPEND | O_NOFOLLOW |
  O_NONBLOCK`` (created ``0600`` with ``O_EXCL`` when missing), the descriptor checked as
  above and against the identity the caller saw last, one line written and flushed.

The identity is :func:`.epoch_store.identity` — device, inode, size and ``st_ctime_ns``
(#661: Linux reuses a freed inode number at once) — recorded by the writer after its own
last change, so a delete + recreate by someone else differs even with the same bytes.

Reason codes are ``<prefix>_unsafe`` / ``_unreadable`` / ``_malformed`` (the caller's
parser adds its own). Nothing here repairs a file. Same limits as the epoch store: an
unkeyed hash chain detects corruption, not tampering by the same user; the checks narrow
a swap race, they do not close it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from stiracore.durable_dir import fsync_dir

from .epoch_store import Identity, _flush, _unsafe, identity, install

__all__ = ["ReadResult", "Replaced", "append_line", "read", "replace"]


@dataclass(frozen=True)
class ReadResult:
    status: str  # missing | ok | bad
    raw: bytes = b""
    reason: str | None = None
    identity: Identity | None = None


def read(path: Path, *, max_bytes: int, prefix: str) -> ReadResult:
    """Read ``path`` under the rules above (at most ``max_bytes``; larger = malformed)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ReadResult("missing")
    except OSError:
        return ReadResult("bad", reason=f"{prefix}_unreadable")
    if stat.S_ISLNK(info.st_mode) or _unsafe(info):
        return ReadResult("bad", reason=f"{prefix}_unsafe")
    if info.st_size > max_bytes:
        return ReadResult("bad", reason=f"{prefix}_malformed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        if error.errno == getattr(errno, "ELOOP", None):
            return ReadResult("bad", reason=f"{prefix}_unsafe")
        return ReadResult("bad", reason=f"{prefix}_unreadable")
    try:
        try:
            opened = os.fstat(fd)
        except OSError:
            return ReadResult("bad", reason=f"{prefix}_unreadable")
        # The path and the descriptor are the same file (device / inode): a size or
        # change-time difference here is a concurrent append, not a swap; the full
        # identity returned below is compared with the writer's own by the caller.
        if _unsafe(opened) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return ReadResult("bad", reason=f"{prefix}_unsafe")
        if opened.st_size > max_bytes:
            return ReadResult("bad", reason=f"{prefix}_malformed")
        chunks = []
        total = 0
        try:
            while total <= max_bytes:
                chunk = os.read(fd, max_bytes + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except OSError:
            return ReadResult("bad", reason=f"{prefix}_unreadable")
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        return ReadResult("bad", reason=f"{prefix}_malformed")
    return ReadResult("ok", raw, identity=identity(opened))


def replace(directory: Path, name: str, body: bytes) -> Identity:
    """Replace ``directory / name`` with ``body``, durably; the new file's identity (taken
    after the rename). The caller checks the current file first (:func:`read`)."""
    directory = Path(directory)
    tmp = directory / f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            _flush(fd)
        except BaseException:
            os.close(fd)
            raise
        written = install(fd, tmp, directory / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(directory, _flush)
    return written


class Replaced(OSError):
    """The file is not the one the caller saw last (or is unsafe): nothing was written."""


def append_line(path: Path, line: bytes, expected: Identity | None) -> Identity:
    """Append one line (``line`` ends with ``\\n``) and flush it. ``expected`` is the
    identity the caller read or wrote last (``None``: the file must not exist yet and is
    created ``0600``). A different, unsafe or vanished file raises :class:`Replaced`;
    any I/O failure raises ``OSError``. Returns the file's identity after the append."""
    path = Path(path)
    base = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    created = expected is None
    try:
        fd = os.open(path, base | (os.O_CREAT | os.O_EXCL if created else 0), 0o600)
    except FileExistsError as error:
        raise Replaced("exists") from error
    except FileNotFoundError as error:
        raise Replaced("vanished") from error
    except OSError as error:
        if error.errno == getattr(errno, "ELOOP", None):
            raise Replaced("symlink") from error
        raise
    try:
        info = os.fstat(fd)
        if _unsafe(info):
            raise Replaced("unsafe")
        if not created and identity(info) != expected:
            raise Replaced("replaced")
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view) :]
        _flush(fd)
        written = identity(os.fstat(fd))
    finally:
        os.close(fd)
    if created:
        fsync_dir(path.parent, _flush)
    return written
