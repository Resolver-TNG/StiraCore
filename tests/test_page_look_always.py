"""ADR 0015 Addendum rev 6, slice S4 (#773): ``page_look``'s **conditional Always**.

A saved Always is effective only while (1) the cached axis result is a fresh ``pass``,
(2) the turn is ``chat_person`` / ``yolo_window``, (3) ``file_read`` reads Always and
(4) the turn has fewer than ``REPEATED_FAILURE_RUNS`` same failures; anything unreadable
caps. The gate runs for real (``NXPAgent`` + ``CheckpointHooks``) with a scripted model;
no browser starts (``run_page_check`` is a recorder), the axis is a fake or runs on an
injected clock, nothing touches the network.
"""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig, StiraEngine
from stiracore.core.engine import RULE_ALWAYS_TOOL_IDS_KEY
from stiracore.identity_paths import WritePathPolicy
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg
from stiracore.tools import page_look as pl
from stiracore.tools import tool_policy as tp
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig
from stiracore.yolo import origin, tiers
from stiracore.yolo.allow_class import allow_again_eligibility
from tests.provider_fakes import FakeModel, text_turn, tool_turn

STABILITY = {"normal": 0.9, "warning": 0.4, "critical": 0.1}
FAILING_AXIS_CODES = [code for code in pg.AXIS_CODES if code != "pass"]


class FakeAxis:
    """The cached axis result, set by the test (``check`` measures nothing)."""

    def __init__(self, code: str = "pass") -> None:
        self.code = code
        self.checks: list[bool] = []

    def current(self):
        return pg.AxisResult(self.code, (), 0.0)

    def check(self, scope, *, full: bool = False):
        self.checks.append(full)
        return self.current()


def _config(file_read: str = "always", page_look: str = "always") -> ToolConfig:
    enabled = {n for n, r in (("file_read", file_read), ("page_look", page_look)) if r != "never"}
    ask = {n for n, r in (("file_read", file_read), ("page_look", page_look)) if r == "ask"}
    return ToolConfig(mode="custom", enabled_tools=enabled, ask_tools=ask)


@pytest.fixture
def world(tmp_path, monkeypatch):
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    (ws / "index.html").write_text("<p>Secret Page Words</p>", encoding="utf-8")
    runs: list = []
    outcomes: list = []  # the next calls' results, in order (then ``ok``)

    def fake_run_page_check(target, policy, **kwargs):
        runs.append(target)
        return outcomes.pop(0) if outcomes else {"status": "ok", "screenshots": []}

    monkeypatch.setattr(pc, "run_page_check", fake_run_page_check)
    axis = FakeAxis("pass")
    monkeypatch.setattr(pc, "axis", axis)
    world = SimpleNamespace(
        ws=ws,
        runs=runs,
        outcomes=outcomes,
        axis=axis,
        config=_config(),
        rows=[],
        policy=WritePathPolicy(base_dir=ws, write_roots=[ws]),
    )
    monkeypatch.setitem(
        pl._AXIS,
        "scope",
        lambda: pc.AxisScope(
            tool_config=lambda: world.config,
            file_read_rule=lambda: tp.rule_policy(world.config, "file_read"),
        ),
    )
    monkeypatch.setitem(pl._AUDIT, "record", lambda *a, **k: world.rows.append((a, k)))
    pl.reset_counts()
    return world


def _cap(world):
    return tp.always_cap_reason("page_look", world.config)


def _gate_turn(
    world,
    *,
    hitl=None,
    mark: bool = True,
    automatic: bool = False,
    mark_agent=None,
    level: str = "normal",
    critical_action: str = "readonly",
    script=None,
):
    """One real turn: the scripted model calls ``page_look`` once (or plays ``script``), the
    gate reads the saved rules through ``rule_policy`` (the server's view) and the card
    preparer runs."""
    tool = pl.WorkspacePageLook(pl.page_look, world.policy)
    script = [tool_turn("page_look", {"target": "index.html"})] if script is None else script
    model = FakeModel([*script, text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=STABILITY[level]),
        config=StiraConfig(critical_action=critical_action),
        tools=[tool],
        on_hitl_request=hitl,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda name: tp.rule_policy(world.config, name),
    )
    agent.checkpoint_hooks.input_preparers["page_look"] = lambda tool_input: pl.prepare_input(
        tool_input, lambda: world.config
    )
    agent_id = agent.engine.agent_id if mark_agent is None else mark_agent
    with origin.human_turn(agent_id, automatic=automatic) if mark else nullcontext():
        agent("go")
    return SimpleNamespace(agent=agent, model=model)


