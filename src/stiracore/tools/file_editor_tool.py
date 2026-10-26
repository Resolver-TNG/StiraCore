"""Stira's ``editor``: the SDK's sandbox-routed ``file_editor`` behind Stira's write policy (#245).

The upstream ``strands_tools.editor`` is deprecated (removed in strands-agents-tools
0.9.0) and has three defects measured with 0.8.9: it edits a per-process copy of
the file (an external change is overwritten by the next ``str_replace``), a
relative ``create`` fails with ``os.makedirs('')``, and the ``{path}.bak`` it
makes with ``shutil.copy2`` copies the ``uchg`` flag, so a retry keeps failing
after the original is unlocked.

Here the model-facing name stays ``editor`` (so every name-based policy, setting
and breaker list keeps applying), backed by ``strands.vended_tools.file_editor``
(identical in strands-agents 1.57.1 and 1.59.0) bound to :class:`StiraWriteSandbox`:

- every command reads the file from disk again; no cache, no ``.bak``;
- writes are allowed only inside an approved step of ``IdentityProtectedTool``
  (the gate grant), only to the exact path that wrapper checked, and only if
  every wrapper's :class:`~stiracore.identity_paths.WritePathPolicy` still
  allows it at write time (SOUL / IPD / ``protected_dirs`` / ``write_roots``);
- the file is opened without following a final symlink;
- no command or code execution, no removal.

Relative paths are resolved against the workspace by ``IdentityProtectedTool``
before the tool sees them. ``file_editor`` wants POSIX-absolute paths; on Windows
``C:\\x`` is passed as ``/C:/x`` and mapped back here (:func:`to_editor_path`).
"""

from __future__ import annotations

import os
import re
import stat as stat_module
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from strands.sandbox import ExecutionResult, FileInfo, Sandbox, StreamChunk
from strands.sandbox.errors import SandboxPathNotFoundError
from strands.vended_tools.file_editor import make_file_editor

from stiracore.identity_paths import READ_FLOOR_ERROR, secret_name

from .checked_open import open_checked
from .file_tools import FileReadError, active_read_floor, open_for_read
from .tool_consent import active_grant

EDITOR_TOOL_NAME = "editor"
EDITOR_DESCRIPTION = (
    "Filesystem editor for viewing, creating, and editing files: view (with line "
    "ranges), create (never overwrites), str_replace (old_str must be unique) and "
    "insert. Every command reads the current file from disk. Paths are absolute or "
    "relative to the agent workspace; writes follow Stira's write policy."
)
_DRIVE_FORM = re.compile(r"^/([A-Za-z]:)(?:[/\\]|$)")
_NOT_APPROVED = (
    "editor write refused: not inside a step Stira's approval gate granted for this "
    "exact path (use the tool registered by stiracore.tools.tool_registry)."
)


def to_editor_path(native: str, *, windows: bool | None = None) -> str:
    """The form ``file_editor`` accepts (POSIX-absolute) for a resolved native path."""
    windows = os.name == "nt" if windows is None else windows
    if windows and re.match(r"^[A-Za-z]:[/\\]", native):
        return "/" + native.replace("\\", "/")
    return native


def from_editor_path(path: str, *, windows: bool | None = None) -> str:
    """The native path for a ``file_editor`` path (inverse of :func:`to_editor_path`)."""
    windows = os.name == "nt" if windows is None else windows
    if windows and _DRIVE_FORM.match(path):
        return path[1:]
    return path


