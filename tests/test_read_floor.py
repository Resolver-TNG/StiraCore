"""#312: the read floor — file tools do not read Stira's data directory.

#271 put the whole data directory on the *write* floor; reads had no floor, and with
the #306 anchoring ``../tool-config.json`` from the workspace was a short path to the
tool rules, the approval state, the Neural State and the checkpoints (past
conversations). Now the read tools follow the same judgement as the write floor:
inside the data directory only ``workspace/``, the episodic layer and
``context-stash/`` are readable; outside it reads are unchanged. A refusal is
``floor`` with the path and never says whether the file exists; no approval lifts it.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest
from PIL import Image

from stiracore.config.data_dir import build_policy, readable_names, shown_readable
from stiracore.config.models import AgentConfig
from stiracore.identity_paths import READ_FLOOR_ERROR
from stiracore.tools import file_tools, tool_registry
from stiracore.tools.file_tools import DIAGNOSIS_MARKER, read_floor
from stiracore.tools.tool_consent import mark_gate_passed

SECRET = "SECRET-POLICY-BODY-312"
MEMORY = Path("memory") / "agents" / "stira" / "memory"
CHECKPOINT = "checkpoints/0123456789abcdef0123456789abcdef.json"

# The #312 report and the rest of the data directory (relative to the data directory).
FLOOR_FILES = [
    "tool-config.json",
    "tool-config.json.head",
    "tool-policy-approvals.json",
    "neural-state.json",
    CHECKPOINT,
    "hitl-approvals.db",
    "hitl-operations.db",
    "events.db",
    "scheduler-events.jsonl",
    "gateway-config.json",
    "agent-config.json",
    "settings-history.jsonl",
    str(MEMORY / "semantic" / "facts.md"),
    str(MEMORY / "procedural" / "rule.md"),
    "memory/agents/other-agent/memory/episodic/x.md",
]
READABLE = [
    "workspace/notes.md",
    "workspace/deeper/x.md",
    str(MEMORY / "episodic" / "2026-10-16.md"),
    "context-stash/stira/offloader/tu-1_0",
]


def _touch(path: Path, text: str = SECRET) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "stira").resolve()
    for name in readable_names():
        (root / name).mkdir(parents=True)
    for name in FLOOR_FILES:
        _touch(root / name)
    connection = sqlite3.connect(root / "turns.db")
    connection.execute("create table t (x)")
    connection.close()
    for name in READABLE:
        _touch(root / name, "readable")
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the server runs from the repository, not the workspace
    return root


@pytest.fixture()
def tools(data_dir: Path) -> dict:
    policy, _floor = build_policy(AgentConfig(), data_dir)
    return {tool.tool_name: tool for tool in tool_registry.get_tools(write_policy=policy)}


def _run(tool, arguments: dict, *, gate: bool = False) -> dict:
    use = {"toolUseId": "u1", "name": tool.tool_name, "input": arguments}

    async def collect():
        state: dict = {"agent": None}
        if gate:
            mark_gate_passed(state, use)  # an approval (#242) does not lift the floor
        events = [event async for event in tool.stream(use, state)]
        last = events[-1]
        return last["tool_result"] if isinstance(last, dict) else last.tool_result

    return asyncio.run(collect())


def _diagnosis(result: dict) -> dict:
    for block in result["content"]:
        text = block.get("text", "")
        if text.startswith(DIAGNOSIS_MARKER + " "):
            return json.loads(text[len(DIAGNOSIS_MARKER) + 1 :])
    raise AssertionError(f"no diagnosis in {result}")


def _assert_floor(result: dict) -> dict:
    assert result["status"] == "error", result
    rendered = json.dumps(result, default=str)
    assert SECRET not in rendered
    assert READ_FLOOR_ERROR in result["content"][0]["text"]
    found = _diagnosis(result)
    assert found["reason"] == "floor"
    # Never whether a protected file exists (or what kind it is).
    assert not {"exists", "is_file", "is_directory"} & set(found)
    return found


# --- the #312 report: relative, absolute, every data-directory file ----------------------


@pytest.mark.parametrize(
    "path",
    [
        "../tool-config.json",
        "../tool-policy-approvals.json",
        "../neural-state.json",
        f"../{CHECKPOINT}",
        "../turns.db",
        "notes/../../tool-config.json",
        "./../hitl-approvals.db",
    ],
)
def test_the_reported_relative_paths_are_on_the_read_floor(tools, path):
    found = _assert_floor(_run(tools["file_read"], {"path": path}))
    assert found["requested_path"] == path


@pytest.mark.parametrize("name", [*FLOOR_FILES, "turns.db"])
def test_absolute_paths_into_the_data_directory_are_refused(tools, data_dir, name):
    _assert_floor(_run(tools["file_read"], {"path": str(data_dir / name)}))


def test_no_approval_lifts_the_read_floor(tools):
    _assert_floor(_run(tools["file_read"], {"path": "../tool-config.json"}, gate=True))


@pytest.mark.parametrize("name", READABLE)
def test_workspace_episodic_and_stash_stay_readable(tools, data_dir, name):
    result = _run(tools["file_read"], {"path": str(data_dir / name)})
    assert result["status"] == "success", result
    assert result["content"][0]["text"] == "readable"


def test_reads_outside_the_data_directory_need_a_read_root(tools, data_dir, tmp_path):
    # #457: outside the data directory reads are no longer unrestricted; with the
    # project as a read root they read as before #457.
    outside = _touch(tmp_path / "project" / "README.md", "outside")
    relative = os.path.relpath(outside, tmp_path / "stira" / "workspace")
    for path in (str(outside), relative):
        assert _diagnosis(_run(tools["file_read"], {"path": path}))["reason"] == (
            "outside_read_roots"
        )
    config = AgentConfig(read_roots=["@workspace", str(outside.parent)])
    policy, _ = build_policy(config, data_dir)
    widened = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}
    assert _run(widened["file_read"], {"path": str(outside)})["content"][0]["text"] == "outside"
    assert _run(widened["file_read"], {"path": relative})["status"] == "success"


def test_the_bare_tool_without_a_policy_is_unchanged(data_dir):
    # Library default (no server, no data directory): no read floor at all.
    bare = {t.tool_name: t for t in tool_registry.get_tools()}["file_read"]
    result = _run(bare, {"path": str(data_dir / "tool-config.json")})
    assert result["content"][0]["text"] == SECRET


# --- spellings: case, Unicode, new paths, the directory itself -------------------------


def test_new_and_missing_paths_say_floor_without_existence(tools, data_dir):
    existing = _assert_floor(_run(tools["file_read"], {"path": "../neural-state.json"}))
    missing = _assert_floor(_run(tools["file_read"], {"path": "../no-such-file.json"}))
    nested = _assert_floor(_run(tools["file_read"], {"path": "../checkpoints/missing/x.json"}))
    for found in (existing, missing, nested):
        assert set(found) == set(existing)
    assert not (data_dir / "no-such-file.json").exists()


def test_case_and_unicode_variants_are_refused(tools, data_dir):
    upper = data_dir.parent / data_dir.name.upper() / "tool-config.json"
    _assert_floor(_run(tools["file_read"], {"path": str(upper)}))
    _assert_floor(_run(tools["file_read"], {"path": "../TOOL-CONFIG.json"}))
    # A workspace spelled differently is not the (exact) exception: refused, never followed.
    _assert_floor(_run(tools["file_read"], {"path": str(data_dir / "WORKSPACE" / "notes.md")}))
    nfc = _touch(data_dir / "データ.md")
    nfd = unicodedata.normalize("NFD", str(nfc))
    assert nfd != str(nfc)
    _assert_floor(_run(tools["file_read"], {"path": nfd}))


def test_the_data_directory_itself_is_refused_as_floor(tools, data_dir):
    _assert_floor(_run(tools["file_read"], {"path": str(data_dir)}))
    _assert_floor(_run(tools["file_read"], {"path": ".."}))


# --- symlinks and hard links --------------------------------------------------------------


def test_a_symlink_from_the_workspace_into_the_data_directory_is_refused(tools, data_dir):
    workspace = data_dir / "workspace"
    (workspace / "policy.json").symlink_to(data_dir / "tool-config.json")
    (workspace / "all").symlink_to(data_dir, target_is_directory=True)
    _assert_floor(_run(tools["file_read"], {"path": "policy.json"}))
    _assert_floor(_run(tools["file_read"], {"path": "all/neural-state.json"}))
    # Also through a symlink outside the data directory.
    outside = data_dir.parent / "outside"
    outside.mkdir()
    (outside / "state.json").symlink_to(data_dir / "neural-state.json")
    _assert_floor(_run(tools["file_read"], {"path": str(outside / "state.json")}))


def test_a_symlinked_exception_is_not_readable(tmp_path, monkeypatch):
    root = (tmp_path / "stira").resolve()
    root.mkdir()
    _touch(root / "real" / "x.md")
    (root / "workspace").symlink_to(root / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    policy, floor = build_policy(AgentConfig(), root)
    assert "workspace" not in floor.writable
    file_read = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}["file_read"]
    _assert_floor(_run(file_read, {"path": str(root / "workspace" / "x.md")}))


def test_a_hard_link_of_a_data_directory_file_is_refused(tools, data_dir):
    inside = data_dir / "workspace" / "copy.json"
    os.link(data_dir / "tool-config.json", inside)
    _assert_floor(_run(tools["file_read"], {"path": "copy.json"}))
    outside = data_dir.parent / "outside-copy.json"
    os.link(data_dir / "tool-policy-approvals.json", outside)
    _assert_floor(_run(tools["file_read"], {"path": str(outside)}))


# --- TOCTOU: the read re-checks after opening -------------------------------------------


def test_a_parent_swapped_for_a_symlink_before_the_open_is_refused(tools, data_dir, monkeypatch):
    workspace = data_dir / "workspace"
    _touch(workspace / "sub" / "tool-config.json", "harmless")
    real_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if str(path).endswith("sub/tool-config.json") and (workspace / "sub").is_dir():
            (workspace / "sub").rename(workspace / "sub-old")
            (workspace / "sub").symlink_to(data_dir, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_tools.os, "open", swap_then_open)
    _assert_floor(_run(tools["file_read"], {"path": "sub/tool-config.json"}))


def test_another_inode_after_the_check_is_refused(tools, data_dir, monkeypatch):
    _touch(data_dir / "workspace" / "plain.txt", "harmless")
    real_open = os.open

    def open_other(path, flags, *args, **kwargs):
        if str(path).endswith("plain.txt"):
            path = str(data_dir / "tool-config.json")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_tools.os, "open", open_other)
    _assert_floor(_run(tools["file_read"], {"path": "plain.txt"}))


def test_read_floor_context_is_checked_inside_the_tool(data_dir):
    # The decorated tool itself refuses while a floor is active (not only the wrapper).
    policy, _ = build_policy(AgentConfig(), data_dir)
    with read_floor(policy), pytest.raises(file_tools.FileReadError) as caught:
        file_tools._read_checked(str(data_dir / "tool-config.json"), 1024)
    assert caught.value.reason == "floor"
    with read_floor(policy):
        assert file_tools._read_bounded(str(data_dir / "workspace" / "notes.md"), 64)


def test_a_nested_wrapper_without_a_floor_never_lifts_the_outer_one(data_dir):
    # The registry's editor is wrapped twice (module default + server policy): the inner
    # policy has no data directory and must not clear the outer read floor.
    from stiracore.identity_paths import WritePathPolicy

    policy, _ = build_policy(AgentConfig(), data_dir)
    floors = (read_floor(policy), read_floor(WritePathPolicy()))
    with floors[0], floors[1], pytest.raises(file_tools.FileReadError) as caught:
        file_tools._read_checked(str(data_dir / "neural-state.json"), 1024)
    assert caught.value.reason == "floor"
    assert file_tools.active_read_floor() is None


# --- the other read tools ----------------------------------------------------------------


def test_view_image_and_image_reader_follow_the_read_floor(tools, data_dir):
    _png(data_dir / "shot.png")
    _png(data_dir / "workspace" / "ok.png")
    _assert_floor(_run(tools["view_image"], {"path": "../shot.png"}))
    assert _run(tools["view_image"], {"path": "ok.png"})["status"] == "success"
    reader = tools["image_reader"]
    refused = _run(reader, {"image_path": "../shot.png"})
    assert refused["status"] == "error" and READ_FLOOR_ERROR in refused["content"][0]["text"]
    assert not any("image" in block for block in refused["content"])
    assert _run(reader, {"image_path": "ok.png"})["status"] == "success"


def test_image_reader_reopen_is_checked(tools, data_dir, monkeypatch):
    # The upstream module opens the path by name: Stira's opener re-checks after opening.
    import strands_tools.image_reader as upstream

    _png(data_dir / "secret.png")
    _png(data_dir / "workspace" / "ok.png")
    real_exists = upstream.os.path.exists

    def swap(path):
        result = real_exists(path)
        if str(path).endswith("workspace/ok.png"):
            os.replace(data_dir / "workspace" / "ok.png", data_dir / "workspace" / "ok-old.png")
            (data_dir / "workspace" / "ok.png").symlink_to(data_dir / "secret.png")
        return result

    monkeypatch.setattr(upstream.os.path, "exists", swap)
    result = _run(tools["image_reader"], {"image_path": "ok.png"})
    assert result["status"] == "error", result


def test_editor_view_follows_the_read_floor(tools, data_dir):
    editor = tools["editor"]
    for path in ("../tool-config.json", str(data_dir / "neural-state.json"), str(data_dir), ".."):
        result = _run(editor, {"command": "view", "path": path})
        assert result["status"] == "error", (path, result)
        assert SECRET not in json.dumps(result, default=str)
        assert READ_FLOOR_ERROR in result["content"][0]["text"]
    (data_dir / "workspace" / "all").symlink_to(data_dir, target_is_directory=True)
    linked = _run(editor, {"command": "view", "path": "all/tool-config.json"})
    assert linked["status"] == "error"
    ok = _run(editor, {"command": "view", "path": "notes.md"})
    assert ok["status"] == "success" and "readable" in json.dumps(ok)
    listing = _run(editor, {"command": "view", "path": "."})
    assert listing["status"] == "success", listing
    # The listing walks into the symlinked directory but shows no data-directory name.
    shown = json.dumps(listing, ensure_ascii=False)
    assert "notes.md" in shown
    for name in ("tool-config.json", "neural-state.json", "checkpoints", "turns.db"):
        assert name not in shown, name
    assert _run(editor, {"command": "view", "path": "all"})["status"] == "error"


def test_the_editor_sandbox_rechecks_reads(data_dir):
    from stiracore.tools.file_editor_tool import StiraWriteSandbox

    policy, _ = build_policy(AgentConfig(), data_dir)
    sandbox = StiraWriteSandbox()
    with read_floor(policy):
        with pytest.raises(PermissionError, match="read floor"):
            asyncio.run(sandbox.read_file(str(data_dir / "tool-config.json")))
        # The editor probes a path by listing its parent: only readable names are listed.
        names = [entry.name for entry in asyncio.run(sandbox.list_files(str(data_dir)))]
        assert names == ["context-stash", "workspace"]
        # The semantic/procedural layers next to the episodic one are not listed.
        layers = [e.name for e in asyncio.run(sandbox.list_files(str(data_dir / MEMORY)))]
        assert layers == ["episodic"]
        assert asyncio.run(sandbox.read_file(str(data_dir / "workspace" / "notes.md")))


@pytest.mark.parametrize("name", ["lsp_diagnostics", "lsp_hover"])
def test_lsp_tools_follow_the_read_floor(tools, data_dir, name):
    _touch(data_dir / "hook.py", f"X = '{SECRET}'\n")
    arguments = {"file_path": str(data_dir / "hook.py")}
    if name == "lsp_hover":
        arguments.update(line=1, col=1)
    result = _run(tools[name], arguments)
    assert result["status"] == "error", result
    assert SECRET not in json.dumps(result, default=str)


# --- server wiring, settings page ---------------------------------------------------------


def test_the_server_wires_the_read_floor(tmp_path, monkeypatch):
    from tests.test_data_dir_floor import _stira_app

    captured, data_dir = _stira_app(tmp_path, monkeypatch)
    _touch(data_dir / "tool-policy-approvals.json")
    from dashboard import stira_server

    tools = {
        t.tool_name: t
        for t in stira_server._stira_tools((), set(), write_policy=captured["write_policy"])
    }
    _assert_floor(_run(tools["file_read"], {"path": "../tool-policy-approvals.json"}))
    _touch(data_dir / "workspace" / "mine.md", "mine")
    assert _run(tools["file_read"], {"path": "mine.md"})["content"][0]["text"] == "mine"


def test_the_settings_page_lists_where_file_tools_can_read(data_dir):
    policy, floor = build_policy(AgentConfig(), data_dir)
    assert shown_readable(policy, floor) == [
        "workspace/",
        "memory/agents/stira/memory/episodic/",
        "context-stash/",
    ]
    assert readable_names() == tuple(n.rstrip("/") for n in shown_readable(policy, floor))


def test_the_settings_floor_item_shows_readable_next_to_writable():
    from stiracore.tools import tool_policy

    items = tool_policy.floor_items(
        data_directory={"writable": ["workspace/"], "readable": ["workspace/", "context-stash/"]}
    )
    item = next(i for i in items if i["id"] == "data_directory")
    assert item == {
        "id": "data_directory",
        "writable": ["workspace/"],
        "readable": ["workspace/", "context-stash/"],
        "approvable": False,
    }


def test_the_settings_hub_api_shows_where_file_tools_can_read(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from dashboard import stira_server
    from stiracore.core import StiraEngine
    from stiracore.observability.config_api import ConfigStore

    class _Agent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)

    data_dir = tmp_path / "stira"
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    app = stira_server.create_app(store)
    floor = app.test_client().get("/api/config/hub").get_json()["floor"]
    item = next(i for i in floor if i["id"] == "data_directory")
    assert item["readable"] == [
        "workspace/",
        "memory/agents/stira/memory/episodic/",
        "context-stash/",
    ]
    assert item["writable"] == item["readable"]


# --- docs: the "Agent can read" column of the data-directory table is the real policy ------

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _read_rows(path: Path) -> list[tuple[str, str]]:
    import re

    text = path.read_text(encoding="utf-8")
    block = text.split("<!-- data-files:begin -->", 1)[1].split("<!-- data-files:end -->", 1)[0]
    lines = [line for line in block.splitlines() if line.startswith("|")]
    rows = []
    for line in lines[2:]:  # header and separator
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append((re.findall(r"`([^`]+)`", cells[0])[0], cells[4]))
    return rows


@pytest.mark.parametrize(
    ("name", "yes", "no"),
    [("configuration.md", "yes", "no"), ("configuration_ja.md", "可", "不可")],
)
def test_the_documented_read_column_matches_the_policy(data_dir, name, yes, no):
    policy, _ = build_policy(AgentConfig(), data_dir)
    rows = _read_rows(DOCS / name)
    assert len(rows) >= 20
    for entry, readable in rows:
        relative = entry.replace("<agent>", "stira")
        target = data_dir / relative / "x.md" if relative.endswith("/") else data_dir / relative
        assert readable == yes or readable.startswith(no), (name, entry, readable)
        assert policy.check_read(str(target)).allowed == (readable == yes), (name, entry)
    assert policy.check_read(str(data_dir / MEMORY / "episodic" / "x.md")).allowed


# --- review round 1 (#316): identity of the opened descriptor, fd-based listing -----------


def _gif(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format="GIF")
    path.write_bytes(buffer.getvalue())
    return path


def test_the_recheck_cannot_be_raced_by_swapping_the_parent_back_and_forth(
    tools, data_dir, monkeypatch
):
    """Astra's sequence: swap before the open, restore for the name check, swap for stat."""
    from stiracore.identity_paths import WritePathPolicy

    workspace = data_dir / "workspace"
    _touch(workspace / "sub" / "tool-config.json", "harmless")
    sub, saved = workspace / "sub", workspace / "sub-saved"
    resolved = str(sub / "tool-config.json")
    state = {"opened": False, "checked": False}

    def to_link():
        if sub.is_dir() and not sub.is_symlink():
            sub.rename(saved)
            sub.symlink_to(data_dir, target_is_directory=True)

    def back():
        if sub.is_symlink():
            sub.unlink()
            saved.rename(sub)

    real_open, real_stat, real_check = os.open, os.stat, WritePathPolicy.check_read

    def racing_open(path, flags, *args, **kwargs):
        if str(path) == resolved and not state["opened"]:
            to_link()
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                state["opened"] = True
                back()
        return real_open(path, flags, *args, **kwargs)

    def racing_check(self, path):
        decision = real_check(self, path)
        if state["opened"] and str(path) == resolved:
            state["checked"] = True
        return decision

    def racing_stat(path, *args, **kwargs):
        if state["checked"] and str(path) == resolved:
            to_link()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(file_tools.os, "open", racing_open)
    monkeypatch.setattr(WritePathPolicy, "check_read", racing_check)
    monkeypatch.setattr(file_tools.os, "stat", racing_stat)
    result = _run(tools["file_read"], {"path": "sub/tool-config.json"})
    monkeypatch.undo()
    assert state["opened"]
    _assert_floor(result)


