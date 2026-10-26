"""#400: a live request between "saved to the store" and "registered in memory".

Deterministic: the store's ``submit`` is wrapped so the checks run right after the real
save and before the worker registers the request (no sleeps, no CPU load).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from test_hitl_binding import CALL, ExternalService, _app, _request, _results
from test_hitl_persistence import Host

from stiracore.agent.hitl_resume import resume_approved_request
from stiracore.core import HITLAction, HITLDecision
from stiracore.core.approval_store import APPROVED, ApprovalStore, ApprovalStoreError
from stiracore.observability.chat_api import DecideOutcome, HITLApprovalBroker


def _after_save(store, hook):
    """Run ``hook(request_id)`` in another thread (the UI) after each real save."""
    real = store.submit
    calls = []

    def submit(**fields):
        saved = real(**fields)
        calls.append(fields["request_id"])
        if len(calls) > 1:
            return saved  # only the first save is intercepted (no nested hooks)
        ui = threading.Thread(target=hook, args=(fields["request_id"],))
        ui.start()
        ui.join(timeout=5)
        assert not ui.is_alive()
        return saved

    store.submit = submit


def _live(broker, worker, timeout=5.0):
    """The listed requests once the worker registered its own (it must still wait)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = broker.pending()
        if listed:
            return listed
        if worker.done():
            result = worker.result()
            raise AssertionError(f"the worker stopped waiting: {getattr(result, 'reason', result)}")
        time.sleep(0.005)
    raise AssertionError("approval request did not appear")


def test_a_request_being_saved_is_not_listed_as_restored(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=5)
    seen = {}

    def hook(request_id):
        seen["stored"] = [r.request_id for r in store.pending()]
        seen["pending"] = broker.pending()
        seen["http"] = _app(broker).get("/api/hitl/pending").json["requests"]

    _after_save(store, hook)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, _request(agent_id="ext-agent"))
        (live,) = _live(broker, future)
        assert "restored" not in live
        assert broker.decide(live["id"], HITLDecision(HITLAction.REJECT))
        assert future.result(timeout=2).action == HITLAction.REJECT
    assert seen["stored"] == [live["id"]], "the hook ran inside the save window"
    assert seen["pending"] == [] and seen["http"] == []


def test_approving_in_the_save_window_never_takes_the_resume_path(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    resumes = []
    broker = HITLApprovalBroker(store=store, ttl_sec=5, on_resume=resumes.append)
    seen = {}

    def hook(request_id):
        record = store.get(request_id)
        seen["approve"] = broker.decide(
            request_id, HITLDecision(HITLAction.APPROVE), args_hash=record.args_hash
        )
        seen["reject"] = broker.decide(request_id, HITLDecision(HITLAction.REJECT))

    _after_save(store, hook)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, _request(agent_id="ext-agent"))
        (live,) = _live(broker, future)
        # Nothing was recorded in the window: the live worker is still waiting.
        assert store.get(live["id"]).status == "pending"
        assert broker.decide(
            live["id"], HITLDecision(HITLAction.APPROVE), args_hash=live["args_hash"]
        )
        assert future.result(timeout=2).action == HITLAction.APPROVE
    assert seen == {"approve": DecideOutcome.NOT_FOUND, "reject": DecideOutcome.NOT_FOUND}
    assert resumes == [], "a running turn is never resumed"
    record = store.get(live["id"])
    assert (record.status, record.consumed, record.resume_claimed) == (APPROVED, True, False)
    # A repeated approval is refused; still nothing resumes.
    again = broker.decide(live["id"], HITLDecision(HITLAction.APPROVE), args_hash=live["args_hash"])
    assert again is DecideOutcome.ALREADY_DECIDED and resumes == []


def test_turn_approved_in_the_save_window_runs_the_tool_once_on_the_live_path(tmp_path):
    """End to end: before #400 the window approval scheduled a resume, the live call was
    rejected, and the resume replayed the call after the turn had reported it failed."""
    service = ExternalService()
    host = Host(tmp_path, [[{"toolUse": CALL}], [{"text": "done"}]], service)
    scheduled = []
    host.broker._on_resume = scheduled.append  # the chat server: queued behind the turn lock
    outcomes = []

    def hook(request_id):
        record = host.store.get(request_id)
        outcomes.append(
            host.broker.decide(
                request_id, HITLDecision(HITLAction.APPROVE), args_hash=record.args_hash
            )
        )

    _after_save(host.store, hook)
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(host.agent, "update")
        (live,) = _live(host.broker, turn)
        assert "restored" not in live
        assert host.broker.decide(
            live["id"], HITLDecision(HITLAction.APPROVE), args_hash=live["args_hash"]
        )
        assert str(turn.result(timeout=5)).strip() == "done"
    assert outcomes == [DecideOutcome.NOT_FOUND] and scheduled == []
    assert _results(host.agent)["ext-1"]["status"] == "success"
    assert [(r, v) for r, v, _key in service.applied] == [("r1", "v1")]
    # Even a stray resume of that request runs nothing more.
    assert not resume_approved_request(host.agent, host.store, live["id"]).resumed
    assert len(service.applied) == 1


def test_a_failed_save_leaves_nothing_being_saved(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=5)

    def failing(**_fields):
        raise ApprovalStoreError("disk full")

    store.submit = failing
    decision = broker.request(_request(agent_id="ext-agent"))
    assert decision.action == HITLAction.REJECT
    assert decision.reason == "approval store unavailable"
    assert broker.pending() == [] and broker._saving == 0
    assert not getattr(broker, "_saving_ids", set())
    assert broker.close(timeout=0.5) is True


def test_a_duplicate_id_is_refused_while_the_first_is_being_saved(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=5)
    seen = {}

    def hook(request_id):
        seen["duplicate"] = broker.request(_request(agent_id="ext-agent", request_id=request_id))

    _after_save(store, hook)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, _request(agent_id="ext-agent", request_id="dup-1"))
        _live(broker, future)
        assert broker.decide("dup-1", HITLDecision(HITLAction.REJECT))
        assert future.result(timeout=2).action == HITLAction.REJECT
    assert seen["duplicate"].action == HITLAction.REJECT
    assert seen["duplicate"].reason == "duplicate approval request id"
