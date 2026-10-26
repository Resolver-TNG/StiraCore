"""#190 / ADR 0010: Always/Ask/Never rules and the effective gate stack.

Pure stack logic (migration, monotonic contract, WARNING/CRITICAL display), the
settings APIs (confirmation, breaker refusal, CSRF), and the runtime gate through a
real NXPAgent turn on the offline FakeModel (Ask asks, Never never runs, approvals do
not lift the breaker or the floor, a rule changed while waiting is not used).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from flask import Flask
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.observability.security import install_security
from stiracore.tools import tool_policy as tp
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig
from tests.provider_fakes import FakeModel, text_turn, tool_turn

LEVELS = ("normal", "coherence", "warning", "critical", "unknown", None)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    snapshot = (dict(tool_registry.READ_TOOLS), dict(tool_registry.WRITE_TOOLS))
    yield
    for target, saved in zip(
        (tool_registry.READ_TOOLS, tool_registry.WRITE_TOOLS), snapshot, strict=True
    ):
        target.clear()
        target.update(saved)


def _names() -> list[str]:
    return sorted(set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS))


def _some(category: str) -> str:
    return next(n for n in _names() if tp.category_of(n) == category)


# --- Migration (acceptance 1) -------------------------------------------------


def test_pre_190_file_migrates_on_to_always_off_to_never_exec_to_ask(tmp_path):
    read, write = _some("read"), _some("write")
    path = tmp_path / "tool-config.json"
    path.write_text(
        json.dumps(
            {"mode": "custom", "enabled_tools": [read, write, "shell"], "shell_allowlist": []}
        ),
        encoding="utf-8",
    )
    config = ToolConfig.load(path)
    assert config.ask_tools == set()
    assert config.policy_of(read) == "always"
    assert config.policy_of(write) == "always"
    # #163: an exec-class ON never becomes an unconditional Always.
    assert config.policy_of("shell") == "ask"
    off = next(n for n in _names() if n not in {read, write, "shell"})
    assert config.policy_of(off) == "never"
    config.save()
    assert ToolConfig.load(path).as_dict()["ask_tools"] == []


def test_trust_all_migrates_to_always_except_exec_class(tmp_path):
    config = ToolConfig(mode="trust_all", path=tmp_path / "t.json")
    for name in _names():
        held = tool_registry.is_exec_tool(name) or name in tp.NO_ALWAYS_TOOLS  # ADR 0015
        expected = "ask" if held else "always"
        assert config.policy_of(name) == expected, name
    for name in tool_registry.EXEC_TOOL_NAMES:
        assert tp.rule_policy(config, name) == "ask"


def test_exec_tools_stay_unexposed_by_default_whatever_the_rule():
    config = ToolConfig(mode="trust_all")
    for name in set(tool_registry.EXEC_TOOL_NAMES) & set(_names()):
        row = tp.tool_stack(config, name, "normal", exposed=tool_registry.exec_tools_enabled())
        assert row["effective"]["decision"] == "forbidden"
        assert "floor" in row["effective"]["decided_by"]
        assert row["always_allowed"] is False


# --- Effective stack contract (acceptance 2) ---------------------------------


def test_effective_is_the_strictest_layer_and_lower_allow_never_overrides():
    for name, policy, level, exposed in itertools.product(
        _names(), tp.POLICIES, LEVELS, (True, False)
    ):
        config = ToolConfig(
            mode="custom",
            enabled_tools=set() if policy == "never" else {name},
            ask_tools={name} if policy == "ask" else set(),
        )
        row = tp.tool_stack(config, name, level, exposed=exposed)
        ranks = [tp.RANK[layer["decision"]] for layer in row["stack"].values()]
        assert tp.RANK[row["effective"]["decision"]] == max(ranks)
        assert row["effective"]["decided_by"], "some layer must be named"
        for key in row["effective"]["decided_by"]:
            assert row["stack"][key]["decision"] == row["effective"]["decision"]
            assert row["stack"][key]["reason"] in row["effective"]["reasons"]


@pytest.mark.parametrize("policy", tp.POLICIES)
def test_warning_is_at_least_ask_for_every_write_and_exec_tool(policy):
    for name in _names():
        if tp.category_of(name) == "read":
            continue
        config = ToolConfig(
            mode="custom",
            enabled_tools=set() if policy == "never" else {name},
            ask_tools={name} if policy == "ask" else set(),
        )
        row = tp.tool_stack(config, name, "warning", exposed=True)
        assert tp.RANK[row["effective"]["decision"]] >= tp.RANK["prompt"], name


@pytest.mark.parametrize("level", ["critical", "unknown", "halted"])
def test_critical_and_unknown_levels_show_every_tool_stopped_without_engine_facts(level):
    config = ToolConfig(mode="trust_all")
    for name in _names():
        row = tp.tool_stack(config, name, level, exposed=True)
        assert row["effective"]["decision"] == "forbidden", name
        assert "breaker" in row["effective"]["decided_by"]


@pytest.mark.parametrize(
    ("action", "write_decision"), [("halt", "prompt"), ("readonly", "forbidden")]
)
def test_critical_display_matches_what_the_engine_enforces(action, write_decision):
    """CRITICAL keeps reads (ADR 0002); halt asks a human for every write; readonly hides them."""
    config = ToolConfig(mode="trust_all")
    for name in _names():
        row = tp.tool_stack(config, name, "critical", exposed=True, critical_action=action)
        if tp.category_of(name) == "read":
            assert row["stack"]["breaker"]["decision"] == "allow", name
        else:
            assert row["stack"]["breaker"]["decision"] == write_decision, name
            assert tp.RANK[row["effective"]["decision"]] >= tp.RANK["prompt"], name


def test_hub_rows_carry_the_stack_for_the_live_breaker(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    for level, expected in (("warning", "prompt"), ("critical", "forbidden")):
        client = _client(store, level=level)
        rows = client.get("/api/config/hub").get_json()["tools"]
        for row in rows:
            if row["category"] == "read" and level == "warning":
                continue
            assert tp.RANK[row["effective"]["decision"]] >= tp.RANK[expected], row["name"]


# --- Isolation is its own field (acceptance 3) ---------------------------------


def test_isolation_is_reported_apart_from_the_approval_policy():
    write = "file_write" if "file_write" in _names() else _some("write")
    for policy in ("always", "ask"):
        config = ToolConfig(
            mode="custom", enabled_tools={write, "web_fetch"}, ask_tools={write, "web_fetch"}
        )
        if policy == "always":
            config.ask_tools = set()
        for name in (write, "web_fetch"):
            row = tp.tool_stack(config, name, "normal", exposed=True)
            assert row["isolation"]["os_sandbox"] is False
            assert "isolation" not in row["stack"]
            assert row["isolation"] == tp.isolation(name)  # does not depend on policy
    assert tp.isolation("file_write")["kind"] == "path_confined"
    assert tp.isolation("shell")["kind"] == "none"


# --- Floor is read-only (acceptance 4) ---------------------------------------


def test_floor_is_listed_read_only_and_not_editable_from_the_hub(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    ids = {item["id"] for item in state["floor"]}
    assert ids == {"identity_read_only", "os_protection", "exec_exposure"}
    assert all(item["approvable"] is False for item in state["floor"])
    assert "SOUL.md" in next(i for i in state["floor"] if i["id"] == "identity_read_only")["files"]
    refused = client.post(
        "/api/config/hub", json={"changes": {"floor": {"approvable": True}}, "base_version": None}
    )
    assert refused.status_code == 400
    assert refused.get_json()["errors"][0]["code"] == "unknown_field"


# --- Persistent grants (acceptance 5) ----------------------------------------


def test_always_grant_states_scope_expiry_and_revocation():
    read = _some("read")
    row = tp.tool_stack(
        ToolConfig(mode="custom", enabled_tools={read}), read, "normal", exposed=True
    )
    assert row["grant"] == {
        "tool": read,
        "scope": "tool_any_arguments",
        "applies_to": "tool-config.json",
        "expires": "until_revoked",
        "revoke": "/settings#sec-tools",
        "still_gated_by": ["breaker", "floor"],
    }
    asked = ToolConfig(mode="custom", enabled_tools={read}, ask_tools={read})
    assert tp.tool_stack(asked, read, "normal", exposed=True)["grant"] is None
    assert tp.persistent_grant("shell", "always") is None


def test_exec_tools_cannot_be_made_always_through_the_api(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    client = _client(store)
    for name in set(tool_registry.EXEC_TOOL_NAMES) & set(_names()):
        response = client.post(
            "/api/config/tools", json={"name": name, "policy": "always", "confirm": True}
        )
        assert response.status_code == 400
        assert response.get_json()["error_code"] == "always_not_allowed"


# --- Settings APIs: confirmation, breaker, audit, CSRF ---------------------------


def _client(store, level="normal", security=False):
    app = Flask(__name__)
    if security:
        install_security(app)
    app.register_blueprint(create_config_blueprint(store, lambda: level))
    return app.test_client()


def _custom_store(tmp_path, enabled=(), ask=()):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(
        mode="custom", enabled_tools=set(enabled), ask_tools=set(ask), path=store.tool_path
    ).save()
    return store


def test_tightening_needs_no_confirmation_but_loosening_does(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    client = _client(store)
    tighter = client.post("/api/config/tools", json={"name": read, "policy": "ask"})
    assert tighter.status_code == 200
    assert store.load_tools().policy_of(read) == "ask"

    looser = client.post("/api/config/tools", json={"name": read, "policy": "always"})
    assert looser.status_code == 400
    assert looser.get_json()["needs_confirmation"] == ["tool_policy_loosened"]
    assert store.load_tools().policy_of(read) == "ask"

    confirmed = client.post(
        "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
    )
    assert confirmed.status_code == 200
    assert store.load_tools().policy_of(read) == "always"
    # Audit: both saves are in the settings history with their source.
    history = hub.read_history(store.history_path, 10)
    assert [entry["source"] for entry in history] == ["api:tools", "api:tools"]


@pytest.mark.parametrize("level", ["warning", "critical", "coherence", "unknown"])
def test_no_loosening_while_the_breaker_is_active_even_when_confirmed(tmp_path, level):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    client = _client(store, level=level)
    for url, body in (
        ("/api/config/tools", {"name": read, "policy": "always", "confirm": True}),
        ("/api/config/tools", {"name": _some("write"), "enabled": True, "confirm": True}),
        ("/api/tools/config", {"ask_tools": [], "confirm": True}),
    ):
        response = client.post(url, json=body)
        assert response.status_code == 403, url
        assert response.get_json()["error_code"] == "breaker_active"
    version = client.get("/api/config/hub").get_json()["version"]
    hub_post = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {"ask_tools": []}},
            "base_version": version,
            "acknowledged_dangers": ["tool_policy_loosened"],
        },
    )
    assert hub_post.status_code == 403
    assert hub_post.get_json()["error_code"] == "breaker_active"
    assert store.load_tools().policy_of(read) == "ask"
    # Tightening during a breaker stays possible.
    never = client.post("/api/config/tools", json={"name": read, "policy": "never"})
    assert never.status_code == 200
    assert store.load_tools().policy_of(read) == "never"


def test_hub_loosening_needs_the_typed_confirmation_code(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    client = _client(store)
    version = client.get("/api/config/hub").get_json()["version"]
    body = {"changes": {"tools": {"ask_tools": []}}, "base_version": version}
    refused = client.post("/api/config/hub", json=body)
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["tool_policy_loosened"]
    saved = client.post(
        "/api/config/hub", json={**body, "acknowledged_dangers": ["tool_policy_loosened"]}
    )
    assert saved.status_code == 200
    assert store.load_tools().policy_of(read) == "always"


def test_rollback_that_loosens_is_refused_during_a_breaker(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    normal = _client(store)
    assert (
        normal.post("/api/config/tools", json={"name": read, "policy": "never"}).status_code == 200
    )
    entry = normal.get("/api/config/history").get_json()["entries"][0]
    warning = _client(store, level="warning")
    version = warning.get("/api/config/hub").get_json()["version"]
    response = warning.post(
        f"/api/config/history/{entry['id']}/rollback",
        json={"base_version": version, "acknowledged_dangers": ["tool_policy_loosened"]},
    )
    assert response.status_code == 403
    assert store.load_tools().policy_of(read) == "never"


def test_preset_never_turns_ask_into_always():
    current = hub.default_values()
    read = _some("read")
    current["tools"]["ask_tools"] = [read]
    for preset_id in hub.PRESET_IDS:
        preset = hub.preset_values(preset_id, current)
        assert read in preset["tools"]["ask_tools"]


def test_policy_updates_need_the_csrf_token_and_same_origin(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    client = _client(store, security=True)
    body = {"name": read, "policy": "never"}
    no_token = client.post("/api/config/tools", json=body, headers={"X-Test-No-Csrf": "1"})
    assert no_token.status_code == 403
    token = client.get("/api/csrf").get_json()["token"]
    cross = client.post(
        "/api/config/tools",
        json=body,
        headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
    )
    assert cross.status_code == 403
    assert store.load_tools().policy_of(read) == "always"
    ok = client.post("/api/config/tools", json=body, headers={"X-CSRF-Token": token})
    assert ok.status_code == 200
    assert store.load_tools().policy_of(read) == "never"


# --- Runtime gate through a real NXPAgent turn --------------------------------


class RecordingTool(AgentTool):
    def __init__(self, name: str, calls: list) -> None:
        super().__init__()
        self._name = name
        self.calls = calls

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> Any:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        return {"name": self._name, "description": "records", "inputSchema": {"json": schema}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        self.calls.append(tool_use["input"].get("path"))
        yield ToolResultEvent(
            {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": "ok"}]}
        )


STABILITY = {"normal": 0.9, "warning": 0.4, "critical": 0.1}


def _turn(policy, *, kind="read", level="normal", hitl=None, path="notes/x.md", read_only=()):
    name = f"policy_probe_{kind}"
    calls: list = []
    tool = RecordingTool(name, calls)
    (tool_registry.READ_TOOLS if kind == "read" else tool_registry.WRITE_TOOLS)[name] = tool
    model = FakeModel([tool_turn(name, {"path": path}), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=STABILITY[level]),
        config=StiraConfig(critical_action="halt"),
        tools=[tool],
        on_hitl_request=hitl,
        read_only_paths=read_only,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=(
            None
            if policy == "no-reader"
            else policy
            if callable(policy)
            else (lambda _name: policy)
        ),
    )
    agent("go")
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    text = results[0]["content"][0].get("text", "") if results else ""
    return calls, text


def _approve() -> Mock:
    return Mock(return_value=HITLDecision(HITLAction.APPROVE))


def test_always_runs_without_asking():
    hitl = _approve()
    calls, _ = _turn("always", hitl=hitl)
    assert calls == ["notes/x.md"]
    assert not hitl.called


def test_never_is_refused_at_the_call_even_if_the_tool_is_offered():
    hitl = _approve()
    calls, text = _turn("never", hitl=hitl)
    assert calls == []
    assert "Never" in text
    assert not hitl.called


@pytest.mark.parametrize("kind", ["read", "write"])
def test_ask_asks_every_call_and_runs_only_when_approved(kind):
    hitl = _approve()
    calls, _ = _turn("ask", kind=kind, hitl=hitl)
    assert calls == ["notes/x.md"]
    request = hitl.call_args.args[0]
    assert "Ask" in request.reason
    assert request.tool_name == f"policy_probe_{kind}"

    reject = Mock(return_value=HITLDecision(HITLAction.REJECT, reason="no"))
    calls, text = _turn("ask", kind=kind, hitl=reject)
    assert calls == [] and "HITL rejected" in text


def test_ask_without_an_approver_fails_closed():
    calls, text = _turn("ask", hitl=None)
    assert calls == []
    assert "no approver" in text


def test_an_unreadable_rule_counts_as_never():
    def broken(_name):
        raise OSError("tool-config.json unreadable")

    hitl = _approve()
    calls, text = _turn(broken, hitl=hitl)
    assert calls == [] and "Never" in text
    assert not hitl.called
    calls, _ = _turn(lambda _name: "sometimes", hitl=hitl)
    assert calls == []


@pytest.mark.parametrize("after", ["never", "always"])
def test_a_rule_changed_while_waiting_invalidates_the_approval(after):
    """Generation check: approve under Ask, rule saved as something else before return."""
    rule = {"value": "ask"}

    def approve_after_change(_request):
        rule["value"] = after
        return HITLDecision(HITLAction.APPROVE)

    calls, text = _turn(lambda _name: rule["value"], hitl=Mock(side_effect=approve_after_change))
    assert calls == []
    assert "changed while waiting" in text


def test_an_ask_approval_never_lifts_the_breaker_write_block():
    """Attacker view: at WARNING a write-capable Ask tool is not even sent for approval."""
    hitl = _approve()
    calls, _ = _turn("ask", kind="write", level="warning", hitl=hitl)
    assert calls == []
    assert not hitl.called


def test_an_ask_approval_never_lifts_the_identity_floor(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("me", encoding="utf-8")
    hitl = _approve()
    calls, text = _turn("ask", kind="write", hitl=hitl, path=str(soul), read_only=(str(soul),))
    assert calls == []
    assert "read-only" in text
    assert not hitl.called  # no futile prompt: an approval cannot lift the floor


def test_without_a_policy_reader_the_old_gate_is_unchanged():
    hitl = _approve()
    calls, _ = _turn("no-reader", hitl=hitl)
    assert calls == ["notes/x.md"] and not hitl.called
    calls, _ = _turn(None, hitl=hitl)  # a reader answering None: not a rule, fail closed
    assert calls == [] and not hitl.called


def test_stira_policy_reader_reads_the_saved_rule_live(tmp_path):
    from dashboard.stira_server import _tool_policy_reader

    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    policy = _tool_policy_reader(store)
    first = policy(read)
    assert first[0] == "ask"
    ToolConfig(mode="custom", enabled_tools=set(), path=store.tool_path).save()
    assert policy(read)[0] == "never"
    assert policy(read)[1] != first[1]  # every save is a new generation
    assert policy("memory_write_not_in_registry")[0] == "always"  # left to breaker/HITL
    Path(store.tool_path).write_text("{not json", encoding="utf-8")
    assert policy(read)[0] == "never"  # malformed: locked
    assert policy("memory_write_not_in_registry")[0] == "never"  # no exception while locked


# --- ADR 0015 phase 1 (#449, review M3): page_check is Ask or Never only -------------


def test_page_check_cannot_be_always_through_any_save(tmp_path):
    tool_registry.WRITE_TOOLS["page_check"] = RecordingTool("page_check", [])
    assert tp.always_allowed("page_check") is False
    store = _custom_store(tmp_path)
    client = _client(store)
    refused = client.post(
        "/api/config/tools",
        json={
            "name": "page_check",
            "policy": "always",
            "confirm": True,
            "confirm_always": ["page_check"],
        },
    )
    assert refused.status_code == 400
    assert refused.get_json()["error_code"] == "always_not_allowed"
    # Turned on without a policy (the old toggle): written as Ask.
    on = client.post(
        "/api/config/tools", json={"name": "page_check", "enabled": True, "confirm": True}
    )
    assert on.status_code == 200, on.get_json()
    assert "page_check" in store.load_tools().ask_tools
    # The hub: dropping it from ask_tools still saves Ask.
    version = client.get("/api/config/hub").get_json()["version"]
    hub_save = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {"ask_tools": []}},
            "base_version": version,
            "acknowledged_dangers": ["tool_policy_loosened"],
            "confirm_always": ["page_check"],
        },
    )
    assert hub_save.status_code == 200, hub_save.get_json()
    saved = store.load_tools()
    assert "page_check" in saved.ask_tools and saved.policy_of("page_check") == "ask"
    rows = client.get("/api/config/hub").get_json()["tools"]
    (row,) = [r for r in rows if r["name"] == "page_check"]
    assert (row["policy"], row["always_allowed"], row["grant"]) == ("ask", False, None)


def test_a_saved_always_for_page_check_is_read_as_ask_and_asks(tmp_path, caplog, monkeypatch):
    from dashboard.stira_server import _tool_policy_reader

    monkeypatch.setattr(tp, "_WARNED_ALWAYS", set())
    store = _custom_store(tmp_path, enabled={"page_check"})  # Always, as phase 0 allowed
    tool_registry.WRITE_TOOLS["page_check"] = RecordingTool("page_check", [])
    with caplog.at_level("WARNING", logger=tp.__name__):
        assert tp.warn_saved_always(store.load_tools()) == ["page_check"]
        assert _tool_policy_reader(store)("page_check")[0] == "ask"
        assert ToolConfig(mode="trust_all").policy_of("page_check") == "ask"
    warnings = [r for r in caplog.records if "page_check" in r.getMessage()]
    assert len(warnings) == 1  # once per process
    assert tp.tool_stack(store.load_tools(), "page_check", "normal", exposed=True)["grant"] is None

    # At run time the rule read from that file puts a card in front of the call.
    calls: list = []
    tool = RecordingTool("page_check", calls)
    tool_registry.WRITE_TOOLS["page_check"] = tool
    reader = _tool_policy_reader(store)
    reject = Mock(return_value=HITLDecision(HITLAction.REJECT, reason="no"))
    agent = NXPAgent(
        model=FakeModel([tool_turn("page_check", {"path": "a.html"}), text_turn("done")]),
        state=NeuralState(stability=STABILITY["normal"]),
        config=StiraConfig(critical_action="halt"),
        tools=[tool],
        on_hitl_request=reject,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=reader,
    )
    agent("go")
    assert reject.called and reject.call_args.args[0].tool_name == "page_check"
    assert calls == []


def test_stira_breaker_transition_keeps_never_tools_out(tmp_path, monkeypatch):
    """Before #190 a mid-turn breaker transition rebuilt the tool list unfiltered."""
    from dashboard import stira_server

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(agent=self, on_breaker=on_breaker, kwargs=kwargs)

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    governed = set(_names())
    victim = "file_write" if "file_write" in governed else _some("write")
    ToolConfig(mode="custom", enabled_tools=governed - {victim}, path=store.tool_path).save()
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    stira_server.create_app(store)
    policy = captured["kwargs"]["tool_policy"]
    assert policy(victim)[0] == "never"
    for level in ("normal", "warning", "critical"):
        captured["on_breaker"]({"level": level})
        names = {stira_server._registered_tool_name(t) for t in captured["agent"].tools}
        assert victim not in names, level