class StiraWriteSandbox(Sandbox):
    """File I/O for Stira's ``editor`` on the host, writes enforced by Stira's policy.

    Reads (``view``) follow Stira's read floor when the registry's wrapper holds one
    (#312: nothing in the data directory outside ``workspace/``, the episodic layer
    and ``context-stash/``); otherwise they read what the process can read. Not an
    isolation boundary for code: execution is always refused.
    """

    def __init__(self, tool_name: str = EDITOR_TOOL_NAME) -> None:
        self.tool_name = tool_name

    @staticmethod
    def _native(path: str) -> Path:
        native = from_editor_path(path)
        if not os.path.isabs(native):
            raise PermissionError(f"editor needs an absolute path, got {path!r}")
        return Path(native)

    def _authorize_write(self, path: str) -> Path:
        grant = active_grant(self.tool_name)
        if grant is None or grant.write_path is None:
            raise PermissionError(_NOT_APPROVED)
        native = self._native(path)
        if str(native) != grant.write_path:
            raise PermissionError(_NOT_APPROVED)
        for policy in grant.policies:
            decision = policy.check(str(native))
            if not decision.allowed:
                raise PermissionError(decision.reason)
            if decision.path != grant.write_path:  # the target changed since the check
                raise PermissionError(_NOT_APPROVED)
        return native

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        native = self._authorize_write(path)
        native.parent.mkdir(parents=True, exist_ok=True)  # parents were checked too
        grant = active_grant(self.tool_name)
        if grant is None:
            raise PermissionError(_NOT_APPROVED)
        # Open without truncating or following a final symlink, re-check the policy
        # on what the path names now (a swapped parent, #271), require the same
        # inode and a single link, and only then truncate (checked_open).
        descriptor = open_checked(str(native), grant.policies, grant.write_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        native = self._native(path)
        if active_read_floor() is None:
            return native.read_bytes()
        # Under the read floor (#312): checked before and after the open, no final
        # symlink, a regular file only (file_tools.open_for_read).
        try:
            _resolved, descriptor = open_for_read(str(native))
        except FileReadError as error:
            raise _os_error(error) from error
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        target = self._native(path)
        floor = active_read_floor()
        if floor is not None:
            return _list_under_floor(floor, target, path)
        try:
            scanner = os.scandir(target)
        except (FileNotFoundError, NotADirectoryError) as error:
            raise SandboxPathNotFoundError(path) from error
        results: list[FileInfo] = []
        with scanner as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                try:
                    stat = entry.stat()
                    results.append(
                        FileInfo(name=entry.name, is_dir=entry.is_dir(), size=stat.st_size)
                    )
                except OSError:
                    results.append(FileInfo(name=entry.name))
        return results

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        raise PermissionError("editor never removes files")

    async def execute_streaming(
        self, command: str, **kwargs: Any
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        raise PermissionError("editor's sandbox runs no commands")
        yield  # pragma: no cover - marks this as an async generator

    async def execute_code_streaming(
        self, code: str, language: str, **kwargs: Any
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        raise PermissionError("editor's sandbox runs no code")
        yield  # pragma: no cover - marks this as an async generator


def _list_under_floor(floor: Any, target: Path, shown: str) -> list[FileInfo]:
    """List ``target`` under the read floor (#312/#316), by descriptor, never by name.

    The directory is opened (``O_DIRECTORY | O_NOFOLLOW``: a final symlink is refused)
    and judged by the descriptor's own identity: a directory on the floor (the data
    directory, ``checkpoints/``, the memory tree above ``episodic/`` …) shows only its
    exception subdirectories that are real directories; any other directory shows its
    entries, except a hard link of a protected file. Entries are read from the
    descriptor (``os.scandir(fd)``) with ``lstat`` semantics, so a symlink is listed as a
    plain entry, never followed or walked. A path the floor refuses by name whose
    descriptor is not a floor directory (a swap), or a walk that fails, is refused.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as error:
        raise SandboxPathNotFoundError(shown) from error
    except NotADirectoryError as error:
        # macOS reports a final symlink under O_DIRECTORY|O_NOFOLLOW as ENOTDIR.
        if os.path.islink(target):
            raise PermissionError(f"{READ_FLOOR_ERROR}: listing refused") from error
        raise SandboxPathNotFoundError(shown) from error
    except OSError as error:
        raise PermissionError(f"{READ_FLOOR_ERROR}: listing refused") from error
    try:
        opened = os.fstat(descriptor)
        try:
            protected, names = floor.listing((opened.st_dev, opened.st_ino))
        except OSError as error:
            raise PermissionError(f"{READ_FLOOR_ERROR}: listing refused") from error
        if names is None:
            decision = floor.check_read(str(target))
            if not decision.allowed:
                reason = decision.reason if decision.code != "invalid" else READ_FLOOR_ERROR
                raise PermissionError(f"{reason}: listing refused")
        results: list[FileInfo] = []
        with os.scandir(descriptor) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if names is not None and entry.name not in names:
                    continue
                if secret_name(entry.name):
                    continue  # the secret-name floor (#457): never says such a file exists
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat_module.S_ISDIR(info.st_mode)
                if names is not None and not is_dir:
                    continue  # an exception that is not a real directory is not shown
                if (info.st_dev, info.st_ino) in protected:
                    continue
                results.append(FileInfo(name=entry.name, is_dir=is_dir, size=info.st_size))
        return results
    finally:
        os.close(descriptor)


def _os_error(error: FileReadError) -> OSError:
    """A failed checked read as the ``OSError`` the editor expects from a sandbox."""
    kinds = {
        "floor": PermissionError,
        "outside_read_roots": PermissionError,
        "secret_path": PermissionError,
        "permission_denied": PermissionError,
        "path_not_found": FileNotFoundError,
        "is_directory": IsADirectoryError,
    }
    return kinds.get(error.reason, OSError)(str(error))


def make_stira_editor():
    """The ``editor`` tool: SDK ``file_editor`` bound to a :class:`StiraWriteSandbox`."""
    return make_file_editor(
        sandbox=StiraWriteSandbox(), name=EDITOR_TOOL_NAME, description=EDITOR_DESCRIPTION
    )
