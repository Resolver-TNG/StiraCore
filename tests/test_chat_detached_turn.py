"""#452: a chat turn runs on its own worker, not on the page's SSE stream.

Closing the stream (a closed tab, a reload, another page, a cut connection) only detaches
that reader: the turn goes on, its events stay in turns.db (``after_seq`` picks them up
again) and its reply is kept in the chat history. Only an explicit stop
(``POST /api/chat/cancel``) is ``stop: "cancelled"``; the server's stop and a restart are
``stop: "interrupted"`` with a reason. One turn at a time (the turn lock), released on
every path. The human-turn mark of ADR 0012 §3 is set on the worker for its turn only.

Blueprint tests run a scripted runner; the ``stira`` tests run the real app on the
offline model of tests/test_new_session.py; the process test runs the real entry point
against tests/fake_bedrock_endpoint.py with a real socket that is closed mid-turn.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

from stiracore.core import HITLAction
from stiracore.observability import chat_api
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.observability.turns_view import page_event
from stiracore.yolo import origin
from tests.provider_fakes import text_turn, tool_turn
from tests.test_new_session import make_stira

WAIT = 10.0


def _until(predicate, timeout=WAIT, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


def _frames(chunks):
    """(event, data) of every SSE frame in ``chunks`` (comments skipped)."""
    out = []
    for block in b"".join(chunks).decode().split("\n\n"):
        event, data = "message", None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if data is not None:
            out.append((event, data))
    return out


class Gate:
    """A runner that yields ``first``, waits for ``release``, then yields ``rest``."""

    def __init__(self, first=("first ",), rest=("second",)):
        self.first, self.rest = list(first), list(rest)
        self.reached = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = False

    def __call__(self, _message):
        self.calls += 1
        try:
            yield from self.first
            self.reached.set()
            assert self.release.wait(WAIT)
            yield from self.rest
        except GeneratorExit:
            self.closed = True
            raise


def _app(runner, agent=None, recorder=None, lock=None, broker=None):
    app = Flask(__name__)
    bp = create_chat_blueprint(
        agent if agent is not None else SimpleNamespace(),
        runner=runner,
        turn_recorder=recorder,
        turn_lock=lock,
        hitl_broker=broker,
    )
    app.register_blueprint(bp)
    return app, bp


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


def _completed(recorder):
    turns = recorder.store.recent_turns(1)
    return turns[0] if turns and turns[0]["completed"] else None


def _open_stream(client, until=b"first"):
    """POST /api/chat, read until ``until`` is seen; -> (response, chunks read)."""
    response = client.post("/api/chat", json={"message": "x"}, buffered=False)
    iterator = iter(response.response)
    chunks = []
    for chunk in iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
        if until in chunks[-1]:
            break
    return response, iterator, chunks


# --- blueprint: the stream is a reader, not the turn ---------------------------------------


def test_closing_the_stream_never_stops_the_turn(recorder):
    runner = Gate()
    app, _ = _app(runner, recorder=recorder)
    client = app.test_client()
    response, _, _ = _open_stream(client)
    response.close()  # the page went away mid-turn
    assert not runner.closed
    runner.release.set()
    done = _until(lambda: _completed(recorder))
    assert (done["stop"], done["error"], done["usage_reason"]) == (
        "completed",
        False,
        "E_USAGE_NOT_REPORTED",
    )
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["response"] == "first second"  # the reply after the cut is kept
    assert "stop" not in history[-1]


def test_reconnecting_with_after_seq_gets_the_rest_without_a_gap(recorder):
    runner = Gate()
    app, _ = _app(runner, recorder=recorder)
    client = app.test_client()
    response, _, chunks = _open_stream(client)
    seen = [data for event, data in _frames(chunks) if event == "ui_event"]
    assert seen and seen[0]["type"] == "turn.started"
    turn_id, last_seq = seen[0]["turn_id"], seen[-1]["seq"]
    response.close()
    runner.release.set()
    _until(lambda: _completed(recorder))
    rest = client.get(f"/api/turns/{turn_id}/events?after_seq={last_seq}").get_json()["events"]
    seqs = [event["seq"] for event in seen] + [event["seq"] for event in rest]
    assert seqs == list(range(1, len(seqs) + 1))
    assert rest[-1]["type"] == "turn.completed" and rest[-1]["data"]["stop"] == "completed"


def test_only_an_explicit_stop_is_cancelled(recorder):
    runner = Gate()

    def more(_message):
        yield from runner(_message)
        yield "never reached"

    agent = SimpleNamespace(cancel=Mock(return_value=True))
    app, _ = _app(more, agent=agent, recorder=recorder)
    client = app.test_client()
    assert client.post("/api/chat/cancel").get_json()["code"] == "no_turn"
    _response, iterator, chunks = _open_stream(client)
    turn_id = next(d for e, d in _frames(chunks) if e == "ui_event")["turn_id"]
    other = client.post("/api/chat/cancel", json={"turn_id": "another"})
    assert (other.status_code, other.get_json()["code"]) == (409, "turn_mismatch")
    stop = client.post("/api/chat/cancel", json={"turn_id": turn_id})
    assert stop.status_code == 202 and stop.get_json()["turn_id"] == turn_id
    _until(lambda: agent.cancel.called)  # the SDK is asked to stop at its next safe point
    runner.release.set()
    frames = _frames(chunks + list(iterator))
    assert [d["text"] for e, d in frames if e == "delta"] == ["first ", "second"]
    assert frames[-1][0] == "turn" and frames[-1][1]["stop"] == "cancelled"
    done = _completed(recorder)
    assert (done["stop"], done["error"]) == ("cancelled", True)
    history = client.get("/api/chat/history").get_json()["turns"]
    assert (history[-1]["response"], history[-1]["stop"]) == ("first second", "cancelled")
    assert client.post("/api/chat/cancel").get_json()["code"] == "no_turn"


def test_the_stop_rejects_only_the_live_cards_of_its_turn(recorder):
    broker = HITLApprovalBroker(turn_id_provider=recorder.active.current)
    broker.decide = Mock(wraps=broker.decide)
    other = {"id": "other", "turn_id": "someone-else"}
    restored = {"id": "restored", "restored": True}
    runner = Gate()
    app, _ = _app(runner, recorder=recorder, broker=broker)
    client = app.test_client()
    response, _, chunks = _open_stream(client)
    turn_id = next(d for e, d in _frames(chunks) if e == "ui_event")["turn_id"]
    mine = {"id": "mine", "turn_id": turn_id}
    broker.pending = lambda: [mine, other, {**restored, "turn_id": turn_id}]
    assert client.post("/api/chat/cancel").get_json()["rejected"] == 0  # not_found: counted 0
    # (the stop repeats the rejection until the turn ends: this mock lists "mine" each time)
    calls = broker.decide.call_args_list
    assert calls and all(c.args[0] == "mine" for c in calls)
    assert all(c.args[1].action == HITLAction.REJECT for c in calls)
    runner.release.set()
    response.close()
    _until(lambda: _completed(recorder))


def test_a_repeated_cancel_never_lands_after_its_turn_ended(recorder):
    """M1: a cancel() of the stop still running keeps its turn from ending (and the lock
    from being released), so it can never reach the next turn's agent."""
    runner = Gate()
    in_cancel, hold = threading.Event(), threading.Event()

    def slow_cancel():
        in_cancel.set()
        assert hold.wait(WAIT)
        return True

    agent = SimpleNamespace(cancel=Mock(side_effect=slow_cancel))
    app, _ = _app(runner, agent=agent, recorder=recorder)
    client = app.test_client()
    _response, iterator, chunks = _open_stream(client)
    assert client.post("/api/chat/cancel").status_code == 202
    assert in_cancel.wait(WAIT)
    runner.release.set()  # the turn's work is over while cancel() still runs
    time.sleep(0.3)
    assert _completed(recorder)["stop"] == "cancelled"  # recorded, but not yet ended:
    busy = _frames([client.post("/api/chat", json={"message": "y"}).get_data()])
    assert busy == [("error", {"error": chat_api.BUSY_MESSAGE, "code": "turn_running"})]
    calls = agent.cancel.call_count
    hold.set()
    frames = _frames(chunks + list(iterator))
    assert frames[-1][0] == "turn" and frames[-1][1]["stop"] == "cancelled"
    agent.cancel.side_effect = None
    body = client.post("/api/chat", json={"message": "z"}).get_data()
    assert _frames([body])[-1][0] == "turn"
    time.sleep(3 * chat_api.CANCEL_REPEAT_SEC)
    assert agent.cancel.call_count == calls  # nothing after its turn ended
    assert _completed(recorder)["stop"] == "completed"