def test_ask_tools_accepts_registry_names_only(tmp_path):
    """No free text (e.g. a pasted secret) reaches tool-config.json or the history."""
    store = _custom_store(tmp_path, enabled={_some("read")})
    client = _client(store)
    for bad in (["sk-live-1234567890abcdef"], "file_read", [1]):
        response = client.post("/api/tools/config", json={"ask_tools": bad})
        assert response.status_code == 400
    assert store.load_tools().ask_tools == set()
    assert not Path(store.history_path).exists()


def _broker_turn(rule: dict, *, change_to: str | None):
    """Ask through the real HITLApprovalBroker; the operator approves what was shown."""
    import threading
    import time

    from stiracore.observability.chat_api import HITLApprovalBroker

    broker = HITLApprovalBroker()
    shown: dict = {}

    def operator():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = broker.pending()
            if pending:
                shown.update(pending[0])
                if change_to is not None:
                    rule["value"] = change_to  # someone saves another rule meanwhile
                broker.decide(
                    pending[0]["id"],
                    HITLDecision(HITLAction.APPROVE),
                    args_hash=pending[0]["args_hash"],
                )
                return
            time.sleep(0.01)

    worker = threading.Thread(target=operator)
    worker.start()
    try:
        calls, text = _turn(lambda _name: rule["value"], hitl=broker.request)
    finally:
        worker.join()
        broker.close()
    return calls, text, shown


