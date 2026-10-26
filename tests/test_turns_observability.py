"""/observability reads turns.db, the chat server's one source of truth for turn events (#286).

- ``page_event`` maps every turn-contract event type to the page's event contract
- the real ``stira_server`` (offline FakeModel): the snapshot and the SSE stream hold
  exactly what ``/api/turns`` and ``/api/turns/<id>/events`` (turns.db) hold
- no route is registered twice, in the chat server or in the observer app
- the real process (fake Bedrock endpoint), with an approval: the snapshot counts agree
  with a direct read of turns.db
"""

from __future__ import annotations

import http.client
import json
import sqlite3
from collections import Counter

import pytest

pytest.importorskip("flask")

from stiracore.core import NeuralState
from stiracore.observability.turn_events import TurnEventStore
from stiracore.observability.turns_view import page_event
from tests.provider_fakes import FakeModel, text_turn, tool_turn


def _event(kind: str, data: dict, turn_id: str = "t1", seq: int = 1) -> dict:
    return {
        "schema_version": 1,
        "event_id": f"e{seq}",
        "turn_id": turn_id,
        "seq": seq,
        "ts": 1_700_000_000.5,
        "type": kind,
        "data": data,
    }


# --- the mapping ---------------------------------------------------------------------


def test_turn_completed_carries_tokens_and_model_for_the_cost_panel():
    out = page_event(
        7,
        _event(
            "turn.completed",
            {
                "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 5},
                "stop": "completed",
                "error": False,
                "model": "m-1",
            },
        ),
    )
    assert out["id"] == "7" and out["type"] == "turn_end" and out["turn_id"] == "t1"
    assert out["usage"] == {"is_final": True, "input_tokens": 120, "output_tokens": 30}
    assert out["usage_id"] == "t1:turn" and out["model"] == "m-1"
    assert out["status"] == "success" and out["severity"] == "info"
    assert out["message"] == "completed"


def test_turn_completed_without_counts_has_no_usage_and_an_error_is_marked():
    out = page_event(
        1,
        _event(
            "turn.completed",
            {
                "usage": {"input_tokens": None, "output_tokens": None},
                "stop": "error",
                "error": True,
            },
        ),
    )
    assert "usage" not in out and "model" not in out
    assert out["status"] == "error" and out["severity"] == "error" and out["message"] == "error"
    resumed = page_event(
        2, _event("turn.completed", {"usage": {}, "stop": "resumed", "error": "OSError"})
    )
    assert resumed["message"] == "resumed: OSError"


def test_tool_call_events_become_a_span_the_timeline_can_draw():
    started = page_event(
        1,
        _event(
            "tool_call.created",
            {"tool_use_id": "u1", "name": "file_write", "status": "running", "started_at": 10.0},
        ),
    )
    assert (started["type"], started["tool"], started["span_id"]) == (
        "tool_start",
        "file_write",
        "u1",
    )
    assert started["start_time"] == 10.0 and "duration_ms" not in started
    done = page_event(
        2,
        _event(
            "tool_call.updated",
            {
                "tool_use_id": "u1",
                "name": "file_write",
                "status": "done",
                "started_at": 10.0,
                "duration_ms": 250,
            },
        ),
    )
    assert done["type"] == "tool_end" and done["span_id"] == "u1"
    assert (done["status"], done["severity"], done["duration_ms"]) == ("success", "info", 250)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("error", ("error", "error")),
        ("timeout", ("error", "error")),
        ("denied", ("denied", "warning")),
    ],
)
def test_failed_and_denied_tool_calls_are_not_reported_as_success(status, expected):
    out = page_event(
        1, _event("tool_call.updated", {"tool_use_id": "u", "name": "x", "status": status})
    )
    assert (out["status"], out["severity"]) == expected


@pytest.mark.parametrize("code", ["E_USAGE_NOT_REPORTED", "E_USAGE_CANCELLED"])
def test_a_turn_without_counts_shows_its_reason_code_beside_the_model_and_stop(code):
    # #363 (usage_reason) and #393 (model) both live on turn.completed and do not collide.
    out = page_event(
        1,
        _event(
            "turn.completed",
            {
                "usage": {"input_tokens": None, "output_tokens": None},
                "usage_reason": code,
                "stop": "max_tokens",
                "error": True,
                "model": "m-1",
            },
        ),
    )
    assert out["usage_reason"] == code and out["model"] == "m-1"
    assert out["message"] == "max_tokens" and out["status"] == "error"
    assert "usage" not in out


