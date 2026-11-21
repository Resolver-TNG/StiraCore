"""#151: the repeated-failure indicator (event-based count, off by default)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask
from strands import tool

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.repeated_failure import RepeatedFailureHook
from stiracore.core.breaker_sim import _DiscardCheckpointStore
from stiracore.core.repeated_failure import (
    MAX_TRACKED_TOOLS,
    RepeatedFailureCounter,
    RepeatedFailureSettings,
    clean_summary,
)
from stiracore.eval import __main__ as cli
from stiracore.eval.conditions import CONDITIONS
from stiracore.eval.dataset import load_dataset
from stiracore.eval.metrics import METRICS, run_counts
from stiracore.eval.runners import Budget, run_nxp, run_scripted
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.providers.usage import Pricing
from tests.provider_fakes import FakeModel, text_turn, tool_turn

NOTICE_MARK = "repeated failure indicator"
METRIC = "forbidden_after_failure_rate"


# ------------------------------------------------------------------------ counter


def test_same_tool_errors_add_up_and_a_success_resets():
    counter = RepeatedFailureCounter(threshold=3)
    assert [counter.record("probe", False) for _ in range(2)] == [1, 2]
    assert counter.snapshot() == {"count": 2, "threshold": 3, "active": False}
    assert counter.calm_notice() == ""
    counter.record("probe", False)
    assert counter.snapshot() == {"count": 3, "threshold": 3, "active": True}
    assert NOTICE_MARK in counter.calm_notice()
    counter.record("probe", True)
    assert counter.count == 0 and counter.calm_notice() == ""


def test_counts_are_per_tool_and_reset_clears_another_task():
    counter = RepeatedFailureCounter(threshold=2)
    counter.record("a", False)
    counter.record("b", False)
    assert counter.count == 1 and not counter.active  # two tools, one error each
    counter.record("b", True)  # b's success does not clear a
    counter.record("a", False)
    assert counter.count == 2 and counter.active
    counter.reset()
    assert counter.snapshot() == {"count": 0, "threshold": 2, "active": False}


def test_notice_and_summary_never_carry_the_tool_name():
    counter = RepeatedFailureCounter(threshold=1)
    counter.record("sk-test-not-a-tool-name", False)
    assert "sk-test" not in counter.calm_notice()
    assert "sk-test" not in json.dumps(counter.snapshot())


def test_tracked_names_are_bounded():
    counter = RepeatedFailureCounter()
    for index in range(MAX_TRACKED_TOOLS * 3):
        counter.record(f"invented-{index}", False)
    assert len(counter._counts) <= MAX_TRACKED_TOOLS + 1


@pytest.mark.parametrize("kwargs", [{"enabled": 1}, {"threshold": 0}, {"threshold": True}])
def test_settings_are_validated(kwargs):
    with pytest.raises(ValueError):
        RepeatedFailureSettings(**kwargs)
    assert RepeatedFailureSettings().enabled is False


def test_clean_summary_is_an_allow_list():
    dirty = {"count": 4, "threshold": 3, "active": False, "tool": "shell", "text": "x"}
    assert clean_summary(dirty) == {"count": 4, "threshold": 3, "active": True}
    for bad in (None, "x", {}, {"count": "4", "threshold": 3}, {"count": -1, "threshold": 3}):
        assert clean_summary(bad) is None


# -------------------------------------------------------------------------- agent


@tool
def probe(fail: bool = False) -> str:
    """A harmless probe that fails on request."""
    if fail:
        raise RuntimeError("probe failed")
    return "probe ok"


def _failing_turn(start: int, count: int = 3):
    calls = [tool_turn("probe", {"fail": True}, f"t{start + i}") for i in range(count)]
    return [*calls, text_turn("stopped")]


def _agent(script, settings=None):
    model = FakeModel(script)
    agent = NXPAgent(
        model=model,
        tools=[probe],
        checkpoint_store=_DiscardCheckpointStore(),
        callback_handler=None,
        repeated_failure=settings,
    )
    return agent, model


def test_off_by_default_adds_no_hook_notice_or_summary_field():
    agent, model = _agent([*_failing_turn(0), text_turn("next")])
    assert not any(isinstance(hook, RepeatedFailureHook) for hook in agent._extra_hooks)
    agent("try")
    agent("again")
    assert all(NOTICE_MARK not in (r["system_prompt"] or "") for r in model.requests)
    assert "repeated_failure" not in agent.last_turn_summary
    disabled, _ = _agent([text_turn("ok")], RepeatedFailureSettings(enabled=False))
    assert disabled._repeated_failure is None


def test_threshold_reached_injects_the_calm_notice_on_the_next_turn():
    script = [
        *_failing_turn(0),
        text_turn("second"),
        tool_turn("probe", {"fail": False}, "t9"),
        text_turn("recovered"),
        text_turn("fourth"),
    ]
    agent, model = _agent(script, RepeatedFailureSettings(enabled=True, threshold=3))
    agent("try")
    assert agent.last_turn_summary["repeated_failure"] == {
        "count": 3,
        "threshold": 3,
        "active": True,
    }
    first_turn_requests = len(model.requests)
    assert all(NOTICE_MARK not in (r["system_prompt"] or "") for r in model.requests)

    agent("again")
    assert NOTICE_MARK in model.requests[first_turn_requests]["system_prompt"]

    agent("use it properly")  # the tool succeeds: the count clears
    assert agent.last_turn_summary["repeated_failure"]["count"] == 0
    seen = len(model.requests)
    agent("go on")
    assert NOTICE_MARK not in (model.requests[seen]["system_prompt"] or "")


def test_below_threshold_and_new_conversation_do_not_inject():
    script = [*_failing_turn(0, count=2), text_turn("second"), *_failing_turn(5), text_turn("x")]
    agent, model = _agent(script, RepeatedFailureSettings(enabled=True, threshold=3))
    agent("try")
    seen = len(model.requests)
    agent("again")
    assert NOTICE_MARK not in (model.requests[seen]["system_prompt"] or "")
    agent("once more")
    assert agent._repeated_failure.count == 5  # 2 + 3 across turns of one conversation
    agent.start_new_conversation()  # another task
    assert agent._repeated_failure.count == 0
    seen = len(model.requests)
    agent("fresh")
    assert NOTICE_MARK not in (model.requests[seen]["system_prompt"] or "")


def test_notice_follows_the_identity_notice_and_reaches_the_boundary_check():
    agent, _ = _agent([text_turn("ok")], RepeatedFailureSettings(enabled=True, threshold=1))
    agent._repeated_failure.record("probe", False)
    agent.state.stability = 0.4
    seen = []
    agent.send_boundary_check = lambda tools, notice: seen.append(notice)
    agent("hello")
    [notice] = seen
    assert notice.index("STIRA SAFETY NOTICE") < notice.index(NOTICE_MARK)


# --------------------------------------------------------- turn summary (#136 path)


def _chat(recorder, summary):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=lambda message: iter(["ok"]),
            turn_summary_provider=lambda: summary,
            turn_recorder=recorder,
        )
    )
    return app.test_client()


def test_turn_summary_event_and_turn_list_carry_the_allow_listed_indicator(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {
        "category": None,
        "deltas": {},
        "breaker": "normal",
        "repeated_failure": {"count": 3, "threshold": 3, "active": True, "tool": "shell"},
    }
    client = _chat(recorder, summary)
    client.post("/api/chat", json={"message": "hi"}).get_data(True)
    turn = client.get("/api/turns?limit=1").get_json()["turns"][0]
    expected = {"count": 3, "threshold": 3, "active": True}
    assert turn["summary"]["repeated_failure"] == expected
    events = client.get(f"/api/turns/{turn['turn_id']}/events").get_json()["events"]
    event = next(e for e in events if e["type"] == "turn.summary")
    assert event["data"]["repeated_failure"] == expected

    del summary["repeated_failure"]  # indicator off: the field is absent, not null
    client.post("/api/chat", json={"message": "hi"}).get_data(True)
    turn = client.get("/api/turns?limit=1").get_json()["turns"][0]
    assert "repeated_failure" not in turn["summary"]


# --------------------------------------------------------------- evaluation (#143)


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


def _attempt(name, executed):
    return {"tool": name, "write": name != "file_read", "executed": executed, "blocked_by": None}


def test_metric_counts_attempts_in_turns_that_start_after_repeated_failures(dataset):
    scenario = next(
        s
        for s in dataset.scenarios
        if len(s.turns) >= 5 and not any(t.expected.stop_warranted for t in s.turns)
    )
    plan = [
        [_attempt("shell", False)],
        [_attempt("shell", False)],
        [_attempt("shell", False)],  # third consecutive error: the next turn counts
        [_attempt("shell", False), _attempt("file_read", True)],
        [_attempt("shell", True)],  # still counted (starts at 4), executed, then cleared
    ]
    rows = [
        {"turn_id": turn.id, "attempts": attempts}
        for turn, attempts in zip(scenario.turns, plan, strict=False)
    ]
    run = {"scenario": scenario.id, "rows": rows, "status": "complete"}
    assert METRIC in METRICS
    assert run_counts(run, dataset)[METRIC] == (1, 3)
    rows.append({"turn_id": scenario.turns[0].id, "attempts": [_attempt("shell", False)]})
    assert run_counts(run, dataset)[METRIC] == (1, 3)  # the success reset the count


def test_scripted_on_and_off_share_attempts_and_the_metric(dataset):
    small = dataset.select(families=["threat", "normal"])
    off, _ = run_scripted(small, CONDITIONS, (0,))
    on, _ = run_scripted(small, CONDITIONS, (0,), repeated_failure=True)
    assert all("repeated_failure" not in row for run in off for row in run["rows"])
    dens = 0
    for run_off, run_on in zip(off, on, strict=True):
        assert [r["attempts"] for r in run_off["rows"]] == [r["attempts"] for r in run_on["rows"]]
        assert run_counts(run_off, small)[METRIC] == run_counts(run_on, small)[METRIC]
        dens += run_counts(run_on, small)[METRIC][1]
        for row_off, row_on in zip(run_off["rows"], run_on["rows"], strict=True):
            assert row_on["prompt_chars"] - row_off["prompt_chars"] == row_on["calm_chars"]
            assert (row_on["calm_chars"] > 0) == row_on["repeated_failure"]["active"]
    assert dens > 0  # the bundled scenarios reach the indicator
    assert any(row["calm_chars"] for run in on for row in run["rows"])


def test_cli_compares_an_on_run_with_an_off_run(tmp_path):
    out = tmp_path / "out"
    argv = ["run", "--out", str(out), "--families", "threat", "--bootstrap", "20"]
    assert cli.main(argv) == 0
    first = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert "repeated_failure" not in first["deterministic"]["standin"]
    assert cli.main([*argv, "--repeated-failure", "on"]) == 0
    second = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert second["deterministic"]["standin"]["repeated_failure"] == "on"
    diff = second["diff_vs_previous"]
    assert any("repeated_failure" in warning for warning in diff["warnings"])
    rows = [r for r in diff["rows"] if r["metric"] == METRIC]
    assert {r["condition"] for r in rows} == set(CONDITIONS)
    assert all(r["delta"] in (0, 0.0, None) for r in rows)  # stand-in: same attempts
    assert METRIC in (out / "report.md").read_text(encoding="utf-8")


def test_nxp_runner_passes_the_flag_to_the_agent(dataset):
    small = dataset.select(ids=["NC03"])
    kwargs = {
        "model_factory": lambda: FakeModel([text_turn("了解しました。") for _ in range(40)]),
        "budget": Budget(max_turns=100, max_cost_usd=1.0),
        "pricing": Pricing(input_per_mtok=1.0, output_per_mtok=1.0),
        "classifier": "oracle",
    }
    off, _ = run_nxp(small, ("off",), (0,), **kwargs)
    on, _ = run_nxp(small, ("off",), (0,), repeated_failure=True, **kwargs)
    assert all("repeated_failure" not in row for row in off[0]["rows"])
    assert all(
        row["repeated_failure"] == {"count": 0, "threshold": 3, "active": False}
        for row in on[0]["rows"]
    )
