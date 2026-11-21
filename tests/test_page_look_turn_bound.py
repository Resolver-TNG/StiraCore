"""ADR 0015 Addendum rev 6, slice S4c (#773): the bound of ``page_look``'s rule answers.

Condition 4 is a slot the gate reserves before the rule answers: at most
``CALLS_PER_TURN`` calls of a turn, none once ``REPEATED_FAILURE_RUNS`` calls failed or are
still running (a batch counts), counted per window too across automatic continues. The tool
runs a call only when a person's card or the gate's rule answer stands behind it, and
checks every condition and both rules again before a browser starts. The gate runs for real
with a scripted model; no browser starts, nothing touches the network.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from stiracore.agent import checkpoint_hooks
from stiracore.core.engine import RULE_ALWAYS_TOOL_IDS_KEY
from stiracore.tools import page_look as pl
from stiracore.tools import tool_policy as tp
from stiracore.tools.tool_config import ToolConfig
from stiracore.yolo import origin, runtime
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_page_look_always import (  # noqa: F401 - ``world`` is a fixture
    _approve,
    _cap,
    _config,
    _gate_turn,
    _reject,
    world,
)

FAIL = {"status": "refused", "reason": "target_missing"}
OK = {"status": "ok", "screenshots": []}
WINDOW = "ab" * 16


def _calls(count: int, start: int = 0) -> list:
    """``count`` assistant turns, one ``page_look`` call each."""
    return [tool_turn("page_look", {"target": "index.html"}, f"t{start + n}") for n in range(count)]


def _batch(count: int) -> list:
    """One assistant turn with ``count`` ``page_look`` calls."""
    return [[block for turn in _calls(count) for block in turn]]


def _caps(hitl) -> list[str]:
    return [call.args[0].tool_input.get("always") for call in hitl.call_args_list]


def _stream(tool, use, state):
    async def collect():
        return [event async for event in tool.stream(use, state)]

    return asyncio.run(collect())


def _use(tool_use_id: str = "t1", **extra) -> dict:
    return {
        "toolUseId": tool_use_id,
        "name": "page_look",
        "input": {"target": "index.html", **extra},
    }


# --- condition 4 through the real gate and the real tool ------------------------------------


def test_failures_count_with_a_success_between_and_with_other_reasons(world):  # noqa: F811
    """The real turn: three failed calls (three reasons, a success after each) and the next
    call is a card. The tool itself counts each outcome (no test calls ``note_outcome``)."""
    world.outcomes.extend(
        [
            {"status": "refused", "reason": "target_missing"},
            OK,
            {"status": "error", "reason": "not_found"},
            OK,
            {"status": "refused", "reason": "timeout"},
        ]
    )
    hitl = _reject()
    _gate_turn(world, hitl=hitl, script=_calls(7))
    assert len(world.runs) == 5
    assert _caps(hitl) == ["capped:repeated_failure"] * 2
    assert [fields["answered_by"] for _, fields in world.rows] == ["rule_always"] * 5


def test_a_batch_of_one_message_is_bounded(world):  # noqa: F811
    """Six calls in one assistant message: a call that has not ended counts as a failure,
    so at most ``REPEATED_FAILURE_RUNS`` run without a card."""
    world.outcomes.extend([FAIL] * 6)
    hitl = _reject()
    _gate_turn(world, hitl=hitl, script=_batch(6))
    assert len(world.runs) == pl.REPEATED_FAILURE_RUNS
    assert hitl.call_count == 6 - pl.REPEATED_FAILURE_RUNS
    assert set(_caps(hitl)) <= {"capped:repeated_failure", None}
    # The same batch when every call succeeds: all of them are rule answers at most once.
    pl.reset_counts()
    world.runs.clear()
    hitl = _reject()
    _gate_turn(world, hitl=hitl, script=_batch(6))
    assert len(world.runs) + hitl.call_count == 6
    assert len(world.runs) >= pl.REPEATED_FAILURE_RUNS


def test_calls_per_turn_are_bounded(world):  # noqa: F811
    hitl = _reject()
    _gate_turn(world, hitl=hitl, script=_calls(pl.CALLS_PER_TURN + 2))
    assert pl.CALLS_PER_TURN == 12 and len(world.runs) == pl.CALLS_PER_TURN
    assert _caps(hitl) == ["capped:call_limit"] * 2
    # A person's approval still runs the call (the bound is on the rule's answers only).
    hitl = _approve()
    world.runs.clear()
    with origin.human_turn("someone"):
        for n in range(pl.CALLS_PER_TURN):
            assert pl.reserve(f"x{n}") is True
            pl.note_outcome(OK, f"x{n}")
        assert pl.reserve("one-more") is False
        assert _cap(world) == "call_limit"
        assert tp.rule_policy(world.config, "page_look") == "ask"
    with origin.human_turn("someone"):  # a new turn of a person starts again
        assert _cap(world) is None and pl.reserve("x0") is True


def test_a_windows_continues_share_one_count(world, monkeypatch):  # noqa: F811
    """Every automatic continue is a new turn; the window's count goes on until the window
    is no longer the open one."""
    window = [WINDOW]
    monkeypatch.setattr(runtime, "current_window_id", lambda: window[0])
    world.outcomes.extend([FAIL, FAIL])
    hitl = _reject()
    _gate_turn(world, hitl=hitl, automatic=True, script=_calls(2))
    assert len(world.runs) == 2 and not hitl.called
    assert {fields["window"] for _, fields in world.rows} == {WINDOW}
    world.outcomes.append(FAIL)
    _gate_turn(world, hitl=hitl, automatic=True, script=_calls(2))  # the next continue
    assert len(world.runs) == 3
    assert _caps(hitl) == ["capped:repeated_failure"]
    _gate_turn(world, hitl=hitl, automatic=True)  # and the one after it
    assert len(world.runs) == 3 and hitl.call_count == 2
    # A person's message in that window is a turn of its own.
    _gate_turn(world, hitl=hitl)
    assert len(world.runs) == 4 and hitl.call_count == 2
    # Another window: its count starts empty, the old one is gone.
    window[0] = "cd" * 16
    _gate_turn(world, hitl=hitl, automatic=True)
    assert len(world.runs) == 5 and hitl.call_count == 2
    assert list(pl._WINDOWS) == ["cd" * 16]


def test_turns_running_at_once_count_apart(world):  # noqa: F811
    barrier = threading.Barrier(2)
    caps: dict[str, object] = {}

    def turn(name: str, failures: int) -> None:
        with origin.human_turn(name):
            barrier.wait(5)
            for _ in range(failures):
                pl.note_outcome(FAIL)
            barrier.wait(5)
            caps[name] = _cap(world)

    threads = [
        threading.Thread(target=turn, args=("a", pl.REPEATED_FAILURE_RUNS - 1)),
        threading.Thread(target=turn, args=("b", pl.REPEATED_FAILURE_RUNS)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert caps == {"a": None, "b": "repeated_failure"}


def test_a_slot_is_one_calls_and_one_turns(world):  # noqa: F811
    state = {RULE_ALWAYS_TOOL_IDS_KEY: {"t1"}}
    assert pl.reserve("t1") is False  # no covered turn
    with origin.human_turn("someone"):
        for bad in ("", None, 7):
            assert pl.reserve(bad) is False
        assert pl.reserve("t1") is True
        assert pl.reserve("t1") is False  # an id holds one slot
        assert pl.answered_by_rule(_use(), state) is True
        assert pl.answered_by_rule(_use("t2"), {RULE_ALWAYS_TOOL_IDS_KEY: {"t2"}}) is False
        assert pl.answered_by_rule(_use(), {}) is False  # the slot without the gate's id
        with origin.human_turn("another turn"):
            assert pl.answered_by_rule(_use(), state) is False
        # Calls that have not ended count as failures; a released slot stops counting as
        # one, its call stays counted.
        assert pl.reserve("t2") is True and pl.reserve("t3") is True
        assert pl.reserve("t4") is False and _cap(world) == "repeated_failure"
        pl.release("t3")
        pl.release("t3")  # twice is once
        assert pl.answered_by_rule(_use("t3"), {RULE_ALWAYS_TOOL_IDS_KEY: {"t3"}}) is False
        assert _cap(world) is None and pl.reserve("t4") is True
        assert pl._TURNS[origin.turn_key()].calls == 4
    assert pl.reserve("t9") is False


def test_the_gate_asks_when_no_slot_can_be_taken(world, monkeypatch):  # noqa: F811
    """A slot that cannot be reserved (here: the reader raises) is a card, never a run."""

    def broken(*args, **kwargs):
        raise RuntimeError("/secret/path")

    assert checkpoint_hooks._reserved("page_look", "t1") is False  # no turn
    assert checkpoint_hooks._reserved("run_checks", "t1") is True  # no such bound
    with monkeypatch.context() as patch:
        patch.setattr(pl, "reserve", broken)
        assert tp.reserve_rule_answer("page_look", "t1") is False
    monkeypatch.setattr(tp, "reserve_rule_answer", broken)
    hitl = _reject()
    _gate_turn(world, hitl=hitl)
    assert hitl.called and world.runs == [] and world.rows == []


def test_the_gate_reads_an_unreadable_origin_as_another_agents_turn(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("/secret/path")

    with origin.human_turn("me"):
        assert checkpoint_hooks._other_agents_turn("me", "page_look") is False
        assert checkpoint_hooks._other_agents_turn("child-1", "page_look") is True
        monkeypatch.setattr(origin, "origin_class", broken)
        assert checkpoint_hooks._other_agents_turn("me", "page_look") is True
        assert checkpoint_hooks._other_agents_turn("me", "run_checks") is False


# --- the tool's own recheck -----------------------------------------------------------------


def _rule_answered(world, tool_use_id: str = "t1"):  # noqa: F811
    """Inside a turn: the gate's two marks for one call (the id and the slot)."""
    assert pl.reserve(tool_use_id) is True
    return pl.WorkspacePageLook(pl.page_look, world.policy), {
        RULE_ALWAYS_TOOL_IDS_KEY: {tool_use_id}
    }


