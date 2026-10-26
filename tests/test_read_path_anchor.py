"""#306: a relative path means the same file for ``file_read`` as for ``file_write``.

The write side anchors a relative ``path`` to the workspace (``WritePathPolicy.base_dir``);
the bare read tools used the process cwd, so a file written as ``x.txt`` could not be read
back as ``x.txt``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from strands.tools.tools import AgentTool

from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.tools import tool_registry
from stiracore.tools.file_tools import file_read, shown_path
from stiracore.tools.identity_tools import WorkspaceAnchoredReadTool


def _run(tool: Any, arguments: dict) -> dict:
    async def go() -> dict:
        result: dict = {}
        async for event in tool.stream({"toolUseId": "t1", "name": "x", "input": arguments}, {}):
            # ToolResultEvent (and the stand-in below) expose the result under "tool_result".
            result = event["tool_result"]
        return result

    return asyncio.run(go())


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "data" / "workspace"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture()
def policy(workspace: Path) -> WritePathPolicy:
    return WritePathPolicy(base_dir=workspace, write_roots=[WORKSPACE_ROOT])


@pytest.fixture()
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A process cwd that is NOT the workspace (the server runs from the repository root)."""
    cwd = tmp_path / "repo-root"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def _tools(policy: WritePathPolicy | None) -> dict[str, AgentTool]:
    tools = tool_registry.get_tools(write_policy=policy, include_exec=False)
    return {t.tool_name: t for t in tools}


def test_relative_read_finds_the_file_written_into_the_workspace(
    workspace: Path, policy: WritePathPolicy, elsewhere: Path
) -> None:
    (workspace / "stira_slash_test.txt").write_text("a / b 日本語\n", encoding="utf-8")
    # The bare tool looks in the cwd and fails (the report in #306) ...
    assert file_read("stira_slash_test.txt")["status"] == "error"
    # ... the registered tool anchors to the workspace, as file_write does.
    result = _run(_tools(policy)["file_read"], {"path": "stira_slash_test.txt"})
    assert result["status"] == "success", result
    assert _text(result) == "a / b 日本語\n"


@pytest.mark.parametrize("name", ["./note.txt", "notes/note.txt", "notes/../note.txt"])
def test_relative_spellings_resolve_in_the_workspace(
    workspace: Path, policy: WritePathPolicy, elsewhere: Path, name: str
) -> None:
    (workspace / "notes").mkdir()
    (workspace / "note.txt").write_text("x", encoding="utf-8")
    (workspace / "notes" / "note.txt").write_text("x", encoding="utf-8")
    result = _run(_tools(policy)["file_read"], {"path": name})
    assert result["status"] == "success", result


def test_absolute_and_home_paths_are_unchanged(
    workspace: Path, policy: WritePathPolicy, elsewhere: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("abs", encoding="utf-8")
    assert _text(_run(_tools(policy)["file_read"], {"path": str(outside)})) == "abs"


def test_without_a_policy_the_tools_are_untouched(elsewhere: Path) -> None:
    tool = _tools(None)["file_read"]
    assert not isinstance(tool, WorkspaceAnchoredReadTool)


def test_missing_file_error_names_the_resolved_path_and_reason(
    workspace: Path, policy: WritePathPolicy, elsewhere: Path
) -> None:
    result = _run(_tools(policy)["file_read"], {"path": "nope.txt"})
    assert result["status"] == "error"
    assert "path_not_found" in _text(result)
    # Shown with the home directory as ~ (#307, shown_path).
    assert shown_path(str(workspace / "nope.txt")) in _text(result)


def test_directory_error_says_so(workspace: Path, policy: WritePathPolicy, elsewhere: Path) -> None:
    (workspace / "sub").mkdir()
    result = _run(_tools(policy)["file_read"], {"path": "sub"})
    assert result["status"] == "error" and "is_directory" in _text(result)


def test_bare_tool_error_shows_the_cwd_resolution(elsewhere: Path) -> None:
    text = file_read("x.txt")["content"][0]["text"]
    assert "path_not_found" in text and shown_path(str(elsewhere / "x.txt")) in text


def test_view_image_relative_path_is_anchored(
    workspace: Path, policy: WritePathPolicy, elsewhere: Path
) -> None:
    from PIL import Image

    Image.new("RGB", (4, 4), (1, 2, 3)).save(workspace / "pic.png")
    result = _run(_tools(policy)["view_image"], {"path": "pic.png"})
    assert result["status"] == "success", result


class _Recorder(AgentTool):
    """Stands in for a read tool whose path argument has another name."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict | None = None

    @property
    def tool_name(self) -> str:
        return "image_reader"

    @property
    def tool_spec(self) -> dict:
        return {"name": "image_reader", "description": "d", "inputSchema": {"json": {}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        self.seen = tool_use["input"]
        yield {"tool_result": {"toolUseId": "t", "status": "success", "content": [{"text": "ok"}]}}


def test_other_argument_names_and_odd_inputs(workspace: Path, policy: WritePathPolicy) -> None:
    inner = _Recorder()
    tool = WorkspaceAnchoredReadTool(inner, policy, "image_path")
    assert (tool.tool_name, tool.tool_spec) == (inner.tool_name, inner.tool_spec)
    _run(tool, {"image_path": "pic.png", "other": "keep"})
    assert inner.seen == {"image_path": str(workspace / "pic.png"), "other": "keep"}
    for odd in ({"image_path": ""}, {"image_path": 3}, {}, {"image_path": "a\0b"}):
        _run(tool, odd)
        assert inner.seen == odd  # passed through; the wrapped tool reports its own error


def test_cwd_is_not_the_anchor(workspace: Path, policy: WritePathPolicy, elsewhere: Path) -> None:
    """A same-named file in the cwd must not be read instead of the workspace's."""
    (elsewhere / "same.txt").write_text("from cwd", encoding="utf-8")
    (workspace / "same.txt").write_text("from workspace", encoding="utf-8")
    assert _text(_run(_tools(policy)["file_read"], {"path": "same.txt"})) == "from workspace"


def test_a_workspace_base_that_cannot_be_resolved_refuses_instead_of_falling_back_to_cwd(tmp_path):
    """Fail closed: when the anchor cannot be computed, the read is refused, never run against cwd."""
    import asyncio

    from stiracore.tools.identity_tools import WorkspaceAnchoredReadTool

    class Boom:
        def absolute(self, path):
            raise OSError("no base")

    class Inner:
        tool_name = "file_read"
        tool_type = "python"
        called = False

        def __init__(self):
            self.tool_spec = {"name": "file_read"}

        async def stream(self, tool_use, invocation_state, **kwargs):
            type(self).called = True
            yield {"toolResult": "ran"}

    tool = WorkspaceAnchoredReadTool(Inner(), Boom())
    (tmp_path / "same.txt").write_text("cwd file", encoding="utf-8")

    async def run():
        return [
            e async for e in tool.stream({"toolUseId": "t1", "input": {"path": "same.txt"}}, {})
        ]

    events = asyncio.run(run())
    assert not Inner.called
    assert len(events) == 1
