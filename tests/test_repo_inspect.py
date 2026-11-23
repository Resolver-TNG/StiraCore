"""Read-only Git repository inspection (#671 stage 1). No network: remotes are file://
bare repositories set up by the test itself; the module never fetches."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from stiracore.diagnostics import repo_inspect
from stiracore.diagnostics.repo_inspect import (
    GIT_COMMANDS,
    HARDENING_ARGS,
    GitOutput,
    RepositoryInspector,
    default_runner,
    discover_repositories,
    git_environment,
)
from stiracore.identity_paths import WritePathPolicy

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

POSIX = os.name != "nt"


def git(path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=env,
    )
    return result.stdout


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")


def make_repo(path: Path, subject: str = "initial") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Stage Test")
    git(path, "config", "user.email", "stage@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    write(path / "tracked.txt", "one\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", subject)
    return path.resolve()


def policy_for(*roots: Path) -> WritePathPolicy:
    return WritePathPolicy(base_dir=roots[0], read_roots=[str(r) for r in roots])


def inspect(repo: Path, root: Path | None = None, **kwargs) -> dict:
    inspector = RepositoryInspector(policy_for(root or repo.parent), **kwargs)
    return inspector.inspect(repo, use_cache=False)


def clone_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare")
    source = make_repo(tmp_path / "source")
    git(source, "remote", "add", "origin", remote.as_uri())
    git(source, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", remote.as_uri(), str(clone))
    for n in (2, 3):
        write(source / "tracked.txt", f"{n}\n")
        git(source, "add", "tracked.txt")
        git(source, "commit", "-q", "-m", f"commit {n}")
    git(source, "push", "-q", "origin", "main")
    git(clone, "fetch", "-q", "origin")  # file:// in the test only; the module never fetches
    return source, clone.resolve()


# --- what it reports ------------------------------------------------------------------


def test_clean_repository_is_green(tmp_path: Path):
    data = inspect(make_repo(tmp_path / "clean"))
    assert data["status"] == "green"
    assert data["issues"] == []
    assert data["branch"] == data["defaultBranch"] == "main"
    assert data["changes"]["total"] == 0
    assert data["worktrees"] == {"total": 1, "prunable": 0, "locked": 0, "detached": 0}
    assert set(data["lastCommit"]) == {"hash", "date"}


def test_staged_unstaged_and_untracked_are_counted(tmp_path: Path):
    repo = make_repo(tmp_path / "changes")
    write(repo / "staged.txt", "staged\n")
    git(repo, "add", "staged.txt")
    write(repo / "tracked.txt", "changed\n")
    write(repo / "untracked.txt", "new\n")
    data = inspect(repo)
    assert data["changes"] == {
        "staged": 1,
        "unstaged": 1,
        "untracked": 1,
        "conflicted": 0,
        "total": 3,
    }
    assert "uncommitted_changes" in data["issues"]
    assert data["status"] == "yellow"


def test_behind_upstream_is_red(tmp_path: Path):
    _source, clone = clone_with_remote(tmp_path)
    data = inspect(clone, tmp_path)
    assert (data["ahead"], data["behind"]) == (0, 2)
    assert data["fetchHeadUpdatedAt"] is not None
    assert "behind_upstream" in data["issues"]
    assert data["status"] == "red"


def test_unpushed_commits_are_reported(tmp_path: Path):
    _source, clone = clone_with_remote(tmp_path)
    git(clone, "config", "user.name", "T")
    git(clone, "config", "user.email", "t@example.invalid")
    git(clone, "config", "commit.gpgsign", "false")
    git(clone, "reset", "-q", "--hard", "origin/main")
    write(clone / "tracked.txt", "local\n")
    git(clone, "commit", "-q", "-am", "local work")
    data = inspect(clone, tmp_path)
    assert (data["ahead"], data["behind"]) == (1, 0)
    assert data["issues"] == ["unpushed_commits"]
    assert data["status"] == "yellow"


def test_default_branch_without_upstream_is_compared_to_origin(tmp_path: Path):
    _source, clone = clone_with_remote(tmp_path)
    git(clone, "branch", "--unset-upstream", "main")
    data = inspect(clone, tmp_path)
    assert data["upstream"] is None
    assert data["compareBase"] == "origin/main"
    assert data["defaultBranchSource"] == "origin/HEAD"
    if data["branches"] is None:
        pytest.skip("git older than 2.41 (no ahead-behind atom)")
    assert {"name": "main", "ahead": 0, "behind": 2} in data["branches"]
    assert data["defaultBranchBehind"] == 2
    assert "default_branch_behind" in data["issues"]
    assert data["status"] == "red"


def test_deleted_worktree_is_prunable_and_locked_detached_are_counted(tmp_path: Path):
    repo = make_repo(tmp_path / "primary")
    gone = tmp_path / "gone-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(gone))
    shutil.rmtree(gone)
    locked = tmp_path / "locked-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(locked))
    git(repo, "worktree", "lock", str(locked))
    data = inspect(repo, tmp_path)
    assert data["worktrees"] == {"total": 3, "prunable": 1, "locked": 1, "detached": 2}
    assert {"prunable_worktree", "locked_worktree", "detached_worktree"} <= set(data["issues"])
    assert data["status"] == "red"
    main = [w for w in data["worktreeList"] if w["main"]]
    assert len(main) == 1 and main[0]["branch"] == "main"


def test_worktree_parser_allow_lists_fields(tmp_path: Path):
    repo = (tmp_path / "primary").resolve()
    text = "\0".join(
        [
            f"worktree {repo}",
            "HEAD 1234567890abcdef",
            "branch refs/heads/main",
            "",
            f"worktree {tmp_path / 'other'}",
            "HEAD abcdef0123456789",
            "detached",
            "locked reason text that is not shown",
            "",
        ]
    )
    items = repo_inspect._parse_worktrees(text, repo)
    assert items[0] == {
        "path": str(repo),
        "branch": "main",
        "head": "1234567",
        "detached": False,
        "prunable": False,
        "locked": False,
        "main": True,
    }
    assert items[1]["locked"] is True and items[1]["detached"] is True
    assert "reason" not in str(items)


def test_stash_is_counted(tmp_path: Path):
    repo = make_repo(tmp_path / "stash")
    write(repo / "tracked.txt", "stashed\n")
    git(repo, "stash", "push", "-q", "-m", "desk-test")
    data = inspect(repo)
    assert data["stashCount"] == 1
    assert data["status"] == "yellow"


def test_commit_subject_is_not_in_the_output(tmp_path: Path):
    secret = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    repo = make_repo(tmp_path / "subject", f"do not show {secret}")
    data = inspect(repo)
    assert secret not in repr(data)
    assert "do not show" not in repr(data)


# --- secrets in names and URLs ----------------------------------------------------------


def test_remote_credentials_are_removed_and_github_is_recognized(tmp_path: Path):
    repo = make_repo(tmp_path / "credential-remote")
    git(repo, "remote", "add", "origin", "https://u:t0kenvalue@github.com/Owner/Repo.git")
    data = inspect(repo)
    assert "t0kenvalue" not in repr(data)
    assert data["originUrl"] == "https://github.com/Owner/Repo.git"
    assert data["github"] == "Owner/Repo"


def test_secret_shaped_branch_name_is_masked(tmp_path: Path):
    repo = make_repo(tmp_path / "branchy")
    secret = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    git(repo, "switch", "-q", "-c", secret)
    data = inspect(repo)
    assert secret not in repr(data)
    assert data["branch"] == repo_inspect.MASKED


def test_clean_text_cuts_and_drops_control_characters():
    assert repo_inspect.clean_text("a\nb\x1b[31m<c>", 100) == "a b [31m\u2039c\u203a"
    assert repo_inspect.clean_text("x" * 50, 10) == "x" * 10 + "\u2026"
    assert repo_inspect.sanitize_remote_url("ssh://user:pw@host:22/r.git?token=abc") == (
        "ssh://host:22/r.git"
    )


# --- paths pass the read policy -------------------------------------------------------


def test_discovery_explicit_scan_roots_depth_and_problems(tmp_path: Path):
    explicit = make_repo(tmp_path / "explicit")
    scan = tmp_path / "scan"
    scanned = make_repo(scan / "scanned")
    deep = make_repo(scan / "group" / "deep")
    found, problems = discover_repositories(
        policy_for(tmp_path),
        paths=[str(explicit), str(explicit), str(tmp_path / "missing"), "relative/path"],
        scan_roots=[str(scan)],
    )
    assert [repo for repo, _ in found] == [explicit, scanned]
    assert dict(found)[scanned] == scan.resolve()
    assert problems == [
        {"field": "paths", "index": 2, "code": "missing"},
        {"field": "paths", "index": 3, "code": "not_absolute"},
    ]
    found, _ = discover_repositories(policy_for(tmp_path), scan_roots=[str(scan)], scan_depth=2)
    assert {repo for repo, _ in found} == {scanned, deep}
    with pytest.raises(ValueError):
        discover_repositories(policy_for(tmp_path), scan_depth=repo_inspect.MAX_SCAN_DEPTH + 1)


def test_paths_outside_read_roots_are_refused(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = make_repo(tmp_path / "outside")
    found, problems = discover_repositories(
        policy_for(allowed), paths=[str(outside)], scan_roots=[str(tmp_path)]
    )
    assert found == []
    assert problems == [
        {"field": "paths", "index": 0, "code": "refused"},
        {"field": "scanRoots", "index": 0, "code": "refused"},
    ]


def test_network_paths_are_refused(tmp_path: Path):
    _found, problems = discover_repositories(
        policy_for(tmp_path), paths=["\\\\server\\share\\repo", "//server/share/repo"]
    )
    assert [p["code"] for p in problems] == ["network_path", "network_path"]


@pytest.mark.skipif(not POSIX, reason="symlink creation needs privileges on Windows")
def test_symlinks_are_not_followed_out_of_the_read_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = make_repo(tmp_path / "outside" / "target")
    (allowed / "link-to-outside").symlink_to(target, target_is_directory=True)
    inside = make_repo(allowed / "inside")
    (allowed / "link-to-inside").symlink_to(inside, target_is_directory=True)
    policy = policy_for(allowed)
    found, problems = discover_repositories(
        policy,
        paths=[str(allowed / "link-to-outside"), str(allowed / "link-to-inside")],
        scan_roots=[str(allowed)],
    )
    assert [repo for repo, _ in found] == [inside]
    assert [p["code"] for p in problems] == ["refused", "link"]


@pytest.mark.skipif(not POSIX, reason="symlink creation needs privileges on Windows")
def test_git_dir_pointing_outside_the_read_roots_is_refused(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    real = make_repo(tmp_path / "outside" / "real")
    fake = allowed / "fake"
    fake.mkdir()
    write(fake / ".git", f"gitdir: {real / '.git'}\n")
    calls: list[list[str]] = []

    def exec_git(argv, **kwargs):
        calls.append(list(argv))
        return GitOutput(0, "", "")

    data = RepositoryInspector(policy_for(allowed), exec_git=exec_git).inspect(fake.resolve())
    assert data["status"] == "red" and data["issues"] == ["inspection_failed"]
    assert calls == []  # refused before git ran


def test_inspect_refuses_a_repository_outside_the_read_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = make_repo(tmp_path / "outside")
    data = RepositoryInspector(policy_for(allowed)).inspect(outside, use_cache=False)
    assert data["issues"] == ["inspection_failed"]
    assert "read policy" in data["error"]


# --- the command line, the environment and malicious configuration ----------------------


def test_only_the_fixed_commands_run_with_hardening(tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    seen: list[list[str]] = []
    envs: list[dict] = []

    def exec_git(argv, *, env, timeout, limit):
        seen.append(list(argv))
        envs.append(dict(env))
        return default_runner(argv, env=env, timeout=timeout, limit=limit)

    environ = {**os.environ, "GIT_DIR": "/elsewhere", "GIT_EXTERNAL_DIFF": "evil"}
    inspect(repo, exec_git=exec_git, environ=environ)
    allowed = [tuple(v) for v in GIT_COMMANDS.values()]
    for argv in seen:
        assert argv[: 1 + len(HARDENING_ARGS)] == ["git", *HARDENING_ARGS]
        at = argv.index("-C")
        assert argv[at + 1 : at + 4] == [
            str(repo),
            f"--git-dir={repo / '.git'}",
            f"--work-tree={repo}",
        ]
        tail = argv[at + 4 :]
        assert any(
            len(tail) == len(cmd)
            and all(t == c or "{" in c for t, c in zip(tail, cmd, strict=True))
            for cmd in allowed
        ), tail
        assert "fetch" not in argv and "pull" not in argv
    for env in envs:
        assert "GIT_DIR" not in env and "GIT_EXTERNAL_DIFF" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_environment_drops_inherited_git_variables(tmp_path: Path):
    env = git_environment(tmp_path / "r", {"PATH": "/bin", "GIT_CONFIG_PARAMETERS": "'x=1'"})
    assert env["PATH"] == "/bin"
    assert not {"GIT_CONFIG_PARAMETERS"} & set(env)
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path)


def _marker_script(path: Path, marker: Path, tail: str = "cat") -> Path:
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n{tail}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def unhardened(monkeypatch):
    """The control: no ``HARDENING_ARGS``, no filter overrides, no lock/lazy-fetch env.
    A test that sees its marker under this fixture really uses a dangerous setting."""
    real_env = repo_inspect.git_environment

    def env(repo, base=None):
        out = real_env(repo, base)
        out.pop("GIT_OPTIONAL_LOCKS", None)
        out.pop("GIT_NO_LAZY_FETCH", None)
        return out

    monkeypatch.setattr(repo_inspect, "HARDENING_ARGS", ())
    monkeypatch.setattr(repo_inspect, "git_environment", env)
    monkeypatch.setattr(RepositoryInspector, "_filter_overrides", staticmethod(lambda d: []))


def _fsmonitor_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = make_repo(tmp_path / "evil")
    marker = tmp_path / "PWNED"
    script = _marker_script(tmp_path / "evil.sh", marker, "exit 1")
    git(repo, "config", "core.fsmonitor", str(script))
    write(repo / "tracked.txt", "changed\n")
    return repo, marker


def _hook_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = make_repo(tmp_path / "hooked")
    marker = tmp_path / "PWNED"
    script = _marker_script(tmp_path / "evil.sh", marker, "exit 0")
    shutil.copy(script, repo / ".git" / "hooks" / "post-index-change")
    later = (repo / "tracked.txt").stat().st_mtime + 100
    os.utime(repo / "tracked.txt", (later, later))  # same content, new stat: index refresh
    return repo, marker


def _lazy_fetch_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A partial clone whose HEAD tree is missing: reading it asks the promisor remote,
    which runs ``core.sshCommand``."""
    repo = make_repo(tmp_path / "partial")
    marker = tmp_path / "PWNED"
    script = _marker_script(tmp_path / "evil.sh", marker, "exit 1")
    git(repo, "remote", "add", "origin", "ssh://example.invalid/r.git")
    git(repo, "config", "core.sshCommand", str(script))
    git(repo, "config", "core.repositoryformatversion", "1")
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    return repo, marker


