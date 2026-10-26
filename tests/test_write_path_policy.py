"""Issue #243: write-path policy — alias bypasses of protected_dirs/read_only_paths.

Each case below was measured on macOS/APFS (2026-10-14 investigation). The
case- and Unicode-normalization variants created a *new* file inside the
protected subtree before this fix; the other aliases were already rejected
and are kept here as regressions.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn
from strands.tools.tools import AgentTool

from stiracore import identity_paths
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.config.models import AgentConfig
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.identity_paths import (
    OUTSIDE_WRITE_ROOTS_ERROR,
    WORKSPACE_ROOT,
    IdentityPathPolicy,
    WritePathPolicy,
)
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.tools.identity_tools import IdentityProtectedTool
from stiracore.tools.tool_consent import mark_gate_passed


class FakeWriteTool(AgentTool):
    """Side effects like the upstream tools.

    file_write: ``os.makedirs(dirname(path))`` on the path *as given* (``..``
    kept, like strands_tools), then write. editor: read-only commands write
    nothing; Stira's editor writes no ``{path}.bak`` (#245).
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.paths: list[str] = []

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> Any:
        return {"name": self._name, "description": "fake", "inputSchema": {"json": {}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        path = tool_use["input"]["path"]
        command = tool_use["input"].get("command")
        self.paths.append(path)
        if not (self._name == "editor" and command in ("view", "find_line")):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            Path(path).write_text("injected", encoding="utf-8")
        yield {
            "tool_result": {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": "ok"}],
            }
        }


def _run(tool: AgentTool, path: str | Path, command: str = "create") -> dict:
    async def collect() -> list:
        use = {
            "toolUseId": "t1",
            "name": tool.tool_name,
            "input": {"command": command, "path": str(path)},
        }
        state: dict = {}
        mark_gate_passed(state, use)  # acting as Stira's hook gate (#242)
        return [event async for event in tool.stream(use, state)]

    return asyncio.run(collect())[-1]["tool_result"]


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    directory = tmp_path / "memory" / "procedural"
    directory.mkdir(parents=True)
    (directory / "rule.md").write_text("orig", encoding="utf-8")
    return directory


def _files(directory: Path) -> set[str]:
    return {p.name for p in directory.rglob("*") if p.is_file()}


# --- Stage 1: alias bypasses (each must be rejected) ---------------------------


@pytest.mark.parametrize("name", ["file_write", "editor"])
def test_case_variant_new_file_rejected(proc: Path, name: str) -> None:
    """``memory/PROCEDURAL/new.md`` was created inside the protected dir (measured)."""
    tool = IdentityProtectedTool(FakeWriteTool(name), protected_dirs=[str(proc)])
    variant = proc.parent / "PROCEDURAL" / "new.md"
    assert _run(tool, variant)["status"] == "error"
    assert _files(proc) == {"rule.md"}
    assert not variant.exists()


def test_nfd_alias_new_file_rejected(tmp_path: Path) -> None:
    """A protected dir named in NFC, written via its NFD spelling (measured bypass)."""
    nfc = unicodedata.normalize("NFC", "保護ガイド")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    protected = tmp_path / nfc
    protected.mkdir()
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), protected_dirs=[str(protected)])
    assert _run(tool, tmp_path / nfd / "x.md")["status"] == "error"
    assert not any(protected.iterdir())
    assert not (tmp_path / nfd / "x.md").exists()


def test_case_and_nfd_variants_of_read_only_file_rejected(tmp_path: Path) -> None:
    target = tmp_path / "Persona" / "ガイド.md"
    policy = IdentityPathPolicy([str(target)])
    assert policy.is_read_only(tmp_path / "PERSONA" / "ガイド.md")
    assert policy.is_read_only(tmp_path / "persona" / unicodedata.normalize("NFD", "ガイド.md"))
    # Replacing a case-variant ancestor also replaces the anchor.
    assert policy.is_read_only(tmp_path / "PERSONA")
    assert not policy.is_read_only(tmp_path / "other.md")


def test_symlink_alias_rejected(proc: Path, tmp_path: Path) -> None:
    dir_link = tmp_path / "dir_link"
    file_link = tmp_path / "file_link.md"
    try:
        dir_link.symlink_to(proc, target_is_directory=True)
        file_link.symlink_to(proc / "rule.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    tool = IdentityProtectedTool(FakeWriteTool("editor"), protected_dirs=[str(proc)])
    assert _run(tool, dir_link / "evil.md")["status"] == "error"
    assert _run(tool, dir_link / "new" / "deeper.md")["status"] == "error"
    assert _run(tool, file_link)["status"] == "error"
    assert _files(proc) == {"rule.md"}
    assert (proc / "rule.md").read_text(encoding="utf-8") == "orig"


def test_symlink_to_case_variant_rejected(proc: Path, tmp_path: Path) -> None:
    link = tmp_path / "upper_link"
    try:
        link.symlink_to(proc.parent / "PROCEDURAL", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), protected_dirs=[str(proc)])
    assert _run(tool, link / "x.md")["status"] == "error"
    assert _files(proc) == {"rule.md"}


