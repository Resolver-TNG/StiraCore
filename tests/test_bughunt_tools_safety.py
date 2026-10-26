"""#601 (C1, tools-safety): regressions found by the pre-release review, one test per item."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.identity_paths import WritePathPolicy
from stiracore.tools import run_checks
from stiracore.tools.artifact_store import ArtifactStore
from stiracore.tools.checked_open import open_checked
from stiracore.tools.memory_store import MemoryLayer, MemoryStore, MemorySymlinkError
from stiracore.tools.os_protection import os_protection_denial

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks/FIFOs")


def _ends(target, *args, timeout: float = 3.0):
    """Run ``target(*args)`` in a thread; ``(finished, result-or-exception)``."""
    box: dict = {}

    def runner():
        try:
            box["value"] = target(*args)
        except BaseException as error:  # noqa: BLE001 - reported to the test
            box["value"] = error

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), box.get("value"), thread


def _unblock_fifo(path: Path, mode: int) -> None:
    """Open the other end of a FIFO so a thread blocked on it can finish."""
    try:
        fd = os.open(path, mode | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


# --- C1: a side file that is a symlink to a secret ----------------------------------------


@posix_only
def test_side_file_symlink_to_a_secret_name_is_refused(tmp_path):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / ".env").write_text("TOKEN=x\n")
    (workspace / "report.bak").symlink_to(workspace / ".env")
    policy = WritePathPolicy(base_dir=workspace, write_roots=[str(workspace)])
    decision = policy.check("report", suffixes=(".bak",))
    assert not decision.allowed and decision.code == "secret_path"


@posix_only
def test_side_file_symlink_into_an_added_secret_path_is_refused(tmp_path):
    workspace = (tmp_path / "ws").resolve()
    (workspace / "private").mkdir(parents=True)
    (workspace / "private" / "token").write_text("x\n")
    (workspace / "report.bak").symlink_to(workspace / "private" / "token")
    policy = WritePathPolicy(
        base_dir=workspace,
        write_roots=[str(workspace)],
        read_secret_paths=[str(workspace / "private")],
    )
    decision = policy.check("report", suffixes=(".bak",))
    assert not decision.allowed and decision.code == "secret_path"
    # A plain side file is still fine.
    assert policy.check("notes", suffixes=(".bak",)).allowed


# --- C15: a Windows full path to sudo -------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        r"C:\Windows\System32\sudo.exe whoami",
        r"dir\sudo whoami",
        r"C:\tools\csrutil.exe status",
    ],
)
def test_os_protection_sees_a_command_after_a_backslash(command):
    assert os_protection_denial("shell", {"command": command}) is not None


def test_os_protection_still_folds_escapes_and_spares_other_words():
    assert os_protection_denial("shell", {"command": r"su\do ls"}) is not None
    assert os_protection_denial("shell", {"command": r"type C:\notes\sudoku.txt"}) is None


# --- C12: MemoryStore.read_text and a symlink swapped in after the check -------------------


@posix_only
def test_read_text_does_not_follow_a_symlink_swapped_in_after_the_check(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "data", "agent")
    store.write(MemoryLayer.EPISODIC, "note", "inside")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE SECRET")
    checked = store._path

    def swap_after_check(layer, key):
        path = checked(layer, key)
        path.unlink()
        path.symlink_to(outside)
        return path

    monkeypatch.setattr(store, "_path", swap_after_check)
    with pytest.raises(MemorySymlinkError):
        store.read_text(MemoryLayer.EPISODIC, "note")


def test_read_text_keeps_the_raw_newlines(tmp_path):
    store = MemoryStore(tmp_path / "data", "agent")
    store.write_text(MemoryLayer.EPISODIC, "note", "---\r\na: 1\r\n---\r\nline\r\n")
    assert store.read_text(MemoryLayer.EPISODIC, "note") == "---\r\na: 1\r\n---\r\nline\r\n"


# --- C14: one broken entry does not hide the others ----------------------------------------


def test_a_broken_entry_does_not_break_list_and_search(tmp_path):
    store = MemoryStore(tmp_path / "data", "agent")
    store.write(MemoryLayer.SEMANTIC, "good", "the apple fact")
    layer_dir = store.memory_dir / MemoryLayer.SEMANTIC.value
    (layer_dir / "bad-yaml.md").write_text("---\nkey: [unclosed\n---\napple\n", encoding="utf-8")
    (layer_dir / "bad-utf8.md").write_bytes(b"---\n---\napple \xff\xfe\n")
    assert [e.key for e in store.list_entries(MemoryLayer.SEMANTIC, limit=10)] == ["good"]
    assert [e.key for e in store.search("apple", layers=[MemoryLayer.SEMANTIC])] == ["good"]
    # The direct read of the broken entry still says it is broken.
    with pytest.raises(ValueError):
        store.read(MemoryLayer.SEMANTIC, "bad-yaml")


# --- C8: open_checked on a FIFO --------------------------------------------------------------


@posix_only
def test_open_checked_refuses_a_fifo_without_blocking(tmp_path):
    target = tmp_path / "out.txt"
    os.mkfifo(target)

    class Allow:
        def check(self, path):
            return SimpleNamespace(allowed=True, reason="", path=str(target))

    finished, value, thread = _ends(open_checked, str(target), [Allow()], str(target))
    if not finished:
        _unblock_fifo(target, os.O_RDONLY)
        thread.join(2)
    assert finished, "open_checked blocked on a FIFO"
    assert isinstance(value, OSError)


@posix_only
def test_open_checked_still_writes_a_regular_file(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old")

    class Allow:
        def check(self, path):
            return SimpleNamespace(allowed=True, reason="", path=str(target))

    fd = open_checked(str(target), [Allow()], str(target))
    try:
        os.write(fd, b"new")
    finally:
        os.close(fd)
    assert target.read_text() == "new"


# --- C18: discovery and a FIFO pyproject.toml ------------------------------------------------


@posix_only
def test_discovery_does_not_block_on_a_fifo_pyproject(tmp_path):
    os.mkfifo(tmp_path / "pyproject.toml")
    (tmp_path / "test_x.py").write_text("def test_x():\n    pass\n")
    finished, value, thread = _ends(run_checks._scan, tmp_path)
    if not finished:
        _unblock_fifo(tmp_path / "pyproject.toml", os.O_WRONLY)
        thread.join(2)
    assert finished, "discovery blocked on a FIFO"
    assert value["test_py"] is True and value["pytest_config"] is False


def test_discovery_still_reads_a_regular_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert run_checks._scan(tmp_path)["pytest_config"] is True


# --- C22: a report the child replaced with a symlink ----------------------------------------


@posix_only
def test_reports_are_not_read_through_a_symlink(tmp_path):
    outside = tmp_path / "home-file.xml"
    outside.write_text(
        '<testsuite><testcase name="t"><failure message="SECRET"/></testcase></testsuite>'
    )
    rows = tmp_path / "home-file.json"
    rows.write_text(json.dumps([{"code": "X", "message": "SECRET"}]))
    out = tmp_path / "out" / "0"
    out.mkdir(parents=True)
    (out / "junit.xml").symlink_to(outside)
    (out / "ruff.json").symlink_to(rows)
    assert run_checks._junit(out / "junit.xml") is None
    assert run_checks._ruff(out / "ruff.json") is None


@posix_only
def test_reports_are_not_read_through_a_symlinked_report_directory(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "junit.xml").write_text('<testsuite><testcase name="t"/></testsuite>')
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "0").symlink_to(real)
    assert run_checks._junit(tmp_path / "out" / "0" / "junit.xml") is None


@posix_only
def test_reports_are_not_read_through_a_symlinked_out_directory(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "0").mkdir(parents=True)
    (elsewhere / "0" / "junit.xml").write_text(
        '<testsuite><testcase name="t"><failure message="SECRET"/></testcase></testsuite>'
    )
    (elsewhere / "0" / "ruff.json").write_text(json.dumps([{"code": "X"}]))
    run = tmp_path / "run"
    run.mkdir()
    (run / "out").symlink_to(elsewhere)
    assert run_checks._junit(run / "out" / "0" / "junit.xml") is None
    assert run_checks._ruff(run / "out" / "0" / "ruff.json") is None


def test_regular_reports_are_still_read(tmp_path):
    out = tmp_path / "run" / "out" / "0"
    out.mkdir(parents=True)
    (out / "junit.xml").write_text(
        '<testsuite><testcase name="a"/><testcase name="b"><failure message="m"/></testcase>'
        "</testsuite>"
    )
    (out / "ruff.json").write_text(json.dumps([{"code": "E1", "message": "m"}]))
    junit = run_checks._junit(out / "junit.xml")
    assert junit["counts"]["passed"] == 1 and junit["counts"]["failed"] == 1
    assert run_checks._ruff(out / "ruff.json")["counts"]["findings"] == 1


# --- C17 / C20: an absolute in-tree symlink in the throwaway copy ---------------------------


@posix_only
def test_copy_tree_points_an_absolute_inner_link_at_the_copy(tmp_path):
    source = (tmp_path / "ws").resolve()
    (source / "data").mkdir(parents=True)
    (source / "data" / "file.txt").write_text("real")
    (source / "link").symlink_to(source / "data")
    (source / "rel").symlink_to("data/file.txt")
    target = tmp_path / "copy"
    run_checks.copy_tree(source, target, None)
    copied = Path(os.path.realpath(target / "link"))
    assert copied == (target / "data").resolve()
    assert (target / "link" / "file.txt").read_text() == "real"
    assert Path(os.path.realpath(target / "rel")) == (target / "data" / "file.txt").resolve()


@posix_only
def test_copy_tree_maps_links_also_from_an_unresolved_source(tmp_path):
    real = (tmp_path / "real-ws").resolve()
    (real / "data").mkdir(parents=True)
    (real / "data" / "file.txt").write_text("real")
    (real / "link").symlink_to(real / "data")
    alias = tmp_path / "alias-ws"
    alias.symlink_to(real)  # the caller passes a path through a symlink
    target = tmp_path / "copy"
    run_checks.copy_tree(alias, target, None)
    assert Path(os.path.realpath(target / "link")) == (target / "data").resolve()


# --- C21: the disk watchdog past its scan bound ---------------------------------------------


def test_dir_size_past_its_bound_is_not_read_as_small(tmp_path):
    (tmp_path / "a").mkdir()
    for index in range(5):
        (tmp_path / "a" / f"f{index}").write_text("")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "big").write_bytes(b"x" * 4096)
    assert run_checks._dir_size(str(tmp_path), cap=3) > run_checks.TMP_TOTAL_BYTES
    assert run_checks._dir_size(str(tmp_path)) < run_checks.TMP_TOTAL_BYTES


# --- C19: the output reader's descriptor ---------------------------------------------------


@posix_only
def test_output_reader_closes_its_descriptor_when_the_pipe_ends():
    read_fd, write_fd = os.pipe()
    output = run_checks._Output(read_fd)
    output.start()
    assert output.owns_fd
    os.write(write_fd, b"hello")
    os.close(write_fd)
    output.thread.join(2)
    assert not output.thread.is_alive()
    with pytest.raises(OSError):
        os.fstat(read_fd)


def _count_closes(monkeypatch):
    """Record every ``os.close`` of the pipe's read end that ``run_contained`` creates."""
    pipes, closes = [], []
    real_pipe, real_close = os.pipe, os.close

    def pipe():
        pair = real_pipe()
        if sys._getframe(1).f_code.co_name == "run_contained":  # not subprocess's own pipes
            pipes.append(pair[0])
        return pair

    def close(fd):
        if fd in pipes:
            closes.append(fd)
        return real_close(fd)

    monkeypatch.setattr(run_checks.os, "pipe", pipe)
    monkeypatch.setattr(run_checks.os, "close", close)
    return pipes, closes