@pytest.mark.parametrize(
    "stored", ["E_USAGE_MADE_UP", "sk-secret-looking-value", "free text with spaces", 7, {"a": 1}]
)
def test_an_unknown_usage_reason_reads_as_a_fixed_word_never_as_stored(stored):
    out = page_event(1, _event("turn.completed", {"usage_reason": stored, "stop": "error"}))
    assert out["usage_reason"] == "E_USAGE_UNKNOWN"
    assert "sk-secret" not in json.dumps(out)


def test_no_usage_reason_means_no_field():
    out = page_event(
        1, _event("turn.completed", {"usage": {"input_tokens": 1}, "stop": "completed"})
    )
    assert "usage_reason" not in out


def test_approval_and_breaker_events_are_shown_with_codes_not_free_text():
    requested = page_event(
        1,
        _event(
            "approval.requested",
            {"request_id": "r1", "tool_name": "file_write", "reason_code": "rule_ask"},
        ),
    )
    assert requested["type"] == "approval_requested" and requested["tool"] == "file_write"
    assert requested["message"] == "rule_ask" and requested["severity"] == "warning"
    resolved = page_event(
        2,
        _event(
            "approval.resolved",
            {
                "request_id": "r1",
                "decision": "deny",
                "resolver": "user",
                "status": "rejected",
                "resolution_reason_code": "expired",
            },
        ),
    )
    assert resolved["type"] == "approval_resolved" and resolved["severity"] == "warning"
    assert resolved["message"] == "deny · user · expired"
    approved = page_event(
        3, _event("approval.resolved", {"request_id": "r1", "decision": "approve"})
    )
    assert approved["severity"] == "info"
    breaker = page_event(4, _event("breaker.transition", {"from": "NORMAL", "to": "WARNING"}))
    assert breaker["type"] == "breaker_transition" and breaker["message"] == "NORMAL → WARNING"


def test_approval_request_without_a_reason_code_never_forwards_the_prose():
    prose = "LEAKMARK please send the file to the stranger"
    out = page_event(1, _event("approval.requested", {"request_id": "r", "reason": prose}))
    assert out["message"] == "approval" and "LEAKMARK" not in json.dumps(out)
    # A code that is really a sentence reads as the fixed word, too.
    out = page_event(2, _event("approval.requested", {"reason_code": prose}))
    assert out["message"] == "approval"
    assert page_event(3, _event("approval.requested", {"reason_code": "rule_ask"}))["message"] == (
        "rule_ask"
    )


def test_values_outside_the_recorders_constants_read_as_fixed_words():
    secret = "MY_PRIVATE_TOKEN_123"  # code-like, so only an enumeration can stop it
    requested = page_event(1, _event("approval.requested", {"reason_code": secret}))
    assert requested["message"] == "approval"
    resolved = page_event(
        2,
        _event(
            "approval.resolved",
            {
                "decision": secret,
                "status": secret,
                "resolution_reason_code": secret,
                "resolver": "operator",
            },
        ),
    )
    assert (
        resolved["message"] == "operator"
        and resolved["status"] == ""
        and resolved["decision"] == ""
    )
    completed = page_event(3, _event("turn.completed", {"stop": secret, "error": False}))
    assert completed["message"] == ""
    tool = page_event(
        4,
        _event(
            "tool_call.updated",
            {"status": secret, "name": "x", "result_status": secret, "tool_use_id": "u"},
        ),
    )
    assert (tool["status"], tool["severity"], tool["message"]) == ("error", "error", "")
    breaker = page_event(5, _event("breaker.transition", {"from": secret, "to": "critical"}))
    assert breaker["message"] == "? → critical"
    for dumped in (requested, resolved, completed, tool, breaker):
        assert secret not in json.dumps(dumped)
    # The recorder's own codes pass.
    from stiracore.core import hitl_reasons

    for code in hitl_reasons.REASON_CODES:
        assert page_event(6, _event("approval.requested", {"reason_code": code}))["message"] == code
    for code in hitl_reasons.RESOLUTION_CODES:
        out = page_event(7, _event("approval.resolved", {"resolution_reason_code": code}))
        assert out["message"] == code


