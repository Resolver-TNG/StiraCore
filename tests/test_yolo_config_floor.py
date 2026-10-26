"""PR #444 review B1: the agent's file tools cannot write the ``yolo`` section.

``gateway-config.json`` decides what the next start allows (ADR 0012 phase 7c), so a
file tool that could rewrite it would loosen YOLO with no person's confirmation. The
real app (``create_app`` with its own configuration store, as the server builds it) is
built with and without ``STIRA_DATA_DIR``; ``file_write`` and ``editor`` — through the
guard with an approval, in an agent turn whose approval would be granted, and in the
editor's write sandbox — are refused for the file itself, a symlink to it, ``..``
spellings and (without ``STIRA_DATA_DIR``) the relative spelling from the working
directory. Every write root
covers the file here, so the refusal is the floor's, not the roots'.

Out of scope (ADR 0012 §9, accepted): a shell command auto-approved in an exec window can
edit the data directory, the tool rules file and this file alike.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig, StiraEngine
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.tools import tool_registry
from tests.provider_fakes import FakeModel, text_turn, tool_turn

SAVED = {"yolo": {"allow": "off", "max_auto_approvals": 50}}
LOOSER = json.dumps({"yolo": {"allow": "external", "host_confirm": "browser"}})


@pytest.fixture(params=["STIRA_DATA_DIR", "default"])
def app(request, tmp_path, monkeypatch):
    from dashboard import stira_server

    for name in ("STIRA_YOLO_ALLOW", "STIRA_YOLO_HOST_CONFIRM", "STIRA_HOST"):
        monkeypatch.delenv(name, raising=False)
    captured: dict = {}

    class _CapturingAgent:
        def __init__(self, *, tools=(), on_breaker=None, state=None, config=None, **kwargs):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            captured.update(kwargs, tools=self.tools)

    data_dir = tmp_path / "stira"
    if request.param == "STIRA_DATA_DIR":
        # The configuration files are the ones in the data directory (#272).
        gateway = data_dir / "gateway-config.json"
        cwd = tmp_path
    else:
        # The long-standing default: relative to the working directory, which here is
        # not the data directory's parent (the file is outside the floor tree).
        cwd = tmp_path / "elsewhere"
        gateway = cwd / ".local" / "stira" / "gateway-config.json"
        # The shipped relative defaults (the root conftest points them elsewhere).
        from stiracore.observability import config_api

        for name, file in (
            ("DEFAULT_GATEWAY_CONFIG_PATH", "gateway-config.json"),
            ("DEFAULT_AGENT_CONFIG_PATH", "agent-config.json"),
            ("DEFAULT_TOOL_CONFIG_PATH", "tool-config.json"),
        ):
            monkeypatch.setattr(config_api, name, Path(".local") / "stira" / file)
    monkeypatch.setattr(stira_server, "DATA_DIR_FROM_ENV", request.param == "STIRA_DATA_DIR")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _CapturingAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    # A person widened the write roots over everything: only the floor can refuse.
    monkeypatch.setattr(
        stira_server.AgentConfig,
        "load",
        classmethod(
            lambda cls, *_a, **_k: cls(
                path=tmp_path / "a.json",
                workspace_dir=str(tmp_path),
                write_roots=["@workspace"],
            )
        ),
    )
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)
    gateway.parent.mkdir(parents=True, exist_ok=True)
    gateway.write_text(json.dumps(SAVED), encoding="utf-8")
    application = stira_server.create_app()  # its own store, as main() builds it
    assert application.extensions["config_store"].gateway_path.resolve() == gateway.resolve()
    (data_dir / "workspace").mkdir(exist_ok=True)
    link = data_dir / "workspace" / "settings.json"
    link.symlink_to(gateway)
    spellings = [
        str(gateway),
        str(link),
        str(gateway.parent / "." / "gateway-config.json"),
        str(gateway.parent / ".." / gateway.parent.name / "gateway-config.json"),
    ]
    if request.param == "default":
        spellings.append(os.path.join(".local", "stira", "gateway-config.json"))
    return stira_server, captured, gateway, spellings


def _unchanged(stira_server, gateway):
    assert json.loads(gateway.read_text(encoding="utf-8")) == SAVED
    assert stira_server._yolo_config_section(gateway) == SAVED["yolo"]


def test_an_approved_file_tool_call_cannot_write_the_yolo_section(app):
    stira_server, captured, gateway, spellings = app
    guard = IdentityGuard(
        StiraEngine(),
        read_only_paths=captured["read_only_paths"],
        path_policy=captured["write_policy"],
    )
    for path in spellings:
        for tool, args in (
            ("file_write", {"path": path, "content": LOOSER}),
            ("editor", {"command": "create", "path": path, "file_text": LOOSER}),
            (
                "editor",
                {"command": "str_replace", "path": path, "old_str": '"off"', "new_str": '"x"'},
            ),
        ):
            result = guard.check_tool_call(tool, args, hitl_approved=True)
            assert not result.allowed and result.layer == 3, (tool, path)
    _unchanged(stira_server, gateway)


def test_an_agent_turn_with_an_approval_cannot_write_the_yolo_section(app):
    """End to end: the model asks for ``file_write``, a person would approve — the floor
    refuses before asking (no approval can lift it) and the file stays as saved."""
    stira_server, captured, gateway, spellings = app
    tools = tool_registry.get_tools(
        "normal",
        read_only_paths=captured["read_only_paths"],
        write_policy=captured["write_policy"],
    )
    file_write = next(t for t in tools if getattr(t, "tool_name", None) == "file_write")
    for path in spellings:
        hitl = Mock(return_value=HITLDecision(HITLAction.APPROVE))
        agent = NXPAgent(
            model=FakeModel(
                [tool_turn("file_write", {"path": path, "content": LOOSER}), text_turn("done")]
            ),
            state=NeuralState(stability=0.9),
            config=StiraConfig(critical_action="halt"),
            tools=[file_write],
            on_hitl_request=hitl,
            read_only_paths=captured["read_only_paths"],
            write_policy=captured["write_policy"],
            stimulus_classifier=Mock(classify=lambda **kw: None),
            tool_policy=lambda name: ("ask", 1),
        )
        agent("go")
        assert not hitl.called, path
    _unchanged(stira_server, gateway)


def test_the_editor_sandbox_refuses_even_with_a_grant(app):
    from stiracore.tools.file_editor_tool import StiraWriteSandbox
    from stiracore.tools.tool_consent import consent_granted

    stira_server, captured, gateway, spellings = app
    policy = captured["write_policy"]
    sandbox = StiraWriteSandbox()
    for path in spellings:
        absolute = os.path.abspath(path)
        with (
            consent_granted("editor", write_path=absolute, policy=policy),
            pytest.raises(PermissionError),
        ):
            asyncio.run(sandbox.write_file(absolute, LOOSER.encode()))
    _unchanged(stira_server, gateway)