def test_hard_link_alias_rejected(proc: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias.md"
    try:
        os.link(proc / "rule.md", alias)
    except OSError:
        pytest.skip("hard links unsupported")
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), protected_dirs=[str(proc)])
    assert _run(tool, alias)["status"] == "error"
    assert (proc / "rule.md").read_text(encoding="utf-8") == "orig"


def test_dotdot_alias_rejected(proc: Path, tmp_path: Path) -> None:
    (tmp_path / "memory" / "episodic").mkdir()
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), protected_dirs=[str(proc)])
    assert _run(tool, f"{tmp_path}/memory/episodic/../procedural/x.md")["status"] == "error"
    assert _run(tool, f"{tmp_path}/memory/missing/../PROCEDURAL/y.md")["status"] == "error"
    assert _files(proc) == {"rule.md"}


@pytest.mark.skipif(sys.platform != "darwin", reason="/tmp -> /private/tmp is macOS-specific")
def test_private_tmp_alias_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="stira-243-", dir="/tmp"))
    try:
        protected = base / "procedural"
        protected.mkdir()
        private = Path("/private") / base.relative_to("/") / "procedural"
        for configured, written in ((protected, private), (private, protected)):
            tool = IdentityProtectedTool(
                FakeWriteTool("file_write"), protected_dirs=[str(configured)]
            )
            assert _run(tool, written / "x.md")["status"] == "error"
            assert _run(tool, str(written / "x.md").upper())["status"] == "error"
        assert not any(protected.iterdir())
    finally:
        for child in sorted(base.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        base.rmdir()


def test_case_variant_rejected_even_on_case_sensitive_volume(tmp_path: Path) -> None:
    """Unsure volumes count as case-insensitive: the variant is denied either way."""
    protected = tmp_path / "procedural"
    protected.mkdir()
    policy = IdentityPathPolicy(protected_dirs=[protected])
    assert policy.is_read_only(tmp_path / "Procedural" / "x.md")
    assert policy.is_read_only(tmp_path / "PROCEDURAL")
    assert not policy.is_read_only(tmp_path / "procedural-notes" / "x.md")
    if not _case_insensitive(tmp_path):
        # The variant is a different directory here, still denied (safe side).
        assert not (tmp_path / "Procedural").exists()


def test_editor_writes_and_checks_no_backup_file(tmp_path: Path) -> None:
    """#245: Stira's editor writes no ``{path}.bak``, so that name is not a write target."""
    target = tmp_path / "notes.md"
    target.write_text("x", encoding="utf-8")
    tool = IdentityProtectedTool(
        FakeWriteTool("editor"), read_only_paths=[str(tmp_path / "NOTES.md.bak")]
    )
    assert _run(tool, target, "str_replace")["status"] == "success"
    assert _run(tool, target, "view")["status"] == "success"
    assert _files(tmp_path) == {"notes.md"}


def test_ordinary_paths_still_allowed(proc: Path, tmp_path: Path) -> None:
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), protected_dirs=[str(proc)])
    for path in (
        tmp_path / "memory" / "episodic" / "note.md",
        tmp_path / "memory" / "procedural-archive" / "x.md",
        tmp_path / "ok" / "nested" / "made.md",
    ):
        assert _run(tool, path)["status"] == "success", path
        assert path.read_text(encoding="utf-8") == "injected"


# --- Pure comparison key (Windows spellings without a Windows machine) ----------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (r"C:\Users\Agent\Memory\PROCEDURAL", "c:/users/agent/memory/procedural"),
        (r"\\?\C:\Users\Agent\memory", r"C:\USERS\agent\Memory"),
        (r"\\?\UNC\server\share\Dir", r"\\SERVER\share\dir"),
        ("/a/b/./c/", "/a/b/c"),
        ("/a/x/../b", "/a/b"),
        (unicodedata.normalize("NFD", "/a/ガイド"), "/a/ガイド"),
        ("/a/STRASSE", "/a/straße"),
    ],
)
def test_path_key_equivalences(left: str, right: str) -> None:
    assert identity_paths.path_key(left) == identity_paths.path_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("/a/b", "a/b"),
        ("/a/b", "/a/bc"),
        (r"C:\a", r"D:\a"),
        (r"\\server\share", "/server/share"),
    ],
)
def test_path_key_differences(left: str, right: str) -> None:
    assert identity_paths.path_key(left) != identity_paths.path_key(right)


# --- Stage 2: write roots (WritePathPolicy) -----------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "memory" / "procedural").mkdir(parents=True)
    return workspace


def _policy(ws: Path, roots=(WORKSPACE_ROOT,), **kwargs) -> WritePathPolicy:
    return WritePathPolicy(
        base_dir=ws, protected_dirs=[ws / "memory" / "procedural"], write_roots=roots, **kwargs
    )


