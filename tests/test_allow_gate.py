"""ADR 0025 PR 3: the session allowance at the gate, the two buttons through the decision
route, and Make Always through the hub — the §10 checklist one test at a time, and the
page-level races of §4. Offline, no model (a real ``StiraEngine``, a real hash-chained
event store, a real ``WritePathPolicy``; the approver is the server's ``AllowanceService``).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.agent.checkpoint_hooks import (
    ALLOW_INTENTS_KEY,
    NEVER_REFUSAL,
    CheckpointHooks,
)
from stiracore.core import HITLAction, StiraConfig, StiraEngine
from stiracore.core.engine import (
    HITL_APPROVED_TOOL_IDS_KEY,
    BreakLevel,
    HITLDecision,
    HITLRequest,
)
from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import tool_consent
from stiracore.yolo import allow_runtime, origin
from stiracore.yolo import session_allow as sa
from stiracore.yolo.allow_class import ORIGIN_CHAT, ClassPolicy
from stiracore.yolo.allow_service import AllowanceService
from stiracore.yolo.host_ops import ClockReading


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> ClockReading:
        return ClockReading(wall=self.t, boot=self.t, mono=self.t)


class World:
    """One gate, one engine, one allowance service over a real audit chain."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.ws = Path(os.path.realpath(tmp_path)) / "ws"
        (self.ws / "game" / "sub").mkdir(parents=True)
        (self.ws / "other").mkdir()
        self.policy = WritePathPolicy(base_dir=self.ws, write_roots=[WORKSPACE_ROOT])
        self.clock = Clock()
        self.table = sa.AllowanceTable(clock=self.clock)
        monkeypatch.setattr(sa, "_TABLE", self.table)
        self.store = SQLiteEventStore(tmp_path / "events.db", namespace="audit")
        self.chain = AuditChain(self.store)
        self.rules = {}
        self.generation = 7
        self.level = "normal"
        self.fail_rows = False
        self.service = AllowanceService(
            self.table,
            record=self._record,
            ledger=self.chain,
            actor="local:tester",
            breaker=lambda: self.level,
            rules=lambda tool: (self.rules.get(tool, "ask"), self.generation),
            roots=lambda: sa.roots_digest(self.policy),
            store_lock=threading.RLock(),
        )
        allow_runtime._reset_for_tests()
        allow_runtime.install(self.service, policy=self.policy)
        self.engine = StiraEngine(config=StiraConfig())
        self.asked = []

        def hitl(request):
            self.asked.append(request.tool_name)
            return HITLDecision(action=HITLAction.REJECT, reason="test person says no")

        self.engine.on_hitl_request = hitl
        self.hooks = CheckpointHooks(
            self.engine,
            policy_check=lambda name: (self.rules.get(name, "ask"), self.generation),
            scope_policy=self.policy,
        )
        self.hooks.set_allow_approver(self.service)

    def _record(self, event_type, **fields):
        if self.fail_rows:
            raise OSError("disk full")
        return self.chain.record_yolo(event_type, **fields)

    def args(self, rel="game/a.py"):
        return {"path": str(self.ws / rel), "content": "x"}

    def allow(self, tool="file_write", tool_input=None, request_id="r1"):
        """A person pressed button 1 on a card of ``tool`` (the stored request)."""
        tool_input = tool_input or self.args()
        view = self.service.offer(
            request_id=request_id,
            args_hash="h" * 64,
            tool=tool,
            tool_input=tool_input,
            policy=self.policy,
            origin=ORIGIN_CHAT,
        )
        assert view["offered"], view
        result = self.service.press(
            view["token"],
            request_id=request_id,
            args_hash="h" * 64,
            tool=tool,
            tool_input=tool_input,
            policy=self.policy,
            origin=ORIGIN_CHAT,
            decide=lambda: True,
        )
        assert result.status == "allowed", result
        return result

    def call(self, name="file_write", args=None):
        event = SimpleNamespace(
            tool_use={
                "name": name,
                "toolUseId": secrets.token_hex(8),
                "input": args if args is not None else self.args("game/b.py"),
            },
            cancel_tool=None,
            invocation_state={},
            result=None,
            exception=None,
        )
        self.hooks.before_tool(event)
        self.hooks.binding_verifier.before_tool(event)
        return event

    def finish(self, event, status="success"):
        event.result = {"toolUseId": event.tool_use["toolUseId"], "status": status, "content": []}
        self.hooks.binding_verifier.after_tool(event)

    def rows(self, event_type):
        return [e.attributes for e in self.store.get_events(limit=500, event_type=event_type)]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    with origin.human_turn(world.engine.agent_id):
        yield world
    allow_runtime._reset_for_tests()


