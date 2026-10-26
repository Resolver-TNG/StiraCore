"""Where every store of one data directory lives, and read-only access to it (#294).

Used by the retention report and the delete request (``stira memory retention`` /
``stira memory forget``). Nothing here creates or writes anything: files are read
without following a symbolic link at any component below the data directory
(``lstat`` walk, then ``O_NOFOLLOW`` open, then the opened descriptor must be the
regular file the walk saw), SQLite files are opened read-only (``mode=ro``). A link
or a non-regular file is skipped and reported, never read.

The data directory itself is resolved once (``/tmp`` is a link on macOS); below it a
link is refused. This is path-based: descriptor-relative (``dir_fd``) opens for every
memory path are follow-up work (#294).
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stiracore.control.forgetting_archive import ARCHIVE_DIRNAME, AUDIT_LOG_NAME
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class UnsafePathError(ValueError):
    """A path below the data directory is a symbolic link or not a regular file."""


class UnreadableStoreError(UnsafePathError):
    """A store cannot be read without changing it (it is reported, not inspected)."""


@dataclass(frozen=True)
class DataLayout:
    """Paths of the stores of a server data directory (``.local/stira``)."""

    data_dir: Path
    agent_id: str = "stira"

    @classmethod
    def at(cls, data_dir: str | os.PathLike, agent_id: str = "stira") -> DataLayout:
        MemoryStore._validate_component(agent_id, "agent_id")
        path = Path(data_dir).expanduser()
        if not path.is_dir():
            raise ValueError("data directory does not exist or is not a directory")
        return cls(path.resolve(), agent_id)

    @property
    def memory_base(self) -> Path:
        return self.data_dir / "memory"

    @property
    def memory_dir(self) -> Path:
        return self.memory_base / "agents" / self.agent_id / "memory"

    def layer_dir(self, layer: MemoryLayer) -> Path:
        return self.memory_dir / MemoryLayer(layer).value

    @property
    def index(self) -> Path:
        return self.memory_dir / "index.sqlite3"

    @property
    def archive_dir(self) -> Path:
        return self.memory_dir / ARCHIVE_DIRNAME

    @property
    def audit_log(self) -> Path:
        return self.archive_dir / AUDIT_LOG_NAME

    @property
    def rejections(self) -> Path:
        return self.memory_dir / "staging_rejections.jsonl"

    @property
    def checkpoints(self) -> Path:
        return self.data_dir / "checkpoints"

    @property
    def stash(self) -> Path:
        return self.data_dir / "context-stash"

    @property
    def turns(self) -> Path:
        return self.data_dir / "turns.db"

    @property
    def scheduler_log(self) -> Path:
        return self.data_dir / "scheduler-events.jsonl"

    @property
    def events(self) -> Path:
        return self.data_dir / "events.db"

    def emotion_record(self, key: str) -> Path:
        """The #364 emotion record of episodic ``key`` (``emotion/<digest>.json``)."""
        from types import SimpleNamespace

        from stiracore.tools.episodic_emotion import emotion_record_path

        return emotion_record_path(SimpleNamespace(memory_dir=self.memory_dir), key)

    def relative(self, path: Path) -> str:
        try:
            return Path(path).relative_to(self.data_dir).as_posix()
        except ValueError:
            return Path(path).name

    def check_inside(self, path: Path) -> None:
        """Every component from the data directory down must not be a link."""
        relative = Path(path).relative_to(self.data_dir)
        current = self.data_dir
        for part in relative.parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                raise UnsafePathError(f"{self.relative(current)} is a symbolic link")


