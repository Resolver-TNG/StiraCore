"""Release tools: exclude list matching and relative link checking (#107)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_links
import release_paths


def test_exclude_patterns() -> None:
    pats = ["docs/research/", "AGENTS.md", "scripts/fix_*.py", "*.local", "node_modules/"]
    assert release_paths.is_excluded("docs/research/a/b.png", pats)
    assert release_paths.is_excluded("AGENTS.md", pats)
    assert release_paths.is_excluded("sub/AGENTS.md", pats)
    assert release_paths.is_excluded("scripts/fix_batch_a.py", pats)
    assert release_paths.is_excluded("x/y.local", pats)
    assert release_paths.is_excluded("dashboard/node_modules/z.js", pats)
    assert not release_paths.is_excluded("docs/research.md", pats)
    assert not release_paths.is_excluded("scripts/check_links.py", pats)


def test_repo_exclude_file_covers_audit_items() -> None:
    pats = release_paths.load_patterns()
    for path in (
        "docs/research/x.md",
        "reviews/r.md",
        "337c_probe.sh",
        "AGENTS.md",
        "memory/2026-10-21.md",
        ".local/stira/x.json",
    ):
        assert release_paths.is_excluded(path, pats), path
    assert not release_paths.is_excluded("examples/personas/resolver/SOUL.md", pats)
    assert not release_paths.is_excluded("LICENSE", pats)


def _check(files: dict[str, str]) -> list[dict]:
    return check_links.broken_links(set(files), files.__getitem__)


def test_broken_and_ok_links() -> None:
    files = {
        "README.md": "[a](docs/x.md) [b](docs/missing.md) [c](https://e.x) [d](#top)\n"
        "![img](docs/i.png) [dir](docs/) [anchor](docs/x.md#s) `[e](nope.md)`\n",
        "docs/x.md": "[up](../README.md) [out](../../escape.md)\n```\n[f](gone.md)\n```\n",
        "docs/i.png": "",
    }
    bad = [(b["file"], b["target"]) for b in _check(files)]
    assert bad == [("README.md", "docs/missing.md"), ("docs/x.md", "../../escape.md")]


def test_public_set_has_no_broken_links_after_strip() -> None:
    import make_release_snapshot as snap

    pats = release_paths.load_patterns()
    files = set(release_paths.release_files(ROOT, pats))
    texts = {}

    def read(f: str) -> str:
        if f not in texts:
            raw = (ROOT / f).read_text(encoding="utf-8", errors="replace")
            texts[f] = snap.strip_excluded_links(f, raw, pats)
        return texts[f]

    assert check_links.broken_links(files, read) == []


def test_agent_setup_uses_the_smoke_commands() -> None:
    import make_release_snapshot as snap

    install = 'python -m pip install -e ".[dev]"'
    tests = "pytest -q " + " ".join(snap.SMOKE_TESTS)
    assert snap.SMOKE_INSTALL[-1] == ".[dev]"
    for name in ("agent-setup.md", "agent-setup_ja.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert install in text and tests in text, name
        assert "| sh" in text  # named only as a forbidden form
        assert "adr/0022-trust-labels-and-taint.md" in text


def test_scan_reports_counts_not_values(tmp_path) -> None:
    import release_scan

    (tmp_path / "a.txt").write_text("key AKIA" + "ABCDEFGHIJKLMNOP and /Users/YOU ok\n")
    res = release_scan.scan(tmp_path, ["a.txt"], ["secretname"])
    assert res == {"aws_access_key": {"count": 1, "files": ["a.txt"]}}
    assert "AKIA" not in repr(res)


def test_persona_words_fail_the_snapshot(tmp_path) -> None:
    """The previous persona's game-derived names: any of them in a text or a path fails."""
    import make_release_snapshot as snap
    import release_scan

    words = release_scan.PERSONA_WORDS
    assert len(words) == 6
    files = []
    for i, word in enumerate(words):
        (tmp_path / f"t{i}.md").write_text(f"x {word.lower()} y {word}\n", encoding="utf-8")
        files.append(f"t{i}.md")
    (tmp_path / "clean.md").write_text("StiraCore, Resolver, Strands Agents SDK\n", "utf-8")
    named = f"img/{words[0].lower()}-shot.png"
    (tmp_path / "img").mkdir()
    (tmp_path / named).write_bytes(b"\x89PNG")
    res = release_scan.persona_word_scan(tmp_path, [*files, "clean.md", named])
    assert res["files"] == sorted([*files, named])
    assert res["count"] >= 2 * len(words) + 1
    assert all(w.lower() not in repr(res).lower() for w in words[1:])
    clean = {"broken_links": [], "persona_words": {"count": 0, "files": []}}
    assert snap.report_ok(clean)
    assert not snap.report_ok({**clean, "persona_words": res})


def test_repository_release_set_has_no_persona_words() -> None:
    import release_scan

    files = release_paths.release_files(ROOT, release_paths.load_patterns())
    assert release_scan.persona_word_scan(ROOT, files) == {"count": 0, "files": []}


# Built at run time so that this file never contains such a link itself.
_GH = "https://github.com/" + "Resolver-TNG/"
_ISSUES = "/iss" + "ues/"


