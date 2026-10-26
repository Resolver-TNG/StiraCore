"""ADR 0023 phase 0 (#460-0): the observer changes no gate decision and no breaker level.

Reference comparison: every case runs twice — the reference agent (no containment hook)
and the observed agent (the hook on, fed by the same calls) — and everything the gate
decides must be identical: the verdict, whether a person was asked, the tool result the
model reads, the messages, the breaker level. The observer must still have *seen* the
refusals (else the comparison would prove nothing).
"""

from __future__ import annotations

import json

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import NeuralState, StiraConfig
from stiracore.core.breaker_sim import simulate, simulate_with_signals
from stiracore.core.containment import CODE_SIGNALS, ContainmentObserver
from stiracore.middleware.containment_observer import ContainmentObserverHook
from stiracore.tools import tool_registry
from tests.test_tool_path_contract import (  # noqa: F401 - the module's isolation fixture
    LEVELS,
    STANDARD,
    RecordingTool,
    _approve,
    _isolate_registry,
    _outcome,
    _quiet,
)


def _run(level, tools, name, tool_input, *, hitl, action, observer):
    model = FakeModel([tool_turn(name, tool_input), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=LEVELS[level]),
        config=StiraConfig(critical_action=action),
        tools=tools,
        on_hitl_request=hitl,
        read_only_paths=(),
        stimulus_classifier=_quiet(),
        extra_hooks=[ContainmentObserverHook(observer)] if observer is not None else [],
    )
    agent("go")
    return agent, model


def _results(agent):
    return [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


@pytest.mark.parametrize(
    ("case", "level", "target", "hitl", "action", "expected", "asked"),
    STANDARD,
    ids=[row[0] for row in STANDARD],
)
def test_the_standard_matrix_matches_with_the_observer(
    monkeypatch, tmp_path, case, level, target, hitl, action, expected, asked
):
    measured = []
    for observed in (False, True):
        root = tmp_path / ("observed" if observed else "reference")
        root.mkdir()  # an unresolvable protected dir fails closed (measured)
        calls: list = []
        monkeypatch.setitem(
            tool_registry.WRITE_TOOLS, "file_write", RecordingTool("file_write", calls)
        )
        tools = tool_registry.get_tools(protected_dirs=[str(root / "procedural")])
        callback = _approve() if hitl else None
        observer = ContainmentObserver() if observed else None
        path = root / target
        agent, model = _run(
            level, tools, "file_write", {"path": str(path)},
            hitl=callback, action=action, observer=observer,
        )  # fmt: skip
        outcome = _outcome(agent, model, calls, "file_write", callback)
        side = "observed" if observed else "reference"
        text = json.dumps(_results(agent)).replace(f"/{side}/", "/<root>/")
        measured.append((outcome, text, agent.engine.breaker_status().level, path.exists()))
        if observer is not None and expected == "blocked:identity":
            assert observer.snapshot()["codes"].get("write_floor") == 1, case
    reference, observed_run = measured
    assert observed_run == reference
    assert reference[0]["verdict"] == expected


def test_real_file_tools_a_repeated_floor_write_is_seen_and_answered_the_same(tmp_path):
    """Real file tools (the read tee is covered in test_containment_phase0): the same floor
    write three times — the model reads the same results with and without the observer."""
    outputs = []
    for observed in (False, True):
        observer = ContainmentObserver() if observed else None
        tools = tool_registry.get_tools(protected_dirs=[str(tmp_path / "procedural")])
        call = {"path": str(tmp_path / "SOUL.md"), "content": "x"}
        model = FakeModel([*(tool_turn("file_write", call) for _ in range(3)), text_turn("done")])
        agent = NXPAgent(
            model=model,
            state=NeuralState(stability=0.9),
            tools=tools,
            stimulus_classifier=_quiet(),
            extra_hooks=[ContainmentObserverHook(observer)] if observer else [],
        )
        agent("go")
        outputs.append((json.dumps(_results(agent)), agent.engine.breaker_status().level))
        if observer is not None:
            snap = observer.snapshot()
            assert snap["codes"] == {"repeated_refusal": 1, "write_floor": 3}
            assert snap["would_be"] == "closed" and snap["score"] == 7.0
    assert outputs[0] == outputs[1]
    assert not (tmp_path / "SOUL.md").exists()


# --- the emotional breaker: the same levels with any signal sequence ----------------------

STIMULI = [
    ("threat", 0.9, 0.0),
    ("threat", 0.9, 0.0),
    (None, 0.0, 0.1),
    ("joy", 0.8, 0.5),
    ("threat", 1.0, 0.0),
    (None, 0.0, 2.0),
]


@pytest.mark.parametrize(
    "codes",
    [
        (),
        ("E_READ_SECRET_PATH",),
        ("E_OWN_DASHBOARD", "E_OWN_DASHBOARD", "E_OWN_DASHBOARD"),
        tuple(CODE_SIGNALS),
    ],
)
def test_breaker_levels_match_the_reference_engine(codes):
    reference = simulate(STIMULI)
    steps = [(*step, codes) for step in STIMULI]
    result = simulate_with_signals(steps)
    assert result.breaker == reference
    assert len(result.containment) == len(STIMULI)
    if codes:
        assert result.containment[0].codes == codes


def test_signal_sequence_reaches_the_expected_would_be():
    steps = [
        (None, 0.0, 0.0, ("E_READ_OUTSIDE_ROOTS",)),  # 0.5
        (None, 0.0, 0.0, ("E_READ_SECRET_PATH",)),  # 2.5 restricted
        (None, 0.0, 0.0, ("write_floor", "secret_in_args")),  # 5.5 closed
        (None, 0.0, 0.0, ("E_OWN_DASHBOARD", "burst_calls")),  # 9.5 quarantine
        (None, 0.0, 1.0, ()),  # an hour later: the window is empty
    ]
    result = simulate_with_signals(steps)
    assert [s.would_be for s in result.containment] == [
        "open",
        "restricted",
        "closed",
        "quarantine",
        "open",
    ]
    assert [s.score for s in result.containment] == [0.5, 2.5, 5.5, 9.5, 0.0]