def test_floor_identities_cover_protected_files_only(data_dir):
    # The descriptor's (st_dev, st_ino) is compared with every protected file's.
    policy, _ = build_policy(AgentConfig(), data_dir)
    files, _dirs = policy.floor_identities()
    secret = os.stat(data_dir / "neural-state.json")
    assert (secret.st_dev, secret.st_ino) in files
    readable = os.stat(data_dir / "workspace" / "notes.md")
    assert (readable.st_dev, readable.st_ino) not in files


def test_a_failed_identity_walk_refuses_the_read(tools, data_dir, monkeypatch):
    def broken(*args, **kwargs):
        raise PermissionError("walk failed")

    monkeypatch.setattr(os, "scandir", broken)
    _assert_floor(_run(tools["file_read"], {"path": "notes.md"}))


def test_an_exception_symlinked_outside_is_not_readable_through_its_name(tmp_path, monkeypatch):
    root = (tmp_path / "stira").resolve()
    root.mkdir()
    outside = tmp_path / "elsewhere"
    _touch(outside / "x.md", "outside")
    (root / "workspace").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    policy, _ = build_policy(AgentConfig(), root)
    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}
    _assert_floor_text = _run(tools["file_read"], {"path": str(root / "workspace" / "x.md")})
    assert _assert_floor_text["status"] == "error"
    assert _diagnosis(_assert_floor_text)["reason"] == "floor"
    # The same file by its own (outside) name: outside the read roots (#457) ...
    refused = _run(tools["file_read"], {"path": str(outside / "x.md")})
    assert _diagnosis(refused)["reason"] == "outside_read_roots"
    # ... and readable once its directory is a read root.
    widened, _ = build_policy(AgentConfig(read_roots=["@workspace", str(outside)]), root)
    reader = {t.tool_name: t for t in tool_registry.get_tools(write_policy=widened)}
    assert _run(reader["file_read"], {"path": str(outside / "x.md")})["status"] == "success"
    from stiracore.tools.file_editor_tool import StiraWriteSandbox

    with read_floor(policy), pytest.raises(PermissionError):
        asyncio.run(StiraWriteSandbox().list_files(str(root / "workspace")))


