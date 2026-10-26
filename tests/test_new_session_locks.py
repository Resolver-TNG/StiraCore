"""/new and approvals: bounded locks, the lock order, and the audit's durability (#350, round 3).

Real chat server on the offline model (tests/test_new_session.py). Ordering is forced with events;
the bounds are shortened with monkeypatch so a stall shows up as a timeout, not a hang.
"""

from __future__ import annotations

import json
import linecache
import logging
import sqlite3
import sys
import threading
import time
import types

import pytest

from stiracore.agent.hitl_resume import ResumeOutcome
from stiracore.observability import chat_api
from tests.test_new_session import NEW, _agent, _history, _messages_at_model, make_stira

WAIT = 5


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _rows(stira):
    rows = sqlite3.connect(stira["data"] / "events.db").execute(
        "SELECT event_type, attributes_json FROM events ORDER BY seq"
    )
    return [(t, json.loads(a)) for t, a in rows]


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
    thread = threading.Thread(target=lambda: out.setdefault("value", fn()), daemon=True)
    thread.start()
    return thread, out


def _no_replay(monkeypatch):
    from dashboard import stira_server

    monkeypatch.setattr(
        stira_server,
        "resume_approved_request",
        lambda agent, store, request_id, on_replay=None: ResumeOutcome(False, "test"),
    )


# --- the Blocker: a stalled audit never holds approvals up; every wait is bounded -------------


def test_a_stalled_audit_write_holds_no_shared_lock_and_decisions_go_through(stira, monkeypatch):
    app, client = stira["app"], stira["client"]
    host = app.extensions["new_session"]
    _no_replay(monkeypatch)
    stira["chat"]("hello")
    args = _restored_pending(app)
    in_emit, release = threading.Event(), threading.Event()
    real_emit = host._events.emit

    def stalled(event):
        if event.event_type == "session.reset":
            in_emit.set()
            assert release.wait(WAIT)
        return real_emit(event)

    monkeypatch.setattr(host._events, "emit", stalled)
    reset, out = _in_thread(lambda: stira["new"]("stall"))
    assert in_emit.wait(WAIT)
    # the shared lock is free while the audit row is being written
    assert host.lock.acquire(blocking=False)
    host.lock.release()
    started = time.monotonic()
    decided = client.post("/api/hitl/req-1/decision", json={"action": "approve", "args_hash": args})
    assert decided.status_code == 202
    assert time.monotonic() - started < 2.0
    release.set()
    reset.join(WAIT)
    # the approval came first: the reset is refused or runs after its resume ended;
    # it never interleaves (the stored approval is decided either way)
    assert out["value"].status_code in (200, 409)
    assert app.extensions["approval_store"].get("req-1").status != "pending"


def test_a_decision_waits_a_bounded_time_and_stays_pending_when_busy(stira, monkeypatch):
    from dashboard import stira_server

    app, client = stira["app"], stira["client"]
    host = app.extensions["new_session"]
    _no_replay(monkeypatch)
    args = _restored_pending(app)
    monkeypatch.setattr(stira_server, "DECISION_LOCK_TIMEOUT_SEC", 0.2)
    holding, done = threading.Event(), threading.Event()

    def hold():
        with host.lock:
            holding.set()
            assert done.wait(WAIT)

    holder, _ = _in_thread(hold)
    assert holding.wait(WAIT)
    started = time.monotonic()
    busy = client.post("/api/hitl/req-1/decision", json={"action": "approve", "args_hash": args})
    elapsed = time.monotonic() - started
    assert (busy.status_code, busy.get_json()["code"]) == (503, "busy")
    assert elapsed < 2.0
    # the decision was not recorded: still pending, still listed, decidable later
    assert app.extensions["approval_store"].get("req-1").status == "pending"
    assert any(r["id"] == "req-1" for r in client.get("/api/hitl/pending").get_json()["requests"])
    done.set()
    holder.join(WAIT)
    again = client.post("/api/hitl/req-1/decision", json={"action": "approve", "args_hash": args})
    assert again.status_code == 202


def test_a_resume_reservation_that_cannot_wait_is_refused_not_lost(stira, monkeypatch):
    from dashboard import stira_server

    host = stira["app"].extensions["new_session"]
    monkeypatch.setattr(stira_server, "DECISION_LOCK_TIMEOUT_SEC", 0.1)
    holding, done = threading.Event(), threading.Event()

    def hold():
        with host.lock:
            holding.set()
            assert done.wait(WAIT)

    holder, _ = _in_thread(hold)
    assert holding.wait(WAIT)
    with pytest.raises(chat_api.LockBusy):
        host.resume_scheduled()
    done.set()
    holder.join(WAIT)
    assert host.blocker() is None


