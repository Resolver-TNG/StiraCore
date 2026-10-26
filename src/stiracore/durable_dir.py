"""Directory fsync shared by the durable stores (#543).

After ``os.replace`` the new directory entry is only crash-durable once the directory
itself is flushed. POSIX does that by opening the directory and ``fsync``-ing it.
Windows cannot open a directory as a file descriptor (``os.open`` raises
``PermissionError``) and has no portable directory flush; there ``os.replace`` is the
whole guarantee, so the directory step is skipped. The file data itself is still
flushed by each caller before the rename — only the directory step is platform-gated.

On POSIX a failure stays a failure (fail-closed): the error propagates to the caller.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

__all__ = ["fsync_dir"]


def _supported() -> bool:
    # Windows cannot open a directory with os.open. Evaluated per call so tests can simulate Windows via ``sys.platform``.
    return os.name == "posix" and sys.platform != "win32"


def fsync_dir(path: str | os.PathLike[str], flush: Callable[[int], None] | None = None) -> None:
    """Flush ``path`` (a directory) so renames in it survive power loss.

    ``flush`` lets a caller keep a stronger primitive (``F_FULLFSYNC`` on macOS in the
    YOLO window). No-op where directories cannot be opened (Windows).
    """
    if not _supported():
        return
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        (flush or os.fsync)(descriptor)  # os.fsync looked up per call (test spies)
    finally:
        os.close(descriptor)
