"""The server data directory's write floor, shared by the server and ``stira doctor`` (#271).

The whole data directory is on the file tools' floor. Only strictly below a few
subdirectories with **fixed names** can the agent write. A name counts only when
every component from the data directory down is a real directory, checked with
``lstat``. A symlink anywhere on the way is never followed and never promoted to
an exception, however it got there (also when it was there before start-up). A
rejected name is dropped with a warning; its contents stay on the floor.

The same names are the only data-directory subdirectories file tools may *read*
(#312, :func:`readable_names`, ``WritePathPolicy.check_read``): one judgement for
both floors.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy, broad_root_problem

WORKSPACE_NAME = "workspace"
CONTEXT_STASH_NAME = "context-stash"


class DataDirSettingError(ValueError):
    """``STIRA_DATA_DIR`` cannot be used. The message names the variable, never its value."""


def is_fully_qualified(raw: str, pure: type[PurePath] = PurePath) -> bool:
    """Absolute on this platform's rules (``pure`` is for tests of the other one).

    Windows: ``C:data`` (the drive's current directory) and ``\\data`` (the current
    drive) are *not* absolute; only ``C:\\data`` and UNC paths are.
    """
    return pure(raw).is_absolute()


def explicit_data_dir(environ: Mapping[str, str]) -> Path | None:
    """The data directory ``STIRA_DATA_DIR`` names (#272), resolved; ``None`` when unset.

    Unset or empty: ``None`` (the server keeps its default ``<repository>/.local/stira``,
    nothing moves). Set: it must be an absolute path, so it never depends on the working
    directory. Symlinks are resolved, so the write/read floor and the policy files are
    the real paths (#243/#271). A filesystem root, the home directory or one of its
    ancestors is refused (the whole data directory is on the agent's floor), and so is
    an existing non-directory. Nothing is created, moved or copied here.
    """
    raw = environ.get("STIRA_DATA_DIR") or ""
    if not raw.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise DataDirSettingError("STIRA_DATA_DIR contains a control character")
    if not is_fully_qualified(raw):
        raise DataDirSettingError(
            "STIRA_DATA_DIR must be an absolute path (it is never resolved against the "
            "working directory)"
        )
    path = Path(raw)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise DataDirSettingError("STIRA_DATA_DIR cannot be resolved") from None
    # A symlink may resolve to a name the value itself did not contain: the wrappers
    # read the result as one line, so the resolved path gets the same check.
    if any(ord(char) < 32 or ord(char) == 127 for char in str(resolved)):
        raise DataDirSettingError("STIRA_DATA_DIR contains a control character")
    # Every file-system probe below can raise (permissions, I/O): one fixed message,
    # never the OSError (it carries the path).
    try:
        broad = broad_root_problem(resolved) is not None
        problem = None
        # The directory itself or, when it does not exist yet, its nearest existing
        # ancestor must be a directory (a path below a regular file can never be created).
        for candidate in (resolved, *resolved.parents):
            if candidate.exists():
                if not candidate.is_dir():
                    problem = "STIRA_DATA_DIR is not a directory (or is below a file)"
                break
    except (OSError, RuntimeError, ValueError):
        raise DataDirSettingError("STIRA_DATA_DIR cannot be checked") from None
    if broad:
        raise DataDirSettingError(
            "STIRA_DATA_DIR is a filesystem root, the home directory or one of its ancestors"
        )
    if problem is not None:
        raise DataDirSettingError(problem)
    return resolved


def writable_names(agent_id: str = "stira") -> tuple[str, ...]:
    """The data-directory subdirectories file tools may write (POSIX, relative, fixed)."""
    return (WORKSPACE_NAME, f"memory/agents/{agent_id}/memory/episodic", CONTEXT_STASH_NAME)


def readable_names(agent_id: str = "stira") -> tuple[str, ...]:
    """The data-directory subdirectories file tools may read (#312): the same names.

    One list for both floors, so the read floor never drifts from the write floor;
    each name counts only after :func:`name_problem` passed it (``build_policy``).
    """
    return writable_names(agent_id)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def name_problem(data_dir: str | os.PathLike, name: str) -> str | None:
    """Why ``name`` cannot be a floor exception under ``data_dir``, or ``None``.

    Pure ``lstat`` walk from ``data_dir``: every component must exist and be a
    directory, not a symlink. ``..``, ``.``, empty and absolute names are refused.
    """
    relative = PurePosixPath(name)
    if (
        not name.strip()
        or "\\" in name
        or relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or str(relative) != name
    ):
        return "not a plain relative name"
    current = Path(data_dir)
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return f"{current} does not exist"
        except OSError as error:
            return f"{current} cannot be checked ({type(error).__name__})"
        if stat.S_ISLNK(info.st_mode):
            return f"{current} is a symbolic link"
        if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            # #601: a Windows junction is a directory to S_ISDIR, not a symlink to S_ISLNK.
            return f"{current} is a reparse point (junction)"
        if not stat.S_ISDIR(info.st_mode):
            return f"{current} is not a directory"
    return None


@dataclass(frozen=True)
class DataDirFloor:
    """What the floor allows in one data directory, and what was refused."""

    data_dir: Path
    writable: tuple[str, ...]
    problems: tuple[str, ...]
    # Set by build_policy: the effective write roots, and whether "@workspace" is
    # one (False when the default workspace failed its check).
    roots: tuple[str, ...] = ()
    workspace_enabled: bool = True


def check_data_dir(data_dir: str | os.PathLike, names: Iterable[str] | None = None) -> DataDirFloor:
    """Keep the names that pass :func:`name_problem`; report the others."""
    writable, problems = [], []
    for name in writable_names() if names is None else names:
        problem = name_problem(data_dir, name)
        if problem is None:
            writable.append(name)
        else:
            problems.append(f"{name}: {problem}")
    return DataDirFloor(Path(data_dir), tuple(writable), tuple(problems))


def default_workspace(config: Any, data_dir: str | os.PathLike) -> Path:
    """``@workspace``: ``workspace_dir`` when set, else ``<data dir>/workspace`` (lexical)."""
    if getattr(config, "workspace_dir", None):
        return Path(config.workspace_dir).expanduser()
    return Path(data_dir) / WORKSPACE_NAME


def _default_workspace_refused(config: Any, floor: DataDirFloor) -> bool:
    """The default workspace failed its check: never resolve it into a root."""
    return not getattr(config, "workspace_dir", None) and WORKSPACE_NAME not in floor.writable


def effective_write_roots(config: Any, floor: DataDirFloor) -> list[str]:
    """Configured roots; with the default workspace also the episodic layer and stash.

    Before #271 the default ``@workspace`` was the whole data directory, so the
    episodic layer and the context stash were inside it. They are write roots now
    only while ``@workspace`` is the default and configured, and only when they
    passed :func:`check_data_dir`; an explicit ``workspace_dir`` adds nothing.
    """
    return _effective_roots(config, floor, list(config.write_roots))


def effective_read_roots(config: Any, floor: DataDirFloor) -> list[str]:
    """``read_roots`` (#457) by the same rule as :func:`effective_write_roots`.

    With the default ``@workspace`` the episodic layer and the context stash stay
    readable (the #312 exceptions); a refused default workspace is no root at all.
    """
    return _effective_roots(config, floor, list(getattr(config, "read_roots", [WORKSPACE_ROOT])))


def _effective_roots(config: Any, floor: DataDirFloor, roots: list[str]) -> list[str]:
    if not getattr(config, "workspace_dir", None) and WORKSPACE_ROOT in roots:
        roots.extend(
            str(floor.data_dir / name) for name in floor.writable if name != WORKSPACE_NAME
        )
    if _default_workspace_refused(config, floor):
        # A symlinked/missing/non-directory workspace/ is not followed anywhere:
        # "@workspace" is disabled (no work area; episodic/stash only), see
        # operations.md §10 for choosing a workspace explicitly (#271).
        roots = [root for root in roots if root != WORKSPACE_ROOT]
    return list(dict.fromkeys(roots))


def build_policy(
    config: Any,
    data_dir: str | os.PathLike,
    *,
    read_only_paths: Iterable[str] = (),
    protected_dirs: Iterable[str | os.PathLike] = (),
    names: Iterable[str] | None = None,
) -> tuple[WritePathPolicy, DataDirFloor]:
    """The file tools' ``WritePathPolicy`` for a server whose data directory is ``data_dir``."""
    floor = check_data_dir(data_dir, names)
    refused = _default_workspace_refused(config, floor)
    roots = effective_write_roots(config, floor)
    floor = replace(floor, roots=tuple(roots), workspace_enabled=not refused)
    policy = WritePathPolicy(
        read_only_paths,
        # Refused workspace: relative paths anchor at the data directory itself,
        # which is on the floor (so they are refused), never at a symlink target.
        base_dir=Path(data_dir) if refused else default_workspace(config, data_dir),
        protected_dirs=protected_dirs,
        write_roots=roots,
        outside_action=config.write_outside_action,
        # "/" or the home directory as a root is a start-up error without opt-in.
        allow_broad_roots=config.allow_broad_write_roots,
        floor_tree=data_dir,
        floor_tree_writable=floor.writable,
        read_roots=effective_read_roots(config, floor),
        read_secret_paths=getattr(config, "read_secret_paths", ()),
    )
    return policy, floor


def shown_writable(policy: WritePathPolicy, floor: DataDirFloor) -> list[str]:
    """For the settings page: exceptions that are also inside the effective roots."""
    return [
        f"{name}/" for name in floor.writable if policy.within_roots(floor.data_dir / name / "x")
    ]


def shown_readable(policy: WritePathPolicy, floor: DataDirFloor) -> list[str]:
    """For the settings page (#312): data-directory subdirectories file tools may read.

    The exceptions the policy enforces (they passed :func:`check_data_dir`) that are
    also inside the read roots (#457); write roots do not narrow reads.
    """
    return [
        f"{name}/"
        for name in policy.floor_tree_writable
        if name in floor.writable and policy.check_read(str(floor.data_dir / name)).allowed
    ]


__all__ = [
    "CONTEXT_STASH_NAME",
    "WORKSPACE_NAME",
    "DataDirFloor",
    "DataDirSettingError",
    "build_policy",
    "check_data_dir",
    "default_workspace",
    "effective_read_roots",
    "effective_write_roots",
    "explicit_data_dir",
    "is_fully_qualified",
    "name_problem",
    "readable_names",
    "shown_readable",
    "shown_writable",
    "writable_names",
]
