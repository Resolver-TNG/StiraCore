"""#695: the failure digest — counts and representative ids, codes only, no model."""

from __future__ import annotations

import ast
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.authority.lifecycle_events import LOG_FILE, LifecycleLog, make_event
from stiracore.control import failure_digest_cli
from stiracore.observability import failure_digest, turn_events
from stiracore.observability.turn_events import TurnEventStore

CANARY = "sk-test-CANARY695"
GRANT = "a" * 32
BASE = datetime(2026, 11, 10, 9, 0, tzinfo=UTC).timestamp()


def _hex(number: int) -> str:
    return f"{number:032x}"


def _tool(number, status, error_class=None, deny_code=None, *, ts=None, **extra):
    row = {
        "event_id": _hex(number),
        "turn_id": _hex(1000 + number),
        "ts": BASE + number if ts is None else ts,
        "status": status,
        **extra,
    }
    if error_class is not None:
        row["error_class"] = error_class
    if deny_code is not None:
        row["deny_code"] = deny_code
    return row


def _life(number, kind, reason, *, ts=None):
    return make_event(
        kind,
        GRANT,
        "running",
        "idle",
        reason,
        incarnation="b" * 32,
        epoch=1,
        ts_wall=BASE + number if ts is None else ts,
        event_id=_hex(5000 + number),
    )


def _sample():
    tools = [
        _tool(1, "error", "tool_failed"),
        _tool(2, "error", "tool_failed"),
        _tool(3, "error", "tool_failed"),
        _tool(4, "error", "tool_failed"),
        _tool(5, "denied", "refused", "rule_never"),
        _tool(6, "denied", "refused", "rule_never"),
        _tool(7, "error", "exception"),
        _tool(8, "error"),  # a row from before #696
        _tool(9, "done"),
        _tool(10, "running"),
        _tool(11, "denied", "refused", "declared"),  # a checkpoint, not a failure
    ]
    lifecycle = [
        _life(1, "grant_iteration", "iteration_timeout"),
        _life(2, "grant_iteration", "iteration_timeout"),
        _life(3, "grant_iteration", "iteration_timeout"),
        _life(4, "goal", "blocked_no_progress"),
        _life(5, "grant_iteration", "iteration_done"),
        _life(6, "grant_iteration", "iteration_shutdown"),
        _life(7, "grant", "paused_user"),
    ]
    return tools, lifecycle


def test_grant_failure_reasons_follow_the_issue_definition():
    reasons = failure_digest.GRANT_FAILURE_REASONS
    for code in (
        "iteration_timeout",
        "iteration_model_error",
        "iteration_failed",
        "blocked_no_progress",
        "halted_breaker_warning",
        "tick_skipped_budget",
    ):
        assert code in reasons
    for code in (
        "iteration_done",
        "iteration_claimed",
        "iteration_yielded_user_turn",
        "iteration_shutdown",
        "iteration_stopped",
        "iteration_permit_stale",
        "paused_user",
        "tick_started",
        "goal_verified",
    ):
        assert code not in reasons


def test_counts_patterns_and_top_share():
    tools, lifecycle = _sample()
    digest = failure_digest.build(tool_rows=tools, lifecycle_rows=lifecycle)
    assert digest["total"] == 12
    assert digest["by_source"] == {"tool": 8, "grant": 4}
    assert [(p["pattern"], p["count"]) for p in digest["patterns"]] == [
        ("tool:error:tool_failed", 4),
        ("grant:grant_iteration:iteration_timeout", 3),
        ("tool:denied:refused:rule_never", 2),
        ("grant:goal:blocked_no_progress", 1),
        ("tool:error:exception", 1),
        ("tool:error:unclassified", 1),
    ]
    assert digest["top"] == {
        "n": 3,
        "patterns": [
            "tool:error:tool_failed",
            "grant:grant_iteration:iteration_timeout",
            "tool:denied:refused:rule_never",
        ],
        "count": 9,
        "share": 0.75,
    }
    assert sum(p["share"] for p in digest["patterns"]) == pytest.approx(1.0)
    assert digest["excluded"] == {"declared_checkpoint": 1}
    assert digest["skipped_rows"] == 0 and digest["lower_bound"] is True
    first = digest["patterns"][0]
    assert first["first_ts"] == BASE + 1 and first["last_ts"] == BASE + 4
    # newest first, at most ``examples``; a tool row is found by event id and turn id
    assert first["examples"] == [
        {"event_id": _hex(number), "turn_id": _hex(1000 + number)} for number in (4, 3, 2)
    ]
    # a grant iteration has no turn id: the event id and the grant id
    assert digest["patterns"][1]["examples"][0] == {"event_id": _hex(5003), "subject_id": GRANT}
    text = failure_digest.to_markdown(digest)
    assert "Top 3 patterns: 9 of 12 (75.0% of all failures)" in text
    assert "| 1 | `tool:error:tool_failed` | 4 | 33.3% |" in text
    assert "lower bound" in text and "1 declared checkpoint" in text


