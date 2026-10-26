"""#245: Stira's ``editor`` (SDK ``file_editor`` + ``StiraWriteSandbox``) vs the upstream one.

The first block reproduces the defects of the deprecated ``strands_tools.editor``
(0.8.9, measured) so the reason for the migration stays visible; the rest
checks that Stira's editor does not have them and that the write boundary is
as strong as ``file_write``'s.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from importlib.util import find_spec
from pathlib import Path

import pytest

from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.tools import tool_registry
from stiracore.tools.file_editor_tool import (
    StiraWriteSandbox,
    from_editor_path,
    to_editor_path,
)
from stiracore.tools.identity_tools import IdentityProtectedTool
from stiracore.tools.tool_consent import consent_granted, mark_gate_passed

DARWIN = sys.platform == "darwin"
NOT_APPROVED = "not inside a step Stira's approval gate granted"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "memory" / "procedural").mkdir(parents=True)
    (workspace / "SOUL.md").write_text("soul", encoding="utf-8")
    return workspace


def _policy(ws: Path) -> WritePathPolicy:
    return WritePathPolicy(
        base_dir=ws, protected_dirs=[ws / "memory" / "procedural"], write_roots=[WORKSPACE_ROOT]
    )


def _tools(ws: Path) -> dict:
    """The tools exactly as an agent gets them (registry wrapper + per-agent policy)."""
    tools = tool_registry.get_tools(write_policy=_policy(ws), include_exec=False)
    return {tool.tool_name: tool for tool in tools}


def _call(tool, tool_input: dict, use_id: str = "t1") -> dict:
    """Run one call as if Stira's hooks approved it (a single-use gate mark)."""
    use = {"toolUseId": use_id, "name": tool.tool_name, "input": tool_input}
    state: dict = {"agent": None}  # the SDK always supplies it

    async def collect():
        return [event async for event in tool.stream(use, state)]

    mark_gate_passed(state, use)
    event = asyncio.run(collect())[-1]
    return getattr(event, "tool_result", None) or event["tool_result"]


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _unlock(path: Path) -> None:
    if path.exists():
        os.chflags(path, 0)


# --- The upstream defects (reproduced) -----------------------------------------


@pytest.fixture
def upstream_editor(monkeypatch):
    if find_spec("strands_tools") is None or find_spec("strands_tools.editor") is None:
        pytest.skip("strands_tools.editor is not installed")
    import strands_tools.editor as module

    # Test-only: skip the upstream terminal prompt; Stira never uses this module.
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    monkeypatch.delenv("EDITOR_DISABLE_BACKUP", raising=False)
    return module.editor


def test_upstream_editor_cache_overwrites_an_external_change(tmp_path, upstream_editor):
    target = tmp_path / "a.txt"
    upstream_editor(command="create", path=str(target), file_text="one\ntwo\n")
    target.write_text("one\ntwo\nEXTERNAL\n", encoding="utf-8")
    upstream_editor(command="str_replace", path=str(target), old_str="two", new_str="TWO")
    assert target.read_text(encoding="utf-8") == "one\nTWO\n"  # the external line is lost


def test_upstream_editor_relative_create_fails(tmp_path, monkeypatch, upstream_editor):
    monkeypatch.chdir(tmp_path)
    result = upstream_editor(command="create", path="rel.txt", file_text="x")
    assert result["status"] == "error" and "No such file or directory: ''" in _text(result)


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_upstream_editor_backup_keeps_the_lock(tmp_path, upstream_editor):
    target = tmp_path / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    backup = Path(f"{target}.bak")
    try:
        os.chflags(target, stat.UF_IMMUTABLE)
        assert upstream_editor(command="str_replace", path=str(target), old_str="one", new_str="1")[
            "status"
        ] == ("error")
        os.chflags(target, 0)
        retry = upstream_editor(command="str_replace", path=str(target), old_str="one", new_str="1")
        assert retry["status"] == "error" and str(backup) in _text(retry)
        assert backup.stat().st_flags & stat.UF_IMMUTABLE  # copy2 copied the flag
    finally:
        _unlock(target)
        _unlock(backup)


# --- Stira's editor: the acceptance criteria -----------------------------------


