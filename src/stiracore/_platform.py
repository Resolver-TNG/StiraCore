"""POSIX-only calls with explicit Windows behaviour (#540).

Python 3.11 on Windows has no ``os.fchmod`` (added in 3.13), ``os.geteuid``,
``os.killpg``, ``signal.SIGKILL``, ``os.O_NOFOLLOW``, ``AF_UNIX`` (in CPython's
Windows build) and no ``follow_symlinks=False`` for ``os.utime``. Calling them
unguarded raises ``AttributeError`` / ``NotImplementedError``.

What each helper does where the call is missing (never a silent weakening):

* :func:`fchmod_private` — Windows permission bits only express read-only; a
  file's privacy there comes from the ACL inherited from the data directory (the
  user's profile by default). The mode is skipped, nothing else changes. Documented
  in ``docs/windows.md``.
* :func:`utime_nofollow` — where ``os.utime`` cannot refuse a link itself, the path
  is ``lstat``-checked first and a symbolic link is refused (``OSError``). Racy
  against a concurrent swap, the same best effort as other Windows paths here.
* :func:`geteuid_or_none` — ``None`` off POSIX; callers must treat it as "ownership
  cannot be checked" and choose fail-closed or skip explicitly.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

__all__ = ["fchmod_private", "geteuid_or_none", "is_posix", "utime_nofollow"]


def is_posix() -> bool:
    return os.name == "posix" and not sys.platform.startswith("win")


def fchmod_private(fd: int, mode: int) -> None:
    """``os.fchmod(fd, mode)`` where it exists; a documented no-op on Windows."""
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def geteuid_or_none() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid is not None else None


def utime_nofollow(path: str | Path, atime_ns: int, mtime_ns: int) -> None:
    """Set times without following a final symbolic link (refuse one where unsupported)."""
    if os.utime in os.supports_follow_symlinks:
        os.utime(path, ns=(atime_ns, mtime_ns), follow_symlinks=False)
        return
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(errno.ELOOP, "refusing to follow a symbolic link", str(path))
    os.utime(path, ns=(atime_ns, mtime_ns))
