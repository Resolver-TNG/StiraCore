"""#450: tool profiles on the settings page and the tools a YOLO window can approve.

* each profile is a fixed tool x rule table (pinned literally here);
* putting a profile in the draft and saving it is a normal hub save: a loosening needs the
  confirmation, the save is a ``settings-history`` row with ``source: "profile:<id>"``;
* nothing rewrites an existing ``tool-config.json`` (loading the page, the YOLO status, a
  restart);
* ``yolo_targets`` / ``GET /api/yolo/status`` name the tools each scope could approve —
  internal only / external too / exec too — and none when no tool is Ask.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import tool_policy, tool_profiles, tool_registry
from stiracore.tools.tool_config import ToolConfig, head_path
from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo import routes as yolo_routes
from stiracore.yolo.targets import yolo_targets
from tests.test_yolo_host_gate import _build

A, K, N = "always", "ask", "never"

# The whole table, written out: changing a profile changes this test on purpose.
EXPECTED = {
    #                    minimal dev research
    "current_time": (A, A, A),
    "file_read": (A, A, A),
    "view_image": (A, A, A),
    "image_reader": (A, A, A),
    "stop": (A, A, A),
    "handoff_to_user": (A, A, A),
    "memory_read": (A, A, A),
    "memory_search": (A, A, A),
    "memory_list": (A, A, A),
    "memory_index": (A, A, A),
    "handoff_read": (A, A, A),
    # ADR 0018 Q3: read category, default Always (first run, dev, research); minimal stays the
    # smallest surface and a pure tightening of any install (Never)
    "memory_rate": (N, A, A),
    "memory_write": (N, K, K),
    "handoff_write": (N, K, K),
    "propose_procedure": (N, K, K),  # #463: Ask or Never only; YOLO keeps it a card
    "file_write": (N, K, K),
    "editor": (N, K, K),
    "journal": (N, K, K),
    "lsp_diagnostics": (N, K, K),
    "lsp_hover": (N, K, K),
    "page_check": (N, K, K),  # ADR 0015 §9 (registered only when a browser is found)
    "run_checks": (N, K, K),  # ADR 0019 (registered only when gateway run_checks is on)
    "diagram": (N, N, N),
    "web_fetch": (N, N, K),
    "web_search": (N, N, K),
    "perplexity_search": (N, N, K),
    "http_request": (N, N, N),
    "webhook_send": (N, N, N),
    "oauth_api_call": (N, N, N),
    "rss": (N, N, N),
    "speak": (N, N, N),
    "memory": (N, N, N),
    "shell": (N, K, K),
    "batch": (N, N, N),
    "cron": (N, N, N),
    "use_agent": (N, N, N),
    "think": (N, N, N),
    "swarm": (N, N, N),
    "graph": (N, N, N),
}


def test_each_profile_is_pinned_as_a_tool_by_rule_table():
    assert tool_profiles.PROFILE_IDS == ("minimal", "dev", "research")
    for index, profile in enumerate(tool_profiles.PROFILE_IDS):
        table = dict(tool_profiles.PROFILE_TABLES[profile])
        assert table == {name: rules[index] for name, rules in EXPECTED.items()}, profile


def test_the_table_covers_every_built_in_tool_and_never_grants_a_write_always():
    registered = (set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)) - set(
        tool_registry.mcp_tool_names()
    )
    assert registered <= tool_profiles.PROFILE_TOOLS, registered - tool_profiles.PROFILE_TOOLS
    for profile, table in tool_profiles.PROFILE_TABLES.items():
        for name, rule in table.items():
            if tool_registry.is_exec_tool(name):
                assert rule != A, (profile, name)  # exec can be Ask or Never only
            if rule == A:
                # Only reads: no profile asks for names to be typed (always_write_tools).
                assert name in tiers_reads(), (profile, name)


def tiers_reads():
    return {
        "current_time",
        "file_read",
        "view_image",
        "image_reader",
        "stop",
        "handoff_to_user",
        "memory_read",
        "memory_search",
        "memory_list",
        "memory_index",
        "memory_rate",
        "handoff_read",
    }


def test_profile_tools_keeps_tools_outside_the_table_and_the_allowlist():
    current = {
        "mode": "custom",
        "enabled_tools": ["file_read", "mcp_thing", "gone_tool"],
        "ask_tools": ["mcp_thing", "gone_tool"],
        "shell_allowlist": ["pwd", "ls"],
    }
    names = {"file_read", "file_write", "shell", "mcp_thing"}
    tools = tool_profiles.profile_tools("dev", current, names)
    assert tools["mode"] == "custom"
    assert tools["shell_allowlist"] == ["pwd", "ls"]
    # mcp_thing (registered, not in the table) keeps Ask; gone_tool (not registered now)
    # keeps its saved switch; the table decides the rest.
    assert tools["enabled_tools"] == ["file_read", "file_write", "gone_tool", "mcp_thing", "shell"]
    assert tools["ask_tools"] == ["file_write", "gone_tool", "mcp_thing", "shell"]
    minimal = tool_profiles.profile_tools("minimal", current, names)
    assert minimal["enabled_tools"] == ["file_read", "gone_tool", "mcp_thing"]
    with pytest.raises(KeyError):
        tool_profiles.profile_tools("everything", current, names)


# --- the hub: preview, confirmed save, history ---------------------------------------


def _store(tmp_path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


def _zero_ask(store) -> None:
    """The reported install (#450): custom, nine tools Always, nothing Ask."""
    ToolConfig(
        mode="custom",
        enabled_tools={
            "current_time",
            "file_read",
            "view_image",
            "image_reader",
            "stop",
            "handoff_to_user",
            "file_write",
            "editor",
            "journal",
        },
        ask_tools=set(),
        path=store.tool_path,
    ).save()


def _client(store):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _profile(state, profile_id):
    return next(p for p in state["tool_profiles"] if p["id"] == profile_id)


def test_the_hub_state_shows_each_profile_before_it_is_applied(tmp_path):
    store = _store(tmp_path)
    _zero_ask(store)
    state = _client(store).get("/api/config/hub").get_json()
    assert [p["id"] for p in state["tool_profiles"]] == ["minimal", "dev", "research"]
    names = [row["name"] for row in state["tools"]]
    for profile in state["tool_profiles"]:
        rows = {row["name"]: row for row in profile["rows"]}
        assert sorted(rows) == sorted(names)
        for name, row in rows.items():
            column = tool_profiles.PROFILE_IDS.index(profile["id"])
            assert row["policy"] == EXPECTED[name][column], (profile["id"], name)
            assert row["in_table"] is True
        assert profile["counts"] == {
            policy: sum(1 for row in rows.values() if row["policy"] == policy)
            for policy in tool_policy.POLICIES
        }
        assert profile["saved"] is False
    dev = {row["name"]: row for row in _profile(state, "dev")["rows"]}
    # shell needs the exec variable (not set in tests): said on its row, and YOLO cannot
    # approve it until then; cron stays Never.
    assert dev["shell"]["policy"] == K and dev["shell"]["needs_env"] is True
    assert dev["shell"]["env_set"] is False and dev["shell"]["yolo"] == "exec_not_exposed"
    assert dev["cron"]["needs_env"] is False and dev["file_write"]["needs_env"] is False
    assert dev["file_write"]["yolo"] == "internal" and dev["file_read"]["yolo"] is None
    # file_write, editor, journal, lsp_* (and memory_write when registered); shell is not
    # exposed, so "Include exec" adds nothing.
    # page_check (when a browser is found) is Ask but stays a person's card (ADR 0015).
    # propose_procedure (#463) is Ask in dev and also a person's card in every window.
    person = {"shell", "page_check", "propose_procedure", "run_checks"}
    local = len([n for n in names if EXPECTED[n][1] == K and n not in person])
    assert local >= 5
    assert _profile(state, "dev")["yolo"] == {"internal": local, "external": local, "exec": local}
    research = _profile(state, "research")
    assert {row["name"]: row["yolo"] for row in research["rows"]}["web_fetch"] == "external"
    assert research["yolo"]["external"] == research["yolo"]["internal"] + len(
        [n for n in ("web_fetch", "web_search", "perplexity_search") if n in names]
    )
    assert _profile(state, "minimal")["yolo"] == {"internal": 0, "external": 0, "exec": 0}


def test_loading_the_page_never_rewrites_an_existing_tool_config(tmp_path):
    store = _store(tmp_path)
    _zero_ask(store)
    before = (store.tool_path.read_bytes(), head_path(store.tool_path).read_bytes())
    client = _client(store)
    client.get("/api/config/hub")
    client.get("/api/config/tools")
    client.post("/api/config/hub/validate", json={"changes": {}})
    assert (store.tool_path.read_bytes(), head_path(store.tool_path).read_bytes()) == before
    assert not (tmp_path / hub.HISTORY_FILE).exists()


def test_applying_a_profile_needs_the_confirmation_and_is_recorded(tmp_path):
    store = _store(tmp_path)
    _zero_ask(store)
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    dev = _profile(state, "dev")
    body = {
        "changes": {"tools": dev["tools"]},
        "base_version": state["version"],
        "source": "profile:dev",
    }
    # Never -> Ask (shell, lsp_*) is a loosening: refused without the checkbox.
    refused = client.post("/api/config/hub", json=body)
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["tool_policy_loosened"]
    assert ToolConfig.load(store.tool_path).ask_tools == set()
    saved = client.post(
        "/api/config/hub", json={**body, "acknowledged_dangers": ["tool_policy_loosened"]}
    )
    assert saved.status_code == 200, saved.get_json()
    config = ToolConfig.load(store.tool_path)
    for name in config.enabled_tools | set(EXPECTED):
        if name in EXPECTED and name in {r["name"] for r in state["tools"]}:
            assert config.policy_of(name) == EXPECTED[name][1], name
    entry = hub.read_history(tmp_path / hub.HISTORY_FILE, 1)[0]
    assert entry["source"] == "profile:dev"
    assert {"tools.ask_tools", "tools.enabled_tools"} <= {c["field"] for c in entry["changes"]}
    after = client.get("/api/config/hub").get_json()
    assert _profile(after, "dev")["saved"] is True
    # An unknown profile is not a source the history accepts.
    bad = client.post(
        "/api/config/hub",
        json={"changes": {}, "base_version": after["version"], "source": "profile:all"},
    )
    assert bad.status_code == 400 and bad.get_json()["error"] == "Unknown source."


def test_applying_minimal_is_a_tightening_and_needs_no_confirmation(tmp_path):
    store = _store(tmp_path)
    _zero_ask(store)
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    response = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": _profile(state, "minimal")["tools"]},
            "base_version": state["version"],
            "source": "profile:minimal",
        },
    )
    assert response.status_code == 200, response.get_json()
    assert ToolConfig.load(store.tool_path).policy_of("file_write") == N
    assert hub.read_history(tmp_path / hub.HISTORY_FILE, 1)[0]["source"] == "profile:minimal"


