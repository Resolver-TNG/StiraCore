"""#271: the whole data directory is on the agent's write floor.

The stores' side files (head anchors, SQLite ``-wal``/``-shm``), logs, locks,
checkpoints, the semantic layer and its index were writable by ``file_write`` /
``editor`` after an approval, because the default ``@workspace`` *was* the data
directory and only the policy files were on the floor (#190). Now ``@workspace``
defaults to ``<data dir>/workspace/`` and the data directory is a floor tree
whose only writable parts are the workspace, the episodic layer and the context
stash (#258), as before.
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig, StiraEngine
from stiracore.identity_paths import WritePathPolicy
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.observability.config_api import ConfigStore
from stiracore.tools import tool_registry
from tests.provider_fakes import FakeModel, text_turn, tool_turn

MEMORY = Path("memory") / "agents" / "stira" / "memory"

# Every data-directory file that is not the agent's to write (the #271 report
# plus the files that were already on the floor).
FLOOR_FILES = [
    "hitl-approvals.db",
    "hitl-approvals.db-wal",
    "hitl-approvals.db-shm",
    "hitl-approvals.db.head",
    "hitl-operations.db",
    "hitl-operations.db-wal",
    "hitl-operations.db.head",
    "events.db",
    "events.db-wal",
    "events.db-shm",
    "events.db.head",
    "scheduler-events.jsonl",
    "scheduler-events.jsonl.head",
    "scheduler.lock",
    "heartbeat.lock",
    "turns.db",
    "turns.db-wal",
    "turns.db-shm",
    "checkpoints/0123456789abcdef0123456789abcdef.json",
    "checkpoints/new-thread/x.json",
    "tool-config.json",
    "tool-config.json.head",
    "tool-config.json.locked",
    "settings-history.jsonl",
    "tool-policy-approvals.json",
    "tool-policy-approvals.json.tmp",
    "neural-state.json",
    "gateway-config.json",
    "agent-config.json",
    "stira_server.pid",
    "a-new-file-at-the-top.md",
    "a-new-directory/x.md",
    str(MEMORY / "semantic" / "facts.md"),
    str(MEMORY / "semantic" / "staging" / "x.md"),
    str(MEMORY / "procedural" / "rule.md"),
    str(MEMORY / "index.sqlite3"),
    str(MEMORY / "index.sqlite3-wal"),
    str(MEMORY / "new-layer" / "x.md"),
    "memory/agents/other-agent/memory/episodic/x.md",
]

WRITABLE = [
    "workspace/notes.md",
    "workspace/new/deeper/x.md",
    str(MEMORY / "episodic" / "2026-10-15.md"),
    str(MEMORY / "episodic" / "new-folder" / "x.md"),
    "context-stash/stira/offloader/tu-1_0",
]


def _stira_app(tmp_path, monkeypatch, agent_config=None):
    from dashboard import stira_server

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(kwargs)

    data_dir = tmp_path / "stira"
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    if agent_config is not None:
        monkeypatch.setattr(
            stira_server.AgentConfig,
            "load",
            classmethod(lambda cls, *_a, **_k: cls(path=tmp_path / "a.json", **agent_config)),
        )
    stira_server.create_app(store)
    return captured, data_dir


@pytest.fixture
def stira(tmp_path, monkeypatch):
    captured, data_dir = _stira_app(tmp_path, monkeypatch)
    return captured, data_dir, captured["write_policy"]


def _touch(path: Path, text: str = "original") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the report: side files, logs, locks, checkpoints, semantic layer, index -------


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_data_directory_files_are_on_the_floor(stira, existing):
    _, data_dir, policy = stira
    for name in FLOOR_FILES:
        target = data_dir / name
        if existing and not target.exists():
            _touch(target)
        decision = policy.check(str(target))
        assert not decision.allowed and decision.code == "floor", name


def test_the_workspace_episodic_layer_and_context_stash_stay_writable(stira):
    _, data_dir, policy = stira
    assert policy.workspace == (data_dir / "workspace").resolve()
    for name in WRITABLE:
        decision = policy.check(str(data_dir / name))
        assert decision.allowed, (name, decision)
    # Relative paths land in the workspace, not the data directory itself.
    assert policy.check("notes.md").path == str((data_dir / "workspace" / "notes.md").resolve())
    # The context stash still passes the check context management makes (#258).
    stash = data_dir / "context-stash" / "stira"
    assert policy.within_roots(stash) and not policy.is_read_only(stash)
    # The data-directory roots are added only for the default workspace.
    assert policy.write_roots == (
        (data_dir / "workspace").resolve(),
        (data_dir / MEMORY / "episodic").resolve(),
        (data_dir / "context-stash").resolve(),
    )


def test_no_approval_lifts_the_data_directory_floor(stira):
    captured, data_dir, policy = stira
    guard = IdentityGuard(
        StiraEngine(), read_only_paths=captured["read_only_paths"], path_policy=policy
    )
    for name in FLOOR_FILES:
        target = str(data_dir / name)
        for tool, args in (
            ("file_write", {"path": target, "content": "x"}),
            ("editor", {"command": "create", "path": target, "file_text": "x"}),
            ("editor", {"command": "str_replace", "path": target, "old_str": "a", "new_str": "b"}),
        ):
            result = guard.check_tool_call(tool, args, hitl_approved=True)
            assert not result.allowed and result.layer == 3, (tool, name)
    # The memory tool's content is not a path into the floor.
    ok = guard.check_tool_call(
        "memory_write", {"layer": "episodic", "key": "x", "content": "hello world"}
    )
    assert ok.allowed


def test_an_approved_file_write_cannot_rewrite_a_head_anchor(stira):
    captured, data_dir, policy = stira
    head = _touch(data_dir / "events.db.head", '{"seq": 7}')
    tools = tool_registry.get_tools(
        "normal", read_only_paths=captured["read_only_paths"], write_policy=policy
    )
    file_write = next(t for t in tools if getattr(t, "tool_name", None) == "file_write")
    hitl = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    agent = NXPAgent(
        model=FakeModel(
            [tool_turn("file_write", {"path": str(head), "content": "{}"}), text_turn("done")]
        ),
        state=NeuralState(stability=0.9),
        config=StiraConfig(critical_action="halt"),
        tools=[file_write],
        on_hitl_request=hitl,
        read_only_paths=captured["read_only_paths"],
        write_policy=policy,
        stimulus_classifier=Mock(classify=lambda **kw: None),
        tool_policy=lambda name: ("ask", 1),
    )
    agent("go")
    assert head.read_text(encoding="utf-8") == '{"seq": 7}'
    assert not hitl.called  # the floor refuses before asking: no approval could lift it


def test_the_execution_boundary_rechecks_without_hooks(stira):
    """The wrapped tool itself refuses (inner agents run without NXP hooks)."""
    captured, data_dir, policy = stira
    target = _touch(data_dir / "checkpoints" / "abc.json", "{}")
    tools = tool_registry.get_tools(
        "normal", read_only_paths=captured["read_only_paths"], write_policy=policy
    )
    file_write = next(t for t in tools if getattr(t, "tool_name", None) == "file_write")

    async def run():
        events = []
        tool_use = {
            "toolUseId": "t1",
            "name": "file_write",
            "input": {"path": str(target), "content": '{"engine_state": {}}'},
        }
        async for event in file_write.stream(tool_use, {}):
            events.append(event)
        return events

    events = asyncio.run(run())
    assert target.read_text(encoding="utf-8") == "{}"
    assert "error" in repr(events[-1]).lower()


# --- attacker spellings -------------------------------------------------------------


def test_relative_and_dotdot_spellings_stay_on_the_floor(stira):
    _, data_dir, policy = stira
    for path in (
        "../events.db",
        "../checkpoints/x.json",
        "./../hitl-approvals.db.head",
        "notes/../../turns.db",
        str(data_dir / "workspace" / ".." / "events.db"),
        str(data_dir / "context-stash" / ".." / "scheduler-events.jsonl"),
        str(data_dir / MEMORY / "episodic" / ".." / "semantic" / "x.md"),
    ):
        assert not policy.check(path).allowed, path


def test_case_and_unicode_variants_stay_on_the_floor(tmp_path, monkeypatch):
    # A non-ASCII data directory, spelled NFD: the floor key folds it (deny side).
    root = tmp_path / unicodedata.normalize("NFC", "café")
    captured, data_dir = _stira_app(root, monkeypatch)
    policy = captured["write_policy"]
    _touch(data_dir / "events.db")
    nfd = unicodedata.normalize("NFD", str(data_dir))
    for path in (
        str(data_dir / "EVENTS.DB"),
        str(data_dir / "Events.db.HEAD"),
        str(data_dir / "Checkpoints" / "x.json"),
        nfd + "/events.db",
        nfd + "/checkpoints/x.json",
        str(data_dir / "WORKSPACE" / ".." / "events.db"),
        # The exception is matched exactly: a case variant of it is not writable.
        str(data_dir / "Workspace" / "x.md"),
        str(data_dir / "memory" / "agents" / "stira" / "memory" / "EPISODIC" / "x.md"),
    ):
        assert not policy.check(path).allowed, path


def test_a_symlink_in_the_workspace_to_a_store_file_is_on_the_floor(stira):
    _, data_dir, policy = stira
    _touch(data_dir / "events.db")
    (data_dir / "checkpoints").mkdir(exist_ok=True)
    (data_dir / "workspace" / "link").symlink_to(data_dir / "events.db")
    (data_dir / "workspace" / "dirlink").symlink_to(data_dir / "checkpoints")
    (data_dir / "workspace" / "up").symlink_to(data_dir)
    for name in ("link", "dirlink/x.json", "up/turns.db", "up/workspace/../events.db"):
        assert not policy.check(str(data_dir / "workspace" / name)).allowed, name


def test_a_hard_link_to_a_store_file_is_on_the_floor(stira, tmp_path):
    _, data_dir, policy = stira
    head = _touch(data_dir / "hitl-approvals.db.head")
    inside = data_dir / "workspace" / "copy.head"
    os.link(head, inside)
    assert not policy.check(str(inside)).allowed
    # Also from another write root outside the data directory.
    other = tmp_path / "elsewhere"
    other.mkdir()
    linked = other / "head"
    os.link(head, linked)
    wide = policy._with(write_roots=(*policy.write_roots, other))
    assert wide.check(str(other / "plain.md")).allowed
    assert not wide.check(str(linked)).allowed


def test_a_symlinked_exception_is_ignored(stira):
    """Replacing workspace/ by a link to the data directory opens nothing (fail closed)."""
    _, data_dir, policy = stira
    workspace = data_dir / "workspace"
    workspace.rmdir()
    workspace.symlink_to(data_dir)
    for name in ("x.md", "events.db", "checkpoints/x.json"):
        assert not policy.check(str(workspace / name)).allowed, name


def test_the_data_directory_and_its_exceptions_cannot_be_replaced(stira):
    _, data_dir, policy = stira
    for target in (
        data_dir,
        data_dir.parent,
        data_dir / "workspace",
        data_dir / "context-stash",
        data_dir / MEMORY / "episodic",
        data_dir / "memory",
        data_dir / "checkpoints",
    ):
        assert not policy.check(str(target)).allowed, target
        assert policy.is_read_only(target), target


def test_delegates_and_extra_floors_keep_the_data_directory_floor(stira):
    _, data_dir, policy = stira
    target = str(data_dir / "events.db.head")
    assert policy.narrow(None).check(target).code == "floor"
    assert policy.narrow(["@workspace"]).check(target).code == "floor"
    assert policy.with_floor(["x"]).check(target).code == "floor"
    assert policy.with_floor(["x"]).check(str(data_dir / "workspace" / "y.md")).allowed


# --- configuration ----------------------------------------------------------------


def test_an_explicit_workspace_adds_no_data_directory_root(tmp_path, monkeypatch):
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    captured, data_dir = _stira_app(
        tmp_path,
        monkeypatch,
        {"workspace_dir": str(elsewhere), "write_roots": ["@workspace"]},
    )
    policy = captured["write_policy"]
    assert policy.write_roots == (elsewhere.resolve(),)
    assert policy.check("notes.md").allowed
    assert policy.check(str(data_dir / MEMORY / "episodic" / "x.md")).code == (
        "outside_write_roots"
    )


def test_a_root_containing_the_data_directory_does_not_lift_the_floor(tmp_path, monkeypatch):
    """Even a human-configured root over the data directory leaves its files on the floor."""
    captured, data_dir = _stira_app(
        tmp_path,
        monkeypatch,
        {"workspace_dir": str(tmp_path), "write_roots": ["@workspace"]},
    )
    policy = captured["write_policy"]
    assert policy.check(str(tmp_path / "notes.md")).allowed
    for name in ("events.db.head", "checkpoints/x.json", "turns.db", "new.md"):
        decision = policy.check(str(data_dir / name))
        assert not decision.allowed, name
    # The data directory's own workspace stays inside the floor exception.
    assert policy.check(str(data_dir / "workspace" / "x.md")).code != "floor"


def test_floor_tree_writable_must_be_plain_relative_paths(tmp_path):
    for bad in ("", "  ", "/abs", "../up", "a/../b"):
        with pytest.raises(ValueError):
            WritePathPolicy(base_dir=tmp_path, floor_tree=tmp_path, floor_tree_writable=[bad])


def test_existing_workspace_contents_are_kept(tmp_path, monkeypatch):
    """An existing install's files are not moved or deleted, only no longer agent-writable."""
    data_dir = tmp_path / "stira"
    old = _touch(data_dir / "notes" / "todo.md", "keep me")
    captured, _ = _stira_app(tmp_path, monkeypatch)
    policy = captured["write_policy"]
    assert old.read_text(encoding="utf-8") == "keep me"
    assert policy.check(str(old)).code == "floor"
    assert policy.check("notes/todo.md").path == str(
        (data_dir / "workspace" / "notes" / "todo.md").resolve()
    )