def test_tracker_links_are_blocking_scan_hits(tmp_path) -> None:
    """Issue / PR links of the owner's repositories must not reach the public snapshot."""
    import make_release_snapshot as snap
    import release_scan

    bad = f"see [#12]({_GH}StiraCore{_ISSUES}12), {_GH}Stira-Strands/pull/3, {_GH}X{_ISSUES}9\n"
    ok = (
        f"git clone {_GH}StiraCore.git, [doc]({_GH}StiraCore/blob/main/docs/x.md), "
        f"{_GH}StiraCore/security/advisories/new, #12, https://github.com/other/r/issues/4\n"
    )
    (tmp_path / "bad.md").write_text(bad, encoding="utf-8")
    (tmp_path / "ok.md").write_text(ok, encoding="utf-8")
    res = release_scan.scan(tmp_path, ["bad.md", "ok.md"], ["unusedword"])
    assert res == {release_scan.TRACKER_LINK: {"count": 3, "files": ["bad.md"]}}
    assert release_scan.blocking_kinds(res) == [release_scan.TRACKER_LINK]
    deny = {"denylist_word_1": {"count": 1, "files": ["a"]}, "email": {"count": 2, "files": []}}
    assert release_scan.blocking_kinds(deny) == ["denylist_word_1"]
    clean = {"broken_links": [], "persona_words": {"count": 0, "files": []}}
    assert snap.report_ok({**clean, "scan": {"email": {"count": 1, "files": ["t.py"]}}})
    assert not snap.report_ok({**clean, "scan": res})
    assert not snap.report_ok({**clean, "scan": deny})
    assert not snap.report_ok({**clean, "smoke": {"install": "failed", "tail": ""}})


def test_repository_has_no_tracker_links() -> None:
    """Issue numbers stay plain `#N` everywhere (they are private development tracker IDs)."""
    import release_scan

    files = release_paths.tracked_files(ROOT)
    res = release_scan.scan(ROOT, files, None)
    assert release_scan.TRACKER_LINK not in res, res.get(release_scan.TRACKER_LINK)


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def dev_repo(tmp_path, monkeypatch):
    """A tiny development repository standing in for ROOT (no network, no denylist file)."""
    import make_release_snapshot as snap
    import release_scan

    cfg = tmp_path / "gitconfig"
    cfg.write_text("[commit]\n\tgpgsign = false\n", encoding="utf-8")
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "1+tester@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "1+tester@users.noreply.github.com",
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "dev"
    for rel, text in {
        "README.md": "[doc](docs/a.md)\n",
        "docs/a.md": "a\n",
        "src/pkg/__init__.py": "",
        ".gitignore": "__pycache__/\n*.egg-info/\n.ruff_cache/\nnode_modules/\n",
        "internal/notes.md": "internal\n",
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dev")
    (repo / "src/pkg/__pycache__").mkdir()
    (repo / "src/pkg/__pycache__/x.pyc").write_bytes(b"untracked in dev")
    monkeypatch.setattr(snap, "ROOT", repo)
    monkeypatch.setattr(snap, "load_patterns", lambda: ["internal/"])
    monkeypatch.setattr(release_scan, "load_denylist", lambda: ["zz-unused-word"])
    return repo


def _fake_smoke(out: Path) -> dict:
    """What a real smoke leaves behind: build metadata, caches and an edited tracked file."""
    for rel in (
        "src/stiracore.egg-info/PKG-INFO",
        "src/pkg/__pycache__/__init__.cpython-311.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/0.8/x",
        "node_modules/axe-core/index.js",
        "stray.txt",
    ):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text("generated", encoding="utf-8")
    (out / "docs/a.md").write_text("changed by a test\n", encoding="utf-8")
    return {"install": "ok", "pytest_rc": 0, "pytest_summary": "1 passed"}


def test_snapshot_removes_what_the_smoke_generated(dev_repo, tmp_path, monkeypatch) -> None:
    import make_release_snapshot as snap

    monkeypatch.setattr(snap, "smoke", _fake_smoke)
    work = tmp_path / "out"
    assert snap.main(["--out", str(work)]) == 0
    tree = work / "StiraCore"
    files = sorted(
        p.relative_to(tree).as_posix()
        for p in tree.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(tree).parts
    )
    assert files == [".gitignore", "README.md", "docs/a.md", "src/pkg/__init__.py"]
    assert (tree / "docs/a.md").read_text(encoding="utf-8") == "a\n"
    assert _git(tree, "status", "--porcelain", "--ignored") == ""
    report = json.loads((work / "release-report.json").read_text(encoding="utf-8"))
    assert report["file_set"] == {"extra": [], "missing": []}
    assert report["source_commit"] == _git(dev_repo, "rev-parse", "HEAD")
    assert report["source_dirty"] is False and report["denylist_words"] == 1
    assert report["snapshot"] == str(tree.resolve())


def test_snapshot_without_smoke_and_the_file_set_check(dev_repo, tmp_path, monkeypatch) -> None:
    import make_release_snapshot as snap

    assert snap.main(["--out", str(tmp_path / "a"), "--no-smoke"]) == 0
    report = json.loads((tmp_path / "a" / "release-report.json").read_text(encoding="utf-8"))
    assert "smoke" not in report and report["file_set"] == {"extra": [], "missing": []}
    # Without the cleanup the generated files are found and the run fails.
    monkeypatch.setattr(snap, "smoke", _fake_smoke)
    monkeypatch.setattr(snap, "clean_generated", lambda out: None)
    assert snap.main(["--out", str(tmp_path / "b")]) == 1
    report = json.loads((tmp_path / "b" / "release-report.json").read_text(encoding="utf-8"))
    assert "src/stiracore.egg-info/PKG-INFO" in report["file_set"]["extra"]
    assert "stray.txt" in report["file_set"]["extra"]
    assert report["file_set"]["missing"] == []
