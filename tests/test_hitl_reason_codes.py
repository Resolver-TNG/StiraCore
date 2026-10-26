"""#279: the approval API adds a ``reason_code`` for server-owned reasons (UI words it).

The reason sentence itself is unchanged (audit text in the store and logs). Only exact,
server-owned sentences map to a code; anything else keeps ``reason_code: None`` so the
UI shows the sentence as before. What is approved (tool, arguments, hash) is untouched.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from test_hitl_binding import _app, _wait_pending

from stiracore.core import BreakLevel, HITLAction, HITLRequest
from stiracore.core import hitl_reasons as reasons
from stiracore.core.approval_store import ApprovalStore
from stiracore.observability.chat_api import HITLApprovalBroker


def test_only_exact_server_sentences_map_to_codes():
    assert reasons.reason_code(reasons.CRITICAL_HALT) == "breaker_critical_halt"
    assert reasons.reason_code(reasons.RULE_ASK) == "rule_ask"
    for other in (
        None,
        42,
        "",
        "test",
        reasons.CRITICAL_HALT + " ",
        reasons.CRITICAL_HALT.lower(),
        f"prefix {reasons.RULE_ASK}",
        "breaker_critical_halt",  # a code is not a reason sentence
    ):
        assert reasons.reason_code(other) is None, other


def _request(reason, tool_use_id, value):
    return HITLRequest(
        tool_name="external_update",
        tool_input={"record": tool_use_id, "value": value},
        break_level=BreakLevel.CRITICAL,
        reason=reason,
        tool_use_id=tool_use_id,
        agent_id="a",
    )


def test_live_and_restored_pending_carry_the_code_and_keep_the_sentence(tmp_path):
    path = tmp_path / "a.db"
    broker = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        halt = pool.submit(broker.request, _request(reasons.CRITICAL_HALT, "u1", "v1"))
        other = pool.submit(broker.request, _request("something else", "u2", "v2"))
        live = {item["tool_use_id"]: item for item in _wait_pending(broker, count=2)}
        api = {
            item["tool_use_id"]: item
            for item in _app(broker).get("/api/hitl/pending").json["requests"]
        }
        broker.close()
        assert halt.result(timeout=2).action == HITLAction.REJECT
        assert other.result(timeout=2).action == HITLAction.REJECT
    for view in (live, api):
        assert view["u1"]["reason"] == reasons.CRITICAL_HALT
        assert view["u1"]["reason_code"] == "breaker_critical_halt"
        assert view["u2"]["reason"] == "something else"
        assert view["u2"]["reason_code"] is None
        # The approval target is unchanged by the code.
        assert view["u1"]["tool_name"] == "external_update"
        assert view["u1"]["args_hash"] and view["u1"]["normalized_input"]

    restarted = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    restored = {item["tool_use_id"]: item for item in restarted.pending()}
    assert restored["u1"]["restored"] is True
    assert restored["u1"]["reason_code"] == "breaker_critical_halt"
    assert restored["u1"]["args_hash"] == live["u1"]["args_hash"]
    assert restored["u2"]["reason_code"] is None


def test_reason_params_come_from_an_allowlist_and_only_for_server_sentences():
    """#279: ``reason_params`` is ``{"level": <breaker level word>}`` or ``{}``; no free text."""
    assert reasons.reason_params(reasons.CRITICAL_HALT, BreakLevel.CRITICAL) == {
        "level": "critical"
    }
    assert reasons.reason_params(reasons.RULE_ASK, "WARNING") == {"level": "warning"}
    assert reasons.reason_params(reasons.RULE_ASK, "coherence") == {"level": "coherence"}
    # A sentence the server does not own, or a level outside the list: nothing is carried.
    assert reasons.reason_params("something else", BreakLevel.CRITICAL) == {}
    assert reasons.reason_params(reasons.RULE_ASK, "sk-not-a-level") == {}
    assert reasons.reason_params(reasons.RULE_ASK, None) == {}
    assert reasons.reason_params(None, "warning") == {}


def test_api_carries_reason_params_live_and_restored_and_sse_shape_is_unchanged(tmp_path):
    path = tmp_path / "p.db"
    broker = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    with ThreadPoolExecutor(max_workers=2) as pool:
        halt = pool.submit(broker.request, _request(reasons.CRITICAL_HALT, "u1", "v1"))
        other = pool.submit(broker.request, _request("something else", "u2", "v2"))
        live = {item["tool_use_id"]: item for item in _wait_pending(broker, count=2)}
        api = {
            item["tool_use_id"]: item
            for item in _app(broker).get("/api/hitl/pending").json["requests"]
        }
        broker.close()
        halt.result(timeout=2)
        other.result(timeout=2)
    for view in (live, api):
        assert view["u1"]["reason_params"] == {"level": "critical"}
        assert view["u2"]["reason_params"] == {}
    restarted = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    restored = {item["tool_use_id"]: item for item in restarted.pending()}
    assert restored["u1"]["reason_params"] == {"level": "critical"}
    assert restored["u2"]["reason_params"] == {}
    # The SSE event keeps its shape (#186): the new field is API-only.
    from stiracore.observability.chat_api import _approval_data

    assert "reason_params" not in _approval_data(live["u1"])