def test_a_turn_not_run_by_the_chat_is_not_stoppable(recorder):
    """m1: a turn the chat route does not run (resumed after an approval) is listed as
    running but not ``stoppable``: the chat page shows no ■ Stop that could only fail."""
    app, _ = _app(Gate(), recorder=recorder)
    recorder.active.set("resumed")
    recorder.emit("turn.started", {"prompt_chars": 0, "image_count": 0})
    (turn,) = app.test_client().get("/api/turns?limit=1").get_json()["turns"]
    assert (turn["turn_id"], turn["completed"], turn["stoppable"]) == ("resumed", False, False)
    recorder.active.clear()


def test_a_second_send_while_a_turn_runs_is_refused_clearly(recorder):
    runner = Gate()
    lock = threading.Lock()
    app, _ = _app(runner, recorder=recorder, lock=lock)
    client = app.test_client()
    response, _, _ = _open_stream(client)
    response.close()
    busy = _frames([client.post("/api/chat", json={"message": "y"}).get_data()])
    assert busy == [("error", {"error": chat_api.BUSY_MESSAGE, "code": "turn_running"})]
    assert runner.calls == 1
    runner.release.set()
    _until(lambda: not lock.locked())
    body = client.post("/api/chat", json={"message": "z"}).get_data()
    assert any(e == "turn" for e, _ in _frames([body]))


