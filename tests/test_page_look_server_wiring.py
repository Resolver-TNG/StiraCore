"""ADR 0015 Addendum rev 6, slice S4b (#773): the chat server wires ``page_look``'s
conditional Always (the axis scope and the card preparer), and the settings page has a
text for every cap code.

The real app (``create_app``) and the real gate through ``/api/chat`` with a scripted
model. No browser starts (``run_page_check`` is a recorder), the axis is the real one on
an injected clock with a scripted guard, nothing touches the network.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg
from stiracore.tools import page_look as pl
from stiracore.tools import tool_registry
from tests.attack.runner import make_stira
from tests.test_page_check_axis import Clock, _verified_guard

ROOT = Path(__file__).resolve().parents[1]
BROWSER = "/x/chrome"
ALWAYS = {"mode": "custom", "enabled_tools": {"file_read", "page_look"}, "ask_tools": set()}
CALL = [("page_look", {"target": "index.html"})]


class Broken:
    """A reader that raises on any use."""

    def __call__(self, *args, **kwargs):
        raise RuntimeError("/secret/path")

    def __getattr__(self, name):
        raise RuntimeError("/secret/path")


@pytest.fixture
def server(tmp_path, monkeypatch):
    """``build(registered=True, wired=True)`` -> the live app's parts. The module state the
    app sets is put back afterwards."""
    from dashboard import stira_server

    for holder, key in (
        (pl._AXIS, "scope"),
        (pl._AUDIT, "record"),
        (pl._KEY_STATE, "dir"),
        (pl._KEY_STATE, "key"),
    ):
        monkeypatch.setitem(holder, key, None)
    pl.reset_counts()
    runs: list = []
    monkeypatch.setattr(
        pc,
        "run_page_check",
        lambda target, policy, **kwargs: runs.append(target) or {"status": "ok", "screenshots": []},
    )
    axis = pg.Axis(clock=Clock(), guard=_verified_guard())
    monkeypatch.setattr(pc, "axis", axis)

    def build(*, registered: bool = True, wired: bool = True):
        if registered:
            monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_look", pl.page_look)
            status = pc.BrowserStatus("page_check_ready", BROWSER)
        else:
            monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_look", raising=False)
            status = pc.BrowserStatus("page_check_no_browser")
        monkeypatch.setattr(tool_registry, "PAGE_CHECK_STATUS", status)
        if not wired:  # the server without its wiring line
            monkeypatch.setattr(stira_server, "_wire_page_look_always", lambda *a, **k: False)
        agents: list = []
        original = stira_server.NXPAgent

        def spy(*args, **kwargs):
            agents.append(original(*args, **kwargs))
            return agents[-1]

        monkeypatch.setattr(stira_server, "NXPAgent", spy)
        app, _model, attack, _module = make_stira(tmp_path, monkeypatch, tools=ALWAYS)
        (agent,) = agents
        return dataclasses.make_dataclass("Server", ["app", "agent", "attack", "runs", "axis"])(
            app, agent, attack, runs, axis
        )

    return build


def _card(server):
    (card,) = server.attack.asks
    return card


# --- (a) / (b): what the server registers ---------------------------------------------------


def test_registered_page_look_gets_the_axis_scope_and_the_card_preparer(server):
    live = server()
    assert "page_look" in live.agent.checkpoint_hooks.input_preparers
    scope = pl._scope()
    assert isinstance(scope, pc.AxisScope)
    assert scope.engine is live.agent.engine
    assert scope.browser == BROWSER
    assert scope.policy is not None and scope.policy.workspace is not None
    assert scope.event_store.healthy is True
    rules = scope.tool_config()
    assert rules.integrity_error is None and rules.generation >= 1
    assert scope.file_read_rule() == "always"
    assert scope.grant_tools is None and scope.ledger is None and scope.tool == "page_look"


def test_unregistered_page_look_gets_neither(server):
    live = server(registered=False)
    assert pl._AXIS["scope"] is None and pl._scope() is None
    assert "page_look" not in live.agent.checkpoint_hooks.input_preparers


def test_unreadable_rules_are_locked_rules():
    """The scope's rules reader never hands out rules it could not read: locked (all Never)."""
    from dashboard import stira_server

    class NoStore:
        tool_path = Path("tools.json")

        @property
        def lock(self):
            raise OSError("unreadable")

    rules = stira_server._live_tool_rules(NoStore())()
    assert rules.integrity_error == "unreadable" and rules.policy_of("file_read") == "never"


# --- (d): effective through the real gate ---------------------------------------------------


def test_saved_always_runs_without_a_card_on_the_server(server):
    live = server()
    assert live.axis.current().code == "not_run"
    live.attack("look at the page", CALL)
    assert live.attack.asks == []  # the rule answered: no card
    assert live.runs == ["index.html"]
    assert live.axis.current().code == "pass"  # measured through the server's scope


# --- the rules the tool reads are the rules the gate reads (S4c) -----------------------------


def _narrow(tmp_path, name: str) -> None:
    """A person's save during the turn: ``name`` becomes Ask in the saved rules."""
    from stiracore.tools.tool_config import ToolConfig

    rules = ToolConfig.load(tmp_path / "tools.json")
    rules.ask_tools = rules.ask_tools | {name}
    rules.save()


