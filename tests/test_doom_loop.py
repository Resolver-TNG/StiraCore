"""#684: tool-call level repetition inside one turn (the pure detector and its hook)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.grants import progress as grant_progress
from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.progress import doom_loop
from stiracore.progress.doom_loop import (
    FAILURE_ACTIONS,
    FAILURE_CLASSES,
    NOTE_MARKER,
    STOP_TEXT,
    Call,
    DoomLoopHook,
    classify_failure,
    detect,
)
from stiracore.tools import tool_registry
from stiracore.tools.identity_tools import PATH_WRITE_TOOLS
from stiracore.tools.run_checks import content_for_model
from tests.provider_fakes import FakeModel, text_turn, tool_turn


def _write(target: str, args: str = "", failure: str = "") -> Call:
    return Call("file_write", args or f"w:{target}", target, True, failure)


def _other(args: str, failure: str = "", tool: str = "shell") -> Call:
    return Call(tool, args, "", False, failure)


# --- the constants and the table ----------------------------------------------------------


def test_thresholds_are_the_grant_detectors_numbers():
    assert doom_loop.WARN_AFTER == grant_progress.SAME_BLOCKER_LIMIT == 3
    assert doom_loop.STOP_AFTER == grant_progress.LOOP_WINDOW == 6
    assert doom_loop.SAME_FAILURE_LIMIT == grant_progress.NO_ACTION_LIMIT == 2
    assert doom_loop.FAILURE_STOP_AFTER == grant_progress.SAME_BLOCKER_LIMIT
    assert doom_loop.RULES == ("loop_detected", "same_blocker")
    assert doom_loop.WRITE_TOOLS == PATH_WRITE_TOOLS


def test_failure_classes_map_to_one_action_each():
    assert FAILURE_ACTIONS == {
        "timeout": "wait_retry",
        "invalid_args": "repair_args",
        "missing_info": "gather_info",
        "test_failed": "fix_code",
        "permission_denied": "ask_person",
        "other": "change_approach",
    }
    assert tuple(FAILURE_ACTIONS) == FAILURE_CLASSES


def _error(text: str) -> dict:
    return {"status": "error", "content": [{"text": text}]}


@pytest.mark.parametrize(
    ("tool", "result", "expected"),
    [
        ("shell", _error("Command timed out after 30 s"), "timeout"),
        ("editor", _error("[Errno 13] Permission denied: 'x'"), "permission_denied"),
        ("file_write", _error("nope [refused by: floor; rule=x]"), "permission_denied"),
        ("file_read", _error("[Errno 2] No such file or directory"), "missing_info"),
        ("editor", _error("Validation failed: path must be a string"), "invalid_args"),
        ("shell", _error("exit 1"), "other"),
        ("shell", {"status": "success", "content": [{"text": "permission denied"}]}, ""),
        (
            "run_checks",
            {
                "status": "success",
                "content": content_for_model({"status": "failed", "pad": "x" * 9000}),
            },
            "test_failed",
        ),
        (
            "run_checks",
            {"status": "success", "content": content_for_model({"status": "passed"})},
            "",
        ),
        ("shell", _error("Connection refused"), "other"),
        ("shell", None, ""),
    ],
)
def test_classify_failure(tool, result, expected):
    assert classify_failure(tool, result) == expected


# --- the pure detector --------------------------------------------------------------------


def test_same_file_edits_get_a_notice_at_the_third_and_stop_at_the_sixth():
    calls = [_write("a", f"edit-{i}") for i in range(6)]
    assert detect(calls[:1]) is None
    assert detect(calls[:2]) is None
    notice = detect(calls[:3])
    assert (notice.rule, notice.level, notice.count) == ("loop_detected", "notice", 3)
    assert (notice.action, notice.tried) == ("change_approach", ())
    stop = detect(calls)
    assert (stop.rule, stop.level, stop.count, stop.action) == ("loop_detected", "stop", 6, "stop")
    assert stop.tried == (("file_write", 6, 0),)


def test_reads_between_edits_do_not_hide_the_loop_but_another_file_starts_over():
    looping = [_write("a"), _other("read"), _write("a"), _other("read"), _write("a")]
    assert detect(looping).rule == "loop_detected"
    alternating = [_write("a"), _write("a"), _write("b"), _write("a")]
    assert detect(alternating) is None
    assert detect([_write("a"), _write("b"), _write("c"), _write("d")]) is None


def test_a_repeated_call_that_succeeds_is_never_a_finding():
    assert detect([_other("pytest")] * 10) is None


def test_the_same_failure_twice_is_a_notice_and_three_times_a_stop():
    fail = _other("pytest", "timeout")
    assert detect([fail]) is None
    notice = detect([fail, _other("ls"), fail])
    assert (notice.rule, notice.level, notice.count) == ("same_blocker", "notice", 2)
    assert (notice.failure, notice.action) == ("timeout", "wait_retry")
    stop = detect([fail, fail, fail])
    assert (stop.level, stop.action, stop.tried) == ("stop", "stop", (("shell", 3, 3),))


def test_a_change_between_two_failures_starts_the_failure_count_over():
    fail = _other("pytest", "test_failed", tool="run_checks")
    assert detect([fail, _write("a"), fail]) is None  # the code was edited in between
    assert detect([fail, _other("pytest", tool="run_checks"), fail]) is None  # it passed once
    assert detect([fail, _other("pytest", "timeout", tool="run_checks"), fail]) is None
    assert detect([fail, _other("other-args", "test_failed", tool="run_checks")]) is None


def test_thresholds_are_parameters():
    calls = [_write("a")] * 2
    assert detect(calls, warn_after=2, stop_after=4).level == "notice"
    assert detect(calls * 2, warn_after=2, stop_after=4).level == "stop"


def test_a_finding_shows_codes_counts_and_tool_names_only():
    finding = detect([_write("secret-target-key", "secret-args-key")] * 6)
    shown = finding.as_dict()
    assert set(shown) == {"rule", "level", "count", "tool", "failure", "action", "tried"}
    assert "secret" not in json.dumps(shown)
    odd = detect([Call("not a name!", "k", "t", True)] * 3)
    assert odd.tool == "other"


# --- the hook on bare events --------------------------------------------------------------


def _event(name: str, tool_input: dict, result: dict | None = None, use_id: str = "u"):
    return SimpleNamespace(
        tool_use={"toolUseId": use_id, "name": name, "input": tool_input},
        result=result or {"status": "success", "content": [{"text": "ok"}]},
        cancel_tool=False,
    )


def test_hook_by_default_only_reports_and_holds_no_path_or_argument():
    seen = []
    hook = DoomLoopHook(seen.append)
    path = "/work/sk-test-secretlooking/file.py"
    for index in range(8):
        event = _event("file_write", {"path": path, "content": f"v{index}"}, use_id=f"u{index}")
        hook.before_tool(event)
        assert event.cancel_tool is False
        before = event.result
        hook.after_tool(event)
        assert event.result is before  # no note, nothing replaced
    assert [(f.level, f.count) for f in seen] == [("notice", 3), ("stop", 6)]
    kept = repr(hook.__dict__) + repr([f.as_dict() for f in seen])
    assert "secretlooking" not in kept and "v3" not in kept


def test_editor_views_are_not_edits_and_a_new_turn_starts_over():
    seen = []
    hook = DoomLoopHook(seen.append)
    for _ in range(5):
        hook.after_tool(_event("editor", {"command": "view", "path": "/w/a.py"}))
    for _ in range(2):
        hook.after_tool(_event("editor", {"command": "insert", "path": "/w/a.py"}))
    hook.begin(SimpleNamespace())
    for _ in range(2):
        hook.after_tool(_event("editor", {"command": "insert", "path": "/w/a.py"}))
    assert seen == []


def test_a_failing_sink_or_a_broken_event_never_raises():
    def boom(_finding):
        raise RuntimeError("sk-test-do-not-log")

    hook = DoomLoopHook(boom, stop=True)
    for _ in range(3):
        hook.after_tool(_event("file_write", {"path": "/w/a", "content": "x"}))
    hook.after_tool(SimpleNamespace(tool_use=None, result=None))
    hook.before_tool(SimpleNamespace(tool_use=None, cancel_tool=False))


def test_stop_never_replaces_another_hooks_cancel():
    hook = DoomLoopHook(stop=True, warn_after=2, stop_after=2)
    for _ in range(2):
        hook.after_tool(_event("file_write", {"path": "/w/a", "content": "x"}))
    event = _event("file_write", {"path": "/w/a", "content": "y"})
    event.cancel_tool = "refused by the gate"
    hook.before_tool(event)
    assert event.cancel_tool == "refused by the gate"
    mine = _event("file_write", {"path": "/w/a", "content": "y"})
    hook.before_tool(mine)
    assert mine.cancel_tool == STOP_TEXT
    elsewhere = _event("file_write", {"path": "/w/b", "content": "y"})
    hook.before_tool(elsewhere)
    assert elsewhere.cancel_tool is False


# --- a scripted model through a real agent turn -------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _run(ws: Path, script: list, hook: DoomLoopHook):
    policy = WritePathPolicy(base_dir=ws, write_roots=[WORKSPACE_ROOT])
    model = FakeModel(script)
    agent = NXPAgent(
        model=model,
        tools=tool_registry.get_tools(write_policy=policy, include_exec=False),
        write_policy=policy,
        stimulus_classifier=Mock(classify=lambda **k: None),
        extra_hooks=[hook],
    )
    agent("go")
    return [
        block["toolResult"]
        for message in model.requests[-1]["messages"]
        for block in message["content"]
        if "toolResult" in block
    ]


def _text(result: dict) -> str:
    return "\n".join(block.get("text", "") for block in result["content"])


def _edits(path: Path, count: int) -> list:
    return [
        tool_turn("file_write", {"path": str(path), "content": f"version {i}\n"}, f"u{i}")
        for i in range(count)
    ]


def test_scripted_model_editing_one_file_is_noticed_at_n_and_stopped_at_the_limit(ws):
    """The issue's acceptance: with both switches on, the N-th edit carries the notice and
    the calls after the limit are not run."""
    seen = []
    hook = DoomLoopHook(seen.append, warn_after=3, stop_after=4, nudge=True, stop=True)
    target = ws / "loop.txt"
    results = _run(ws, [*_edits(target, 6), text_turn("I stop.")], hook)
    assert [r["status"] for r in results[:4]] == ["success"] * 4
    noted = [NOTE_MARKER in _text(r) for r in results]
    assert noted == [False, False, True, True, False, False]
    assert f"{NOTE_MARKER} loop_detected] edit 3 of the same file" in _text(results[2])
    assert [(r["status"], _text(r)) for r in results[4:]] == [("error", STOP_TEXT)] * 2
    assert target.read_text(encoding="utf-8") == "version 3\n"  # the fifth and sixth never ran
    assert [(f.rule, f.level, f.count) for f in seen] == [
        ("loop_detected", "notice", 3),
        ("loop_detected", "stop", 4),
    ]
    assert seen[-1].as_dict()["tried"] == [{"tool": "file_write", "calls": 4, "failed": 0}]


def test_scripted_model_default_switches_report_and_change_nothing(ws):
    seen = []
    target = ws / "loop.txt"
    results = _run(ws, [*_edits(target, 7), text_turn("done")], DoomLoopHook(seen.append))
    assert [r["status"] for r in results] == ["success"] * 7
    assert not any(NOTE_MARKER in _text(r) for r in results)
    assert target.read_text(encoding="utf-8") == "version 6\n"
    assert [(f.level, f.count) for f in seen] == [("notice", 3), ("stop", 6)]


def test_scripted_model_doing_ordinary_work_is_left_alone(ws):
    """Other files, and the same successful call repeated, with both switches on."""
    seen = []
    hook = DoomLoopHook(seen.append, nudge=True, stop=True)
    script = [
        tool_turn("file_write", {"path": str(ws / f"f{i % 4}.txt"), "content": "same\n"}, f"u{i}")
        for i in range(8)
    ]
    script += [tool_turn("file_read", {"path": str(ws / "f0.txt")}, f"r{i}") for i in range(4)]
    results = _run(ws, [*script, text_turn("done")], hook)
    assert [r["status"] for r in results] == ["success"] * 12
    assert not any(NOTE_MARKER in _text(r) for r in results)
    assert seen == []