# --- docs: the "Floor" column of the data-directory table is the real policy -------

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _floor_rows(path: Path) -> list[tuple[str, str]]:
    import re

    text = path.read_text(encoding="utf-8")
    block = text.split("<!-- data-files:begin -->", 1)[1].split("<!-- data-files:end -->", 1)[0]
    rows = []
    lines = [line for line in block.splitlines() if line.startswith("|")]
    for line in lines[2:]:  # header and separator
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4:
            rows.append((re.findall(r"`([^`]+)`", cells[0])[0], cells[3]))
    return rows


@pytest.mark.parametrize(
    ("name", "yes", "no"),
    [("configuration.md", "yes", "no"), ("configuration_ja.md", "あり", "なし")],
)
def test_the_documented_floor_column_matches_the_policy(stira, name, yes, no):
    _, data_dir, policy = stira
    rows = _floor_rows(DOCS / name)
    assert len(rows) >= 20
    for entry, floor in rows:
        relative = entry.replace("<agent>", "stira")
        target = data_dir / relative / "x.md" if relative.endswith("/") else data_dir / relative
        assert floor.startswith((yes, no)), (name, entry, floor)
        on_floor = policy.check(str(target)).code == "floor"
        assert on_floor == floor.startswith(yes), (name, entry)
    memory = data_dir / MEMORY
    assert policy.check(str(memory / "episodic" / "x.md")).allowed
    assert policy.check(str(memory / "semantic" / "x.md")).code == "floor"