def test_an_unknown_event_type_is_a_fixed_type_and_carries_no_data():
    out = page_event(1, _event("MY_PRIVATE_TOKEN_123", {"message": "x", "name": "y"}))
    assert out["type"] == "event" and out["message"] == "" and "tool" not in out
    assert "MY_PRIVATE" not in json.dumps(out)


def test_non_finite_and_out_of_range_numbers_are_left_out_not_raised():
    inf, nan = float("inf"), float("nan")
    event = _event(
        "tool_call.updated",
        {
            "tool_use_id": "u",
            "name": "x",
            "status": "done",
            "started_at": nan,
            "duration_ms": inf,
        },
    )
    event["ts"], event["seq"] = inf, 10**400
    out = page_event(1, event)
    assert not {"timestamp", "seq", "start_time", "duration_ms"} & set(out)
    completed = _event(
        "turn.completed",
        {"usage": {"input_tokens": inf, "output_tokens": 10**400}, "stop": "completed"},
    )
    completed["ts"] = nan
    assert "usage" not in page_event(2, completed)
    summary = page_event(3, _event("turn.summary", {"category": "joy", "intensity": nan}))
    assert summary["message"] == "joy"
    for value in (inf, -inf, nan, -1, 2**60, 1e300, True, "5", None):
        started = page_event(
            4, _event("tool_call.created", {"tool_use_id": "u", "name": "x", "started_at": value})
        )
        assert "start_time" not in started, value
    assert page_event(5, _event("tool_call.created", {"started_at": 12.5}))["start_time"] == 12.5
    # Whatever remains is strict JSON (the stream uses allow_nan=False).
    json.dumps(out, allow_nan=False)


def test_previews_and_bodies_are_never_forwarded():
    out = page_event(
        1,
        _event(
            "tool_call.updated",
            {
                "tool_use_id": "u",
                "name": "x",
                "status": "done",
                "input_preview": "SECRET-IN",
                "output_preview": "SECRET-OUT",
            },
        ),
    )
    assert "SECRET" not in json.dumps(out)


def test_an_unknown_event_type_and_malformed_data_do_not_raise():
    assert page_event(1, _event("memory.injected", {}))["type"] == "memory_injected"
    assert page_event(2, {"type": "turn.started", "data": "oops"})["type"] == "turn_started"


# --- free text in a real turns.db never leaves ---------------------------------------


LEAK = "LEAKMARK"
PROSE = f"{LEAK} write this sentence down"


def _seed_free_text(data_dir) -> None:
    """Events whose every text field holds prose, written through the real store."""
    store = TurnEventStore(data_dir / "turns.db")
    turn = "f" * 32
    store.append(turn, "turn.started", {"prompt_chars": 5, "note": PROSE})
    store.append(
        turn,
        "approval.requested",
        {
            "request_id": "r1",
            "tool_name": PROSE,
            "reason": PROSE,  # no reason_code at all
        },
    )
    store.append(
        turn,
        "approval.requested",
        {"request_id": "r2", "tool_name": "file_write", "reason": PROSE, "reason_code": PROSE},
    )
    store.append(
        turn,
        "approval.resolved",
        {
            "request_id": "r1",
            "decision": "deny",
            "resolver": PROSE,
            "status": PROSE,
            "resolution_reason": PROSE,
            "resolution_reason_code": PROSE,
        },
    )
    store.append(
        turn,
        "tool_call.created",
        {"tool_use_id": "u1", "name": "file_write", "status": "running", "input_preview": PROSE},
    )
    store.append(
        turn,
        "tool_call.updated",
        {
            "tool_use_id": "u1",
            "name": "file_write",
            "status": "error",
            "output_preview": PROSE,
            "exception_type": PROSE,
            "result_status": PROSE,
        },
    )
    store.append(turn, "breaker.transition", {"from": PROSE, "to": "WARNING"})
    store.append(
        turn,
        "turn.summary",
        {
            "category": PROSE,
            "intensity": 0.5,
            "emotion_transcription": PROSE,
            "classifier": PROSE,
            "deltas": {"trust": 0.1},
            "before": {"trust": 0.5},
            "after": {"trust": 0.6},
        },
    )
    store.append(
        turn,
        "turn.completed",
        {
            "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_tokens": 3, "x": PROSE},
            "stop": PROSE,
            "error": PROSE,
            "model": PROSE,
            "prompt_cache": {"changed": PROSE},
        },
    )
    store.close()