def read_bytes(layout: DataLayout, path: Path) -> bytes | None:
    """The bytes of a regular file below the data directory, or ``None`` if missing."""
    layout.check_inside(path)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UnsafePathError(f"{layout.relative(path)} not readable ({error.strerror})") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafePathError(f"{layout.relative(path)} is not a regular file")
        layout.check_inside(path)
        now = os.lstat(path)
        if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
            raise UnsafePathError(f"{layout.relative(path)} was replaced while it was read")
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def walk_files(
    layout: DataLayout, root: Path, errors: list[str] | None = None
) -> Iterator[tuple[Path, os.stat_result]]:
    """Regular files below ``root`` (sorted); links and special files are skipped.

    A directory that cannot be listed (no permission) is never skipped silently: it
    raises :class:`UnreadableStoreError`, or, with ``errors``, its relative path is
    appended there and the walk goes on (the caller decides what it means).
    """

    def failed(error: OSError) -> None:
        where = layout.relative(Path(error.filename)) if error.filename else layout.relative(root)
        if errors is None:
            raise UnreadableStoreError(f"{where} not readable ({type(error).__name__})")
        errors.append(where)

    try:
        layout.check_inside(root)
        info = os.lstat(root)
    except (FileNotFoundError, UnsafePathError):
        return
    if stat.S_ISREG(info.st_mode):
        yield root, info
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    for directory, names, files in os.walk(root, followlinks=False, onerror=failed):
        names.sort()
        for name in sorted(files):
            path = Path(directory) / name
            try:
                entry = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(entry.st_mode):
                yield path, entry


def skipped_links(layout: DataLayout, root: Path) -> list[str]:
    """Links found below ``root`` (reported, never followed)."""
    found: list[str] = []
    if not root.exists() and not root.is_symlink():
        return found
    if root.is_symlink():
        return [layout.relative(root)]
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            if path.is_symlink():
                found.append(layout.relative(path))
    return sorted(found)


@dataclass(frozen=True)
class EntryFile:
    layer: MemoryLayer
    key: str
    path: Path
    size: int
    mtime: float
    text: str
    metadata: dict[str, Any]


def memory_entries(layout: DataLayout, layer: MemoryLayer) -> Iterator[EntryFile]:
    """Every ``*.md`` entry of a layer, read without touching ``last_accessed``."""
    root = layout.layer_dir(layer)
    for path, info in walk_files(layout, root):
        if path.suffix != ".md" or path.name.startswith("."):
            continue
        data = read_bytes(layout, path)
        if data is None:
            continue
        text = data.decode("utf-8", errors="replace")
        try:
            _content, metadata = MemoryStore._parse(text)
        except ValueError:
            metadata = {}
        key = path.relative_to(root).with_suffix("").as_posix()
        yield EntryFile(MemoryLayer(layer), key, path, info.st_size, info.st_mtime, text, metadata)