def test_the_settings_page_lists_the_data_directory_floor(tmp_path):
    from flask import Flask

    from stiracore.observability.config_api import create_config_blueprint
    from stiracore.tools import tool_policy

    assert [i["id"] for i in tool_policy.floor_items()] == [
        "identity_read_only",
        "os_protection",
        "exec_exposure",
    ]
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    runtime = {
        "tools": None,
        "breaker": None,
        "classifier_choice": "auto",
        "error": None,
        "data_directory": {"writable": ["workspace/", "context-stash/"]},
    }
    app = Flask(__name__)
    app.register_blueprint(
        create_config_blueprint(store, lambda: "normal", runtime_settings_provider=lambda: runtime)
    )
    floor = app.test_client().get("/api/config/hub").get_json()["floor"]
    item = next(i for i in floor if i["id"] == "data_directory")
    assert item == {
        "id": "data_directory",
        "writable": ["workspace/", "context-stash/"],
        "approvable": False,
    }


def test_stira_doctor_cli_mirrors_the_server_floor(tmp_path, monkeypatch, capsys):
    import json

    from stiracore.diagnostics import __main__ as cli

    # Run from a repository whose default config is this data directory's.
    repo = tmp_path / "repo"
    captured, data_dir = _stira_app(repo / ".local", monkeypatch)
    from stiracore.config.data_dir import writable_names

    assert writable_names() == captured["write_policy"].floor_tree_writable
    monkeypatch.chdir(repo)
    capsys.readouterr()  # the server's start-up banner
    assert cli.main(["doctor", "--json", "--root", str(data_dir / "memory")]) in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert report["roots"][0]["findings"][0]["code"] == "floor"
    assert cli.main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["roots"][0]["root"] == "@workspace" and report["roots"][0]["status"] == "ok"
    assert report["workspace"].endswith("stira/workspace")
    assert list((data_dir / "workspace").iterdir()) == []  # the probe was removed


