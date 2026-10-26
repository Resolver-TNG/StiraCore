"""#448: what ran in a YOLO window — every tool call of the window's period, by decision.

The window's ``used`` / ``external_used`` / ``exec_used`` count automatic approvals only
(what the caps limit). These tests pin the additive counter next to them:

* on the real chat server app (offline, scripted model, browser channel): one turn with
  Always × 2, YOLO automatic × 3, a person's approval × 1 and a refusal × 1 gives exactly
  that breakdown in ``/api/yolo/status`` ``window.tool_calls`` and, after the stop, in
  ``audit.yolo.ended`` (and ``last_end``); calls before the window and after its end are
  not counted; ``used`` keeps its meaning; nothing of the arguments, the outputs or the
  query reaches the status or the row; the event chain still verifies;
* a server stop (what a restart does first) ends the window with the counts of that
  process; a window its cap ends still includes its last call (counted with the slot);
* the gate's classification on the real ``CheckpointHooks`` + ``ApprovalBindingVerifier``
  (a refused automatic approval is ``denied``, a person's RESPOND is ``manual``, an error
  result adds ``failed``) and the row contract (optional fields; bad values ``invalid``).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from stiracore.agent.checkpoint_hooks import (
    YOLO_COUNTED_KEY,
    YOLO_DECIDED_KEY,
    YOLO_RAN_IN_KEY,
    CheckpointHooks,
)
from stiracore.core import HITLAction, StiraConfig, StiraEngine
from stiracore.core.engine import HITLDecision
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.yolo import tool_stats
from stiracore.yolo.audit_schema import AuditContractError, build
from stiracore.yolo.window import WindowMarker
from tests import test_yolo_exec_app as _exec_app
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_yolo_auto_approve import AGENT, _Approver, _call, _execute
from tests.test_yolo_auto_approve import _gate as _base_gate
from tests.test_yolo_browser_confirm import _confirm_request
from tests.test_yolo_exec_app import _chat
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.test_yolo_window import _open as _open_service
from tests.test_yolo_window import _service

make_app = _exec_app.make_app  # the real-app fixture of phase 7a

SECRET_QUERY = "query-text-that-must-not-leak"
SECRET_BODY = "body-text-that-must-not-leak"


def _open(client):
    response = client.post("/api/yolo/request", json={"tier": "internal"})
    assert response.status_code == 202, response.get_json()
    confirmed = _confirm_request(client, response.get_json())
    assert confirmed.status_code == 200, confirmed.get_json()
    return client.get("/api/yolo/status").get_json()["window"]


def _turn(app, client, model, key, script, approve=()):
    """One chat turn of ``script`` (tool turns, then a text turn). A card is approved by a
    person when its tool is in ``approve``, else rejected. Returns the cards' tool names."""
    model.script = [*script, text_turn("done")]
    broker = app.extensions["hitl_broker"]
    done: dict = {}
    worker = threading.Thread(target=lambda: done.setdefault("body", _chat(app, client, key)))
    worker.start()
    cards: list[str] = []
    answered: set[str] = set()
    deadline = time.monotonic() + 60
    while worker.is_alive() and time.monotonic() < deadline:
        for item in broker.pending():
            if item["id"] in answered:
                continue
            answered.add(item["id"])
            cards.append(item["tool_name"])
            if item["tool_name"] in approve:
                decision = HITLDecision(HITLAction.APPROVE)
                outcome = broker.decide(item["id"], decision, args_hash=item["args_hash"])
            else:
                outcome = broker.decide(item["id"], HITLDecision(HITLAction.REJECT, reason="no"))
            assert outcome is not None
        time.sleep(0.01)
    worker.join(5)
    assert "body" in done, "the turn did not finish"
    return cards


def _store(app) -> SQLiteEventStore:
    return app.extensions["yolo_settings_record"].__self__.store


def _ended_rows(app):
    return [e.attributes for e in _store(app).get_events(event_type="audit.yolo.ended")]