def test_a_failing_turn_releases_the_lock_and_the_slot(recorder):
    lock = threading.Lock()

    def boom(_message):
        raise RuntimeError("model exploded")
        yield  # pragma: no cover

    app, _ = _app(boom, recorder=recorder, lock=lock)
    client = app.test_client()
    frames = _frames([client.post("/api/chat", json={"message": "x"}).get_data()])
    assert frames[-1][0] == "error"
    assert not lock.locked()  # the stream ends only after the lock is free
    assert _completed(recorder)["stop"] == "error"
    assert client.post("/api/chat/cancel").get_json()["code"] == "no_turn"


def test_a_worker_that_cannot_start_releases_the_lock(recorder, monkeypatch):
    lock = threading.Lock()
    app, _ = _app(lambda _m: "never", recorder=recorder, lock=lock)
    real = threading.Thread

    class NoStart(real):
        def start(self):
            if self.name.startswith("chat-turn-"):
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(chat_api.threading, "Thread", NoStart)
    response = app.test_client().post("/api/chat", json={"message": "x"})
    assert response.status_code == 500
    assert not lock.locked()
    monkeypatch.setattr(chat_api.threading, "Thread", real)
    assert app.test_client().post("/api/chat/cancel").get_json()["code"] == "no_turn"


def test_a_reader_that_stops_reading_is_dropped_at_the_limit(recorder, monkeypatch):
    monkeypatch.setattr(chat_api, "STREAM_FRAME_LIMIT", 5)
    words = [f"w{i} " for i in range(40)]
    app, _ = _app(lambda _m: iter(words), recorder=recorder)
    client = app.test_client()
    response = client.post("/api/chat", json={"message": "x"}, buffered=False)
    iterator = iter(response.response)
    first = next(iterator)  # ": connected"; nothing more is read while the turn runs
    _until(lambda: _completed(recorder))
    frames = _frames([first, *iterator])
    assert len(frames) <= 6  # at most the limit, then why it stopped
    assert frames[-1] == (
        "error",
        {"error": chat_api.STREAM_DROPPED_MESSAGE, "code": "stream_dropped"},
    )
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["response"] == "".join(words)
    assert _completed(recorder)["stop"] == "completed"