# --- review round 1 (#290): symlinks present before start-up, doctor parity, UI ------


def _guard(captured):
    return IdentityGuard(
        StiraEngine(),
        read_only_paths=captured["read_only_paths"],
        path_policy=captured["write_policy"],
    )


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("workspace", "checkpoints"),
        ("workspace", "."),
        ("context-stash", "checkpoints"),
        ("context-stash", "."),
        ("workspace", "memory"),
    ],
)
def test_a_symlink_there_before_start_up_never_becomes_an_exception(
    tmp_path, monkeypatch, capsys, name, target
):
    data_dir = tmp_path / "stira"
    (data_dir / "checkpoints").mkdir(parents=True)
    (data_dir / "memory").mkdir()
    head = _touch(data_dir / "events.db.head", "anchor")
    checkpoint = _touch(data_dir / "checkpoints" / "abc.json", "{}")
    (data_dir / name).symlink_to(data_dir if target == "." else data_dir / target)
    captured, _ = _stira_app(tmp_path, monkeypatch)
    policy = captured["write_policy"]
    assert name not in policy.floor_tree_writable
    assert "not writable for the agent" in capsys.readouterr().out
    guard = _guard(captured)
    for path in (
        data_dir / "checkpoints" / "new.json",
        checkpoint,
        head,
        data_dir / "new.md",
        data_dir / name / "x.md",
        data_dir / name / "abc.json",
        data_dir / MEMORY / "semantic" / "x.md",
    ):
        assert policy.check(str(path)).code == "floor", path
        for tool, args in (
            ("file_write", {"path": str(path), "content": "x"}),
            ("editor", {"command": "create", "path": str(path), "file_text": "x"}),
        ):
            result = guard.check_tool_call(tool, args, hitl_approved=True)
            assert not result.allowed and result.layer == 3, (tool, path)
    # Relative paths (anchored at the symlinked workspace) are refused too.
    if name == "workspace":
        assert not policy.check("x.md").allowed
    assert head.read_text(encoding="utf-8") == "anchor"