def test_ask_through_the_real_approval_queue_shows_why_and_runs_once():
    calls, _, shown = _broker_turn({"value": "ask"}, change_to=None)
    assert calls == ["notes/x.md"]
    assert "Ask" in shown["reason"]
    assert shown["break_level"] == "normal"


def test_real_queue_approval_after_a_rule_change_is_not_used():
    calls, text, shown = _broker_turn({"value": "ask"}, change_to="never")
    assert shown, "the request reached the queue"
    assert calls == [] and "changed while waiting" in text


# === Review round 1 (#269): reproductions first, then the fixes =================


def _store_reader(store):
    """(rule, generation) straight from the saved file, like the chat server reader."""

    def read(name):
        config = store.load_tools()
        return ("never" if config.integrity_error else config.policy_of(name), config.generation)

    return read


def _save_rules(store, enabled, ask=()):
    ToolConfig(
        mode="custom", enabled_tools=set(enabled), ask_tools=set(ask), path=store.tool_path
    ).save()


# --- B1: generation, not value ------------------------------------------------


def test_b1_ask_never_ask_while_waiting_does_not_revive_the_old_approval(tmp_path):
    name = "policy_probe_read"
    store = _custom_store(tmp_path, enabled={name}, ask={name})

    def approve_after_revoke_and_restore(_request):
        _save_rules(store, set())  # Never: revokes
        _save_rules(store, {name}, {name})  # back to Ask: same value, newer generation
        return HITLDecision(HITLAction.APPROVE)

    calls, text = _turn(
        _store_reader(store), hitl=Mock(side_effect=approve_after_revoke_and_restore)
    )
    assert calls == []
    assert "changed while waiting" in text


