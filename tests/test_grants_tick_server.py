"""W6d (#296): the ``grant_tick`` fire kind on the scheduler and in the chat server.

Offline: the scheduler runs with ``background=False`` (its ``tick`` is called by hand);
the chat server is built with a model that raises if reached.
"""

from __future__ import annotations

import json
import threading

import pytest
from test_grants_server import SAME, _build, _close, _issue

from stiracore.scheduler import Scheduler

T0 = 1_800_000_000.0


class FakeTicker:
    def __init__(self) -> None:
        self.fires: list[tuple[str, object]] = []
        self.raises = False

    def fire(self, *, source, heartbeat_healthy, run_token=None):
        self.fires.append((source, heartbeat_healthy()))
        self.tokens = [*getattr(self, "tokens", []), run_token]
        if self.raises:
            raise RuntimeError("boom")
        return "no_grant"


def _scheduler(tmp_path, heartbeat=None):
    clock = [T0]
    scheduler = Scheduler(
        tmp_path / "sched",
        now_fn=lambda: clock[0],
        health_source=None if heartbeat is None else (lambda: dict(heartbeat)),
    )
    scheduler.start(background=False)
    return scheduler, clock


def test_no_ticker_attached_is_the_scheduler_as_before(tmp_path):
    scheduler, _clock = _scheduler(tmp_path)
    scheduler.tick()
    before = scheduler.status()
    scheduler.wake_grant_tick()  # nothing attached: a no-op
    assert scheduler._grant_event is False and not scheduler._wake.is_set()
    scheduler.tick()
    assert scheduler.status() == before
    scheduler.stop()


def test_each_tick_fires_once_and_an_event_wake_is_marked(tmp_path):
    healthy = {"enabled": True, "running": True, "last_tick_at": T0, "interval_sec": 60}
    scheduler, _clock = _scheduler(tmp_path, healthy)
    ticker = FakeTicker()
    scheduler.attach_grant_tick(ticker)
    with pytest.raises(RuntimeError):
        scheduler.attach_grant_tick(FakeTicker())
    scheduler.tick()
    scheduler.wake_grant_tick()
    assert scheduler._wake.is_set()
    scheduler.tick()
    scheduler.tick()
    assert ticker.fires == [("periodic", True), ("event", True), ("periodic", True)]
    scheduler.stop()


def test_the_heartbeat_health_is_read_fresh_and_none_is_not_healthy(tmp_path):
    stale = {"enabled": True, "running": True, "last_tick_at": T0 - 10_000, "interval_sec": 60}
    scheduler, _clock = _scheduler(tmp_path, stale)
    ticker = FakeTicker()
    scheduler.attach_grant_tick(ticker)
    scheduler.tick()
    scheduler.stop()
    unwired, _ = _scheduler(tmp_path / "b")
    other = FakeTicker()
    unwired.attach_grant_tick(other)
    unwired.tick()
    unwired.stop()
    assert ticker.fires == [("periodic", False)] and other.fires == [("periodic", None)]


def test_a_stopped_scheduler_fires_nothing_and_a_failing_fire_is_contained(tmp_path):
    scheduler, _clock = _scheduler(tmp_path)
    ticker = FakeTicker()
    ticker.raises = True
    scheduler.attach_grant_tick(ticker)
    scheduler.tick()  # the failure never breaks the jobs' tick
    scheduler.stop()
    scheduler.tick()
    assert len(ticker.fires) == 1


def test_the_fire_checks_the_scheduler_state_itself(tmp_path):
    """A stop between the jobs' evaluation and the fire: nothing fires."""
    scheduler, _clock = _scheduler(tmp_path)
    ticker = FakeTicker()
    scheduler.attach_grant_tick(ticker)
    scheduler._fire_grant_tick()
    scheduler.stop()
    scheduler._fire_grant_tick()
    assert len(ticker.fires) == 1


def test_the_fire_runs_outside_the_scheduler_lock(tmp_path):
    scheduler, _clock = _scheduler(tmp_path)
    held = []

    class Probe(FakeTicker):
        def fire(self, *, source, heartbeat_healthy, run_token=None):
            got = scheduler._lock.acquire(blocking=False)
            # An RLock re-enters on the same thread: count its depth instead.
            held.append(scheduler._lock._is_owned() and got)
            if got:
                scheduler._lock.release()
            return "no_grant"

    scheduler.attach_grant_tick(Probe())
    t = threading.Thread(target=scheduler.tick)
    t.start()
    t.join(5)
    assert held == [True]  # it could take the lock: nobody (not the tick) was holding it
    scheduler.stop()


# --- the chat server ------------------------------------------------------------------------------

TICK = {
    "autonomy": {
        "stage_c": True,
        "stage_c_exec": True,
        "heartbeat_start": True,
        "allow_browser_confirm": True,
    }
}
EXEC = {"autonomy": {"stage_c": True, "stage_c_exec": True, "allow_browser_confirm": True}}