def test_listing_opens_the_directory_and_checks_its_identity(data_dir, monkeypatch):
    from stiracore.tools.file_editor_tool import StiraWriteSandbox

    policy, _ = build_policy(AgentConfig(), data_dir)
    workspace = data_dir / "workspace"
    _touch(workspace / "sub" / "checkpoints" / "harmless.md", "harmless")
    sub, saved = workspace / "sub", workspace / "sub-saved"
    real_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if str(path).endswith("sub/checkpoints") and not sub.is_symlink():
            sub.rename(saved)
            sub.symlink_to(data_dir, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with read_floor(policy):
        try:
            names = [
                e.name
                for e in asyncio.run(StiraWriteSandbox().list_files(str(sub / "checkpoints")))
            ]
        except PermissionError:
            names = []
    assert "0123456789abcdef0123456789abcdef.json" not in names
    assert names in ([], ["harmless.md"])


def test_listing_never_names_protected_entries_and_follows_no_symlink(data_dir):
    from stiracore.tools.file_editor_tool import StiraWriteSandbox

    policy, _ = build_policy(AgentConfig(), data_dir)
    sandbox = StiraWriteSandbox()
    (data_dir / "workspace" / "all").symlink_to(data_dir, target_is_directory=True)
    with read_floor(policy):
        names = [e.name for e in asyncio.run(sandbox.list_files(str(data_dir)))]
        assert names == ["context-stash", "workspace"]
        assert [
            e.name for e in asyncio.run(sandbox.list_files(str(data_dir / "checkpoints")))
        ] == []
        listed = asyncio.run(sandbox.list_files(str(data_dir / "workspace")))
        link = next(e for e in listed if e.name == "all")
        assert not link.is_dir  # a symlink is never followed (and never walked)
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.list_files(str(data_dir / "workspace" / "all")))


