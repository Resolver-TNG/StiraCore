"""#291 (review of #289): ``callback_handler=None`` leaves the HITL hooks untouched.

The SDK callback (printing) and the hooks (approval gate, checkpoints) are separate
mechanisms. stira_server and the chat bots build their agent with
``callback_handler=None``; these tests run the approval flows that way:

- pending approval -> rejected: the tool never runs;
- approved by a human: runs once and its result reaches the conversation;
- paused -> process stops -> restored from the checkpoint -> approved -> resumed once.

and nothing is printed to stdout on the way.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import test_hitl_binding as binding
import test_hitl_persistence as persistence
from test_subagent import ScriptedModel

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.core.approval import OperationLedger
from stiracore.core.approval_store import ApprovalStore
from stiracore.core.checkpoint import FileCheckpointStore
from stiracore.observability.chat_api import HITLApprovalBroker

CALL = binding.CALL


@pytest.fixture
def quiet_agents(monkeypatch):
    """Every agent the HITL helpers build: theirs, plus ``callback_handler=None``."""
    built = []

    def quiet(tmp_path, responses, tools, *, approver, ledger, stability=0.1, extra_hooks=()):
        config = StiraConfig(critical_action="halt")
        config.blocked_tool_names = set(config.blocked_tool_names) | {"external_update"}
        agent = NXPAgent(
            model=ScriptedModel(responses),
            state=NeuralState(stability=stability),
            config=config,
            on_hitl_request=approver,
            tools=tools,
            agent_id="ext-agent",
            checkpoint_store=FileCheckpointStore(tmp_path / "checkpoints"),
            operation_ledger=ledger,
            extra_hooks=list(extra_hooks),
            stimulus_classifier=Mock(classify=lambda **kwargs: None),
            callback_handler=None,
        )
        built.append(agent)
        return agent

    monkeypatch.setattr(binding, "_agent", quiet)
    monkeypatch.setattr(persistence, "_agent", quiet)
    return built


def _live(tmp_path, decide):
    service = binding.ExternalService()
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=30)
    agent = binding._agent(
        tmp_path,
        [[{"toolUse": CALL}], [{"text": "finished"}]],
        [binding._external_tool(service)],
        approver=broker.request,
        ledger=OperationLedger(tmp_path / "ops.db"),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(agent, "update")
        (pending,) = binding._wait_pending(broker)
        assert service.applied == [], "nothing runs while the approval is pending"
        assert broker.decide(pending["id"], decide, args_hash=pending["args_hash"])
        result = turn.result(timeout=10)
    return service, agent, result


def test_rejected_approval_never_runs_the_tool(tmp_path, quiet_agents, capsys):
    service, agent, result = _live(tmp_path, HITLDecision(HITLAction.REJECT, reason="no"))
    assert [a._callback_kwargs for a in quiet_agents] == [{"callback_handler": None}]
    assert service.applied == []
    assert binding._results(agent)["ext-1"]["status"] == "error"
    assert "finished" in str(result)
    assert capsys.readouterr().out == ""


def test_human_approval_runs_once_and_the_result_reaches_the_conversation(
    tmp_path, quiet_agents, capsys
):
    service, agent, result = _live(tmp_path, HITLDecision(HITLAction.APPROVE))
    assert [(r, v) for r, v, _key in service.applied] == [("r1", "v1")]
    tool_result = binding._results(agent)["ext-1"]
    assert tool_result["status"] == "success"
    assert tool_result["content"][0]["text"] == "updated r1=v1"
    assert "finished" in str(result)
    assert capsys.readouterr().out == ""


def test_checkpoint_restore_and_resume_work_without_the_callback(tmp_path, quiet_agents, capsys):
    persistence.test_pending_approval_survives_restart_and_approval_resumes_the_paused_call(
        tmp_path
    )
    assert len(quiet_agents) == 2, "the paused process and the restarted one"
    assert capsys.readouterr().out == ""