def _filter_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = make_repo(tmp_path / "filtered")
    marker = tmp_path / "PWNED"
    script = _marker_script(tmp_path / "evil.sh", marker)
    write(repo / ".gitattributes", "*.txt filter=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attributes")
    git(repo, "config", "filter.evil.clean", str(script))
    git(repo, "config", "filter.evil.required", "true")
    write(repo / "tracked.txt", "changed\n")
    git(repo, "add", "tracked.txt")  # the index now holds the file with a new stat
    write(repo / "tracked.txt", "CHANGED\n")  # same size, new stat: status must hash it
    later = (repo / "tracked.txt").stat().st_mtime + 100
    os.utime(repo / "tracked.txt", (later, later))
    marker.unlink(missing_ok=True)  # ``git add`` above ran the filter; only ours counts
    return repo, marker


ATTACKS = {
    "fsmonitor": _fsmonitor_repo,
    "hook": _hook_repo,
    "ssh_lazy_fetch": _lazy_fetch_repo,
    "filter": _filter_repo,
}


@pytest.mark.skipif(not POSIX, reason="the marker scripts are POSIX shell")
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_malicious_configuration_does_not_run(tmp_path: Path, attack: str):
    repo, marker = ATTACKS[attack](tmp_path)
    data = inspect(repo)  # the lazy-fetch repository fails (missing tree); nothing may run
    assert not marker.exists()
    if attack != "ssh_lazy_fetch":
        assert "error" not in data, data