def _approve():
    return Mock(return_value=HITLDecision(HITLAction.APPROVE))


def _reject():
    return Mock(return_value=HITLDecision(HITLAction.REJECT, reason="no"))


# --- effective ----------------------------------------------------------------------------


def test_page_look_always_effective_in_person_turn(world):
    with origin.human_turn("someone"):
        assert origin.origin_class() == "chat_person"
        assert _cap(world) is None
        assert tp.rule_policy(world.config, "page_look") == "always"
    hitl = _approve()
    _gate_turn(world, hitl=hitl)
    assert world.runs == ["index.html"]
    assert not hitl.called  # the rule answered, no card
    ((args, fields),) = world.rows
    assert args == ("page_look", "ok")
    assert (fields["answered_by"], fields["origin"], fields["axis"]) == (
        "rule_always",
        "chat_person",
        "pass",
    )
    assert fields["guard"] in pg.GUARD_STATES and fields["window"] is None


def test_page_look_driver_turn_is_chat_person(world):
    """A message an external driver posted on the chat route carries the same mark as a
    person's (O1: the route cannot tell them apart), so it is ``chat_person``."""
    with origin.human_turn("stira"):  # the chat route's mark, whoever posted
        assert origin.origin_class() == origin.origin_class("stira") == "chat_person"
        assert origin.started_by_person() is True
        assert _cap(world) is None
    _gate_turn(world, hitl=_approve())
    assert [fields["origin"] for _, fields in world.rows] == ["chat_person"]


# --- capped -------------------------------------------------------------------------------


@pytest.mark.parametrize("code", FAILING_AXIS_CODES)
def test_page_look_capped_when_axis_fails(world, code):
    world.axis.code = code
    with origin.human_turn("someone"):
        assert _cap(world) == code
        assert tp.rule_policy(world.config, "page_look") == "ask"
        prepared = pl.prepare_input({"target": "index.html"}, lambda: world.config)
    assert prepared["asks_because"] == "rule_always_capped"
    assert prepared["always"] == f"capped:{code}"
    # The saved rule is unchanged: the settings row keeps Always and names the cap.
    row = tp.tool_stack(world.config, "page_look", "normal", exposed=True)
    assert row["policy"] == "always" and row["always_cap_reason"] == code
    assert row["stack"]["rule"] == {"decision": "prompt", "reason": "rule_always_capped"}
    assert row["grant"] is None


def test_page_look_capped_when_axis_stale(world, monkeypatch, tmp_path):
    """The real axis on an injected clock: a full pass is effective until it is older than
    ``AXIS_MAX_AGE_S``; then the cached code is ``axis_stale`` and the Always is capped."""
    from tests.test_page_check_axis import Clock, _verified_guard

    clock = Clock()
    axis = pg.Axis(clock=clock, guard=_verified_guard())
    rules = SimpleNamespace(integrity_error=None, generation=1)
    scope = pg.AxisScope(
        policy=world.policy,
        tool_config=lambda: rules,
        event_store=SQLiteEventStore(tmp_path / "events.db"),
        engine=StiraEngine(state=NeuralState(), config=StiraConfig()),
        file_read_rule=lambda: "always",
        browser="/x/chrome",
    )
    monkeypatch.setattr(pc, "axis", axis)
    assert axis.check(scope, full=True).code == "pass"
    with origin.human_turn("someone"):
        assert _cap(world) is None
        clock.now += pg.AXIS_MAX_AGE_S
        assert _cap(world) is None
        clock.now += 1
        assert axis.current().code == "axis_stale"
        assert _cap(world) == "axis_stale"
        assert tp.rule_policy(world.config, "page_look") == "ask"