def test_image_reader_format_detection_is_checked_too(tools, data_dir, monkeypatch):
    # PIL's format detection decodes the bytes of the checked read (#315): there is no
    # second open, so a swap after the read shows neither the protected bytes nor its
    # format, and the format always belongs to the returned bytes. (Before #315 the
    # second open was checked too and refused the swapped path: status error.)
    import strands_tools.image_reader as upstream

    _gif(data_dir / "secret.gif")
    original = _png(data_dir / "workspace" / "ok.png").read_bytes()
    target = data_dir / "workspace" / "ok.png"
    real_open = file_tools._floor_checked_open
    calls = {"n": 0}

    def swap_after_first(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            os.replace(target, data_dir / "workspace" / "ok-old.png")
            target.symlink_to(data_dir / "secret.gif")
        return handle

    monkeypatch.setattr(upstream, "open", swap_after_first)
    monkeypatch.setattr(file_tools, "_floor_checked_open", swap_after_first)
    result = _run(tools["image_reader"], {"image_path": "ok.png"})
    rendered = json.dumps(result, default=str).lower()
    assert "gif" not in rendered.replace("secret.gif", "")
    assert calls["n"] == 1  # the name was opened once, by the checked opener
    assert result["status"] == "success", result
    image = result["content"][0]["image"]
    assert (image["format"], image["source"]["bytes"]) == ("png", original)


def test_lsp_output_is_discarded_when_the_file_changed_during_analysis(
    tools, data_dir, monkeypatch
):
    import subprocess

    from stiracore.tools import lsp_tools

    real_executable = lsp_tools._executable
    # Ruff only (the hook swaps once, before the first analyzer starts).
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: None if "pyright" in name else real_executable(name)
    )
    secret = "VERY_PRIVATE_IDENTIFIER"
    _touch(data_dir / "hook.py", f"{secret} = undefined_name\n")
    target = _touch(data_dir / "workspace" / "ok.py", "x = 1\n")
    real_popen = subprocess.Popen

    # Just before the analyzer starts (Popen since the bounded runner, #315 review).
    def swap_then_run(args, *a, **kw):
        if kw.get("process_group") != 0 and target.exists() and not target.is_symlink():
            os.replace(target, data_dir / "workspace" / "ok-old.py")
            target.symlink_to(data_dir / "hook.py")
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(lsp_tools.subprocess, "Popen", swap_then_run)
    result = _run(tools["lsp_diagnostics"], {"file_path": str(target)})
    rendered = json.dumps(result, default=str)
    assert secret not in rendered and "undefined_name" not in rendered
    # The decorated tool wraps the analyzer's dict; its own status says it was discarded.
    assert lsp_tools._CHANGED in rendered