def test_the_reset_section_waits_a_bounded_time_for_the_shared_lock(stira, monkeypatch):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("hello")
    monkeypatch.setattr(chat_api, "NEW_SESSION_LOCK_TIMEOUT_SEC", 0.2)
    holding, done = threading.Event(), threading.Event()

    def hold():
        with host.lock:
            holding.set()
            assert done.wait(WAIT)

    holder, _ = _in_thread(hold)
    assert holding.wait(WAIT)
    started = time.monotonic()
    refused = stira["new"]("bounded")
    assert time.monotonic() - started < 2.0
    done.set()
    holder.join(WAIT)
    assert (refused.status_code, refused.get_json()["code"]) == (409, "decision_in_progress")
    assert refused.get_json()["outcome_audit"] == "ok"
    assert len(_history(stira["client"])["turns"]) == 1


# --- the lock order, recorded while the real paths run ------------------------------------------

_SITES = {
    "turn_lock = TurnLock(threading.Lock(), preempt)": "turn",
    "new_session_lock = threading.RLock()": "shared",
    "state_lock = threading.Lock()": "chat.state",
    "self._condition = threading.Condition()": "broker",
}


class _Recorder:
    def __init__(self):
        self.local = threading.local()
        self.edges = set()
        self.guard = threading.Lock()

    def held(self):
        if not hasattr(self.local, "held"):
            self.local.held = []
        return self.local.held

    def acquired(self, name):
        held = self.held()
        with self.guard:
            for previous in held:
                if previous != name:
                    self.edges.add((previous, name))
        held.append(name)

    def released(self, name):
        held = self.held()
        for index in range(len(held) - 1, -1, -1):
            if held[index] == name:
                del held[index]
                return


class _Tracked:
    def __init__(self, inner, name, recorder):
        self._inner, self._name, self._rec = inner, name, recorder

    def acquire(self, blocking=True, timeout=-1):
        ok = self._inner.acquire(blocking, timeout)
        if ok:
            self._rec.acquired(self._name)
        return ok

    def release(self):
        self._rec.released(self._name)
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def __getattr__(self, attr):  # wait / notify / locked / _is_owned
        return getattr(self._inner, attr)


def _tracking_threading(recorder):
    real = threading

    def factory(kind):
        maker = getattr(real, kind)

        def make(*args, **kwargs):
            frame = sys._getframe(1)
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            lock = maker(*args, **kwargs)
            for site, name in _SITES.items():
                if line.endswith(site):
                    return _Tracked(lock, name, recorder)
            return lock

        return make

    shim = types.SimpleNamespace(
        **{n: getattr(real, n) for n in dir(real) if not n.startswith("__")}
    )
    shim.Lock, shim.RLock, shim.Condition = factory("Lock"), factory("RLock"), factory("Condition")
    return shim


def test_no_lock_is_ever_taken_in_both_orders(tmp_path, monkeypatch):
    """chat turn, /new (ok and refused), an approval decision with its resume, a failed audit:
    no pair of locks is taken in both orders; the event store is never used under the shared
    lock; the turn lock is never taken while the shared lock is held."""
    from dashboard import stira_server

    recorder = _Recorder()
    monkeypatch.setattr(stira_server, "threading", _tracking_threading(recorder))
    monkeypatch.setattr(chat_api, "threading", _tracking_threading(recorder))
    gen = make_stira(tmp_path, monkeypatch)
    stira = next(gen)
    try:
        app, client = stira["app"], stira["client"]
        host = app.extensions["new_session"]
        host._events._lock = _Tracked(host._events._lock, "events", recorder)
        assert isinstance(app.extensions["turn_lock"]._plain_lock, _Tracked)  # inside the TurnLock
        assert isinstance(host.lock, _Tracked)
        stira["chat"]("hello")
        assert stira["new"]("o1").status_code == 200
        stira["chat"]("again")
        args = _restored_pending(app)
        assert stira["new"]("o2").status_code == 409  # approval_pending
        decided = client.post(
            "/api/hitl/req-1/decision", json={"action": "approve", "args_hash": args}
        )
        assert decided.status_code == 202
        deadline = time.monotonic() + WAIT
        while host.blocker() is not None and time.monotonic() < deadline:
            time.sleep(0.01)  # the real resume thread: refused (no checkpoint cp-1)
        real_emit = host._events.emit
        host._events.emit = lambda event: (_ for _ in ()).throw(OSError("full"))
        assert stira["new"]("o3").status_code == 500
        host._events.emit = real_emit
        assert stira["new"]("o4").status_code == 200
    finally:
        gen.close()
    edges = recorder.edges
    assert ("turn", "shared") in edges and ("shared", "broker") in edges, edges
    assert ("turn", "events") in edges  # the audit rows: under the turn lock only
    assert ("shared", "events") not in edges, "the event store under the shared lock"
    assert ("shared", "turn") not in edges, "the turn lock taken under the shared lock"
    inverted = {(a, b) for a, b in edges if (b, a) in edges}
    assert not inverted, inverted