def _workspace(app):
    return app.extensions["test_agent"].checkpoint_hooks.scope_policy.base_dir


def _mixed_turn(app):
    """Always × 2 (``file_read``), YOLO × 3 (``file_write`` in the workspace), a person ×
    1 (``web_search``: Ask, external — a card in a window of tier internal) and a
    refusal × 1 (``file_write`` of the protected ``SOUL.md``: the floor)."""
    workspace = _workspace(app)
    (workspace / "seen.md").write_text("seen\n")
    return [
        tool_turn("file_read", {"path": "seen.md"}, "use-r1"),
        tool_turn("file_write", {"path": "a.md", "content": SECRET_BODY}, "use-w1"),
        tool_turn("file_read", {"path": "seen.md"}, "use-r2"),
        tool_turn("file_write", {"path": "b.md", "content": SECRET_BODY}, "use-w2"),
        tool_turn("web_search", {"query": SECRET_QUERY}, "use-s1"),
        tool_turn("file_write", {"path": "c.md", "content": SECRET_BODY}, "use-w3"),
        tool_turn("file_write", {"path": "SOUL.md", "content": SECRET_BODY}, "use-soul"),
    ]


EXPECTED = {
    "total": 7,
    "by_decision": {"auto_approved": 3, "always": 2, "manual": 1, "denied": 1, "allowed_again": 0},
    "by_category": {"internal": 6, "external": 1, "exec": 0},
    "by_tool": {"file_read": 2, "file_write": 4, "web_search": 1},
}


def _without_failed(calls):
    return {key: value for key, value in calls.items() if key != "failed"}


def test_the_window_counts_each_decision_of_a_real_turn(make_app):
    model = FakeModel([])
    app = make_app(allowance="internal", exec_tools=False, model=model, ask=())
    client = app.test_client()
    workspace = _workspace(app)
    (workspace / "seen.md").write_text("seen\n")
    # Before the window: a call that runs is not counted anywhere.
    _turn(app, client, model, "before", [tool_turn("file_read", {"path": "seen.md"}, "use-0")])
    window = _open(client)
    assert window["tool_calls"] == tool_stats.empty() and window["turns"] == 0

    cards = _turn(app, client, model, "mixed", _mixed_turn(app), approve=("web_search",))
    assert cards == ["web_search"]  # the one card a person answered
    assert sorted(p.name for p in workspace.glob("*.md")) == ["a.md", "b.md", "c.md", "seen.md"]
    status = client.get("/api/yolo/status").get_json()
    window = status["window"]
    calls = window["tool_calls"]
    assert _without_failed(calls) == EXPECTED, calls
    # The person's web_search ran and failed (no endpoint configured): one error result.
    assert calls["failed"] == 1
    assert window["turns"] == 1
    # The caps' counts keep their meaning: automatic approvals only.
    assert (window["used"], window["auto_approved"], window["reserved"]) == (3, 3, 0)
    assert len(status["auto_approvals"]["items"]) == 3

    stopped = client.post("/api/yolo/stop").get_json()
    [row] = _ended_rows(app)
    assert row["end_reason"] == "revoked_stop" and row["used"] == 3
    assert row["tool_calls"] == calls and row["turns"] == 1
    assert stopped["last_end"]["tool_calls"] == calls and stopped["last_end"]["turns"] == 1

    # After the end: a call that runs is not counted (the row and last_end stay).
    _turn(app, client, model, "after", [tool_turn("file_read", {"path": "seen.md"}, "use-9")])
    assert client.get("/api/yolo/status").get_json()["last_end"]["tool_calls"] == calls
    assert len(_ended_rows(app)) == 1

    # Counts only: no argument, output, file content or query anywhere.
    text = json.dumps([status, stopped, row])
    for leaked in (SECRET_QUERY, SECRET_BODY, "seen.md", "SOUL", "a.md"):
        assert leaked not in text, leaked
    # The audit chain verifies as before.
    assert _store(app).verify_chain() == (True, None)