# --- #315: no tool opens a checked name a second time ------------------------------------


def _swap_around(monkeypatch, owner, name, target: Path, protected: Path) -> dict:
    """Around ``owner.name`` (the analyzer start), point ``target`` at ``protected`` and
    undo it before the post-run check: the window #316's check could not see."""
    real = getattr(owner, name)
    saved = target.with_name(target.name + ".saved")
    seen = {"swapped": 0}

    def swapped(*args, **kwargs):
        if kwargs.get("process_group") == 0:
            return real(*args, **kwargs)  # the group's sentinel, not the analyzer
        os.replace(target, saved)
        target.symlink_to(protected)
        seen["swapped"] += 1
        try:
            return real(*args, **kwargs)
        finally:
            target.unlink()
            os.replace(saved, target)

    monkeypatch.setattr(owner, name, swapped)
    return seen


def test_ruff_reads_the_checked_bytes_even_when_a_swap_is_undone(tools, data_dir, monkeypatch):
    from stiracore.tools import lsp_tools

    real_executable = lsp_tools._executable
    if real_executable("ruff") is None:
        pytest.skip("optional ruff is not installed")
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: None if "pyright" in name else real_executable(name)
    )
    _touch(data_dir / "hook.py", "VERY_PRIVATE_IDENTIFIER = undefined_private_name\n")
    target = _touch(data_dir / "workspace" / "ok.py", "checked = checked_only_name\n")
    seen = _swap_around(monkeypatch, lsp_tools.subprocess, "Popen", target, data_dir / "hook.py")
    result = _run(tools["lsp_diagnostics"], {"file_path": str(target)})
    rendered = json.dumps(result, default=str)
    assert seen["swapped"] == 1
    assert "PRIVATE" not in rendered and "undefined_private_name" not in rendered
    # The diagnostics are about the checked bytes (Ruff's F821 for the checked name).
    assert "checked_only_name" in rendered, rendered