def test_free_text_in_turns_db_reaches_neither_the_snapshot_nor_the_stream(stira, tmp_path):
    _seed_free_text(tmp_path / "data")
    client = stira.test_client()
    raw_snapshot = client.get("/api/observability/snapshot").get_data(as_text=True)
    snapshot = json.loads(raw_snapshot)
    assert snapshot["event_count"] == 9 and snapshot["turn_count"] == 1
    assert LEAK not in raw_snapshot

    response = client.get("/api/observability/events")
    raw_stream = ""
    for chunk in response.response:
        raw_stream += chunk.decode() if isinstance(chunk, bytes) else chunk
        if raw_stream.count("\ndata: ") >= 9:
            break
    response.close()
    assert raw_stream.count("\ndata: ") == 9 and LEAK not in raw_stream
    # The code-like fields that are fine still arrive: the tokens and the fixed words.
    end = next(e for e in snapshot["events"] if e["type"] == "turn_end")
    assert end["usage"]["input_tokens"] == 11 and "model" not in end
    requested = [e for e in snapshot["events"] if e["type"] == "approval_requested"]
    assert [e["message"] for e in requested] == ["approval", "approval"]
    # The page's own endpoint for the same data (previews, notes) is not the issue here:
    # the stored events do hold the prose, so the checks above are not vacuous.
    assert LEAK in client.get("/api/turns/" + "f" * 32 + "/events").get_data(as_text=True)


def test_non_finite_stored_numbers_do_not_break_the_snapshot_or_the_stream(stira, tmp_path):
    """A row written by something else (NaN / Infinity are not strict JSON): shown without them."""
    store = TurnEventStore(tmp_path / "data" / "turns.db")
    store.append("c" * 32, "turn.started", {})
    store.close()
    connection = sqlite3.connect(tmp_path / "data" / "turns.db")
    bad = (
        '{"schema_version":1,"event_id":"e2","turn_id":"%s","seq":2,"ts":Infinity,'
        '"type":"tool_call.updated","data":{"tool_use_id":"u","name":"x","status":"done",'
        '"started_at":NaN,"duration_ms":Infinity}}' % ("c" * 32)
    )
    connection.execute(
        "INSERT INTO ui_events (turn_id, seq, type, ts, payload) VALUES (?, 2, ?, 0, ?)",
        ("c" * 32, "tool_call.updated", bad),
    )
    connection.commit()
    connection.close()
    client = stira.test_client()

    def strict(text):
        return json.loads(text, parse_constant=lambda name: pytest.fail(name))

    snapshot = strict(client.get("/api/observability/snapshot").get_data(as_text=True))
    assert snapshot["event_count"] == 2
    response = client.get("/api/observability/events")
    raw = ""
    for chunk in response.response:
        raw += chunk.decode() if isinstance(chunk, bytes) else chunk
        if raw.count("\ndata: ") >= 2:
            break
    response.close()
    rows = [block for block in raw.split("\n\n") if "data: " in block]
    assert len(rows) == 2
    last = strict(rows[-1].split("data: ", 1)[1])
    assert last["type"] == "tool_end" and "duration_ms" not in last and "start_time" not in last


def test_snapshot_is_one_point_in_time_while_a_turn_is_being_recorded(tmp_path):
    import threading

    store = TurnEventStore(tmp_path / "turns.db")
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            turn = f"{n:032x}"
            store.append(turn, "turn.started", {})
            store.append(turn, "turn.completed", {"usage": {}, "stop": "completed"})

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(150):
            taken = store.snapshot(50, 20)
            counts, tail, turns = taken["counts"], taken["tail"], taken["turns"]
            if tail:
                assert tail[-1][0] == counts["latest_id"]
            assert counts["event_count"] == counts["latest_id"]  # nothing is ever deleted here
            # The newest turn in the list is the turn of the newest event in the tail.
            if turns:
                assert turns[0]["turn_id"] == tail[-1][1]["turn_id"]
    finally:
        stop.set()
        thread.join()
        store.close()