def test_an_idle_stream_sends_a_comment(recorder, monkeypatch):
    monkeypatch.setattr(chat_api, "KEEPALIVE_SEC", 0.02)

    def slow(_message):
        time.sleep(0.3)
        yield "late"

    app, _ = _app(slow, recorder=recorder)
    body = app.test_client().post("/api/chat", json={"message": "x"}).get_data()
    assert b": keep-alive\n\n" in body
    assert [d["text"] for e, d in _frames([body]) if e == "delta"] == ["late"]


def test_the_server_stop_records_interrupted_once(recorder):
    runner = Gate()
    app, bp = _app(runner, recorder=recorder)
    client = app.test_client()
    assert bp.interrupt_turn("server_stopped") == "off"
    _response, iterator, chunks = _open_stream(client)
    assert bp.interrupt_turn("server_stopped") == "interrupted"
    done = _completed(recorder)
    assert (done["stop"], done["error"], done["usage_reason"]) == (
        "interrupted",
        True,
        "E_USAGE_INTERRUPTED",
    )
    runner.release.set()
    frames = _frames(chunks + list(iterator))
    assert ("error", {"error": chat_api.INTERRUPTED_MESSAGE, "code": "interrupted"}) in frames
    events = recorder.store.events(done["turn_id"])
    assert [e["type"] for e in events].count("turn.completed") == 1
    assert events[-1]["data"]["reason"] == "server_stopped"


def test_turns_left_open_by_a_previous_process_are_closed_as_interrupted(recorder):
    store = recorder.store
    store.append("open", "turn.started", {"prompt_chars": 1, "image_count": 0})
    store.append("done", "turn.started", {"prompt_chars": 1, "image_count": 0})
    store.append("done", "turn.completed", {"usage": {}, "stop": "completed", "error": False})
    assert store.close_unfinished("server_restarted") == ["open"]
    assert store.close_unfinished("server_restarted") == []
    turns = {t["turn_id"]: t for t in store.recent_turns(5)}
    assert (turns["open"]["stop"], turns["open"]["usage_reason"]) == (
        "interrupted",
        "E_USAGE_INTERRUPTED",
    )
    assert turns["done"]["stop"] == "completed"
    end = store.events("open")[-1]
    assert page_event(1, end)["message"] == "interrupted: server_restarted"


def test_the_human_turn_mark_lives_on_the_worker_for_its_turn_only(recorder):
    """ADR 0012 §3 across the new thread boundary: covered inside the turn on the worker;
    not on a thread the turn starts, not after the turn, never on the request thread."""
    seen = {}
    request_thread = []

    def runner(_message):
        seen["inside"] = (origin.current(), origin.covered("stira"), origin.covered("other"))
        seen["thread"] = threading.current_thread().name
        child = threading.Thread(target=lambda: seen.setdefault("child", origin.current()))
        child.start()
        child.join()
        return "ok"

    app, _ = _app(runner, agent=SimpleNamespace(agent_id="stira"), recorder=recorder)

    @app.before_request
    def remember():
        request_thread.append(threading.current_thread().name)
        seen.setdefault("request", origin.current())

    app.test_client().post("/api/chat", json={"message": "x"}).get_data()
    assert seen["inside"] == (origin.HUMAN_TURN, True, False)
    assert seen["thread"].startswith("chat-turn-") and seen["thread"] != request_thread[0]
    assert seen["child"] is None and seen["request"] is None
    assert origin.current() is None and not origin._LIVE


# --- the real app (offline model) -----------------------------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _post_chat(stira, message, key):
    return stira["client"].post(
        "/api/chat",
        json={"message": message},
        headers={"X-CSRF-Token": stira["token"], "Idempotency-Key": key},
        buffered=False,
    )