def test_b1_the_execution_boundary_rechecks_the_generation(tmp_path):
    """A save between the gate and the final verifier is caught by the verifier."""
    from strands.hooks import BeforeToolCallEvent, HookProvider

    name = "policy_probe_read"
    store = _custom_store(tmp_path, enabled={name}, ask={name})

    class SaveInBetween(HookProvider):
        def register_hooks(self, registry, **kwargs):
            registry.add_callback(BeforeToolCallEvent, self.save)

        def save(self, event):
            _save_rules(store, {name}, {name})

    calls: list = []
    tool = RecordingTool(name, calls)
    tool_registry.READ_TOOLS[name] = tool
    model = FakeModel([tool_turn(name, {"path": "x"}), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        config=StiraConfig(critical_action="halt"),
        tools=[tool],
        on_hitl_request=_approve(),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=_store_reader(store),
        extra_hooks=[SaveInBetween()],
    )
    agent("go")
    assert calls == []


def test_b1_rule_change_revokes_open_durable_approvals(tmp_path):
    from dashboard.stira_server import _tool_policy_reader
    from stiracore.core.approval_store import ApprovalStore

    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    approvals = ApprovalStore(None)
    marker = tmp_path / "marker.json"
    reader = _tool_policy_reader(store, approvals, marker)
    reader.sync()  # the server start records the generation of the open approvals
    approvals.submit(
        request_id="r1", agent_id="stira", tool_name=read, tool_input={}, expires_at=1e12
    )
    approvals.decide(
        "r1", "approve", args_hash=approvals.get("r1").args_hash, approver="local:operator"
    )
    approvals.submit(
        request_id="r2", agent_id="stira", tool_name=read, tool_input={"a": 1}, expires_at=1e12
    )
    reader(read)  # same generation: nothing revoked
    assert approvals.get("r1").status == "approved" and approvals.get("r2").status == "pending"
    _save_rules(store, set())
    _save_rules(store, {read}, {read})  # Ask again (value equal, generation newer)
    reader(read)
    assert approvals.get("r1").status == "cancelled"
    assert approvals.get("r2").status == "cancelled"


def test_b1_a_failed_revocation_fails_closed(tmp_path):
    from dashboard.stira_server import _tool_policy_reader

    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    stuck = Mock()
    stuck.open_records.return_value = [Mock(request_id="r1", status="pending", consumed=False)]
    stuck.get.return_value = Mock(status="pending", consumed=False)  # cancel did nothing
    reader = _tool_policy_reader(store, stuck, tmp_path / "marker.json")
    with pytest.raises(RuntimeError):
        reader(read)
    hitl = _approve()
    calls, text = _turn(reader, hitl=hitl)
    assert calls == [] and "Never" in text and not hitl.called


# --- B2: rollback / missing field / deletion lock the rules ---------------------


def _write_raw(store, data):
    Path(store.tool_path).write_text(json.dumps(data), encoding="utf-8")


def test_b2_first_read_of_a_pre_190_file_migrates_once_and_records_a_head(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    read = _some("read")
    legacy = {"mode": "custom", "enabled_tools": [read], "shell_allowlist": []}
    _write_raw(store, legacy)
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "always"
    saved = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2 and saved["generation"] == 1
    assert Path(str(store.tool_path) + ".head").exists()
    _write_raw(store, legacy)  # the same pre-#190 file restored later: not a migration
    assert store.load_tools().integrity_error == "rolled_back_to_legacy"


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("legacy", "rolled_back_to_legacy"),
        ("drop_ask_tools", "incomplete"),
        ("older_copy", "generation_rollback"),
        ("delete", "missing"),
        ("edit", "edited"),
        ("delete_head", "head_missing"),
    ],
)
def test_b2_tampered_rules_lock_every_tool_never(tmp_path, tamper, reason):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})  # generation 1: Always
    old_copy = Path(store.tool_path).read_text(encoding="utf-8")
    _save_rules(store, {read}, {read})  # generation 2: Ask
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    if tamper == "legacy":
        _write_raw(store, {k: data[k] for k in ("mode", "enabled_tools", "shell_allowlist")})
    elif tamper == "drop_ask_tools":
        data.pop("ask_tools")
        _write_raw(store, data)
    elif tamper == "older_copy":
        Path(store.tool_path).write_text(old_copy, encoding="utf-8")
    elif tamper == "delete":
        Path(store.tool_path).unlink()
    elif tamper == "edit":
        data["ask_tools"] = []
        _write_raw(store, data)
    elif tamper == "delete_head":
        Path(str(store.tool_path) + ".head").unlink()
    config = store.load_tools()
    assert config.integrity_error == reason
    assert all(config.policy_of(name) == "never" for name in _names())
    hitl = _approve()
    calls, _ = _turn(_store_reader(store), hitl=hitl)
    assert calls == [] and not hitl.called
    rows = _client(store).get("/api/config/hub").get_json()
    assert rows["tools_locked"] == reason
    assert all(row["effective"]["decision"] == "forbidden" for row in rows["tools"])


def test_b2_deleting_file_and_head_with_a_history_still_locks(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    client = _client(store)
    assert (
        client.post("/api/config/tools", json={"name": read, "policy": "never"}).status_code == 200
    )
    Path(store.tool_path).unlink()
    Path(str(store.tool_path) + ".head").unlink()
    assert store.load_tools().integrity_error == "missing"


def test_b2_a_human_save_from_the_settings_page_recovers_a_locked_config(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    Path(str(store.tool_path) + ".head").unlink()
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    assert state["tools_locked"] == "head_missing"
    body = {
        "changes": {"tools": {"enabled_tools": [read]}},
        "base_version": state["version"],
        "acknowledged_dangers": ["tool_policy_loosened"],
    }
    refused = client.post("/api/config/hub", json=body)
    assert refused.status_code == 409
    assert refused.get_json()["error_code"] == "tool_rules_locked"
    saved = client.post("/api/config/hub", json={**body, "recover": "RECOVER"})
    assert saved.status_code == 200, saved.get_json()
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "always"


def test_b2_unreadable_rules_lock_the_stira_tool_list(tmp_path, monkeypatch):
    from dashboard import stira_server

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(agent=self, on_breaker=on_breaker)

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    Path(store.tool_path).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    stira_server.create_app(store)
    captured["on_breaker"]({"level": "normal"})
    names = {stira_server._registered_tool_name(t) for t in captured["agent"].tools}
    assert not names & set(_names())


# --- Major (a): display == behaviour at CRITICAL, breaker worsening while waiting ---


def test_a_critical_halt_runs_reads_and_asks_for_every_write():
    hitl = _approve()
    calls, _ = _turn("always", kind="read", level="critical", hitl=hitl)
    assert calls == ["notes/x.md"] and not hitl.called  # display: read allow
    calls, _ = _turn("always", kind="write", level="critical", hitl=hitl)
    assert calls == ["notes/x.md"] and hitl.called  # display: write prompt
    assert "CRITICAL" in hitl.call_args.args[0].reason
    calls, _ = _turn("always", kind="write", level="critical", hitl=None)
    assert calls == []  # no approver: refused


def test_a_breaker_worsening_while_waiting_voids_the_approval():
    for target in (0.4, 0.1):  # NORMAL -> WARNING / CRITICAL during the wait
        holder: dict = {}

        def worsen(request, target=target, holder=holder):
            holder["agent"].state.stability = target
            return HITLDecision(HITLAction.APPROVE)

        name = "policy_probe_read"
        calls: list = []
        tool = RecordingTool(name, calls)
        tool_registry.READ_TOOLS[name] = tool
        agent = NXPAgent(
            model=FakeModel([tool_turn(name, {"path": "x"}), text_turn("done")]),
            state=NeuralState(stability=0.9),
            config=StiraConfig(critical_action="halt"),
            tools=[tool],
            on_hitl_request=Mock(side_effect=worsen),
            stimulus_classifier=Mock(classify=lambda **kwargs: None),
            tool_policy=lambda _name: ("ask", 1),
        )
        holder["agent"] = agent
        agent("go")
        assert calls == [], target


def test_a_hub_rows_use_the_engine_critical_action(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    app = Flask(__name__)
    app.register_blueprint(
        create_config_blueprint(store, lambda: "critical", critical_action_provider=lambda: "halt")
    )
    rows = app.test_client().get("/api/config/hub").get_json()["tools"]
    by_category = {row["category"]: row["stack"]["breaker"]["decision"] for row in rows}
    assert by_category["read"] == "allow" and by_category["write"] == "prompt"


# --- Major (b)/(c): breaker re-checked at commit, audit before loosening -----------


def test_b_breaker_tripping_between_check_and_commit_refuses_the_loosening(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    levels = iter(["normal"] + ["warning"] * 20)
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: next(levels)))
    response = app.test_client().post(
        "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
    )
    assert response.status_code == 403
    assert response.get_json()["error_code"] == "breaker_active"
    assert store.load_tools().policy_of(read) == "ask"
    sources = [entry["source"] for entry in hub.read_history(store.history_path, 10)]
    assert sources[0].startswith("aborted:") and sources[1] == "api:tools"


def test_c_loosening_is_refused_when_the_audit_cannot_be_written(tmp_path, monkeypatch):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hub, "append_history", disk_full)
    client = _client(store)
    looser = client.post(
        "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
    )
    assert looser.status_code == 500
    assert looser.get_json()["error_code"] == "audit_failed"
    assert store.load_tools().policy_of(read) == "ask"
    version = client.get("/api/config/hub").get_json()["version"]
    hub_looser = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {"ask_tools": []}},
            "base_version": version,
            "acknowledged_dangers": ["tool_policy_loosened"],
        },
    )
    assert hub_looser.status_code == 500 and store.load_tools().policy_of(read) == "ask"
    tighter = client.post("/api/config/tools", json={"name": read, "policy": "never"})
    assert tighter.status_code == 200  # tightening is never blocked by the audit
    assert store.load_tools().policy_of(read) == "never"