def _contained(tmp_path, argv):
    return run_checks.run_contained(
        argv,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        timeout_s=20,
        spec=run_checks.limit_spec(20, run_checks._user_processes() or 0),
        profile=None,
        tmp_root=str(tmp_path),
    )


@posix_only
def test_run_contained_closes_the_read_end_once(tmp_path, monkeypatch):
    pipes, closes = _count_closes(monkeypatch)
    ran = _contained(tmp_path, [sys.executable, "-c", "print('hi')"])
    assert "hi" in ran.output and ran.exit_code == 0
    assert len(pipes) == 1 and closes == pipes  # by the reader thread, exactly once


@posix_only
def test_run_contained_closes_the_read_end_once_when_the_launch_fails(tmp_path, monkeypatch):
    pipes, closes = _count_closes(monkeypatch)
    real_popen = run_checks.subprocess.Popen

    def popen(command, *args, **kwargs):
        if run_checks._LIMITER in command:  # the check itself (not the sentinel)
            raise OSError("launch failed")
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(run_checks.subprocess, "Popen", popen)
    with pytest.raises(OSError, match="launch failed"):
        _contained(tmp_path, [sys.executable, "-c", "pass"])
    assert len(pipes) == 1 and closes == pipes


# --- C27: a parent directory swapped for a symlink after the eligibility check -------------


