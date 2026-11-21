"""The round ledger on the executor, the heartbeat start and the route (ADR 0028 §5; #757).

Offline and deterministic: a scripted provider, fake clocks, no sleeps, no Bedrock.
"""

from __future__ import annotations

import json
import logging

import pytest
from grants_exec_helpers import MARKER, make_env, run_one, use
from test_grants_round_ledger import round_record
from test_grants_tick import (
    _tick_events,
    fire,
    fire_and_wait,
    scripted,
    text_only,
    tick_env,
    w,
    write,
)

from stiracore.grants import round_ledger as rl

MODEL_TEXT = "modeltext-canary I rewrote the plan as requested"
FILE_TEXT = "see https://canary.invalid/hook and sk-test-0123456789abcdef0123456789abcdef"
FILE_NAME = "canaryfile_plan.md"
WALL = 1_700_000_000


def _ledger_bytes(env) -> bytes:
    path = env.service.data_dir / rl.LEDGER_FILE
    return path.read_bytes() if path.exists() else b""


def _lines(env) -> list[dict]:
    return [json.loads(text) for text in _ledger_bytes(env).decode().splitlines()]


def _write_then_talk(env) -> list:
    path = str(env.sub / FILE_NAME)
    return [
        [use("editor", {"command": "create", "path": path, "file_text": FILE_TEXT})],
        [{"text": MODEL_TEXT}],
    ]


def _press_env(tmp_path, **kw):
    env = make_env(tmp_path, **kw)
    env.executor._wall = lambda: WALL
    env.provider.script = _write_then_talk(env)
    return env


def test_a_pressed_iteration_appends_exactly_one_allow_listed_record(tmp_path):
    env = _press_env(tmp_path)
    assert _ledger_bytes(env) == b""  # nothing before an iteration ends
    view = run_one(env)
    assert (env.sub / FILE_NAME).read_text() == FILE_TEXT and view["end"] == "done"
    (line,) = _lines(env)
    assert set(line) == rl._ROUND_KEYS | {"v", "seq", "prev", "hash"}
    assert (line["k"], line["origin"], line["grant"]) == ("round", "press", env.grant_id)
    assert (line["round"], line["of_max"]) == (1, 10)
    assert (line["end_code"], line["detail"], line["progress"]) == ("done", "none", "new")
    assert (line["model_calls"], line["writes"], line["reports"]) == (2, 1, 0)
    assert line["tools"] == {**dict.fromkeys(rl.TOOL_KEYS, 0), "editor": 1}
    assert line["files_changed"] == 1
    assert (line["tokens_in"], line["tokens_out"], line["largest_input"]) == (14, 6, 7)
    assert line["cache_read"] is None and line["summaries"] is None
    assert (line["start"], line["end"], line["errors"], line["stop"]) == (WALL, WALL, [], None)
    assert line["limits"]["iterations_left"] == 9 and line["limits"]["ceiling_min"] == 360
    assert env.executor.ledger.unhealthy_reason is None
    run_one(env)  # a second press: exactly one more
    assert [item["round"] for item in _lines(env)] == [1, 2]