# --- Major: the audit is durable; a failure keeps the conversation -------------------------------


def test_emit_returns_after_commit_and_raises_on_a_read_only_database(stira):
    """The real event store: emit() commits synchronously (BEGIN IMMEDIATE ... COMMIT) and a
    write error propagates. A database switched to read-only refuses the reset row: nothing
    is reset, and the store stays refused afterwards (sticky, fail-closed)."""
    host, client, model = (
        stira["app"].extensions["new_session"],
        stira["client"],
        stira["model"],
    )
    conn = host._events._connection
    assert tuple(conn.execute("PRAGMA synchronous").fetchone()) == (2,)  # FULL
    assert tuple(conn.execute("PRAGMA journal_mode").fetchone()) == ("wal",)
    stira["chat"]("one")
    stira["chat"]("two")
    assert stira["new"]("ok-first").status_code == 200
    # what emit() returned is already in the database (a second connection sees it)
    assert [t for t, _ in _rows(stira)] == ["session.reset", "session.started"]
    stira["chat"]("three")
    host._events._connection.execute("PRAGMA query_only = ON")
    failed = stira["new"]("ro")
    assert (failed.status_code, failed.get_json()["code"]) == (500, "audit_failed")
    assert [t["message"] for t in _history(client)["turns"]] == ["three"]
    assert _agent(stira["app"]).agent is not None
    stira["chat"]("four")
    assert _messages_at_model(model) == 3  # three + four: the conversation was kept
    assert not host._events.healthy  # sticky: the next reset is refused before any row
    refused = stira["new"]("ro-2")
    assert (refused.status_code, refused.get_json()["code"]) == (503, "audit_unavailable")
    assert len(_history(client)["turns"]) == 2


def test_a_failed_outcome_row_is_retried_once_logged_and_reported(stira, caplog):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("one")
    real_emit = host._events.emit
    attempts = []

    def outcome_fails(event):
        if event.event_type == "session.started":
            attempts.append(event.event_type)
            raise OSError("disk full")
        return real_emit(event)

    host._events.emit = outcome_fails
    with caplog.at_level(logging.ERROR):
        response = stira["new"]("oa")
    host._events.emit = real_emit
    body = response.get_json()
    assert response.status_code == 200
    assert body["outcome_audit"] == "failed"
    assert attempts == ["session.started", "session.started"]  # one retry
    assert "row not written" in caplog.text
    # the reset row is the record; the reset is not run again for this key
    assert [t for t, _ in _rows(stira)] == ["session.reset"]
    assert _history(stira["client"])["turns"] == []
    assert stira["new"]("oa").get_json()["code"] == "duplicate_request"


def test_a_failed_reset_closes_its_row_and_reports_the_outcome_audit(stira):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("one")
    real_reset = host.reset
    host.reset = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    response = stira["new"]("rf")
    host.reset = real_reset
    assert (response.status_code, response.get_json()["code"]) == (500, "reset_failed")
    assert response.get_json()["outcome_audit"] == "ok"
    assert [t for t, _ in _rows(stira)] == ["session.reset", "session.reset_failed"]
    assert stira["new"]("rf").status_code == 200  # not recorded as done: may run again


def test_a_success_reports_its_outcome_audit(stira):
    stira["chat"]("one")
    body = stira["new"]("fine").get_json()
    assert body["outcome_audit"] == "ok"
    assert body["status"] == "started"
    _ = NEW


# --- final round: only the acquisition is "busy"; when audit_unavailable starts and ends --------