def _latest(client):
    turns = client.get("/api/turns?limit=1").get_json()["turns"]
    return turns[0] if turns and turns[0]["completed"] else None


def test_real_app_a_cut_stream_finishes_the_turn_and_keeps_the_reply(stira):
    client, model = stira["client"], stira["model"]
    inside, release = threading.Event(), threading.Event()
    marks = []

    def during():
        marks.append(origin.covered(stira["app"].extensions["new_session"]._agent.agent_id))
        inside.set()
        assert release.wait(WAIT)

    model.during = during
    response = _post_chat(stira, "a long job", "k1")
    iterator = iter(response.response)
    next(iterator)  # ": connected"
    assert inside.wait(WAIT)
    response.close()  # the tab is closed while the model works
    release.set()
    done = _until(lambda: _latest(client))
    model.during = None
    assert (done["stop"], done["error"]) == ("completed", False)
    assert done["usage"]["output_tokens"] and done["usage_reason"] is None  # usage kept
    history = client.get("/api/chat/history").get_json()["turns"]
    assert (history[-1]["message"], history[-1]["response"]) == ("a long job", "ok")
    assert marks == [True]  # the model call ran inside the worker's human-turn mark
    assert not origin._LIVE
    assert "event: turn\n" in stira["chat"]("next")  # the lock was released


def _card(client):
    pending = client.get("/api/hitl/pending").get_json()["requests"]
    return pending[0] if pending else None


def test_real_app_an_approval_waits_while_no_page_is_open(stira):
    client, model = stira["client"], stira["model"]
    target = "notes/kept.txt"
    model.script[:] = [
        tool_turn("file_write", {"path": target, "content": "hello"}),
        text_turn("written"),
    ]
    response = _post_chat(stira, "write it", "k2")
    next(iter(response.response))
    card = _until(lambda: _card(client))
    response.close()  # nobody is looking: the card stays until hitl_timeout_sec
    time.sleep(0.5)
    assert _card(client)["id"] == card["id"]
    assert _latest(client) is None
    decided = client.post(
        f"/api/hitl/{card['id']}/decision",
        json={"action": "approve", "args_hash": card["args_hash"]},
        headers={"X-CSRF-Token": stira["token"]},
    )
    assert decided.status_code == 202, decided.get_json()
    done = _until(lambda: _latest(client))
    assert done["stop"] == "completed"
    assert client.get("/api/chat/history").get_json()["turns"][-1]["response"] == "written"
    assert (stira["data"] / "workspace" / target).read_text() == "hello"


def test_real_app_an_unanswered_approval_still_expires_as_a_rejection(stira):
    app, client, model = stira["app"], stira["client"], stira["model"]
    app.extensions["new_session"]._agent.engine.config.hitl_timeout_sec = 1.0
    model.script[:] = [
        tool_turn("file_write", {"path": "notes/never.txt", "content": "x"}),
        text_turn("not written"),
    ]
    response = _post_chat(stira, "write it", "k3")
    next(iter(response.response))
    card = _until(lambda: _card(client))
    response.close()
    done = _until(lambda: _latest(client))
    assert done["stop"] == "completed"
    history = client.get("/api/hitl/history").get_json()["history"]
    assert next(h for h in history if h["id"] == card["id"])["status"] == "expired"
    assert not (stira["data"] / "workspace" / "notes" / "never.txt").exists()


