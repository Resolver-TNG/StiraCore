"""POST /api/chat/session/new under concurrency and at its edges (#350 phase 3, review round).

Real chat server (``create_app``) on the offline model of tests/test_new_session.py. Ordering is
forced with events/barriers, never with sleeps that decide the result.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from stiracore.agent import nxp_agent
from stiracore.agent.hitl_resume import ResumeOutcome
from stiracore.observability import chat_api, security
from tests.approval_helpers import approving_cards, set_rule
from tests.test_new_session import NEW, _agent, _history, _messages_at_model, make_stira

WAIT = 5


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _rows(stira, kind=None):
    rows = sqlite3.connect(stira["data"] / "events.db").execute(
        "SELECT event_type, attributes_json FROM events ORDER BY seq"
    )
    return [(t, json.loads(a)) for t, a in rows if kind is None or t == kind]


def _restored_pending(app, request_id="req-1"):
    store = app.extensions["approval_store"]
    store.submit(
        request_id=request_id,
        agent_id="stira",
        tool_name="file_write",
        tool_input={"path": "a.txt", "content": "x"},
        expires_at=time.time() + 600,
        checkpoint_id="cp-1",
    )
    return store.get(request_id).args_hash


def _in_thread(fn):
    out = {}

    def run():
        out["value"] = fn()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


# --- Blocker 1: a decision / resume reservation and the reset never interleave -------------


def test_a_decision_in_progress_holds_the_reset_off_and_its_resume_then_refuses_it(
    stira, monkeypatch
):
    """The approval goes approved in the store, then the reset tries before the resume is
    scheduled (the gap the review found): the reset waits for the decision, and then the
    scheduled resume refuses it. Nothing is reset while the old conversation can come back."""
    from dashboard import stira_server

    app, client = stira["app"], stira["client"]
    host = app.extensions["new_session"]
    stira["chat"]("hello")
    digest = _restored_pending(app)
    store = app.extensions["approval_store"]

    in_gap, leave_gap = threading.Event(), threading.Event()
    real_decide = store.decide

    def decide_then_pause(*args, **kwargs):
        outcome = real_decide(*args, **kwargs)  # approved in the store ...
        in_gap.set()
        assert leave_gap.wait(WAIT)  # ... the resume is not scheduled yet
        return outcome

    monkeypatch.setattr(store, "decide", decide_then_pause)
    resuming, finish_resume = threading.Event(), threading.Event()

    def slow_resume(agent, approval_store, request_id, on_replay=None):
        resuming.set()
        assert finish_resume.wait(WAIT)
        return ResumeOutcome(False, "test: not replayed")

    monkeypatch.setattr(stira_server, "resume_approved_request", slow_resume)
    monkeypatch.setattr(chat_api, "NEW_SESSION_LOCK_TIMEOUT_SEC", 0.3)

    decision, decided = _in_thread(
        lambda: client.post(
            "/api/hitl/req-1/decision", json={"action": "approve", "args_hash": digest}
        )
    )
    assert in_gap.wait(WAIT)
    during = stira["new"]("g1")
    assert (during.status_code, during.get_json()["code"]) == (409, "decision_in_progress")
    assert len(_history(client)["turns"]) == 1

    leave_gap.set()
    decision.join(WAIT)
    assert decided["value"].status_code == 202
    assert resuming.wait(WAIT)  # the resume holds the turn lock now
    after = stira["new"]("g1")
    assert after.status_code == 409
    assert after.get_json()["code"] in {"turn_running", "resume_pending"}
    assert len(_history(client)["turns"]) == 1
    # every refused attempt left its reset row closed by a refused row, and no "started"
    # (turn_running is refused before any row: the turn lock is taken first)
    expected = ["decision_in_progress"] + (
        ["resume_pending"] if after.get_json()["code"] == "resume_pending" else []
    )
    kinds = [kind for kind, _ in _rows(stira)]
    assert kinds == ["session.reset", "session.reset_refused"] * len(expected)
    assert [a["refused"] for k, a in _rows(stira) if k == "session.reset_refused"] == expected

    finish_resume.set()
    deadline = time.monotonic() + WAIT
    while host.blocker() is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host.blocker() is None
    assert stira["new"]("g1").status_code == 200


def test_a_resume_reservation_waits_for_a_reset_in_progress(stira):
    """The other order: the reset is between its checks and its end; a reservation waits
    (same lock) and lands after it, so the next reset sees it (409 resume_pending)."""
    host = stira["app"].extensions["new_session"]
    stira["chat"]("hello")
    checked, go_on = threading.Event(), threading.Event()
    real_blocker = host.blocker

    def blocker_then_pause():
        result = real_blocker()
        checked.set()
        assert go_on.wait(WAIT)
        return result

    host.blocker = blocker_then_pause
    reset, reset_out = _in_thread(lambda: stira["new"]("w1"))
    assert checked.wait(WAIT)
    reserve, _ = _in_thread(host.resume_scheduled)
    reserve.join(0.3)
    assert reserve.is_alive(), "the reservation must wait for the reset"
    go_on.set()
    reset.join(WAIT)
    reserve.join(WAIT)
    host.blocker = real_blocker
    assert reset_out["value"].status_code == 200
    assert not reserve.is_alive()
    refused = stira["new"]("w2")
    assert (refused.status_code, refused.get_json()["code"]) == (409, "resume_pending")
    host.resume_finished()
    assert stira["new"]("w2").status_code == 200


def test_the_broker_holds_the_shared_lock_around_every_decision(stira):
    app = stira["app"]
    broker, host = app.extensions["hitl_broker"], app.extensions["new_session"]
    held = []
    real = broker._decide

    def spy(*args, **kwargs):
        held.append(host.lock._is_owned())
        return real(*args, **kwargs)

    broker._decide = spy
    broker.decide("nope", chat_api.HITLDecision(chat_api.HITLAction.REJECT))
    assert held == [True]


# --- Major 3: the same key, keys, bodies, origins, locks -------------------------------------


def test_two_requests_with_one_key_at_the_same_time_clear_once(stira):
    host, client = stira["app"].extensions["new_session"], stira["client"]
    stira["chat"]("hello")
    inside, release = threading.Event(), threading.Event()
    real_reset = host.reset

    def slow_reset():
        inside.set()
        assert release.wait(WAIT)
        return real_reset()

    host.reset = slow_reset
    barrier = threading.Barrier(2)

    def send():
        barrier.wait(WAIT)
        return stira["new"]("same")

    first, a = _in_thread(send)
    second, b = _in_thread(send)
    assert inside.wait(WAIT)
    release.set()
    first.join(WAIT)
    second.join(WAIT)
    host.reset = real_reset
    statuses = sorted(r["value"].status_code for r in (a, b))
    assert statuses == [200, 409]
    loser = next(r["value"] for r in (a, b) if r["value"].status_code == 409)
    assert loser.get_json()["code"] == "duplicate_request"
    assert len(_rows(stira, "session.reset")) == 1
    assert len(_rows(stira, "session.started")) == 1
    assert _history(client)["turns"] == []


def test_a_completed_key_stays_a_duplicate_after_the_generic_cache_time(stira, monkeypatch):
    stira["chat"]("hello")
    assert stira["new"]("old").status_code == 200
    stira["chat"]("after")
    # The generic Idempotency-Key cache (300 s) is not what protects this route.
    monkeypatch.setattr(security, "IDEMPOTENCY_TTL", 0.0)
    again = stira["new"]("old")
    assert (again.status_code, again.get_json()["code"]) == (409, "duplicate_request")
    assert len(_history(stira["client"])["turns"]) == 1


def test_the_oldest_keys_are_forgotten_beyond_the_bound(stira, monkeypatch):
    """Documented limit: after NEW_SESSION_KEYS_KEPT later resets a key runs again."""
    monkeypatch.setattr(chat_api, "NEW_SESSION_KEYS_KEPT", 2)
    for key in ("a", "b", "c"):
        assert stira["new"](key).status_code == 200
    assert stira["new"]("c").status_code == 409
    assert stira["new"]("a").status_code == 200


@pytest.mark.parametrize("key", ["x" * 129, "has space", "tab\tkey"])
def test_the_key_is_validated(stira, key):
    stira["chat"]("hello")
    response = stira["client"].post(NEW, headers={"Idempotency-Key": key})
    assert (response.status_code, response.get_json()["code"]) == (400, "invalid_idempotency_key")
    assert len(_history(stira["client"])["turns"]) == 1


@pytest.mark.parametrize(
    ("size", "status"), [(70 * 1024, 413), (1024, 400)], ids=["over-64KiB", "1KiB-object"]
)
def test_bodies_too_large_or_with_content_are_refused(stira, size, status):
    stira["chat"]("hello")
    body = json.dumps({"pad": "x" * size})
    response = stira["client"].post(
        NEW, data=body, content_type="application/json", headers={"Idempotency-Key": "big"}
    )
    assert response.status_code == status
    assert len(_history(stira["client"])["turns"]) == 1


def test_the_turn_lock_is_the_one_every_turn_holds(stira):
    app = stira["app"]
    lock = app.extensions["turn_lock"]
    assert app.blueprints["webhooks"].turn_lock is lock
    stira["chat"]("hello")
    with lock:  # a webhook turn or a resume holds exactly this lock
        refused = stira["new"]("l1")
    assert (refused.status_code, refused.get_json()["code"]) == (409, "turn_running")
    assert stira["new"]("l1").status_code == 200


def test_the_lock_is_held_until_the_stream_ends(stira):
    model = stira["model"]
    stira["chat"]("hello")
    in_model, release = threading.Event(), threading.Event()

    def block():
        in_model.set()
        assert release.wait(WAIT)

    model.during = block
    stream, _ = _in_thread(lambda: stira["chat"]("streaming"))
    assert in_model.wait(WAIT)
    during = stira["new"]("s1")
    model.during = None
    release.set()
    stream.join(WAIT)
    assert (during.status_code, during.get_json()["code"]) == (409, "turn_running")
    assert len(_history(stira["client"])["turns"]) == 2
    assert stira["new"]("s1").status_code == 200


# --- Major 2: the history is one snapshot -----------------------------------------------------


def test_history_and_session_come_from_one_lock_section():
    import inspect

    source = inspect.getsource(chat_api.create_chat_blueprint)
    view = source[source.index("def history():") : source.index("if new_session is not None:")]
    block = view[view.index("with state_lock:") :]
    first = block[: block.index("if memory_store is not None:")]
    assert "recent = list(turns)" in first and "current = dict(session)" in first
    assert view.count("with state_lock:") == 1


def test_history_never_pairs_old_turns_with_a_new_session(stira):
    """Writer: turn, reset, turn, reset...; reader: every snapshot's turns belong to its
    session (messages carry the generation they were sent in)."""
    client = stira["client"]
    generation_of = {_history(client)["session"]["id"]: 0}
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            snap = client.get("/api/chat/history").get_json()
            gen = generation_of.get(snap["session"]["id"])
            if gen is None:
                continue  # the writer has not recorded this session id yet
            for turn in snap["turns"]:
                if turn["message"] != f"gen-{gen}":
                    bad.append((gen, turn["message"]))
            time.sleep(0.002)  # let the writer run (pacing only; the check decides)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for gen in range(8):
            stira["chat"](f"gen-{gen}")
            response = stira["new"](f"gen-key-{gen}")
            assert response.status_code == 200
            generation_of[response.get_json()["session"]["id"]] = gen + 1
    finally:
        stop.set()
        thread.join(WAIT)
    assert bad == []


# --- the agent's own http_request cannot reset its context (real tool, real HTTP) ------------


def test_the_agents_own_http_request_is_refused_during_its_turn(stira, monkeypatch):
    pytest.importorskip("strands_tools.http_request")
    from werkzeug.serving import make_server

    app, client, model = stira["app"], stira["client"], stira["model"]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        # The worst case: the POST really goes out. Since #547 a modifying http_request runs
        # only after a person approved that call (BYPASS_TOOL_CONSENT is ignored), so the
        # rule is Ask and the card is approved; the turn lock must still refuse the reset.
        saved = set_rule(client, "http_request", "ask")
        assert saved.status_code == 200, saved.get_json()
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
        stira["chat"]("hello")
        token = app.config["STIRA_CSRF_TOKEN"]
        model.script = [
            [
                {
                    "toolUse": {
                        "name": "http_request",
                        "toolUseId": "self-reset",
                        "input": {
                            "method": "POST",
                            "url": f"http://127.0.0.1:{port}{NEW}",
                            "headers": {
                                "X-CSRF-Token": token,
                                "Idempotency-Key": "agent-self",
                                "Content-Type": "application/json",
                            },
                            "body": "{}",
                        },
                    }
                }
            ],
            [{"text": "tried"}],
        ]
        with approving_cards(client, token) as approved:
            body = stira["chat"]("please reset yourself")
        assert [card["tool_name"] for card in approved] == ["http_request"]
        assert len(_history(client)["turns"]) == 2
        assert _rows(stira) == []  # refused at the turn lock, before any row
        turn_id = json.loads(
            next(b for b in body.split("\n\n") if b.startswith("event: turn\n")).split("data: ", 1)[
                1
            ]
        )["turn_id"]
        events = client.get(f"/api/turns/{turn_id}/events").get_json()["events"]
        outputs = [
            e["data"].get("output_preview") or ""
            for e in events
            if e["type"] == "tool_call.updated" and e["data"].get("name") == "http_request"
        ]
        assert any("409" in o and "turn_running" in o for o in outputs), outputs
    finally:
        server.shutdown()
        serving.join(WAIT)


# --- Minor: the static prefix survives /new (the cache is not broken by it) -------------------


def test_new_conversation_keeps_the_static_prefix(tmp_path):
    import importlib.util
    from pathlib import Path

    from tests.test_prompt_cache import _claude, _wired

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "stira_server_new_prefix", root / "dashboard" / "stira_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = _claude()
    agent, _store, _ = _wired(tmp_path, module, model=model, enabled=True)
    agent("one")
    agent("two")
    before = agent.last_prompt_cache
    assert agent.start_new_conversation() == 4
    agent("three")
    after = agent.last_prompt_cache
    assert after["active"] is True
    assert after["tools_hash"] == before["tools_hash"]
    assert after["system_hash"] == before["system_hash"]
    raw = model.requests.captured
    assert raw[-1]["system"] == raw[-2]["system"]  # static system blocks + cache point
    assert raw[-1]["toolConfig"] == raw[-2]["toolConfig"]
    assert len(raw[-1]["messages"]) == 1
    assert isinstance(agent, nxp_agent.NXPAgent)
    _ = _agent, _messages_at_model  # shared helpers stay importable
