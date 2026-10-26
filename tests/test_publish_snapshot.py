"""scripts/publish_snapshot.py: a local bare repository plays the public repository (no network)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_snapshot as pub

NOREPLY = "12345+tester@users.noreply.github.com"
SNAP = {"README.md": "new readme\n", "src/a.py": "x = 1\n", "docs/new.md": "new doc\n"}
PUBLIC = {"README.md": "old readme\n", "src/a.py": "x = 1\n", "old.txt": "old\n"}


def git(cwd: Path, *args: str) -> str:
    ident = ["-c", "user.name=Tester", "-c", f"user.email={NOREPLY}", "-c", "commit.gpgsign=false"]
    run = subprocess.run(
        ["git", *ident, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[commit]\n\tgpgsign = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def make_snapshot(tmp: Path, files: dict[str, str] = SNAP, **override) -> Path:
    work = tmp / "snap"
    tree = work / "StiraCore"
    tree.mkdir(parents=True)
    write(tree, files)
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "Initial public release")
    report = {
        "snapshot": str(tree.resolve()),
        "files": len(files),
        "commit": git(tree, "rev-parse", "HEAD"),
        "source_commit": "abc1234def5678abc1234def5678abc1234def56",
        "source_date": "2026-10-24",
        "source_dirty": False,
        "denylist_words": 9,
        "scan": {"aws_access_key": {"count": 2, "files": ["tests/x.py"]}},
        "broken_links": [],
        "persona_words": {"count": 0, "files": []},
        "smoke": {"install": "ok", "pytest_rc": 0, "pytest_summary": "1 passed"},
        "file_set": {"extra": [], "missing": []},
    }
    report.update(override)
    (work / "release-report.json").write_text(json.dumps(report), encoding="utf-8")
    return work


def make_public(tmp: Path, files: dict[str, str] | None = PUBLIC, name: str = "StiraCore"):
    bare = tmp / "Resolver-TNG" / f"{name}.git"
    bare.mkdir(parents=True)
    git(bare, "init", "-q", "--bare", "-b", "main")
    clone = tmp / "clone"
    git(tmp, "clone", "-q", str(bare), str(clone))
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    git(clone, "config", "user.name", "Tester")
    git(clone, "config", "user.email", NOREPLY)
    if files:
        write(clone, files)
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "seed")
        git(clone, "push", "-q", "origin", "main")
    return bare, clone


def state(clone: Path) -> tuple[str, str, dict[str, str]]:
    head = git(clone, "rev-parse", "--verify", "-q", "HEAD") if (clone / ".git").exists() else ""
    files = {
        p.relative_to(clone).as_posix(): p.read_text(encoding="utf-8")
        for p in clone.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(clone).parts
    }
    return head, git(clone, "status", "--porcelain"), files


def run(work: Path, clone: Path, *extra: str) -> int:
    return pub.main(["--snapshot", str(work), "--public-clone", str(clone), *extra])


def test_dry_run_changes_nothing(tmp_path, capsys) -> None:
    work = make_snapshot(tmp_path)
    _, clone = make_public(tmp_path)
    before = state(clone)
    assert run(work, clone) == 0
    out = capsys.readouterr().out
    assert "added: 1, modified: 1, removed: 1" in out
    assert "dry run" in out
    assert state(clone) == before
    # the StiraCore/ directory itself is accepted too
    assert run(work / "StiraCore", clone) == 0


def test_commit_mirrors_the_snapshot_and_keeps_the_message_to_counts(tmp_path) -> None:
    work = make_snapshot(tmp_path)
    bare, clone = make_public(tmp_path)
    seed = git(bare, "rev-parse", "main")
    assert run(work, clone, "--commit") == 0
    assert git(clone, "log", "-1", "--format=%B") == (
        "Sync from development snapshot abc1234 (2026-10-24)\n\nAdded: 1, Modified: 1, Removed: 1."
    )
    assert git(clone, "log", "-1", "--format=%an <%ae>|%cn <%ce>") == (
        f"Tester <{NOREPLY}>|Tester <{NOREPLY}>"
    )
    _, status, files = state(clone)
    assert status == ""
    assert files == SNAP  # old.txt deleted, README.md replaced, docs/new.md added
    assert git(bare, "rev-parse", "main") == seed  # nothing pushed
    assert run(work, clone, "--commit") == 0  # second run: nothing to commit
    assert git(clone, "rev-list", "--count", "HEAD") == "2"


def test_commit_and_push_into_an_empty_public_repository(tmp_path, capsys) -> None:
    work = make_snapshot(tmp_path)
    bare, clone = make_public(tmp_path, files=None)
    assert run(work, clone, "--commit", "--push", "--yes") == 0
    assert "pushing main" in capsys.readouterr().out
    assert git(bare, "rev-parse", "main") == git(clone, "rev-parse", "HEAD")
    assert git(bare, "ls-tree", "-r", "--name-only", "main").splitlines() == sorted(SNAP)


@pytest.mark.parametrize(
    "flags", [["--push"], ["--push", "--yes"], ["--commit", "--push"], ["--yes", "--push"]]
)
def test_push_needs_commit_and_yes(tmp_path, capsys, flags) -> None:
    work = make_snapshot(tmp_path)
    bare, clone = make_public(tmp_path)
    before, seed = state(clone), git(bare, "rev-parse", "main")
    assert run(work, clone, *flags) == 1
    assert "--push needs --commit and --yes" in capsys.readouterr().err
    assert state(clone) == before and git(bare, "rev-parse", "main") == seed


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"broken_links": [{"file": "a.md", "line": 1, "target": "b.md"}]}, "broken_links"),
        ({"persona_words": {"count": 1, "files": ["a.md"]}}, "persona_words"),
        ({"smoke": {"install": "ok", "pytest_rc": 1}}, "smoke did not pass"),
        ({"smoke": {"install": "failed", "tail": ""}}, "smoke did not pass"),
        ({"denylist_words": 0}, "denylist_words"),
        ({"denylist_words": None}, "denylist_words"),
        ({"scan": {"denylist_word_2": {"count": 1, "files": ["a.md"]}}}, "denylist_word_2"),
        ({"scan": {"tracker_link": {"count": 3, "files": ["a.md"]}}}, "tracker_link"),
        ({"file_set": {"extra": ["src/x.egg-info/PKG-INFO"], "missing": []}}, "files differ"),
        ({"file_set": None}, "no file_set"),
        ({"source_dirty": True}, "uncommitted"),
        ({"source_commit": None}, "source_commit"),
        ({"commit": "0" * 40}, "HEAD is not the commit"),
        ({"snapshot": "/elsewhere/StiraCore"}, "another snapshot"),
    ],
)
def test_report_checks_refuse(tmp_path, capsys, override, reason) -> None:
    work = make_snapshot(tmp_path, **override)
    _, clone = make_public(tmp_path)
    before = state(clone)
    assert run(work, clone, "--commit") == 1
    err = capsys.readouterr().err
    assert "refusing to publish" in err and reason in err
    assert state(clone) == before


def test_no_smoke_report_needs_the_flag(tmp_path, capsys) -> None:
    work = make_snapshot(tmp_path)
    report_path = work / "release-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    del report["smoke"]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    _, clone = make_public(tmp_path)
    assert run(work, clone) == 1
    assert "--allow-no-smoke" in capsys.readouterr().err
    assert run(work, clone, "--allow-no-smoke") == 0


def test_stray_file_in_the_snapshot_refuses(tmp_path, capsys) -> None:
    work = make_snapshot(tmp_path)
    write(work / "StiraCore", {"src/pkg/__pycache__/a.pyc": "x"})
    _, clone = make_public(tmp_path)
    assert run(work, clone) == 1
    assert "untracked, ignored or modified" in capsys.readouterr().err


def test_clone_checks(tmp_path, capsys) -> None:
    work = make_snapshot(tmp_path)
    _, clone = make_public(tmp_path, name="SomethingElse")
    assert run(work, clone) == 1
    assert "origin is not Resolver-TNG/StiraCore" in capsys.readouterr().err
    assert run(work, clone, "--expect-remote", "Resolver-TNG/SomethingElse") == 0
    write(clone, {"untracked.txt": "x"})
    assert run(work, clone, "--expect-remote", "Resolver-TNG/SomethingElse") == 1
    assert "not clean" in capsys.readouterr().err
    (clone / "untracked.txt").unlink()
    git(clone, "checkout", "-q", "-b", "topic")
    assert run(work, clone, "--expect-remote", "Resolver-TNG/SomethingElse") == 1
    assert "not main" in capsys.readouterr().err


def test_remote_matching() -> None:
    expect = "Resolver-TNG/StiraCore"
    for url in (
        "https://github.com/Resolver-TNG/StiraCore.git",
        "https://github.com/resolver-tng/stiracore/",
        "git@github.com:Resolver-TNG/StiraCore.git",
    ):
        assert pub.remote_matches(url, expect), url
    for url in (
        "https://github.com/Resolver-TNG/StiraCore-dev.git",
        "https://github.com/Resolver-TNG/Stira-Strands.git",
        "https://github.com/someone/StiraCore.git",
    ):
        assert not pub.remote_matches(url, expect), url


@pytest.mark.parametrize("email", [None, "tester@example.com"])
def test_commit_needs_a_noreply_identity_from_the_clone(tmp_path, capsys, email) -> None:
    work = make_snapshot(tmp_path)
    _, clone = make_public(tmp_path)
    if email is None:
        git(clone, "config", "--unset", "user.email")
    else:
        git(clone, "config", "user.email", email)
    before = state(clone)
    assert run(work, clone, "--commit") == 1
    assert "user.email" in capsys.readouterr().err
    assert state(clone) == before
