"""Shared helpers for the release tools: the exclude list and the tracked file set."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_FILE = ROOT / "release-exclude.txt"


def load_patterns(path: Path = EXCLUDE_FILE) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def is_excluded(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    for pat in patterns:
        if pat.endswith("/"):
            name = pat.rstrip("/")
            if rel.startswith(pat) or ("/" not in name and name in parts[:-1]):
                return True
        elif "/" in pat:
            if fnmatch.fnmatch(rel, pat):
                return True
        elif fnmatch.fnmatch(parts[-1], pat):
            return True
    return False


def tracked_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def release_files(root: Path = ROOT, patterns: list[str] | None = None) -> list[str]:
    pats = load_patterns() if patterns is None else patterns
    return [p for p in tracked_files(root) if not is_excluded(p, pats)]