def test_real_app_the_stop_button_route_cancels_the_running_turn(stira):
    client, model = stira["client"], stira["model"]
    inside, release = threading.Event(), threading.Event()

    def during():
        inside.set()
        assert release.wait(WAIT)

    model.during = during
    response = _post_chat(stira, "long", "k4")
    next(iter(response.response))
    assert inside.wait(WAIT)
    # /api/turns names the one turn ■ Stop can stop (the chat page offers it only for that)
    assert client.get("/api/turns?limit=1").get_json()["turns"][0]["stoppable"] is True
    stopped = client.post("/api/chat/cancel", headers={"X-CSRF-Token": stira["token"]})
    assert stopped.status_code == 202
    release.set()
    done = _until(lambda: _latest(client))
    model.during = None
    response.close()
    assert done["stop"] == "cancelled" and done["stoppable"] is False
    assert client.get("/api/chat/history").get_json()["turns"][-1]["stop"] == "cancelled"
    # without the CSRF token nothing is stopped (same protection as every POST)
    assert client.post("/api/chat/cancel", headers={"X-Test-No-Csrf": "1"}).status_code == 403


def test_real_app_the_servers_stop_records_the_running_turn_as_interrupted(stira):
    from dashboard import stira_server

    app, client, model = stira["app"], stira["client"], stira["model"]
    inside, release = threading.Event(), threading.Event()

    def during():
        inside.set()
        release.wait(WAIT)

    model.during = during
    response = _post_chat(stira, "long", "k5")
    next(iter(response.response))
    assert inside.wait(WAIT)
    steps = stira_server.shutdown_app(app)
    release.set()
    response.close()
    assert steps["chat_turn"] == "interrupted"
    done = client.get("/api/turns?limit=1").get_json()["turns"][0]
    assert (done["stop"], done["error"]) == ("interrupted", True)


def test_real_app_start_closes_turns_a_crashed_process_left_open(tmp_path, monkeypatch):
    data = tmp_path / "data"
    store = TurnEventStore(data / "turns.db")
    store.append("crashed", "turn.started", {"prompt_chars": 3, "image_count": 0})
    store.close()
    gen = make_stira(tmp_path, monkeypatch)
    stira = next(gen)
    try:
        turns = stira["client"].get("/api/turns?limit=5").get_json()["turns"]
        crashed = next(t for t in turns if t["turn_id"] == "crashed")
        assert (crashed["completed"], crashed["stop"]) == (True, "interrupted")
        events = stira["client"].get("/api/turns/crashed/events").get_json()["events"]
        assert events[-1]["data"]["reason"] == "server_restarted"
    finally:
        gen.close()


# --- the real process, a real socket closed mid-turn ----------------------------------------

