"""#279: the approval history adds ``resolution_reason_code`` for server-owned closings.

``resolution_reason`` (the audit sentence) is unchanged. Only the sentences the server
closes a request with, and only when the server resolved it (resolver ``system``), map
to a code; an operator's reject reason is never turned into a code, even if it reads
the same. The UI words known codes (``approvals.js``) and shows anything else as sent.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from test_hitl_binding import _app, _wait_pending

from stiracore.core import BreakLevel, HITLAction, HITLDecision, HITLRequest
from stiracore.core import hitl_reasons as reasons
from stiracore.core.approval_store import ApprovalStore
from stiracore.observability.chat_api import HITLApprovalBroker


def _request(tool_use_id, value="v"):
    return HITLRequest(
        tool_name="external_update",
        tool_input={"record": tool_use_id, "value": value},
        break_level=BreakLevel.CRITICAL,
        reason=reasons.CRITICAL_HALT,
        tool_use_id=tool_use_id,
        agent_id="a",
    )


def test_only_server_sentences_resolved_by_the_system_map_to_codes():
    expected = {
        reasons.EXPIRED: "expired",
        reasons.STORE_EXPIRED: "expired",
        reasons.CLOSED: "closed",
        reasons.REPLACED: "replaced",
        reasons.BINDING_MISMATCH: "binding_mismatch",
        reasons.LIVE_ONLY: "live_only",
        reasons.RULES_CHANGED: "rules_changed",
        reasons.BREAKER_STRICTER: "breaker_stricter",
        reasons.JOB_RESTART: "job_restart",
        reasons.GRANT_STOPPED: "grant_stopped",
    }
    for sentence, code in expected.items():
        assert reasons.resolution_code(sentence, "system") == code
        # The same words typed by an operator stay the operator's text.
        assert reasons.resolution_code(sentence, "operator") is None
        assert reasons.resolution_code(sentence, None) is None
    for other in (
        None,
        42,
        "",
        reasons.EXPIRED + " ",
        reasons.EXPIRED.upper(),
        f"note: {reasons.CLOSED}",
        "rules_changed",  # a code is not a sentence
        reasons.CRITICAL_HALT,  # a request reason, not a closing
    ):
        assert reasons.resolution_code(other, "system") is None, other


def test_live_history_codes_for_close_and_operator_reasons(tmp_path):
    broker = HITLApprovalBroker(store=ApprovalStore(tmp_path / "a.db"), ttl_sec=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        typed = pool.submit(broker.request, _request("u1"))
        closed = pool.submit(broker.request, _request("u2"))
        pending = {item["tool_use_id"]: item for item in _wait_pending(broker, count=2)}
        # The operator types the server's own sentence as their reason.
        broker.decide(
            pending["u1"]["id"],
            HITLDecision(HITLAction.REJECT, reason=reasons.EXPIRED),
            resolver="operator",
        )
        assert typed.result(timeout=2).action == HITLAction.REJECT
        broker.close()
        assert closed.result(timeout=2).action == HITLAction.REJECT
    for view in (broker.history(), _app(broker).get("/api/hitl/history").json["history"]):
        rows = {item["tool_use_id"]: item for item in view}
        assert rows["u1"]["resolution_reason"] == reasons.EXPIRED
        assert rows["u1"]["resolver"] == "operator"
        assert rows["u1"]["resolution_reason_code"] is None
        assert rows["u2"]["resolution_reason"] == reasons.CLOSED
        assert rows["u2"]["resolver"] == "system"
        assert rows["u2"]["resolution_reason_code"] == "closed"


def test_live_expiry_and_restored_records_carry_the_code(tmp_path):
    path = tmp_path / "a.db"
    # One fake clock for the broker *and* the store. With the store on the wall clock, a
    # request that expires at fake-time 1005 was already past due for the store, so its
    # sweep (run by every ``pending()`` poll) could expire it before the test saw it
    # pending: an intermittent "approval request did not appear" (#356).
    clock = [1000.0]

    def now():
        return clock[0]

    broker = HITLApprovalBroker(store=ApprovalStore(path, now_fn=now), ttl_sec=5, now_fn=now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(broker.request, _request("u1"))
        _wait_pending(broker)
        clock[0] += 10
        assert waiting.result(timeout=3).action == HITLAction.REJECT
    live = {item["tool_use_id"]: item for item in broker.history()}
    # Whichever side noticed first (broker wait or store sweep) words it as expired.
    assert live["u1"]["resolution_reason"] in (reasons.EXPIRED, reasons.STORE_EXPIRED)
    assert live["u1"]["resolution_reason_code"] == "expired"

    # A restored request revoked by the server (tool rules changed) keeps its code.
    store = ApprovalStore(path, now_fn=now)
    with ThreadPoolExecutor(max_workers=1) as pool:
        second = HITLApprovalBroker(store=store, ttl_sec=30, now_fn=now)
        revoked = pool.submit(second.request, _request("u2", "w"))
        request_id = _wait_pending(second)[0]["id"]
        store.cancel(request_id, reason=reasons.RULES_CHANGED)
        second.close()
        revoked.result(timeout=3)
    restarted = HITLApprovalBroker(store=ApprovalStore(path, now_fn=now), ttl_sec=30, now_fn=now)
    rows = {item["tool_use_id"]: item for item in restarted.history()}
    assert rows["u2"]["resolution_reason"] == reasons.RULES_CHANGED
    assert rows["u2"]["resolver"] == "system"
    assert rows["u2"]["resolution_reason_code"] == "rules_changed"
    assert rows["u1"]["resolution_reason_code"] == "expired"
