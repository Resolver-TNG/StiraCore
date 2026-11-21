"""Follow-ups of the round ledger review (#761; ADR 0028 §5). Offline and deterministic:
a scripted provider, fake clocks, no sleeps, no Bedrock."""

from __future__ import annotations

import json
import os
import threading

import pytest
from grants_exec_helpers import grant, make_env, run_one, use
from test_grants_round_ledger import round_record

from stiracore.authority import private_file
from stiracore.grants import round_ledger as rl
from stiracore.grants.round_ledger import RoundLedger
from stiracore.grants.schema import BoardBusy


def _foreign_ledger(tmp_path):
    """The file of a self-consistent ledger someone else wrote (one ``tick`` record)."""
    other = tmp_path / "foreign"
    other.mkdir()
    RoundLedger(other).append(round_record(origin="tick"))
    return other / rl.LEDGER_FILE


def _fill_until_the_next_append_rotates(ledger, start: int) -> int:
    n = start
    size = len(json.dumps(ledger.records()[-1])) + 200  # above one stored line
    while ledger._size + size <= rl.ROTATE_BYTES:
        ledger.append(round_record(round=n))
        n += 1
    return n


# --- 1. rotation checks the file before it moves it ---------------------------------------------


def test_a_rotation_never_moves_a_replaced_file_over_the_older_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 4000)
    ledger = RoundLedger(tmp_path)
    for n in range(1, 9):
        ledger.append(round_record(round=n))
    good_old = (tmp_path / rl.OLD_LEDGER_FILE).read_bytes()  # at least one rotation
    n = _fill_until_the_next_append_rotates(ledger, 9)
    os.replace(_foreign_ledger(tmp_path), tmp_path / rl.LEDGER_FILE)
    foreign = (tmp_path / rl.LEDGER_FILE).read_bytes()
    with pytest.raises(OSError):
        ledger.append(round_record(round=n))  # this append would rotate first
    assert ledger.unhealthy_reason == "round_ledger_replaced"
    assert (tmp_path / rl.OLD_LEDGER_FILE).read_bytes() == good_old  # nothing moved
    assert (tmp_path / rl.LEDGER_FILE).read_bytes() == foreign  # ... and nothing written
    # A restart does not take the foreign file as the history.
    restarted = RoundLedger(tmp_path)
    assert restarted.unhealthy_reason in rl.UNHEALTHY_REASONS
    assert restarted.records() == []


def test_a_rotation_refuses_a_vanished_current_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 4000)
    ledger = RoundLedger(tmp_path)
    ledger.append(round_record())
    n = _fill_until_the_next_append_rotates(ledger, 2)
    os.replace(tmp_path / rl.LEDGER_FILE, tmp_path / "moved-away.jsonl")
    with pytest.raises(OSError):
        ledger.append(round_record(round=n))
    assert ledger.unhealthy_reason == "round_ledger_replaced"
    assert not (tmp_path / rl.OLD_LEDGER_FILE).exists()


def test_a_file_replaced_before_the_startup_rotation_is_reported_as_replaced(tmp_path, monkeypatch):
    ledger = RoundLedger(tmp_path)
    for n in range(1, 4):
        ledger.append(round_record(round=n))
    source = _foreign_ledger(tmp_path)
    monkeypatch.setattr(rl, "ROTATE_BYTES", 10)  # the start-up rotation is due
    real_read = private_file.read

    def read_then_swap(path, **kw):
        out = real_read(path, **kw)
        if str(path).endswith(rl.LEDGER_FILE) and source.exists():
            os.replace(source, path)  # replaced between the read and the rotation
        return out

    monkeypatch.setattr(rl.private_file, "read", read_then_swap)
    started = RoundLedger(tmp_path)
    assert started.unhealthy_reason == "round_ledger_replaced"
    assert not (tmp_path / rl.OLD_LEDGER_FILE).exists()  # the foreign file was not moved


def test_an_unchanged_file_still_rotates_and_continues_the_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 4000)
    ledger = RoundLedger(tmp_path)
    for n in range(1, 13):
        ledger.append(round_record(round=n))
    assert ledger.unhealthy_reason is None and ledger.verify() is None
    again = RoundLedger(tmp_path)
    assert again.unhealthy_reason is None
    assert again.append(round_record(round=13))["seq"] == 13


# --- 2. the grant of the round is read by its id ------------------------------------------------


@pytest.mark.parametrize("board", ["busy", "another_grant_is_newest"])
def test_the_rounds_stop_and_limits_do_not_depend_on_the_board_read(tmp_path, monkeypatch, board):
    env = make_env(tmp_path)
    env.provider.script = [[use("goal_report", {"kind": "blocked", "code": "missing_input"})]]

    def board_record():
        if board == "busy":
            raise BoardBusy()

    monkeypatch.setattr(env.service, "board_record", board_record)
    assert run_one(env)["end"] == "reported_blocked"
    monkeypatch.undo()
    line = json.loads((env.service.data_dir / rl.LEDGER_FILE).read_text().splitlines()[-1])
    record = grant(env)
    assert line["stop"] == "missing_input"
    assert line["limits"]["iterations_left"] == record.max_iterations - record.iterations == 9
    assert line["limits"]["tokens_left"] == record.total_tokens - record.used_tokens > 0
    assert line["limits"]["ttl_left_s"] > 0