def test_workspace_root_allows_inside_and_denies_outside(ws: Path, tmp_path: Path) -> None:
    policy = _policy(ws)
    assert policy.check(str(ws / "notes" / "a.md")).allowed
    outside = policy.check(str(tmp_path / "elsewhere.md"))
    assert (outside.allowed, outside.code) == (False, "outside_write_roots")
    assert OUTSIDE_WRITE_ROOTS_ERROR in outside.reason
    # Prefix siblings, .. escapes and ~ are outside too.
    for path in (f"{ws}-evil/x.md", f"{ws}/memory/../../x.md", "~/x.md", "/etc/x"):
        assert policy.check(path).code == "outside_write_roots", path
    # ``..`` through a directory that does not exist yet is refused outright.
    assert policy.check(f"{ws}/notes/../x.md").code == "invalid"


def test_floor_wins_inside_write_roots(ws: Path) -> None:
    decision = _policy(ws).check(str(ws / "memory" / "PROCEDURAL" / "x.md"))
    assert (decision.allowed, decision.code) == (False, "floor")
    assert _policy(ws).check(str(ws / "SOUL.md")).code == "floor"


def test_relative_paths_anchor_to_workspace_not_cwd(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = _policy(ws)
    decision = policy.check("notes/rel.md")
    assert decision.allowed and decision.path == str(ws.resolve() / "notes" / "rel.md")
    fake = FakeWriteTool("file_write")
    tool = IdentityProtectedTool(fake, policy=policy)
    assert _run(tool, "notes/rel.md")["status"] == "success"
    # The tool received exactly the checked absolute path.
    assert fake.paths == [str(ws.resolve() / "notes" / "rel.md")]
    assert (ws / "notes" / "rel.md").exists() and not (tmp_path / "notes").exists()


def test_symlink_inside_workspace_pointing_outside_is_denied(ws: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (ws / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), policy=_policy(ws))
    result = _run(tool, ws / "escape" / "x.md")
    assert result["status"] == "error"
    assert OUTSIDE_WRITE_ROOTS_ERROR in result["content"][0]["text"]
    assert not any(outside.iterdir())


def test_case_variant_of_root_follows_the_volume(ws: Path) -> None:
    """Allow is by identity, never by folding: a variant passes only where it IS the root."""
    variant = str(ws.parent / ws.name.upper() / "x.md")
    assert _policy(ws).check(variant).allowed is _case_insensitive(ws.parent)


@pytest.mark.skipif(sys.platform != "darwin", reason="/tmp -> /private/tmp is macOS-specific")
def test_tmp_root_accepts_private_tmp_alias(ws: Path) -> None:
    policy = _policy(ws, roots=(WORKSPACE_ROOT, "/tmp"))
    assert policy.check("/private/tmp/stira-243-probe/x.md").allowed
    assert policy.check("/tmp/stira-243-probe/x.md").allowed
    assert not _policy(ws).check("/tmp/x.md").allowed


def test_makedirs_outside_roots_is_denied(tmp_path: Path) -> None:
    """A root that does not exist yet may be created, its missing parents may not."""
    root = tmp_path / "missing-parent" / "root"
    policy = WritePathPolicy(base_dir=tmp_path, write_roots=[str(root)])
    assert policy.check(str(root / "x.md")).code == "outside_write_roots"
    (tmp_path / "missing-parent").mkdir()
    assert policy.check(str(root / "deep" / "x.md")).allowed


def test_editor_with_file_sized_root_needs_no_backup_sibling(ws: Path) -> None:
    """A file-sized root: both tools may write it; editor has no ``.bak`` sibling (#245)."""
    notes = ws / "notes.md"
    notes.write_text("x", encoding="utf-8")
    policy = WritePathPolicy(base_dir=ws, write_roots=[str(notes)])
    assert (
        _run(IdentityProtectedTool(FakeWriteTool("file_write"), policy=policy), notes)["status"]
        == "success"
    )
    editor = IdentityProtectedTool(FakeWriteTool("editor"), policy=policy)
    assert _run(editor, notes, "str_replace")["status"] == "success"
    assert _run(editor, notes, "view")["status"] == "success"
    assert _run(editor, ws / "other.md")["status"] == "error"


def test_missing_path_with_roots_fails_closed(ws: Path) -> None:
    assert _policy(ws).check(None).code == "invalid"
    assert _policy(ws).check("").code == "invalid"
    assert WritePathPolicy().check(None).allowed  # no allowlist: the tool decides


def test_outside_action_only_deny_for_now(ws: Path) -> None:
    with pytest.raises(ValueError):
        _policy(ws, outside_action="ask")


# --- Shared decision: IdentityGuard (#5) and the HITL gate (#4) ----------------


def _engine(stability: float = 0.9):
    from stiracore.core import StiraEngine

    return StiraEngine(state=NeuralState(stability=stability), config=StiraConfig())


def test_identity_guard_applies_write_roots_and_protected_dirs(ws: Path, tmp_path: Path) -> None:
    guard = IdentityGuard(_engine(), path_policy=_policy(ws))
    assert guard.check_tool_call("file_write", {"path": str(ws / "a.md")}).allowed
    outside = guard.check_tool_call("file_write", {"path": str(tmp_path / "a.md")})
    assert not outside.allowed and OUTSIDE_WRITE_ROOTS_ERROR in outside.reason
    # Before #243 IdentityGuard did not know protected_dirs at all.
    proc = guard.check_tool_call("write_blob", {"key": str(ws / "memory" / "Procedural" / "r")})
    assert not proc.allowed and proc.layer == 3
    # Read-only editor commands are not writes.
    assert guard.check_tool_call("editor", {"command": "view", "path": "/etc/hosts"}).allowed


def _critical_agent(ws: Path, target: Path, hitl: Mock):
    calls: list = []
    model = FakeModel([tool_turn("file_write", {"path": str(target)}), text_turn("done")])
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), policy=_policy(ws))
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.1),
        config=StiraConfig(critical_action="halt"),
        tools=[tool],
        on_hitl_request=hitl,
        write_policy=_policy(ws),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    agent.config.blocked_tool_names = frozenset({"file_write"})
    agent("go")
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    return results, tool.wrapped.paths, calls


