"""Review of #441 M1: what a ``batch`` can reach in an exec window — measured on the real app.

``tiers.exec_refusal("batch", …)`` looks at the arguments' secrets only, not at the inner
calls' names. That is safe only because the SDK's direct tool call (``agent.tool.X``, what
upstream ``batch`` uses) runs ``BeforeToolCallEvent`` for every inner call, so each one
passes Stira's whole gate on its own (rules, floors, breaker, YOLO classes, the exec gate
mark). These tests measure that premise on the real chat server app (offline, scripted model,
browser channel) for each inner kind the review listed, and spy on the gate to show that
the inner call reached it:

* exec window, inner ``cron`` / ``think`` / ``use_agent`` / ``swarm`` / ``graph`` (Ask) →
  their own card (rejected here) and not run;
* inner exec tool on its default Never, an MCP tool, a name that is not a tool → refused
  or a card, not run; ``handoff_to_user`` is judged exactly as when called directly;
* ``batch{shell}`` and ``batch{batch{shell}}`` outside a window, in a window of tier
  internal, in a window of tier external without "Include exec", and under the breaker's
  write block → never answered automatically; even with every ``batch`` approved by a
  person, the inner ``shell`` asks on its own card;
* ``batch{batch{shell}}`` in an exec window: one slot per call (three), one run.

``cron`` would edit the real crontab: the tests put a fake ``crontab`` first on ``PATH``
that only records that it was called. No child-agent tool reaches the model (the scripted
model counts its requests).
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from stiracore.agent import checkpoint_hooks
from stiracore.core.engine import HITLAction, HITLDecision
from stiracore.tools import tool_registry
from tests import test_yolo_exec_app as _exec_app
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_yolo_browser_confirm import _confirm_request
from tests.test_yolo_exec_app import _chat, _press_exec

make_app = _exec_app.make_app  # the real-app fixture of phase 7a

pytestmark = pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="shell is POSIX-only here"
)

CHILD_TOOLS = ("think", "use_agent", "swarm", "graph")
ALL_ASK = ("shell", "batch", "cron", *CHILD_TOOLS)
_INNER_ARGS = {
    "cron": {"action": "list"},
    "think": {"thought": "x", "cycle_count": 1, "system_prompt": "child"},
    "use_agent": {"prompt": "x", "system_prompt": "child"},
    "swarm": {"task": "x", "agents": [{"name": "c", "system_prompt": "child"}]},
    "graph": {"action": "create", "graph_id": "g", "topology": {"nodes": [], "edges": []}},
}


@pytest.fixture
def gate_seen(monkeypatch):
    """The tool names that reached Stira's gate (``CheckpointHooks.before_tool``)."""
    seen: list[str] = []
    real = checkpoint_hooks.CheckpointHooks.before_tool

    def spy(self, event):
        seen.append(event.tool_use["name"])
        return real(self, event)

    monkeypatch.setattr(checkpoint_hooks.CheckpointHooks, "before_tool", spy)
    return seen


