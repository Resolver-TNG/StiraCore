"""ADR 0012 phase 7 on the real chat server app (offline, scripted model, browser channel).

* ``STIRA_ENABLE_EXEC_TOOLS`` + ``STIRA_YOLO_ALLOW=external``: the status offers the exec
  choice; a press with ``exec: true`` answers ``confirm.exec``; the confirmation needs
  ``exec_acknowledged: true``; in the window a scripted ``shell`` call in a real
  ``/api/chat`` turn runs (no card), once, through ``ExecGatedTool``; one log line with the
  tool name and ``#ref`` only; a ``batch`` with an inner ``shell`` is judged call by call
  (two slots); the tool specs the model is sent are the same with and without the window.
* Without exec tools, or with allowance ``internal``: no exec choice, and a press that asks
  for it is refused (409 ``yolo_exec_not_allowed``).
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import Mock

import pytest

from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.allowance import YoloSettings
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_yolo_browser_confirm import BROWSER, _confirm_request

pytestmark = pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="shell is POSIX-only here"
)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(allowance="external", exec_tools=True, model=None, ask=("shell", "batch"), never=()):
        from dashboard import stira_server
        from stiracore.agent import nxp_agent, stimulus_classifier
        from stiracore.agent.stimulus_classifier import StimulusResult
        from stiracore.observability.config_api import ConfigStore
        from stiracore.tools.tool_config import ToolConfig

        if exec_tools:
            monkeypatch.setenv("STIRA_ENABLE_EXEC_TOOLS", "1")
        else:
            monkeypatch.delenv("STIRA_ENABLE_EXEC_TOOLS", raising=False)
        base = tmp_path / str(len(made))
        monkeypatch.setattr(stira_server, "DATA_DIR", base / "data")
        monkeypatch.setattr(
            nxp_agent, "BedrockModel", Mock(return_value=model if model else Mock())
        )
        if model is None:
            monkeypatch.setattr(
                nxp_agent, "Agent", Mock(side_effect=AssertionError("model called"))
            )
        monkeypatch.setattr(
            stimulus_classifier,
            "get_classifier",
            lambda: Mock(
                model_id=None,
                classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5)),
            ),
        )
        runtime._reset_for_tests(
            YoloSettings(allowance, host_confirm="browser", exec_tools=exec_tools)
        )
        store = ConfigStore(base / "gw.json", base / "agent.json", base / "tools.json")
        if ask:
            # Exec tools are Never on a fresh install; the operator made these Ask (the
            # save is done here, before the server starts, not through a route).
            current = store.load_tools()
            ToolConfig(
                mode="custom",
                enabled_tools=(set(current.enabled_tools) | set(ask)) - set(never),
                ask_tools=(set(current.ask_tools) | set(ask)) - set(never),
                path=store.tool_path,
            ).save()
        real = stira_server.NXPAgent
        agents = []

        def capture(*args, **kwargs):
            agents.append(real(*args, **kwargs))
            agents[-1]._test_tools = kwargs.get("tools")
            return agents[-1]

        monkeypatch.setattr(stira_server, "NXPAgent", capture)
        app = stira_server.create_app(config_store=store)
        monkeypatch.setattr(stira_server, "NXPAgent", real)
        app.extensions["test_agent"] = agents[0]
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _press_exec(client):
    response = client.post("/api/yolo/request", json={"tier": "external", "exec": True})
    assert response.status_code == 202, response.get_json()
    data = response.get_json()
    assert data["confirm"]["exec"] is True and data["confirm"]["external"] is True
    assert data["request"]["exec"] is True
    return data


def _chat(app, client, key, message="run it"):
    response = client.post(
        "/api/chat",
        json={"message": message},
        headers={
            **BROWSER,
            "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
            "Idempotency-Key": key,
        },
    )
    body = response.get_data(as_text=True)  # the turn runs while the stream is read
    assert response.status_code == 200, body
    return body


def _model_input(agent):
    """The tool specs the agent was built with (the model is sent these) and its prompt."""
    tools = agent._test_tools or []
    specs = [getattr(t, "tool_spec", None) or {"name": t.tool_name} for t in tools]
    digest = hashlib.sha256(json.dumps(specs, sort_keys=True, default=str).encode()).hexdigest()
    return digest, agent.system_prompt, sorted(t.tool_name for t in tools)


def test_a_scripted_shell_call_runs_once_in_an_exec_window(make_app, tmp_path):
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    rules = client.get("/api/tools/config").get_json()
    assert "shell" in rules["ask_tools"], rules["ask_tools"]
    status = client.get("/api/yolo/status").get_json()
    assert status["exec_offered"] is True and status["caps"]["exec"] == 200
    # A directory outside the data directory (the floor refuses a ``work_dir`` inside it).
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target = sandbox / "yolo7.txt"
    command = {"command": f"printf yolo7 >> {target.name}", "work_dir": str(sandbox)}
    data = _press_exec(client)
    # The third checkbox is required: without it nothing opens and the request stays.
    missing = _confirm_request(client, data, external_acknowledged=True)
    assert missing.status_code == 400 and missing.get_json()["code"] == "host_not_acknowledged"
    assert client.get("/api/yolo/status").get_json()["window"] is None
    confirmed = _confirm_request(client, data, external_acknowledged=True, exec_acknowledged=True)
    assert confirmed.status_code == 200, confirmed.get_json()
    window = client.get("/api/yolo/status").get_json()["window"]
    assert (window["tier"], window["exec"], window["exec_limit"]) == ("external", True, 200)

    model.script = [tool_turn("shell", command, "use-7"), text_turn("ran")]
    body = _chat(app, client, "t7")
    assert target.read_text() == "yolo7"  # ran exactly once
    assert app.extensions["hitl_broker"].pending() == []
    status = client.get("/api/yolo/status").get_json()
    assert (status["window"]["used"], status["window"]["exec_used"]) == (1, 1)
    [line] = status["auto_approvals"]["items"]
    assert set(line) == {"n", "tool", "ref", "limit", "tier", "exec_n", "exec_limit"}
    assert (line["tool"], line["tier"], line["exec_n"], line["exec_limit"]) == (
        "shell",
        "exec",
        1,
        200,
    )
    # The command never reaches the status the page draws.
    assert "printf" not in json.dumps(status) and "yolo7" not in json.dumps(status)
    assert "exec-class tools run only after a human approved" not in body


def test_a_batch_is_judged_call_by_call(make_app, tmp_path):
    """``batch`` and its inner ``shell`` each pass the gate: two slots, one run."""
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target = sandbox / "batch7.txt"
    inner = {"command": f"printf b >> {target.name}", "work_dir": str(sandbox)}
    data = _press_exec(client)
    assert (
        _confirm_request(
            client, data, external_acknowledged=True, exec_acknowledged=True
        ).status_code
        == 200
    )
    model.script = [
        tool_turn("batch", {"invocations": [{"name": "shell", "arguments": inner}]}, "use-7b"),
        text_turn("done"),
    ]
    _chat(app, client, "t7b")
    assert target.read_text() == "b"
    window = client.get("/api/yolo/status").get_json()["window"]
    assert (window["used"], window["exec_used"]) == (2, 2)
    assert app.extensions["hitl_broker"].pending() == []


def test_the_tool_specs_are_the_same_with_and_without_an_exec_window(make_app, tmp_path):
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    agent = app.extensions["test_agent"]
    before = _model_input(agent)
    assert "shell" in before[2] and "batch" in before[2]
    data = _press_exec(client)
    _confirm_request(client, data, external_acknowledged=True, exec_acknowledged=True)
    assert client.get("/api/yolo/status").get_json()["window"]["exec"] is True
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    model.script = [
        tool_turn("shell", {"command": "true", "work_dir": str(sandbox)}, "use-7s"),
        text_turn("ok"),
    ]
    _chat(app, client, "t7s")
    assert client.get("/api/yolo/status").get_json()["window"]["exec_used"] == 1
    after = _model_input(agent)
    assert after == before
    # #508: the only YOLO text the model reads is the Tool Availability line on the window
    # (its tier and exec flag); the tool specs above are unchanged.
    yolo_lines = [line for line in agent.system_prompt.splitlines() if "yolo" in line.lower()]
    assert yolo_lines[0] == "YOLO: no window is open." and len(yolo_lines) == 2
    live = [line for line in agent.system_prompt_provider().splitlines() if "yolo" in line.lower()]
    assert live[0].startswith("YOLO: a window is open (external allowed, exec included)")
    # The model saw the same tool names in every request of the turn.
    assert len({tuple(r["tool_names"]) for r in model.requests}) == 1


@pytest.mark.parametrize(("allowance", "exec_tools"), [("external", False), ("internal", True)])
def test_no_exec_choice_where_it_is_not_offered(make_app, allowance, exec_tools):
    app = make_app(allowance=allowance, exec_tools=exec_tools, ask=())
    client = app.test_client()
    status = client.get("/api/yolo/status").get_json()
    assert status["exec_offered"] is False
    tier = "external" if allowance == "external" else "internal"
    refused = client.post("/api/yolo/request", json={"tier": tier, "exec": True})
    assert refused.status_code == 409 and refused.get_json()["code"] == "yolo_exec_not_allowed"
    assert client.get("/api/yolo/status").get_json()["request"] is None
    for odd in ("true", 1, None):
        response = client.post("/api/yolo/request", json={"tier": tier, "exec": odd})
        assert response.status_code == 409, odd
    plain = client.post("/api/yolo/request", json={"tier": tier})
    assert plain.status_code == 202 and plain.get_json()["confirm"]["exec"] is False


def test_the_floor_still_refuses_a_shell_work_dir_in_the_data_directory(make_app):
    """The gate's floor (``path_check``) runs before the approver: a ``work_dir`` inside
    the data directory is refused in an exec window too (not approved, not a card)."""
    model = FakeModel([])
    app = make_app(model=model)
    client = app.test_client()
    workspace = app.extensions["test_agent"].write_policy.workspace  # inside the data dir
    data = _press_exec(client)
    _confirm_request(client, data, external_acknowledged=True, exec_acknowledged=True)
    model.script = [
        tool_turn("shell", {"command": "printf x >> f.txt", "work_dir": str(workspace)}, "u7f"),
        text_turn("ok"),
    ]
    _chat(app, client, "t7f")
    result = model.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "not sent for approval" in result["content"][0]["text"]
    assert not (workspace / "f.txt").exists()
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window["auto_approved"] == 0 and window["exec_used"] == 0
    assert app.extensions["hitl_broker"].pending() == []
