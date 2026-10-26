"""Build the public snapshot: copy, strip links to excluded files, git init, scan, check, smoke.

Usage: python scripts/make_release_snapshot.py [--out DIR] [--no-smoke] [--persona-report]
Nothing is pushed. The JSON report (counts, kinds and paths only, never matched values) is
printed and written to <out>/../release-report.json. After the smoke, everything it generated
is removed and the file set is checked against "git ls-files minus release-exclude.txt".
scripts/publish_snapshot.py copies the result into a clone of the public repository.
See docs/release.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_links
import release_scan
from release_paths import ROOT, is_excluded, load_patterns, release_files

MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?\s*\)")
# The clean-venv smoke; docs/agent-setup.md uses the same commands.
SMOKE_INSTALL = ["-m", "pip", "install", "-q", "-e", ".[dev]"]
SMOKE_TESTS = ["tests/attack", "tests/test_server_startup_smoke.py"]


def strip_excluded_links(md: str, text: str, patterns: list[str]) -> str:
    """Replace links to excluded files with their plain text (public copy only)."""

    def repl(m: re.Match[str]) -> str:
        target = m.group(3)
        if "://" in target or target.startswith(("#", "mailto:")):
            return m.group(0)
        path = target.split("#", 1)[0]
        if not path:
            return m.group(0)
        joined = PurePosixPath(md).parent / path
        norm = check_links._normalise(joined)
        if norm and is_excluded(norm + ("/" if path.endswith("/") else ""), patterns):
            return m.group(2)
        return m.group(0)

    return MD_LINK.sub(repl, text)


def copy_tree(out: Path, files: list[str], patterns: list[str]) -> None:
    for rel in files:
        src, dst = ROOT / rel, out / rel
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".md"):
            text = src.read_text(encoding="utf-8")
            dst.write_text(strip_excluded_links(rel, text, patterns), encoding="utf-8")
            shutil.copymode(src, dst)
        else:
            shutil.copy2(src, dst)


def git_init(out: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=out,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", "-A", "-f")  # every copied file, also one the snapshot's .gitignore matches
    git("commit", "-q", "-m", "Initial public release")
    return git("rev-parse", "HEAD")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def source_info(root: Path) -> dict:
    """The development commit the snapshot was cut from (for the publish commit message)."""
    try:
        return {
            "source_commit": _git(root, "rev-parse", "HEAD"),
            "source_date": _git(root, "log", "-1", "--format=%cs"),
            "source_dirty": bool(_git(root, "status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"source_commit": None, "source_date": None, "source_dirty": None}


def clean_generated(out: Path) -> None:
    """Remove what the smoke (or anything else) left in the snapshot: untracked and ignored
    files such as __pycache__, *.egg-info, .pytest_cache, .ruff_cache and node_modules, and
    undo changes to committed files. Every snapshot file is committed by git_init()."""
    _git(out, "clean", "-fdxq")
    _git(out, "checkout", "-q", "--", ".")


def file_set_check(out: Path, expected: set[str]) -> dict:
    """Snapshot files (outside .git) against the expected set: {"extra": [...], "missing": [...]}."""
    actual = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if (p.is_file() or p.is_symlink()) and ".git" not in p.relative_to(out).parts[:1]
    }
    return {"extra": sorted(actual - expected), "missing": sorted(expected - actual)}


def smoke(out: Path) -> dict:
    venv = out.parent / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PYTHON"))}
    env.update(AWS_EC2_METADATA_DISABLED="true", HOME=str(out.parent / "home"))
    Path(env["HOME"]).mkdir(exist_ok=True)
    inst = subprocess.run(
        [str(py), *SMOKE_INSTALL], cwd=out, env=env, capture_output=True, text=True, check=False
    )
    if inst.returncode:
        return {"install": "failed", "tail": inst.stderr[-800:]}
    run = subprocess.run(
        [str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", *SMOKE_TESTS],
        cwd=out,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    last = run.stdout.strip().splitlines()[-1:] or [""]
    return {"install": "ok", "pytest_rc": run.returncode, "pytest_summary": last[0]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="empty directory outside the repository")
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--persona-report", action="store_true")
    args = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="stira-release-")) if args.out is None else args.out
    out = work / "StiraCore"
    if ROOT in out.resolve().parents:
        ap.error("--out must be outside the repository")
    if out.exists() and any(out.iterdir()):
        ap.error(f"{out} is not empty")
    patterns = load_patterns()
    files = release_files(ROOT, patterns)
    copy_tree(out, files, patterns)
    report: dict = {"snapshot": str(out.resolve()), "files": len(files)}
    report["commit"] = git_init(out)
    report.update(source_info(ROOT))
    deny = release_scan.load_denylist()
    if deny is None:
        print(
            f"WARNING: no denylist at {release_scan.DENYLIST}; name scan skipped", file=sys.stderr
        )
    elif not deny:
        print(f"WARNING: {release_scan.DENYLIST} has no words (template only)", file=sys.stderr)
    report["denylist_words"] = None if deny is None else len(deny)
    report["scan"] = release_scan.scan(out, files, deny)
    snap_files = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(out).parts
    }
    report["broken_links"] = check_links.broken_links(
        snap_files, lambda f: (out / f).read_text(encoding="utf-8", errors="replace")
    )
    report["persona_words"] = release_scan.persona_word_scan(out, files)
    if args.persona_report:
        report["persona"] = release_scan.persona_report(out, files)
    if not args.no_smoke:
        report["smoke"] = smoke(out)
    clean_generated(out)
    expected = {f for f in files if (ROOT / f).is_file()}
    report["file_set"] = file_set_check(out, expected)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (work / "release-report.json").write_text(text, encoding="utf-8")
    print(text)
    return 0 if report_ok(report) else 1


def report_ok(report: dict) -> bool:
    """No broken link, no previous-persona word (release_scan.PERSONA_WORDS), no blocking scan
    hit (denylist word, tracker link), smoke passed, and exactly the expected files."""
    file_set = report.get("file_set", {})
    return (
        not report["broken_links"]
        and report["persona_words"]["count"] == 0
        and not release_scan.blocking_kinds(report.get("scan", {}))
        and ("smoke" not in report or report["smoke"].get("pytest_rc") == 0)
        and not file_set.get("extra")
        and not file_set.get("missing")
    )


if __name__ == "__main__":
    sys.exit(main())