@pytest.fixture
def crontab(tmp_path, monkeypatch):
    """A fake ``crontab`` first on ``PATH``: the file it appends to counts its calls."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls = tmp_path / "crontab.calls"
    script = bin_dir / "crontab"
    script.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return calls


def _open(client, tier="external", with_exec=True):
    if with_exec:
        data = _press_exec(client)
        extra = {"external_acknowledged": True, "exec_acknowledged": True}
    else:
        response = client.post("/api/yolo/request", json={"tier": tier})
        assert response.status_code == 202, response.get_json()
        data = response.get_json()
        extra = {"external_acknowledged": True} if tier == "external" else {}
    confirmed = _confirm_request(client, data, **extra)
    assert confirmed.status_code == 200, confirmed.get_json()
    window = client.get("/api/yolo/status").get_json()["window"]
    assert (window["tier"], window["exec"]) == (tier, with_exec)


def _batch(*invocations):
    return {"invocations": [{"name": n, "arguments": a} for n, a in invocations]}


def _turn(app, client, model, key, tool_input, approve=()):
    """One chat turn whose model calls ``batch(tool_input)``. Each card is answered by a
    person: approved when its tool is in ``approve``, else rejected. Returns the cards'
    tool names in order and the inner results of the outer ``batch``."""
    model.script = [tool_turn("batch", tool_input, "use-" + key), text_turn("done")]
    broker = app.extensions["hitl_broker"]
    done: dict = {}
    worker = threading.Thread(target=lambda: done.setdefault("body", _chat(app, client, key)))
    worker.start()
    cards: list[str] = []
    answered: set[str] = set()
    deadline = time.monotonic() + 60
    while worker.is_alive() and time.monotonic() < deadline:
        for item in broker.pending():
            if item["id"] in answered:
                continue
            answered.add(item["id"])
            cards.append(item["tool_name"])
            if item["tool_name"] in approve:
                outcome = broker.decide(
                    item["id"], HITLDecision(HITLAction.APPROVE), args_hash=item["args_hash"]
                )
            else:
                outcome = broker.decide(item["id"], HITLDecision(HITLAction.REJECT, reason="no"))
            assert outcome is not None
        time.sleep(0.01)
    worker.join(5)
    assert "body" in done, "the turn did not finish"
    result = model.requests[1]["messages"][-1]["content"][0]["toolResult"]
    return cards, result


def _inner(result):
    """``[(name, status, text)]`` of the batch's inner calls (the SDK's direct results)."""
    blocks = [b for b in result.get("content", []) if "json" in b]
    if not blocks:
        return result["status"], result["content"][0]["text"]
    out = []
    for entry in blocks[0]["json"]["results"]:
        inner = entry.get("result")
        if isinstance(inner, dict):
            text = " ".join(c.get("text", "") for c in inner.get("content", []))
            out.append((entry["name"], inner.get("status"), text))
        else:
            out.append((entry["name"], entry["status"], entry.get("error", "")))
    return out


def _window(client):
    return client.get("/api/yolo/status").get_json()["window"]


# --- exec window: the inner call of every excluded exec tool is its own card -----------------


@pytest.mark.parametrize("inner", ["cron", *CHILD_TOOLS])
def test_an_excluded_exec_tool_inside_a_batch_is_its_own_card(make_app, gate_seen, crontab, inner):
    model = FakeModel([])
    app = make_app(model=model, ask=ALL_ASK)
    client = app.test_client()
    _open(client)
    cards, result = _turn(app, client, model, "c-" + inner, _batch((inner, _INNER_ARGS[inner])))
    assert gate_seen == ["batch", inner]  # the inner call reached the gate
    assert cards == [inner]  # not answered automatically: a person's card
    [(name, status, text)] = _inner(result)
    assert (name, status) == (inner, "error") and text.startswith("HITL rejected"), text
    assert not crontab.exists()  # cron never ran
    assert len(model.requests) == 2  # no child agent reached the model
    window = _window(client)
    assert (window["used"], window["exec_used"]) == (1, 1)  # the batch only


def test_the_cron_check_above_would_see_a_run(make_app, gate_seen, crontab, monkeypatch):
    """The detector works: with the exclusion removed (monkeypatched), the same batched
    ``cron`` is answered automatically and the fake ``crontab`` is called."""
    from stiracore.tools.tool_consent import EXEC_TOOLS
    from stiracore.yolo import requests as yolo_requests
    from stiracore.yolo import tiers

    monkeypatch.setattr(tiers, "EXEC_AUTO_TOOLS", EXEC_TOOLS)
    monkeypatch.setattr(yolo_requests, "EXEC_AUTO_TOOLS", EXEC_TOOLS)
    model = FakeModel([])
    app = make_app(model=model, ask=ALL_ASK)
    client = app.test_client()
    _open(client)
    cards, result = _turn(app, client, model, "c-proof", _batch(("cron", _INNER_ARGS["cron"])))
    assert cards == [] and gate_seen == ["batch", "cron"]
    [(_name, status, _text)] = _inner(result)
    assert status == "success" and crontab.read_text().strip() == "-l"
    assert _window(client)["exec_used"] == 2


@pytest.mark.parametrize("inner", ["shell", "cron", *CHILD_TOOLS])
def test_an_exec_tool_on_its_default_never_inside_a_batch_is_refused(
    make_app, gate_seen, crontab, tmp_path, inner
):
    """Exec tools are Never on a fresh install: only ``batch`` is Ask here."""
    model = FakeModel([])
    app = make_app(model=model, ask=("batch",), never=("shell", "cron", *CHILD_TOOLS))
    client = app.test_client()
    rules = client.get("/api/tools/config").get_json()
    assert inner not in rules["enabled_tools"] and inner not in rules["ask_tools"]
    _open(client)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    args = (
        {"command": "printf n >> never.txt", "work_dir": str(sandbox)}
        if inner == "shell"
        else _INNER_ARGS[inner]
    )
    cards, result = _turn(app, client, model, "n-" + inner, _batch((inner, args)))
    assert cards == []
    [(_name, status, text)] = _inner(result)
    # A Never tool is not in this turn's registry ("not found"), or the gate's Never.
    assert status == "error" and ("not found" in text or "Never" in text), text
    assert not (sandbox / "never.txt").exists() and not crontab.exists()
    assert len(model.requests) == 2
    assert _window(client)["exec_used"] == 1  # the batch only


def test_an_mcp_tool_inside_a_batch_is_a_card(make_app, gate_seen, monkeypatch):
    """An MCP tool is never answered by YOLO (also when it reuses a listed name: here
    ``web_fetch``, which a window of tier external would otherwise answer)."""
    monkeypatch.setattr(tool_registry, "mcp_tool_names", lambda: frozenset({"web_fetch"}))
    model = FakeModel([])
    app = make_app(model=model, ask=("shell", "batch", "web_fetch"))
    client = app.test_client()
    _open(client)
    url = "http://127.0.0.1:9/never-fetched"  # the discard port: nothing is sent anyway
    cards, result = _turn(app, client, model, "mcp", _batch(("web_fetch", {"url": url})))
    assert gate_seen == ["batch", "web_fetch"] and cards == ["web_fetch"]
    [(name, status, text)] = _inner(result)
    assert (name, status) == ("web_fetch", "error") and text.startswith("HITL rejected")
    window = _window(client)
    assert (window["used"], window["external_used"], window["exec_used"]) == (1, 0, 1)


@pytest.mark.parametrize("name", ["_agent", "__class__", "__init__", "no_such_tool"])
def test_a_name_that_is_not_a_tool_reaches_nothing(make_app, gate_seen, name):
    """Upstream ``batch`` looks the name up with ``getattr(agent.tool, name)``: the
    caller's own attributes are callable, but none runs a tool or the model (``_agent``
    is refused by the SDK's concurrency guard: the agent is in this very invocation)."""
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    _open(client)
    cards, result = _turn(app, client, model, "attr" + str(len(name)), _batch((name, {})))
    assert gate_seen == ["batch"] and cards == []
    [(inner, status, _text)] = _inner(result)
    assert (inner, status) == (name, "error")
    assert len(model.requests) == 2