def test_hitl_gate_does_not_ask_for_writes_outside_roots(ws: Path, tmp_path: Path) -> None:
    hitl = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    results, written, _ = _critical_agent(ws, tmp_path / "outside.md", hitl)
    assert not hitl.called
    assert results[0]["status"] == "error"
    assert OUTSIDE_WRITE_ROOTS_ERROR in results[0]["content"][0]["text"]
    assert written == [] and not (tmp_path / "outside.md").exists()


def test_hitl_gate_still_asks_inside_roots(ws: Path) -> None:
    hitl = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    _results, written, _ = _critical_agent(ws, ws / "inside.md", hitl)
    assert hitl.called
    assert written == [str(ws.resolve() / "inside.md")]


# --- Delegation (#212): a child can narrow, never widen ---------------------------


def _parent(ws: Path) -> NXPAgent:
    return NXPAgent(
        model=FakeModel([text_turn("x")]),
        write_policy=_policy(ws),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )


def test_child_inherits_parent_write_roots(ws: Path, tmp_path: Path) -> None:
    fake = FakeWriteTool("file_write")
    child = create_nxp_subagent(_parent(ws), "task", tools=[fake])
    assert child.write_policy.write_roots == (ws.resolve(),)
    wrapped = next(t for t in child.tools if t.tool_name == "file_write")
    assert _run(wrapped, tmp_path / "outside.md")["status"] == "error"
    assert _run(wrapped, ws / "inside.md")["status"] == "success"


def test_child_may_narrow_but_not_widen(ws: Path, tmp_path: Path) -> None:
    parent = _parent(ws)
    narrow = create_nxp_subagent(parent, "task", write_roots=[str(ws / "sub")])
    assert narrow.write_policy.check(str(ws / "sub" / "x.md")).allowed
    assert not narrow.write_policy.check(str(ws / "other.md")).allowed
    # The floor is inherited unchanged.
    assert narrow.write_policy.check(str(ws / "memory" / "procedural" / "x")).code == "floor"
    for wider in ([str(tmp_path)], ["/tmp"], [str(ws), str(tmp_path / "x")]):
        with pytest.raises(ValueError, match="cannot widen"):
            create_nxp_subagent(parent, "task", write_roots=wider)


def test_child_of_unrestricted_parent_may_set_roots(ws: Path) -> None:
    parent = NXPAgent(
        model=FakeModel([text_turn("x")]), stimulus_classifier=Mock(classify=lambda **k: None)
    )
    child = create_nxp_subagent(parent, "task", write_roots=[str(ws)])
    assert child.write_policy.write_roots == (ws.resolve(),)


# --- Configuration ------------------------------------------------------------------


def test_agent_config_defaults_to_workspace_only(tmp_path: Path) -> None:
    config = AgentConfig(path=tmp_path / "agent.json")
    assert config.write_roots == [WORKSPACE_ROOT]
    assert config.write_outside_action == "deny"
    assert config.workspace_dir is None
    config.write_roots = [WORKSPACE_ROOT, "/tmp"]
    config.save()
    loaded = AgentConfig.load(tmp_path / "agent.json")
    assert loaded.write_roots == [WORKSPACE_ROOT, "/tmp"]
    for bad in ({"write_roots": [""]}, {"write_roots": "x"}, {"write_outside_action": "ask"}):
        with pytest.raises(ValueError):
            AgentConfig(path=tmp_path / "x.json", **bad)


def test_stira_file_tools_are_limited_to_its_workspace(tmp_path: Path, monkeypatch) -> None:
    from dashboard import stira_server

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(kwargs)

    data = tmp_path / "stira"
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    monkeypatch.setattr(
        stira_server.AgentConfig,
        "load",
        classmethod(lambda cls, *_a, **_k: cls(path=tmp_path / "a.json")),
    )
    stira_server.create_app()
    policy = captured["write_policy"]
    # #271: the default workspace is the data directory's workspace/ subdirectory
    # (plus the episodic layer and the context stash); the rest is on the floor.
    assert policy.write_roots[0] == (data / "workspace").resolve()
    assert policy.check(str(data / "workspace" / "notes" / "x.md")).allowed
    assert policy.check(str(data / "notes" / "x.md")).code == "floor"
    assert policy.check(str(tmp_path / "outside.md")).code == "outside_write_roots"
    assert (
        policy.check(
            str(data / "memory" / "agents" / "stira" / "memory" / "procedural" / "r.md")
        ).code
        == "floor"
    )