def test_dot_and_empty_exceptions_are_refused(tmp_path):
    from stiracore.config.data_dir import name_problem

    for bad in (".", "./", "", "a/..", "../x", "/abs"):
        with pytest.raises(ValueError):
            WritePathPolicy(base_dir=tmp_path, floor_tree=tmp_path, floor_tree_writable=[bad])
    for bad in (".", "./", "", "a/..", "../x", "/abs", "a//b", "a\\b", "a/"):
        assert name_problem(tmp_path, bad) is not None, bad
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert "not a directory" in name_problem(tmp_path, "file")
    assert "does not exist" in name_problem(tmp_path, "missing")
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert "symbolic link" in name_problem(tmp_path, "link")
    (tmp_path / "real" / "sub").mkdir()
    assert "symbolic link" in name_problem(tmp_path, "link/sub")
    assert name_problem(tmp_path, "real/sub") is None


def test_doctor_and_server_build_the_same_policy(tmp_path, monkeypatch, capsys):
    """Same roots and floor; the default config is recognised however it is spelled."""
    import json

    from stiracore.diagnostics import __main__ as cli

    repo = tmp_path / "repo"
    captured, data_dir = _stira_app(repo / ".local", monkeypatch)
    server = captured["write_policy"]
    built = {}
    real_run = cli.run_doctor

    def spy(roots, *, policy, **kwargs):
        built["policy"] = policy
        return real_run(roots, policy=policy, **kwargs)

    monkeypatch.setattr(cli, "run_doctor", spy)
    monkeypatch.chdir(repo)
    (repo / "sub").mkdir()
    capsys.readouterr()
    for spelling in (
        None,
        "sub/../.local/stira/agent-config.json",
        str(data_dir / "agent-config.json"),
    ):
        argv = ["doctor", "--json"] + (["--config", spelling] if spelling else [])
        assert cli.main(argv) == 0, spelling
        json.loads(capsys.readouterr().out)
        policy = built["policy"]
        assert policy.write_roots == server.write_roots, spelling
        assert policy.floor_tree_writable == server.floor_tree_writable
        assert policy.workspace == server.workspace
        assert policy.check(str(data_dir / "events.db.head")).code == "floor"