# --- Major (d): rules never change the tool definitions the model sees -------------


def test_d_rules_do_not_change_tool_specs_or_static_system_prompt():
    name = "policy_probe_read"
    calls: list = []
    tool = RecordingTool(name, calls)
    tool_registry.READ_TOOLS[name] = tool
    rule = {"value": ("always", 1)}
    model = FakeModel([text_turn("a"), text_turn("b"), text_turn("c")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        tools=[tool],
        system_prompt="static",
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda _name: rule["value"],
    )
    for value in (("always", 1), ("ask", 2), ("never", 3)):
        rule["value"] = value
        agent("hi")
    specs = [request["tool_names"] for request in model.requests]
    systems = [request.get("system_prompt") for request in model.requests]
    assert specs[0] == specs[1] == specs[2] == [name]
    assert systems[0] == systems[1] == systems[2]


def test_d_ask_and_always_produce_the_same_stira_tool_list():
    tools = [Mock(tool_name=n) for n in _names()]
    always = ToolConfig(mode="custom", enabled_tools=set(_names()))
    ask = ToolConfig(mode="custom", enabled_tools=set(_names()), ask_tools=set(_names()))
    name_of = lambda t: t.tool_name
    assert hub.filter_tools_by_config(tools, always, name_of) == hub.filter_tools_by_config(
        tools, ask, name_of
    )


# --- Major (e): sub-agents get the parent's rules ---------------------------------


def test_e_a_child_agent_inherits_the_parent_rules():
    from stiracore.agent.subagent import create_nxp_subagent

    name = "policy_probe_write"
    calls: list = []
    tool = RecordingTool(name, calls)
    tool_registry.WRITE_TOOLS[name] = tool
    parent = NXPAgent(
        model=FakeModel([text_turn("p")]),
        state=NeuralState(stability=0.9),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda _name: ("never", 1),
    )
    child = create_nxp_subagent(parent, "do it", tools=[tool])
    child.model = FakeModel([tool_turn(name, {"path": "x"}), text_turn("done")])
    child.agent.model = child.model
    assert child.checkpoint_hooks.policy_check is parent.checkpoint_hooks.policy_check
    child()
    assert calls == []


# --- Major (f): making a write/external tool Always needs its name typed -----------


def test_f_always_for_a_write_tool_needs_the_typed_name(tmp_path):
    write = _some("write")
    store = _custom_store(tmp_path, enabled={write}, ask={write})
    client = _client(store)
    body = {"name": write, "policy": "always", "confirm": True}
    refused = client.post("/api/config/tools", json=body)
    assert refused.status_code == 400
    assert refused.get_json()["needs_tool_names"] == [write]
    wrong = client.post("/api/config/tools", json={**body, "confirm_always": ["other"]})
    assert wrong.status_code == 400
    ok = client.post("/api/config/tools", json={**body, "confirm_always": [write]})
    assert ok.status_code == 200 and store.load_tools().policy_of(write) == "always"


def test_f_hub_always_for_a_write_tool_needs_the_typed_name(tmp_path):
    write = _some("write")
    store = _custom_store(tmp_path, enabled={write}, ask={write})
    client = _client(store)
    version = client.get("/api/config/hub").get_json()["version"]
    body = {
        "changes": {"tools": {"ask_tools": []}},
        "base_version": version,
        "acknowledged_dangers": ["tool_policy_loosened"],
    }
    refused = client.post("/api/config/hub", json=body)
    assert refused.status_code == 400 and refused.get_json()["needs_tool_names"] == [write]
    ok = client.post("/api/config/hub", json={**body, "confirm_always": [write]})
    assert ok.status_code == 200


# --- Minor: CSRF on every rule-changing route -----------------------------------


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/api/config/tools", {"name": "current_time", "policy": "never"}),
        ("/api/tools/config", {"ask_tools": []}),
        ("/api/config/hub", {"changes": {}}),
        ("/api/config/history/x/rollback", {}),
    ],
)
def test_minor_every_rule_route_needs_the_csrf_token(tmp_path, url, body):
    store = _custom_store(tmp_path, enabled={_some("read")})
    client = _client(store, security=True)
    response = client.post(url, json=body, headers={"X-Test-No-Csrf": "1"})
    assert response.status_code == 403


# === Review round 2 (#269) =======================================================


def _stira_app(tmp_path, monkeypatch, store=None):
    from dashboard import stira_server

    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(agent=self, on_breaker=on_breaker, kwargs=kwargs)

    data_dir = tmp_path / "stira"
    store = store or ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    stira_server.create_app(store)
    return captured, store, data_dir


# --- (0) the agent cannot write the files that decide what it may do ----------------


def test_0_policy_files_are_in_the_write_floor_and_no_approval_lifts_it(tmp_path, monkeypatch):
    from stiracore.core import StiraEngine
    from stiracore.middleware.identity_guard import IdentityGuard

    captured, store, data_dir = _stira_app(tmp_path, monkeypatch)
    kwargs = captured["kwargs"]
    policy = kwargs["write_policy"]
    targets = sorted(store.protected_files()) + [
        str(data_dir / "tool-policy-approvals.json"),
        str(data_dir / "hitl-approvals.db"),
    ]
    guard = IdentityGuard(
        StiraEngine(), read_only_paths=kwargs["read_only_paths"], path_policy=policy
    )
    for target in targets:
        assert policy.is_read_only(target), target
        for tool, args in (
            ("file_write", {"path": target, "content": "{}"}),
            ("editor", {"command": "create", "path": target, "file_text": "{}"}),
        ):
            result = guard.check_tool_call(tool, args, hitl_approved=True)
            assert not result.allowed and result.layer == 3, (tool, target)


def test_0_an_approved_file_write_cannot_rewrite_the_tool_rules(tmp_path, monkeypatch):
    captured, store, _ = _stira_app(tmp_path, monkeypatch)
    _save_rules(store, {"file_write"}, {"file_write"})
    before = Path(store.tool_path).read_text(encoding="utf-8")
    kwargs = captured["kwargs"]
    tools = tool_registry.get_tools(
        "normal", read_only_paths=kwargs["read_only_paths"], write_policy=kwargs["write_policy"]
    )
    file_write = next(t for t in tools if getattr(t, "tool_name", None) == "file_write")
    hitl = _approve()
    model = FakeModel(
        [
            tool_turn(
                "file_write", {"path": str(store.tool_path), "content": '{"mode":"trust_all"}'}
            ),
            text_turn("done"),
        ]
    )
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        config=StiraConfig(critical_action="halt"),
        tools=[file_write],
        on_hitl_request=hitl,
        read_only_paths=kwargs["read_only_paths"],
        write_policy=kwargs["write_policy"],
        stimulus_classifier=Mock(classify=lambda **kw: None),
        tool_policy=_store_reader(store),
    )
    agent("go")
    assert Path(store.tool_path).read_text(encoding="utf-8") == before
    assert not hitl.called  # the floor refuses before asking: no approval could lift it