def test_build_is_deterministic_and_independent_of_row_order():
    tools, lifecycle = _sample()
    expected = failure_digest.build(tool_rows=tools, lifecycle_rows=lifecycle)
    shuffled = random.Random(695)
    for _ in range(5):
        shuffled.shuffle(tools)
        shuffled.shuffle(lifecycle)
        assert failure_digest.build(tool_rows=tools, lifecycle_rows=lifecycle) == expected


def test_representative_events_do_not_depend_on_row_order_at_equal_time_without_event_id():
    tools = [
        {**_tool(1, "error", "exception", ts=BASE), "event_id": None},
        {**_tool(2, "error", "exception", ts=BASE), "event_id": None},
    ]
    lifecycle = [
        {**_life(1, "goal", "blocked_no_progress", ts=BASE), "event_id": None, "subject_id": sid}
        for sid in ("d" * 32, "e" * 32)
    ]
    for count in (1, 3):
        forward = failure_digest.build(tool_rows=tools, lifecycle_rows=lifecycle, examples=count)
        backward = failure_digest.build(
            tool_rows=tools[::-1], lifecycle_rows=lifecycle[::-1], examples=count
        )
        assert forward == backward
    by_pattern = {p["pattern"]: p["examples"] for p in forward["patterns"]}
    assert by_pattern["tool:error:exception"][0] == {"event_id": None, "turn_id": _hex(1002)}
    assert by_pattern["grant:goal:blocked_no_progress"][0] == {
        "event_id": None,
        "subject_id": "e" * 32,
    }


def test_only_a_denied_declared_checkpoint_is_excluded():
    tools = [
        _tool(1, "denied", "refused", "declared"),  # the checkpoint (#545): excluded
        _tool(2, "denied", None, "declared"),  # the same pair without a class: excluded
        _tool(3, "error", "exception", "declared"),  # not the pair: a failure
        _tool(4, "denied", "refused", "other"),
        _tool(5, "done", None, "declared"),  # not a failure and not a checkpoint
        _tool(6, "denied", "refused", "declared", ts=BASE - 1),  # outside the period
    ]
    digest = failure_digest.build(tool_rows=tools, since=BASE)
    assert digest["excluded"] == {"declared_checkpoint": 2}
    assert digest["total"] == 2
    assert sorted(p["pattern"] for p in digest["patterns"]) == [
        "tool:denied:refused:other",
        "tool:error:exception:declared",
    ]
    text = failure_digest.to_markdown(digest)
    header = text.split("\n\n")[1]
    assert "- Excluded (not failures): 2 declared checkpoint(s)" in header
    only = failure_digest.build(tool_rows=tools[:1])
    assert only["total"] == 0 and only["excluded"] == {"declared_checkpoint": 1}
    header = failure_digest.to_markdown(failure_digest.build()).split("\n\n")[1]
    assert "- Excluded (not failures): 0 declared checkpoint(s)" in header