# --- the tools a YOLO window can approve ---------------------------------------------


def _rules(ask, *, always=(), path=None):
    return ToolConfig(
        mode="custom",
        enabled_tools=set(ask) | set(always),
        ask_tools=set(ask),
        **({"path": path} if path else {}),
    )


NAMES = {"file_read", "file_write", "editor", "web_fetch", "shell", "batch", "cron", "think"}


def test_no_ask_tool_means_no_scope_approves_anything():
    targets = yolo_targets(
        _rules((), always=("file_read", "file_write", "editor")), names=NAMES, exec_exposed=True
    )
    assert targets["ask_total"] == 0 and targets["excluded"] == []
    assert {s: v["count"] for s, v in targets["scopes"].items()} == {
        "internal": 0,
        "external": 0,
        "exec": 0,
    }


def test_each_scope_adds_its_tools_and_the_rest_stays_a_card():
    rules = _rules(("file_write", "web_fetch", "shell", "cron", "think"), always=("file_read",))
    targets = yolo_targets(rules, names=NAMES, exec_exposed=True, is_mcp=lambda n: False)
    assert targets["scopes"] == {
        "internal": {"count": 1, "tools": ["file_write"]},
        "external": {"count": 2, "tools": ["file_write", "web_fetch"]},
        "exec": {"count": 3, "tools": ["file_write", "shell", "web_fetch"]},
    }
    assert targets["excluded"] == [
        {"name": "cron", "reason": "exec_runs_later"},
        {"name": "think", "reason": "exec_child_ungated"},
    ]
    assert targets["ask_total"] == 5
    # Exec tools not exposed: shell is never offered, so no scope approves it.
    hidden = yolo_targets(rules, names=NAMES, exec_exposed=False, is_mcp=lambda n: False)
    assert hidden["scopes"]["exec"] == hidden["scopes"]["external"]
    assert {"name": "shell", "reason": "exec_not_exposed"} in hidden["excluded"]