def test_registered_editor_is_stiras_with_the_same_name_and_place():
    tool = tool_registry.WRITE_TOOLS["editor"]
    assert isinstance(tool, IdentityProtectedTool)
    assert tool.tool_spec["name"] == "editor"
    assert set(tool.tool_spec["inputSchema"]["json"]["properties"]) >= {
        "command",
        "path",
        "file_text",
        "old_str",
        "new_str",
        "insert_line",
        "view_range",
    }
    names = list(tool_registry.WRITE_TOOLS)
    assert names.index("editor") == names.index("file_write") + 1  # order kept (#254)


def test_str_replace_reads_the_current_file_and_keeps_an_external_change(ws):
    editor = _tools(ws)["editor"]
    target = ws / "a.txt"
    assert _call(editor, {"command": "create", "path": str(target), "file_text": "one\ntwo\n"})[
        "status"
    ] == ("success")
    assert _call(editor, {"command": "view", "path": str(target)})["status"] == "success"
    target.write_text("one\ntwo\nEXTERNAL\n", encoding="utf-8")
    result = _call(
        editor, {"command": "str_replace", "path": str(target), "old_str": "two", "new_str": "TWO"}
    )
    assert result["status"] == "success", result
    assert target.read_text(encoding="utf-8") == "one\nTWO\nEXTERNAL\n"
    # A replacement that no longer matches the file on disk fails and writes nothing.
    target.write_text("changed\n", encoding="utf-8")
    stale = _call(
        editor, {"command": "str_replace", "path": str(target), "old_str": "TWO", "new_str": "x"}
    )
    assert stale["status"] == "error" and target.read_text(encoding="utf-8") == "changed\n"


def test_relative_create_succeeds_in_the_workspace(ws, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not the workspace: the cwd must not matter
    editor = _tools(ws)["editor"]
    result = _call(editor, {"command": "create", "path": "notes/rel.txt", "file_text": "x"})
    assert result["status"] == "success", result
    assert (ws / "notes" / "rel.txt").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "notes").exists()
    view = _call(editor, {"command": "view", "path": "notes/rel.txt"})
    assert view["status"] == "success" and "x" in _text(view)


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_unlocking_once_lets_the_retry_succeed_and_leaves_no_backup(ws):
    editor = _tools(ws)["editor"]
    target = ws / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    edit = {"command": "str_replace", "path": str(target), "old_str": "one", "new_str": "1"}
    try:
        os.chflags(target, stat.UF_IMMUTABLE)
        assert _call(editor, edit)["status"] == "error"
        os.chflags(target, 0)
        assert _call(editor, edit, "t2")["status"] == "success"
    finally:
        _unlock(target)
    assert target.read_text(encoding="utf-8") == "1\n"
    assert sorted(p.name for p in ws.iterdir() if p.is_file()) == ["SOUL.md", "a.txt"]


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def _boundary_targets(ws: Path, outside: Path) -> dict[str, str]:
    link = ws / "proc-link"
    link.symlink_to(ws / "memory" / "procedural", target_is_directory=True)
    targets = {
        "soul": str(ws / "SOUL.md"),
        "ipd_new": str(ws / "IPD.md"),
        "protected_dir": str(ws / "memory" / "procedural" / "new.md"),
        "symlink_into_protected": str(link / "new.md"),
        "dotdot": f"{ws}/memory/../memory/procedural/x.md",
        "dotdot_through_missing": f"{ws}/notes/../memory/procedural/x.md",
        "outside_write_roots": str(outside / "x.md"),
        "relative_soul": "SOUL.md",
    }
    if _case_insensitive(ws):
        targets["case_variant"] = str(ws / "memory" / "PROCEDURAL" / "new.md")
    return targets


