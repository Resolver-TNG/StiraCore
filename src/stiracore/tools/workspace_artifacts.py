"""Workspace artifacts: what ``file_write`` / ``editor`` wrote, as versions (#454, ADR 0020).

A successful mutating ``file_write`` / ``editor`` call registers the file it wrote when the
path is inside the workspace (judged with the write side's own ``WritePathPolicy``), not a
symlink, not on a floor, not hidden and not secret-looking. Per version the registry keeps
facts only — relative path, turn id, create/update, size, SHA-256, kind, time — in
``<root>/index.db``, and a content-addressed snapshot of the bytes in ``<root>/blobs/`` when
small enough. ``<root>`` is ``<data dir>/artifacts`` on the server, on the data-directory
floor: agent file tools neither read nor write it. Nothing here writes an event or an audit
row, and no path is logged.

Every count, size and time is bounded (``MAX_*``); past a limit the registry refuses (a new
path) or keeps a hash-only version, and counts it as a warning. It never deletes a
workspace file.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from stiracore.observability.masking import mask_secrets

from .file_editor_tool import EDITOR_TOOL_NAME, from_editor_path
from .identity_tools import PATH_WRITE_TOOLS, editor_read_only

_LOG = logging.getLogger(__name__)

MAX_ARTIFACTS = 1000
MAX_VERSIONS = 50
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_STORE_BYTES = 256 * 1024 * 1024
MAX_LIST = 200
TURN_COUNTS_MAX = 32  # #535: turns per count read (the board lists 20 rows)
MAX_DIFF_INPUT_BYTES = 1024 * 1024
MAX_DIFF_LINES = 2000
MAX_PATH_CHARS = 1024

_CODE_SUFFIXES = [
    "py",
    "js",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "jsx",
    "css",
    "json",
    "sh",
    "yaml",
    "yml",
    "toml",
    "rs",
    "go",
    "java",
    "c",
    "h",
    "cpp",
    "hpp",
    "rb",
    "sql",
    "xml",
    "kt",
    "swift",
    "lua",
    "php",
]
KINDS = ("html", "markdown", "image", "code", "text", "other")
# A person's labels (ADR 0020 §7): a fixed set, set only through the dashboard route.
LABELS = ("stable", "experimental", "latest")
MAX_LABEL_HISTORY = 20
MAX_CHECKS = 5
PAGE_CHECK_TOOL = "page_check"
_EXTENSIONS = {
    "html": {".html", ".htm"},
    "markdown": {".md", ".markdown"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"},
    "code": {f".{s}" for s in _CODE_SUFFIXES},
    "text": {".txt", ".csv", ".tsv", ".log", ".rst"},
}
_TEXT_KINDS = frozenset({"html", "markdown", "code", "text"})
_MIME = {
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
# Fixed reason words of a skipped registration (counted, never a path).
SKIP_REASONS = (
    "invalid_path",
    "outside_workspace",
    "symlink",
    "floor",
    "hidden",
    "secret_name",
    "missing",
    "not_regular",
    "too_large",
    "limit_artifacts",
    "io_error",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    latest INTEGER NOT NULL,
    labels TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS artifacts_updated ON artifacts(updated_at);
CREATE TABLE IF NOT EXISTS versions (
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    turn_id TEXT,
    op TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    at REAL NOT NULL,
    stored INTEGER NOT NULL,
    tool TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);
CREATE INDEX IF NOT EXISTS versions_turn ON versions(turn_id);
CREATE INDEX IF NOT EXISTS versions_sha ON versions(sha256);
CREATE TABLE IF NOT EXISTS label_history (
    artifact_id TEXT NOT NULL,
    at REAL NOT NULL,
    labels TEXT NOT NULL,
    previous TEXT NOT NULL,
    version INTEGER NOT NULL,
    principal TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS label_history_artifact ON label_history(artifact_id, at);
CREATE TABLE IF NOT EXISTS checks (
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    turn_id TEXT,
    at REAL NOT NULL,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_artifact ON checks(artifact_id, at);
"""


