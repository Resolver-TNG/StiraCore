"""Agent-scoped Markdown memories with a rebuildable SQLite search index.

Markdown files are authoritative. Search refreshes the index from disk so that
memories edited outside this API are immediately searchable. SQLite only holds
rebuildable search text and access timestamps used by semantic maintenance.
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from stiracore.durable_dir import fsync_dir
from stiracore.identity_paths import DEFAULT_READ_ONLY_PATHS, IdentityPathPolicy

_LOG = logging.getLogger(__name__)

# POSIX sets times on an open descriptor; Windows only by path (best effort, #292).
_UTIME_BY_DESCRIPTOR = os.utime in os.supports_fd


class MemorySymlinkError(ValueError):
    """A memory path contains a symbolic link (also one that appeared mid-operation)."""


class MemoryLayer(Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


@dataclass
class MemoryEntry:
    key: str
    layer: MemoryLayer
    content: str
    metadata: dict[str, Any]
    path: Path
    modified_at: datetime
    last_accessed: datetime


class MemoryStore:
    """Store one agent's memories without sharing state with other agents."""

    def __init__(self, base_dir: Path, agent_id: str, read_only_paths: set[str] | None = None):
        self._validate_component(agent_id, "agent_id")
        base_path = Path(base_dir).absolute()
        # The ancestors of the configured root are the operator's choice (macOS /tmp and
        # /var are links, a home or project folder may be one): they are resolved once
        # here. A link at the root itself or anywhere below it is still refused (#296).
        base_path = base_path.parent.resolve() / base_path.name
        self._reject_symlinks(base_path)
        self.base_dir = base_path.resolve()
        self.agent_id = agent_id
        self.memory_dir = self.base_dir / "agents" / agent_id / "memory"
        self.read_only_paths: set[str] = set(DEFAULT_READ_ONLY_PATHS) | set(read_only_paths or ())
        self._reject_links(self.memory_dir)
        for layer in MemoryLayer:
            directory = self.memory_dir / layer.value
            self._reject_links(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self._reject_links(directory)
            if not directory.resolve().is_relative_to(self.base_dir):
                raise ValueError("Memory directories must remain inside base_dir.")

        self.index_path = self.memory_dir / "index.sqlite3"
        self._reject_links(self.index_path)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    layer TEXT NOT NULL,
                    key TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    modified_at REAL NOT NULL,
                    last_accessed REAL,
                    PRIMARY KEY (layer, key)
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_entries)")}
            if "last_accessed" not in columns:
                connection.execute("ALTER TABLE memory_entries ADD COLUMN last_accessed REAL")
            # Verified provenance of an Episodic entry (#294, memory_origin): set only by
            # the server's own writers, bound to the SHA-256 of the file's whole text.
            for column in ("origin", "origin_source", "origin_sha256"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE memory_entries ADD COLUMN {column} TEXT")
            connection.execute(
                "UPDATE memory_entries SET last_accessed = modified_at WHERE last_accessed IS NULL"
            )

    @staticmethod
    def _reject_symlinks(path: Path, *, root: Path | None = None) -> None:
        """Reject symbolic links in the path itself or any parent component.

        With ``root`` (a store's resolved ``base_dir``) and a ``path`` lexically inside it,
        only ``root`` itself and the components below it are checked: the ancestors were
        resolved once when the store was built (the operator's choice). Any other path
        (outside ``root``, or with a ``..``) is checked from the filesystem root."""
        path = Path(path).absolute()
        components = (*reversed(path.parents), path)
        if root is not None and ".." not in path.parts and path.is_relative_to(root):
            components = components[len(root.parents) :]
        for component in components:
            if component.is_symlink():
                raise MemorySymlinkError("Memory paths must not contain symbolic links.")

    def _reject_links(self, path: Path) -> None:
        """:meth:`_reject_symlinks` for a path of this store: from ``base_dir`` down."""
        self._reject_symlinks(path, root=self.base_dir)

    @staticmethod
    def _validate_component(value: str, name: str) -> None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value in {".", ".."}
            or any(character in value for character in ("/", "\\", "\0"))
        ):
            raise ValueError(f"{name} must be a non-empty, single path component.")

    @classmethod
    def _validate_key(cls, key: str) -> tuple[str, ...]:
        if not isinstance(key, str) or not key.strip() or "\\" in key or "\0" in key:
            raise ValueError("key must be a non-empty relative POSIX path.")
        parts = tuple(key.split("/"))
        if any(not part or part in {".", ".."} for part in parts):
            raise ValueError("key must be a non-empty relative POSIX path.")
        for part in parts:
            cls._validate_component(part, "key component")
        return parts

    def _path(self, layer: MemoryLayer, key: str) -> Path:
        parts = self._validate_key(key)
        directory = self.memory_dir / layer.value
        path = directory.joinpath(*parts[:-1], f"{parts[-1]}.md")
        self._reject_links(path)
        if not path.resolve().is_relative_to(self.memory_dir.resolve()):
            raise ValueError("Memory entries must remain inside the memory directory.")
        return path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._reject_links(self.index_path)
        connection = sqlite3.connect(self.index_path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _parse(text: str) -> tuple[str, dict[str, Any]]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return text, {}

        for position in range(1, len(lines)):
            if lines[position].strip() in {"---", "..."}:
                try:
                    metadata = yaml.safe_load("".join(lines[1:position]))
                except yaml.YAMLError as error:
                    raise ValueError(f"Invalid memory frontmatter: {error}") from error
                if metadata is None:
                    metadata = {}
                if not isinstance(metadata, dict) or any(
                    not isinstance(key, str) for key in metadata
                ):
                    raise ValueError("Memory frontmatter must be a string-keyed mapping.")
                return "".join(lines[position + 1 :]), metadata

        return text, {}

    @staticmethod
    def _index_row(entry: MemoryEntry) -> tuple[str, str, str, float, float]:
        metadata_text = yaml.safe_dump(entry.metadata, allow_unicode=True)
        search_text = (f"{entry.key}\n{entry.content}\n{metadata_text}").casefold()
        return (
            entry.layer.value,
            entry.key,
            search_text,
            entry.modified_at.timestamp(),
            entry.last_accessed.timestamp(),
        )

    def _update_index(self, entry: MemoryEntry) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_entries
                    (layer, key, search_text, modified_at, last_accessed)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(layer, key) DO UPDATE SET
                    search_text = excluded.search_text,
                    modified_at = excluded.modified_at,
                    last_accessed = COALESCE(memory_entries.last_accessed, excluded.last_accessed)
                """,
                self._index_row(entry),
            )

    def _last_accessed(self, layer: MemoryLayer, key: str, fallback: float) -> datetime:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_accessed FROM memory_entries WHERE layer = ? AND key = ?",
                (layer.value, key),
            ).fetchone()
        timestamp = fallback if row is None or row[0] is None else float(row[0])
        return datetime.fromtimestamp(timestamp, tz=UTC)

    def _touch_access(self, entry: MemoryEntry) -> MemoryEntry:
        timestamp = time.time()
        with self._connection() as connection:
            connection.execute(
                "UPDATE memory_entries SET last_accessed = ? WHERE layer = ? AND key = ?",
                (timestamp, entry.layer.value, entry.key),
            )
        return replace(entry, last_accessed=datetime.fromtimestamp(timestamp, tz=UTC))

    def _require_writable(self, path: Path) -> None:
        IdentityPathPolicy(self.read_only_paths, base_dir=self.memory_dir).require_writable(path)

    def _open_entry(self, path: Path):
        """Open an entry for reading without following a symlink swapped in after the
        path check (#291): no-follow open, then the path must still name, through
        real directories, the inode that was opened. Not an OS sandbox."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise MemorySymlinkError("Memory paths must not contain symbolic links.") from None
            raise
        try:
            opened = os.fstat(descriptor)
            self._reject_links(path)  # a parent swapped before the open
            now = os.stat(path)
            if not stat.S_ISREG(opened.st_mode) or (now.st_dev, now.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise MemorySymlinkError("Memory paths must not contain symbolic links.")
        except BaseException:
            os.close(descriptor)
            raise
        return open(descriptor, encoding="utf-8", newline="")

    def _read_entry(self, layer: MemoryLayer, key: str) -> MemoryEntry | None:
        path = self._path(layer, key)
        try:
            with self._open_entry(path) as source:
                text = source.read()
                modified_timestamp = os.fstat(source.fileno()).st_mtime
        except FileNotFoundError:
            return None
        content, metadata = self._parse(text)
        modified_at = datetime.fromtimestamp(modified_timestamp, tz=UTC)
        last_accessed = self._last_accessed(layer, key, modified_timestamp)
        return MemoryEntry(key, layer, content, metadata, path, modified_at, last_accessed)

    def write(
        self,
        layer: MemoryLayer,
        key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        preserve_mtime: bool = False,
        exclusive: bool = False,
    ) -> MemoryEntry:
        """Atomically write a memory, replacing any existing entry.

        ``preserve_mtime`` keeps an existing entry's modification time and
        ``exclusive`` refuses to replace one (see :meth:`write_text`).
        """
        layer = MemoryLayer(layer)
        path = self._path(layer, key)
        if not isinstance(content, str):
            raise TypeError("content must be a string.")
        self._require_writable(path)
        if metadata is not None and (
            not isinstance(metadata, dict) or any(not isinstance(name, str) for name in metadata)
        ):
            raise ValueError("metadata must be a string-keyed mapping.")
        return self.write_text(
            layer,
            key,
            self.render(content, metadata),
            preserve_mtime=preserve_mtime,
            exclusive=exclusive,
        )

    @staticmethod
    def render(content: str, metadata: dict[str, Any] | None = None) -> str:
        """The exact Markdown text :meth:`write` stores for ``content`` and ``metadata``."""
        frontmatter = yaml.safe_dump(
            metadata if metadata is not None else {},
            allow_unicode=True,
            sort_keys=False,
        )
        return f"---\n{frontmatter}---\n{content}"

    def read_text(self, layer: MemoryLayer, key: str) -> str | None:
        """The raw Markdown of an entry (frontmatter included), not access-touched."""
        path = self._path(MemoryLayer(layer), key)
        try:
            # The no-follow open of every other read (#291; #601 C12).
            with self._open_entry(path) as source:
                return source.read()
        except FileNotFoundError:
            return None

    def write_text(
        self,
        layer: MemoryLayer,
        key: str,
        text: str,
        *,
        preserve_mtime: bool = False,
        exclusive: bool = False,
    ) -> MemoryEntry:
        """Atomically replace an entry with raw Markdown (byte-exact restore, #294).

        ``preserve_mtime`` keeps the modification time of the entry being
        replaced (#292): :meth:`list_entries` orders by it, so a metadata-only
        rewrite such as a Harvest mark must not move the entry to the front.
        The time is set on the open temporary file (by descriptor, so a path
        swapped for a link cannot redirect it) before it is fsynced, closed and
        atomically put in place, and the directory is fsynced afterwards: the
        entry is never visible with a new time. If setting the time fails,
        nothing is replaced and the temporary file is removed. A new entry gets
        the current time. Concurrent writers are not detected (single writer,
        #285). Ordinary writes still update the time.

        ``exclusive`` (#144) puts the complete temporary file in place with a
        hard link instead of a rename, which fails with ``FileExistsError`` if
        anything (also a symbolic link) is already at the path: of concurrent
        exclusive writers exactly one succeeds and nothing is overwritten. A
        file system without hard links fails closed (``OSError``).
        """
        layer = MemoryLayer(layer)
        path = self._path(layer, key)
        if not isinstance(text, str):
            raise TypeError("text must be a string.")
        self._require_writable(path)
        self._parse(text)  # invalid frontmatter is refused before anything is replaced

        self._reject_links(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_links(path.parent)
        if not path.parent.resolve().is_relative_to(self.memory_dir.resolve()):
            raise ValueError("Memory entries must remain inside the memory directory.")

        kept_times: tuple[int, int] | None = None
        if preserve_mtime:
            try:
                previous = os.stat(path, follow_symlinks=False)
                kept_times = (previous.st_atime_ns, previous.st_mtime_ns)
            except FileNotFoundError:
                kept_times = None
        by_descriptor = _UTIME_BY_DESCRIPTOR

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=path.parent,
                prefix=".memory-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(text)
                temporary.flush()
                if kept_times is not None and by_descriptor:
                    os.utime(temporary.fileno(), ns=kept_times)
                os.fsync(temporary.fileno())
                identity = os.fstat(temporary.fileno())
            if kept_times is not None and not by_descriptor:
                # Windows has no descriptor-based utime: set it by path after
                # close, only if the path is still the file just written.
                current = os.stat(temporary_path, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise OSError("Temporary memory file was replaced before its time was set.")
                os.utime(temporary_path, ns=kept_times)
            if exclusive:
                os.link(temporary_path, path)
            else:
                os.replace(temporary_path, path)
            if kept_times is not None:
                self._fsync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        entry = self._read_entry(layer, key)
        if entry is None:
            raise FileNotFoundError(f"Memory disappeared after writing: {path}")
        self._update_index(entry)
        return entry

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist a rename in ``directory`` (POSIX; Windows cannot open a directory)."""
        fsync_dir(directory)

    def read(self, layer: MemoryLayer, key: str) -> MemoryEntry | None:
        """Read Markdown directly and update its access timestamp."""
        layer = MemoryLayer(layer)
        entry = self._read_entry(layer, key)
        if entry is None:
            return None
        self._update_index(entry)
        return self._touch_access(entry)

    def _entries(self, layer: MemoryLayer) -> Iterator[MemoryEntry]:
        for relative in self._entry_keys(layer):
            entry = self._read_unless_broken(layer, relative)
            if entry is not None:
                yield entry

    def _read_unless_broken(self, layer: MemoryLayer, key: str) -> MemoryEntry | None:
        """``_read_entry`` for listings and searches: an entry that cannot be parsed (bad
        frontmatter, not UTF-8 — an outside edit) is skipped with a log line instead of
        failing the whole listing (#601 C14). A direct ``read`` still raises."""
        try:
            return self._read_entry(layer, key)
        except MemorySymlinkError:
            raise
        except ValueError as error:  # UnicodeDecodeError is a ValueError
            _LOG.warning(
                "memory: skipped an unreadable %s entry (%s)", layer.value, type(error).__name__
            )
            return None

    def _entry_keys(self, layer: MemoryLayer) -> Iterator[str]:
        """Keys of the regular ``*.md`` entries of ``layer``; no file is opened."""
        directory = self.memory_dir / layer.value
        self._reject_links(directory)
        for path in sorted(directory.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            self._reject_links(path)
            yield path.relative_to(directory).with_suffix("").as_posix()

    def count_entries(self, layer: MemoryLayer, *, exclude_prefix: str | None = None) -> int:
        """Number of entries in ``layer`` without reading or parsing them.

        Every regular ``*.md`` file the enumeration finds (symlinks excluded); no upper
        bound (the count was ``len(list_entries(layer, 9999))`` before #289).
        ``exclude_prefix`` leaves out keys that start with it (#574: the memory page's
        procedural total does not count ``staging/`` proposals, which are not rules).
        """
        return sum(
            1
            for key in self._entry_keys(MemoryLayer(layer))
            if not (exclude_prefix and key.startswith(exclude_prefix))
        )

    def list_entries(self, layer: MemoryLayer, limit: int = 20) -> list[MemoryEntry]:
        """List newest entries without treating listing as content access.

        Ordered by modification time (newest first), then key. Only the newest
        ``limit`` files are read and parsed: the order comes from ``stat``, so a
        growing layer no longer costs a YAML parse per entry on every call.

        One ``lstat`` pass decides which entries are read (#291): an entry touched
        after it keeps its place in this result and moves at the next call (no
        retry). A name that became a symlink (itself or a parent directory) since
        the enumeration is skipped; its target is never read.
        """
        layer = MemoryLayer(layer)
        if limit <= 0:
            return []
        directory = self.memory_dir / layer.value
        candidates = []
        for key in self._entry_keys(layer):
            try:
                info = os.lstat(directory / f"{key}.md")
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # replaced by a symlink (or anything else) since enumerated
            candidates.append((-info.st_mtime, key))
        candidates.sort()
        entries: list[MemoryEntry] = []
        for _modified, key in candidates:
            try:
                entry = self._read_unless_broken(layer, key)
            except MemorySymlinkError:
                continue
            if entry is not None:
                entries.append(entry)
            if len(entries) >= limit:
                break
        # Re-sort what was read (a file may change between stat and read).
        entries.sort(key=lambda entry: (-entry.modified_at.timestamp(), entry.key))
        return entries[:limit]

    def search(
        self,
        query: str,
        layers: Iterable[MemoryLayer] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Case-insensitive literal search; matching entries are access-touched."""
        selected = (
            list(MemoryLayer)
            if layers is None
            else list(dict.fromkeys(MemoryLayer(layer) for layer in layers))
        )
        if limit <= 0 or not query.strip() or not selected:
            return []

        entries = {
            (entry.layer.value, entry.key): entry
            for layer in selected
            for entry in self._entries(layer)
        }
        placeholders = ", ".join("?" for _ in selected)
        layer_names = [layer.value for layer in selected]

        with self._connection() as connection:
            existing_rows = connection.execute(
                f"SELECT layer, key FROM memory_entries WHERE layer IN ({placeholders})",
                layer_names,
            ).fetchall()
            missing = [row for row in existing_rows if row not in entries]
            connection.executemany(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?",
                missing,
            )
            connection.executemany(
                """
                INSERT INTO memory_entries
                    (layer, key, search_text, modified_at, last_accessed)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(layer, key) DO UPDATE SET
                    search_text = excluded.search_text,
                    modified_at = excluded.modified_at,
                    last_accessed = COALESCE(memory_entries.last_accessed, excluded.last_accessed)
                """,
                [self._index_row(entry) for entry in entries.values()],
            )
            rows = connection.execute(
                f"""
                SELECT layer, key
                FROM memory_entries
                WHERE layer IN ({placeholders})
                  AND instr(search_text, ?) > 0
                ORDER BY modified_at DESC, layer ASC, key ASC
                LIMIT ?
                """,
                [*layer_names, query.casefold(), limit],
            ).fetchall()

        return [self._touch_access(entries[(layer, key)]) for layer, key in rows]

    def delete(self, layer: MemoryLayer, key: str) -> bool:
        """Delete episodic memory only; protected layers raise ValueError."""
        layer = MemoryLayer(layer)
        if layer is not MemoryLayer.EPISODIC:
            raise ValueError("Only episodic memories may be deleted.")
        path = self._path(layer, key)
        self._require_writable(path)
        # The episode's emotion record (#364) goes first: if that fails, the episode stays
        # and a retry removes both; an episode is never deleted with its record left behind.
        from .episodic_emotion import delete_emotion_snapshot

        delete_emotion_snapshot(self, key)
        try:
            path.unlink()
            deleted = True
        except FileNotFoundError:
            deleted = False

        with self._connection() as connection:
            connection.execute(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?",
                (layer.value, key),
            )
        return deleted