# --- B1: only a genuinely new install gets defaults, and they are not "everything" ---


def test_b1_a_new_install_is_not_everything_always(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    config = store.load_tools()
    assert config.integrity_error is None and config.mode == "custom"
    policies = {name: config.policy_of(name) for name in _names()}
    assert set(policies.values()) != {"always"}
    for name, policy in policies.items():
        if tp.category_of(name) == "read":
            assert policy == "always", name
        elif name in tp.EXTERNAL_SEND_TOOLS or tool_registry.is_exec_tool(name):
            assert policy == "never", name
        else:
            assert policy == "ask", name


def test_b1_an_unreadable_history_is_an_integrity_error_not_a_first_run(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    Path(store.history_path).mkdir()  # exists but cannot be read
    _write_raw(store, {"mode": "trust_all", "enabled_tools": [], "shell_allowlist": []})
    config = store.load_tools()
    assert config.integrity_error == "history_unreadable"
    assert all(config.policy_of(name) == "never" for name in _names())


# --- B2: a hand-edited generation N+1 is not "an interrupted save" ------------------


def test_b2_a_looser_generation_n_plus_1_stays_locked(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data.update(generation=data["generation"] + 1, mode="trust_all", ask_tools=[])
    _write_raw(store, data)
    config = store.load_tools()
    assert config.integrity_error == "unconfirmed_generation"
    assert config.policy_of(read) == "never"


def test_b2_a_stricter_interrupted_save_is_completed(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data.update(generation=data["generation"] + 1, ask_tools=[read])  # Always -> Ask
    _write_raw(store, data)
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "ask"
    head = json.loads(Path(str(store.tool_path) + ".head").read_text(encoding="utf-8"))
    assert head["generation"] == data["generation"]


def test_b2_a_head_without_a_snapshot_or_a_failed_repair_stays_locked(tmp_path, monkeypatch):
    read = _some("read")
    for case in ("no_snapshot", "repair_fails"):
        root = tmp_path / case
        root.mkdir()
        store = _custom_store(root, enabled={read})
        data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
        data.update(generation=data["generation"] + 1, ask_tools=[read])  # stricter
        _write_raw(store, data)
        head_file = Path(str(store.tool_path) + ".head")
        if case == "no_snapshot":
            head = json.loads(head_file.read_text(encoding="utf-8"))
            head.pop("rules")
            head_file.write_text(json.dumps(head), encoding="utf-8")
            expected = "unconfirmed_generation"
        else:

            def fail(self):
                raise OSError("read-only disk")

            monkeypatch.setattr(ToolConfig, "_write_head", fail)
            expected = "head_repair_failed"
        config = store.load_tools()
        assert config.integrity_error == expected, case
        assert config.policy_of(read) == "never"
        monkeypatch.undo()


# --- B3: the lock latches until a typed human recovery -----------------------------


def _latched_store(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})  # Always
    original = Path(store.tool_path).read_text(encoding="utf-8")
    data = json.loads(original)
    data["ask_tools"] = [read]  # an edit the head does not know
    _write_raw(store, data)
    assert store.load_tools().integrity_error == "edited"
    Path(store.tool_path).write_text(original, encoding="utf-8")  # put the original back
    return store, read


def test_b3_restoring_the_original_files_does_not_unlock(tmp_path):
    from stiracore.tools import tool_config as tc

    store, read = _latched_store(tmp_path)
    config = store.load_tools()
    assert config.integrity_error == "edited" and config.policy_of(read) == "never"
    with tc._LATCH_LOCK:  # a restarted process still sees the on-disk marker
        tc._LATCHED.clear()
    assert store.load_tools().integrity_error == "edited"


def test_b3_empty_saves_do_not_unlock_only_a_typed_recovery_does(tmp_path):
    store, read = _latched_store(tmp_path)
    client = _client(store)
    assert client.post("/api/tools/config", json={}).status_code == 409
    assert client.post("/api/tools/config", json={"recover": "recover"}).status_code == 409
    assert (
        client.post("/api/config/tools", json={"name": read, "policy": "never"}).status_code == 409
    )
    assert store.load_tools().integrity_error == "edited"
    state = client.get("/api/config/hub").get_json()
    hub_body = {
        "changes": {"tools": {"enabled_tools": [read]}},
        "base_version": state["version"],
        "acknowledged_dangers": ["tool_policy_loosened"],
    }
    assert client.post("/api/config/hub", json=hub_body).status_code == 409
    ok = client.post("/api/config/hub", json={**hub_body, "recover": "RECOVER"})
    assert ok.status_code == 200, ok.get_json()
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "always"
    assert not Path(str(store.tool_path) + ".locked").exists()


def test_b3_unregistered_tools_are_never_while_locked(tmp_path, monkeypatch):
    from dashboard.stira_server import _tool_policy_reader

    store, _ = _latched_store(tmp_path)
    reader = _tool_policy_reader(store)
    assert reader("memory_write_not_in_registry")[0] == "never"
    tools = [Mock(tool_name="memory_write"), Mock(tool_name=_some("read"))]
    assert hub.filter_tools_by_config(tools, store.load_tools(), lambda t: t.tool_name) == []


# --- Major: lock order (no reverse acquisition, no deadlock) ------------------------


def test_major_saving_while_a_turn_holds_the_engine_lock_does_not_deadlock(tmp_path):
    import threading

    from stiracore.core import StiraEngine

    engine = StiraEngine(state=NeuralState(stability=0.9))
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: engine.breaker_status().level))
    client = app.test_client()
    result: dict = {}
    with engine.state_lock:  # e.g. pre_turn / a heartbeat tick in progress
        worker = threading.Thread(
            target=lambda: result.update(
                response=client.post(
                    "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
                )
            )
        )
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "save blocked on the engine lock"
    assert result["response"].status_code == 200


# --- Minor: the full tool definitions (and order) hash is unchanged by rules --------


def test_minor_full_tool_spec_hash_is_unchanged_by_rule_changes():
    from stiracore.agent.prompt_cache import digest

    class SpecModel(FakeModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            self.spec_hashes = getattr(self, "spec_hashes", [])
            self.spec_hashes.append(digest(tool_specs or []))
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    tools = []
    for suffix in ("a", "b", "c"):
        name = f"policy_probe_{suffix}"
        tool = RecordingTool(name, [])
        tool_registry.READ_TOOLS[name] = tool
        tools.append(tool)
    rule = {"value": ("always", 1)}
    model = SpecModel([text_turn("1"), text_turn("2"), text_turn("3")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        tools=tools,
        system_prompt="static",
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda _name: rule["value"],
    )
    for value in (("always", 1), ("ask", 2), ("never", 3)):
        rule["value"] = value
        agent("hi")
    assert len(set(model.spec_hashes)) == 1
    assert model.requests[0]["tool_names"] == [t.tool_name for t in tools]


# === Review round 3 (#269) =======================================================


@pytest.mark.parametrize(
    "history",
    ["", '{"id": "x", "source": "manual", "changes": [{"field": "breaker.x"}]}\n', "junk"],
)
def test_r3_b1_any_history_file_forbids_the_legacy_migration(tmp_path, history):
    """No head, no marker, a history without tool changes (or empty/junk): not a new install."""
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    Path(store.history_path).write_text(history, encoding="utf-8")
    _write_raw(store, {"mode": "trust_all", "enabled_tools": [], "shell_allowlist": []})
    config = store.load_tools()
    assert config.integrity_error == "rolled_back_to_legacy"
    assert all(config.policy_of(name) == "never" for name in _names())
    assert store.load_tools().integrity_error == "rolled_back_to_legacy"  # latched


def test_r3_b1_missing_rules_with_any_history_lock(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    Path(store.history_path).write_text("", encoding="utf-8")
    assert store.load_tools().integrity_error == "missing"


def test_r3_b1_a_new_install_records_its_defaults_at_once(tmp_path):
    """So a later settings save (history) never turns the next start into "missing"."""
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    first = store.load_tools()
    assert first.integrity_error is None and first.generation == 1
    assert Path(str(store.tool_path) + ".head").exists()
    client = _client(store)
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"breaker": {"stability_warning": 0.55}}, "base_version": version},
    )
    assert saved.status_code == 200
    assert store.load_tools().integrity_error is None


@pytest.mark.parametrize("change", ["allowlist", "mode"])
def test_r3_b2_n_plus_1_compares_every_field(tmp_path, change):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data["generation"] += 1
    if change == "allowlist":
        data["shell_allowlist"] = [*data["shell_allowlist"], "python3"]
    else:
        data["mode"] = "trust_all"  # ignores the allowlist and every Never
        data["enabled_tools"] = _names()  # same tools on: only the mode is looser
    _write_raw(store, data)
    assert store.load_tools().integrity_error == "unconfirmed_generation"


def test_r3_b2_rules_loosening_orders_each_field():
    from stiracore.tools.tool_config import rules_loosening

    base = ToolConfig(mode="custom", enabled_tools={"x"}, shell_allowlist=["ls"]).as_dict()
    assert rules_loosening(base, base) == []
    assert rules_loosening(base, {**base, "shell_allowlist": []}) == []  # fewer commands
    assert rules_loosening(base, {**base, "shell_allowlist": ["ls", "env"]}) == ["shell_allowlist"]
    assert "mode" in rules_loosening(base, {**base, "mode": "trust_all"})
    assert rules_loosening(base, {**base, "unknown": 1}) == ["snapshot"]
    assert rules_loosening(None, base) == ["snapshot"]


def test_r3_b3_a_marker_read_after_restart_latches_in_the_process(tmp_path):
    from stiracore.tools import tool_config as tc

    store, read = _latched_store(tmp_path)  # locked, original file + head put back
    with tc._LATCH_LOCK:  # a restart: module state is gone, the marker remains
        tc._LATCHED.clear()
    assert store.load_tools().integrity_error == "edited"
    Path(str(store.tool_path) + ".locked").unlink()  # an external process removes it
    config = store.load_tools()
    assert config.integrity_error == "edited" and config.policy_of(read) == "never"


def test_r3_major_every_open_approval_is_revoked_however_long_the_history(tmp_path, monkeypatch):
    from dashboard.stira_server import _tool_policy_reader
    from stiracore.core.approval_store import ApprovalStore

    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    approvals = ApprovalStore(None)
    reader = _tool_policy_reader(store, approvals, tmp_path / "marker.json")
    reader.sync()
    for index in range(30):
        request_id = f"r{index}"
        approvals.submit(
            request_id=request_id,
            agent_id="stira",
            tool_name=read,
            tool_input={"n": index},
            expires_at=1e12,
        )
        if index % 2:
            approvals.decide(
                request_id,
                "approve",
                args_hash=approvals.get(request_id).args_hash,
                approver="local:operator",
            )
    # A page-limited history (stand-in for more than 10 000 decided records).
    monkeypatch.setattr(approvals, "history", lambda limit=200: approvals.open_records()[:5])
    _save_rules(store, set())
    reader(read)
    assert {approvals.get(f"r{i}").status for i in range(30)} == {"cancelled"}


# === Review round 4 (#269): one loosening check on every save path ================


def _allowlist_store(tmp_path, *, all_on=False):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    enabled = set(_names()) if all_on else {_some("read")}
    ToolConfig(
        mode="custom", enabled_tools=enabled, shell_allowlist=["ls"], path=store.tool_path
    ).save()
    return store


def _hub_post(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def _route_attempts(client, store, kind, confirmed):
    """(name, response) for one loosening on every save path."""
    ack = {"confirm": True} if confirmed else {}
    dangers = {"acknowledged_dangers": ["tool_policy_loosened", "trust_all"]} if confirmed else {}
    if kind == "allowlist":
        legacy = client.post(
            "/api/tools/config", json={"shell_allowlist": ["ls", "python3"], **ack}
        )
        hub_change = {"tools": {"shell_allowlist": ["ls", "python3"]}}
    else:
        legacy = client.post(
            "/api/config/tools", json={"enabled_tools": _names(), "mode": "trust_all", **ack}
        )
        hub_change = {"tools": {"mode": "trust_all"}}
    return [("legacy", legacy), ("hub", _hub_post(client, hub_change, **dangers))]


@pytest.mark.parametrize("kind", ["allowlist", "trust_all"])
def test_r4_b1_every_path_needs_confirmation(tmp_path, kind):
    store = _allowlist_store(tmp_path, all_on=kind == "trust_all")
    client = _client(store)
    before = store.load_tools().as_dict()
    for name, response in _route_attempts(client, store, kind, confirmed=False):
        assert response.status_code == 400, (name, response.get_json())
        assert store.load_tools().as_dict() == before, name


@pytest.mark.parametrize("kind", ["allowlist", "trust_all"])
@pytest.mark.parametrize("level", ["warning", "critical"])
def test_r4_b1_every_path_refuses_during_a_breaker(tmp_path, kind, level):
    store = _allowlist_store(tmp_path, all_on=kind == "trust_all")
    client = _client(store, level=level)
    before = store.load_tools().as_dict()
    for name, response in _route_attempts(client, store, kind, confirmed=True):
        assert response.status_code == 403, (name, response.get_json())
        assert response.get_json()["error_code"] == "breaker_active"
        assert store.load_tools().as_dict() == before, name


@pytest.mark.parametrize("kind", ["allowlist", "trust_all"])
def test_r4_b1_every_path_saves_nothing_when_the_audit_fails(tmp_path, monkeypatch, kind):
    store = _allowlist_store(tmp_path, all_on=kind == "trust_all")
    client = _client(store)
    before = store.load_tools().as_dict()

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hub, "append_history", disk_full)
    for name, response in _route_attempts(client, store, kind, confirmed=True):
        assert response.status_code == 500, (name, response.get_json())
        assert store.load_tools().as_dict() == before, name


def test_r4_b1_a_rollback_that_re_adds_a_command_is_a_loosening(tmp_path):
    store = _allowlist_store(tmp_path)
    client = _client(store)
    assert client.post("/api/tools/config", json={"shell_allowlist": []}).status_code == 200
    entry = client.get("/api/config/history").get_json()["entries"][0]
    url = f"/api/config/history/{entry['id']}/rollback"
    version = client.get("/api/config/hub").get_json()["version"]
    refused = client.post(url, json={"base_version": version})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["tool_policy_loosened"]
    blocked = _client(store, level="warning").post(
        url, json={"base_version": version, "acknowledged_dangers": ["tool_policy_loosened"]}
    )
    assert blocked.status_code == 403
    assert store.load_tools().shell_allowlist == []


def test_r4_b1_the_hub_and_the_apis_share_one_check():
    from stiracore.tools.tool_config import rules_loosening

    current = hub.default_values()
    candidate = json.loads(json.dumps(current))
    candidate["tools"]["shell_allowlist"] = [*candidate["tools"]["shell_allowlist"], "python3"]
    assert (
        hub.loosened_tools(current, candidate)
        == rules_loosening(current["tools"], candidate["tools"])
        == ["shell_allowlist"]
    )


# --- B2: unlocking is a loosening, whatever the registered-tool diff ----------------


def test_r4_b2_recover_only_post_needs_normal_breaker_and_audit(tmp_path, monkeypatch):
    from dashboard.stira_server import _tool_policy_reader

    store, _ = _latched_store(tmp_path)
    body = {"recover": "RECOVER", "confirm": True}
    warning = _client(store, level="warning").post("/api/tools/config", json=body)
    assert warning.status_code == 403
    assert store.load_tools().integrity_error == "edited"
    client = _client(store)
    no_confirm = client.post("/api/tools/config", json={"recover": "RECOVER"})
    assert no_confirm.status_code == 400
    assert store.load_tools().integrity_error == "edited"

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(hub, "append_history", disk_full)
        failed = client.post("/api/tools/config", json=body)
    assert failed.status_code == 500
    assert store.load_tools().integrity_error == "edited"
    assert _tool_policy_reader(store)("memory_write_not_in_registry")[0] == "never"
    ok = client.post("/api/tools/config", json=body)
    assert ok.status_code == 200, ok.get_json()
    assert store.load_tools().integrity_error is None
    newest = hub.read_history(store.history_path, 1)[0]
    assert newest["recovered_lock"] == "edited"


def test_r4_b2_hub_recovery_during_a_breaker_is_refused(tmp_path):
    store, read = _latched_store(tmp_path)
    client = _client(store, level="warning")
    state = client.get("/api/config/hub").get_json()
    response = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {"enabled_tools": [read]}},
            "base_version": state["version"],
            "recover": "RECOVER",
        },
    )
    assert response.status_code == 403
    assert store.load_tools().integrity_error == "edited"