# --- Settings hub (#227): the write scope is editable and recorded ----------------


def _hub_client(tmp_path: Path, **kwargs):
    from flask import Flask

    from stiracore.observability.config_api import ConfigStore, create_config_blueprint

    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client(), store


def _hub_save(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


_WIDENED = {"acknowledged_dangers": ["write_roots_widened"]}  # #536: added roots confirm


def test_hub_default_write_scope_is_workspace_only(tmp_path: Path) -> None:
    from stiracore.observability import settings_hub as hub

    client, _store = _hub_client(tmp_path)
    state = client.get("/api/config/hub").get_json()
    assert state["values"]["write"] == {"roots": [WORKSPACE_ROOT]}
    assert state["defaults"]["write"] == {"roots": [WORKSPACE_ROOT]}
    assert state["constraints"]["write_scopes"]["workspace"] == [WORKSPACE_ROOT]
    assert hub.write_scope([WORKSPACE_ROOT]) == "workspace"
    assert hub.write_scope([WORKSPACE_ROOT, "/tmp"]) == "workspace_tmp"
    assert hub.write_scope(["/data"]) == "custom"
    # Safety presets (tools/breaker) leave the write scope alone.
    for preset in state["presets"]:
        assert preset["values"]["write"] == {"roots": [WORKSPACE_ROOT]}


def test_hub_saves_write_roots_with_history_and_rollback(tmp_path: Path) -> None:
    client, store = _hub_client(tmp_path)
    response = _hub_save(client, {"write": {"roots": [WORKSPACE_ROOT, "/tmp"]}}, **_WIDENED)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["saved"] == ["write"]
    assert store.load()[1].write_roots == [WORKSPACE_ROOT, "/tmp"]
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "write.roots", "added": ["/tmp"], "removed": []}]
    version = client.get("/api/config/hub").get_json()["version"]
    undo = client.post(
        f"/api/config/history/{entry['id']}/rollback", json={"base_version": version}
    )
    assert undo.status_code == 200, undo.get_json()
    assert store.load()[1].write_roots == [WORKSPACE_ROOT]


@pytest.mark.parametrize(
    "roots", [["relative/dir"], [""], [" /tmp"], "/tmp", [1], ["/x\x00y"], ["/r"] * 33]
)
def test_hub_rejects_invalid_write_roots(tmp_path: Path, roots) -> None:
    client, store = _hub_client(tmp_path)
    response = _hub_save(client, {"write": {"roots": roots}})
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["field"] == "write.roots"
    assert store.load()[1].write_roots == [WORKSPACE_ROOT]


def test_hub_reports_write_scope_applies_at_restart(tmp_path: Path) -> None:
    runtime = {"tools": None, "breaker": None, "classifier_choice": "auto", "error": None}
    runtime["write"] = {"roots": [WORKSPACE_ROOT]}
    client, _store = _hub_client(tmp_path, runtime_settings_provider=lambda: dict(runtime))
    assert client.get("/api/config/hub").get_json()["apply"]["write"]["status"] == "applied"
    saved = _hub_save(client, {"write": {"roots": ["~/stira-work"]}}, **_WIDENED).get_json()
    assert saved["state"]["apply"]["write"] == {"status": "pending", "applies": "restart"}


# --- Review fixes (PR #250) -------------------------------------------------------

from pathlib import PureWindowsPath

from stiracore.identity_paths import (
    _parts_within,
    broad_root_problem,
    path_key,
    windows_path_problem,
)


def _real_tool(name: str, monkeypatch: pytest.MonkeyPatch) -> AgentTool:
    """The real strands_tools tool (consent prompt bypassed in this test only)."""
    from stiracore.tools import tool_registry

    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    tool = tool_registry.WRITE_TOOLS.get(name)
    if tool is None:
        pytest.skip(f"strands_tools.{name} unavailable")
    return tool.wrapped if isinstance(tool, IdentityProtectedTool) else tool


# B1: the floor also applies to every directory makedirs would create.


@pytest.mark.parametrize("name", ["file_write", "editor"])
def test_missing_read_only_parent_is_not_created(ws: Path, name: str) -> None:
    locked = ws / "locked"
    policy = WritePathPolicy([str(locked)], base_dir=ws, write_roots=[WORKSPACE_ROOT])
    tool = IdentityProtectedTool(FakeWriteTool(name), policy=policy)
    result = _run(tool, locked / "child.txt")
    assert result["status"] == "error"
    assert "read-only" in result["content"][0]["text"]
    assert not locked.exists()


def test_soul_md_as_a_new_parent_is_not_created(ws: Path) -> None:
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), policy=_policy(ws))
    assert _run(tool, ws / "SOUL.md" / "child.txt")["status"] == "error"
    assert _run(tool, ws / "new" / "ipd.MD" / "child.txt")["status"] == "error"
    assert not (ws / "SOUL.md").exists() and not (ws / "new").exists()


