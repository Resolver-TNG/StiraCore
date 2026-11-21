"""#700: one recorded turn as an ATIF trajectory — one way, opt-in, codes only, no model."""

from __future__ import annotations

import ast
import json
import os
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stiracore.control import atif_export_cli
from stiracore.observability import atif_export
from stiracore.observability.turn_events import TurnEventStore

CANARY = "sk-test-CANARY700"
BASE = datetime(2026, 11, 10, 9, 0, tzinfo=UTC).timestamp()
TURN = "a" * 32
CALL_1 = "1" * 32
CALL_2 = "2" * 32


def _event(seq, kind, data, *, turn=TURN, ts=None):
    return {
        "schema_version": 1,
        "event_id": f"{seq:032x}",
        "turn_id": turn,
        "seq": seq,
        "ts": BASE + seq if ts is None else ts,
        "type": kind,
        "data": data,
    }


def _tool(seq, kind, status, **extra):
    data = {"tool_use_id": "tooluse_01", "name": "file_read", "status": status, **extra}
    return _event(seq, kind, data)


def _turn(taint=None):
    summary = {"category": "neutral", "breaker": "NORMAL"}
    if taint is not None:
        summary["taint"] = {"context_trust": taint, "context_sensitive": False}
    return [
        _event(1, "turn.started", {"prompt_chars": 12, "image_count": 0}),
        _event(2, "model.started", {"call_id": CALL_1, "model": "test-model"}),
        _event(
            3,
            "model.completed",
            {
                "call_id": CALL_1,
                "model": "test-model",
                "duration_ms": 800,
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "stop": "tool_use",
                "status": "success",
            },
        ),
        _tool(4, "tool_call.created", "running", input_preview='{"path": "notes.md"}'),
        _event(5, "approval.requested", {"request_id": "req-1"}),
        _event(6, "approval.resolved", {"request_id": "req-1", "decision": "approved"}),
        _tool(7, "tool_call.updated", "done", duration_ms=40, output_preview="file body"),
        _event(
            8,
            "model.completed",
            {
                "call_id": CALL_2,
                "model": "test-model",
                "duration_ms": 500,
                "usage": {"input_tokens": 150, "output_tokens": 30},
                "stop": "end_turn",
                "status": "success",
            },
        ),
        _event(9, "turn.summary", summary),
        _event(10, "breaker.transition", {"from": "NORMAL", "to": "WARNING"}),
        _event(
            11,
            "turn.completed",
            {
                "usage": {"input_tokens": 250, "output_tokens": 50},
                "stop": "end_turn",
                "error": False,
                "model": "test-model",
                "prompt_cache": {"prefix": "abc"},
            },
        ),
    ]


def test_one_turn_maps_to_an_atif_trajectory_with_ids_numbers_and_codes():
    document = atif_export.build(_turn())
    assert document["schema_version"] == atif_export.ATIF_SCHEMA_VERSION
    assert document["session_id"] == TURN
    assert document["agent"]["name"] == "stiracore"
    assert document["agent"]["model_name"] == "test-model"
    steps = document["steps"]
    assert [step["step_id"] for step in steps] == [1, 2, 3]
    assert [step["source"] for step in steps] == ["user", "agent", "agent"]
    assert all(step["message"] == "" for step in steps)
    assert steps[0]["timestamp"] == "2026-11-10T09:00:01Z"
    assert steps[0]["extra"]["stira"] == {"prompt_chars": 12, "image_count": 0}
    assert steps[1]["model_name"] == "test-model"
    assert steps[1]["metrics"] == {"prompt_tokens": 100, "completion_tokens": 20}
    assert steps[1]["tool_calls"] == [
        {"tool_call_id": "tooluse_01", "function_name": "file_read", "arguments": {}}
    ]
    assert steps[1]["observation"] == {
        "results": [{"source_call_id": "tooluse_01", "content": "done"}]
    }
    assert steps[1]["extra"]["stira"] == {
        "call_id": CALL_1,
        "duration_ms": 800,
        "stop": "tool_use",
        "status": "success",
        "tool_results": [{"tool_call_id": "tooluse_01", "status": "done", "duration_ms": 40}],
    }
    assert "tool_calls" not in steps[2] and "observation" not in steps[2]
    assert document["final_metrics"] == {
        "total_prompt_tokens": 250,
        "total_completion_tokens": 50,
        "total_steps": 3,
        "extra": {"stira": {"stop": "end_turn", "error": False}},
    }
    assert document["extra"]["stira"] == {
        "export_version": 1,
        "turn_id": TURN,
        "bodies": "excluded",
        "context_trust": "unknown",
        "approvals": [
            {"event": "requested", "request_id": "req-1"},
            {"event": "resolved", "request_id": "req-1", "decision": "approved"},
        ],
        "breaker_transitions": [{"from": "NORMAL", "to": "WARNING"}],
        "unmapped_events": {"model.started": 1, "turn.summary": 1},
        "skipped_events": 0,
    }