def test_the_digest_modules_import_no_model_network_or_memory_module():
    allowed = {
        "__future__",
        "argparse",
        "collections.abc",
        "datetime",
        "json",
        "math",
        "pathlib",
        "re",
        "shutil",
        "sqlite3",
        "sys",
        "tempfile",
        "typing",
        "stiracore.authority",
        "stiracore.authority.lifecycle_events",
        "stiracore.grants.progress",
        "stiracore.observability",
        ".hooks",
        ".turn_events",
    }
    for module in (failure_digest, failure_digest_cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
        assert imported <= allowed, sorted(imported - allowed)


def test_window_is_half_open_and_empty_digest_renders():
    tools = [_tool(1, "error", "exception", ts=BASE), _tool(2, "error", "exception", ts=BASE + 10)]
    lifecycle = [_life(1, "grant_tick", "tick_skipped_budget", ts=BASE + 10)]
    digest = failure_digest.build(
        tool_rows=tools, lifecycle_rows=lifecycle, since=BASE, until=BASE + 10
    )
    assert digest["total"] == 1 and digest["since"] == BASE and digest["until"] == BASE + 10
    empty = failure_digest.build(since=BASE, until=BASE + 10)
    assert empty["total"] == 0 and empty["top"]["share"] == 0.0 and empty["patterns"] == []
    assert "No failure was recorded" in failure_digest.to_markdown(empty)
    for bad in ({"top": 0}, {"examples": -1}, {"since": float("nan")}, {"until": "x"}):
        with pytest.raises(ValueError):
            failure_digest.build(**bad)


def test_output_is_an_allow_list_bodies_previews_and_secrets_never_appear():
    event = {
        "schema_version": 1,
        "event_id": _hex(1),
        "turn_id": _hex(2),
        "seq": 3,
        "ts": BASE,
        "type": "tool_call.updated",
        "message": CANARY,
        "data": {
            "tool_use_id": CANARY,
            "name": CANARY,
            "status": "error",
            "error_class": "exception",
            "input_preview": f"password={CANARY}",
            "output_preview": f"body {CANARY}",
            "error": CANARY,
        },
    }
    assert failure_digest.tool_row(event) == {
        "event_id": _hex(1),
        "turn_id": _hex(2),
        "ts": BASE,
        "status": "error",
        "error_class": "exception",
        "deny_code": None,
    }
    assert set(failure_digest.tool_row(event)) == set(failure_digest.TOOL_FIELDS)
    hostile_tools = [
        failure_digest.tool_row(event),
        # free text where a code or an id belongs: counted as unclassified, never echoed
        _tool(2, "error", CANARY, CANARY, name=CANARY, output_preview=CANARY),
        {**_tool(3, "denied", "refused", "other"), "event_id": CANARY, "turn_id": CANARY},
        _tool(4, CANARY),  # an unknown status: skipped
        {"status": "error", "ts": CANARY},  # skipped
        CANARY,  # skipped
    ]
    hostile_life = [
        {**_life(1, "grant_iteration", "iteration_failed"), "note": CANARY, "subject_id": CANARY},
        {**_life(2, "goal", "blocked_no_progress"), "reason_code": CANARY},
        {**_life(3, "goal", "blocked_no_progress"), "subject_kind": CANARY},
        {"ts_wall": CANARY},  # skipped
    ]
    digest = failure_digest.build(
        tool_rows=hostile_tools,
        lifecycle_rows=hostile_life,
        drop_counts={"record_failed": 2, CANARY: 1, "no_call_id": CANARY},
        source_problems=["turns_db_unreadable", CANARY, 7],
    )
    assert digest["total"] == 4 and digest["skipped_rows"] == 4
    assert [p["pattern"] for p in digest["patterns"]] == [
        "grant:grant_iteration:iteration_failed",
        "tool:denied:refused:other",
        "tool:error:exception",
        "tool:error:unclassified",
    ]
    assert digest["patterns"][0]["examples"] == [{"event_id": _hex(5001), "subject_id": None}]
    assert digest["patterns"][1]["examples"] == [{"event_id": None, "turn_id": None}]
    assert digest["dropped_events"] == {"record_failed": 2}
    assert digest["source_problems"] == ["turns_db_unreadable"]
    # the shape itself is fixed: no free-text key can be added without changing this test
    assert set(digest) == {
        "since",
        "until",
        "total",
        "by_source",
        "patterns",
        "top",
        "excluded",
        "skipped_rows",
        "dropped_events",
        "source_problems",
        "lower_bound",
    }
    allowed = {
        "pattern",
        "source",
        "status",
        "error_class",
        "deny_code",
        "subject_kind",
        "reason_code",
        "count",
        "share",
        "first_ts",
        "last_ts",
        "examples",
    }
    for pattern in digest["patterns"]:
        assert set(pattern) <= allowed
        for example in pattern["examples"]:
            assert set(example) <= {"event_id", "turn_id", "subject_id"}
    text = failure_digest.to_markdown(digest)
    for rendered in (json.dumps(digest), text):
        assert "CANARY" not in rendered and "password" not in rendered
    assert "Dropped turn events (process counter): record_failed 2." in text
    assert "`turns_db_unreadable`" in text and "Rows skipped as malformed: 4." in text


def _snapshot(directory):
    """Every path under ``directory`` (a file with its bytes, a directory with ``None``)."""
    return {
        str(path.relative_to(directory)): path.read_bytes() if path.is_file() else None
        for path in directory.rglob("*")
    }


def _write_data_dir(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=BASE)
    monkeypatch.setattr(turn_events, "time", SimpleNamespace(time=lambda: clock.now))
    store = TurnEventStore(tmp_path / "turns.db")
    try:

        def tool(offset, status, **codes):
            clock.now = BASE + offset
            return store.append(
                "c" * 32,
                "tool_call.updated",
                {
                    "tool_use_id": f"tool-{offset}",
                    "name": "secret_tool_name",
                    "status": status,
                    "input_preview": "input body text",
                    "output_preview": "output body text",
                    **codes,
                },
            )

        tool(-8 * 86400, "error", error_class="tool_failed")  # before the 7 days
        failed = [tool(n, "error", error_class="tool_failed") for n in (1, 2)]
        tool(3, "denied", error_class="refused", deny_code="rule_never")
        tool(4, "done")
        clock.now = BASE + 5
        store.append("c" * 32, "turn.completed", {"usage": {}, "stop": "end_turn"})
    finally:
        store.close()
    log = LifecycleLog(tmp_path)
    log.append(_life(1, "grant_iteration", "iteration_model_error"))
    log.append(_life(2, "grant_iteration", "iteration_done"))
    return failed


def test_cli_prints_markdown_from_the_stores_and_writes_nothing(tmp_path, monkeypatch, capsys):
    failed = _write_data_dir(tmp_path, monkeypatch)
    before = _snapshot(tmp_path)
    argv = ["--data-dir", str(tmp_path), "--now", "2026-11-10T10:00:00+00:00"]
    assert failure_digest_cli.main(argv) == 0
    text = capsys.readouterr().out
    assert text.startswith("# Failure digest\n")
    assert "Period (UTC): 2026-11-03T10:00:00Z to 2026-11-10T10:00:00Z" in text
    assert "Failures counted: 4 (tool calls 3, grants 1), at least" in text
    assert "Top 3 patterns: 4 of 4 (100.0% of all failures)" in text
    assert "| 1 | `tool:error:tool_failed` | 2 | 50.0% |" in text
    assert f"event_id={failed[1]['event_id']} turn_id={'c' * 32}" in text
    assert "`grant:grant_iteration:iteration_model_error`" in text
    assert "lower bound" in text and "Dropped turn events: not known here" in text
    for body in ("secret_tool_name", "body text", "tool-1"):
        assert body not in text
    assert failure_digest_cli.main([*argv, "--format", "json", "--top", "1"]) == 0
    digest = json.loads(capsys.readouterr().out)
    assert digest["total"] == 4 and digest["top"]["share"] == 0.5
    for body in ("secret_tool_name", "body text", "tool-1"):
        assert body not in json.dumps(digest)
    # nothing is written into the data directory: the same files with the same bytes
    assert _snapshot(tmp_path) == before


def test_cli_reads_a_live_wal_database_from_a_copy_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    clock = SimpleNamespace(now=BASE + 1)
    monkeypatch.setattr(turn_events, "time", SimpleNamespace(time=lambda: clock.now))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(failure_digest_cli.tempfile, "tempdir", str(scratch))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = TurnEventStore(data_dir / "turns.db")  # stays open: a running server
    try:
        store.append(
            "c" * 32,
            "tool_call.updated",
            {"tool_use_id": "tool-1", "name": "n", "status": "error", "error_class": "exception"},
        )
        wal = data_dir / "turns.db-wal"
        assert wal.is_file() and wal.stat().st_size > 0  # the row is still in the WAL
        before = _snapshot(data_dir)
        argv = ["--data-dir", str(data_dir), "--now", "2026-11-10T10:00:00+00:00"]
        assert failure_digest_cli.main(argv) == 0
        assert _snapshot(data_dir) == before  # before the store's own close checkpoints
    finally:
        store.close()
    text = capsys.readouterr().out
    assert "| 1 | `tool:error:exception` | 1 | 100.0% |" in text
    assert list(scratch.iterdir()) == []  # the copy is removed


def test_cli_reports_a_source_it_cannot_read_and_usage_errors(tmp_path, monkeypatch, capsys):
    assert failure_digest_cli.main(["--data-dir", str(tmp_path / "absent")]) == 1
    assert "not found" in capsys.readouterr().err
    # no turns.db: the lifecycle side is still printed, with the note, and the exit is 1
    LifecycleLog(tmp_path).append(_life(1, "goal", "blocked_no_progress"))
    argv = ["--data-dir", str(tmp_path), "--now", "2026-11-10T10:00:00+00:00"]
    assert failure_digest_cli.main(argv) == 1
    text = capsys.readouterr().out
    assert "`grant:goal:blocked_no_progress`" in text
    assert "A source could not be read: `turns_db_missing`" in text
    # a lifecycle log that is not usable (a torn last line) is named by its fixed code
    (tmp_path / "turns.db").write_bytes(b"not a database")
    with (tmp_path / LOG_FILE).open("ab") as handle:
        handle.write(b'{"seq"')
    assert failure_digest_cli.main(argv) == 1
    text = capsys.readouterr().out
    assert "`turns_db_unreadable`" in text and "`lifecycle_log_torn`" in text
    assert "Failures counted: 0 " in text
    for bad in (["--days", "0"], ["--now", "nope"], ["--top", "0"], ["--format", "html"]):
        with pytest.raises(SystemExit) as exit_info:
            failure_digest_cli.main(["--data-dir", str(tmp_path), *bad])
        assert exit_info.value.code == 2