def _server(tmp_path, monkeypatch, gateway, *, scheduler=True):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    if scheduler:
        monkeypatch.setenv("STIRA_SCHEDULER", "1")
    else:
        monkeypatch.delenv("STIRA_SCHEDULER", raising=False)
    build, _data = _build(tmp_path, monkeypatch, gateway)
    try:
        app = build()
    except Exception as error:  # noqa: BLE001 - a server that does not start fails here
        raise AssertionError(f"the server did not start: {type(error).__name__}") from None
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    ws = (roots[0] if roots else tmp_path) / "grant-ws"
    ws.mkdir(parents=True, exist_ok=True)
    return app, ws


def _issue_tick(client, ws, flag=True):
    body = {
        "goal": "tidy the docs",
        "scope": {"tools": ["file_read", "editor"], "workspace_subtree": str(ws)},
        "budget": {"total_tokens": 1000, "per_tick_tokens": 100},
        "verifier": "user",
        "confirm": "ISSUE",
        **({"heartbeat_start": True} if flag else {}),
    }
    first = client.post("/api/grants/issue", json=body, headers=SAME)
    if first.status_code != 428:
        return first
    op = first.get_json()["operation"]
    client.post(
        f"/api/grants/operations/{op['id']}/confirm",
        json={"token": op["confirm_token"], "acknowledged": True, "instance": op["instance"]},
        headers=SAME,
    )
    return client.post(
        "/api/grants/issue", json=body, headers={**SAME, "X-Stira-Host-Operation": op["id"]}
    )


def _volatile(answer):
    """``/api/grants`` without the values that move with the clock or the id."""
    data = json.loads(json.dumps(answer))
    data.pop("instance", None)
    for grant in data.get("grants", []):
        for key in ("grant_id", "short", "session_ref", "ttl_left_sec", "issued_wall"):
            grant.pop(key, None)
        if grant.get("claim_evidence"):
            grant["claim_evidence"].pop("claimed_at", None)
    return data


def test_with_the_switch_on_the_ticker_is_attached_and_wakes_on_the_heartbeat(
    tmp_path, monkeypatch
):
    app, ws = _server(tmp_path, monkeypatch, TICK)
    try:
        ticker = app.extensions["grant_tick"]
        scheduler = app.extensions["scheduler"]
        assert scheduler._grant_tick is ticker
        client = app.test_client()
        done = _issue_tick(client, ws)
        assert done.status_code == 200, done.get_json()
        assert done.get_json()["grant"]["heartbeat_start"] is True
        listed = client.get("/api/grants").get_json()
        item = listed["grants"][0]
        assert item["tick"]["started"] == 0 and item["tick"]["spacing_sec"] == 300
        assert "heartbeat_start" not in listed  # only a flagged grant carries anything
        one = client.get(f"/api/grants/{item['grant_id']}").get_json()
        assert one["grant"]["tick"]["costs"] == []
        # No heartbeat in this server: every wake is skipped, nothing reaches the model.
        assert ticker.fire(
            heartbeat_healthy=lambda: scheduler._current_heartbeat(), run_token=scheduler._run_token
        ) == ("heartbeat_unhealthy")
        page = client.get("/settings").get_data(as_text=True)
        assert 'id="grants-heartbeat-start"' in page
    finally:
        _close(app)


def test_without_the_scheduler_nothing_wakes_and_the_grant_says_so(tmp_path, monkeypatch):
    """N3: the ticker exists but is attached to nothing; a flagged grant shows
    ``unavailable: scheduler_off`` (settings panel and board), and nothing ever fires."""
    app, ws = _server(tmp_path, monkeypatch, TICK, scheduler=False)
    try:
        ticker = app.extensions["grant_tick"]
        assert ticker.unavailable == "scheduler_off" and app.extensions.get("scheduler") is None
        client = app.test_client()
        assert _issue_tick(client, ws).status_code == 200
        item = client.get("/api/grants").get_json()["grants"][0]
        assert item["tick"].get("unavailable") == "scheduler_off"
        assert item["tick"]["started"] == 0 and item["tick"]["skipped"] == {}
        grant = client.get("/api/activity").get_json()["grant"]
        assert grant["tick"].get("unavailable") == "scheduler_off"
    finally:
        _close(app)


