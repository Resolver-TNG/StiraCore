"""One server per data directory: an owner lock on ``<data_dir>/server.lock`` (#285).

The heartbeat (#238) and the scheduler (#239) each lock their own file, but the
HTTP server itself, the approval store (#240), the neural state, the checkpoints
and ``turns.db`` are shared by every process that opens the same directory. A
second server would list the first one's *live* approvals as restored and could
replay them in a different conversation. So the server takes this lock before it
opens anything in the directory and refuses to start without it (fail-closed).

Same implementation as ``heartbeat.lock`` and ``scheduler.lock``
(:mod:`stiracore.owner_lock`, #322): an exclusive, non-blocking OS file lock
(``flock``; ``msvcrt.locking`` on Windows, unverified) that the kernel releases
when the owner exits, however it exits. A lock file left behind by a crashed or
killed server is therefore reclaimed by the next start. The file holds the
owner's PID as a note for the refusal message; the kernel lock, not the note,
decides who owns the directory.

The lock is per open file, not per process: a second ``acquire`` in the *same*
process from another :class:`DataDirOwnerLock` is refused as well.

Opening the file never follows a symlink at its last component (``O_NOFOLLOW``),
never blocks on a FIFO (``O_NONBLOCK``), and the opened descriptor must be a
regular file with one link, owned by this user; anything else refuses the start
(:class:`UnsafeLockFileError`) without writing to it. The note is only truncated
after the kernel lock is held. **Windows has no ``O_NOFOLLOW``**: there a symlink
named ``server.lock`` is followed and only the descriptor checks (regular file,
one link) apply; the owner check is POSIX only. The start is still allowed there
(refusing would make the server unusable on Windows); unverified on Windows, to be
revisited after testing on a Windows machine.

Not covered (operator's responsibility): whoever can replace ``server.lock``'s
parent directory — the data directory itself — can make a second server lock a
different file. The data directory's permissions are the boundary.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import ClassVar

from stiracore import owner_lock
from stiracore.owner_lock import (  # noqa: F401 - re-exported (distillation_cli's probe)
    _SYMLINK_ERRNOS,
    OwnerLockFile,
    _check_descriptor,
    read_note_pid,
)

LOCK_FILENAME = "server.lock"


class DataDirLockError(RuntimeError):
    """Another server owns this data directory; this one must not start."""

    def __init__(
        self,
        path: Path,
        owner_pid: int | None,
        owner_alive: bool | None,
        *,
        same_process: bool = False,
    ) -> None:
        self.path = path
        self.owner_pid = os.getpid() if same_process else owner_pid
        self.owner_alive = True if same_process else owner_alive
        self.same_process = same_process
        owner_pid = self.owner_pid
        if same_process:
            owner = f"this process (pid {owner_pid})"
        elif owner_pid is None:
            # Unknown PID, e.g. on Windows, where the locked note cannot be read.
            owner = "another process"
        elif owner_alive is False:
            # The note names a process that is gone while the lock is still held (e.g.
            # a child inherited the descriptor, or a new owner has not written its
            # note yet). Still refused: the kernel lock decides, not the note.
            owner = f"pid {owner_pid} per the lock note, no longer running; the lock is held"
        else:
            owner = f"pid {owner_pid}"
        super().__init__(
            f"another server ({owner}) owns the data directory {path.parent}; not starting. "
            "Stop that server first, or use another data directory."
        )


class UnsafeLockFileError(DataDirLockError, owner_lock.UnsafeLockFileError):
    """``server.lock`` is not a plain file of ours (symlink, hard link, other owner…).

    Also an :class:`stiracore.owner_lock.UnsafeLockFileError` (the shared check).
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.owner_pid = None
        self.owner_alive = None
        self.same_process = False
        owner_lock.UnsafeLockFileError.__init__(
            self,
            path,
            reason,
            f"the data-directory lock file {path} is unsafe ({reason}); not starting. "
            "Remove it while no server runs, then start again.",
        )


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (signal 0; permission denied counts as alive)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows only (unverified)
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_owner_pid(path: str | os.PathLike) -> int | None:
    """The PID note in a lock file, or ``None`` (missing, empty, unreadable, not a PID).

    Opened like the lock itself (no symlink, no FIFO wait); only a regular file is read.
    """
    return read_note_pid(path)


class DataDirOwnerLock(OwnerLockFile):
    """Exclusive, non-blocking owner lock on ``<data_dir>/server.lock``.

    The shared owner-lock implementation (:class:`OwnerLockFile`, also behind
    ``heartbeat.lock`` and ``scheduler.lock``) with its own errors. Locks held by this
    process are also remembered by path, so a refusal can say "this process" without
    reading the note (which a Windows byte-range lock makes unreadable).

    :meth:`acquire` raises :class:`DataDirLockError` on contention and
    :class:`UnsafeLockFileError` (a subclass) when the path is a symlink or the file is
    not a regular, singly linked file of this user (nothing is written). Any other
    ``OSError`` (permissions, I/O) propagates unchanged: the server does not start
    either way.
    """

    _held_here: ClassVar[set[str]] = set()
    _held_here_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, data_dir: str | os.PathLike) -> None:
        super().__init__(Path(data_dir) / LOCK_FILENAME)

    def _contended(self) -> BaseException:
        with self._held_here_lock:
            same_process = self._key() in self._held_here
        pid = None if same_process else read_owner_pid(self.path)
        return DataDirLockError(
            self.path,
            pid,
            None if pid is None else pid_alive(pid),
            same_process=same_process,
        )

    def _unsafe(self, reason: str) -> BaseException:
        return UnsafeLockFileError(self.path, reason)

    def _acquired(self) -> None:
        with self._held_here_lock:
            self._held_here.add(self._key())

    def _released(self) -> None:
        with self._held_here_lock:
            self._held_here.discard(self._key())

    def _key(self) -> str:
        return os.path.normcase(os.path.realpath(self.path))