def _answered(world, event):
    return event.cancel_tool is None and world.asked == []


# --- the allowance answers the same tool on the same class, and nothing else -----------------


def test_same_tool_same_directory_runs_without_a_card_and_is_audited(w):
    w.allow()
    event = w.call()
    assert _answered(w, event)
    # Not a human approval anywhere (IdentityGuard's breaker exception, exec marks).
    assert event.tool_use["toolUseId"] not in event.invocation_state.get(
        HITL_APPROVED_TOOL_IDS_KEY, set()
    )
    w.finish(event)
    (started,) = [r for r in w.rows("audit.tool.started") if r["decision"] == "auto_approved"]
    assert started["authorizer_kind"] == "session_allow"
    # The reference chain of §7: call → issued → the person's decision row.
    (issued,) = w.rows("audit.allow.issued")
    assert started["issued_ref"] == w.service.table.get(started["allow_id"]).issued_ref
    assert issued["decision_receipt_ref"].startswith("op:")
    (decided,) = w.rows("audit.allow.decided")
    assert decided["button"] == "allow_again"
    assert len(w.rows("audit.tool.committed")) == 1
    assert allow_runtime.status()["seq"] == 1  # the chat's log line


def test_below_the_class_directory_is_the_same_class(w):
    w.allow()
    assert _answered(w, w.call(args=w.args("game/sub/c.py")))


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("file_write", {"rel": "other/a.py"}),  # another directory of the same root
        ("editor", {"command": "create", "rel": "game/b.py"}),  # another tool
        ("shell", {"command": "ls"}),  # exec: never covered
        ("mcp_server_tool", {"x": 1}),  # MCP / unclassified: never covered
    ],
)
def test_another_class_or_tool_raises_a_card(w, name, args):
    w.allow()
    rel = args.pop("rel", None)
    if rel is not None:
        args = {**args, "path": str(w.ws / rel), "file_text": "x"}
    event = w.call(name, args)
    assert w.asked == [name], "a person is asked again"
    assert event.cancel_tool  # the test person rejected the card


def test_secret_looking_arguments_raise_a_card(w):
    w.allow()
    w.call(args={"path": str(w.ws / "game/k.py"), "content": "AKIA" + "B" * 16})
    assert w.asked == ["file_write"]


def test_never_and_floors_are_decided_before_the_allowance(w):
    w.allow()
    w.rules["file_write"] = "never"
    assert w.call().cancel_tool == NEVER_REFUSAL
    w.rules.pop("file_write")
    w.generation += 0  # unchanged: a Never → Ask round trip is not a save here
    # A protected location (the write floor, the secret-path floor): refused before the slot.
    w.hooks.path_check = lambda name, args: SimpleNamespace(reason="protected location")
    event = w.call()
    assert "protected location" in event.cancel_tool and w.asked == []
    w.hooks.path_check = None


def test_always_needs_no_allowance_and_uses_none(w):
    result = w.allow()
    w.rules["file_write"] = "always"
    assert w.call().cancel_tool is None
    assert w.service.table.get(result.allow_id).uses == 0


def test_breaker_write_block_and_critical_halt_are_not_answered(w, monkeypatch):
    w.allow()
    halt = SimpleNamespace(
        level=__import__("stiracore.core.engine", fromlist=["BreakLevel"]).BreakLevel.CRITICAL,
        actions=__import__("stiracore.core.engine", fromlist=["BreakAction"]).BreakAction.HALT,
    )
    monkeypatch.setattr(w.engine, "breaker_status", lambda: halt)
    w.call()
    assert w.asked == ["file_write"], "CRITICAL_HALT goes to a person, never to an allowance"
    blocked = SimpleNamespace(
        level=halt.level.__class__.WARNING,
        actions=__import__(
            "stiracore.core.engine", fromlist=["BreakAction"]
        ).BreakAction.BLOCK_WRITES,
    )
    monkeypatch.setattr(w.engine, "breaker_status", lambda: blocked)
    assert "blocked by the circuit breaker" in w.call().cancel_tool


