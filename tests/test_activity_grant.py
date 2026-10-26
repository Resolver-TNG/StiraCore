"""W5b (#296): the goal grant on the Stage board — the ``grant`` block of ``GET /api/activity``.

* **Off = as before.** Without ``autonomy.stage_c`` the answer is the one of main before
  W5b, key for key and value for value (a golden recorded on main's code, fixed clock).
* **An allow-list.** ``activity.grant_view`` keeps codes, counts, flags and tool names; every
  field is pinned, a value that is not a plain code is dropped (a tool name is ``other``).
* **No goal, ever.** A high-entropy marker in the goal, echoed by the model in its text and
  in a tool argument, is in no answer of ``/api/activity`` (block and rows).
* **Memory only.** The read goes through ``GrantService.board_view`` and
  ``GoalIterationExecutor.board`` — no look, no store I/O; a failing read is one fixed code,
  never a 500. Offline: no model, no Bedrock.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask
from test_grants_server import SAME, _build, _close, _issue

from stiracore.observability import activity, chat_api
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore

MARKER = "w5bMarker-7c41e09d2b8a5f36"
EXEC = {"autonomy": {"stage_c": True, "stage_c_exec": True, "allow_browser_confirm": True}}
T0 = 1_800_000_000.0


# --- off: the answer of main, byte for byte ---------------------------------------------------


def golden_scenario(tmp_path, monkeypatch) -> list[str]:
    """The raw bodies of three reads on a fixed clock (also run on main's code for the golden)."""
    monkeypatch.setattr(chat_api.time, "time", lambda: T0)
    monkeypatch.setattr(
        chat_api.yolo_runtime,
        "status",
        lambda: {"window": {"state": "active", "tier": "workspace", "remaining_sec": 60}},
    )
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    store = recorder.store
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(), runner=lambda _m: iter(["x"]), turn_recorder=recorder
        )
    )
    client = app.test_client()
    store.append("t1", "turn.started", {})
    store.append("t1", "tool_call.updated", {"tool_use_id": "u", "name": "file_read",
                                             "status": "done", "duration_ms": 12})  # fmt: skip
    store.append("t1", "turn.completed", {"usage": {"input_tokens": 5, "output_tokens": 2},
                                          "stop": "completed", "error": False})  # fmt: skip
    first = client.get("/api/activity")
    store.append("t2", "turn.started", {})
    diff = client.get(f"/api/activity?after_id={first.get_json()['cursor']}")
    empty = client.get(f"/api/activity?after_id={diff.get_json()['cursor']}")
    return [r.get_data(as_text=True) for r in (first, diff, empty)]


# Recorded with main's code (76189f7) by running golden_scenario (the raw answers).
GOLDEN = [
    (
        '{"cursor":3,"items":[{"id":1,"kind":"turn","ts":1800000000.0,"turn":"t1","what":"sta'
        'rted"},{"duration_ms":12,"id":2,"kind":"tool","name":"file_read","ok":true,"ts":1800'
        '000000.0,"turn":"t1","what":"done"},{"id":3,"kind":"turn","ok":true,"stop":"complete'
        'd","ts":1800000000.0,"turn":"t1","what":"completed"}],"more":false,"reset":true,"ser'
        'ver_time":1800000000.0,"stall_sec":120,"started_at":1800000000.0,"state":{"code":"id'
        'le","last_event":null,"running":false,"since":null},"totals":{"cache_read":0,"cache_'
        'write":0,"context_tokens":null,"cost":null,"input":5,"output":2,"turns":1},"yolo":{"'
        'remaining_sec":60,"state":"active","tier":"workspace"}}\n'
    ),
    (
        '{"cursor":4,"items":[{"id":4,"kind":"turn","ts":1800000000.0,"turn":"t2","what":"sta'
        'rted"}],"more":false,"reset":false,"server_time":1800000000.0,"stall_sec":120,"start'
        'ed_at":1800000000.0,"state":{"code":"idle","last_event":null,"running":false,"since"'
        ':null},"totals":{"cache_read":0,"cache_write":0,"context_tokens":null,"cost":null,"i'
        'nput":5,"output":2,"turns":1},"yolo":{"remaining_sec":60,"state":"active","tier":"wo'
        'rkspace"}}\n'
    ),
    (
        '{"cursor":4,"items":[],"more":false,"reset":false,"server_time":1800000000.0,"stall_'
        'sec":120,"started_at":1800000000.0,"state":{"code":"idle","last_event":null,"running'
        '":false,"since":null},"totals":{"cache_read":0,"cache_write":0,"context_tokens":null'
        ',"cost":null,"input":5,"output":2,"turns":1},"yolo":{"remaining_sec":60,"state":"act'
        'ive","tier":"workspace"}}\n'
    ),
]