def test_dotdot_through_new_protected_subdir_creates_nothing(ws: Path) -> None:
    """``procedural/new/../../notes/x.md`` resolves outside, but makedirs makes ``new``."""
    tool = IdentityProtectedTool(FakeWriteTool("file_write"), policy=_policy(ws))
    path = f"{ws}/memory/procedural/new/../../notes/x.md"
    assert _run(tool, path)["status"] == "error"
    assert not (ws / "memory" / "procedural" / "new").exists()
    assert not (ws / "memory" / "notes").exists()
    # Also with the floor-only (library default) policy.
    legacy = IdentityProtectedTool(
        FakeWriteTool("file_write"), protected_dirs=[str(ws / "memory" / "procedural")]
    )
    assert _run(legacy, path)["status"] == "error"
    assert not any((ws / "memory" / "procedural").iterdir())


def test_real_file_write_dotdot_and_missing_read_only_parent(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _real_tool("file_write", monkeypatch)
    locked = ws / "locked"
    policy = WritePathPolicy(
        [str(locked)],
        base_dir=ws,
        protected_dirs=[ws / "memory" / "procedural"],
        write_roots=[WORKSPACE_ROOT],
    )
    tool = IdentityProtectedTool(real, policy=policy)
    before = sorted(p.relative_to(ws) for p in ws.rglob("*"))
    for path in (f"{ws}/memory/procedural/new/../../notes/x.md", str(locked / "child.txt")):
        use = {"toolUseId": "r", "name": "file_write", "input": {"path": path, "content": "x"}}
        result = asyncio.run(_collect(tool, use))
        assert result["status"] == "error", path
    assert sorted(p.relative_to(ws) for p in ws.rglob("*")) == before


async def _collect(tool: AgentTool, use: dict) -> dict:
    state: dict = {"agent": None}  # the SDK always supplies it
    mark_gate_passed(state, use)  # acting as Stira's hook gate (#242)
    events = [event async for event in tool.stream(use, state)]
    last = events[-1]
    return last["tool_result"] if isinstance(last, dict) else last.tool_result


def test_real_editor_writes_the_checked_target_and_no_backup(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool gets the resolved path; Stira's editor writes no ``.bak`` (#245)."""
    real = _real_tool("editor", monkeypatch)
    target = ws / "real.md"
    target.write_text("hello world\n", encoding="utf-8")
    link = ws / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported")
    tool = IdentityProtectedTool(real, policy=_policy(ws))
    use = {
        "toolUseId": "e",
        "name": "editor",
        "input": {"command": "str_replace", "path": str(link), "old_str": "hello", "new_str": "hi"},
    }
    result = asyncio.run(_collect(tool, use))
    assert result["status"] == "success", result
    assert target.read_text(encoding="utf-8").startswith("hi world")
    assert not (ws / "real.md.bak").exists() and not (ws / "link.md.bak").exists()
    # A read-only resolved target is refused before anything is written.
    guarded = IdentityProtectedTool(real, policy=WritePathPolicy([str(target)], base_dir=ws))
    use["input"] = {**use["input"], "old_str": "hi", "new_str": "yo"}
    assert asyncio.run(_collect(guarded, use))["status"] == "error"
    assert target.read_text(encoding="utf-8").startswith("hi world")


def test_read_only_directory_path_protects_its_subtree(ws: Path) -> None:
    (ws / "frozen").mkdir()
    policy = WritePathPolicy([str(ws / "frozen")], base_dir=ws, write_roots=[WORKSPACE_ROOT])
    assert policy.check(str(ws / "frozen" / "x.md")).code == "floor"


# M1: Windows-only syntax fails closed.


@pytest.mark.parametrize(
    "path",
    [
        r"C:\ws\SOUL.md:stream",
        r"C:\ws\notes.md:hidden:$DATA",
        r"C:\ws\memory\procedural:x\rule.md",
        r"C:\ws\SOUL.md.",
        r"C:\ws\SOUL.md ",
        r"C:\ws\dir.\x.md",
        "\\\\?\\C:\\ws\\x.md",
        "\\\\.\\C:\\ws\\x.md",
        "\\??\\C:\\ws\\x.md",
        r"C:\ws\NUL.txt",
        r"C:\ws\com1",
        r"rel\a:b",
    ],
)
def test_windows_path_problem_refuses(path: str) -> None:
    assert windows_path_problem(path)


@pytest.mark.parametrize(
    "path", [r"C:\ws\notes.md", "C:/ws/a/b.md", r"\\server\share\x.md", r"C:\ws\console.md"]
)
def test_windows_path_problem_accepts(path: str) -> None:
    assert windows_path_problem(path) is None


def test_windows_mode_policy_refuses_streams(ws: Path) -> None:
    policy = WritePathPolicy(base_dir=ws, write_roots=[WORKSPACE_ROOT], windows=True)
    decision = policy.check(f"{ws}/SOUL.md:stream")
    assert (decision.allowed, decision.code) == (False, "invalid")
    assert policy.check(f"{ws}/ok.md").allowed


def test_stream_and_trailing_dot_spellings_hit_the_floor_everywhere(ws: Path) -> None:
    """Deny side folds ``name:stream`` / ``name.`` to ``name`` on every OS (over-deny only)."""
    policy = _policy(ws)
    for path in ("SOUL.md:s", "soul.md.", "memory/procedural:x/rule.md", "memory/procedural./r"):
        assert policy.is_read_only(ws / path), path
    assert path_key(r"C:\ws\SOUL.md:stream") == path_key("c:/ws/soul.md")
    assert path_key("C:/ws/Dir. /x") == path_key("c:/ws/dir/x")
    assert path_key("C:/x") != path_key("D:/x")


# M2: allow-side containment is exact per component.


def test_allow_containment_is_case_exact_even_for_windows_paths() -> None:
    root, sibling = PureWindowsPath(r"C:\ws\Allowed"), PureWindowsPath(r"C:\ws\allowed\x.md")
    # The platform comparison would over-allow on a case-sensitive NTFS directory.
    assert root in sibling.parents
    assert not _parts_within(sibling, root)
    assert _parts_within(PureWindowsPath(r"C:\ws\Allowed\x.md"), root)
    assert not _parts_within(PureWindowsPath(r"C:\ws\Allowedx\x.md"), root)


def test_narrow_uses_exact_containment(ws: Path) -> None:
    (ws / "Allowed").mkdir()
    parent = WritePathPolicy(base_dir=ws, write_roots=[str(ws / "Allowed")])
    with pytest.raises(ValueError, match="cannot widen"):
        parent.narrow([str(ws / "Allowedx")])
    variant = str(ws / "allowed" / "sub")
    if _case_insensitive(ws):
        # Same directory on this volume: proven by file identity, not by folding.
        assert parent.narrow([variant]).check(f"{variant}/x.md").allowed
    else:
        with pytest.raises(ValueError, match="cannot widen"):
            parent.narrow([variant])


# M3: "workspace only" can never mean everything.


def test_broad_workspaces_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path.home()
    link = tmp_path / "to-root"
    try:
        link.symlink_to("/", target_is_directory=True)
    except (OSError, NotImplementedError):
        link = Path("/")
    for workspace in (Path("/"), home, home.parent, link):
        assert broad_root_problem(workspace), workspace
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=workspace, write_roots=[WORKSPACE_ROOT])
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=tmp_path, write_roots=[str(workspace)])
    monkeypatch.chdir("/")
    with pytest.raises(ValueError, match="too broad"):
        WritePathPolicy(base_dir=Path("."), write_roots=[WORKSPACE_ROOT])
    # Explicit opt-in only; narrow subdirectories of home are fine.
    assert WritePathPolicy(base_dir=Path("/"), write_roots=["@workspace"], allow_broad_roots=True)
    assert broad_root_problem(home / "projects" / "x") is None
    assert broad_root_problem(tmp_path) is None