def test_the_services_round_read_is_by_id_from_memory_and_bounded(tmp_path):
    env = make_env(tmp_path)
    assert env.service.round_record(env.grant_id) is grant(env)
    assert env.service.round_record("0" * 32) is None
    assert env.service.round_record(None) is None
    held, release, answers = threading.Event(), threading.Event(), []

    def hold():
        with env.service._lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        answers.append(env.service.round_record(env.grant_id, wait=0.01))  # never a hang
    finally:
        release.set()
        holder.join(10)
    assert answers == [None]


def test_an_unreadable_grant_keeps_the_unknown_stop(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    env.provider.script = [[{"text": "x"}]]
    monkeypatch.setattr(env.service, "round_record", lambda *_a, **_k: None)
    run_one(env)
    line = json.loads((env.service.data_dir / rl.LEDGER_FILE).read_text().splitlines()[-1])
    assert line["stop"] == "unknown" and line["limits"]["iterations_left"] == 0


# --- 3. an unexpected failure of the append is not only logged ----------------------------------


def test_an_unexpected_error_from_the_append_makes_the_ledger_unhealthy(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    env.provider.script = [[{"text": "x"}]]

    def boom(_record):
        raise RuntimeError("sk-test-0123456789abcdef0123456789abcdef")

    monkeypatch.setattr(env.executor.ledger, "append", boom)
    assert run_one(env)["end"] == "no_action"  # the iteration's end is as usual
    assert env.executor.ledger.unhealthy_reason == "round_ledger_record_refused"
    assert env.executor.ledger_view(env.grant_id)["ledger"] == "round_ledger_record_refused"
    assert env.executor.tick_facts(env.grant_id)["ledger"] is not None  # the tick skips


def test_a_failed_write_keeps_its_own_code_and_is_not_counted_as_refused(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    env.provider.script = [[{"text": "x"}]]

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rl.private_file, "append_line", fail)
    run_one(env)
    assert env.executor.ledger.unhealthy_reason == "round_ledger_write_failed"
    assert env.executor.ledger.refused == 0


# --- 4. the CLI reads a live ledger -------------------------------------------------------------


def _cli(tmp_path, capsys) -> tuple[int, dict]:
    from stiracore.control import grant_rounds_cli

    code = grant_rounds_cli.main(["--data-dir", str(tmp_path), "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def test_the_cli_reads_again_when_an_append_lands_during_its_read(tmp_path, monkeypatch, capsys):
    writer = RoundLedger(tmp_path)
    writer.append(round_record())
    real_open, fired = os.open, []

    def racing_open(path, flags, *a):
        if not fired and str(path).endswith(rl.LEDGER_FILE) and not flags & os.O_WRONLY:
            fired.append(1)
            writer.append(round_record(round=2))  # the server's append lands here
        return real_open(path, flags, *a)

    monkeypatch.setattr(private_file.os, "open", racing_open)
    code, answer = _cli(tmp_path, capsys)
    assert fired and code == 0 and answer["ledger"] == "ok"
    assert [r["round"] for r in answer["records"]] == [1, 2]
    assert writer.verify() is None  # the reader wrote nothing


def test_the_cli_reads_again_when_a_rotation_lands_between_the_segments(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 4000)
    writer = RoundLedger(tmp_path)
    count = [0]

    def more():
        count[0] += 1
        writer.append(round_record(round=count[0]))

    while not (tmp_path / rl.OLD_LEDGER_FILE).exists():
        more()
    real_read, fired = private_file.read, []

    def racing_read(path, **kw):
        out = real_read(path, **kw)
        if not fired:  # after the older segment was read: the server rotates
            fired.append(1)
            first = writer._old_lines[0]["seq"]
            while writer._old_lines[0]["seq"] == first or not writer._cur_lines:
                more()
        return out

    monkeypatch.setattr(rl.private_file, "read", racing_read)
    code, answer = _cli(tmp_path, capsys)
    assert fired and code == 0 and answer["ledger"] == "ok"
    assert answer["records"][-1]["round"] == count[0]


def test_the_cli_still_reports_a_ledger_that_stays_bad(tmp_path, capsys):
    writer = RoundLedger(tmp_path)
    writer.append(round_record())
    path = tmp_path / rl.LEDGER_FILE
    path.write_bytes(path.read_bytes().replace(b'"model_calls":2', b'"model_calls":7', 1))
    code, answer = _cli(tmp_path, capsys)
    assert code == 1 and answer == {
        "ledger": "round_ledger_hash_mismatch",
        "records": [],
        "summary": rl.summary([]),
    }