def test_off_the_answer_is_mains_byte_for_byte(tmp_path, monkeypatch):
    bodies = golden_scenario(tmp_path, monkeypatch)
    assert bodies == GOLDEN  # the raw bytes of the three answers
    assert all('"grant"' not in body for body in bodies)


def test_off_in_the_server_no_grant_key_and_no_stage_attribute(tmp_path, monkeypatch):
    monkeypatch.setenv("STIRA_STAGE", "1")
    build, _data = _build(tmp_path, monkeypatch, {})
    app = build()
    try:
        client = app.test_client()
        assert "grants" not in app.extensions
        body = client.get("/api/activity").get_json()
        assert "grant" not in body
        page = client.get("/stage/")
        assert page.status_code == 200 and "data-grants" not in page.get_data(as_text=True)
    finally:
        _close(app)


# --- the allow-list ---------------------------------------------------------------------------


def _view(**over):
    view = {
        "grant_id": "ab" * 16,
        "short": "abababab",
        "kind": "goal",
        "session_ref": "grant:" + "ab" * 16,
        "state": "active",
        "reason": "",
        "goal_state": "active",
        "goal_reason": "",
        "headline": {"subject": "grant", "state": "active", "reason": ""},
        "resume_to": {"state": None, "reason": None},
        "tools": ["editor", "file_read"],
        "ttl_sec": 7200,
        "ttl_left_sec": 3600,
        "budget_total": 1000,
        "budget_per_tick": 100,
        "budget_used": 250,
        "budget_left": 750,
        "iterations": 2,
        "max_iterations": 10,
        "no_progress": 0,
        "no_progress_ticks": 3,
        "verifier": "user",
        "generation": 4,
        "stop_epoch": 0,
        "recheck": False,
        "late_verification": None,
        "issued_wall": T0,
        "ended_wall": None,
    }
    view.update(over)
    return view


def _board(**over):
    board = {
        "iteration": {"state": "running", "number": 3, "end": "", "detail": "", "card": "none",
                      "abandoned": False, "residual": [], "report": "", "tools": ["editor"],
                      "model_calls": 1, "tool_calls": 0, "writes": 0, "checks": 0, "waits": 0,
                      "reports": 0, "tick_used": 40},
        "outcomes": [{"number": 2, "end": "done", "detail": "", "tools": ["editor", "file_read"],
                      "tick_used": 60, "ts": T0 + 5, "duration_ms": 1200}],
    }  # fmt: skip
    board.update(over)
    return board


def test_the_block_is_exactly_the_allow_list():
    out = activity.grant_view(_view(), _board(), pending=[{"grant_card": True}, {}], since=T0)
    assert out == {
        "id": "abababab",
        "state": "active",
        "live": True,
        "headline": {"subject": "grant", "state": "active"},
        "goal_state": "active",
        "iterations": 2,
        "max_iterations": 10,
        "ttl_sec": 7200,
        "ttl_left_sec": 3600,
        "budget_total": 1000,
        "budget_left": 750,
        "budget_per_tick": 100,
        "awaiting_verification": False,
        "cards": 1,
        "card": True,
        "iteration": {"run": "running", "number": 3, "tick_used": 40},
        "outcomes": [
            {"what": "done", "number": 2, "duration_ms": 1200, "tick_used": 60, "ts": T0 + 5,
             "tools": ["editor", "file_read"]},
        ],
    }  # fmt: skip


def test_without_the_executor_no_iteration_and_no_outcomes():
    out = activity.grant_view(_view(reason="user", state="paused"), None, since=None)
    assert "iteration" not in out and "outcomes" not in out
    assert out["card"] is False and out["cards"] == 0
    assert out["state"] == "paused" and out["reason"] == "user" and out["live"] is True


def test_nothing_to_show():
    assert activity.grant_view(None) is None
    assert activity.grant_view("x") is None
    # issued before this server started (ended by the restart): not shown
    assert activity.grant_view(_view(issued_wall=T0 - 1), since=T0) is None
    assert activity.grant_view(_view(issued_wall=None), since=T0) is None
    assert activity.grant_view(_view(issued_wall=T0 - 1), since=None) is not None