def test_stira_refuses_to_start_with_a_broad_workspace(tmp_path: Path, monkeypatch) -> None:
    from dashboard import stira_server

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    monkeypatch.setattr(
        stira_server.AgentConfig,
        "load",
        classmethod(lambda cls, *_a, **_k: cls(path=tmp_path / "a.json", workspace_dir="/")),
    )
    with pytest.raises(ValueError, match="too broad"):
        stira_server.create_app()


def test_agent_config_broad_opt_in_is_a_boolean(tmp_path: Path) -> None:
    assert AgentConfig(path=tmp_path / "a.json").allow_broad_write_roots is False
    with pytest.raises(ValueError):
        AgentConfig(path=tmp_path / "a.json", allow_broad_write_roots="yes")


@pytest.mark.parametrize("roots", [["/"], ["~"], [str(Path.home())], [str(Path.home().parent)]])
def test_hub_refuses_broad_roots(tmp_path: Path, roots) -> None:
    client, store = _hub_client(tmp_path)
    response = _hub_save(client, {"write": {"roots": roots}})
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["code"] == "broad_root"
    assert store.load()[1].write_roots == [WORKSPACE_ROOT]


def test_hub_shows_the_effective_workspace(tmp_path: Path) -> None:
    runtime = {"tools": None, "breaker": None, "classifier_choice": "auto", "error": None}
    runtime["workspace"] = str(tmp_path / "stira")
    client, _store = _hub_client(tmp_path, runtime_settings_provider=lambda: dict(runtime))
    assert client.get("/api/config/hub").get_json()["workspace"] == str(tmp_path / "stira")
    bare, _ = _hub_client(tmp_path / "other")
    assert bare.get("/api/config/hub").get_json()["workspace"] is None


# M4: a policy given to the parent reaches its execution boundary.