@pytest.mark.parametrize("rule", ["ask", "never"])
def test_page_look_capped_when_file_read_not_always(world, rule):
    world.config = _config(file_read=rule)
    with origin.human_turn("someone"):
        assert _cap(world) == "file_read_not_always"
        assert tp.rule_policy(world.config, "page_look") == "ask"
    # Without the rules (a caller that passes none) it is capped too.
    with origin.human_turn("someone"):
        assert tp.always_cap_reason("page_look", None) == "file_read_not_always"


def test_page_look_capped_after_repeated_failures(world):
    """Condition 4: ``REPEATED_FAILURE_RUNS`` failed calls of this turn — whatever the
    reason, and a success between them takes nothing back."""
    reasons = ["target_missing", "not_found", "timeout"]
    with origin.human_turn("someone"):
        for reason in reasons[: pl.REPEATED_FAILURE_RUNS - 1]:
            pl.note_outcome({"status": "refused", "reason": reason})
            pl.note_outcome({"status": "ok"})
        assert _cap(world) is None
        pl.note_outcome({"status": "error", "reason": reasons[-1]})
        assert _cap(world) == "repeated_failure"
        assert tp.rule_policy(world.config, "page_look") == "ask"
        pl.note_outcome({"status": "ok"})
        assert _cap(world) == "repeated_failure"  # not reset by a success
    with origin.human_turn("someone"):  # a new turn starts counting again
        assert _cap(world) is None


def test_page_look_capped_in_bot_and_webhook_turns(world):
    """A bot, a webhook, a job or a resume sets no live mark (``unknown``); a mark of
    another agent is ``child``. Each caps ``origin_not_covered``, and the real gate never
    lets the rule answer: a card where an approver exists, a refusal where none does."""
    assert origin.origin_class() == "unknown"
    assert _cap(world) == "origin_not_covered"
    assert tp.rule_policy(world.config, "page_look") == "ask"
    # A bot / webhook turn: no mark, no approver: refused, nothing runs, no rule answer.
    _gate_turn(world, mark=False)
    assert world.runs == [] and world.rows == []
    # Another agent's live mark (child): the gate reads its own agent's class.
    hitl = _reject()
    _gate_turn(world, hitl=hitl, mark_agent="another-agent")
    assert hitl.called and world.runs == []
    with origin.human_turn("another-agent"):
        assert origin.origin_class("this-agent") == "child"
    # A context that outlived its turn carries a dead mark: unknown again.
    import contextvars

    with origin.human_turn("someone"):
        copied = contextvars.copy_context()
    assert copied.run(origin.origin_class) == "unknown"
    assert copied.run(_cap, world) == "origin_not_covered"


@pytest.mark.parametrize("automatic", [False, True], ids=["chat_person", "yolo_window"])
def test_page_look_capped_is_a_card_in_every_window(world, automatic):
    """In a person's turn and in a window's automatic continue: while capped the saved
    Always is a person's card (``PERSON_TOOLS``: a window never answers it); while
    effective the rule answers and the row names the origin."""
    assert "page_look" in tiers.PERSON_TOOLS and tiers.tool_class("page_look") == "exec"
    assert allow_again_eligibility("page_look", {}, world.policy, "turn") == "person_only"
    world.axis.code = "browser_guard_unverified"
    hitl = _reject()
    _gate_turn(world, hitl=hitl, automatic=automatic)
    assert hitl.called and world.runs == [] and world.rows == []
    request = hitl.call_args.args[0]
    assert request.tool_name == "page_look"
    assert request.tool_input["asks_because"] == "rule_always_capped"
    assert request.tool_input["always"] == "capped:browser_guard_unverified"
    # Effective again: the rule answers, no card.
    world.axis.code = "pass"
    hitl = _reject()
    _gate_turn(world, hitl=hitl, automatic=automatic)
    assert not hitl.called and world.runs == ["index.html"]
    expected = "yolo_window" if automatic else "chat_person"
    assert [(f["answered_by"], f["origin"]) for _, f in world.rows] == [("rule_always", expected)]