def test_the_scope_reads_the_saved_rules_at_every_check(server, tmp_path):
    live = server()
    scope = pl._scope()
    generation = scope.tool_config().generation
    assert scope.file_read_rule() == "always"
    _narrow(tmp_path, "file_read")
    assert scope.file_read_rule() == "ask"  # the same scope object, the file as it is now
    assert scope.tool_config().generation == generation + 1
    live.attack("look at the page", CALL)
    assert "capped:file_read_not_always" in json.dumps(_card(live)) and live.runs == []


@pytest.mark.parametrize("narrowed", ["file_read", "page_look"])
def test_a_rule_narrowed_after_the_gate_refuses_the_call_in_the_tool(
    server, tmp_path, monkeypatch, narrowed
):
    """The real app: the gate lets the rule answer, then a person narrows a rule before the
    tool starts. The tool reads the saved rules again (not the turn's start snapshot):
    refused with a code, no browser. The next call of the same turn is a card."""
    from stiracore.tools import tool_policy

    live = server()
    reserve = tool_policy.reserve_rule_answer
    passed: list = []

    def reserve_then_narrow(name, tool_use_id):
        taken = reserve(name, tool_use_id)
        if taken:
            passed.append(tool_use_id)
            _narrow(tmp_path, narrowed)
        return taken

    monkeypatch.setattr(tool_policy, "reserve_rule_answer", reserve_then_narrow)
    first, second = live.attack("look at the page", CALL + CALL)
    assert len(passed) == 1 and live.runs == []
    refusal = json.loads(first)
    text = json.dumps(refusal)
    assert "refused" in text
    assert any(code in text for code in ("rules_changed", "file_read_not_always"))
    assert "tools.json" not in text and str(tmp_path) not in text
    assert _card(live)["tool_name"] == "page_look"  # the second call: a person's card
    assert second != first


# --- (c): fail closed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reader", ["provider", "policy", "tool_config", "event_store", "engine", "file_read_rule"]
)
def test_a_reader_that_raises_keeps_the_saved_always_a_card(server, monkeypatch, reader):
    live = server()
    wired = pl._AXIS["scope"]
    if reader == "provider":
        # The server's own scope reader catches nothing: page_look then fails every item.
        with monkeypatch.context() as patch:
            patch.setattr(tool_registry, "PAGE_CHECK_STATUS", object())
            with pytest.raises(AttributeError):
                wired()
            assert pl._scope() == pc.AxisScope()
        pl.set_axis_scope(Broken())
    else:
        pl.set_axis_scope(lambda: dataclasses.replace(wired(), **{reader: Broken()}))
    live.attack("look at the page", CALL)
    card = _card(live)
    assert card["tool_name"] == "page_look" and live.runs == []
    assert "rule_always_capped" in json.dumps(card)  # the wired preparer wrote the card
    assert live.axis.current().code != "pass"


@pytest.mark.parametrize(
    "reader", ["policy", "tool_config", "event_store", "engine", "file_read_rule"]
)
def test_a_missing_reader_keeps_the_saved_always_a_card(server, reader):
    live = server()
    wired = pl._AXIS["scope"]
    pl.set_axis_scope(lambda: dataclasses.replace(wired(), **{reader: None}))
    live.attack("look at the page", CALL)
    assert _card(live)["tool_name"] == "page_look" and live.runs == []


# --- (e): without the wiring line -----------------------------------------------------------


def test_without_the_wiring_the_saved_always_stays_a_card(server):
    """The same server and turn as (a) and (d) with the wiring call removed: no scope, no
    preparer, nothing measures the axis and the saved Always is a card."""
    live = server(wired=False)
    assert pl._AXIS["scope"] is None
    assert "page_look" not in live.agent.checkpoint_hooks.input_preparers
    live.attack("look at the page", CALL)
    assert _card(live)["tool_name"] == "page_look" and live.runs == []
    assert live.axis.current().code == "not_run"


# --- the settings page's text for a cap -----------------------------------------------------


def _catalog(lang):
    return json.loads((ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text("utf-8"))


def test_every_cap_code_has_settings_text_in_both_languages():
    ja, en = _catalog("ja"), _catalog("en")
    script = (ROOT / "dashboard/static/js/settings_hub.js").read_text("utf-8")
    assert len(set(pl.CAP_CODES)) == len(pl.CAP_CODES) >= 18
    for code in pl.CAP_CODES:
        key = f"policy.cap_{code}"
        assert ja.get(key) and en.get(key), key
        assert ja[key] != en[key], key
        assert f'{code}: () => t("{key}")' in script, code
    for key in ("policy.cap_unknown", "policy.reason_page_look_always_capped"):
        assert ja.get(key) and en.get(key), key
    for lang in (ja, en):
        assert "{why}" in lang["policy.reason_page_look_always_capped"]
    # An unknown code is the general sentence, never the code as sent.
    assert (
        "Object.hasOwn(CAP_TEXT, row.always_cap_reason)\n"
        '        ? CAP_TEXT[row.always_cap_reason]() : t("policy.cap_unknown")'
    ) in script