def test_parent_policy_wraps_its_file_tools(ws: Path, tmp_path: Path) -> None:
    fake = FakeWriteTool("file_write")
    agent = NXPAgent(
        model=FakeModel([text_turn("x")]),
        tools=[fake, FakeWriteTool("other")],
        write_policy=_policy(ws),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    wrapped = next(t for t in agent.tools if t.tool_name == "file_write")
    assert isinstance(wrapped, IdentityProtectedTool) and wrapped.wrapped is fake
    assert not isinstance(
        next(t for t in agent.tools if t.tool_name == "other"), IdentityProtectedTool
    )
    # Direct execution (no hooks) is bound by the policy.
    assert _run(wrapped, tmp_path / "outside.md")["status"] == "error"
    assert _run(wrapped, ws / "inside.md")["status"] == "success"
    # Re-assigning (as the chat server does each turn) does not nest the same policy again.
    agent.tools = agent.tools
    assert next(t for t in agent.tools if t.tool_name == "file_write") is wrapped


def test_parent_policy_never_weakens_an_existing_wrapper(ws: Path) -> None:
    (ws / "sub").mkdir()
    inner = IdentityProtectedTool(
        FakeWriteTool("file_write"),
        policy=WritePathPolicy(base_dir=ws, write_roots=[str(ws / "sub")]),
    )
    agent = NXPAgent(
        model=FakeModel([text_turn("x")]),
        tools=[inner],
        write_policy=_policy(ws),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    outer = agent.tools[0]
    assert outer.wrapped is inner
    assert _run(outer, ws / "elsewhere.md")["status"] == "error"  # inner roots still hold
    assert _run(outer, ws / "sub" / "ok.md")["status"] == "success"


def test_agent_without_policy_keeps_tools_as_given() -> None:
    fake = FakeWriteTool("file_write")
    agent = NXPAgent(model=FakeModel([text_turn("x")]), tools=[fake], stimulus_classifier=Mock())
    assert agent.tools == (fake,)


# M5: memory writes are host-managed — a documented exception with its own boundary.


def test_memory_write_stays_inside_memory_dir_whatever_the_roots(tmp_path: Path) -> None:
    from stiracore.tools.memory_store import MemoryLayer, MemoryStore
    from stiracore.tools.memory_tools import create_memory_tools

    data = tmp_path / "stira"
    store = MemoryStore(data / "memory", "stira")
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    # Roots shrunk to a custom directory that excludes the memory store.
    policy = WritePathPolicy(
        base_dir=data,
        protected_dirs=[store.memory_dir / MemoryLayer.PROCEDURAL.value],
        write_roots=[str(elsewhere)],
    )
    file_tool = IdentityProtectedTool(FakeWriteTool("file_write"), policy=policy)
    assert _run(file_tool, store.memory_dir / "episodic" / "x.md")["status"] == "error"
    tools = {t.tool_name: t for t in create_memory_tools(store)}
    before = {p for p in tmp_path.rglob("*")}
    assert "written" in tools["memory_write"](layer="episodic", key="a/b", content="ok")
    for key in ("../procedural/x", "/etc/x", "a/../../x", "a\\b", ""):
        assert "failed" in tools["memory_write"](layer="episodic", key=key, content="x"), key
    assert "denied" in tools["memory_write"](layer="procedural", key="r", content="x")
    created = {p for p in tmp_path.rglob("*")} - before
    episodic = store.memory_dir / "episodic"
    # Only the entry (and its key directory) below episodic/, plus the store's own
    # SQLite index next to the layers (memory_dir/index.sqlite3[-journal|-wal|-shm]).
    index = store.memory_dir / "index.sqlite3"
    assert created and all(
        p == episodic / "a"
        or p.is_relative_to(episodic / "a")
        or (p.parent == store.memory_dir and p.name.startswith(index.name))
        for p in created
    ), created
    assert not list(episodic.rglob(".memory-*.tmp"))  # temp file renamed, none left
    assert not any((store.memory_dir / "procedural").iterdir())


# M3 (re-review): spelling variants of home are still "home".


def _home_variants(home: Path) -> list[Path]:
    upper = Path(str(home).upper())
    return [upper, upper.parent] if upper != home else []


def test_case_variant_of_home_is_refused(tmp_path: Path) -> None:
    home = Path.home().resolve()
    variants = _home_variants(home)
    if not variants:
        pytest.skip("home path has no case to vary")
    for variant in variants:
        assert broad_root_problem(variant), variant
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=tmp_path, write_roots=[str(variant)])
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=variant, write_roots=[WORKSPACE_ROOT])
    # Below home stays allowed.
    assert broad_root_problem(Path(str(home).upper()) / "projects" / "x") is None


def test_unicode_and_case_aliases_of_a_home_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real directory as home: NFD, case and symlink spellings are all refused."""
    nfc = unicodedata.normalize("NFC", "ホーム")
    home = tmp_path / "Users" / nfc
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)
    spellings = [
        home,
        tmp_path / "Users" / unicodedata.normalize("NFD", nfc),
        tmp_path / "USERS" / nfc,
        tmp_path / "users",
        link,
    ]
    for spelling in spellings:
        assert broad_root_problem(spelling), spelling
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=tmp_path / "ws", write_roots=[str(spelling)])
        with pytest.raises(ValueError, match="too broad"):
            WritePathPolicy(base_dir=spelling, write_roots=[WORKSPACE_ROOT])
    assert broad_root_problem(home / "work") is None
    assert broad_root_problem(tmp_path / "Users" / "other") is None