def test_mcp_person_tools_unclassified_and_locked_rules():
    names = {"file_write", "diagram", "web_fetch", "some_new_tool"}
    rules = _rules(names)
    targets = yolo_targets(
        rules, names=names, exec_exposed=False, is_mcp=lambda n: n == "web_fetch"
    )
    assert targets["scopes"]["external"]["tools"] == ["file_write"]
    assert {e["name"]: e["reason"] for e in targets["excluded"]} == {
        "diagram": "person_tool",
        "web_fetch": "mcp_tool",
        "some_new_tool": "unclassified",
    }
    locked = _rules(names)
    locked.integrity_error = "head_mismatch"
    empty = yolo_targets(locked, names=names, exec_exposed=False, is_mcp=lambda n: False)
    assert empty["locked"] is True and empty["ask_total"] == 0
    assert empty["scopes"]["exec"] == {"count": 0, "tools": []}


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(allowance="internal", setup=None):
        base = tmp_path / str(len(made))
        base.mkdir()
        if setup is not None:
            setup(base)
        app = _build(base, monkeypatch, allowance, host_confirm="browser")
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def test_the_status_names_the_tools_of_each_scope(make_app):
    client = make_app("external").test_client()
    targets = client.get("/api/yolo/status").get_json()["targets"]
    rules = ToolConfig.load(client.application.extensions["config_store"].tool_path)
    ask = sorted(n for n in rules.ask_tools if n in rules.enabled_tools)
    assert ask, "a new install has Ask tools"
    internal = targets["scopes"]["internal"]
    assert internal["count"] == len(internal["tools"]) > 0
    assert set(internal["tools"]) <= set(ask)
    assert set(internal["tools"]) <= set(targets["scopes"]["external"]["tools"])
    assert targets["ask_total"] == len(ask)