def test_only_a_persons_chat_turn_is_covered(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.allow()
    world.call()  # no human turn: a job, a webhook, a resume
    assert world.asked == ["file_write"]
    with origin.human_turn(world.engine.agent_id, automatic=True):
        world.call()  # a window's automatic continue
    assert world.asked == ["file_write", "file_write"]
    with origin.human_turn("another-agent"):
        world.call()  # a child agent's gate
    assert len(world.asked) == 3


# --- every end condition of §5 ------------------------------------------------------------


@pytest.mark.parametrize(
    "end",
    ["time", "new_session", "stop", "breaker", "rule_save", "roots", "revoke"],
)
def test_every_end_raises_the_card_again(w, end):
    result = w.allow()
    assert _answered(w, w.call())
    if end == "time":
        w.clock.t += 2 * 3600
    elif end == "new_session":
        sa.publish_end("new_session")
    elif end == "stop":
        sa.publish_end("stop")
    elif end == "breaker":
        w.level = "warning"
    elif end == "rule_save":
        sa.publish_end("rule_changed")
        w.generation += 1
    elif end == "roots":
        w.policy = WritePathPolicy(base_dir=w.ws, write_roots=[str(w.ws / "game")])
        w.hooks.scope_policy = w.hooks.binding_verifier.scope_policy = w.policy
    elif end == "revoke":
        w.service.table.revoke(result.allow_id)
    w.call()
    assert w.asked == ["file_write"], end
    if end == "breaker":
        w.level = "normal"  # back to NORMAL: the ended allowance does not come back
        w.call()
        assert w.asked == ["file_write", "file_write"]


def test_a_restart_leaves_nothing(tmp_path, monkeypatch):
    first = World(tmp_path / "a", monkeypatch)
    first.allow()
    second = World(tmp_path / "b", monkeypatch)  # a new process: a new table
    with origin.human_turn(second.engine.agent_id):
        second.call()
    assert second.asked == ["file_write"]


# --- audit failure is fail-closed ------------------------------------------------------------


def test_an_audit_row_that_cannot_be_written_refuses_the_call_and_ends_everything(w):
    w.allow()
    w.fail_rows = True
    event = w.call()
    assert event.cancel_tool and "allowance ended" in event.cancel_tool
    w.fail_rows = False
    assert w.service.table.active() == []
    w.call()
    assert w.asked == ["file_write"]  # a card; the buttons stay hidden until restart
    assert w.service.offer(
        request_id="r9",
        args_hash="h" * 64,
        tool="file_write",
        tool_input=w.args(),
        policy=w.policy,
        origin=ORIGIN_CHAT,
    ) == {"offered": False, "reason": "audit_failed"}


def test_a_failed_call_writes_a_failed_outcome(w):
    w.allow()
    event = w.call()
    w.finish(event, status="error")
    assert len(w.rows("audit.tool.failed")) == 1
    assert ALLOW_INTENTS_KEY in event.invocation_state


# --- the class is bound at the open (§3) ----------------------------------------------------


def test_the_class_policy_refuses_a_parent_swapped_for_a_link(tmp_path):
    base = tmp_path / "game"
    (base / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    policy = ClassPolicy(str(base))
    target = base / "sub" / "a.py"
    assert policy.check(str(target)).allowed
    (base / "sub").rmdir()
    (base / "sub").symlink_to(other, target_is_directory=True)  # swapped after the check
    decision = policy.check(str(target))
    assert not decision.allowed and "left the directory" in decision.reason


def test_an_allowance_authorized_write_carries_its_class_into_the_grant(w, monkeypatch):
    """The verifier binds the class to the call's own gate mark; the consent grant of that
    mark (the file tools' ``open_checked`` policies) then includes it."""
    bound = []
    monkeypatch.setattr(tool_consent, "bind_mark_policy", lambda m, p: bound.append((m, p)))
    w.allow()
    owned = {}

    def issue(event, *, exec_mark_allowed=False):
        mark = tool_consent.GateMark("file_write", event.tool_use["toolUseId"], "d")
        owned[id(event.tool_use)] = (event.tool_use, mark)
        event.invocation_state[tool_consent.GATE_ISSUED_KEY] = owned

    monkeypatch.setattr(w.hooks.binding_verifier, "_issue_gate_mark", issue)
    event = w.call()
    assert event.cancel_tool is None
    ((mark, policy),) = bound
    assert isinstance(policy, ClassPolicy) and policy.base == w.ws / "game"
    # The grant of that mark carries the class as one more policy.
    monkeypatch.undo()
    tool_consent.bind_mark_policy(mark, policy)
    monkeypatch.setattr(tool_consent, "bridge_refusal", lambda name: None)
    with tool_consent.consent_granted("file_write", mark, write_path="x", policy=None):
        assert policy in tool_consent.active_grant("file_write").policies
    tool_consent.release_mark_policy(mark)
    with tool_consent.consent_granted("file_write", mark, write_path="x", policy=None):
        assert policy not in tool_consent.active_grant("file_write").policies


# --- the two buttons through the decision route (page races, §4) ---------------------------


class Card:
    """A real broker with one pending card raised in a person's chat turn."""

    def __init__(self, world, tool="file_write", tool_input=None) -> None:
        self.world = world
        self.broker = HITLApprovalBroker()
        app = Flask(__name__)
        app.register_blueprint(
            create_chat_blueprint(object(), runner=lambda m: "unused", hitl_broker=self.broker)
        )
        self.client = app.test_client()
        self.decision = {}
        tool_input = tool_input or world.args()

        def worker():
            with origin.human_turn(world.engine.agent_id):
                self.decision["d"] = self.broker.request(
                    HITLRequest(
                        tool_name=tool,
                        tool_input=tool_input,
                        break_level=BreakLevel.NORMAL,
                        reason="Tool policy is Ask: every call of this tool needs a human "
                        "approval.",
                        agent_id=world.engine.agent_id,
                    )
                )

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.broker.pending():
                break
            time.sleep(0.01)

    def view(self):
        (item,) = self.client.get("/api/hitl/pending").get_json()["requests"]
        return item

    def press(self, item, kind="session", token=None):
        return self.client.post(
            f"/api/hitl/{item['id']}/decision",
            json={
                "action": "approve",
                "args_hash": item["args_hash"],
                "allow_again": kind,
                "view_token": token or item["allow_again"]["token"],
            },
        )

    def approved(self):
        self.thread.join(timeout=5)
        return self.decision.get("d")


def test_the_card_offers_both_buttons_and_a_press_creates_one_allowance(w):
    card = Card(w)
    item = card.view()
    assert item["allow_again"]["offered"] is True and item["allow_again"]["label"] == "game/"
    assert "allow_origin" not in item
    response = card.press(item)
    assert response.status_code == 202
    assert response.get_json()["allow"]["status"] == "allowed"
    assert card.approved().action == HITLAction.APPROVE
    assert len(w.service.table.active()) == 1


def test_rule_save_after_the_card_was_shown_then_a_delayed_click_approves_nothing(w):
    card = Card(w)
    item = card.view()
    sa.publish_end("rule_changed")  # a rule save (still Ask)
    w.generation += 1
    response = card.press(item)
    assert (response.status_code, response.get_json()["code"]) == (409, "stale_view")
    assert card.broker.pending(), "the card stays pending"
    fresh = card.view()  # the page re-reads its view: a new token
    assert card.press(fresh).status_code == 202
    assert card.approved().action == HITLAction.APPROVE


def test_revoke_all_after_the_card_was_shown_then_a_delayed_click_approves_nothing(w):
    card = Card(w)
    item = card.view()
    sa.publish_end("stop")
    assert card.press(item).get_json()["code"] == "stale_view"
    assert card.broker.pending() and w.service.table.active() == []
    card.broker.close()


def test_the_same_token_pressed_twice_approves_once(w):
    card = Card(w)
    item = card.view()
    assert card.press(item).status_code == 202
    second = card.press(item)
    assert second.status_code in (404, 409)
    assert len(w.service.table.active()) == 1


def test_an_edited_request_or_an_exec_card_is_not_offered(w):
    card = Card(w, tool="shell", tool_input={"command": "ls"})
    item = card.view()
    assert item["allow_again"] == {"offered": False, "reason": "exec"}
    response = card.press(item, token="forged")
    assert response.get_json()["code"] in ("stale_view", "class_changed")
    assert card.broker.pending()
    card.broker.close()


# --- Make Always (§9) -----------------------------------------------------------------------


def _hub(tmp_path):
    from test_settings_hub import _client, _store

    store = _store(tmp_path / "cfg")
    store.tool_path.parent.mkdir(parents=True, exist_ok=True)
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    tools = state["values"]["tools"]
    tools["enabled_tools"] = sorted({*tools["enabled_tools"], "file_write"})
    tools["ask_tools"] = sorted({*tools["ask_tools"], "file_write"})
    saved = client.post(
        "/api/config/hub",
        json={
            "changes": {"tools": tools},
            "base_version": state["version"],
            "acknowledged_dangers": state.get("dangers", []),
        },
    )
    assert saved.status_code == 200, saved.get_json()
    return client, store


def _press_always(w, request_id="c1"):
    view = w.service.offer(
        request_id=request_id,
        args_hash="h" * 64,
        tool="file_write",
        tool_input=w.args(),
        policy=w.policy,
        origin=ORIGIN_CHAT,
    )
    result = w.service.press_always(
        view["token"],
        request_id=request_id,
        args_hash="h" * 64,
        tool="file_write",
        tool_input=w.args(),
        policy=w.policy,
        origin=ORIGIN_CHAT,
        decide=lambda: True,
    )
    assert result.status == "approved_once", result
    return result


def _save(client, view, request_id="c1", **extra):
    return client.post(
        "/api/config/hub",
        json={
            "source": "approval_card",
            "request_id": request_id,
            "save_token": view["token"],
            "base_version": view["base_version"],
            "acknowledged_dangers": view["needs_confirmation"],
            "confirm_always": view["confirm_names"],
            **extra,
        },
    )


def test_make_always_waits_for_the_call_then_saves_through_the_hub(w, tmp_path):
    client, store = _hub(tmp_path)
    _press_always(w)
    waiting = client.get("/api/config/hub/card/c1").get_json()
    assert waiting["state"] == "waiting"
    early = client.post(
        "/api/config/hub",
        json={"source": "approval_card", "request_id": "c1", "save_token": "x"},
    )
    assert early.get_json()["code"] == "call_not_finished"
    w.service.terminal("c1", "committed")
    view = client.get("/api/config/hub/card/c1").get_json()
    assert view["state"] == "ready" and view["terminal"] == "committed"
    assert view["confirm_names"] == ["file_write"]  # Q7: the hub's typed name
    missing = _save(client, view, confirm_always=[])
    assert missing.status_code == 400  # the typed name is required
    view = client.get("/api/config/hub/card/c1").get_json()
    saved = _save(client, view)
    assert saved.status_code == 200, saved.get_json()
    rules = json.loads(store.tool_path.read_text())
    assert "file_write" not in rules.get("ask_tools", [])
    history = client.get("/api/config/history").get_json()["entries"]
    assert history[0]["source"] == "approval_card"
    again = _save(client, view)
    assert again.get_json()["code"] in ("op_token_used", "stale_confirmation")


def test_a_make_always_confirmation_delayed_across_a_stop_saves_nothing(w, tmp_path):
    client, store = _hub(tmp_path)
    _press_always(w)
    w.service.terminal("c1", "committed")
    view = client.get("/api/config/hub/card/c1").get_json()
    before = store.tool_path.read_text()
    sa.publish_end("stop")
    refused = _save(client, view)
    assert (refused.status_code, refused.get_json()["code"]) == (409, "stale_confirmation")
    assert store.tool_path.read_text() == before
    fresh = client.get("/api/config/hub/card/c1").get_json()  # confirm anew: a new token
    assert _save(client, fresh).status_code == 200


def test_a_card_save_names_no_change_and_needs_its_token(w, tmp_path):
    client, _store = _hub(tmp_path)
    _press_always(w)
    w.service.terminal("c1", "committed")
    bad = client.post(
        "/api/config/hub",
        json={"source": "approval_card", "request_id": "c1", "changes": {"tools": {}}},
    )
    assert bad.status_code == 400
    unknown = client.get("/api/config/hub/card/nope")
    assert unknown.status_code == 404
