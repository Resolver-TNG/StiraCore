"""Owner lock files: ``server.lock``, ``heartbeat.lock`` and ``scheduler.lock`` (#322).

One implementation for every owner lock of a data directory (#285 server, #238
heartbeat, #239 scheduler; #300: no third copy). Each is an exclusive, non-blocking
OS file lock (``flock``; ``msvcrt.locking`` on Windows, unverified) that the kernel
releases when the owner exits, however it exits. The file holds the owner's PID as a
note; the kernel lock, not the note, decides who owns it.

Opening the file never follows a symlink at its last component (``O_NOFOLLOW``),
never blocks on a FIFO (``O_NONBLOCK``), and the opened descriptor must be a regular
file with one link, owned by this user; anything else is refused
(:class:`UnsafeLockFileError`) without writing to it. The note is only truncated after
the kernel lock is held, so a refused or contended open never changes the file.

**Windows has no ``O_NOFOLLOW``**: there a symlink named like the lock is followed and
only the descriptor checks (regular file, one link) apply; the owner check is POSIX
only. Unverified on Windows, to be revisited after testing on a Windows machine.

Not covered (operator's responsibility): whoever can replace the lock file's parent
directory — the data directory itself — can make a second owner lock a different file.
The data directory's permissions are the boundary (docs/operations.md).
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# flock reports contention as EWOULDBLOCK/EAGAIN; msvcrt.locking as EACCES/EDEADLOCK.
_CONTENTION_ERRNOS = frozenset(
    code
    for code in (
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EACCES,
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)
_OPEN_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)  # absent on Windows (see the module notes)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
# O_NOFOLLOW on a symlink: ELOOP (Linux, macOS), EMLINK (FreeBSD).
_SYMLINK_ERRNOS = frozenset({errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)})


class UnsafeLockFileError(RuntimeError):
    """The lock file is not a plain file of ours (symlink, hard link, other owner…).

    ``reason`` is a fixed phrase (no path). Nothing was written to the file.
    """

    def __init__(self, path: Path, reason: str, message: str | None = None) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            message
            or f"the lock file {path.name} is unsafe ({reason}); not taking it. "
            "Remove it while nothing runs, then start again."
        )


def _try_lock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only (unverified)
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only (unverified)
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _check_descriptor(fd: int) -> str | None:
    """Why the opened lock file is refused, or ``None`` when it is acceptable."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_nlink != 1:
        return f"{info.st_nlink} hard links"
    if os.name != "nt" and info.st_uid != os.geteuid():
        return "owned by another user"
    return None


def read_note_pid(path: str | os.PathLike) -> int | None:
    """The PID note in a lock file, or ``None`` (missing, empty, unreadable, not a PID).

    Opened like the lock itself (no symlink, no FIFO wait); only a regular file is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | (_OPEN_FLAGS & ~(os.O_RDWR | os.O_CREAT)))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 32).decode("ascii", errors="replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return int(text) if text.isdigit() and int(text) > 0 else None


def read_private_file(path: str | os.PathLike, limit: int = 65536) -> bytes:
    """Read a small state file with the lock file's rules (#273: the tool-rule anchor).

    No final symlink (``O_NOFOLLOW``), no FIFO wait; the descriptor must be a regular
    file with one link, owned by this user (:func:`_check_descriptor`). Absent:
    ``FileNotFoundError``. Refused (symlink, hard link, other owner, larger than
    ``limit``): :class:`UnsafeLockFileError`. Windows: as in the module notes.
    """
    target = Path(path)
    try:
        fd = os.open(target, os.O_RDONLY | (_OPEN_FLAGS & ~(os.O_RDWR | os.O_CREAT)))
    except OSError as error:
        if error.errno in _SYMLINK_ERRNOS:
            raise UnsafeLockFileError(target, "symlink", f"{target.name} is a symlink") from None
        raise
    try:
        reason = _check_descriptor(fd)
        if reason is not None:
            raise UnsafeLockFileError(target, reason, f"{target.name} is unsafe ({reason})")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise UnsafeLockFileError(target, "too large", f"{target.name} is too large")
    return b"".join(chunks)


class OwnerLockFile:
    """Exclusive, non-blocking owner lock on one file (the shared implementation).

    Subclasses choose the errors: :meth:`_contended` (another owner holds the kernel
    lock) and :meth:`_unsafe` (the file is refused), and may track ownership in
    :meth:`_acquired` / :meth:`_released`. Every failure closes the descriptor.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    # -- hooks ------------------------------------------------------------------------

    def _contended(self) -> BaseException:
        return RuntimeError(f"{self.path.name} is held by another owner")

    def _unsafe(self, reason: str) -> BaseException:
        return UnsafeLockFileError(self.path, reason)

    def _acquired(self) -> None:
        """Called once the kernel lock is held and recorded (before the note)."""

    def _released(self) -> None:
        """Called on release, before the kernel lock is dropped."""

    # -- lock -------------------------------------------------------------------------

    def acquire(self) -> None:
        """Take the lock or raise.

        :meth:`_contended`'s error: another owner holds it. :meth:`_unsafe`'s error:
        the path is a symlink or a directory, or the opened file is not a regular,
        singly linked file of this user; nothing is written. Any other ``OSError``
        (permissions, I/O) propagates unchanged.
        """
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, _OPEN_FLAGS, 0o600)
        except OSError as error:
            if error.errno in _SYMLINK_ERRNOS:
                raise self._unsafe("a symbolic link") from None
            if error.errno == errno.EISDIR:
                raise self._unsafe("a directory") from None
            raise
        try:
            problem = _check_descriptor(fd)
            if problem is not None:
                raise self._unsafe(problem)
            _try_lock(fd)
        except OSError as error:
            os.close(fd)
            if error.errno in _CONTENTION_ERRNOS:
                raise self._contended() from None
            raise
        except BaseException:
            os.close(fd)
            raise
        # Ownership is recorded before anything else can fail, so release() can
        # always undo it. A failing hook undoes it here (no descriptor left behind).
        self._fd = fd
        try:
            self._acquired()
        except BaseException:
            self._fd = None
            try:
                _unlock(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
            raise
        # The note is informational (the kernel lock decides); a failed write keeps
        # the lock. It replaces whatever a crashed owner left behind.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError as error:
            logger.info(
                "%s acquired; PID note not written (%s)", self.path.name, type(error).__name__
            )

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        # The descriptor is closed (the kernel lock dropped) even if a hook or the
        # unlock fails; the hook's error still propagates.
        try:
            self._released()
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)