def test_the_status_says_zero_for_the_reported_install_and_it_is_not_rewritten(make_app):
    def setup(base):
        _zero_ask(ConfigStore(base / "gw.json", base / "agent.json", base / "tools.json"))

    app = make_app("external", setup=setup)
    path = app.extensions["config_store"].tool_path
    before = path.read_bytes()
    client = app.test_client()
    targets = client.get("/api/yolo/status").get_json()["targets"]
    assert targets["ask_total"] == 0
    assert [targets["scopes"][s]["count"] for s in ("internal", "external", "exec")] == [0, 0, 0]
    # Neither the start-up nor the status read changed the saved rules.
    assert path.read_bytes() == before
    assert json.loads(before)["ask_tools"] == []


def test_unreadable_rules_give_targets_null_not_an_error(monkeypatch):
    def broken():
        raise OSError("gone")

    assert yolo_routes._targets(broken) is None
    assert yolo_routes._targets(None) is None


def test_exec_exposure_is_read_from_the_variable_by_default(monkeypatch):
    rules = _rules(("file_write", "shell"))
    names = {"file_write", "shell"}
    monkeypatch.delenv("STIRA_ENABLE_EXEC_TOOLS", raising=False)
    off = yolo_targets(rules, names=names, is_mcp=lambda n: False)
    assert off["scopes"]["exec"]["tools"] == ["file_write"]
    monkeypatch.setenv("STIRA_ENABLE_EXEC_TOOLS", "1")
    on = yolo_targets(rules, names=names, is_mcp=lambda n: False)
    assert on["scopes"]["exec"]["tools"] == ["file_write", "shell"]
    assert on["scopes"]["external"]["tools"] == ["file_write"]