def test_the_settings_page_shows_only_exceptions_inside_the_roots(tmp_path, monkeypatch):
    from stiracore.config.data_dir import build_policy, check_data_dir, shown_writable
    from stiracore.config.models import AgentConfig

    captured, data_dir = _stira_app(tmp_path, monkeypatch)
    floor = check_data_dir(data_dir)
    assert shown_writable(captured["write_policy"], floor) == [
        "workspace/",
        "memory/agents/stira/memory/episodic/",
        "context-stash/",
    ]
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    explicit = AgentConfig(path=tmp_path / "a.json", workspace_dir=str(elsewhere))
    policy, floor = build_policy(explicit, data_dir)
    assert shown_writable(policy, floor) == []
    assert not policy.check(str(data_dir / MEMORY / "episodic" / "x.md")).allowed


# --- TOCTOU: the write re-checks after opening (file_write bridge and editor) --------


def _granted_open(policy, path, mode="w"):
    """Run the upstream file_write module's ``open`` inside a grant, as the tool does."""
    import strands_tools.file_write as upstream

    from stiracore.tools.tool_consent import consent_granted, install_consent_bridge

    assert "file_write" in install_consent_bridge()
    decision = policy.check(str(path))
    assert decision.allowed
    return upstream, decision.path, consent_granted


def test_file_write_refuses_a_parent_swapped_after_the_check(stira):
    _, data_dir, policy = stira
    sub = data_dir / "workspace" / "sub"
    sub.mkdir()
    target = sub / "abc.json"
    upstream, checked, consent_granted = _granted_open(policy, target)
    existing = _touch(data_dir / "checkpoints" / "abc.json", "{}")
    # Between the check and the open, the parent becomes a link into the floor.
    sub.rmdir()
    sub.symlink_to(data_dir / "checkpoints")
    with (
        consent_granted("file_write", write_path=checked, policy=policy),
        pytest.raises(PermissionError),
    ):
        upstream.open(checked, "w")
    assert existing.read_text(encoding="utf-8") == "{}"  # never truncated


def test_file_write_removes_its_own_file_when_the_recheck_fails(stira):
    _, data_dir, policy = stira
    sub = data_dir / "workspace" / "sub"
    sub.mkdir()
    upstream, checked, consent_granted = _granted_open(policy, sub / "new.json")
    sub.rmdir()
    sub.symlink_to(data_dir / "checkpoints")
    (data_dir / "checkpoints").mkdir(exist_ok=True)
    with (
        consent_granted("file_write", write_path=checked, policy=policy),
        pytest.raises(PermissionError),
    ):
        upstream.open(checked, "w")
    assert not (data_dir / "checkpoints" / "new.json").exists()