@pytest.mark.parametrize("state, live", [("active", True), ("paused", True), ("halted", True),
                                         ("revoked", False), ("expired", False), ("ended", False),
                                         ("bad state!", False)])  # fmt: skip
def test_live_follows_the_grant_state(state, live):
    out = activity.grant_view(_view(state=state))
    assert out["live"] is live and out["state"] == (state if " " not in state else "other")


def test_values_that_are_not_codes_are_dropped():
    hostile = _view(
        short=MARKER,
        reason=f"{MARKER} reason text",
        goal_state=["x"],
        goal_reason="a b",
        headline={"subject": MARKER, "state": "x y", "reason": MARKER * 3},
        ttl_left_sec=-1,
        budget_left=1.5,
        iterations=True,
        max_iterations=2**60,
        ttl_sec="7200",
        goal=MARKER,
        workspace_subtree=f"/tmp/{MARKER}",
    )
    board = _board(
        iteration={"state": MARKER, "number": -1, "end": "x y", "detail": MARKER * 3,
                   "card": MARKER, "tick_used": "4", "report": MARKER, "residual": [MARKER]},
        outcomes=[{"end": f"{MARKER} !", "tools": [MARKER + "/x", "editor"] + ["e"] * 20,
                   "number": "2", "ts": "now", "detail": "a b", "goal": MARKER}, "row", None],
    )  # fmt: skip
    out = activity.grant_view(hostile, board, pending=[{"grant_card": "yes"}, "x"])
    text = json.dumps(out)
    assert "/tmp" not in text and " " not in json.dumps(out, separators=(",", ":"))
    assert out["id"] is None
    assert out["headline"] == {"subject": "grant", "state": "other"}
    assert out["goal_state" if False else "awaiting_verification"] is False
    for key in ("reason", "goal_state", "goal_reason", "ttl_left_sec", "budget_left",
                "iterations", "max_iterations", "ttl_sec"):  # fmt: skip
        assert key not in out
    assert out["iteration"] == {"run": "other"}
    assert out["cards"] == 0 and out["card"] is False
    assert out["outcomes"] == [{"what": "other", "tools": ["other", "editor"] + ["other"] * 6}]
    assert MARKER not in text


def test_awaiting_verification_abandoned_and_the_card_flag():
    out = activity.grant_view(
        _view(goal_state="awaiting_verification",
              headline={"subject": "goal", "state": "awaiting_verification", "reason": ""}),
        _board(iteration={"state": "idle", "abandoned": True, "card": "waiting", "end": "stopped",
                          "detail": "ttl", "number": 4}, outcomes=[]),
    )  # fmt: skip
    assert out["awaiting_verification"] is True
    assert out["headline"] == {"subject": "goal", "state": "awaiting_verification"}
    assert out["iteration"] == {"run": "abandoned", "end": "stopped", "detail": "ttl", "number": 4}
    assert out["card"] is True and out["cards"] == 0 and out["outcomes"] == []
    yielded = activity.grant_view(_view(), _board(iteration={"state": "yielded"}))
    assert yielded["iteration"] == {"run": "yielded"}
    assert activity.grant_view(_view(), _board(iteration="x"))["iteration"] == {"run": "other"}
    assert activity.grant_view(_view(), _board(outcomes="x"))["outcomes"] == []


def test_only_the_last_outcomes_are_kept():
    rows = [{"end": "done", "number": n, "tools": []} for n in range(1, 12)]
    out = activity.grant_view(_view(), _board(outcomes=rows))
    assert [r["number"] for r in out["outcomes"]] == list(range(12 - activity.GRANT_OUTCOMES, 12))
    assert activity.GRANT_OUTCOMES == 5


# --- in the server ----------------------------------------------------------------------------


@pytest.fixture
def stage_c_exec(tmp_path, monkeypatch):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    monkeypatch.setenv("STIRA_STAGE", "1")
    build, data = _build(tmp_path, monkeypatch, EXEC)
    app = build()
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    ws = (roots[0] if roots else tmp_path) / "grant-ws"
    ws.mkdir(parents=True, exist_ok=True)
    yield app, data, ws
    _close(app)


def test_on_without_a_grant_the_block_is_null_and_the_page_says_exec(stage_c_exec):
    app, _data, _ws = stage_c_exec
    client = app.test_client()
    body = client.get("/api/activity").get_json()
    assert "grant" in body and body["grant"] is None
    page = client.get("/stage/")
    assert page.status_code == 200 and 'data-grants="exec"' in page.get_data(as_text=True)


