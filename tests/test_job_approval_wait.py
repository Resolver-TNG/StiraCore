"""ADR 0024 phase 2 / ADR 0021 S3 (#240, #239): a job run waits for an approval, then
continues from where it stopped — only the approved call, only once.

The child is a scripted stand-in that drives the executor's real guards (no model, no
network); the approval store is the real durable store (in memory). Mutations show the
argument-hash binding and the single hand-out of the approval matter.
"""

from __future__ import annotations

import time

import pytest
from test_scheduler import HUMAN, T0, Breaker, Clock, _tick_at
from test_scheduler_s1 import AGENT

from stiracore.control.agent_jobs import GatedJobExecutor
from stiracore.control.job_approvals import JobApprovalGate, JobApprovalWaits
from stiracore.core.approval import args_hash
from stiracore.core.approval_store import ApprovalStore
from stiracore.scheduler.scheduler import Scheduler, SchedulerRefused

SENT: list[dict] = []


class _Event:
    def __init__(self, use, agent):
        self.tool_use, self.agent, self.cancel_tool = use, agent, False


class FakeChild:
    """Runs a scripted list of calls through the guards; the tool records what ran.

    ``first`` is the call list of the first invocation, ``replay`` the one of the
    continuation (``agent(None)``)."""

    def __init__(self, guards, first, replay):
        self.pre_hooks, self.first, self.replay = list(guards), first, replay
        self.stopped = False
        self.agent = self._invoke

    def cancel(self):
        self.stopped = True

    def _invoke(self, prompt, cancel_signal=None):
        self.stopped = False
        for use in self.first if prompt is not None else self.replay:
            if self.stopped:
                break
            event = _Event(dict(use), self)
            for guard in self.pre_hooks:
                guard.before_tool(event)
            if not event.cancel_tool:
                SENT.append({"tool": use["name"], "input": use["input"]})
        return "done"


def _call(args, uid="t1", name="memory_read"):
    return {"name": name, "toolUseId": uid, "input": args}


RULES = {"memory_read": "ask", "current_time": "always"}


@pytest.fixture
def world(tmp_path):
    SENT.clear()
    clock = Clock()
    store = ApprovalStore(None, now_fn=clock)
    waits = JobApprovalWaits(store, now_fn=clock)
    script = {"first": [_call({"path": "notes.md"})], "replay": [_call({"path": "notes.md"})]}

    def build(ctx, names, guards):
        return FakeChild(guards, script["first"], script["replay"])

    executor = GatedJobExecutor(build, breaker_level=Breaker(), rule_of=RULES.get, waits=waits)
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=executor,
        dispatch=lambda work: work(),
        approval_waits=waits,
    )
    scheduler.start(background=False)
    yield scheduler, store, clock, script
    scheduler.stop()


def _wait_job(**extra):
    return {
        **AGENT,
        "tools": ["memory_read", "current_time"],
        "unattended_approval": "wait",
        "requires_heartbeat": False,
        **extra,
    }


def _start(scheduler, clock):
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    run = scheduler.runs()[0]
    assert run["status"] == "awaiting_approval", run
    (waiting,) = scheduler.waiting_runs()
    return waiting["request_id"]