def test_a_server_stop_records_the_counts_of_that_process(make_app):
    """A restart ends the window with the server's stop first: the ended row carries
    what ran before it (the next process has no window and starts at zero)."""
    model = FakeModel([])
    app = make_app(allowance="internal", exec_tools=False, model=model, ask=())
    client = app.test_client()
    _open(client)
    _turn(
        app,
        client,
        model,
        "one",
        [tool_turn("file_write", {"path": "x.md", "content": SECRET_BODY}, "use-x")],
    )
    store = _store(app)
    app.extensions["lifecycle"].shutdown()
    [row] = [e.attributes for e in store.get_events(event_type="audit.yolo.ended")]
    assert (row["end_reason"], row["end_class"], row["used"]) == ("revoked_stop", "normal", 1)
    assert row["tool_calls"]["total"] == 1
    assert row["tool_calls"]["by_decision"]["auto_approved"] == 1
    assert row["turns"] == 1
    assert store.verify_chain() == (True, None)


# --- the service and the gate (fake clocks and audit, the real hooks) ---------------------


@pytest.fixture
def yolo(tmp_path):
    return _service(Recorder(), Clocks(), WindowMarker(tmp_path))


@pytest.fixture
def human():
    from stiracore.yolo import origin

    with origin.human_turn(AGENT):
        yield


class _Counting(_Approver):
    """``runtime.APPROVER`` over one service, with the #448 counting methods."""

    def record_call(self, tool_name, decision):
        return self.yolo.record_call(tool_name, decision)

    def record_failure(self, window_id):
        self.yolo.record_failure(window_id)

    def record_turn(self):
        self.yolo.record_turn()


def _gate(yolo, **kw):
    hooks, engine, asked = _base_gate(yolo, **kw)
    hooks.set_auto_approver(_Counting(yolo))
    return hooks, engine, asked


def _calls(yolo):
    return yolo.status()["window"]["tool_calls"]


def test_nothing_is_counted_without_an_open_window(yolo, human):
    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always"})
    _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    assert yolo.status()["window"] is None
    _open_service(yolo)
    assert _calls(yolo)["total"] == 0
    _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    assert _calls(yolo)["by_decision"]["always"] == 1


def test_the_last_call_of_a_window_its_cap_ends_is_in_the_ended_row(tmp_path, human):
    audit = Recorder()
    yolo = _service(audit, Clocks(), WindowMarker(tmp_path))
    assert yolo.set_caps(2, 0, 0)
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo)
    for _ in range(2):
        event = _execute(hooks, _call(hooks, "file_write"))
        assert event.cancel_tool is None
    assert yolo.status()["window"] is None  # expired_cap
    [row] = [r["attributes"] for r in audit.rows if r["event_type"] == "audit.yolo.ended"]
    assert row["end_reason"] == "expired_cap" and row["used"] == 2
    assert row["tool_calls"]["by_decision"]["auto_approved"] == 2
    assert row["tool_calls"]["total"] == 2
    assert yolo.status()["last_end"]["tool_calls"]["total"] == 2


def test_a_refused_automatic_approval_counts_denied_not_auto(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "file_write")  # approved automatically (a permit)
    yolo._breaker = lambda: "warning"
    yolo.status()  # a look samples the breaker: paused, the permit is stale
    _execute(hooks, event)
    assert event.cancel_tool and "no longer valid" in event.cancel_tool
    window = yolo.status()["window"]
    assert window["state"] == "paused" and window["used"] == 0
    assert window["tool_calls"]["by_decision"] == {
        "auto_approved": 0,
        "always": 0,
        "manual": 0,
        "denied": 1,
        "allowed_again": 0,
    }


def test_a_window_that_ends_before_the_boundary_counts_nothing(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "file_write")
    yolo.fence("stop")  # the window ends between the approval and the execution boundary
    _execute(hooks, event)
    assert event.cancel_tool
    assert yolo.status()["last_end"]["tool_calls"]["total"] == 0