@posix_only
def test_register_does_not_read_through_a_parent_swapped_after_the_check(tmp_path, monkeypatch):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.tools import workspace_artifacts as wa

    data_dir = (tmp_path / "data").resolve()
    workspace = data_dir / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "page.html").write_text("<p>inside</p>")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.html").write_text("OUTSIDE SECRET")
    policy, _floor = build_policy(AgentConfig(), data_dir)
    registry = wa.WorkspaceArtifacts(data_dir / "artifacts")
    checked = wa.eligible_path

    def swap_after_check(policy, path):
        answer = checked(policy, path)
        os.rename(workspace / "sub", workspace / "sub-moved")
        (workspace / "sub").symlink_to(outside)
        return answer

    monkeypatch.setattr(wa, "eligible_path", swap_after_check)
    try:
        result = registry.register(policy, "sub/page.html")
        assert result["status"] == "skipped" and result["reason"] == "symlink"
    finally:
        registry.close()


@posix_only
def test_register_does_not_read_through_a_workspace_swapped_after_the_check(tmp_path, monkeypatch):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.tools import workspace_artifacts as wa

    data_dir = (tmp_path / "data").resolve()
    workspace = data_dir / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "page.html").write_text("<p>inside</p>")
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "page.html").write_text("OUTSIDE SECRET")
    policy, _floor = build_policy(AgentConfig(), data_dir)
    registry = wa.WorkspaceArtifacts(data_dir / "artifacts")
    checked = wa.eligible_path

    def swap_after_check(policy, path):
        answer = checked(policy, path)
        os.rename(workspace, data_dir / "workspace-moved")
        workspace.symlink_to(outside)  # the workspace itself, left swapped
        return answer

    monkeypatch.setattr(wa, "eligible_path", swap_after_check)
    try:
        result = registry.register(policy, "sub/page.html")
        assert result["status"] == "skipped" and result["reason"] == "symlink"
    finally:
        registry.close()