def _ended(scheduler, timeout=3.0):
    """Cancellation callbacks run on their own thread: wait for the run to close."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = scheduler.runs()[0]
        if run["status"] not in ("running", "awaiting_approval"):
            return run
        time.sleep(0.01)
    return scheduler.runs()[0]


def _approve(store, request_id, args):
    return store.decide(request_id, "approve", args_hash=args_hash(args), approver=HUMAN)


def test_a_wait_job_pauses_on_ask_and_continues_after_approval(world):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    assert SENT == []  # nothing ran while waiting
    record = store.get(request_id)
    assert record.origin["kind"] == "job" and record.status == "pending"
    codes = [n["code"] for n in scheduler.notifications()]
    assert "approval_waiting" in codes
    assert _approve(store, request_id, {"path": "notes.md"})
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is True
    run = scheduler.runs()[0]
    assert run["status"] == "completed", run
    assert SENT == [{"tool": "memory_read", "input": {"path": "notes.md"}}]
    assert store.get(request_id).consumed


def test_without_approval_nothing_runs(world):
    scheduler, _store, clock, _ = world
    request_id = _start(scheduler, clock)
    with pytest.raises(SchedulerRefused):
        scheduler.resume_waiting_run("job-unknown", by=HUMAN)
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is False  # pending
    assert SENT == []
    assert _ended(scheduler)["status"] == "cancelled"


def test_a_replayed_call_with_other_arguments_is_refused(world):
    scheduler, store, clock, script = world
    script["replay"] = [_call({"path": "secrets.env"})]
    request_id = _start(scheduler, clock)
    assert _approve(store, request_id, {"path": "notes.md"})
    scheduler.resume_waiting_run(request_id, by=HUMAN)
    assert SENT == []
    assert scheduler.runs()[0]["reason"] == "binding_mismatch"


def test_mutation_without_the_hash_binding_other_arguments_run(world, monkeypatch):
    scheduler, store, clock, script = world
    monkeypatch.setattr(JobApprovalGate, "_bound", staticmethod(lambda resume, tool_input: True))
    script["replay"] = [_call({"path": "secrets.env"})]
    request_id = _start(scheduler, clock)
    _approve(store, request_id, {"path": "notes.md"})
    scheduler.resume_waiting_run(request_id, by=HUMAN)
    assert SENT == [{"tool": "memory_read", "input": {"path": "secrets.env"}}]


def test_the_approved_call_runs_once_even_if_replayed_twice(world):
    scheduler, store, clock, script = world
    script["replay"] = [_call({"path": "notes.md"}), _call({"path": "notes.md"})]
    request_id = _start(scheduler, clock)
    _approve(store, request_id, {"path": "notes.md"})
    scheduler.resume_waiting_run(request_id, by=HUMAN)
    assert len(SENT) == 1
    with pytest.raises(SchedulerRefused):
        scheduler.resume_waiting_run(request_id, by=HUMAN)  # the run has ended
    assert len(SENT) == 1


def test_mutation_without_the_single_hand_out_the_call_runs_twice(world, monkeypatch):
    scheduler, store, clock, script = world
    monkeypatch.setattr(ApprovalStore, "consume", lambda self, *a, **kw: True)
    script["replay"] = [_call({"path": "notes.md"}), _call({"path": "notes.md"})]
    request_id = _start(scheduler, clock)
    _approve(store, request_id, {"path": "notes.md"})
    scheduler.resume_waiting_run(request_id, by=HUMAN)
    assert len(SENT) == 2


def _reason(scheduler):
    return _ended(scheduler)["reason"]


def test_reject_and_expiry_end_the_run_at_the_next_tick(world):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    store.decide(request_id, "reject", approver="local:operator")
    _tick_at(scheduler, clock, T0 + 3610)
    assert _reason(scheduler) == "approval_rejected"
    assert SENT == []


def test_a_wait_past_its_limit_expires(world):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    _tick_at(scheduler, clock, T0 + 3600 + 3601)  # approval_wait_max_sec default 3600
    assert _reason(scheduler) == "approval_expired"
    assert store.get(request_id).status == "expired"


def test_a_waiting_run_counts_as_active_for_overlap(world):
    scheduler, _store, clock, _ = world
    _start(scheduler, clock)
    _tick_at(scheduler, clock, T0 + 7200)  # the next hourly fire
    runs = scheduler.runs()
    assert len(runs) == 1 and runs[0]["status"] == "awaiting_approval"
    assert scheduler.status()  # still healthy


@pytest.mark.parametrize("how", ["pause", "critical", "stop", "update"])
def test_pause_critical_stop_or_update_end_a_waiting_run_and_revoke(world, how):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    _approve(store, request_id, {"path": "notes.md"})  # approved, not yet continued
    if how == "pause":
        scheduler.pause_job("digest", by=HUMAN)
    elif how == "critical":
        scheduler._breaker_level = lambda: "critical"
        _tick_at(scheduler, clock, T0 + 3610)
    elif how == "update":
        scheduler.update_job("digest", _wait_job(prompt="other"), by=HUMAN, confirm=True)
    else:
        scheduler.stop()
    run = _ended(scheduler)
    assert run["status"] == "cancelled", run
    assert SENT == []
    record = store.get(request_id)
    assert record.status == "cancelled" and not record.consumed


def test_a_breaker_off_normal_at_resume_refuses_the_continuation(world):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    _approve(store, request_id, {"path": "notes.md"})
    scheduler._breaker_level = lambda: "warning"
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is False
    assert _ended(scheduler)["status"] == "cancelled" and SENT == []


def test_a_restart_cancels_job_requests_and_closes_the_run(world, tmp_path):
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    scheduler.stop()
    # A fresh process: a new registry over the same store, the same state directory.
    waits = JobApprovalWaits(store, now_fn=clock)
    again = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=GatedJobExecutor(lambda *a: None, breaker_level=Breaker(), waits=waits),
        dispatch=lambda work: work(),
        approval_waits=waits,
    )
    again.start(background=False)
    try:
        assert store.get(request_id).status == "cancelled"
        assert again.runs()[0]["status"] == "cancelled"
        assert again.waiting_runs() == []
        assert _approve(store, request_id, {"path": "notes.md"}).name != "ACCEPTED"
    finally:
        again.stop()


def test_a_store_that_cannot_cancel_at_start_keeps_the_scheduler_down(tmp_path):
    class Broken:
        def cancel_at_start(self):
            raise RuntimeError("down")

    scheduler = Scheduler(tmp_path, approval_waits=Broken())
    with pytest.raises(Exception):  # noqa: B017 - SchedulerLogError, state unavailable
        scheduler.start(background=False)
    assert scheduler.state.value == "unavailable"


def test_at_most_three_runs_wait_per_server(world):
    from stiracore.control.job_approvals import MAX_WAITING_RUNS

    assert MAX_WAITING_RUNS == 3


def _broker(store, scheduler, clock):
    from stiracore.observability.chat_api import HITLApprovalBroker

    return HITLApprovalBroker(store=store, now_fn=clock, on_reject=scheduler.note_approval_rejected)


def _reject_in_dashboard(broker, request_id):
    from stiracore.core.engine import HITLAction, HITLDecision

    return broker.decide(request_id, HITLDecision(HITLAction.REJECT), resolver=HUMAN)


def _no_tick(scheduler, monkeypatch):
    def forbidden():
        raise AssertionError("tick must not run")

    monkeypatch.setattr(scheduler, "tick", forbidden)


def test_a_dashboard_rejection_ends_the_run_without_waiting_for_a_tick(world, monkeypatch):
    """#556 follow-up: the rejection is reflected at once (no tick, the clock is still)."""
    scheduler, store, clock, _ = world
    broker = _broker(store, scheduler, clock)  # the dashboard is up before the run
    request_id = _start(scheduler, clock)
    _no_tick(scheduler, monkeypatch)
    _reject_in_dashboard(broker, request_id)
    assert store.get(request_id).status == "rejected"
    assert _reason(scheduler) == "approval_rejected"
    assert scheduler.waiting_runs() == []
    assert SENT == []