@pytest.mark.skipif(os.name == "nt", reason="a shell launcher")
def test_pyright_gets_the_checked_text_even_when_a_swap_is_undone(
    tools, data_dir, tmp_path, monkeypatch
):
    from stiracore.tools import lsp_tools
    from tests.test_lsp_tools import _fake_langserver

    server = _fake_langserver(tmp_path)
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: server if "pyright" in name else None
    )
    _touch(data_dir / "hook.py", "VERY_PRIVATE_IDENTIFIER = 1\n")
    target = _touch(data_dir / "workspace" / "ok.py", "checked = 1\n")
    seen = _swap_around(monkeypatch, lsp_tools.subprocess, "Popen", target, data_dir / "hook.py")
    result = _run(tools["lsp_diagnostics"], {"file_path": str(target)})
    rendered = json.dumps(result, default=str)
    assert seen["swapped"] == 1
    assert "PRIVATE" not in rendered
    assert "seen: checked = 1" in rendered, rendered


def test_lsp_hover_reads_the_checked_text_only(tools, data_dir, monkeypatch):
    import sys
    from types import SimpleNamespace

    seen = {}

    def script(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(infer=lambda line, column: [])

    monkeypatch.setitem(sys.modules, "jedi", SimpleNamespace(Script=script))
    target = _touch(data_dir / "workspace" / "ok.py", "checked = 1\n")
    result = _run(tools["lsp_hover"], {"file_path": str(target), "line": 1, "col": 1})
    assert result["status"] == "success", result
    assert seen == {"code": "checked = 1\n", "path": str(target)}  # code=, not a reopen


def test_lsp_cannot_reveal_a_protected_file_imported_through_a_workspace_symlink(
    tools, data_dir, monkeypatch
):
    # #315 review residual (4): Pyright resolves imports by name. A workspace module that
    # is a symlink to a protected JSON file parses as an expression with no names, so
    # type inference (reveal_type) has nothing of it to show.
    from stiracore.tools import lsp_tools

    server = lsp_tools._executable("pyright-langserver")
    if server is None:
        pytest.skip("optional pyright is not installed")
    (data_dir / "tool-config.json").write_text(
        json.dumps({"rules": [SECRET], "key": SECRET}), encoding="utf-8"
    )
    (data_dir / "workspace" / "evil.py").symlink_to(data_dir / "tool-config.json")
    target = _touch(
        data_dir / "workspace" / "probe.py",
        "import evil\nreveal_type(evil)\nreveal_type(evil.rules)\nreveal_type(evil.__dict__)\n",
    )
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: server if "pyright" in name else None
    )
    result = _run(tools["lsp_diagnostics"], {"file_path": str(target)})
    rendered = json.dumps(result, default=str)
    assert "pyright" in rendered, rendered
    assert SECRET not in rendered