def test_a_paused_window_counts_its_calls_as_the_person_decides(yolo, human):
    _open_service(yolo)
    hooks, _engine, asked = _gate(yolo)
    yolo._breaker = lambda: "warning"
    event = _execute(hooks, _call(hooks, "file_write"))  # paused: the card (rejected)
    assert asked and event.cancel_tool
    assert yolo.status()["window"]["state"] == "paused"
    calls = _calls(yolo)
    assert calls["by_decision"] == {
        "auto_approved": 0,
        "always": 0,
        "manual": 0,
        "denied": 1,
        "allowed_again": 0,
    }


def test_a_persons_approval_and_respond_count_manual(yolo, human):
    _open_service(yolo)
    hooks, engine, _asked = _gate(yolo)
    answers = iter(
        [
            HITLDecision(HITLAction.RESPOND, response="answered by a person"),
            HITLDecision(HITLAction.REJECT, reason="no"),
        ]
    )
    engine.on_hitl_request = lambda request: next(answers)
    for name in ("web_search", "web_search"):  # a card in a window of tier internal
        _execute(hooks, _call(hooks, name, {"query": SECRET_QUERY}))
    calls = _calls(yolo)
    assert calls["by_decision"] == {
        "auto_approved": 0,
        "always": 0,
        "manual": 1,
        "denied": 1,
        "allowed_again": 0,
    }
    assert calls["by_category"]["external"] == 2 and calls["by_tool"] == {"web_search": 2}
    assert SECRET_QUERY not in json.dumps(calls)