# --- Minor: read-only loads never write the head ------------------------------------


def test_r4_minor_readonly_load_completes_n_plus_1_in_memory_only(tmp_path):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data.update(generation=data["generation"] + 1, ask_tools=[read])  # stricter
    _write_raw(store, data)
    head_file = Path(str(store.tool_path) + ".head")
    head_before = head_file.read_text(encoding="utf-8")
    config = ToolConfig.load(store.tool_path, readonly=True)
    assert config.integrity_error is None and config.policy_of(read) == "ask"
    assert head_file.read_text(encoding="utf-8") == head_before


# === Review round 5 (#269): one decision for confirmation, breaker and audit ======


def _locked_with_history(tmp_path):
    """Locked rules, plus a history entry whose rollback only sets ask_tools."""
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read}, ask={read})
    client = _client(store)
    assert (
        client.post(
            "/api/config/tools", json={"name": read, "policy": "always", "confirm": True}
        ).status_code
        == 200
    )
    entry_id = client.get("/api/config/history").get_json()["entries"][0]["id"]
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data["shell_allowlist"] = ["pwd"]  # a hand edit the head does not know
    _write_raw(store, data)
    assert store.load_tools().integrity_error == "edited"
    return store, read, entry_id


def _locked_attempt(client, path, read, entry_id, extra):
    """One save over locked rules that changes no effective rule (all stay Never)."""
    never_tool = read
    if path == "hub":
        version = client.get("/api/config/hub").get_json()["version"]
        body = {"changes": {"tools": {"ask_tools": [never_tool]}}, "base_version": version}
        if extra.get("other_field"):
            body["changes"]["breaker"] = {"stability_warning": 0.55}
        if extra.get("confirm"):
            body["acknowledged_dangers"] = ["tool_policy_loosened"]
        return client.post("/api/config/hub", json={**body, "recover": "RECOVER"})
    if path == "rollback":
        version = client.get("/api/config/hub").get_json()["version"]
        body = {"base_version": version, "recover": "RECOVER"}
        if extra.get("confirm"):
            body["acknowledged_dangers"] = ["tool_policy_loosened"]
        return client.post(f"/api/config/history/{entry_id}/rollback", json=body)
    body = {"recover": "RECOVER", **({"confirm": True} if extra.get("confirm") else {})}
    if path == "tools_config":
        if not extra.get("recover_only"):
            body["ask_tools"] = [never_tool]
        return client.post("/api/tools/config", json=body)
    return client.post("/api/config/tools", json={**body, "name": never_tool, "policy": "never"})