def connect_read_only(layout: DataLayout, path: Path) -> sqlite3.Connection | None:
    """A read-only connection, or ``None`` when the database does not exist."""
    layout.check_inside(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"{layout.relative(path)} is not a regular file")
    # Reading must not create or change a file. A WAL database opened read-only
    # creates -wal/-shm when they are missing: with neither (and no hot rollback
    # journal) nothing has to be replayed and ``immutable`` reads the file as it is.
    # With -wal and -shm present (a server is writing, or left them), read through
    # them. A -wal without -shm, or a hot -journal, needs recovery, which writes:
    # refused (the caller reports the store as not inspected; an --apply recovers it
    # under the owner lock first, see :func:`recover_for_apply`).
    wal, shm, journal = (Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    if _present(wal) and not _present(shm):
        raise UnreadableStoreError(
            f"{layout.relative(path)} has a -wal without its -shm (reading would create files)"
        )
    if _present(journal):
        raise UnreadableStoreError(
            f"{layout.relative(path)} has a hot -journal (reading would roll it back)"
        )
    options = "mode=ro" if _present(wal) else "mode=ro&immutable=1"
    connection = sqlite3.connect(f"{path.as_uri()}?{options}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def recover_for_apply(layout: DataLayout, *paths: Path) -> None:
    """Let SQLite recover a -wal without -shm / a hot journal before an ``--apply``.

    Opens each database read-write once (the caller holds the owner lock): SQLite
    replays the WAL or rolls back the journal, as the server would on its next start.
    """
    for path in paths:
        layout.check_inside(path)
        if not path.is_file() or path.is_symlink():
            continue
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            connection.close()


_AREAS = {
    "episodic": "episodic",
    "semantic": "semantic",
    "procedural": "procedural",
    "emotion": "emotion",
    "forgotten": "forgotten",
    "memory-plans": "memory_plans",
}
_ROOT_FILES = (
    "turns.db",
    "events.db",
    "scheduler-events.jsonl",
    "hitl-approvals.db",
    "hitl-operations.db",
)


def area_of(layout: DataLayout, relative: str) -> str:
    """A fixed label for where a path is (never a name taken from the path itself)."""
    memory = layout.relative(layout.memory_dir)
    if relative == memory or relative.startswith(f"{memory}/"):
        rest = relative[len(memory) + 1 :]
        first = rest.split("/", 1)[0]
        if "/" in rest or first in _AREAS:
            return _AREAS.get(first, "memory")
        if first.startswith("index.sqlite3"):
            return "index.sqlite3"
        return "memory"
    top = relative.split("/", 1)[0]
    if top in ("checkpoints", "context-stash", "workspace"):
        return top
    for name in _ROOT_FILES:
        if top.startswith(name):
            return name
    return "other"


def located(layout: DataLayout, relative: str) -> dict[str, str]:
    return {"area": area_of(layout, relative), "where_sha256": _sha(relative)}


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def public_reasons(layout: DataLayout, reasons) -> list[dict[str, str]]:
    """Skipped-store reasons (which start with a relative path) as area + digest only."""
    return [
        {"area": area_of(layout, str(reason)), "where_sha256": _sha(str(reason))}
        for reason in sorted(set(reasons))
    ]


def file_size(layout: DataLayout, path: Path, *suffixes: str) -> int:
    """Size of a file plus its siblings with ``suffixes`` (``-wal``, ``.head``...)."""
    total = 0
    for suffix in ("", *suffixes):
        candidate = Path(f"{path}{suffix}")
        try:
            layout.check_inside(candidate)
            info = os.lstat(candidate)
        except (FileNotFoundError, UnsafePathError):
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


_ESCAPE = re.compile(
    r"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|\\u([0-9a-fA-F]{4})|\\([\"\\\\/])"
)


def _unescaped(match: re.Match) -> str:
    high, low, single, simple = match.groups()
    if simple is not None:
        return simple
    if single is not None:
        code = int(single, 16)
        return match.group(0) if 0xD800 <= code <= 0xDFFF else chr(code)
    return chr(0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00))


def contains(text: str, needles: tuple[str, ...]) -> bool:
    """Case-insensitive literal match of any needle (also JSON-escaped forms).

    ``\\uXXXX`` escapes of the text are decoded too before folding: an escaped
    capital (``\\u00c4``) folds only once it is a letter again.
    """
    if not needles:
        return False
    folded = text.casefold()
    if any(needle in folded for needle in needles):
        return True
    if "\\" not in text:
        return False
    decoded = _ESCAPE.sub(_unescaped, text).casefold()
    return any(needle in decoded for needle in needles)


def needles_for(queries: tuple[str, ...]) -> tuple[str, ...]:
    """The casefolded forms a query is searched in: plain and ``\\uXXXX`` escaped."""
    import json

    forms: list[str] = []
    for query in queries:
        plain = query.casefold()
        forms.append(plain)
        escaped = json.dumps(query)[1:-1].casefold()
        if escaped != plain:
            forms.append(escaped)
    return tuple(dict.fromkeys(form for form in forms if form))


__all__ = [
    "DataLayout",
    "EntryFile",
    "UnreadableStoreError",
    "UnsafePathError",
    "area_of",
    "connect_read_only",
    "contains",
    "file_size",
    "located",
    "memory_entries",
    "needles_for",
    "public_reasons",
    "read_bytes",
    "recover_for_apply",
    "skipped_links",
    "walk_files",
]