def test_build_is_deterministic_and_independent_of_row_order():
    events = _turn()
    expected = atif_export.build(events)
    shuffled = random.Random(700)
    for _ in range(5):
        shuffled.shuffle(events)
        assert atif_export.build(events) == expected


def test_output_is_an_allow_list_bodies_previews_and_secrets_never_appear():
    events = [
        _event(1, "turn.started", {"prompt_chars": 5, "prompt": CANARY, "auto_continue": CANARY}),
        _event(
            2,
            "model.completed",
            {
                "call_id": CANARY,
                "model": f"m {CANARY}",
                "duration_ms": 1,
                "stop": CANARY,
                "status": CANARY,
                "text": CANARY,
            },
        ),
        _event(
            3,
            "tool_call.updated",
            {
                "tool_use_id": CANARY,
                "name": f"run {CANARY}",
                "status": "error",
                "error_class": "exception",
                "exception_type": CANARY,
                "result_code": CANARY,
                "input_preview": f"password={CANARY}",
                "output_preview": f"body {CANARY}",
            },
        ),
        _event(4, "approval.resolved", {"request_id": f"r {CANARY}", "decision": f"x {CANARY}"}),
        _event(5, "breaker.transition", {"from": f"a {CANARY}", "to": "WARNING"}),
        _event(6, "progress.report", {"report_id": "r1", "trigger": CANARY, "end": CANARY}),
        _event(
            7,
            "turn.completed",
            {
                "usage": {"input_tokens": CANARY},
                "stop": f"s {CANARY}",
                "error": True,
                "reason": CANARY,
                "usage_reason": CANARY,
                "failure": CANARY,
                "failure_host": CANARY,
                "model": f"m {CANARY}",
            },
        ),
    ]
    events[0]["message"] = CANARY
    document = atif_export.build(events)
    text = json.dumps(document)
    assert "CANARY" not in text and "password" not in text and "body" not in text
    step = document["steps"][1]
    assert step["tool_calls"] == [
        {"tool_call_id": "call-1", "function_name": "unnamed_tool", "arguments": {}}
    ]
    assert step["extra"]["stira"]["tool_results"] == [
        {"tool_call_id": "call-1", "status": "error", "error_class": "exception"}
    ]
    assert document["final_metrics"] == {"total_steps": 2, "extra": {"stira": {"error": True}}}
    assert document["extra"]["stira"]["approvals"] == [{"event": "resolved"}]
    assert document["extra"]["stira"]["breaker_transitions"] == [{"to": "WARNING"}]


def test_the_name_of_an_unknown_tool_is_not_exported():
    events = [
        _event(1, "turn.started", {}),
        _tool(2, "tool_call.created", "running", name="delete_everything_now"),
        _tool(
            3,
            "tool_call.updated",
            "error",
            name="delete_everything_now",
            error_class="unknown_tool",
        ),
        _event(4, "turn.completed", {"usage": {}, "stop": "end_turn", "error": False}),
    ]
    document = atif_export.build(events)
    assert "delete_everything_now" not in json.dumps(document)
    assert document["steps"][1]["tool_calls"][0]["function_name"] == "unknown_tool"


def test_an_automatic_stop_keeps_its_fixed_reason():
    # #680 (ADR 0034): the automatic stop's codes pass the reason allowlist.
    for reason in ("yolo_window_ended", "approval_unanswered", "unattended_token_limit"):
        events = [
            _event(1, "turn.started", {}),
            _event(2, "turn.completed", {"usage": {}, "stop": "cancelled", "reason": reason}),
        ]
        outcome = atif_export.build(events)["final_metrics"]["extra"]["stira"]
        assert (outcome["stop"], outcome["reason"]) == ("cancelled", reason)


def test_an_untrusted_turn_is_refused_unless_asked_for():
    with pytest.raises(atif_export.ExportRefused) as refused:
        atif_export.build(_turn(taint="untrusted"))
    assert refused.value.code == "turn_untrusted"
    document = atif_export.build(_turn(taint="untrusted"), include_untrusted=True)
    assert document["extra"]["stira"]["context_trust"] == "untrusted"
    assert atif_export.build(_turn(taint="trusted"))["extra"]["stira"]["context_trust"] == (
        "trusted"
    )


