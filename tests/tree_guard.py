"""Which files a test run left in the checkout (#514); used by the root conftest.py.

Only new and missing files count (a changed one may be an editor's save during the run).
Skipped: caches, virtualenvs, node_modules, coverage data, editor/OS litter, and a data
directory under ``.local/`` that a running server owns (its ``server.lock`` names a live
process other than this one).
"""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
    | {".ruff_cache", ".mypy_cache", ".hypothesis", ".tox", "htmlcov"}
)
SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", "~")
SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a running process other than this one (unknown counts as running)?"""
    if pid == os.getpid():
        return False
    if os.name == "nt":
        return True  # os.kill(pid, 0) would end the process on Windows: assume it runs
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists, another user's (or Windows' answer): leave it alone
    return True


def owned_data_dirs(root: Path) -> list[Path]:
    """Data directories under ``root/.local`` that a running server holds now."""
    try:
        from stiracore.owner_lock import read_note_pid
    except ImportError:  # pragma: no cover
        return []
    local = root / ".local"
    if not local.is_dir():
        return []
    owned = []
    for lock in local.glob("*/server.lock"):
        pid = read_note_pid(lock)
        if pid is not None and pid_alive(pid):
            owned.append(lock.parent)
    return owned


def tree_files(
    root: Path, skip: tuple[Path, ...] = (), exclude: frozenset[str] = frozenset()
) -> set[str]:
    """Relative POSIX paths under ``root``: files and symlinks, plus empty directories and
    symlinks to directories (with a trailing ``/``); ``exclude`` is left out."""
    found = set()
    for here, dirs, files in os.walk(root):
        base = Path(here)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and base / d not in skip]
        for name in list(dirs):
            if (base / name).is_symlink():  # os.walk does not enter it: record it
                dirs.remove(name)
                found.add((base / name).relative_to(root).as_posix() + "/")
        kept = [
            name
            for name in files
            if not (
                name in SKIP_NAMES
                or name.endswith(SKIP_SUFFIXES)
                or name.startswith((".coverage", ".#"))
            )
        ]
        for name in kept:
            found.add((base / name).relative_to(root).as_posix())
        if not dirs and not files and base != root:
            found.add(base.relative_to(root).as_posix() + "/")
    # An excluded output's parent directories are not "empty" or "removed" either: an
    # empty reports/ at the start that holds the report at the end is no change.
    parents = {
        "/".join(parts[:i]) + "/"
        for parts in (path.split("/") for path in exclude)
        for i in range(1, len(parts))
    }
    return found - exclude - parents


def excluded_paths(root: Path, environ) -> frozenset[str]:
    """Intended outputs of the run, fixed at its start: ``STIRA_CONTRACT_REPORT`` (CI)."""
    report = (environ.get("STIRA_CONTRACT_REPORT") or "").strip()
    if not report:
        return frozenset()
    try:
        return frozenset({Path(report).resolve().relative_to(root.resolve()).as_posix()})
    except ValueError:  # outside the checkout: nothing to leave out
        return frozenset()


def tree_changes(before: set[str], after: set[str]) -> list[str]:
    return [f"+ {p}" for p in sorted(after - before)] + [f"- {p}" for p in sorted(before - after)]