def test_file_write_open_refuses_without_a_grant_or_another_path(stira):
    from stiracore.tools.tool_consent import ToolConsentError

    _, data_dir, policy = stira
    upstream, checked, consent_granted = _granted_open(policy, data_dir / "workspace" / "a.md")
    with pytest.raises(ToolConsentError):
        upstream.open(checked, "w")
    with consent_granted("file_write", write_path=checked, policy=policy):
        with pytest.raises(PermissionError):
            upstream.open(str(data_dir / "workspace" / "b.md"), "w")
        with pytest.raises(ToolConsentError):
            upstream.open(checked, "a")
        with upstream.open(checked, "w") as handle:
            handle.write("ok")
    assert (data_dir / "workspace" / "a.md").read_text(encoding="utf-8") == "ok"


def test_file_write_refuses_a_final_symlink_or_hard_link_swapped_in(stira):
    _, data_dir, policy = stira
    head = _touch(data_dir / "events.db.head", "anchor")
    target = data_dir / "workspace" / "x.md"
    upstream, checked, consent_granted = _granted_open(policy, target)
    target.symlink_to(head)
    with (
        consent_granted("file_write", write_path=checked, policy=policy),
        pytest.raises(OSError),
    ):
        upstream.open(checked, "w")
    target.unlink()
    os.link(head, target)
    with (
        consent_granted("file_write", write_path=checked, policy=policy),
        pytest.raises(PermissionError),
    ):
        upstream.open(checked, "w")
    assert head.read_text(encoding="utf-8") == "anchor"


def test_an_approved_file_write_end_to_end_still_works(stira):
    captured, data_dir, policy = stira
    tools = tool_registry.get_tools(
        "normal", read_only_paths=captured["read_only_paths"], write_policy=policy
    )
    file_write = next(t for t in tools if getattr(t, "tool_name", None) == "file_write")
    agent = NXPAgent(
        model=FakeModel(
            [tool_turn("file_write", {"path": "notes/a.md", "content": "hello"}), text_turn("ok")]
        ),
        state=NeuralState(stability=0.9),
        config=StiraConfig(critical_action="halt"),
        tools=[file_write],
        on_hitl_request=Mock(return_value=HITLDecision(HITLAction.APPROVE)),
        read_only_paths=captured["read_only_paths"],
        write_policy=policy,
        stimulus_classifier=Mock(classify=lambda **kw: None),
        tool_policy=lambda name: ("ask", 1),
    )
    agent("go")
    assert (data_dir / "workspace" / "notes" / "a.md").read_text(encoding="utf-8") == "hello"


def test_the_editor_sandbox_refuses_a_parent_swapped_after_the_check(stira):
    from stiracore.tools.file_editor_tool import StiraWriteSandbox
    from stiracore.tools.tool_consent import consent_granted

    _, data_dir, policy = stira
    sub = data_dir / "workspace" / "sub"
    sub.mkdir()
    checked = policy.check(str(sub / "abc.json")).path
    existing = _touch(data_dir / "checkpoints" / "abc.json", "{}")
    sandbox = StiraWriteSandbox()
    with consent_granted("editor", write_path=checked, policy=policy):
        # Swapped after the sandbox's own pre-check would run: patch it out to
        # model the race, so only the post-open re-check stands in the way.
        sub.rmdir()
        sub.symlink_to(data_dir / "checkpoints")
        sandbox._authorize_write = lambda path: Path(path)
        with pytest.raises(PermissionError):
            asyncio.run(sandbox.write_file(checked, b"forged"))
    assert existing.read_text(encoding="utf-8") == "{}"


# --- review round 2 (#290): a refused default workspace is never resolved into a root -


