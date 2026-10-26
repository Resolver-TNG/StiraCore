"""Copy a release snapshot into a local clone of the public repository (docs/release.md).

Usage:
    python scripts/publish_snapshot.py --snapshot DIR --public-clone DIR            # dry run
    python scripts/publish_snapshot.py --snapshot DIR --public-clone DIR --commit   # commit only
    python scripts/publish_snapshot.py ... --commit --push --yes                    # and push

DIR for --snapshot is the --out directory of make_release_snapshot.py (it holds
release-report.json and StiraCore/) or the StiraCore/ directory itself. Standard library and
git only; nothing touches the network except `git push origin main` with --push --yes.
The dry run compares only and leaves the clone as it is.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_scan

DEFAULT_REMOTE = "Resolver-TNG/StiraCore"
REPORT = "release-report.json"
NOREPLY = "@users.noreply.github.com"


class PublishError(Exception):
    pass


def _env() -> dict[str, str]:
    # The identity comes from the clone's git config only, never from the environment.
    drop = ("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
    return {k: v for k, v in os.environ.items() if not k.startswith(drop)}


def git(cwd: Path, *args: str, check: bool = True, stdin: bytes | None = None) -> str:
    run = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, env=_env(), check=False
    )
    if check and run.returncode:
        err = run.stderr.decode("utf-8", "replace").strip()
        raise PublishError(f"git {args[0]} failed in {cwd}: {err}")
    return run.stdout.decode("utf-8", "replace").strip()


def ls_files(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, capture_output=True, env=_env(), check=True
    ).stdout.decode("utf-8")
    return {p for p in out.split("\0") if p}


def locate(snapshot: Path) -> tuple[Path, Path]:
    """(snapshot tree, report path) from either the --out directory or the tree itself."""
    if (snapshot / REPORT).is_file() and (snapshot / "StiraCore").is_dir():
        return snapshot / "StiraCore", snapshot / REPORT
    if (snapshot.parent / REPORT).is_file():
        return snapshot, snapshot.parent / REPORT
    raise PublishError(f"no {REPORT} for {snapshot} (run make_release_snapshot.py first)")


def check_report(report: dict, tree: Path, allow_no_smoke: bool) -> list[str]:
    errors = []
    if report.get("broken_links") != []:
        errors.append("report: broken_links is not empty")
    if (report.get("persona_words") or {}).get("count") != 0:
        errors.append("report: persona_words.count is not 0")
    if "smoke" not in report:
        if not allow_no_smoke:
            errors.append("report: built with --no-smoke (pass --allow-no-smoke to accept)")
    elif report["smoke"].get("pytest_rc") != 0:
        errors.append("report: smoke did not pass (smoke.pytest_rc != 0)")
    words = report.get("denylist_words")
    if not isinstance(words, int) or words < 1:
        errors.append("report: denylist_words < 1 (denylist missing or still the empty template)")
    blocking = release_scan.blocking_kinds(report.get("scan") or {})
    if blocking:
        errors.append(f"report: blocking scan hits: {', '.join(blocking)}")
    file_set = report.get("file_set")
    if not isinstance(file_set, dict):
        errors.append("report: no file_set check (rebuild with the current script)")
    elif file_set.get("extra") or file_set.get("missing"):
        errors.append("report: snapshot files differ from git ls-files minus release-exclude")
    if not report.get("source_commit") or not report.get("source_date"):
        errors.append("report: no source_commit / source_date (rebuild with the current script)")
    if report.get("source_dirty"):
        errors.append("report: the development tree had uncommitted changes")
    if Path(str(report.get("snapshot", ""))).resolve() != tree.resolve():
        errors.append("report: it describes another snapshot directory")
    head = git(tree, "rev-parse", "HEAD", check=False)
    if head != report.get("commit"):
        errors.append("snapshot: HEAD is not the commit in the report")
    if git(tree, "status", "--porcelain", "--ignored", check=False):
        errors.append("snapshot: untracked, ignored or modified files present")
    return errors


def remote_matches(url: str, expect: str) -> bool:
    def norm(s: str) -> str:
        s = s.strip().rstrip("/")
        return s.removesuffix(".git").lower()

    u, e = norm(url), norm(expect)
    return u == e or u.endswith(("/" + e, ":" + e))


def check_clone(clone: Path, expect_remote: str) -> list[str]:
    errors = []
    if not (clone / ".git").exists():
        return [f"public clone: {clone} is not a git clone"]
    url = git(clone, "remote", "get-url", "origin", check=False)
    if not url:
        errors.append("public clone: no origin remote")
    elif not remote_matches(url, expect_remote):
        errors.append(f"public clone: origin is not {expect_remote} (see --expect-remote)")
    if git(clone, "status", "--porcelain", check=False):
        errors.append("public clone: working tree is not clean")
    if git(clone, "symbolic-ref", "--short", "HEAD", check=False) != "main":
        errors.append("public clone: current branch is not main")
    return errors


def identity(clone: Path) -> tuple[str, str]:
    name = git(clone, "config", "--get", "user.name", check=False)
    email = git(clone, "config", "--get", "user.email", check=False)
    if not name or not email:
        raise PublishError("public clone: set user.name and user.email (noreply) in git config")
    if not email.endswith(NOREPLY):
        raise PublishError(f"public clone: user.email must be a GitHub noreply address ({NOREPLY})")
    return name, email


def _mode(p: Path) -> bool:
    return bool(p.stat().st_mode & 0o111)


def plan(tree: Path, clone: Path) -> dict[str, list[str]]:
    new, old = ls_files(tree), ls_files(clone)
    for rel in new:
        if (tree / rel).is_symlink() or not (tree / rel).is_file():
            raise PublishError(f"snapshot: {rel} is not a regular file")
    modified = [
        rel
        for rel in sorted(new & old)
        if not (clone / rel).is_file()
        or (clone / rel).is_symlink()
        or (tree / rel).read_bytes() != (clone / rel).read_bytes()
        or _mode(tree / rel) != _mode(clone / rel)
    ]
    return {"added": sorted(new - old), "modified": modified, "removed": sorted(old - new)}


def summary(changes: dict[str, list[str]]) -> str:
    lines = [", ".join(f"{k}: {len(v)}" for k, v in changes.items())]
    for kind, paths in changes.items():
        top = Counter(p.split("/", 1)[0] if "/" in p else "(root)" for p in paths)
        if top:
            lines.append(f"  {kind}: " + ", ".join(f"{d} {n}" for d, n in sorted(top.items())))
    return "\n".join(lines)


def apply(tree: Path, clone: Path, changes: dict[str, list[str]]) -> None:
    if changes["removed"]:
        paths = "\0".join(changes["removed"]).encode("utf-8")
        git(clone, "rm", "-q", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=paths)
    copy = changes["added"] + changes["modified"]
    for rel in copy:
        dst = clone / rel
        if dst.is_symlink() or dst.is_dir():
            raise PublishError(f"public clone: {rel} is a directory or symlink")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tree / rel, dst)
    if copy:
        paths = "\0".join(copy).encode("utf-8")
        git(clone, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=paths)


def commit_message(report: dict, changes: dict[str, list[str]]) -> str:
    short = str(report["source_commit"])[:7]
    body = ", ".join(f"{k.capitalize()}: {len(v)}" for k, v in changes.items())
    return f"Sync from development snapshot {short} ({report['source_date']})\n\n{body}.\n"


def push(clone: Path) -> None:
    head = git(clone, "rev-parse", "HEAD")
    remote = git(clone, "rev-parse", "--verify", "-q", "refs/remotes/origin/main", check=False)
    span = f"{remote}..HEAD" if remote else "HEAD"
    log = git(clone, "log", "--format=%h %s", span)
    if not log:
        print("nothing to push: main is not ahead of origin/main")
        return
    url = git(clone, "remote", "get-url", "origin")
    print(f"pushing main {head[:12]} to origin ({url}) main, over {remote[:12] or '(empty)'}:")
    print("\n".join("  " + line for line in log.splitlines()))
    git(clone, "push", "origin", "main:main")
    print("pushed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--public-clone", type=Path, required=True)
    ap.add_argument("--commit", action="store_true", help="copy and commit in the clone")
    ap.add_argument("--push", action="store_true", help="git push origin main (needs --yes)")
    ap.add_argument("--yes", action="store_true", help="confirm --push")
    ap.add_argument("--allow-no-smoke", action="store_true")
    ap.add_argument("--expect-remote", default=DEFAULT_REMOTE)
    args = ap.parse_args(argv)
    try:
        if args.push and not (args.commit and args.yes):
            raise PublishError("--push needs --commit and --yes")
        tree, report_path = locate(args.snapshot)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        clone = args.public_clone
        errors = check_report(report, tree, args.allow_no_smoke) + check_clone(
            clone, args.expect_remote
        )
        if errors:
            raise PublishError("refusing to publish:\n  " + "\n  ".join(errors))
        if args.commit:
            name, email = identity(clone)
        changes = plan(tree, clone)
        print(summary(changes))
        if not args.commit:
            print("dry run: the public clone was not changed (--commit to apply)")
            return 0
        if any(changes.values()):
            apply(tree, clone, changes)
            msg = commit_message(report, changes)
            git(clone, "commit", "-q", "-F", "-", stdin=msg.encode("utf-8"))
            print(f"committed as {name} <{email}>: {msg.splitlines()[0]}")
        else:
            print("no changes: nothing committed")
        if args.push:
            push(clone)
        elif any(changes.values()):
            print("not pushed: review with `git show --stat`, then push (or --push --yes)")
        return 0
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