def test_boundaries_hold_for_editor_exactly_as_for_file_write(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    tools = _tools(ws)
    for label, path in _boundary_targets(ws, outside).items():
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        written = _call(tools["file_write"], {"path": path, "content": "x"})
        created = _call(tools["editor"], {"command": "create", "path": path, "file_text": "x"})
        assert written["status"] == created["status"] == "error", label
        assert _text(created) == _text(written), label  # the same decision and reason
        if Path(path).is_file() or label == "soul":
            edited = _call(
                tools["editor"],
                {"command": "str_replace", "path": path, "old_str": "soul", "new_str": "x"},
            )
            assert edited["status"] == "error", label
        assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before, label
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul"


# --- The sandbox enforces the policy itself (defence in depth) -----------------


def _write(sandbox: StiraWriteSandbox, path: str, content: bytes = b"x") -> None:
    asyncio.run(sandbox.write_file(path, content))


def test_sandbox_refuses_writes_outside_an_approved_step(ws):
    sandbox, target = StiraWriteSandbox(), ws / "a.txt"
    with pytest.raises(PermissionError, match=NOT_APPROVED):
        _write(sandbox, str(target))
    with (
        consent_granted("file_write", write_path=str(target)),  # another tool's grant
        pytest.raises(PermissionError, match=NOT_APPROVED),
    ):
        _write(sandbox, str(target))
    with (
        consent_granted("editor", write_path=str(ws / "other.txt"), policy=_policy(ws)),
        pytest.raises(PermissionError, match=NOT_APPROVED),  # not the checked path
    ):
        _write(sandbox, str(target))
    assert not target.exists()


def test_sandbox_rechecks_every_wrappers_policy_at_write_time(ws):
    sandbox = StiraWriteSandbox()
    soul = ws / "SOUL.md"
    with (
        consent_granted("editor", write_path=str(soul), policy=WritePathPolicy(base_dir=ws)),
        pytest.raises(PermissionError, match="read-only"),
    ):
        _write(sandbox, str(soul))
    target = ws / "a.txt"
    roots_elsewhere = WritePathPolicy(base_dir=ws, write_roots=[ws / "memory"])
    # A nested grant keeps the outer policy: the inner (floor only) is not enough.
    with (
        consent_granted("editor", write_path=str(target), policy=roots_elsewhere),
        consent_granted("editor", write_path=str(target), policy=WritePathPolicy()),
        pytest.raises(PermissionError, match="write roots"),
    ):
        _write(sandbox, str(target))
    assert soul.read_text(encoding="utf-8") == "soul" and not target.exists()


def test_sandbox_does_not_follow_a_symlink_swapped_in_after_the_check(ws):
    sandbox = StiraWriteSandbox()
    target = ws / "a.txt"
    with consent_granted("editor", write_path=str(target), policy=_policy(ws)):
        target.symlink_to(ws / "SOUL.md")  # after the wrapper's check, before the write
        with pytest.raises(OSError):
            _write(sandbox, str(target))
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul"


def test_sandbox_refuses_a_hard_link_swapped_in_after_the_check(ws, monkeypatch):
    """Between the policy re-check and the open, the target becomes a hard link to SOUL.md."""
    sandbox = StiraWriteSandbox()
    target = ws / "a.txt"
    original = StiraWriteSandbox._authorize_write

    def swap_after_check(self, path):
        native = original(self, path)
        os.link(ws / "SOUL.md", target)
        return native

    monkeypatch.setattr(StiraWriteSandbox, "_authorize_write", swap_after_check)
    with (
        consent_granted("editor", write_path=str(target), policy=_policy(ws)),
        pytest.raises(PermissionError),
    ):
        _write(sandbox, str(target))
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == "soul"


def test_sandbox_runs_nothing_and_removes_nothing(ws):
    sandbox = StiraWriteSandbox()
    (ws / "a.txt").write_text("x", encoding="utf-8")

    async def run(stream):
        return [chunk async for chunk in stream]

    with pytest.raises(PermissionError):
        asyncio.run(run(sandbox.execute_streaming("touch pwned")))
    with pytest.raises(PermissionError):
        asyncio.run(run(sandbox.execute_code_streaming("print(1)", "python3")))
    with pytest.raises(PermissionError):
        asyncio.run(sandbox.remove_file(str(ws / "a.txt")))
    assert (ws / "a.txt").exists() and not Path("pwned").exists()


def test_raw_editor_without_the_wrapper_cannot_write(ws):
    raw = tool_registry.WRITE_TOOLS["editor"].wrapped
    result = _call(raw, {"command": "create", "path": str(ws / "raw.txt"), "file_text": "x"})
    assert result["status"] == "error" and NOT_APPROVED in _text(result)
    assert not (ws / "raw.txt").exists()


def test_windows_paths_round_trip_through_the_posix_form():
    assert to_editor_path(r"C:\ws\a.md", windows=True) == "/C:/ws/a.md"
    assert from_editor_path("/C:/ws/a.md", windows=True) == "C:/ws/a.md"
    assert from_editor_path("/c:", windows=True) == "c:"
    assert to_editor_path("/ws/a.md", windows=True) == "/ws/a.md"
    assert to_editor_path("/ws/a.md", windows=False) == "/ws/a.md"
    assert from_editor_path("/C:/ws", windows=False) == "/C:/ws"