def test_mutation_without_the_wake_the_run_waits_for_the_next_tick(world, monkeypatch):
    scheduler, store, clock, _ = world
    monkeypatch.setattr(Scheduler, "note_approval_rejected", lambda self, request_id: False)
    broker = _broker(store, scheduler, clock)
    request_id = _start(scheduler, clock)
    _no_tick(scheduler, monkeypatch)
    _reject_in_dashboard(broker, request_id)
    assert store.get(request_id).status == "rejected"
    assert _ended(scheduler, timeout=0.3)["status"] == "awaiting_approval"  # still parked
    monkeypatch.undo()
    _tick_at(scheduler, clock, T0 + 3610)
    assert _reason(scheduler) == "approval_rejected"


def test_the_wake_never_approves_or_resumes(world):
    """Called on a pending or approved request it changes nothing (reject only)."""
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    assert scheduler.note_approval_rejected(request_id) is True  # still pending: stays
    assert scheduler.runs()[0]["status"] == "awaiting_approval"
    assert _approve(store, request_id, {"path": "notes.md"})
    scheduler.note_approval_rejected(request_id)
    assert scheduler.runs()[0]["status"] == "awaiting_approval"
    assert SENT == []  # only resume_waiting_run continues a run
    assert scheduler.note_approval_rejected("job-unknown") is False


def _dashboard_approval(scheduler, store, request_id):
    """The chat server's ``resume_job_after_approval``: approve, continue through the
    scheduler; a refusal revokes the approval (``dashboard/stira_server.py``)."""
    assert _approve(store, request_id, {"path": "notes.md"})
    try:
        return scheduler.resume_waiting_run(request_id, by=HUMAN)
    except SchedulerRefused:
        store.cancel(request_id, reason="no run waits for this approval")
        return None


def _approve_before_the_park(monkeypatch, scheduler, store, seam):
    """The approval arrives in the window where the request is stored (and listed in
    the dashboard) but the run is not parked yet: as soon as it is stored (``stored``) or
    when the first segment ended and is about to park (``parking``)."""
    from stiracore.scheduler.scheduler import RunContext

    answers = []
    if seam == "stored":
        original = RunContext.wait_for_approval

        def wait_for_approval(self, request_id, tool_name, digest):
            answers.append((request_id, _dashboard_approval(scheduler, store, request_id)))
            original(self, request_id, tool_name, digest)

        monkeypatch.setattr(RunContext, "wait_for_approval", wait_for_approval)
    else:
        park = Scheduler._park

        def parking(self, ctx, outcome):
            answers.append(
                (ctx.waiting_request, _dashboard_approval(scheduler, store, ctx.waiting_request))
            )
            park(self, ctx, outcome)

        monkeypatch.setattr(Scheduler, "_park", parking)
    return answers