def artifact_kind(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    for kind, suffixes in _EXTENSIONS.items():
        if suffix in suffixes:
            return kind
    return "other"


def media_type(path: str, kind: str) -> str:
    """The ``Content-Type`` a snapshot is served with (text kinds as UTF-8 text)."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _MIME:
        return _MIME[suffix]
    return "text/plain" if kind in _TEXT_KINDS else "application/octet-stream"


def artifact_id(relative: str) -> str:
    """A stable opaque id for a relative path (no path in URLs or the audit)."""
    return "a" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]


def eligible_path(policy: Any, path: Any) -> tuple[str | None, str]:
    """``(relative POSIX path, "ok")`` if ``path`` may be an artifact, else ``(None, reason)``.

    Uses the write side's normalization (``WritePathPolicy.absolute`` / ``check``) and the
    read floor (``check_read``); never a lexical shortcut of its own beyond ``normpath``.
    """
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        return None, "invalid_path"
    workspace = getattr(policy, "workspace", None)
    if workspace is None:
        return None, "outside_workspace"
    try:
        workspace = Path(workspace)
        lexical = Path(os.path.normpath(policy.absolute(path)))
        try:
            relative = lexical.relative_to(workspace)
        except ValueError:
            return None, "outside_workspace"
        parts = relative.parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            return None, "outside_workspace"
        if any(part.startswith(".") for part in parts):
            return None, "hidden"
        posix = PurePosixPath(*parts).as_posix()
        if len(posix) > MAX_PATH_CHARS or mask_secrets(posix) != posix:
            return None, "secret_name"
        # No symlink anywhere below the workspace (lstat walk), and the resolved path is
        # the place that was named.
        current = workspace
        for part in parts:
            current = current / part
            if current.is_symlink():
                return None, "symlink"
        if os.path.lexists(lexical) and Path(os.path.realpath(lexical)) != workspace.joinpath(
            *parts
        ):
            return None, "symlink"
        decision = policy.check(str(lexical))
        if not decision.allowed:
            return None, "floor" if decision.code == "floor" else "outside_workspace"
        read = policy.check_read(str(lexical))
        if not read.allowed:
            return None, "floor"
    except (OSError, RuntimeError, ValueError):
        return None, "invalid_path"
    return posix, "ok"


_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FD_CHAIN = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd


def _open_below(workspace: Path, path: Path, flags: int) -> int:
    """``path`` opened by a descriptor chain from ``workspace`` (#601 C27).

    The workspace itself and every directory below it are opened with ``O_NOFOLLOW``,
    each relative to its parent's descriptor, so no component at or below the workspace
    can be a symlink at the moment it is used — there is no gap between a check and the
    open. The workspace's ancestors (Stira's data directory and above) are the trust
    boundary. A directory that is a symlink (or not a directory) raises
    ``_SwappedDirectory``; the final open's own errors propagate.

    ``workspace`` must be the canonical (``realpath``) workspace, never a configured
    spelling through a symlink: ``WritePathPolicy.workspace`` is its ``base_dir``, which
    ``IdentityPathPolicy.__init__`` resolves (``identity_paths.py``), also for a
    ``workspace_dir`` set through a symlink (pinned by a test).
    """
    parts = path.relative_to(workspace).parts
    if not parts:
        raise ValueError("no file below the workspace")
    try:
        current = os.open(workspace, _DIR_FLAGS)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _SwappedDirectory from error
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                raise
            except OSError as error:
                raise _SwappedDirectory from error
            os.close(current)
            current = following
        return os.open(parts[-1], flags, dir_fd=current)
    finally:
        os.close(current)


class _SwappedDirectory(OSError):
    """A directory at or below the workspace is a symlink (or no directory) now."""


def _still_names(workspace: Path, path: Path, opened: os.stat_result) -> bool:
    """No symlink from ``workspace`` (itself included) down to ``path`` and ``path`` is the
    opened inode — the fallback where descriptors cannot be chained (Windows)."""
    try:
        if workspace.is_symlink():
            return False
        current = workspace
        for part in path.relative_to(workspace).parts:
            current = current / part
            if current.is_symlink():
                return False
        now = os.stat(path)
    except (OSError, ValueError):
        return False
    return (now.st_dev, now.st_ino) == (opened.st_dev, opened.st_ino)


def _read_regular(
    path: Path, limit: int, *, named_by: Path | None = None
) -> tuple[bytes | None, str]:
    """The file's bytes (no final symlink followed, regular file only, at most ``limit``).

    ``named_by`` (a workspace, #601 C27): ``O_NOFOLLOW`` covers the final component only,
    so the file is opened by a descriptor chain from the workspace (:func:`_open_below`) and
    a directory swapped for a symlink after ``eligible_path`` — the workspace itself
    included — reads as ``symlink``. Without descriptor chains the path is re-checked
    after the open (:func:`_still_names`).
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    chained = named_by is not None and _FD_CHAIN
    try:
        fd = _open_below(named_by, path, flags) if chained else os.open(path, flags)
    except FileNotFoundError:
        return None, "missing"
    except _SwappedDirectory:
        return None, "symlink"
    except (OSError, ValueError):
        return None, "not_regular"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not_regular"
        if named_by is not None and not chained and not _still_names(named_by, path, info):
            return None, "symlink"
        if info.st_size > limit:
            return None, "too_large"
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                return None, "too_large"
            chunks.append(chunk)
        return b"".join(chunks), "ok"
    except OSError:
        return None, "io_error"
    finally:
        os.close(fd)


def _clean_turn(turn_id: Any) -> str | None:
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 64:
        return None
    if not all(ch.isalnum() or ch in "._:-" for ch in turn_id):
        return None
    return turn_id


class WorkspaceArtifacts:
    """The registry: SQLite facts plus content-addressed snapshots under ``root``."""

    def __init__(
        self,
        root: str | os.PathLike,
        *,
        max_artifacts: int = MAX_ARTIFACTS,
        max_versions: int = MAX_VERSIONS,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
        max_store_bytes: int = MAX_STORE_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root, self.blobs):
            if directory.is_symlink():
                raise ValueError("artifact store directory must not be a symlink")
        self.max_artifacts = max_artifacts
        self.max_versions = max_versions
        self.max_file_bytes = max_file_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_store_bytes = max_store_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "index.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
        self.skipped: Counter[str] = Counter()
        self.hash_only = 0
        self._stored_bytes = self._scan_store_bytes()  # once; kept up to date below

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- registration -------------------------------------------------------------------

    def register(
        self, policy: Any, path: Any, *, turn_id: Any = None, tool: str = "file_write"
    ) -> dict[str, Any]:
        """Record the file at ``path`` (as the tool was given it) as a new version.

        Returns ``{"status": "registered"|"unchanged"|"skipped", ...}``; a skip carries a
        fixed ``reason`` word and is counted in :attr:`skipped`.
        """
        relative, reason = eligible_path(policy, path)
        if relative is None:
            return self._skip(reason)
        absolute = Path(policy.workspace).joinpath(*PurePosixPath(relative).parts)
        data, reason = _read_regular(absolute, self.max_file_bytes, named_by=Path(policy.workspace))
        if data is None:
            return self._skip(reason)
        digest = hashlib.sha256(data).hexdigest()
        now = self._clock()
        ident = artifact_id(relative)
        kind = artifact_kind(relative)
        with self._lock:
            row = self._db.execute("SELECT latest FROM artifacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                count = self._db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
                if count >= self.max_artifacts:
                    self.skipped["limit_artifacts"] += 1
                    return {"status": "skipped", "reason": "limit_artifacts"}
                version, op = 1, "create"
            else:
                latest = self._db.execute(
                    "SELECT sha256 FROM versions WHERE artifact_id = ? AND version = ?",
                    (ident, row["latest"]),
                ).fetchone()
                if latest is not None and latest["sha256"] == digest:
                    return {"status": "unchanged", "id": ident, "version": row["latest"]}
                version, op = row["latest"] + 1, "update"
            stored = self._store_blob(digest, data)
            with self._db:
                if row is None:
                    self._db.execute(
                        "INSERT INTO artifacts (id, path, kind, created_at, updated_at, latest)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (ident, relative, kind, now, now, version),
                    )
                else:
                    self._db.execute(
                        "UPDATE artifacts SET updated_at = ?, latest = ? WHERE id = ?",
                        (now, version, ident),
                    )
                self._db.execute(
                    "INSERT INTO versions (artifact_id, version, turn_id, op, size, sha256, at,"
                    " stored, tool) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ident,
                        version,
                        _clean_turn(turn_id),
                        op,
                        len(data),
                        digest,
                        now,
                        int(stored),
                        tool if tool in PATH_WRITE_TOOLS else "other",
                    ),
                )
            self._prune(ident)
        return {"status": "registered", "id": ident, "version": version, "op": op}

    def _skip(self, reason: str) -> dict[str, Any]:
        self.skipped[reason] += 1
        return {"status": "skipped", "reason": reason}

    def _blob(self, digest: str) -> Path:
        return self.blobs / digest

    def _scan_store_bytes(self) -> int:
        total = 0
        try:
            with os.scandir(self.blobs) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            return self.max_store_bytes
        return total

    def _store_blob(self, digest: str, data: bytes) -> bool:
        """Keep a snapshot when it fits; ``False`` = a hash-only version (warned)."""
        blob = self._blob(digest)
        if os.path.lexists(blob):
            return blob.is_file() and not blob.is_symlink()
        if len(data) > self.max_snapshot_bytes:
            self.hash_only += 1
            return False
        if self._stored_bytes + len(data) > self.max_store_bytes:
            self.hash_only += 1
            return False
        temporary = self.blobs / f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temporary, blob)
            self._stored_bytes += len(data)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            self.hash_only += 1
            return False
        return True

    def _prune(self, ident: str) -> None:
        """Keep the newest ``max_versions`` of one artifact; drop unreferenced snapshots."""
        old = self._db.execute(
            "SELECT version, sha256 FROM versions WHERE artifact_id = ?"
            " ORDER BY version DESC LIMIT -1 OFFSET ?",
            (ident, self.max_versions),
        ).fetchall()
        if not old:
            return
        with self._db:
            self._db.executemany(
                "DELETE FROM versions WHERE artifact_id = ? AND version = ?",
                [(ident, row["version"]) for row in old],
            )
        for digest in {row["sha256"] for row in old}:
            used = self._db.execute(
                "SELECT 1 FROM versions WHERE sha256 = ? LIMIT 1", (digest,)
            ).fetchone()
            if used is None:
                blob = self._blob(digest)
                try:
                    size = blob.lstat().st_size
                    blob.unlink()
                    self._stored_bytes = max(0, self._stored_bytes - size)
                except OSError:
                    pass

    # --- reading -------------------------------------------------------------------------

    @staticmethod
    def _labels(text: str) -> list[str]:
        return [label for label in text.split(",") if label]

    def _view(self, row: Mapping[str, Any], latest: Mapping[str, Any] | None) -> dict:
        return {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "version": row["latest"],
            "labels": self._labels(row["labels"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "size": latest["size"] if latest else None,
            "sha256": latest["sha256"] if latest else None,
            "turn_id": latest["turn_id"] if latest else None,
            "stored": bool(latest["stored"]) if latest else False,
        }

    def _visible(self, policy: Any, relative: str) -> bool:
        """The registration rules again, now: a later symlink or floor hides it."""
        if policy is None:
            return False
        shown, _reason = eligible_path(policy, relative)
        return shown == relative

    def list(
        self,
        policy: Any,
        *,
        kind: str | None = None,
        turn_id: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Newest first; ``turn_id`` = artifacts with a version written in that turn."""
        limit = max(1, min(int(limit), MAX_LIST))
        clauses, values = [], []
        if kind:
            clauses.append("a.kind = ?")
            values.append(kind)
        if turn_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM versions v WHERE v.artifact_id = a.id AND v.turn_id = ?)"
            )
            values.append(turn_id)
        if query:
            clauses.append("instr(lower(a.path), ?) > 0")
            values.append(query.lower()[:200])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        out: list[dict] = []
        with self._lock:
            # Bounded scan: at most a few pages past the limit for hidden rows.
            rows = self._db.execute(
                f"SELECT * FROM artifacts a {where} ORDER BY a.updated_at DESC, a.id LIMIT ?",
                (*values, limit * 2 + 20),
            ).fetchall()
            latest = (
                {
                    (r["artifact_id"], r["version"]): r
                    for r in self._db.execute(
                        "SELECT v.* FROM versions v JOIN artifacts a ON a.id = v.artifact_id"
                        " AND a.latest = v.version"
                    ).fetchall()
                }
                if rows
                else {}
            )
        for row in rows:
            if len(out) >= limit:
                break
            if self._visible(policy, row["path"]):
                out.append(self._view(row, latest.get((row["id"], row["latest"]))))
        return out

    def turn_counts(self, policy: Any, turn_ids: Any) -> dict[str, int]:
        """#535: ``{turn id: artifacts with a version written in that turn}`` for at most
        ``TURN_COUNTS_MAX`` ids (counts only, visible paths only, turns without one left out).
        """
        wanted = []
        for turn in turn_ids or ():
            clean = _clean_turn(turn)
            if clean is not None and clean not in wanted:
                wanted.append(clean)
        wanted = wanted[:TURN_COUNTS_MAX]
        if not wanted:
            return {}
        marks = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT v.turn_id AS turn_id, a.path AS path FROM versions v"
                f" JOIN artifacts a ON a.id = v.artifact_id WHERE v.turn_id IN ({marks})"
                " LIMIT ?",
                (*wanted, MAX_LIST * TURN_COUNTS_MAX),
            ).fetchall()
        out: dict[str, int] = {}
        for row in rows:
            if self._visible(policy, row["path"]):
                out[row["turn_id"]] = out.get(row["turn_id"], 0) + 1
        return out

    def get(self, policy: Any, ident: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM artifacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return None
            versions = self._db.execute(
                "SELECT * FROM versions WHERE artifact_id = ? ORDER BY version DESC", (ident,)
            ).fetchall()
        if not self._visible(policy, row["path"]):
            return None
        latest = next((v for v in versions if v["version"] == row["latest"]), None)
        view = self._view(row, latest)
        view["versions"] = [
            {
                "version": v["version"],
                "turn_id": v["turn_id"],
                "op": v["op"],
                "size": v["size"],
                "sha256": v["sha256"],
                "at": v["at"],
                "stored": bool(v["stored"]),
                "tool": v["tool"],
            }
            for v in versions
        ]
        view.update(self._extras(ident))
        return view

    def content(self, policy: Any, ident: str, version: int | None = None) -> dict | None:
        """``{artifact, version, data: bytes | None}``; ``data`` None = hash-only/missing."""
        artifact = self.get(policy, ident)
        if artifact is None:
            return None
        number = artifact["version"] if version is None else version
        entry = next((v for v in artifact["versions"] if v["version"] == number), None)
        if entry is None:
            return None
        data = self._load_blob(entry["sha256"]) if entry["stored"] else None
        return {"artifact": artifact, "version": entry, "data": data}

    def _load_blob(self, digest: str) -> bytes | None:
        blob = self._blob(digest)
        data, _reason = _read_regular(blob, self.max_snapshot_bytes)
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            return None
        return data

    def diff(self, policy: Any, ident: str, old: int, new: int) -> dict | None:
        """A line diff of two stored text versions (bounded); ``None`` = not found."""
        first = self.content(policy, ident, old)
        second = self.content(policy, ident, new)
        if first is None or second is None:
            return None
        if first["artifact"]["kind"] not in _TEXT_KINDS:
            return {"available": False, "reason": "binary"}
        if first["data"] is None or second["data"] is None:
            return {"available": False, "reason": "not_stored"}
        if max(len(first["data"]), len(second["data"])) > MAX_DIFF_INPUT_BYTES:
            return {"available": False, "reason": "too_large"}
        try:
            before = first["data"].decode("utf-8").splitlines()
            after = second["data"].decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return {"available": False, "reason": "binary"}
        lines, truncated = [], False
        for line in difflib.unified_diff(before, after, f"v{old}", f"v{new}", lineterm="", n=3):
            if len(lines) >= MAX_DIFF_LINES:
                truncated = True
                break
            lines.append(line)
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        return {
            "available": True,
            "from": old,
            "to": new,
            "lines": lines,
            "added": added,
            "removed": removed,
            "truncated": truncated,
        }

    # --- a person's labels (ADR 0020 §7) and page_check results (§6) ---------------------

    def set_labels(
        self,
        policy: Any,
        ident: str,
        labels: Any,
        *,
        principal: str,
        before_write: Callable[[dict], Any] | None = None,
    ) -> dict | None:
        """Replace an artifact's labels with ``labels`` (a subset of :data:`LABELS`).

        Only the dashboard route calls this (no agent tool does). ``before_write`` gets
        ``{id, labels, previous, version}`` after validation and before the change is
        committed (the route writes its audit row there); if it raises, nothing changes.
        ``None`` = not found / not shown; ``ValueError`` = an invalid label list.
        """
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise ValueError("invalid_labels")
        if not set(labels) <= set(LABELS) or len(labels) > len(LABELS):
            raise ValueError("invalid_labels")
        chosen = [label for label in LABELS if label in labels]
        if self.get(policy, ident) is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT labels, latest FROM artifacts WHERE id = ?", (ident,)
            ).fetchone()
            if row is None:
                return None
            change = {
                "id": ident,
                "labels": chosen,
                "previous": self._labels(row["labels"]),
                "version": row["latest"],
            }
            if change["labels"] == change["previous"]:
                return {**change, "changed": False}
            if before_write is not None:
                before_write(change)
            with self._db:
                self._db.execute(
                    "UPDATE artifacts SET labels = ? WHERE id = ?", (",".join(chosen), ident)
                )
                self._db.execute(
                    "INSERT INTO label_history (artifact_id, at, labels, previous, version,"
                    " principal) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ident,
                        self._clock(),
                        ",".join(chosen),
                        row["labels"],
                        row["latest"],
                        principal,
                    ),
                )
                self._db.execute(
                    "DELETE FROM label_history WHERE artifact_id = ? AND rowid NOT IN ("
                    "SELECT rowid FROM label_history WHERE artifact_id = ?"
                    " ORDER BY at DESC, rowid DESC LIMIT ?)",
                    (ident, ident, MAX_LABEL_HISTORY),
                )
        return {**change, "changed": True}

    def record_check(self, policy: Any, path: Any, summary: Mapping, *, turn_id: Any) -> bool:
        """Keep a ``page_check`` summary (codes and counts only) for a registered artifact."""
        relative, _reason = eligible_path(policy, path)
        if relative is None:
            return False
        ident = artifact_id(relative)
        text = json.dumps(dict(summary), sort_keys=True)[:4000]
        with self._lock:
            row = self._db.execute("SELECT latest FROM artifacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return False
            with self._db:
                self._db.execute(
                    "INSERT INTO checks (artifact_id, version, turn_id, at, summary)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ident, row["latest"], _clean_turn(turn_id), self._clock(), text),
                )
                self._db.execute(
                    "DELETE FROM checks WHERE artifact_id = ? AND rowid NOT IN ("
                    "SELECT rowid FROM checks WHERE artifact_id = ?"
                    " ORDER BY at DESC, rowid DESC LIMIT ?)",
                    (ident, ident, MAX_CHECKS),
                )
        return True

    def _extras(self, ident: str) -> dict[str, list]:
        with self._lock:
            history = self._db.execute(
                "SELECT * FROM label_history WHERE artifact_id = ? ORDER BY at DESC, rowid DESC",
                (ident,),
            ).fetchall()
            checks = self._db.execute(
                "SELECT * FROM checks WHERE artifact_id = ? ORDER BY at DESC, rowid DESC",
                (ident,),
            ).fetchall()
        return {
            "label_history": [
                {
                    "at": h["at"],
                    "labels": self._labels(h["labels"]),
                    "previous": self._labels(h["previous"]),
                    "version": h["version"],
                    "by": h["principal"],
                }
                for h in history
            ],
            "checks": [
                {
                    "version": c["version"],
                    "turn_id": c["turn_id"],
                    "at": c["at"],
                    "summary": json.loads(c["summary"]),
                }
                for c in checks
            ],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            count = self._db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        return {
            "count": count,
            "limits": {
                "artifacts": self.max_artifacts,
                "versions": self.max_versions,
                "file_bytes": self.max_file_bytes,
                "snapshot_bytes": self.max_snapshot_bytes,
                "store_bytes": self.max_store_bytes,
            },
            "warnings": {
                "limit_artifacts": self.skipped.get("limit_artifacts", 0),
                "hash_only": self.hash_only,
                "too_large": self.skipped.get("too_large", 0),
            },
        }


class ArtifactHook(HookProvider):
    """Register what a successful mutating ``file_write`` / ``editor`` call wrote.

    ``policy_provider`` returns the server's ``WritePathPolicy`` (the one the tools use);
    ``turn_id_provider`` the running turn's id. A failure here never changes the tool
    result and logs the exception class only.
    """

    def __init__(
        self,
        registry: WorkspaceArtifacts,
        *,
        policy_provider: Callable[[], Any],
        turn_id_provider: Callable[[], Any] = lambda: None,
    ) -> None:
        self.registry = registry
        self.policy_provider = policy_provider
        self.turn_id_provider = turn_id_provider

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use
        name = tool_use.get("name")
        result = event.result
        if name == PAGE_CHECK_TOOL and isinstance(result, Mapping):
            self._page_check(result)
            return
        if name not in PATH_WRITE_TOOLS or not isinstance(result, Mapping):
            return
        if result.get("status") != "success":
            return
        arguments = tool_use.get("input")
        if not isinstance(arguments, Mapping) or editor_read_only(name, dict(arguments)):
            return
        path = arguments.get("path")
        if name == EDITOR_TOOL_NAME and isinstance(path, str):
            path = from_editor_path(path)
        try:
            self.registry.register(
                self.policy_provider(), path, turn_id=self.turn_id_provider(), tool=name
            )
        except Exception as error:  # noqa: BLE001 - registration must never break a turn
            _LOG.warning("Artifact registration failed (%s)", type(error).__name__)

    def _page_check(self, result: Mapping) -> None:
        """#449 beside the preview: the codes and counts ``page_check.for_log`` keeps."""
        try:
            from .page_check import for_log

            blocks = result.get("content") or ()
            first = blocks[0].get("text") if blocks and isinstance(blocks[0], Mapping) else None
            data = json.loads(first) if isinstance(first, str) else None
            opened = data.get("open_yourself") if isinstance(data, Mapping) else None
            path = opened.get("path") if isinstance(opened, Mapping) else None
            if not isinstance(path, str):
                return
            summary = {k: v for k, v in for_log(data).items() if k != "target"}
            self.registry.record_check(
                self.policy_provider(), path, summary, turn_id=self.turn_id_provider()
            )
        except Exception as error:  # noqa: BLE001 - never break a turn
            _LOG.warning("Artifact check record failed (%s)", type(error).__name__)