@pytest.mark.parametrize("name", ["workspace", "context-stash"])
def test_a_pre_start_symlink_to_an_outside_directory_opens_nothing(
    tmp_path, monkeypatch, capsys, name
):
    data_dir = tmp_path / "stira"
    data_dir.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    (data_dir / name).symlink_to(escape)
    captured, _ = _stira_app(tmp_path, monkeypatch)
    policy = captured["write_policy"]
    out = capsys.readouterr().out
    assert "not writable for the agent" in out
    guard = _guard(captured)
    targets = [str(escape / "x.md"), str(data_dir / name / "x.md")]
    if name == "workspace":
        assert "@workspace is disabled" in out
        # Relative paths now anchor at the data directory, which is on the floor.
        targets += ["x.md", "sub/y.md", "workspace/x.md"]
        assert escape.resolve() not in policy.write_roots
        assert policy.workspace == data_dir.resolve()  # anchors on the floor
    for path in targets:
        assert not policy.check(path).allowed, path
        for tool, args in (
            ("file_write", {"path": path, "content": "x"}),
            ("editor", {"command": "create", "path": path, "file_text": "x"}),
        ):
            assert not guard.check_tool_call(tool, args, hitl_approved=True).allowed, path
    assert list(escape.iterdir()) == []
    assert all(escape.resolve() != Path(root).resolve() for root in policy.write_roots)


def test_a_disabled_workspace_still_leaves_episodic_and_stash(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "stira"
    data_dir.mkdir()
    (data_dir / "workspace").write_text("not a directory", encoding="utf-8")
    captured, _ = _stira_app(tmp_path, monkeypatch)
    policy = captured["write_policy"]
    assert "@workspace is disabled" in capsys.readouterr().out
    assert policy.write_roots == (
        (data_dir / MEMORY / "episodic").resolve(),
        (data_dir / "context-stash").resolve(),
    )
    assert policy.check(str(data_dir / MEMORY / "episodic" / "x.md")).allowed
    assert policy.check(str(data_dir / "context-stash" / "stira" / "x")).allowed
    assert not policy.check("x.md").allowed


def test_a_workspace_that_cannot_be_created_disables_the_root_without_crashing(
    tmp_path, monkeypatch, capsys
):
    """mkdir may fail (permission, read-only file system): warn and disable, never crash."""
    data_dir = tmp_path / "stira"
    data_dir.mkdir()
    real_mkdir = Path.mkdir

    def refusing_mkdir(self, *args, **kwargs):
        if self == data_dir / "workspace":
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refusing_mkdir)
    captured, _ = _stira_app(tmp_path, monkeypatch)
    policy = captured["write_policy"]
    assert "@workspace is disabled" in capsys.readouterr().out
    assert not policy.check("x.md").allowed
    assert not (data_dir / "workspace").exists()


def test_a_symlinked_episodic_layer_stops_the_server(tmp_path, monkeypatch):
    """MemoryStore refuses symlinks in its paths: fail closed before any tool exists."""
    data_dir = tmp_path / "stira"
    layer_parent = data_dir / MEMORY
    layer_parent.mkdir(parents=True)
    escape = tmp_path / "escape"
    escape.mkdir()
    (layer_parent / "episodic").symlink_to(escape)
    with pytest.raises(ValueError, match="symbolic links"):
        _stira_app(tmp_path, monkeypatch)


def test_an_explicit_workspace_inside_the_data_directory_stays_on_the_floor(tmp_path, monkeypatch):
    data_dir = tmp_path / "stira"
    (data_dir / "checkpoints").mkdir(parents=True)
    captured, _ = _stira_app(
        tmp_path, monkeypatch, {"workspace_dir": str(data_dir / "checkpoints")}
    )
    policy = captured["write_policy"]
    assert policy.write_roots == ((data_dir / "checkpoints").resolve(),)
    for path in ("x.json", str(data_dir / "checkpoints" / "x.json")):
        assert policy.check(path).code == "floor", path


def test_the_start_up_doctor_checks_the_effective_roots(stira):
    """The roots handed to the doctor are the policy's (count checked in test_write_diagnostics)."""
    from stiracore.config.data_dir import check_data_dir, effective_write_roots

    _, data_dir, policy = stira
    from stiracore.config.models import AgentConfig

    roots = effective_write_roots(AgentConfig(path=data_dir / "a.json"), check_data_dir(data_dir))
    assert roots == [
        "@workspace",
        str(data_dir / MEMORY / "episodic"),
        str(data_dir / "context-stash"),
    ]
    assert tuple(Path(r).resolve() if r != "@workspace" else policy.workspace for r in roots) == (
        policy.write_roots
    )