PATHS_R5 = [
    ("hub", {}),
    ("hub", {"other_field": True}),
    ("rollback", {}),
    ("tools_config", {}),
    ("tools_config", {"recover_only": True}),
    ("config_tools", {}),
]


@pytest.mark.parametrize(("path", "extra"), PATHS_R5)
def test_r5_b1_unlocking_needs_confirmation_on_every_path(tmp_path, path, extra):
    store, read, entry_id = _locked_with_history(tmp_path)
    response = _locked_attempt(_client(store), path, read, entry_id, extra)
    assert response.status_code == 400, (path, response.get_json())
    assert store.load_tools().integrity_error == "edited"


@pytest.mark.parametrize(("path", "extra"), PATHS_R5)
def test_r5_b1_unlocking_is_refused_during_a_breaker_on_every_path(tmp_path, path, extra):
    store, read, entry_id = _locked_with_history(tmp_path)
    response = _locked_attempt(
        _client(store, level="warning"), path, read, entry_id, {**extra, "confirm": True}
    )
    assert response.status_code == 403, (path, response.get_json())
    assert store.load_tools().integrity_error == "edited"


@pytest.mark.parametrize(("path", "extra"), PATHS_R5)
def test_r5_b1_unlocking_needs_the_audit_on_every_path(tmp_path, monkeypatch, path, extra):
    store, read, entry_id = _locked_with_history(tmp_path)

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    client = _client(store)
    with monkeypatch.context() as patched:
        patched.setattr(hub, "append_history", disk_full)
        response = _locked_attempt(client, path, read, entry_id, {**extra, "confirm": True})
    assert response.status_code == 500, (path, response.get_json())
    assert store.load_tools().integrity_error == "edited"
    ok = _locked_attempt(client, path, read, entry_id, {**extra, "confirm": True})
    assert ok.status_code == 200, (path, ok.get_json())
    assert store.load_tools().integrity_error is None
    assert hub.read_history(store.history_path, 1)[0]["recovered_lock"] == "edited"


def test_r5_b1_the_hub_decision_is_made_once(tmp_path, monkeypatch):
    """Confirmation, breaker and audit all use the one loosening decision."""
    store, read, _ = _locked_with_history(tmp_path)
    calls = []
    original = hub.loosened_tools

    def spy(*args, **kwargs):
        calls.append(kwargs.get("locked"))
        return original(*args, **kwargs)

    monkeypatch.setattr(hub, "loosened_tools", spy)
    client = _client(store)
    version = client.get("/api/config/hub").get_json()["version"]
    response = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": {"ask_tools": [read]}},
            "base_version": version,
            "recover": "RECOVER",
            "acknowledged_dangers": ["tool_policy_loosened"],
        },
    )
    assert response.status_code == 200, response.get_json()
    assert calls == [True]


# --- Major: no secret in the allowlist, the rules, their head or the history ---------


SECRET_COMMAND = "curl -H Authorization:Bearer " + "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8"


def _no_secret_anywhere(store):
    for path in (
        store.tool_path,
        Path(str(store.tool_path) + ".head"),
        store.history_path,
    ):
        if Path(path).exists():
            assert b"Zz9Yy8Xx7Ww6" not in Path(path).read_bytes(), path


def test_r5_major_secret_commands_are_refused_on_every_path(tmp_path):
    store = _allowlist_store(tmp_path)
    client = _client(store)
    legacy = client.post(
        "/api/tools/config", json={"shell_allowlist": ["ls", SECRET_COMMAND], "confirm": True}
    )
    assert legacy.status_code == 400 and legacy.get_json()["error_code"] == "secret_command"
    hub_response = _hub_post(
        client,
        {"tools": {"shell_allowlist": ["ls", SECRET_COMMAND]}},
        acknowledged_dangers=["tool_policy_loosened"],
    )
    assert hub_response.status_code == 400
    assert hub_response.get_json()["errors"][0]["code"] == "secret_command"
    with pytest.raises(ValueError):
        ToolConfig(
            mode="custom", shell_allowlist=["ls", SECRET_COMMAND], path=store.tool_path
        ).save()
    _no_secret_anywhere(store)


def test_r5_major_an_existing_secret_is_warned_about_and_not_copied_to_the_head(tmp_path, caplog):
    store = _allowlist_store(tmp_path)
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data["shell_allowlist"] = ["ls", SECRET_COMMAND]  # e.g. a file from before this check
    head_file = Path(str(store.tool_path) + ".head")
    _write_raw(store, data)
    head = json.loads(head_file.read_text(encoding="utf-8"))
    from stiracore.tools.tool_config import _rules_digest

    config = ToolConfig(
        **{k: data[k] for k in ("mode", "enabled_tools", "shell_allowlist", "ask_tools")}
    )
    head["digest"] = _rules_digest(config.as_dict(), data["generation"])
    head_file.write_text(json.dumps(head), encoding="utf-8")  # an accepted older install
    with caplog.at_level("WARNING", logger="stiracore.tools.tool_config"):
        loaded = store.load_tools()
    assert loaded.integrity_error is None
    assert any("secret-looking" in r.getMessage() for r in caplog.records)
    assert all("Zz9Yy8" not in r.getMessage() for r in caplog.records)
    _write_raw(store, {**data, "generation": data["generation"]})
    # An unrelated tightening save does not write the existing entry again (#270): it is
    # refused until the entry is removed, and the head never holds it.
    loaded.ask_tools = {_some("read")}
    with pytest.raises(ValueError):
        loaded.save()
    assert b"Zz9Yy8Xx7Ww6" not in head_file.read_bytes()
    loaded.shell_allowlist = ["ls"]
    loaded.save()
    _no_secret_anywhere(store)
