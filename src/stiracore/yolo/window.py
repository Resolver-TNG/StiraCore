"""The window marker, its durable writes and the latch judgement (ADR 0012 §11, phase 3).

**Marker.** ``yolo-window.json`` in the data directory (on the agent's read and write
floors) records ``{v, window_id, incarnation_ref, state}``. It is ``open`` from before
``audit.yolo.started`` until a clean, audited end; every other end — a failed row or
marker write, a clock anomaly, a crash, a kill — leaves it ``open``. ``window_id`` is the
random id the chain rows already carry (allocated at the press), so a later process can
match the marker against the chain; it is not a secret.

**Durable write** (:func:`durable_write`): a new temporary file in the same directory,
written and flushed (``F_FULLFSYNC`` on macOS, where plain ``fsync`` does not empty the
drive cache), renamed over the marker, then the **directory** flushed. Any step failing
raises ``OSError``; the caller treats that as "not durable".

**Latch judgement** (:func:`judge`): from the chain rows (``started`` / ``ended`` /
``unlatched``) and the marker. A window id is *closed* only by an ``ended`` row with
``end_class = normal`` and a reason in the normal list (§6), or by an ``unlatched`` row
for it. Latched: a ``started`` window that is not closed (also when an abnormal ``ended``
row exists — it never closes the window), an ``open`` marker with no ``started`` row
(an *orphan*: a crash after the marker and before ``started``), an unreadable /
malformed / symlinked / foreign-owned / multiply-linked marker, a chain that does not
verify. The only automatic repair: an ``open`` marker whose window the chain shows closed
(normally, or by an unlatch) is rewritten ``closed`` / ``closed_by_unlatch``.

Nothing here opens a window or decides one; it reads, writes the marker and judges.
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stiracore.durable_dir import fsync_dir

from .state import NORMAL_END_REASONS

__all__ = [
    "MARKER_NAME",
    "MARKER_STATES",
    "Latch",
    "MarkerRead",
    "WindowMarker",
    "durable_write",
    "judge",
]

MARKER_NAME = "yolo-window.json"
MARKER_STATES = ("open", "closed", "closed_by_unlatch")
_MAX_BYTES = 4096
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_KEYS = frozenset({"v", "window_id", "incarnation_ref", "state", "operation_id"})
# macOS <sys/fcntl.h>
_F_FULLFSYNC = 51
# errno values meaning "F_FULLFSYNC is not supported here" (fall back to fsync).
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


def _flush(fd: int) -> None:
    """Flush a file or directory to stable storage (``F_FULLFSYNC`` on macOS)."""
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, _F_FULLFSYNC)
            return
        except OSError as error:
            # Only "this file system does not support it" falls back to fsync; a real
            # flush failure (EIO, ENOSPC, …) is a failed durable write.
            if error.errno not in _UNSUPPORTED:
                raise
    os.fsync(fd)


def durable_write(path: Path, data: bytes) -> None:
    """temporary file → flush → rename → directory flush (§11 durable order)."""
    directory = path.parent
    tmp = directory / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            _flush(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(directory, _flush)  # POSIX only; Windows cannot open a directory (#543)


@dataclass(frozen=True)
class MarkerRead:
    """What :meth:`WindowMarker.read` found: ``missing`` | ``ok`` | ``bad`` (+ why)."""

    status: str
    data: Mapping[str, Any] | None = None
    digest: str = hashlib.sha256(b"").hexdigest()
    problem: str | None = None

    @property
    def state(self) -> str | None:
        return None if self.data is None else self.data["state"]

    @property
    def window_id(self) -> str | None:
        return None if self.data is None else self.data["window_id"]


def _valid(data: Any) -> bool:
    if type(data) is not dict or not {"v", "window_id", "incarnation_ref", "state"} <= set(data):
        return False
    if not set(data) <= _KEYS or data["v"] != 1 or type(data["v"]) is not int:
        return False
    if type(data["window_id"]) is not str or not _HEX32.fullmatch(data["window_id"]):
        return False
    ref = data["incarnation_ref"]
    if type(ref) is not str or not _HEX64.fullmatch(ref):
        return False
    if type(data["state"]) is not str or data["state"] not in MARKER_STATES:
        return False
    operation = data.get("operation_id")
    return operation is None or (type(operation) is str and bool(_HEX32.fullmatch(operation)))


class WindowMarker:
    """The marker file of one data directory. Writes are durable (§11) or raise."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / MARKER_NAME

    def read(self) -> MarkerRead:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return MarkerRead("missing")
        except OSError:
            return MarkerRead("bad", problem="marker_unreadable")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return MarkerRead("bad", problem="marker_not_a_file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return MarkerRead("bad", problem="marker_foreign_owner")
        if info.st_nlink != 1:
            return MarkerRead("bad", problem="marker_multiply_linked")
        if info.st_size > _MAX_BYTES:
            return MarkerRead("bad", problem="marker_malformed")
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(fd, _MAX_BYTES + 1)
            finally:
                os.close(fd)
        except OSError:
            return MarkerRead("bad", problem="marker_unreadable")
        digest = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return MarkerRead("bad", digest=digest, problem="marker_malformed")
        if not _valid(data):
            return MarkerRead("bad", digest=digest, problem="marker_malformed")
        return MarkerRead("ok", data=data, digest=digest)

    def write(
        self,
        state: str,
        window_id: str,
        incarnation_ref: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "v": 1,
            "window_id": window_id,
            "incarnation_ref": incarnation_ref,
            "state": state,
        }
        if operation_id is not None:
            data["operation_id"] = operation_id
        if not _valid(data):
            raise ValueError("invalid marker")
        durable_write(self.path, json.dumps(data, sort_keys=True).encode("utf-8"))


# --- the judgement --------------------------------------------------------------------


@dataclass(frozen=True)
class Latch:
    """One latch: ``window`` / ``orphan_marker`` (each closed by its own unlatch) or
    ``unreadable`` (no id: the files need a look; a restart judges again)."""

    subject: str
    window_id: str | None
    reason: str


@dataclass(frozen=True)
class Judgement:
    latches: tuple[Latch, ...]
    repair: str | None = None  # marker state to write for marker.window_id, if any


def _hex32(value: Any) -> str | None:
    return value if type(value) is str and _HEX32.fullmatch(value) else None


def judge(rows: Iterable[tuple[str, Mapping[str, Any]]] | None, marker: MarkerRead) -> Judgement:
    """The latch judgement of §11. ``rows is None``: the chain could not be read."""
    if rows is None:
        return Judgement((Latch("unreadable", None, "chain_unverified"),))
    started: list[str] = []
    closed_normal: set[str] = set()
    abnormal: dict[str, str] = {}
    unlatched: dict[str, set[str]] = {}
    for event_type, attributes in rows:
        window_id = _hex32(attributes.get("window_id"))
        if window_id is None:
            return Judgement((Latch("unreadable", None, "chain_malformed"),))
        if event_type == "audit.yolo.started":
            if window_id not in started:
                started.append(window_id)
        elif event_type == "audit.yolo.ended":
            reason = attributes.get("end_reason")
            if attributes.get("end_class") == "normal" and reason in NORMAL_END_REASONS:
                closed_normal.add(window_id)
            else:
                abnormal[window_id] = reason if type(reason) is str else "unknown"
        elif event_type == "audit.yolo.unlatched":
            subject = attributes.get("latch_subject")
            if subject in ("window", "orphan_marker"):
                unlatched.setdefault(window_id, set()).add(subject)
    latches: list[Latch] = []
    for window_id in started:
        if window_id in closed_normal or "window" in unlatched.get(window_id, set()):
            continue
        latches.append(Latch("window", window_id, abnormal.get(window_id, "no_end_row")))
    repair = None
    if marker.status == "bad":
        latches.append(Latch("unreadable", None, marker.problem or "marker_malformed"))
    elif marker.status == "ok" and marker.state == "open":
        window_id = marker.window_id
        if window_id in started:
            if window_id in closed_normal:
                repair = "closed"
            elif "window" in unlatched.get(window_id, set()):
                repair = "closed_by_unlatch"
            # else: already latched as a window above
        elif unlatched.get(window_id):
            repair = "closed_by_unlatch"
        else:
            latches.append(Latch("orphan_marker", window_id, "orphan_marker"))
    return Judgement(tuple(latches), repair)