def test_stage_c_alone_shows_the_grant_without_an_iteration(tmp_path, monkeypatch):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    monkeypatch.setenv("STIRA_STAGE", "1")
    gateway = {"autonomy": {"stage_c": True, "allow_browser_confirm": True}}
    build, _data = _build(tmp_path, monkeypatch, gateway)
    app = build()
    try:
        policy = app.extensions["grants"]._policy()
        roots = list(getattr(policy, "write_roots", None) or [])
        ws = (roots[0] if roots else tmp_path) / "grant-ws"
        ws.mkdir(parents=True, exist_ok=True)
        client = app.test_client()
        page = client.get("/stage/")
        assert page.status_code == 200
        # no executor: nothing runs, nothing to watch
        assert "data-grants" not in page.get_data(as_text=True)
        _issue(client, ws, goal=f"tidy {MARKER} docs")
        body = client.get("/api/activity").get_json()
        grant = body["grant"]
        assert grant["state"] == "active" and grant["live"] is True
        assert "iteration" not in grant and grant["budget_left"] == 1000
        assert MARKER not in json.dumps(body) and str(ws) not in json.dumps(body)
    finally:
        _close(app)


def test_the_marker_of_the_goal_and_of_the_model_is_never_on_the_board(stage_c_exec):
    from grants_exec_helpers import Counter, ScriptedProvider, use

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    provider = ScriptedProvider(
        [
            [{"text": f"I will echo {MARKER}"},
             use("file_read", {"path": str(ws / f"{MARKER}.txt")})],
            [{"text": MARKER}, use("goal_report", {"kind": "progress", "code": "step_done"}, 2)],
        ]
    )  # fmt: skip
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws, goal=f"write {MARKER} into the notes")["grant"]["grant_id"]
    assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
    assert executor.wait_idle(20)
    assert MARKER in json.dumps(provider.seen)  # the model saw the goal (and echoed it)
    body = client.get("/api/activity").get_json()
    reload = client.get(f"/api/activity?after_id={body['cursor'] - 1 if body['cursor'] else 0}")
    for answer in (body, reload.get_json()):
        text = json.dumps(answer)
        assert MARKER not in text and str(ws) not in text
    grant = body["grant"]
    assert grant["id"] == grant_id[:8] and grant["iterations"] == 1
    assert grant["iteration"]["run"] == "idle" and grant["iteration"]["number"] == 1
    assert len(grant["outcomes"]) == 1
    outcome = grant["outcomes"][0]
    assert outcome["number"] == 1 and outcome["what"] == grant["iteration"]["end"]
    assert type(outcome["duration_ms"]) is int and type(outcome["tick_used"]) is int
    assert set(outcome["tools"]) <= {"editor", "file_read", "goal_report", "other"}


def test_a_running_iteration_is_read_from_memory_only(stage_c_exec, monkeypatch):
    from grants_exec_helpers import Counter, ScriptedProvider, use

    from stiracore.grants.service import GrantService

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    hold, started = threading.Event(), threading.Event()
    provider = ScriptedProvider([[use("goal_report", {"kind": "progress", "code": "step_done"})]])
    provider.on_call = lambda n: (started.set(), hold.wait(10))
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    try:
        assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
        assert started.wait(10)

        def no_look(*_a, **_kw):
            raise AssertionError("the board read looked, synced or wrote")

        for name in ("sync", "check_now", "_look", "_request_write", "_apply"):
            monkeypatch.setattr(GrantService, name, no_look)
        body = client.get("/api/activity").get_json()
        grant = body["grant"]
        assert "error" not in grant, grant  # no look, no sync, no write was needed
        assert grant["iteration"]["run"] == "running" and grant["iteration"]["number"] == 1
        assert type(grant["iteration"].get("tick_used")) is int and grant["outcomes"] == []
        monkeypatch.undo()
    finally:
        hold.set()
    assert executor.wait_idle(20)


def test_a_failed_read_is_one_fixed_code_never_a_500(stage_c_exec, monkeypatch):
    app, _data, ws = stage_c_exec
    client = app.test_client()
    _issue(client, ws)

    def broken(*_a, **_kw):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(app.extensions["grants"], "board_view", broken)
    response = client.get("/api/activity")
    assert response.status_code == 200
    body = response.get_json()
    assert body["grant"] == {"error": "grant_unreadable"} and body["state"]["code"] == "idle"
    assert MARKER not in response.get_data(as_text=True)
    monkeypatch.setattr(app.extensions["grant_executor"], "board", broken)
    monkeypatch.undo()
    monkeypatch.setattr(app.extensions["grant_executor"], "board", broken)
    assert client.get("/api/activity").get_json()["grant"] == {"error": "grant_unreadable"}