def test_a_heartbeat_started_iteration_appends_exactly_one_record(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    env.executor._wall = lambda: WALL
    assert fire_and_wait(env) == "started"
    (line,) = _lines(env)
    assert (line["origin"], line["grant"], line["end_code"]) == ("tick", env.grant_id, "done")
    assert line["tools"]["editor"] == 1 and line["tokens_in"] == 14
    assert fire(env) == "min_spacing" and len(_lines(env)) == 1  # a skip writes nothing


def test_no_goal_path_model_text_url_or_secret_reaches_the_ledger_or_the_route(tmp_path):
    from flask import Flask

    from stiracore.grants.routes import create_grants_blueprint

    env = _press_env(tmp_path)
    run_one(env)
    env.provider.script = [[use("file_read", {"path": str(env.sub / FILE_NAME)})]]
    run_one(env)
    app = Flask(__name__)
    app.register_blueprint(create_grants_blueprint(lambda: env.service, executor=env.executor))
    response = app.test_client().get(f"/api/grants/{env.grant_id}/rounds")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ledger"] == "ok" and [r["round"] for r in body["rounds"]] == [1, 2]
    assert all("prev" not in r and "hash" not in r for r in body["rounds"])
    assert set(body["rounds"][0]) == rl._ROUND_KEYS | {"v", "seq"}
    canaries = (
        MARKER,
        "tidy the notes",
        MODEL_TEXT,
        "modeltext-canary",
        FILE_TEXT,
        "canary.invalid",
        "sk-test-",
        FILE_NAME,
        "canaryfile",
        str(env.sub),
        str(env.ws),
        str(tmp_path),
        "proj",
    )
    stored = _ledger_bytes(env).decode()
    shown = response.get_data(as_text=True)
    for canary in canaries:
        assert canary not in stored, canary
        assert canary not in shown, canary
    # every string in a stored record is a member of a fixed list (or the grant id)
    allowed = (
        rl.END_CODES | rl.DETAIL_CODES | rl.PROGRESS | rl.ORIGINS | rl.STOP_CODES | rl.ERROR_CODES
    )
    allowed |= rl.AXIS_CODES | {"round", env.grant_id, "unknown"}

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    assert set(strings(body["rounds"])) <= allowed
    unknown = app.test_client().get(f"/api/grants/{'0' * 32}/rounds")
    assert unknown.status_code >= 400 and unknown.get_json()["code"] == "grant_unknown"


@pytest.mark.parametrize(
    "spoil",
    [
        lambda r: {**r, "goal": f"tidy the notes {MARKER}"},  # an unknown key
        lambda r: {**r, "detail": MODEL_TEXT},  # a free-text value
        lambda r: {**r, "end_code": "finished"},  # a string outside its enum
        lambda r: {**r, "tools": {**r["tools"], FILE_NAME: 1}},
        lambda r: {**r, "axis": {"start": "passed", "end": "not_run"}},  # outside AXIS_CODES
        lambda r: {**r, "axis": {**r["axis"], "items": MODEL_TEXT}},  # an extra key
        lambda r: {k: v for k, v in r.items() if k != "axis"},  # the key is required
    ],
)
def test_a_bad_record_is_refused_not_truncated_and_the_error_is_visible(
    tmp_path, spoil, monkeypatch, caplog
):
    env = _press_env(tmp_path)
    build = env.executor._round_record
    monkeypatch.setattr(env.executor, "_round_record", lambda *a: spoil(build(*a)))
    with caplog.at_level(logging.WARNING, logger="stiracore.grants.executor"):
        view = run_one(env)
    assert view["end"] == "done"  # the iteration itself ended as usual
    assert _ledger_bytes(env) == b""  # nothing written: not the record minus the bad field
    shown = env.executor.ledger_view(env.grant_id)
    assert shown == {"ledger": "round_ledger_record_refused", "rounds": []}
    assert "round ledger refused a record" in caplog.text
    assert MARKER not in caplog.text and MODEL_TEXT not in caplog.text
    assert env.authority.unhealthy_reason is None  # observation: the authority is not latched


def test_round_record_default_axis_not_run(tmp_path):
    """#767: every round the executor writes — a press and a heartbeat start — carries
    ``axis`` ``not_run`` and zero page tool counts; nothing measures or reads the axis."""
    not_run = {"start": "not_run", "end": "not_run"}
    env = _press_env(tmp_path)
    run_one(env)
    env.provider.script = [[use("file_read", {"path": str(env.sub / FILE_NAME)})]]
    run_one(env)
    lines = _lines(env)
    assert [line["axis"] for line in lines] == [not_run, not_run]
    for line in lines:
        assert set(line["tools"]) == set(rl.TOOL_KEYS)
        assert [line["tools"][name] for name in ("page_look", "page_review", "view_image")] == [
            0,
            0,
            0,
        ]
    assert env.executor.ledger.unhealthy_reason is None
    assert env.executor.ledger_view(env.grant_id)["rounds"][-1]["axis"] == not_run
    (tmp_path / "tick").mkdir()
    ticked = scripted(tick_env(tmp_path / "tick"), w(1), text_only())
    assert fire_and_wait(ticked) == "started"
    (line,) = _lines(ticked)
    assert (line["origin"], line["axis"]) == ("tick", not_run)


def _break_writes(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rl.private_file, "append_line", fail)


def test_a_failed_write_skips_the_heartbeat_start_but_a_press_still_runs(tmp_path, monkeypatch):
    env = scripted(tick_env(tmp_path), w(1), text_only(), w(2), text_only(), w(3), text_only())
    assert fire_and_wait(env) == "started"
    assert len(_lines(env)) == 1
    env.clocks.tick(300)
    env.mono[0] += 300
    _break_writes(monkeypatch)
    # a person's press: the iteration runs; its record cannot be written — not swallowed
    calls = env.provider.calls
    env.executor.step(env.grant_id)
    assert env.executor.wait_idle(20)
    assert env.provider.calls > calls and env.executor.view(env.grant_id)["end"] == "done"
    assert (env.sub / "note2.md").exists()
    assert env.executor.ledger_view(env.grant_id)["ledger"] == "round_ledger_write_failed"
    assert len(_lines(env)) == 1
    # the heartbeat start: skipped, nothing pressed, no model call
    env.clocks.tick(300)
    env.mono[0] += 300
    calls = env.provider.calls
    assert fire(env) == "ledger_unhealthy"
    assert fire(env, "event") == "ledger_unhealthy"
    assert env.provider.calls == calls and not (env.sub / "note3.md").exists()
    tick = env.ticker.view(env.grant_id)
    assert tick["last"] == "ledger_unhealthy" and tick["skipped"]["ledger_unhealthy"] == 2
    # observation, not authority: nothing is latched, the grant is as it was, no event
    assert env.authority.unhealthy_reason is None and env.service.unhealthy_reason is None
    assert env.service._get(env.grant_id).status == "active"
    assert "ledger_unhealthy" not in _tick_events(env)  # no lifecycle event, no outbox item
    # a pressed iteration still runs (a person is there)
    monkeypatch.undo()
    calls = env.provider.calls
    env.executor.step(env.grant_id)
    assert env.executor.wait_idle(20)
    assert env.provider.calls > calls and (env.sub / "note3.md").exists()
    # sticky until a person has looked (a restart): past the spacing, still skipped
    env.clocks.tick(300)
    env.mono[0] += 300
    assert fire(env) == "ledger_unhealthy"


def test_the_ledger_clause_is_the_last_one(tmp_path, monkeypatch):
    """Every existing reason is reported as before; ``ledger_unhealthy`` only replaces a
    start."""
    env = scripted(tick_env(tmp_path), w(1), text_only())
    env.executor.ledger._bad = "round_ledger_write_failed"
    env.healthy.value = False
    assert fire(env) == "heartbeat_unhealthy"
    env.healthy.value = True
    env.busy = True
    assert fire(env) == "turn_busy"
    env.busy = False
    assert fire(env) == "ledger_unhealthy"
    assert env.provider.calls == 0


def test_a_refused_record_also_stops_the_heartbeat_start(tmp_path, monkeypatch):
    env = scripted(tick_env(tmp_path), w(1), text_only(), w(2), text_only())
    build = env.executor._round_record
    monkeypatch.setattr(env.executor, "_round_record", lambda *a: {**build(*a), "note": "x"})
    assert fire_and_wait(env) == "started"
    assert _ledger_bytes(env) == b""
    env.clocks.tick(300)
    env.mono[0] += 300
    assert fire(env) == "ledger_unhealthy"


def test_without_a_ledger_nothing_is_written_or_skipped(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    env.executor.ledger = None
    assert env.executor.tick_facts(env.grant_id)["ledger"] is None
    assert fire_and_wait(env) == "started"
    assert _ledger_bytes(env) == b""
    assert env.executor.ledger_view(env.grant_id) == {"ledger": "off", "rounds": []}


def test_the_stop_code_is_the_grants_rule_not_a_text(tmp_path):
    env = _press_env(tmp_path, max_iterations=10)
    env.provider.script = [[use("goal_report", {"kind": "blocked", "code": "missing_input"})]]
    view = run_one(env)
    assert view["end"] == "reported_blocked"
    (line,) = _lines(env)
    assert (line["end_code"], line["stop"]) == ("reported_blocked", "missing_input")
    assert line["tools"]["goal_report"] == 1 and line["reports"] == 1


def test_the_board_read_carries_the_last_five_rounds_as_codes_and_counts(tmp_path):
    from stiracore.grants.schema import BoardBusy
    from stiracore.observability import activity

    env = _press_env(tmp_path)
    record = env.service.board_record()
    assert "rounds" not in env.executor.board(env.grant_id)  # nothing before a round
    for n in range(6):
        env.provider.script = [write(env, n), [{"text": MODEL_TEXT}]]
        run_one(env)
    board = env.executor.board(env.grant_id)
    assert [r["round"] for r in board["rounds"]] == [2, 3, 4, 5, 6] and board["ledger"] == "ok"
    out = activity.grant_view(env.service.board_view(record), board)
    assert [r["round"] for r in out["rounds"]] == [2, 3, 4, 5, 6] and out["ledger"] == "ok"
    assert out["rounds"][0]["end_code"] == "done" and out["rounds"][0]["origin"] == "press"
    text = json.dumps(out)
    for canary in (MARKER, MODEL_TEXT, FILE_NAME, str(env.sub), "canary.invalid", "sk-test-"):
        assert canary not in text
    # the board's allow-list is its own: a value outside the lists never passes
    board["rounds"][0].update(end_code=MODEL_TEXT, stop=FILE_NAME, tokens_in=MARKER, note=MARKER)
    board["ledger"] = MODEL_TEXT
    again = json.dumps(activity.grant_view(env.service.board_view(record), board))
    assert MODEL_TEXT not in again and FILE_NAME not in again and MARKER not in again
    # never a wait: an append that is flushing makes the read busy
    with env.executor.ledger._lock, pytest.raises(BoardBusy):
        env.executor.board(env.grant_id)


def test_the_cli_prints_the_table_and_the_summary(tmp_path, capsys):
    from stiracore.control import grant_rounds_cli

    env = _press_env(tmp_path)
    run_one(env)
    run_one(env)
    data_dir = str(env.service.data_dir)
    before = _ledger_bytes(env)
    assert grant_rounds_cli.main(["--data-dir", data_dir]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[:4] == ["seq", "grant", "round", "origin"]
    assert len(out.splitlines()) == 4 and env.grant_id[:8] in out
    assert "summary: rounds=2 hours=0.0 tokens_in=" in out and "new=1" in out
    assert grant_rounds_cli.main(["--data-dir", data_dir, "--format", "json", "--last", "1"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["ledger"] == "ok" and [r["round"] for r in answer["records"]] == [2]
    assert grant_rounds_cli.main(["--data-dir", data_dir, "--grant", "0" * 32]) == 0
    assert "rounds=0" in capsys.readouterr().out
    for canary in (MARKER, MODEL_TEXT, FILE_NAME, str(env.sub), "canary.invalid", "sk-test-"):
        assert canary not in out and canary not in json.dumps(answer)
    assert _ledger_bytes(env) == before  # the view writes nothing
    # a tampered ledger: its code, no record, exit 1
    path = env.service.data_dir / rl.LEDGER_FILE
    path.write_bytes(before.replace(b'"model_calls":2', b'"model_calls":7', 1))
    assert grant_rounds_cli.main(["--data-dir", data_dir]) == 1
    shown = capsys.readouterr().out
    assert "ledger: round_ledger_hash_mismatch" in shown and "rounds=0" in shown
    assert grant_rounds_cli.main(["--data-dir", str(tmp_path / "absent")]) == 1
    with pytest.raises(SystemExit):
        grant_rounds_cli.main(["--data-dir", data_dir, "--grant", "../etc"])


def test_the_builders_record_shape_is_the_unit_tests(tmp_path):
    """The executor builds exactly the keys the allow-list test fixture has."""
    env = _press_env(tmp_path)
    run_one(env)
    assert set(_lines(env)[0]) - {"v", "seq", "prev", "hash"} == set(round_record())