def test_image_reader_does_not_say_whether_a_swapped_in_protected_file_exists(
    tools, data_dir, monkeypatch
):
    # ``os.path.exists`` by name ran after the wrapper's check: with a parent swapped to
    # the data directory it told an existing protected file from a missing one (#315).
    import strands_tools.image_reader as upstream

    sub = data_dir / "workspace" / "sub"
    sub.mkdir()
    real_expanduser = upstream.expanduser

    def swap_then_expand(path):
        if not sub.is_symlink():
            sub.rename(data_dir / "workspace" / "sub-saved")
            sub.symlink_to(data_dir, target_is_directory=True)
        return real_expanduser(path)

    monkeypatch.setattr(upstream, "expanduser", swap_then_expand)
    answers = []
    for name in ("tool-config.json", "no-such-file.json"):
        result = _run(tools["image_reader"], {"image_path": f"sub/{name}"})
        assert result["status"] == "error", result
        answers.append(result["content"][0]["text"].replace(name, "<name>"))
        sub.unlink()
        (data_dir / "workspace" / "sub-saved").rename(sub)
    assert answers[0] == answers[1], answers
    assert READ_FLOOR_ERROR in answers[0]


def test_image_reader_is_refused_when_its_os_was_replaced_by_someone_else():
    import types

    module = types.ModuleType("fake_upstream")
    module.os = types.SimpleNamespace(path=types.SimpleNamespace(exists=lambda p: True))
    module.Image = Image
    assert file_tools.install_read_floor_opener(module) is False
    assert not file_tools.read_floor_opener_live(module)
