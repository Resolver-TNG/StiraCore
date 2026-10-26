"""ADR 0024 phase 2 with the real gated child (``NXPSubagent``, ADR 0016's gate): a ``wait``
job's Ask call pauses the run, and after the dashboard-side approval the continuation
replays it from the job's checkpoint — through the child's own CheckpointHooks, answered
only by the job gate. Scripted model (no Bedrock, no network).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from strands import tool
from test_job_approval_wait import _ended
from test_scheduler import HUMAN, T0, Breaker, Clock, _tick_at
from test_scheduler_s1 import AGENT
from test_subagent import ScriptedModel

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.control.agent_jobs import GatedJobExecutor, stira_child_builder
from stiracore.control.job_approvals import JobApprovalWaits
from stiracore.core.approval import args_hash
from stiracore.core.approval_store import ApprovalStore
from stiracore.core.checkpoint import FileCheckpointStore
from stiracore.scheduler.scheduler import Scheduler

CALLED: list[dict] = []


@tool
def memory_read(path: str) -> str:
    """Read a note (stands in for a governed call the operator set to Ask)."""
    CALLED.append({"path": path})
    return "the note"


RULES = {"memory_read": "ask", "current_time": "always"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    CALLED.clear()
    model = ScriptedModel()
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kw: model)
    monkeypatch.setattr(
        "stiracore.core.engine.FileCheckpointStore",
        lambda *a, **kw: FileCheckpointStore(tmp_path / "checkpoints"),
    )
    parent = NXPAgent("offline", "us-east-1", stimulus_classifier=Mock(classify=lambda **kw: None))
    parent.checkpoint_hooks.policy_check = RULES.get
    clock = Clock()
    store = ApprovalStore(None, now_fn=clock)
    waits = JobApprovalWaits(store, now_fn=clock)
    executor = GatedJobExecutor(
        stira_child_builder(lambda: parent, lambda names: [memory_read]),
        breaker_level=Breaker(),
        rule_of=RULES.get,
        waits=waits,
    )
    scheduler = Scheduler(
        tmp_path / "state",
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=executor,
        dispatch=lambda work: work(),
        approval_waits=waits,
    )
    scheduler.start(background=False)
    yield scheduler, store, clock, model
    scheduler.stop()


def _use(args, uid="t1"):
    return [{"toolUse": {"name": "memory_read", "toolUseId": uid, "input": args}}]


def test_a_real_child_pauses_and_the_approved_call_runs_once_after_approval(world):
    scheduler, store, clock, model = world
    model.responses = [_use({"path": "notes.md"}), [{"text": "summary done"}]]
    scheduler.create_job(
        {
            **AGENT,
            "tools": ["memory_read"],
            "unattended_approval": "wait",
            "requires_heartbeat": False,
        },
        by=HUMAN,
        confirm=True,
    )
    _tick_at(scheduler, clock, T0 + 3600)
    assert scheduler.runs()[0]["status"] == "awaiting_approval", scheduler.runs()[0]
    assert CALLED == []
    (waiting,) = scheduler.waiting_runs()
    request_id = waiting["request_id"]
    record = store.get(request_id)
    assert record.agent_id == "job:digest" and record.origin["kind"] == "job"
    store.decide(request_id, "approve", args_hash=args_hash({"path": "notes.md"}), approver=HUMAN)
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is True
    run = _ended(scheduler)
    assert run["status"] == "completed", run
    assert CALLED == [{"path": "notes.md"}]
    assert store.get(request_id).consumed