def test_snapshot_turns_are_projected_to_an_allow_list(stira, tmp_path):
    _seed_free_text(tmp_path / "data")
    (turn,) = stira.test_client().get("/api/observability/snapshot").json["turns"]
    assert set(turn) == {
        "turn_id",
        "started_at",
        "ended_at",
        "completed",
        "error",
        "tool_count",
        "stop",
        "usage_reason",
        "usage",
        "category",
        "intensity",
    }
    assert turn["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert turn["error"] == "error" and turn["category"] == "" and turn["tool_count"] == 1
    assert turn["completed"] is True and turn["intensity"] == 0.5


# --- the store -----------------------------------------------------------------------


def test_store_counts_tail_and_cursor_reads(tmp_path):
    store = TurnEventStore(tmp_path / "turns.db")
    assert store.counts() == {"event_count": 0, "turn_count": 0, "latest_id": 0}
    for turn in ("a", "b"):
        for kind in ("turn.started", "turn.completed"):
            store.append(turn, kind, {})
    assert store.counts() == {"event_count": 4, "turn_count": 2, "latest_id": 4}
    assert [row_id for row_id, _ in store.events_after(0, 100)] == [1, 2, 3, 4]
    assert [row_id for row_id, _ in store.events_after(2, 100)] == [3, 4]
    assert [row_id for row_id, _ in store.events_after(0, 2)] == [1, 2]
    assert [row_id for row_id, _ in store.tail(3)] == [2, 3, 4]
    store.close()


# --- the real stira_server, offline --------------------------------------------------


def _csrf(app) -> dict:
    return {
        "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
        "Origin": "http://localhost",
    }


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server

    model = FakeModel(
        [
            text_turn("one", usage={"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}),
            tool_turn("memory_read", {"layer": "semantic", "key": "none"}, "use-1"),
            text_turn("two", usage={"inputTokens": 200, "outputTokens": 20, "totalTokens": 220}),
            text_turn("three", usage={"inputTokens": 300, "outputTokens": 30, "totalTokens": 330}),
        ],
        model_id="fake-model-1",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(stira_server, "model_kwargs", lambda *a, **k: {"model": model})
    return stira_server.create_app()


def _chat(client, app, text: str, key: str) -> None:
    response = client.post(
        "/api/chat", json={"message": text}, headers={**_csrf(app), "Idempotency-Key": key}
    )
    assert response.status_code == 200
    response.get_data()


def _sse_rows(response, wanted: int) -> list[tuple[str, dict]]:
    """The first ``wanted`` data events of a streamed response; closes the stream."""
    rows: list[tuple[str, dict]] = []
    pending = ""
    for chunk in response.response:
        pending += chunk.decode() if isinstance(chunk, bytes) else chunk
        while "\n\n" in pending:
            block, pending = pending.split("\n\n", 1)
            fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "data" in fields:
                rows.append((fields["id"], json.loads(fields["data"])))
        if len(rows) >= wanted:
            break
    response.close()
    return rows[:wanted]


def test_snapshot_and_stream_hold_what_turns_db_holds(stira):
    client = stira.test_client()
    empty = client.get("/api/observability/snapshot").json
    assert (empty["event_count"], empty["turn_count"], empty["events"]) == (0, 0, [])
    assert empty["source"] == "turns.db" and empty["latest_event_id"] is None

    _chat(client, stira, "hello", "k1")
    _chat(client, stira, "read", "k2")
    _chat(client, stira, "again", "k3")

    turns = client.get("/api/turns").json["turns"]
    per_turn = {
        turn["turn_id"]: client.get(f"/api/turns/{turn['turn_id']}/events").json["events"]
        for turn in turns
    }
    total = sum(len(events) for events in per_turn.values())
    assert len(turns) == 3 and total >= 9

    snap = client.get("/api/observability/snapshot").json
    assert snap["turn_count"] == 3 and snap["event_count"] == total
    assert snap["latest_event_id"] == str(total)
    assert {t["turn_id"] for t in snap["turns"]} == set(per_turn)
    assert len(snap["events"]) == min(total, 50)
    assert snap["events"][-1]["id"] == snap["latest_event_id"]

    rows = _sse_rows(client.get("/api/observability/events"), total)
    assert [row_id for row_id, _ in rows] == [str(n) for n in range(1, total + 1)]
    assert all(payload["id"] == row_id for row_id, payload in rows)
    kinds = Counter(payload["type"] for _, payload in rows)
    assert kinds["turn_started"] == 3 and kinds["turn_end"] == 3
    assert kinds["tool_start"] >= 1 and kinds["tool_end"] >= 1
    # The cost panel input: the tokens turns.db holds, per turn, with the model.
    ends = [payload for _, payload in rows if payload["type"] == "turn_end"]
    stored = {turn["turn_id"]: turn["usage"] for turn in turns if turn["usage"] is not None}
    assert len(stored) == 3
    for end in ends:
        assert end["usage"]["input_tokens"] == stored[end["turn_id"]]["input_tokens"]
        assert end["usage"]["output_tokens"] == stored[end["turn_id"]]["output_tokens"]
        assert end["model"] == "fake-model-1"
    assert sum(end["usage"]["input_tokens"] for end in ends) == sum(
        usage["input_tokens"] for usage in stored.values()
    )


def test_the_stream_resumes_after_a_cursor_and_refuses_a_bad_one(stira):
    client = stira.test_client()
    _chat(client, stira, "hello", "k1")
    total = client.get("/api/observability/snapshot").json["event_count"]
    assert total >= 3
    after = _sse_rows(
        client.get("/api/observability/events", headers={"Last-Event-ID": str(total - 2)}), 2
    )
    assert [row_id for row_id, _ in after] == [str(total - 1), str(total)]
    via_query = _sse_rows(client.get(f"/api/observability/events?since_id={total - 1}"), 1)
    assert [row_id for row_id, _ in via_query] == [str(total)]
    for bad in ("abc", "-1", "1.5", "１"):
        assert client.get(f"/api/observability/events?last_id={bad}").status_code == 400


@pytest.mark.parametrize(
    "cursor",
    [
        "9223372036854775808",
        "99999999999999999999",
        "00000000000000000001",
        "abc",
        "-1",
        "1.5",
        "１",
    ],
)
def test_a_cursor_outside_a_sqlite_integer_is_refused_before_the_stream_starts(stira, cursor):
    response = stira.test_client().get(f"/api/observability/events?last_id={cursor}")
    assert response.status_code == 400
    assert response.mimetype == "application/json" and ": connected" not in response.get_data(
        as_text=True
    )


def test_the_largest_cursor_is_accepted(stira):
    response = stira.test_client().get("/api/observability/events?last_id=9223372036854775807")
    assert response.status_code == 200 and response.mimetype == "text/event-stream"
    assert next(iter(response.response)) in (b": connected\n\n", ": connected\n\n")
    response.close()


FOREIGN = (
    {"Origin": "https://evil.example"},
    {"Origin": "http://localhost:9999"},  # same host, another port: another origin
    {"Origin": "null"},
    {"Origin": "http://[::1"},
    {"Sec-Fetch-Site": "cross-site"},
)
OBSERVABILITY_READS = ("/api/observability/snapshot", "/api/observability/events")


@pytest.mark.parametrize("path", OBSERVABILITY_READS)
def test_foreign_origins_and_hosts_are_refused_before_a_stream_starts(stira, path):
    client = stira.test_client()
    for headers in FOREIGN:
        response = client.get(path, headers=headers)
        assert response.status_code == 403, headers
        assert response.mimetype == "application/json"
        assert response.json == {"error": "Cross-origin API reads are not allowed."}
        assert "Access-Control-Allow-Origin" not in response.headers
        assert ": connected" not in response.get_data(as_text=True)
    for host in ("evil.example", "localhost.evil.example"):
        response = client.get(path, headers={"Host": host})
        assert response.status_code == 403 and response.json == {"error": "Host not allowed."}
    for headers in ({}, {"Origin": "http://localhost"}, {"Sec-Fetch-Site": "same-origin"}):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, headers
        response.close()


def test_the_page_says_it_reads_turns_db(stira):
    html = stira.test_client().get("/observability").get_data(as_text=True)
    assert 'data-event-source="turns"' in html
    assert 'data-i18n="observability.source_turns_notice"' in html


def _route_pairs(app) -> list[tuple[str, str]]:
    return [
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"})
    ]


def test_no_route_is_registered_twice_in_stira(stira):
    duplicates = {pair: n for pair, n in Counter(_route_pairs(stira)).items() if n > 1}
    assert not duplicates, duplicates
    for path in ("/api/turns", "/api/turns/<turn_id>/events"):
        owners = [r.endpoint for r in stira.url_map.iter_rules() if r.rule == path]
        assert len(owners) == 1 and owners[0].startswith("chat"), (path, owners)
    owners = {
        r.rule: r.endpoint
        for r in stira.url_map.iter_rules()
        if r.rule.startswith("/api/observability/")
    }
    assert owners == {
        "/api/observability/events": "observability.stream_events",
        "/api/observability/snapshot": "observability.snapshot",
    }


def test_no_route_is_registered_twice_in_the_observer_app(tmp_path):
    from dashboard.app import create_app

    path = tmp_path / "state.json"
    NeuralState().save(path)
    app = create_app(path, tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    duplicates = {pair: n for pair, n in Counter(_route_pairs(app)).items() if n > 1}
    assert not duplicates, duplicates


def test_the_event_store_blueprint_no_longer_carries_turn_routes(tmp_path):
    from flask import Flask

    from stiracore.observability.event_store import SQLiteEventStore
    from stiracore.observability.sse import create_sse_blueprint

    app = Flask(__name__)
    store = SQLiteEventStore(tmp_path / "e.db")
    app.register_blueprint(create_sse_blueprint(store))
    paths = {rule.rule for rule in app.url_map.iter_rules()}
    assert paths >= {"/api/observability/events", "/api/observability/snapshot"}
    assert not {p for p in paths if p.startswith("/api/turns")}
    store.close()


def _raw_get(port: int, path: str, headers: dict) -> tuple[int, str]:
    """One GET with exactly these headers (http.client keeps a custom Host as given)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(4096).decode("utf-8", "replace")
    finally:
        connection.close()


# --- the real process: an approval, then the counts agree with turns.db -----------------


def test_real_process_snapshot_agrees_with_turns_db_including_an_approval(tmp_path):
    from tests.fake_bedrock_endpoint import FakeBedrock
    from tests.test_headless_file_tools_e2e import POSIX_ONLY, Server

    if POSIX_ONLY.args[0]:
        pytest.skip("POSIX process/stdin semantics")
    with FakeBedrock() as fake:
        server = Server(tmp_path, fake, "devnull")
        try:
            server.request("POST", "/api/chat", {"message": "hello"})
            server.turn({"path": "ok.txt", "content": "x\n"}, "approve")
            server.turn({"path": "no.txt", "content": "y\n"}, "reject")
            status, body = server.request("GET", "/api/observability/snapshot")
            assert status == 200
            snap = json.loads(body)
            _, page = server.request("GET", "/observability")
            guard = {
                (path, label): _raw_get(server.port, path, headers)
                for path in OBSERVABILITY_READS
                for label, headers in (
                    ("origin", {"Origin": "https://evil.example"}),
                    ("null", {"Origin": "null"}),
                    ("host", {"Host": "evil.example"}),
                )
            }
        finally:
            server.stop()
    connection = sqlite3.connect(server.data_dir / "turns.db")
    try:
        rows = connection.execute("SELECT type FROM ui_events").fetchall()
        turn_ids = connection.execute("SELECT COUNT(DISTINCT turn_id) FROM ui_events").fetchone()
    finally:
        connection.close()
    for key, (status_line, body) in guard.items():
        assert status_line == 403 and ": connected" not in body, key
    kinds = Counter(row[0] for row in rows)
    assert snap["event_count"] == len(rows) and snap["turn_count"] == turn_ids[0] == 3
    assert kinds["approval.requested"] == 2 and kinds["approval.resolved"] == 2
    seen = Counter(event["type"] for event in snap["events"])
    assert seen["approval_requested"] == 2 and seen["approval_resolved"] == 2
    assert 'data-event-source="turns"' in page