def test_only_a_failed_acquisition_is_busy(stira):
    broker = stira["app"].extensions["hitl_broker"]

    def raises_inside(*args, **kwargs):
        raise chat_api.LockBusy("from inside _decide")

    broker._decide = raises_inside
    with pytest.raises(chat_api.LockBusy):  # propagates: never reported as "nothing recorded"
        broker.decide("r", chat_api.HITLDecision(chat_api.HITLAction.REJECT))
    host = stira["app"].extensions["new_session"]
    holding, done = threading.Event(), threading.Event()

    def hold():
        with host.lock:
            holding.set()
            assert done.wait(WAIT)

    from dashboard import stira_server

    holder, _ = _in_thread(hold)
    assert holding.wait(WAIT)
    original = stira_server.DECISION_LOCK_TIMEOUT_SEC
    stira_server.DECISION_LOCK_TIMEOUT_SEC = 0.1
    try:
        outcome = broker.decide("r", chat_api.HITLDecision(chat_api.HITLAction.REJECT))
    finally:
        stira_server.DECISION_LOCK_TIMEOUT_SEC = original
        done.set()
        holder.join(WAIT)
    assert outcome is chat_api.DecideOutcome.BUSY


def test_a_busy_decision_retried_with_its_key_is_recorded_once(stira, monkeypatch):
    from dashboard import stira_server

    app, client = stira["app"], stira["client"]
    host = app.extensions["new_session"]
    _no_replay(monkeypatch)
    args = _restored_pending(app)
    monkeypatch.setattr(stira_server, "DECISION_LOCK_TIMEOUT_SEC", 0.1)
    holding, done = threading.Event(), threading.Event()

    def hold():
        with host.lock:
            holding.set()
            assert done.wait(WAIT)

    holder, _ = _in_thread(hold)
    assert holding.wait(WAIT)
    body = {"action": "approve", "args_hash": args}
    headers = {"Idempotency-Key": "decision-key-1"}
    busy = client.post("/api/hitl/req-1/decision", json=body, headers=headers)
    done.set()
    holder.join(WAIT)
    assert (busy.status_code, busy.get_json()["code"]) == (503, "busy")
    again = client.post("/api/hitl/req-1/decision", json=body, headers=headers)
    assert again.status_code == 202  # the 503 left the key unused: the retry runs once
    third = client.post("/api/hitl/req-1/decision", json=body, headers=headers)
    assert (third.status_code, third.get_json()["code"]) == (409, "duplicate_request")
    store = app.extensions["approval_store"]
    decided = [
        e
        for e in store.audit_events()
        if e.get("request_id") == "req-1" and e.get("kind") != "submitted"
    ]
    assert [e.get("kind") for e in decided][:1] == ["decided"], decided
    assert sum(1 for e in decided if e.get("kind") == "decided") == 1


def test_audit_unavailable_after_a_transient_lock_is_not_permanent(stira):
    """SQLITE_BUSY on the reset row: that request is 500 audit_failed, nothing reset, and the
    store is not refused afterwards; once the other writer is gone /new works."""
    host, client = stira["app"].extensions["new_session"], stira["client"]
    stira["chat"]("one")
    host._events._connection.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(stira["data"] / "events.db", isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    try:
        locked = stira["new"]("t1")
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert (locked.status_code, locked.get_json()["code"]) == (500, "audit_failed")
    assert host._events.error is None and host.audit_ready()
    assert len(_history(client)["turns"]) == 1
    assert stira["new"]("t1").status_code == 200


def test_a_store_busy_at_open_is_retried_by_the_write(stira):
    host = stira["app"].extensions["new_session"]
    stira["chat"]("one")
    host._events._state = None  # as after an open that found the database busy
    assert not host._events.healthy and host._events.error is None
    assert host.audit_ready()  # not refused up front: the write retries the open
    assert stira["new"]("b1").status_code == 200
    assert host._events.healthy


def test_a_sticky_store_failure_stays_refused_until_the_store_is_reopened(stira, tmp_path):
    from stiracore.observability.event_store import SQLiteEventStore

    host, client = stira["app"].extensions["new_session"], stira["client"]
    stira["chat"]("one")
    host._events._connection.execute("PRAGMA query_only = ON")
    assert stira["new"]("s1").get_json()["code"] == "audit_failed"
    host._events._connection.execute("PRAGMA query_only = OFF")  # the fault is gone ...
    refused = stira["new"]("s2")  # ... but the store stays refused (fail-closed)
    assert (refused.status_code, refused.get_json()["code"]) == (503, "audit_unavailable")
    assert len(_history(client)["turns"]) == 1
    # a reopen (a restart) classifies the files and accepts writes again
    reopened = SQLiteEventStore(stira["data"] / "events.db")
    assert reopened.healthy and reopened.verify_chain()[0]
    host._events = reopened
    assert stira["new"]("s3").status_code == 200