@pytest.mark.parametrize("rule", ["always", "ask"])
def test_handoff_to_user_inside_a_batch_is_judged_as_a_direct_call(make_app, gate_seen, rule):
    """``handoff_to_user`` is an internal tool (§4): called directly it runs on Always and
    is answered by any window on Ask. Inside a ``batch`` the same — it passes the gate on
    its own and takes an internal slot, never an exec one."""
    model = FakeModel([])
    ask = ("shell", "batch", "handoff_to_user") if rule == "ask" else ("shell", "batch")
    app = make_app(model=model, ask=ask)
    client = app.test_client()
    _open(client)
    args = {"message": "hi", "breakout_of_loop": True}  # never waits on a terminal
    cards, result = _turn(app, client, model, "h-" + rule, _batch(("handoff_to_user", args)))
    assert gate_seen == ["batch", "handoff_to_user"] and cards == []
    [(_name, status, _text)] = _inner(result)
    assert status == "success"
    window = _window(client)
    assert window["exec_used"] == 1  # the batch only
    assert window["used"] == (2 if rule == "ask" else 1)


# --- batch{shell} / batch{batch{shell}} in no window, or a window without "Include exec" ------


def _nested(depth, inner):
    tool_input = _batch(("shell", inner))
    for _ in range(depth - 1):
        tool_input = _batch(("batch", tool_input))
    return tool_input


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("where", ["no_window", "internal", "external"])
def test_a_batched_shell_is_never_automatic_without_the_exec_flag(
    make_app, gate_seen, tmp_path, depth, where
):
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    if where != "no_window":
        _open(client, where, with_exec=False)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    inner = {"command": "printf s >> s.txt", "work_dir": str(sandbox)}
    tool_input = _nested(depth, inner)
    # A person rejects the first card: nothing runs.
    cards, _result = _turn(app, client, model, f"r{depth}{where}", tool_input)
    assert cards == ["batch"] and not (sandbox / "s.txt").exists()
    # A person approves every batch card: the inner shell still asks on its own card.
    del gate_seen[:]
    model.requests.clear()
    cards, result = _turn(app, client, model, f"a{depth}{where}", tool_input, approve=("batch",))
    assert cards == ["batch"] * depth + ["shell"]
    assert gate_seen == ["batch"] * depth + ["shell"]
    assert not (sandbox / "s.txt").exists()
    assert "HITL rejected" in json.dumps(result)
    window = _window(client)
    if window is not None:
        assert window["exec_used"] == 0 and window["auto_approved"] == 0


def test_a_batch_under_the_breakers_write_block_is_refused(make_app, gate_seen, tmp_path):
    """WARNING: the window pauses, and the gate refuses ``batch`` (write-capable, like the
    ``blocked_tool_names``) before any approval — no card, no slot, the inner call never
    starts (ADR 0010 §2: the breaker is not lifted by an approval, YOLO's included)."""
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    _open(client)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    agent = app.extensions["test_agent"]
    agent.engine.state.stability = 0.4  # WARNING (writes need a confirmation: refused)
    assert agent.engine.breaker_status().level.value == "warning"
    inner = {"command": "printf w >> w.txt", "work_dir": str(sandbox)}
    cards, result = _turn(app, client, model, "warn", _batch(("shell", inner)))
    assert cards == [] and gate_seen == ["batch"]
    assert result["status"] == "error" and "Writes are blocked" in result["content"][0]["text"]
    assert not (sandbox / "w.txt").exists()
    window = _window(client)
    assert (window["state"], window["pause_reason"]) == ("paused", "breaker_warning")
    assert window["exec_used"] == 0 and window["auto_approved"] == 0


# --- exec window: a nested batch is judged call by call ------------------------------------


def test_a_nested_batch_in_an_exec_window_holds_one_slot_per_call(make_app, gate_seen, tmp_path):
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    _open(client)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    inner = {"command": "printf x >> x.txt", "work_dir": str(sandbox)}
    cards, _result = _turn(app, client, model, "nest", _nested(2, inner))
    assert cards == [] and gate_seen == ["batch", "batch", "shell"]
    assert (sandbox / "x.txt").read_text() == "x"  # ran exactly once
    window = _window(client)
    assert (window["used"], window["exec_used"]) == (3, 3)