def test_page_tools_listed_but_refused_at_warning(world):
    """#259: at WARNING ``page_look`` stays in the list; its call is refused with
    ``breaker_write_refused`` before any card, and nothing runs."""
    hitl = _approve()
    turn = _gate_turn(world, hitl=hitl, level="warning")
    assert "page_look" in turn.model.requests[0]["tool_names"]
    assert "breaker_write_refused" in str(turn.model.requests[1]["messages"][-1])
    assert world.runs == [] and not hitl.called and world.rows == []
    assert "page_look" in StiraConfig().blocked_tool_names


def test_page_tools_card_under_halt(world):
    """CRITICAL + ``halt``: the effective Always is not consulted, every call is a card
    and a person's answer is never recorded as the rule's."""
    hitl = _approve()
    _gate_turn(world, hitl=hitl, level="critical", critical_action="halt")
    assert hitl.called and hitl.call_args.args[0].tool_name == "page_look"
    assert world.runs == ["index.html"]
    assert world.rows == []  # a person's card answered it: no rule row


def test_rule_answer_rechecks_before_the_browser(world):
    """A call the rule answered checks the conditions once more in the tool: capped by
    then, no browser starts and the call is refused with the cap code."""
    import asyncio

    tool = pl.WorkspacePageLook(pl.page_look, world.policy)
    use = {"toolUseId": "t1", "name": "page_look", "input": {"target": "index.html"}}
    state = {RULE_ALWAYS_TOOL_IDS_KEY: {"t1"}}
    world.axis.code = "record_unhealthy"

    async def run():
        return [event async for event in tool.stream(use, state)]

    with origin.human_turn("someone"):
        assert pl.reserve("t1") is True  # the gate's slot for this call
        asyncio.run(run())
    assert world.runs == []
    ((args, fields),) = world.rows
    assert args == ("page_look", "refused")
    assert (fields["reason"], fields["answered_by"]) == ("record_unhealthy", "rule_always")
    # Without a wired scope (no file_read rule to read) the recheck caps too.
    world.axis.code = "pass"
    pl._AXIS["scope"] = None
    with origin.human_turn("someone"):
        assert pl.live_cap_reason() == "file_read_not_always"


# --- the save -----------------------------------------------------------------------------