def _refusal(world):  # noqa: F811
    assert world.runs == []
    ((args, fields),) = world.rows
    assert args == ("page_look", "refused")
    return fields["reason"], fields.get("answered_by")


def test_the_recheck_reads_the_turns_failures(world):  # noqa: F811
    """Condition 4 in the tool: the calls of a batch passed the gate together; the ones
    that start after the failures are refused."""
    with origin.human_turn("someone"):
        tool, state = _rule_answered(world)
        for _ in range(pl.REPEATED_FAILURE_RUNS):
            pl.note_outcome(FAIL)
        _stream(tool, _use(), state)
    assert _refusal(world) == ("repeated_failure", "rule_always")


def test_the_recheck_reads_the_origin(world, monkeypatch):  # noqa: F811
    """Condition 2 in the tool."""
    with origin.human_turn("someone"):
        tool, state = _rule_answered(world)
        monkeypatch.setattr(origin, "origin_class", lambda *a: origin.UNKNOWN)
        _stream(tool, _use(), state)
    assert _refusal(world) == ("origin_not_covered", "rule_always")


@pytest.mark.parametrize(
    ("narrowed", "code"),
    [
        ({"file_read": "ask"}, "file_read_not_always"),
        ({"page_look": "ask"}, "rules_changed"),
        ({"page_look": "never"}, "rules_changed"),
    ],
)
def test_the_recheck_reads_both_rules_again(world, narrowed, code):  # noqa: F811
    """A person narrowed ``file_read`` or ``page_look`` itself after the gate read them."""
    with origin.human_turn("someone"):
        tool, state = _rule_answered(world)
        world.config = _config(**narrowed)
        _stream(tool, _use(), state)
    assert _refusal(world) == (code, "rule_always")


