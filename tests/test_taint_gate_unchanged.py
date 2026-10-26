"""ADR 0022 phase 0 (#458): the taint hook changes no gate decision, prompt or result.

Reference comparison: each case runs without and with :class:`TaintObserverHook`, and the
verdict, the tool results, every request the model saw (messages, system prompt, tool
names — the prompt-cache bytes) and the breaker level must be identical. The observer must
still have recorded a row (else the comparison proves nothing).
"""

from __future__ import annotations

import json

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import NeuralState, StiraConfig
from stiracore.middleware.taint_observer import TaintObserver, TaintObserverHook
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


def _run(level, tools, script, *, hitl=None, action="halt", observer=None):
    model = FakeModel([*script, text_turn("done")])
    hooks = [TaintObserverHook(observer, lambda: True)] if observer is not None else []
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=LEVELS[level]),
        config=StiraConfig(critical_action=action),
        tools=tools,
        on_hitl_request=hitl,
        read_only_paths=(),
        stimulus_classifier=_quiet(),
        extra_hooks=hooks,
    )
    agent("go")
    return agent, model


def _seen(agent, model, side):
    requests = json.dumps(
        [(r["messages"], r["system_prompt"], r["tool_names"]) for r in model.requests],
        sort_keys=True,
        default=str,
    )
    return requests.replace(f"/{side}/", "/<root>/"), agent.engine.breaker_status().level


@pytest.mark.parametrize(
    ("case", "level", "target", "hitl", "action", "expected", "asked"),
    STANDARD,
    ids=[row[0] for row in STANDARD],
)
def test_the_standard_matrix_matches_with_the_taint_hook(
    monkeypatch, tmp_path, case, level, target, hitl, action, expected, asked
):
    measured = []
    for observed in (False, True):
        side = "observed" if observed else "reference"
        root = tmp_path / side
        root.mkdir()
        calls: list = []
        monkeypatch.setitem(
            tool_registry.WRITE_TOOLS, "file_write", RecordingTool("file_write", calls)
        )
        tools = tool_registry.get_tools(protected_dirs=[str(root / "procedural")])
        callback = _approve() if hitl else None
        observer = TaintObserver() if observed else None
        script = [tool_turn("file_write", {"path": str(root / target)})]
        agent, model = _run(level, tools, script, hitl=callback, action=action, observer=observer)
        outcome = _outcome(agent, model, calls, "file_write", callback)
        measured.append((outcome, *_seen(agent, model, side)))
        if observer is not None:
            assert observer.last is not None and observer.last["egress_calls"] == 0
    assert measured[0] == measured[1]
    assert measured[0][0]["verdict"] == expected


def test_an_egress_call_in_an_untrusted_context_is_counted_and_runs_the_same(monkeypatch):
    measured = []
    for observed in (False, True):
        calls: list = []
        fetch = RecordingTool("web_fetch", calls)
        send = RecordingTool("webhook_send", calls)
        monkeypatch.setitem(tool_registry.WRITE_TOOLS, "web_fetch", fetch)
        observer = TaintObserver() if observed else None
        script = [
            tool_turn("web_fetch", {"url": "https://a.example/x"}),
            tool_turn("webhook_send", {"payload": "p"}),
        ]
        agent, model = _run("normal", [fetch, send], script, observer=observer)
        measured.append((len(calls), *_seen(agent, model, "-")))
        if observer is not None:
            row = observer.last
            assert row["context_trust"] == "untrusted"
            assert row["flow_would_hold"] == {"untrusted_send": 1}
            assert "a.example" not in json.dumps(row)
    assert measured[0] == measured[1]
