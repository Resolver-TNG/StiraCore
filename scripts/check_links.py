"""Check relative Markdown links against the public file set (release-exclude.txt applied).

Usage: python scripts/check_links.py [--root DIR] [--all] [--json]
  --root  check a directory as it is (e.g. a release snapshot): every *.md under it
  --all   in the repository, do not apply release-exclude.txt
Exit code 1 when a link points to a file that is missing from the checked set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_paths import ROOT, release_files, tracked_files

LINK = re.compile(
    r"(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|!\[[^\]]*\]\(\s*<?([^)\s>]+)"
)
FENCE = re.compile(r"^\s*(```|~~~)")
SKIP = ("http://", "https://", "mailto:", "#", "data:", "tel:")


def _links(text: str) -> list[tuple[int, str]]:
    found, fenced = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "", line)
        for m in LINK.finditer(line):
            found.append((no, m.group(1) or m.group(2)))
    return found


def broken_links(files: set[str], read) -> list[dict]:
    """``files``: POSIX paths of the checked set; ``read(path)`` returns a Markdown text."""
    dirs = {str(p) for f in files for p in PurePosixPath(f).parents}
    out = []
    for md in sorted(f for f in files if f.endswith(".md")):
        for no, target in _links(read(md)):
            if target.startswith(SKIP) or "://" in target:
                continue
            path = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path:
                continue
            base = PurePosixPath(md).parent
            joined = PurePosixPath(path.lstrip("/")) if path.startswith("/") else base / path
            norm = _normalise(joined)
            if norm is None or (norm not in files and norm not in dirs):
                out.append({"file": md, "line": no, "target": target})
    return out


def _normalise(p: PurePosixPath) -> str | None:
    parts: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) or "."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.root:
        root = args.root.resolve()
        files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        files = {f for f in files if not f.startswith(".git/")}
    else:
        root = ROOT
        files = set(tracked_files(root) if args.all else release_files(root))
    bad = broken_links(files, lambda f: (root / f).read_text(encoding="utf-8", errors="replace"))
    if args.json:
        print(json.dumps({"checked_files": len(files), "broken": bad}, ensure_ascii=False))
    else:
        for b in bad:
            print(f"{b['file']}:{b['line']}: {b['target']}")
        print(f"{len(bad)} broken relative link(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