def test_the_executor_keeps_five_outcomes_at_most():
    from stiracore.grants import executor as ex

    assert ex.OUTCOMES_KEPT == 5


def _get_while_held(app, lock):
    """GET /api/activity while another thread holds ``lock``: (seconds, body)."""
    got, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            got.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert got.wait(5)
    try:
        start = time.monotonic()
        body = app.test_client().get("/api/activity").get_json()
        return time.monotonic() - start, body
    finally:
        release.set()
        holder.join(5)


def test_a_held_service_lock_never_holds_the_activity_answer(stage_c_exec):
    app, _data, ws = stage_c_exec
    _issue(app.test_client(), ws)
    took, body = _get_while_held(app, app.extensions["grants"]._lock)
    assert took < 0.4, took
    assert body["grant"] == {"error": "grant_busy"} and body["state"]["code"] == "idle"
    assert app.test_client().get("/api/activity").get_json()["grant"]["live"] is True


def test_a_held_iteration_lock_never_holds_the_activity_answer(stage_c_exec):
    from grants_exec_helpers import Counter, ScriptedProvider

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    provider = ScriptedProvider([[{"text": "done"}]])
    hold, started = threading.Event(), threading.Event()
    provider.on_call = lambda n: (started.set(), hold.wait(10))
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    try:
        assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
        assert started.wait(10)
        for lock in (executor._lock, executor._current.lock, executor._current.budget._lock):
            took, body = _get_while_held(app, lock)
            assert took < 0.4, took
            assert body["grant"] == {"error": "grant_busy"}
        assert client.get("/api/activity").get_json()["grant"]["iteration"]["run"] == "running"
    finally:
        hold.set()
    assert executor.wait_idle(20)


def test_a_code_shaped_marker_in_every_slot_is_never_in_the_block():
    """W5b fix round 1 (B5): the block is an allow-list of values, not of shapes."""
    m = MARKER
    assert activity._CODE.fullmatch(m)  # it has the shape of a code
    hostile = _view(
        state=m,
        reason=m,
        goal_state=m,
        goal_reason=m,
        headline={"subject": "goal", "state": m, "reason": m},
    )
    board = _board(
        iteration={"state": "idle", "end": m, "detail": m, "number": 1},
        outcomes=[{"end": m, "detail": m, "tools": [m, "file_read"], "number": 1}],
    )  # fmt: skip
    out = activity.grant_view(hostile, board)
    assert m not in json.dumps(out)
    assert out["state"] == "other" and out["live"] is False
    assert out["headline"] == {"subject": "goal", "state": "other"}
    assert "reason" not in out and "goal_state" not in out and "goal_reason" not in out
    assert out["iteration"] == {"run": "idle", "number": 1}
    assert out["outcomes"] == [{"what": "other", "number": 1, "tools": ["other", "file_read"]}]
    grant_line = activity.grant_view(_view(headline={"subject": "grant", "state": "revoked",
                                                     "reason": "verified"}))  # fmt: skip
    assert grant_line["headline"] == {"subject": "grant", "state": "revoked"}  # a goal reason


def test_every_known_code_passes():
    from stiracore.grants import executor, schema, state

    for status, reasons in state.GRANT_REASONS.items():
        for reason in reasons - {""}:
            out = activity.grant_view(_view(state=status, reason=reason,
                                            headline={"subject": "grant", "state": status,
                                                      "reason": reason}))  # fmt: skip
            assert out["reason"] == reason and out["headline"]["reason"] == reason
    for status, reasons in state.GOAL_REASONS.items():
        for reason in reasons - {""}:
            out = activity.grant_view(_view(goal_state=status, goal_reason=reason))
            assert out["goal_state"] == status and out["goal_reason"] == reason
    ends = sorted(executor.END_CODES)
    details = sorted(executor.DETAIL_CODES - {""})
    rows = [{"end": e, "detail": details[n % len(details)], "tools": sorted(schema.TOOLS)}
            for n, e in enumerate(ends)]  # fmt: skip
    out = activity.grant_view(_view(), _board(outcomes=rows))
    assert [r["what"] for r in out["outcomes"]] == ends[-5:]
    assert all(r["tools"] == sorted(schema.TOOLS) for r in out["outcomes"])