@pytest.mark.parametrize("seam", ["stored", "parking"])
def test_an_approval_before_the_run_parks_continues_it(world, monkeypatch, seam):
    """The A8 window (#296): approved while the first segment was still ending, the run
    is not cancelled with its approval revoked; it continues once parked, only the
    approved call runs, once."""
    scheduler, store, clock, _ = world
    answers = _approve_before_the_park(monkeypatch, scheduler, store, seam)
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    ((request_id, answer),) = answers
    assert answer is not None and answer["resumed"] is True, answer
    run = _ended(scheduler)
    assert run["status"] == "completed", run
    assert SENT == [{"tool": "memory_read", "input": {"path": "notes.md"}}]
    assert store.open_records() == []
    assert store.get(request_id).consumed
    assert scheduler.waiting_runs() == [] and scheduler._resume_on_park == {}


@pytest.mark.parametrize("how", ["cancel", "pause"])
def test_an_early_approval_of_a_run_that_ends_instead_runs_nothing(world, monkeypatch, how):
    """Approved before the park, then the run is cancelled (it never parks) or its job is
    paused (it parks, the continuation's checks refuse): nothing runs, the approval is
    revoked and no continuation is kept for later."""
    from stiracore.scheduler.scheduler import RunContext

    scheduler, store, clock, _ = world
    answers = _approve_before_the_park(monkeypatch, scheduler, store, "stored")
    approve_then = RunContext.wait_for_approval

    def and_end(self, request_id, tool_name, digest):
        approve_then(self, request_id, tool_name, digest)
        if how == "cancel":
            scheduler.cancel_run(self.run_id, by=HUMAN)
        else:
            scheduler.pause_job("digest", by=HUMAN)

    monkeypatch.setattr(RunContext, "wait_for_approval", and_end)
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    ((request_id, answer),) = answers
    assert answer is not None and answer["resumed"] is True  # for a park that may not come
    run = _ended(scheduler)
    expected = "operator" if how == "cancel" else "paused"
    assert run["status"] == "cancelled" and run["reason"] == expected, run
    assert SENT == []
    record = store.get(request_id)
    assert record.status == "cancelled" and not record.consumed
    assert scheduler._resume_on_park == {} and scheduler.waiting_runs() == []


def test_an_early_continuation_without_an_approval_ends_the_run(world, monkeypatch):
    """As for a parked run: continuing a request that is not approved (still pending)
    refuses and ends the run, even before it parked; nothing runs."""
    from stiracore.scheduler.scheduler import RunContext

    scheduler, _store, clock, _ = world
    original, answers = RunContext.wait_for_approval, []

    def early_resume(self, request_id, tool_name, digest):
        answers.append(scheduler.resume_waiting_run(request_id, by=HUMAN))
        original(self, request_id, tool_name, digest)

    monkeypatch.setattr(RunContext, "wait_for_approval", early_resume)
    scheduler.create_job(_wait_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert answers == [
        {"run_id": answers[0]["run_id"], "resumed": False, "reason": "approval_unavailable"}
    ]
    run = _ended(scheduler)
    assert run["status"] == "cancelled" and run["reason"] == "approval_unavailable", run
    assert SENT == [] and scheduler._resume_on_park == {}


def test_a_second_continuation_while_the_first_runs_is_refused(world, monkeypatch):
    """A run being continued is not an early wait: another resume of its request is
    refused (not found) and never cancels the running continuation."""
    scheduler, store, clock, _ = world
    request_id = _start(scheduler, clock)
    original, refusals = GatedJobExecutor.resume, []

    def resume(self, ctx, request):
        try:
            refusals.append(scheduler.resume_waiting_run(request, by=HUMAN))
        except SchedulerRefused as refused:
            refusals.append(refused.code)
        return original(self, ctx, request)

    monkeypatch.setattr(GatedJobExecutor, "resume", resume)
    assert _approve(store, request_id, {"path": "notes.md"})
    assert scheduler.resume_waiting_run(request_id, by=HUMAN)["resumed"] is True
    assert refusals == ["not_found"]
    assert scheduler.runs()[0]["status"] == "completed"
    assert SENT == [{"tool": "memory_read", "input": {"path": "notes.md"}}]