@posix_only
def test_register_still_records_a_nested_file(tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.tools import workspace_artifacts as wa

    data_dir = (tmp_path / "data").resolve()
    (data_dir / "workspace" / "a" / "b").mkdir(parents=True)
    (data_dir / "workspace" / "a" / "b" / "page.html").write_text("<p>ok</p>")
    policy, _floor = build_policy(AgentConfig(), data_dir)
    registry = wa.WorkspaceArtifacts(data_dir / "artifacts")
    try:
        assert registry.register(policy, "a/b/page.html")["status"] == "registered"
    finally:
        registry.close()


@posix_only
def test_register_works_with_a_workspace_configured_through_a_symlink(tmp_path):
    """The descriptor chain starts at ``policy.workspace``, which is always canonical:
    ``IdentityPathPolicy`` resolves ``base_dir`` (also ``workspace_dir`` via a symlink,
    like macOS ``/tmp`` -> ``/private/tmp``), so a legitimate link is never a skip."""
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.tools import workspace_artifacts as wa

    data_dir = (tmp_path / "data").resolve()
    (data_dir / "workspace").mkdir(parents=True)
    real = (tmp_path / "real-projects").resolve()
    (real / "sub").mkdir(parents=True)
    (real / "sub" / "page.html").write_text("<p>ok</p>")
    alias = tmp_path / "projects-link"
    alias.symlink_to(real)
    policy, _floor = build_policy(AgentConfig(workspace_dir=str(alias)), data_dir)
    assert Path(policy.workspace) == real  # canonical, not the configured spelling
    registry = wa.WorkspaceArtifacts(data_dir / "artifacts")
    try:
        assert registry.register(policy, "sub/page.html")["status"] == "registered"
        assert registry.register(policy, str(alias / "sub" / "page.html"))["status"] in (
            "unchanged",
            "skipped",
        )
    finally:
        registry.close()


@posix_only
def test_open_below_refuses_the_workspace_itself(tmp_path):
    from stiracore.tools import workspace_artifacts as wa

    workspace = tmp_path.resolve()
    with pytest.raises(ValueError):
        wa._open_below(workspace, workspace, os.O_RDONLY)
    assert wa._read_regular(workspace, 100, named_by=workspace) == (None, "not_regular")


def test_discovery_head_survives_short_reads(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n" * 20 + "[tool.pytest.ini_options]\n")
    real_read = run_checks.os.read
    monkeypatch.setattr(run_checks.os, "read", lambda fd, n: real_read(fd, min(n, 7)))
    assert run_checks._scan(tmp_path)["pytest_config"] is True


# --- C3 / C5 / C6 / C7: ArtifactStore -------------------------------------------------------


@pytest.mark.parametrize("agent_id", ["../escape", "<absolute>", "a/b", "", ".."])
def test_artifact_store_refuses_an_agent_id_that_is_not_one_name(tmp_path, agent_id):
    elsewhere = tmp_path / "elsewhere"
    agent_id = str(elsewhere) if agent_id == "<absolute>" else agent_id
    with pytest.raises(ValueError):
        ArtifactStore(str(tmp_path / "base"), agent_id)
    assert not (tmp_path / "base" / "escape").exists() and not elsewhere.exists()


def test_artifact_store_bad_kind_leaves_no_directory(tmp_path):
    store = ArtifactStore(str(tmp_path), "agent")
    with pytest.raises(ValueError):
        store.create("x", "invalid", "body")
    assert store.create("x", "text", "body").version == 1


def test_artifact_store_update_never_overwrites_a_version(tmp_path, monkeypatch):
    store = ArtifactStore(str(tmp_path), "agent")
    store.create("x", "text", "v1")
    stale = store.get("x")
    store.update("x", "v2 by the first writer")
    # A second writer that read v1 before the first update finished.
    monkeypatch.setattr(store, "get", lambda slug, version=None: stale)
    second = store.update("x", "v2 by the second writer")
    monkeypatch.undo()
    assert second.version == 3
    assert store.get("x", 2).content == "v2 by the first writer"
    assert store.get("x").content == "v2 by the second writer"


def test_artifact_store_number_collision_keeps_both_versions(tmp_path, monkeypatch):
    """Two updates that both chose v2 (C7): the second finds v2 taken and writes v3."""
    store = ArtifactStore(str(tmp_path), "agent")
    store.create("x", "text", "v1")
    stale = store.get("x")
    store.update("x", "first writer")
    # The second writer read the state before the first published v2.
    monkeypatch.setattr(store, "get", lambda slug, version=None: stale)
    monkeypatch.setattr(store, "_versions", lambda dir_: [1])
    second = store.update("x", "second writer")
    monkeypatch.undo()
    assert second.version == 3
    assert store.get("x", 2).content == "first writer"
    assert store.get("x", 3).content == "second writer"


def test_artifact_store_failed_write_publishes_nothing(tmp_path, monkeypatch):
    """A write that fails part-way (C5) leaves no v2 and v1 untouched."""
    from stiracore.tools import artifact_store as module

    store = ArtifactStore(str(tmp_path), "agent")
    store.create("x", "text", "v1")

    def broken_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        store.update("x", "never published")
    monkeypatch.undo()
    names = sorted(p.name for p in (store.root / "x").iterdir())
    assert names == ["v1.json"]
    assert store.get("x").content == "v1"


def test_artifact_store_publishes_a_version_only_when_complete(tmp_path, monkeypatch):
    """The final name appears by one link of a complete file (C5), never by a write to it."""
    from stiracore.tools import artifact_store as module

    store = ArtifactStore(str(tmp_path), "agent")
    store.create("x", "text", "v1")
    seen = []
    real_link = module.os.link

    def link(src, dst):
        seen.append((Path(dst).exists(), json.loads(Path(src).read_text())["content"]))
        return real_link(src, dst)

    monkeypatch.setattr(module.os, "link", link)
    store.update("x", "complete body")
    assert seen == [(False, "complete body")]


def test_artifact_store_create_checks_metadata_and_cleans_up(tmp_path, monkeypatch):
    from stiracore.tools import artifact_store as module

    store = ArtifactStore(str(tmp_path), "agent")
    with pytest.raises(TypeError):
        store.create("x", "text", "body", {"bad": object()})
    assert not (store.root / "x").exists()

    def broken_link(src, dst):
        raise OSError("link failed")

    monkeypatch.setattr(module.os, "link", broken_link)
    with pytest.raises(OSError):
        store.create("y", "text", "body")
    monkeypatch.undo()
    assert not (store.root / "y").exists()
    assert store.create("y", "text", "body").version == 1


def test_artifact_store_skips_a_torn_latest_version(tmp_path):
    store = ArtifactStore(str(tmp_path), "agent")
    store.create("x", "text", "good")
    (store.root / "x" / "v2.json").write_text('{"slug": "x", "vers')
    assert store.get("x").content == "good"
    assert [a.content for a in store.list_artifacts()] == ["good"]
    assert not list((store.root / "x").glob(".*tmp*"))