def test_saving_always_is_a_loosening(world, tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from flask import Flask

    from stiracore.observability import settings_hub as hub
    from stiracore.observability.config_api import ConfigStore, create_config_blueprint

    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_look", pl.page_look)
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(
        mode="custom",
        enabled_tools={"page_look", "file_read"},
        ask_tools={"page_look"},
        path=store.tool_path,
    ).save()

    def client(level):
        app = Flask(__name__)
        app.register_blueprint(create_config_blueprint(store, lambda: level))
        return app.test_client()

    body = {"name": "page_look", "policy": "always", "confirm": True}
    for level in ("warning", "critical"):
        refused = client(level).post("/api/config/tools", json=body)
        assert refused.status_code == 403
        assert refused.get_json()["error_code"] == "breaker_active"
    normal = client("normal")
    unconfirmed = normal.post("/api/config/tools", json={"name": "page_look", "policy": "always"})
    assert unconfirmed.status_code == 400
    assert unconfirmed.get_json()["needs_confirmation"] == ["tool_policy_loosened"]
    untyped = normal.post("/api/config/tools", json=body)
    assert untyped.status_code == 400
    assert untyped.get_json()["needs_tool_names"] == ["page_look"]
    assert store.load_tools().policy_of("page_look") == "ask"
    saved = normal.post("/api/config/tools", json={**body, "confirm_always": ["page_look"]})
    assert saved.status_code == 200
    assert tp.rule_policy(store.load_tools(), "page_look", effective=False) == "always"
    assert [entry["source"] for entry in hub.read_history(store.history_path, 10)] == ["api:tools"]
    # The stated grant names every condition (ADR 0010 §5) while it is effective.
    grant = tp.persistent_grant("page_look", "always")
    assert grant["conditions"] == list(tp.PAGE_LOOK_CONDITIONS) and len(grant["conditions"]) == 4
    row = tp.tool_stack(store.load_tools(), "page_look", "normal", exposed=True)
    assert row["grant"] == grant and "always_cap_reason" not in row
    world.axis.code = "rules_changed"
    capped = tp.tool_stack(store.load_tools(), "page_look", "normal", exposed=True)
    assert capped["always_cap_reason"] == "rules_changed" and capped["grant"] is None


# --- fail closed --------------------------------------------------------------------------


def test_page_look_cap_on_exception_in_reader(world, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("/secret/path")

    with origin.human_turn("someone"):
        assert _cap(world) is None  # the premise
        for target, name in (
            (world.axis, "current"),
            (origin, "origin_class"),
            (pl, "_turn_limit"),
        ):
            with monkeypatch.context() as patch:
                patch.setattr(target, name, broken)
                assert _cap(world) == "cap_unreadable", name
                assert tp.rule_policy(world.config, "page_look") == "ask", name
                assert tp.always_capped("page_look", world.config) is True
        with monkeypatch.context() as patch:
            patch.setattr(tp, "rule_policy", broken)  # file_read's rule cannot be read
            assert tp.always_cap_reason("page_look", world.config) == "cap_unreadable"
        with monkeypatch.context() as patch:
            patch.setattr(pl, "always_cap_reason", broken)
            assert pl.live_cap_reason() == "cap_unreadable"
    assert "secret" not in repr(_cap(world))


def test_children_still_never_get_page_look_with_effective_always(world):
    from tests.test_delegate_tool import Probe, build, delegate_turn, results, text_of

    world.config = ToolConfig(
        mode="custom", enabled_tools={"page_look", "file_read", "lookup", "delegate"}
    )
    calls: list = []
    for requested in (["page_look", "lookup"], None):
        arguments = {} if requested is None else {"tools": requested}
        parent, _tool, model, _rules, _ = build(
            [delegate_turn(**arguments), text_turn("child"), text_turn("done")],
            tools=[Probe("page_look", calls), Probe("lookup", calls)],
            tool_policy=lambda name: tp.rule_policy(world.config, name),
        )
        with origin.human_turn(parent.engine.agent_id):
            assert tp.rule_policy(world.config, "page_look") == "always"  # effective
            parent("go")
        assert "page_look" in model.requests[0]["tool_names"]
        assert model.requests[1]["tool_names"] == ["lookup"]
        text = text_of(results(parent.agent.messages)["d1"])
        assert "status: done" in text
        assert ("not given: page_look" in text) is (requested is not None)
    assert calls == []


# --- the record's key ---------------------------------------------------------------------


def test_target_hash_key_is_stable_across_restarts(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(pl, "_KEY_STATE", {"dir": None, "key": None})
    store = SQLiteEventStore(data / "events.db")
    try:
        pl.set_audit(AuditChain(store).record_page)  # the server's one call
        first = pl.target_ref("index.html")
        key_file = data / pl.KEY_FILE
        assert key_file.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        # A restart: a new process key, the same data directory.
        monkeypatch.setattr(pl, "_PROCESS_KEY", os.urandom(32))
        pl.set_key_dir(data)
        assert pl.target_ref("index.html") == first
        assert pl.target_ref("other.html") != first
        # Another install has another key.
        other = tmp_path / "other"
        other.mkdir()
        pl.set_key_dir(other)
        assert pl.target_ref("index.html") != first
        # An unsafe key file is neither used nor replaced: this process's key instead.
        if os.name == "posix":
            bad = tmp_path / "bad"
            bad.mkdir()
            (bad / pl.KEY_FILE).write_text("00" * 32 + "\n", encoding="ascii")
            os.chmod(bad / pl.KEY_FILE, 0o644)
            pl.set_key_dir(bad)
            ref = pl.target_ref("index.html")
            assert ref.startswith("page:") and (bad / pl.KEY_FILE).read_text() == "00" * 32 + "\n"
            import hashlib
            import hmac

            weak = hmac.new(bytes(32), b"index.html", hashlib.sha256).hexdigest()
            assert ref != "page:" + weak
    finally:
        store.close()
    assert Path(data / pl.KEY_FILE).read_text(encoding="ascii").strip() != ""