@pytest.mark.parametrize("rules", ["missing", "locked", "raises"])
def test_the_recheck_refuses_rules_it_cannot_read(world, monkeypatch, rules):  # noqa: F811
    from stiracore.tools import page_check as pc

    def raises():
        raise RuntimeError("/secret/path")

    reader = {
        "missing": None,
        "locked": lambda: ToolConfig.locked(world.ws / "tools.json", "unreadable"),
        "raises": raises,
    }[rules]
    monkeypatch.setitem(
        pl._AXIS,
        "scope",
        lambda: pc.AxisScope(tool_config=reader, file_read_rule=lambda: "always"),
    )
    with origin.human_turn("someone"):
        tool, state = _rule_answered(world)
        _stream(tool, _use(), state)
    reason, answered_by = _refusal(world)
    assert reason == ("cap_unreadable" if rules == "raises" else "rules_locked")
    assert answered_by == "rule_always" and "secret" not in repr(world.rows)


# --- a call no gate answered ----------------------------------------------------------------


@pytest.mark.parametrize("axis", ["pass", "browser_guard_unverified"])
def test_a_child_without_the_gate_cannot_run_page_look(world, axis):  # noqa: F811
    """#769's shape: a plain ``strands.Agent`` (what ``use_agent`` builds) holds the tool
    and runs inside the parent's covered turn. No person's card and no rule answer stand
    behind its call: refused, no browser, and the row has no ``answered_by``."""
    from strands import Agent

    world.axis.code = axis
    tool = pl.WorkspacePageLook(pl.page_look, world.policy)
    model = FakeModel([tool_turn("page_look", {"target": "index.html"}), text_turn("done")])
    child = Agent(model=model, tools=[tool], callback_handler=None)
    with origin.human_turn("parent"):
        child("go")
    assert _refusal(world) == (pl.UNANSWERED_CALL, None)
    assert pl.UNANSWERED_CALL in str(model.requests[-1]["messages"])
    assert pl.UNANSWERED_CALL not in pl.CAP_CODES