@pytest.mark.skipif(not POSIX, reason="the marker scripts are POSIX shell")
@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_control_without_hardening_the_marker_is_created(tmp_path: Path, attack: str, unhardened):
    repo, marker = ATTACKS[attack](tmp_path)
    inspect(repo)
    assert marker.exists()


@pytest.mark.skipif(not POSIX, reason="the marker scripts are POSIX shell")
def test_filter_driver_added_during_status_is_detected(tmp_path: Path):
    repo = make_repo(tmp_path / "race")
    script = _marker_script(tmp_path / "evil.sh", tmp_path / "PWNED")

    def exec_git(argv, **kwargs):
        out = default_runner(argv, **kwargs)
        if "status" in argv:
            git(repo, "config", "filter.late.clean", str(script))
        return out

    data = inspect(repo, exec_git=exec_git)
    assert data["issues"] == ["inspection_failed"]
    assert data["error"] == "filter drivers changed during inspection"
    assert set(data) == {"id", "name", "path", "status", "issues", "error"}


def test_core_worktree_cannot_move_the_work_tree_outside(tmp_path: Path):
    allowed = tmp_path / "allowed"
    repo = make_repo(allowed / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    for n in range(3):
        write(outside / f"secret-{n}.txt", "x\n")
    git(repo, "config", "core.worktree", str(outside))
    assert "secret-0.txt" in git(repo, "status", "--porcelain=v2")  # the attack is real
    data = inspect(repo, allowed)
    assert "error" not in data, data
    assert data["changes"]["total"] == 0
    assert "secret" not in repr(data)


@pytest.mark.skipif(not POSIX, reason="symlink creation needs privileges on Windows")
def test_commondir_symlink_refuses_the_repository(tmp_path: Path):
    allowed = tmp_path / "allowed"
    repo = make_repo(allowed / "repo")
    other = make_repo(tmp_path / "outside")
    pointer = tmp_path / "commondir-target"
    write(pointer, f"{other / '.git'}\n")
    (repo / ".git" / "commondir").symlink_to(pointer)
    calls: list[list[str]] = []

    def exec_git(argv, **kwargs):
        calls.append(list(argv))
        return GitOutput(0, "", "")

    data = RepositoryInspector(policy_for(allowed), exec_git=exec_git).inspect(repo)
    assert data["issues"] == ["inspection_failed"]
    assert "commondir" in data["error"]
    assert calls == []


def test_git_view_other_than_the_checked_directories_is_refused(tmp_path: Path):
    allowed = tmp_path / "allowed"
    repo = make_repo(allowed / "repo")
    outside = (tmp_path / "outside").resolve()

    def exec_git(argv, **kwargs):
        if "rev-parse" in argv:
            return GitOutput(0, f"{outside}/.git\n{outside}/.git\n{outside}\n", "")
        return default_runner(argv, **kwargs)

    data = inspect(repo, allowed, exec_git=exec_git)
    assert data["issues"] == ["inspection_failed"]
    assert data["error"] == "git resolved a directory other than the checked one"


def test_status_settings_cannot_fake_a_green_report(tmp_path: Path):
    _source, clone = clone_with_remote(tmp_path)
    git(clone, "config", "status.showUntrackedFiles", "no")
    git(clone, "config", "status.aheadBehind", "false")
    git(clone, "config", "color.ui", "always")
    write(clone / "untracked.txt", "new\n")
    data = inspect(clone, tmp_path)
    assert data["behind"] == 2
    assert data["changes"]["untracked"] == 1
    assert data["status"] == "red"


def test_scp_style_and_ipv6_remote_urls():
    clean = repo_inspect.sanitize_remote_url
    assert clean("user:pass@host:path/r.git") == "host:path/r.git"
    assert clean("oauth2:t0kenvalue@gitlab.com:g/r.git") == "gitlab.com:g/r.git"
    assert clean("git@github.com:Owner/Repo.git") == "github.com:Owner/Repo.git"
    assert repo_inspect.github_repository(clean("git@github.com:Owner/Repo.git")) == "Owner/Repo"
    assert clean("ssh://u:pw@[::1]:2222/r.git") == "ssh://[::1]:2222/r.git"
    assert clean("/srv/git/r.git") == "/srv/git/r.git"


def test_unusable_filter_driver_name_refuses_the_repository(tmp_path: Path):
    repo = make_repo(tmp_path / "odd")
    git(repo, "config", "filter.a=b.clean", "x")
    data = inspect(repo)
    assert data["issues"] == ["inspection_failed"]
    assert "filter driver" in data["error"]


def test_huge_output_is_cut_and_reported(tmp_path: Path):
    repo = make_repo(tmp_path / "big")
    for n in range(200):
        write(repo / f"untracked-{n:03}.txt", "x\n")
    data = inspect(repo, max_output=512)
    assert data["status"] == "red"
    assert "exceeded 512 bytes" in data["error"]


def test_default_runner_stops_at_the_limit(tmp_path: Path):
    repo = make_repo(tmp_path / "limit")
    for n in range(100):
        write(repo / f"u{n:03}.txt", "x\n")
    argv = ["git", *HARDENING_ARGS, "-C", str(repo), *GIT_COMMANDS["status"]]
    out = default_runner(argv, env=git_environment(repo), timeout=20, limit=64)
    assert out.truncated and len(out.stdout.encode()) <= 64


def test_missing_git_and_timeout_become_data(tmp_path: Path):
    repo = make_repo(tmp_path / "r")

    def missing(argv, **kwargs):
        raise FileNotFoundError("git")

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert inspect(repo, exec_git=missing)["error"] == "git executable not found"
    assert "timed out" in inspect(repo, exec_git=slow)["error"]


def test_old_git_without_ahead_behind_returns_null_branches(tmp_path: Path):
    repo = make_repo(tmp_path / "old-git")

    def exec_git(argv, **kwargs):
        if "for-each-ref" in argv:
            return GitOutput(129, "", "unknown field name: ahead-behind")
        return default_runner(argv, **kwargs)

    data = inspect(repo, exec_git=exec_git)
    assert data["branches"] is None and "error" not in data


# --- cache ------------------------------------------------------------------------------


def test_cache_lasts_five_minutes_on_the_injected_clock(tmp_path: Path):
    repo = make_repo(tmp_path / "cached")
    now = [1000.0]
    calls: list[str] = []

    def exec_git(argv, **kwargs):
        calls.append(argv[argv.index("-C") + 2])
        return default_runner(argv, **kwargs)

    inspector = RepositoryInspector(policy_for(tmp_path), exec_git=exec_git, clock=lambda: now[0])
    first = inspector.inspect(repo)
    count = len(calls)
    now[0] += 299
    assert inspector.inspect(repo) == first
    assert len(calls) == count
    now[0] += 2
    inspector.inspect(repo)
    assert len(calls) == 2 * count


def test_inspect_all_marks_roots(tmp_path: Path):
    scan = tmp_path / "scan"
    explicit = make_repo(scan / "explicit")
    make_repo(scan / "scanned")
    result = RepositoryInspector(policy_for(tmp_path)).inspect_all(
        paths=[str(explicit)], scan_roots=[str(scan)], use_cache=False
    )
    by_name = {item["name"]: item for item in result["repositories"]}
    assert by_name["explicit"]["root"] is None
    assert by_name["scanned"]["root"] == str(scan.resolve())
    assert result["problems"] == []


def test_a_policy_is_required():
    with pytest.raises(TypeError):
        RepositoryInspector(None)  # type: ignore[arg-type]


def test_cache_asks_the_read_policy_first(tmp_path: Path):
    allowed = tmp_path / "allowed"
    repo = make_repo(allowed / "repo")
    inspector = RepositoryInspector(policy_for(allowed), clock=lambda: 0.0)
    assert "error" not in inspector.inspect(repo)
    other = tmp_path / "other"
    other.mkdir()
    inspector.policy = policy_for(other)
    data = inspector.inspect(repo)
    assert data["issues"] == ["inspection_failed"] and "read policy" in data["error"]


def test_cache_has_an_entry_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(repo_inspect, "MAX_CACHE_ENTRIES", 2)
    inspector = RepositoryInspector(policy_for(tmp_path), clock=lambda: 0.0)
    for n in range(4):
        inspector.inspect(tmp_path / f"missing-{n}")
    assert len(inspector._cache) == 2


def test_inspect_all_has_a_deadline(tmp_path: Path):
    first = make_repo(tmp_path / "scan" / "a")
    make_repo(tmp_path / "scan" / "b")
    now = [0.0]

    def exec_git(argv, **kwargs):
        now[0] += 61.0
        return default_runner(argv, **kwargs)

    inspector = RepositoryInspector(policy_for(tmp_path), exec_git=exec_git, clock=lambda: now[0])
    result = inspector.inspect_all(scan_roots=[str(tmp_path / "scan")], use_cache=False)
    a, b = result["repositories"]
    assert a["path"] == str(first) and "error" not in a
    assert b["issues"] == ["inspection_failed"] and b["error"] == "deadline"


def test_worktree_list_uses_nul_records_and_hides_outside_paths(tmp_path: Path):
    repo = (tmp_path / "primary").resolve()
    text = "\0".join(
        [
            f"worktree {repo}",
            "HEAD 1234567890abcdef",
            "branch refs/heads/main",
            "",
            "worktree /outside/fake\ndetached\nlocked",
            "HEAD abcdef0123456789",
            "branch refs/heads/x",
            "",
        ]
    )
    items = repo_inspect._parse_worktrees(text, repo, lambda path: False)
    assert len(items) == 2
    assert items[0]["path"] == str(repo)
    assert items[1]["path"] == repo_inspect.OUTSIDE
    assert items[1]["locked"] is False and items[1]["detached"] is False


def test_linked_worktree_outside_the_read_roots_is_hidden(tmp_path: Path):
    allowed = tmp_path / "allowed"
    repo = make_repo(allowed / "repo")
    git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "outside-wt"))
    data = inspect(repo, allowed)
    paths = [
        w["path"] if w["path"] == repo_inspect.OUTSIDE else os.path.normpath(w["path"])
        for w in data["worktreeList"]
    ]
    assert paths == [os.path.normpath(str(repo)), repo_inspect.OUTSIDE]
    assert "outside-wt" not in repr(data)