def test_an_error_result_of_a_call_that_ran_adds_failed(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always", "file_write": "never"})
    ran = _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    ran_in = ran.invocation_state[YOLO_RAN_IN_KEY][ran.tool_use["toolUseId"]]
    assert ran_in == yolo.machine.window.window_id
    after = SimpleNamespace(
        tool_use=ran.tool_use,
        invocation_state=ran.invocation_state,
        result={"status": "error", "content": [{"text": "boom"}]},
        exception=None,
    )
    hooks.binding_verifier.after_tool(after)
    refused = _execute(hooks, _call(hooks, "file_write", {"path": "a.md"}))
    assert refused.cancel_tool.startswith("Tool policy is Never")
    hooks.binding_verifier.after_tool(
        SimpleNamespace(
            tool_use=refused.tool_use,
            invocation_state=refused.invocation_state,
            result={"status": "error"},
            exception=None,
        )
    )
    calls = _calls(yolo)
    assert calls["failed"] == 1 and calls["total"] == 2  # a refused call is no failure


def test_a_turn_is_counted_only_while_a_window_is_open(yolo):
    hooks, _engine, _asked = _gate(yolo)
    hooks.before_invocation(SimpleNamespace())
    _open_service(yolo)
    hooks.before_invocation(SimpleNamespace())
    hooks.before_invocation(SimpleNamespace())
    assert yolo.status()["window"]["turns"] == 2


def test_without_the_approver_the_gate_keeps_no_new_state():
    engine = StiraEngine(config=StiraConfig())
    engine.on_hitl_request = lambda request: HITLDecision(HITLAction.APPROVE)
    hooks = CheckpointHooks(engine, policy_check=lambda name: ("ask", 1))
    event = _execute(hooks, _call(hooks, "file_write", {"path": "a.md", "content": "x"}))
    hooks.before_invocation(SimpleNamespace())
    for key in (YOLO_DECIDED_KEY, YOLO_COUNTED_KEY, YOLO_RAN_IN_KEY):
        assert key not in event.invocation_state


def test_an_approver_that_fails_to_count_never_changes_the_call(yolo, human):
    _open_service(yolo)

    class Failing(_Counting):
        def record_call(self, *args):
            raise RuntimeError("count failed")

    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always"})
    hooks.set_auto_approver(Failing(yolo))
    event = _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    assert event.cancel_tool is None


# --- the row contract ----------------------------------------------------------------------


def _ended_fields(**extra):
    import secrets

    from stiracore.yolo.audit_schema import SCHEMA

    operation = secrets.token_hex(16)
    return {
        "schema": SCHEMA,
        "operation_id": operation,
        "phase": "transition",
        "event_id": f"op:{operation}:transition",
        "actor_kind": "system",
        "actor": "system:yolo",
        "surface": "system",
        "incarnation_ref": "b" * 64,
        "key_id": "proc-abc",
        "delta_class": "narrowing",
        "window_id": "c" * 32,
        "generation_from": 1,
        "generation_to": 2,
        "authority_epoch": 0,
        "end_reason": "revoked_stop",
        "end_class": "normal",
        "used": 0,
        **extra,
    }


def _counted(**changes):
    stats = tool_stats.empty()
    tool_stats.add(stats, "file_read", "always")
    tool_stats.add(stats, "mcp_server_tool", "denied")
    return {**tool_stats.snapshot(stats), **changes}


def test_the_ended_row_takes_the_counts_and_stays_valid_without_them():
    assert "tool_calls" not in build("audit.yolo.ended", **_ended_fields())["attributes"]
    row = build("audit.yolo.ended", **_ended_fields(tool_calls=_counted(), turns=3))
    assert row["attributes"]["tool_calls"]["by_tool"] == {"file_read": 1, "other": 1}
    assert row["attributes"]["turns"] == 3


@pytest.mark.parametrize(
    "bad",
    [
        _counted(by_tool={"file_read": 1, "rm -rf /tmp/x": 1}),  # free text as a key
        _counted(by_tool={"file_read": 1}),  # does not add up
        _counted(total=3),
        _counted(failed=5),
        _counted(by_decision={"auto_approved": 0, "always": 1, "manual": 0, "denied": 1, "x": 0}),
        _counted(args="secret"),
        {**_counted(), "total": True},
        "seven",
    ],
)
def test_a_bad_count_is_stored_as_invalid_never_as_given(bad):
    row = build("audit.yolo.ended", **_ended_fields(tool_calls=bad, turns=-1))
    assert row["attributes"]["tool_calls"] == "invalid"
    assert row["attributes"]["turns"] == "invalid"


def test_an_unknown_field_on_the_ended_row_is_still_refused():
    with pytest.raises(AuditContractError):
        build("audit.yolo.ended", **_ended_fields(tool_arguments={"path": "x"}))


# --- review round: counting is isolated; a result after the end still reaches last_end -------


def _cap_window(tmp_path, cap=2):
    audit = Recorder()
    yolo = _service(audit, Clocks(), WindowMarker(tmp_path))
    assert yolo.set_caps(cap, 0, 0)
    _open_service(yolo)
    return audit, yolo


def _ended(audit):
    return [r["attributes"] for r in audit.rows if r["event_type"] == "audit.yolo.ended"]


def _failing(*_args, **_kwargs):
    raise RuntimeError("count failed")


def test_a_count_that_fails_never_changes_the_last_automatic_approval_of_a_cap(
    tmp_path, human, monkeypatch
):
    audit, yolo = _cap_window(tmp_path)
    hooks, _engine, _asked = _gate(yolo)
    monkeypatch.setattr(tool_stats, "add", _failing)
    for _ in range(2):
        event = _execute(hooks, _call(hooks, "file_write"))
        assert event.cancel_tool is None  # approved and run, as without the count
    status = yolo.status()
    assert status["window"] is None and status["last_end"]["reason"] == "expired_cap"
    [row] = _ended(audit)
    assert row["end_reason"] == "expired_cap" and row["used"] == 2
    consumed = [r for r in audit.rows if r["event_type"] == "audit.yolo.consumed"]
    assert len(consumed) == 2


def test_a_counter_that_cannot_start_never_changes_the_opening_or_the_cap_end(
    tmp_path, human, monkeypatch
):
    # tool_stats.empty() runs while the window is issued: observation only, so a failure
    # there must neither stop the window from opening nor change its approvals and end.
    monkeypatch.setattr(tool_stats, "empty", _failing)
    audit, yolo = _cap_window(tmp_path)
    assert yolo.status()["window"] is not None  # the window opened
    hooks, _engine, _asked = _gate(yolo)
    for _ in range(2):
        event = _execute(hooks, _call(hooks, "file_write"))
        assert event.cancel_tool is None
    status = yolo.status()
    assert status["window"] is None and status["last_end"]["reason"] == "expired_cap"
    [row] = _ended(audit)
    assert row["end_reason"] == "expired_cap" and row["used"] == 2
    assert row.get("tool_calls") is None  # no counts, but the end row is written


def test_a_snapshot_that_fails_never_changes_the_end_of_a_cap(tmp_path, human, monkeypatch):
    audit, yolo = _cap_window(tmp_path)
    hooks, _engine, _asked = _gate(yolo)
    _execute(hooks, _call(hooks, "file_write"))
    monkeypatch.setattr(tool_stats, "snapshot", _failing)
    assert yolo.status()["window"]["used"] == 1  # a look still answers (without the line)
    event = _execute(hooks, _call(hooks, "file_write"))
    assert event.cancel_tool is None
    status = yolo.status()
    assert status["window"] is None and status["last_end"]["reason"] == "expired_cap"
    [row] = _ended(audit)  # written, without the optional counts
    assert row["end_reason"] == "expired_cap" and row["used"] == 2
    assert row.get("tool_calls") is None


def _after(hooks, ran, status="error"):
    hooks.binding_verifier.after_tool(
        SimpleNamespace(
            tool_use=ran.tool_use,
            invocation_state=ran.invocation_state,
            result={"status": status, "content": [{"text": "boom"}]},
            exception=None,
        )
    )


def test_an_error_of_the_call_its_cap_ended_the_window_on_reaches_last_end(tmp_path, human):
    audit, yolo = _cap_window(tmp_path)
    hooks, _engine, _asked = _gate(yolo)
    _after(hooks, _execute(hooks, _call(hooks, "file_write")), status="success")
    last = _execute(hooks, _call(hooks, "file_write"))  # consume: used == cap, the end
    assert yolo.status()["window"] is None
    [row] = _ended(audit)
    assert row["tool_calls"]["failed"] == 0  # written before the call ran
    _after(hooks, last)  # the last call returns an error after the end
    calls = yolo.status()["last_end"]["tool_calls"]
    assert calls["total"] == 2 and calls["failed"] == 1
    assert len(_ended(audit)) == 1 and _ended(audit)[0]["tool_calls"]["failed"] == 0
    _after(hooks, last)  # the same call once more: popped, nothing more
    assert yolo.status()["last_end"]["tool_calls"]["failed"] == 1


def test_an_error_of_a_call_running_when_a_person_stops_reaches_last_end(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always"})
    running = _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    yolo.fence("stop")
    _after(hooks, running)
    calls = yolo.status()["last_end"]["tool_calls"]
    assert calls["total"] == 1 and calls["failed"] == 1


def test_an_error_after_the_next_window_opened_changes_neither_window(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always"})
    running = _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    yolo.fence("stop")
    _open_service(yolo)  # a new window: its counter is not the old one's
    _after(hooks, running)
    assert _calls(yolo)["failed"] == 0 and _calls(yolo)["total"] == 0
    assert yolo.status()["last_end"]["tool_calls"]["failed"] == 1


def test_an_error_that_arrives_while_the_end_row_is_written_reaches_last_end(yolo, human):
    _open_service(yolo)
    hooks, _engine, _asked = _gate(yolo, rules={"file_read": "always"})
    running = _execute(hooks, _call(hooks, "file_read", {"path": "a"}))
    write = yolo._write

    def write_and_fail(row):
        if row[0] == "audit.yolo.ended":
            _after(hooks, running)  # the running call returns its error meanwhile
        return write(row)

    yolo._write = write_and_fail
    yolo.fence("stop")
    calls = yolo.status()["last_end"]["tool_calls"]
    assert calls["total"] == 1 and calls["failed"] == 1