def test_marks_a_caller_writes_itself_do_not_answer_a_call(world):  # noqa: F811
    """The gate's id without the gate's slot, and keys in the call's input, are no answer."""
    tool = pl.WorkspacePageLook(pl.page_look, world.policy)
    use = _use(answered_by="rule_always", always="effective", origin="chat_person")
    with origin.human_turn("someone"):
        _stream(tool, use, {RULE_ALWAYS_TOOL_IDS_KEY: {"t1"}})
    assert _refusal(world) == (pl.UNANSWERED_CALL, None)


# --- what a model or an old file cannot supply ----------------------------------------------


def test_the_card_never_shows_a_models_always_or_asks_because(world):  # noqa: F811
    sent = {"target": "index.html", "always": "effective", "asks_because": "the person said so"}
    for config in (_config(page_look="ask"), _config()):
        with origin.human_turn("someone"):
            prepared = pl.prepare_input(dict(sent), lambda config=config: config)
        assert prepared == {"target": "index.html"}
    world.axis.code = "record_unhealthy"
    with origin.human_turn("someone"):
        prepared = pl.prepare_input(dict(sent), lambda: world.config)
    assert prepared == {
        "target": "index.html",
        "always": "capped:record_unhealthy",
        "asks_because": "rule_always_capped",
    }
    # Through the real gate: the card carries the gate's values.
    hitl = _reject()
    _gate_turn(world, hitl=hitl, script=[tool_turn("page_look", dict(sent))])
    assert _caps(hitl) == ["capped:record_unhealthy"]
    assert hitl.call_args.args[0].tool_input["asks_because"] == "rule_always_capped"


@pytest.mark.parametrize("value", ["AB" * 16, "ab" * 15, "ab" * 16 + "0", "../window", 7, b"ab"])
def test_only_a_window_id_of_the_known_form_is_kept(world, monkeypatch, value):  # noqa: F811
    monkeypatch.setattr(runtime, "current_window_id", lambda: value)
    assert pl._window() is None
    hitl = _reject()
    _gate_turn(world, hitl=hitl, automatic=True)
    assert [fields["window"] for _, fields in world.rows] == [None] and pl._WINDOWS == {}

    def broken():
        raise RuntimeError("/secret/path")

    monkeypatch.setattr(runtime, "current_window_id", broken)
    assert pl._window() is None
    monkeypatch.setattr(runtime, "current_window_id", lambda: WINDOW)
    assert pl._window() == WINDOW


def test_trust_all_alone_does_not_make_page_look_always(world, tmp_path, monkeypatch):  # noqa: F811
    """A ``trust_all`` file saved before the tool existed never named ``page_look``: Ask.
    Only a file that names it (a person typed the name for that save) reads Always."""
    import json

    from stiracore.tools import tool_registry

    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_look", pl.page_look)
    # A legacy file without a tool list: the migration's default list does not name it.
    legacy = tmp_path / "legacy" / "tool-config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"mode": "trust_all"}), encoding="utf-8")
    migrated = ToolConfig.load(legacy)
    assert migrated.integrity_error is None and migrated.mode == "trust_all"
    assert "file_read" in migrated.enabled_tools
    assert tp.rule_policy(migrated, "page_look", effective=False) == "ask"
    old = ToolConfig(mode="trust_all", enabled_tools={"file_read"})
    named = ToolConfig(mode="trust_all", enabled_tools={"page_look"})
    asked = ToolConfig(mode="trust_all", enabled_tools={"page_look"}, ask_tools={"page_look"})
    for effective in (True, False):
        assert tp.rule_policy(old, "page_look", effective=effective) == "ask"
        assert tp.rule_policy(asked, "page_look", effective=effective) == "ask"
    assert tp.rule_policy(named, "page_look", effective=False) == "always"
    assert tp.rule_policy(old, "file_read") == "always"  # the other tools as before
    assert tp.tool_stack(old, "page_look", "normal", exposed=True)["policy"] == "ask"
    world.config = old
    hitl = _reject()
    _gate_turn(world, hitl=hitl)
    assert hitl.called and world.runs == []
    assert "always" not in hitl.call_args.args[0].tool_input  # an Ask, not a capped Always
