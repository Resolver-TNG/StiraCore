"""ADR 0022 phase 0 (#458): the observer hook only reads; one row per turn, codes only."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from stiracore.middleware.taint_observer import (
    TaintObserver,
    TaintObserverHook,
    injected_sources,
)


def _agent(messages, prompt="SOUL"):
    return SimpleNamespace(messages=messages, system_prompt=prompt)


def _use(name, arguments):
    return {"role": "assistant", "content": [{"toolUse": {"name": name, "input": arguments}}]}


def test_turn_row_counts_would_hold_and_changes_nothing():
    rows = []
    hook = TaintObserverHook(TaintObserver(rows.append), started_by_person=lambda: True)
    messages = [_use("web_fetch", {"url": "https://private.example/a"})]
    agent = _agent(messages)
    frozen = copy.deepcopy(messages)
    hook.begin(SimpleNamespace(agent=agent))
    tool_use = {"name": "webhook_send", "input": {"payload": "hi private.example"}}
    event = SimpleNamespace(agent=agent, tool_use=tool_use, cancel_tool=False)
    hook.before_tool(event)
    hook.before_tool(SimpleNamespace(agent=agent, tool_use={"name": "file_write", "input": {}}))
    hook.finish(SimpleNamespace(agent=agent))
    assert event.cancel_tool is False and agent.messages == frozen
    assert agent.system_prompt == "SOUL"
    [row] = rows
    assert row["context_trust"] == "untrusted"
    assert row["flow_would_hold"] == {"untrusted_send": 1}
    assert row["egress_calls"] == 1
    assert "private.example" not in repr(row) and "hi" not in str(row["flow_would_hold"])


def test_recalled_block_in_prompt_counts_recall_only():
    observer = TaintObserver()
    hook = TaintObserverHook(observer, started_by_person=lambda: True)
    agent = _agent([], prompt="SOUL\n## Episodic Memory\n- x")
    assert injected_sources(agent) == ["memory"]
    hook.begin(SimpleNamespace(agent=agent))
    hook.finish(SimpleNamespace(agent=agent))
    assert observer.last["untrusted_recall_only"] is True
    assert observer.snapshot()["totals"]["untrusted_recall_only"] == 1


def test_failures_never_raise():
    hook = TaintObserverHook(TaintObserver(), started_by_person=lambda: 1 / 0)
    hook.begin(SimpleNamespace(agent=None))
    hook.before_tool(SimpleNamespace(agent=None, tool_use=None))
    hook.finish(SimpleNamespace(agent=object()))
