"""Open a checked write target without trusting the path between check and write.

Shared by Stira's ``editor`` sandbox (#245/#261) and the upstream ``file_write``
bridge (#242). The policy already decided the path. Here the file is opened
first, without truncating and without following a final symlink. The policy is
then asked again: it resolves the path *now*, so a parent directory swapped for
a symlink before the open shows up as another target or as the floor (#271).
Only the inode that was opened may be the one the path names now, and only then
is it truncated. A file this call created and then refused is removed again. A
check-then-act race window remains between the re-check and the truncate (both
on the same open descriptor, so the bytes go to the inode that was checked).
This is not an OS sandbox.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
from collections.abc import Iterable
from typing import Any

NOT_APPROVED = "This write was not approved by Stira's tool policy."


def open_checked(path: str, policies: Iterable[Any], write_path: str) -> int:
    """A descriptor for writing ``path`` (truncated), or ``PermissionError``."""
    if os.fspath(path) != write_path:
        raise PermissionError(NOT_APPROVED)
    # Non-blocking open (#601 C8): a FIFO in the path's place would otherwise block the
    # open until a reader appears, before the regular-file check below could refuse it.
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    info = None
    try:
        descriptor = os.open(path, flags | os.O_EXCL, 0o666)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, flags, 0o666)
        except OSError as error:
            if error.errno == errno.ENXIO:  # a FIFO without a reader
                raise PermissionError(NOT_APPROVED) from None
            raise
    try:
        info = os.fstat(descriptor)
        if not stat_module.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise PermissionError(NOT_APPROVED)
        _set_blocking(descriptor)
        for policy in policies:
            decision = policy.check(path)
            if not decision.allowed:
                raise PermissionError(decision.reason)
            if decision.path != write_path:  # the target changed since the check
                raise PermissionError(NOT_APPROVED)
        now = os.stat(write_path)
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise PermissionError(NOT_APPROVED)
        os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        if created:
            _remove_if_same(path, info)
        raise
    return descriptor


def _set_blocking(descriptor: int) -> None:
    """Back to ordinary (blocking) writes once the file is known to be regular."""
    set_blocking = getattr(os, "set_blocking", None)
    if set_blocking is not None and getattr(os, "O_NONBLOCK", 0):
        set_blocking(descriptor, True)


def _remove_if_same(path: str, info: os.stat_result | None) -> None:
    """Undo our own empty file, but never a file that someone else put there."""
    if info is None:
        return
    try:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            os.unlink(path)
    except OSError:
        pass


__all__ = ["NOT_APPROVED", "open_checked"]