def test_outside_paths_in_git_errors_are_replaced(tmp_path: Path):
    policy = policy_for(tmp_path)
    text = f"fatal: '/elsewhere/secret/repo' and {tmp_path}/ok: bad refs/heads/x"
    out = repo_inspect.scrub_outside_paths(policy, text)
    assert "/elsewhere" not in out
    assert f"'{repo_inspect.OUTSIDE}'" in out
    assert f"{tmp_path}/ok:" in out and "refs/heads/x" in out


def test_scan_lists_no_more_than_the_entry_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(repo_inspect, "MAX_SCAN_ENTRIES", 3)
    scan = tmp_path / "scan"
    for n in range(6):
        (scan / f"d{n}").mkdir(parents=True)
    seen: list[int] = []
    real = os.scandir

    class Counting:
        def __init__(self, path):
            self.inner = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def __iter__(self):
            for entry in self.inner:
                seen.append(1)
                yield entry

    monkeypatch.setattr(repo_inspect.os, "scandir", Counting)
    found, _ = discover_repositories(policy_for(tmp_path), scan_roots=[str(scan)])
    assert found == [] and len(seen) <= 4


@pytest.mark.skipif(not POSIX, reason="the marker scripts are POSIX shell")
def test_a_failed_second_driver_listing_refuses_the_repository(tmp_path: Path):
    """Fail closed: if the post-status listing errors (a bad config line), it is not read as 'no drivers'."""
    repo = make_repo(tmp_path / "failclosed")
    calls = {"filters": 0}

    def exec_git(argv, **kwargs):
        out = default_runner(argv, **kwargs)
        if "--get-regexp" in argv and r"^filter\." in argv:
            calls["filters"] += 1
            if calls["filters"] == 2:
                return GitOutput(128, "", "fatal: bad config line 3")
        return out

    data = inspect(repo, exec_git=exec_git)
    assert data["issues"] == ["inspection_failed"]
    assert "git filters failed" in data["error"]


def test_a_driver_that_is_removed_again_inside_the_status_window_is_not_detected_and_the_docs_say_so():
    text = (Path(__file__).resolve().parents[1] / "docs" / "repo-inspection.md").read_text(
        encoding="utf-8"
    )
    assert "added and removed again inside the `status` window" in text
    assert "Decision (commander, 2026-11-22)" in text and "Awaiting" not in text