SLOW_SEC = float(os.environ.get("STIRA_TEST_SLOW_TURN_SEC", "3"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics (as the headless E2E)")
def test_process_a_closed_connection_does_not_stop_a_long_turn(tmp_path):
    """``SLOW_SEC`` (3 s here; measured once with 31 s, see the PR) of model time, then a
    tool call and a second model call; the connection is closed one second in. Set
    STIRA_TEST_SLOW_TURN_SEC to run it longer."""
    from tests.fake_bedrock_endpoint import FakeBedrock
    from tests.test_headless_file_tools_e2e import Server

    with FakeBedrock() as fake:
        server = Server(tmp_path, fake, "devnull")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
            conn.request(
                "POST",
                "/api/chat",
                body=json.dumps({"message": f"SLOW {SLOW_SEC} CALL current_time {{}}"}),
                headers={"Content-Type": "application/json", "X-CSRF-Token": server.token},
            )
            sock = conn.sock
            reply = conn.getresponse()
            assert reply.status == 200
            seen = b""
            while b"turn.started" not in seen:
                seen += reply.read1(4096)
            started = time.monotonic()
            time.sleep(1.0)
            # The browser tab is closed while the model works: a reset, as a closed tab or a
            # dropped network sends, so the server's next write fails at once.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            reply.close()
            conn.close()

            def latest():
                turns = json.loads(server.request("GET", "/api/turns?limit=1")[1])["turns"]
                return turns[0] if turns and turns[0]["completed"] else None

            done = _until(latest, timeout=SLOW_SEC + 60, step=0.2)
            assert time.monotonic() - started >= SLOW_SEC - 1.5
            assert (done["stop"], done["error"]) == ("completed", False)
            history = json.loads(server.request("GET", "/api/chat/history")[1])["turns"]
            # The tool ran after the cut and the model answered its result.
            assert history[-1]["response"].startswith("RESULT success"), history[-1]
        finally:
            server.stop()


# --- review round (#482): stop leaks, cards of a stopped turn, the server stop's turn id -----


def _started_turn_id(response):
    """Read the stream until its turn.started ui_event; -> (iterator, that event's turn id)."""
    iterator = iter(response.response)
    seen = b""
    for chunk in iterator:
        seen += chunk if isinstance(chunk, bytes) else chunk.encode()
        for event, data in _frames([seen]):
            if event == "ui_event" and data.get("type") == "turn.started":
                return iterator, data["turn_id"]
    raise AssertionError("no turn.started on the stream")


def test_real_app_the_next_turn_after_a_stop_completes(stira):
    """M1: the stop's repeated agent.cancel() never reaches the next turn (chat, webhook or
    job): the next send right after a stop, and one after a late cancel(), are completed."""
    app, client, model = stira["app"], stira["client"], stira["model"]
    agent = app.extensions["new_session"]._agent
    for round_ in range(3):
        inside, release = threading.Event(), threading.Event()

        def during(inside=inside, release=release):
            inside.set()
            assert release.wait(WAIT)

        model.during = during
        response = _post_chat(stira, "long", f"m1-{round_}")
        next(iter(response.response))
        assert inside.wait(WAIT)
        stopped = client.post("/api/chat/cancel", headers={"X-CSRF-Token": stira["token"]})
        assert stopped.status_code == 202
        model.during = None
        release.set()
        assert _until(lambda: _latest(client))["stop"] == "cancelled"
        response.close()
        # at once, while the stop may still be repeating cancel()
        stira["chat"]("next")
        assert _latest(client)["stop"] == "completed"
    # a cancel() that lands after its turn ended stays on that turn's (discarded) agent
    agent.cancel()
    stira["chat"]("after a late cancel")
    assert _latest(client)["stop"] == "completed"
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["response"] and "stop" not in history[-1]


def test_real_app_a_stop_while_a_card_waits_ends_the_turn_at_once(stira):
    """M2 on the real broker: the card of the stopped turn is rejected, the turn ends
    cancelled right away (not at hitl_timeout_sec) and nothing is written."""
    app, client, model = stira["app"], stira["client"], stira["model"]
    app.extensions["new_session"]._agent.engine.config.hitl_timeout_sec = 600.0
    target = "notes/stopped.txt"
    model.script[:] = [
        tool_turn("file_write", {"path": target, "content": "x"}),
        text_turn("not written"),
    ]
    response = _post_chat(stira, "write it", "m2-a")
    _iterator, turn_id = _started_turn_id(response)
    card = _until(lambda: _card(client))
    assert card["turn_id"] == turn_id  # the card knows its turn (set on the worker)
    started = time.monotonic()
    stopped = client.post("/api/chat/cancel", headers={"X-CSRF-Token": stira["token"]})
    assert stopped.status_code == 202 and stopped.get_json()["rejected"] == 1
    done = _until(lambda: _latest(client), timeout=5.0)
    assert time.monotonic() - started < 5.0
    response.close()
    assert done["turn_id"] == turn_id and done["stop"] == "cancelled"
    assert _card(client) is None
    history = client.get("/api/hitl/history").get_json()["history"]
    assert next(h for h in history if h["id"] == card["id"])["status"] == "rejected"
    assert not (stira["data"] / "workspace" / target).exists()
    stira["chat"]("next")  # the lock is free again
    assert _latest(client)["stop"] == "completed"


def test_real_app_a_card_raised_after_the_stop_is_rejected_too(stira):
    """M2: the stop lands while the model still streams; the tool call it then returns
    raises no card that outlives the stop (rejected, or never raised), nothing is written."""
    app, client, model = stira["app"], stira["client"], stira["model"]
    app.extensions["new_session"]._agent.engine.config.hitl_timeout_sec = 600.0
    target = "notes/late.txt"
    inside, release = threading.Event(), threading.Event()

    def during():
        model.during = None
        inside.set()
        assert release.wait(WAIT)

    model.during = during
    model.script[:] = [
        tool_turn("file_write", {"path": target, "content": "x"}),
        text_turn("not written"),
    ]
    response = _post_chat(stira, "write it", "m2-b")
    _iterator, turn_id = _started_turn_id(response)
    assert inside.wait(WAIT)
    stopped = client.post("/api/chat/cancel", headers={"X-CSRF-Token": stira["token"]})
    assert stopped.status_code == 202
    release.set()
    done = _until(lambda: _latest(client), timeout=5.0)
    response.close()
    assert done["turn_id"] == turn_id and done["stop"] == "cancelled"
    assert _card(client) is None
    assert not (stira["data"] / "workspace" / target).exists()


def test_real_app_a_card_that_appears_after_the_stop_is_rejected_too(stira, monkeypatch):
    """M2: the stop lands while the turn's tool is already asking (its card not listed yet,
    so the first rejection misses it); the card that then appears is rejected as well."""
    app, client, model = stira["app"], stira["client"], stira["model"]
    app.extensions["new_session"]._agent.engine.config.hitl_timeout_sec = 600.0
    broker = app.extensions["hitl_broker"]
    asking, go_on = threading.Event(), threading.Event()
    original = broker._persist

    def slow_save(pending):  # the card is being saved: not listed yet
        asking.set()
        assert go_on.wait(WAIT)
        return original(pending)

    monkeypatch.setattr(broker, "_persist", slow_save)
    target = "notes/late-card.txt"
    model.script[:] = [
        tool_turn("file_write", {"path": target, "content": "x"}),
        text_turn("not written"),
    ]
    response = _post_chat(stira, "write it", "m2-c")
    _iterator, turn_id = _started_turn_id(response)
    assert asking.wait(WAIT)
    stopped = client.post("/api/chat/cancel", headers={"X-CSRF-Token": stira["token"]})
    assert stopped.status_code == 202 and stopped.get_json()["rejected"] == 0
    go_on.set()  # the card is raised only now
    done = _until(lambda: _latest(client), timeout=5.0)
    response.close()
    assert done["turn_id"] == turn_id and done["stop"] == "cancelled"
    assert _card(client) is None
    history = client.get("/api/hitl/history").get_json()["history"]
    assert [h["status"] for h in history if h.get("turn_id") == turn_id] == ["rejected"]
    assert not (stira["data"] / "workspace" / target).exists()


def test_real_app_the_servers_stop_records_interrupted_on_the_running_turn(stira):
    """M3: the stop runs on another thread (the lifecycle's, as on SIGTERM); its
    turn.completed lands on the turn the worker runs, exactly once."""
    from dashboard import stira_server

    app, client, model = stira["app"], stira["client"], stira["model"]
    stira["chat"]("an earlier turn")
    earlier = _latest(client)["turn_id"]
    inside, release = threading.Event(), threading.Event()

    def during():
        inside.set()
        release.wait(WAIT)

    model.during = during
    response = _post_chat(stira, "long", "m3")
    _iterator, turn_id = _started_turn_id(response)
    assert inside.wait(WAIT) and turn_id != earlier
    steps = {}
    stopper = threading.Thread(
        target=lambda: steps.update(stira_server.shutdown_app(app)), name="lifecycle"
    )
    stopper.start()
    stopper.join(WAIT)
    release.set()
    response.close()
    assert steps["chat_turn"] == "interrupted"
    turns = {t["turn_id"]: t for t in client.get("/api/turns?limit=5").get_json()["turns"]}
    assert (turns[turn_id]["stop"], turns[turn_id]["error"]) == ("interrupted", True)
    assert turns[earlier]["stop"] == "completed"
    events = client.get(f"/api/turns/{turn_id}/events").get_json()["events"]
    ends = [e for e in events if e["type"] == "turn.completed"]
    assert len(ends) == 1 and ends[0]["data"]["reason"] == "server_stopped"
    assert all(e["turn_id"] == turn_id for e in events)