def test_refusals_are_fixed_codes():
    cases = {
        "no_events": [],
        "turn_not_completed": _turn()[:-1],
        "turn_id_mismatch": [*_turn(), _event(1, "turn.started", {}, turn="b" * 32)],
        "turn_id_unsafe": [
            _event(1, "turn.completed", {"usage": {}, "stop": "end_turn"}, turn="a b")
        ],
    }
    for code, events in cases.items():
        with pytest.raises(atif_export.ExportRefused) as refused:
            atif_export.build(events)
        assert refused.value.code == code
        assert code in atif_export.REFUSALS
    # An event the turn-event contract refuses is skipped and counted, not exported.
    document = atif_export.build([*_turn(), None, {"type": "x"}])
    assert document["extra"]["stira"]["skipped_events"] == 2


def _data_dir(tmp_path, *, taint=None):
    data_dir = tmp_path / "data"
    store = TurnEventStore(data_dir / "turns.db")
    try:
        for event in _turn(taint=taint):
            store.append(event["turn_id"], event["type"], event["data"])
        store.append("b" * 32, "turn.started", {"prompt_chars": 1, "image_count": 0})
    finally:
        store.close()
    return data_dir


def _listing(directory: Path) -> dict[str, int]:
    return {
        str(path.relative_to(directory)): path.stat().st_mtime_ns
        for path in sorted(directory.rglob("*"))
    }


def test_cli_writes_one_new_file_outside_the_data_directory(tmp_path, capsys):
    data_dir = _data_dir(tmp_path)
    before = _listing(data_dir)
    out = tmp_path / "turn.atif.json"
    assert atif_export_cli.main(["--data-dir", str(data_dir), "--out", str(out)]) == 0
    assert _listing(data_dir) == before  # nothing written into the data directory
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["session_id"] == TURN  # the last turn that has ended, not the open one
    assert [step["source"] for step in document["steps"]] == ["user", "agent", "agent"]
    assert "file body" not in out.read_text(encoding="utf-8")
    assert "notes.md" not in out.read_text(encoding="utf-8")
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == f"wrote 3 step(s) of turn {TURN}\n"
    # The file is never overwritten.
    assert atif_export_cli.main(["--data-dir", str(data_dir), "--out", str(out)]) == 1
    assert capsys.readouterr().err == "error: out_exists\n"


def test_cli_refuses_with_a_code_and_writes_nothing(tmp_path, capsys):
    data_dir = _data_dir(tmp_path, taint="untrusted")
    out = tmp_path / "out.json"
    base = ["--data-dir", str(data_dir)]
    cases = [
        (["--data-dir", str(tmp_path / "none"), "--out", str(out)], "data_dir_missing"),
        ([*base, "--out", str(data_dir / "x.json")], "out_inside_data_dir"),
        ([*base, "--out", str(out), "--turn-id", "c" * 32], "turn_not_found"),
        ([*base, "--out", str(out), "--turn-id", "b" * 32], "turn_not_completed"),
        ([*base, "--out", str(out)], "turn_untrusted"),
    ]
    for argv, code in cases:
        assert atif_export_cli.main(argv) == 1
        assert capsys.readouterr().err == f"error: {code}\n"
        assert not out.exists() and not (data_dir / "x.json").exists()
        assert code in atif_export.REFUSALS | atif_export_cli.PROBLEMS
    empty = tmp_path / "empty"
    empty.mkdir()
    assert atif_export_cli.main(["--data-dir", str(empty), "--out", str(out)]) == 1
    assert capsys.readouterr().err == "error: turns_db_missing\n"
    assert atif_export_cli.main([*base, "--out", str(out), "--include-untrusted"]) == 0
    assert json.loads(out.read_text())["extra"]["stira"]["context_trust"] == "untrusted"


def test_the_export_modules_import_no_model_network_or_memory_module():
    allowed = {
        "__future__",
        "argparse",
        "collections.abc",
        "datetime",
        "json",
        "os",
        "pathlib",
        "re",
        "shutil",
        "sqlite3",
        "sys",
        "tempfile",
        "typing",
        "stiracore",
        "stiracore.observability",
        ".hooks",
        ".model_progress",
        ".security",
        ".turn_autostop",  # #680: the automatic stop's fixed codes (time / typing only)
        ".turn_events",
    }
    for module in (atif_export, atif_export_cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
        assert imported <= allowed, sorted(imported - allowed)