def test_the_pages_code_lists_are_the_servers():
    """activity.js keeps the same fixed sets for what it draws (a reason, a detail, a tool)."""
    import re
    from pathlib import Path

    from stiracore.grants import executor, schema, state

    src = (Path(__file__).resolve().parents[1] / "dashboard/stage/js/activity.js").read_text()

    def js(name):
        body = re.search(rf"const {name} = new Set\(\[([^\]]*)\]\)", src).group(1)
        return set(re.findall(r"'([a-z_]+)'", body))

    grant = set().union(*state.GRANT_REASONS.values()) - {""}
    goal = set().union(*state.GOAL_REASONS.values()) - {""}
    assert js("GRANT_REASONS") == grant
    assert js("GOAL_REASONS") == goal
    assert js("DETAIL_CODES") == set(executor.DETAIL_CODES) - {""}
    assert js("SCOPE_TOOLS") == set(schema.TOOLS)
    end_keys = re.search(r"const END_KEY = \{([^}]*)\}", src).group(1)
    assert set(re.findall(r"([a-z_]+): 'stage\.", end_keys)) == set(executor.END_CODES)
    for name, values in (("GRANT_STATE_KEY", state.GRANT_STATUSES),
                         ("GOAL_STATE_KEY", state.GOAL_STATUSES)):  # fmt: skip
        keys = re.search(rf"const {name} = \{{([^}}]*)\}}", src).group(1)
        assert set(re.findall(r"([a-z_]+): 'stage\.", keys)) == set(values), name
    # W6c: the claim evidence's codes, on the board and in the settings panel
    panel = Path(__file__).resolve().parents[1] / "dashboard/static/js/settings_grants.js"
    panel = panel.read_text()

    def js_list(name):
        body = re.search(rf"const {name} = \[([^\]]*)\]", panel).group(1)
        return set(re.findall(r'"([a-z_]+)"', body))

    claim_sets = (
        ("CLAIM_CHECKS", set(schema.CLAIM_CHECKS)),
        ("CLAIM_WORKSPACE", set(schema.CLAIM_WORKSPACE)),
        ("CLAIM_PROGRESS", set(schema.CLAIM_PROGRESS)),
        ("CHECK_STATUSES", set(schema.CHECK_STATUSES)),
    )
    for name, values in claim_sets:
        assert js(name) == values, name
        assert js_list(name) == values, name
    assert js("CHECK_KINDS") == set(schema.CHECK_KINDS) | {"other"}
    assert js_list("CHECK_KINDS") == set(schema.CHECK_KINDS)
    counts = re.search(r"const CHECK_COUNT_KEYS = \[([^\]]*)\]", src).group(1)
    assert re.findall(r"'([a-z_]+)'", counts) == list(schema.CHECK_COUNT_KEYS)
    assert js_list("CHECK_COUNT_KEYS") == set(schema.CHECK_COUNT_KEYS)


def test_the_route_never_publishes_a_mixed_version(stage_c_exec, monkeypatch):
    """Fix round 2: an iteration ending while the board reads -> grant_busy; the next read
    is the new version (the iteration counted, the budget charged, its row listed)."""
    from grants_exec_helpers import Counter, ScriptedProvider

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    hold, started = threading.Event(), threading.Event()
    provider = ScriptedProvider([[{"text": "done"}]])
    provider.on_call = lambda n: (started.set(), hold.wait(10))
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
    assert started.wait(10)
    it = executor._current
    original = it.try_summary

    def the_iteration_ends_here():
        hold.set()
        assert executor.wait_idle(10)
        return original()

    monkeypatch.setattr(it, "try_summary", the_iteration_ends_here)
    first = client.get("/api/activity").get_json()["grant"]
    second = client.get("/api/activity").get_json()["grant"]
    assert first == {"error": "grant_busy"}, first
    assert second["iterations"] == 1 and len(second["outcomes"]) == 1
    assert second["budget_left"] == 1000 - second["outcomes"][0]["tick_used"]
    assert second["iteration"]["number"] == 1 and second["iteration"]["run"] == "idle"


def test_a_grant_change_between_the_pieces_is_busy(stage_c_exec, monkeypatch):
    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    grants = app.extensions["grants"]
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    board = executor.board

    def read_while_paused(gid):
        out = board(gid)
        grants.pause(grant_id)  # the record moves between the two reads of it
        return out

    monkeypatch.setattr(executor, "board", read_while_paused)
    assert client.get("/api/activity").get_json()["grant"] == {"error": "grant_busy"}
    monkeypatch.undo()
    assert client.get("/api/activity").get_json()["grant"]["state"] == "paused"