@pytest.mark.parametrize("scheduler", [True, False])
def test_off_the_answers_are_as_before(tmp_path, monkeypatch, scheduler):
    """With the switch off (or a grant without the flag) /api/grants and /api/activity
    carry nothing new: the same keys and values as a server without W6d's switch."""
    answers = {}
    for name, gateway, flag in (("exec", EXEC, False), ("tick", TICK, False)):
        (tmp_path / name).mkdir()
        app, ws = _server(tmp_path / name, monkeypatch, gateway, scheduler=scheduler)
        try:
            client = app.test_client()
            if name == "exec":
                assert "grant_tick" not in app.extensions
                assert app.extensions.get("scheduler") is None or (
                    app.extensions["scheduler"]._grant_tick is None
                )
                assert 'id="grants-heartbeat-start"' not in client.get("/settings").get_data(
                    as_text=True
                )
            assert _issue_tick(client, ws, flag=flag).status_code == 200
            listed = _volatile(client.get("/api/grants").get_json())
            activity = client.get("/api/activity").get_json()
            answers[name] = (listed, sorted(activity), sorted(activity.get("grant") or {}))
        finally:
            _close(app)
    assert answers["exec"] == answers["tick"]


def test_the_off_server_is_byte_identical_for_activity_and_grants(tmp_path, monkeypatch):
    """No switch: /api/grants has no tick key, no heartbeat_start key anywhere."""
    app, ws = _server(tmp_path, monkeypatch, EXEC)
    try:
        client = app.test_client()
        _issue(client, ws)
        text = client.get("/api/grants").get_data(as_text=True)
        activity = client.get("/api/activity").get_data(as_text=True)
        for word in ("heartbeat_start", '"tick"', "tick_costs", "origin"):
            assert word not in text and word not in activity, word
    finally:
        _close(app)


def test_the_activity_answer_carries_the_tick_block_of_a_flagged_grant(tmp_path, monkeypatch):
    app, ws = _server(tmp_path, monkeypatch, TICK)
    try:
        client = app.test_client()
        assert _issue_tick(client, ws).status_code == 200
        ticker = app.extensions["grant_tick"]
        ticker.fire(
            heartbeat_healthy=lambda: False, run_token=app.extensions["scheduler"]._run_token
        )
        grant = client.get("/api/activity").get_json()["grant"]
        assert grant["tick"]["last"] == "heartbeat_unhealthy"
        assert grant["tick"]["skipped"] == {"heartbeat_unhealthy": 1}
        with ticker._lock:  # a held lock on the read path: never waited for
            assert client.get("/api/activity").get_json()["grant"] == {"error": "grant_busy"}
    finally:
        _close(app)


@pytest.mark.parametrize("gateway", [TICK, EXEC])
def test_the_heartbeat_tick_wakes_the_scheduler_only_when_on(tmp_path, monkeypatch, gateway):
    """The server's after-tick hook asks the scheduler for an event-driven wake (memory
    only, on the scheduler's own thread) — with the switch on; off, it never does."""
    calls = []
    monkeypatch.setattr(Scheduler, "wake_grant_tick", lambda self: calls.append(self))
    monkeypatch.setenv("STIRA_HEARTBEAT", "1")
    app, _ws = _server(tmp_path, monkeypatch, gateway)
    try:
        daemon = app.extensions["heartbeat"]
        assert daemon is not None
        daemon._on_tick({})
        if gateway is TICK:
            assert calls and all(c is app.extensions["scheduler"] for c in calls)
        else:
            assert calls == []
    finally:
        _close(app)


def test_a_heartbeat_that_is_off_is_warned_and_shown(tmp_path, monkeypatch, caplog):
    """Decision (1): one structured warning at start-up and ``unavailable: heartbeat_off``
    on a flagged grant's tick block (codes only)."""
    monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)
    with caplog.at_level("WARNING"):
        app, ws = _server(tmp_path, monkeypatch, TICK)
    try:
        lines = [
            r.getMessage()
            for r in caplog.records
            if "heartbeat_start unavailable" in r.getMessage()
        ]
        assert lines == ["grants: heartbeat_start unavailable (reason=heartbeat_off)"]
        client = app.test_client()
        assert _issue_tick(client, ws).status_code == 200
        item = client.get("/api/grants").get_json()["grants"][0]
        assert item["tick"].get("unavailable") == "heartbeat_off"
        assert client.get("/api/activity").get_json()["grant"]["tick"].get("unavailable") == (
            "heartbeat_off"
        )
    finally:
        _close(app)


def test_a_running_heartbeat_is_not_unavailable(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("STIRA_HEARTBEAT", "1")
    with caplog.at_level("WARNING"):
        app, _ws = _server(tmp_path, monkeypatch, TICK)
    try:
        assert app.extensions["grant_tick"].unavailable is None
        assert not [r for r in caplog.records if "heartbeat_start unavailable" in r.getMessage()]
    finally:
        _close(app)


def test_no_scheduler_is_one_structured_warning(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        app, _ws = _server(tmp_path, monkeypatch, TICK, scheduler=False)
    try:
        lines = [
            r.getMessage()
            for r in caplog.records
            if "heartbeat_start unavailable" in r.getMessage()
        ]
        assert lines == ["grants: heartbeat_start unavailable (reason=scheduler_off)"]
    finally:
        _close(app)
