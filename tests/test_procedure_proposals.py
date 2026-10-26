"""#463: the agent's ``propose_procedure`` (staging only) and the operator's review screen.

Measured offline (FakeModel / the real chat server app on an offline model, temporary data
directories, no cloud call). What is fixed here:

* the tool writes ``procedural/staging/<key>`` only — never ``procedural/<key>`` or another
  layer — and ``source_turn`` is the harness's turn id, whatever the model sends;
* secret-looking values, oversized bodies and too many proposals are refused, nothing
  written;
* the review routes approve (provenance ``source_turn`` / ``approved_by`` = the OS account
  like the CLI / ``approved_at``) and reject (the reason kept); both need ``confirm`` and
  the SHA-256 of what was shown; approve is refused while a turn runs;
* an approved rule is in "Procedural Rules" from the next turn; the static prompt part is
  byte-identical on the turns before and the turns after (one change, at the approval);
* the audit rows carry key, turn, approver and the body hash — never the body;
* the tool rule defaults to Ask (Ask or Never only) and YOLO never answers it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.agent.context_assembler import ContextAssembler
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.control import procedural_cli
from stiracore.control.procedural_proposals import (
    REVIEW_LOG_NAME,
    ProcedureProposals,
    ProposalLimits,
    ProposalRefused,
)
from stiracore.core import StiraEngine
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.procedural_provenance import provenance_problem
from stiracore.tools import tool_policy, tool_profiles, tool_registry
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.tools.memory_tools import create_memory_tools
from stiracore.tools.procedure_tool import (
    PROPOSE_PROCEDURE_TOOL_NAME,
    create_propose_procedure_tool,
    unbound_propose_procedure,
)
from stiracore.tools.tool_config import ToolConfig
from stiracore.yolo import tiers
from stiracore.yolo.targets import yolo_targets
from tests.provider_fakes import FakeModel, text_turn, tool_turn

BODY = "1. Read the file first.\n2. Make the smallest change.\n3. Read it back."
SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"  # an AWS access key id shape (mask_secrets knows it)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _proc(store: MemoryStore) -> Path:
    return store.memory_dir / MemoryLayer.PROCEDURAL.value


def _tree(store: MemoryStore) -> list[str]:
    return sorted(
        p.relative_to(store.memory_dir).as_posix() for p in store.memory_dir.rglob("*.md")
    )


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    events = SQLiteEventStore(tmp_path / "events.db")
    turn = SimpleNamespace(id="turn-463-a")
    proposals = ProcedureProposals(
        store,
        review_log=tmp_path / REVIEW_LOG_NAME,
        audit=AuditChain(events).record_procedural_write,
    )
    tool = create_propose_procedure_tool(proposals, lambda: turn.id)
    yield SimpleNamespace(
        store=store, events=events, turn=turn, proposals=proposals, tool=tool, tmp=tmp_path
    )
    events.close()


def _agent_run(env, attempts, *, extra_tools=()):
    """One turn of a FakeModel agent that calls ``attempts``; returns the tool results."""
    tools = [*create_memory_tools(env.store), env.tool, *extra_tools]
    model = FakeModel([*attempts, text_turn("done")])
    agent = NXPAgent(model=model, tools=tools, stimulus_classifier=Mock(classify=lambda **k: None))
    agent("remember how to edit files safely")
    return [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


def _text(result) -> str:
    return " ".join(c.get("text", "") for c in result["content"])


# --- requirement 1 / acceptance 1-3: staging only, harness turn, limits ---------------------


def test_the_tool_writes_procedural_staging_only(env):
    store = env.store
    store.write(MemoryLayer.EPISODIC, "note", "an episode", {})
    before = _tree(store)
    attempts = [
        tool_turn("propose_procedure", {"key": "safe_edit", "body": BODY, "rationale": "r"}, "p1"),
        # A path, a staging prefix, an upper-case or dotted key never leaves one component.
        tool_turn("propose_procedure", {"key": "staging/x", "body": BODY, "rationale": ""}, "p2"),
        tool_turn("propose_procedure", {"key": "../episodic/x", "body": BODY}, "p3"),
    ]
    results = _agent_run(env, attempts)
    assert [r["status"] for r in results] == ["success", "error", "error"], results
    assert "E_INVALID_KEY" in _text(results[1]) and "E_INVALID_KEY" in _text(results[2])
    after = _tree(store)
    assert sorted(set(after) - set(before)) == ["procedural/staging/safe_edit.md"]
    assert not (_proc(store) / "safe_edit.md").exists()
    staged = store.read(MemoryLayer.PROCEDURAL, "staging/safe_edit")
    assert staged.content == BODY
    # Not a rule: the prompt does not read it.
    prompt = ContextAssembler(
        "stira", store, StiraEngine(), Path("/nonexistent")
    ).build_system_prompt()
    assert "smallest change" not in prompt


def test_source_turn_is_the_harness_value_not_the_model_argument(env):
    attempts = [
        tool_turn(
            "propose_procedure",
            {
                "key": "k1",
                "body": BODY,
                "rationale": "r",
                "source_turn": "forged-turn",
                "approved_by": "local:attacker",
                "review": "approved",
            },
            "p1",
        )
    ]
    results = _agent_run(env, attempts)
    # Measured (strands 1.57): unknown arguments are dropped, the call succeeds.
    assert results[0]["status"] == "success", results
    staged = env.store.read(MemoryLayer.PROCEDURAL, "staging/k1")
    assert staged.metadata["source_turn"] == "turn-463-a"
    assert "approved_by" not in staged.metadata and "review" not in staged.metadata
    raw = (_proc(env.store) / "staging" / "k1.md").read_text("utf-8")
    assert "forged-turn" not in raw and "attacker" not in raw
    # The tool's schema has no way to name a turn or an approver.
    props = env.tool.tool_spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"key", "body", "rationale"}
    assert env.tool.tool_spec == unbound_propose_procedure.tool_spec


def test_harness_metadata_and_no_turn(env):
    result = env.proposals.propose("k2", BODY, "because", source_turn="turn-463-a")
    entry = env.store.read(MemoryLayer.PROCEDURAL, "staging/k2")
    assert result["sha256"] == _sha(BODY) and result["revision"] is False
    assert entry.metadata["proposed_via"] == "propose_procedure"
    assert entry.metadata["origin"] == "model"
    assert entry.metadata["rationale"] == "because"
    with pytest.raises(ProposalRefused, match="E_NO_TURN"):
        env.proposals.propose("k3", BODY, "", source_turn=None)


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("body", "E_SECRET_IN_CONTENT"),
        ("rationale", "E_SECRET_IN_CONTENT"),
    ],
)
def test_secret_like_values_are_refused(env, field, code):
    args = {"key": "k", "body": BODY, "rationale": "r"}
    args[field] = f"{args[field]} use {SECRET} here"
    results = _agent_run(env, [tool_turn("propose_procedure", args, "s1")])
    assert results[0]["status"] == "error" and code in _text(results[0])
    assert SECRET not in _text(results[0])
    assert not list(_proc(env.store).rglob("*.md"))


def test_size_and_rate_limits(env):
    limits = ProposalLimits(
        max_body_chars=50, max_rationale_chars=10, max_pending=3, max_per_turn=2, max_per_hour=3
    )
    clock = SimpleNamespace(now=1000.0)
    proposals = ProcedureProposals(
        env.store, review_log=env.tmp / "log.jsonl", limits=limits, clock=lambda: clock.now
    )
    with pytest.raises(ProposalRefused, match="E_BODY_TOO_LARGE"):
        proposals.propose("big", "x" * 51, "", source_turn="t1")
    with pytest.raises(ProposalRefused, match="E_RATIONALE_TOO_LARGE"):
        proposals.propose("why", "x", "y" * 11, source_turn="t1")
    proposals.propose("a", "x", "", source_turn="t1")
    proposals.propose("b", "x", "", source_turn="t1")
    with pytest.raises(ProposalRefused, match="E_RATE_TURN"):
        proposals.propose("c", "x", "", source_turn="t1")
    proposals.propose("c", "x", "", source_turn="t2")
    with pytest.raises(ProposalRefused, match="E_RATE_HOUR"):
        proposals.propose("d", "x", "", source_turn="t3")
    clock.now += 3600
    with pytest.raises(ProposalRefused, match="E_TOO_MANY_PENDING"):
        proposals.propose("d", "x", "", source_turn="t3")
    assert [e.key for e in proposals.review.list_staged()] == [
        "staging/a",
        "staging/b",
        "staging/c",
    ]


def test_a_known_key_is_staged_as_a_revision_and_the_rule_is_untouched(env, tmp_path):
    env.proposals.propose("style", "old rule", "", source_turn="t0")
    env.proposals.approve("style", sha256=_sha("old rule"))
    before = (_proc(env.store) / "style.md").read_bytes()
    result = env.proposals.propose("style", "new rule", "clearer", source_turn="t1")
    assert result["revision"] is True
    assert (_proc(env.store) / "style.md").read_bytes() == before
    view = env.proposals.overview()["proposals"][0]
    assert (
        view["revision"] and view["approvable"] and view["existing"]["sha256"] == _sha("old rule")
    )
    assert {"op": "-", "text": "old rule"} in view["diff"]
    assert {"op": "+", "text": "new rule"} in view["diff"]
    # Without replace (the CLI) the rule is never overwritten.
    with pytest.raises(ProposalRefused, match="E_TARGET_EXISTS"):
        env.proposals.approve("style", sha256=_sha("new rule"))
    out = env.proposals.approve("style", sha256=_sha("new rule"), replace=True)
    rule = env.store.read(MemoryLayer.PROCEDURAL, "style")
    assert out["written"] and rule.content == "new rule"
    assert rule.metadata["previous_sha256"] == _sha("old rule")
    assert provenance_problem(rule.metadata) is None
    # The replaced text is kept in the review log (not served by the overview).
    log = (env.tmp / REVIEW_LOG_NAME).read_text("utf-8")
    assert "old rule" in log
    assert all("previous_body" not in r for r in env.proposals.overview()["reviews"])


def test_a_revision_goes_stale_when_the_rule_changed(env):
    env.proposals.propose("r", "v1", "", source_turn="t0")
    env.proposals.approve("r", sha256=_sha("v1"))
    env.proposals.propose("r", "v2", "", source_turn="t1")
    # The operator replaces the rule by hand in between.
    rule = env.store.read(MemoryLayer.PROCEDURAL, "r")
    env.store.write(MemoryLayer.PROCEDURAL, "r", "v1-edited", dict(rule.metadata))
    assert env.proposals.overview()["proposals"][0]["problem"] == "E_REVISION_STALE"
    with pytest.raises(ProposalRefused, match="E_REVISION_STALE"):
        env.proposals.approve("r", sha256=_sha("v2"), replace=True)
    assert env.store.read(MemoryLayer.PROCEDURAL, "r").content == "v1-edited"


def test_an_unapproved_entry_of_the_same_key_is_not_revisable(env):
    env.store.write(MemoryLayer.PROCEDURAL, "legacy", "hand written", {})
    with pytest.raises(ProposalRefused, match="E_TARGET_EXISTS"):
        env.proposals.propose("legacy", BODY, "", source_turn="t1")


# --- requirement 2 (service level): approver, stale hash, reject reason ---------------------


def test_approve_records_the_os_account_like_the_cli(env):
    env.proposals.propose("k", BODY, "", source_turn="turn-463-a")
    with pytest.raises(ProposalRefused, match="E_STALE_PROPOSAL"):
        env.proposals.approve("k", sha256=_sha(BODY + "x"))
    result = env.proposals.approve("k", sha256=_sha(BODY))
    rule = env.store.read(MemoryLayer.PROCEDURAL, "k")
    assert result["approved_by"] == procedural_cli.operator_principal()
    assert rule.metadata["approved_by"] == procedural_cli.operator_principal()
    assert rule.metadata["source_turn"] == "turn-463-a"
    assert provenance_problem(rule.metadata) is None
    assert not (_proc(env.store) / "staging" / "k.md").exists()


def test_approve_is_refused_when_the_account_is_unknown(env, monkeypatch):
    env.proposals.propose("k", BODY, "", source_turn="t1")

    def broken():
        raise KeyError("no passwd entry")

    monkeypatch.setattr(procedural_cli, "_account_name", broken)
    proposals = ProcedureProposals(env.store, review_log=env.tmp / "l.jsonl")
    with pytest.raises(ProposalRefused, match="E_APPROVER_UNAVAILABLE"):
        proposals.approve("k", sha256=_sha(BODY))
    assert env.store.read(MemoryLayer.PROCEDURAL, "k") is None


def test_reject_keeps_the_masked_reason_and_writes_no_rule(env):
    env.proposals.propose("k", BODY, "", source_turn="t1")
    out = env.proposals.reject("k", sha256=_sha(BODY), reason=f"too vague; token {SECRET}")
    assert out["rejected"] == "k"
    assert not list(_proc(env.store).rglob("*.md"))
    review = env.proposals.overview()["reviews"][0]
    assert review["action"] == "rejected" and review["by"] == procedural_cli.operator_principal()
    assert review["reason"].startswith("too vague") and SECRET not in review["reason"]
    with pytest.raises(ProposalRefused, match="E_REASON_INVALID"):
        env.proposals.reject("k", sha256=_sha(BODY), reason="x" * 501)


# --- requirement 5 / acceptance 7: audit rows without the body --------------------------------


def test_audit_rows_carry_hashes_never_the_body(env):
    marker = "AUDIT-BODY-MARKER-463"
    env.proposals.propose("a", marker, "RATIONALE-MARKER", source_turn="turn-463-a")
    env.proposals.propose("b", marker + "2", "", source_turn="turn-463-b")
    env.proposals.approve("a", sha256=_sha(marker))
    env.proposals.reject("b", sha256=_sha(marker + "2"), reason="REASON-MARKER")
    with pytest.raises(ProposalRefused):
        env.proposals.propose("c", f"{marker} {SECRET}", "", source_turn="turn-463-c")
    events = [e for e in env.events.get_events() if e.event_type.startswith("audit.procedural.")]
    kinds = [e.event_type.rsplit(".", 1)[1] for e in events]
    assert kinds == ["proposed", "proposed", "approved", "rejected", "refused"]
    approved = events[2].attributes
    assert approved["entry_key"] == "a" and approved["content_hash"] == _sha(marker)
    assert approved["source_turn"] == "turn-463-a"
    assert approved["approved_by"] == procedural_cli.operator_principal()
    assert events[4].attributes["code"] == "E_SECRET_IN_CONTENT"
    raw = b"".join(
        bytes(str(row), "utf-8")
        for row in sqlite3.connect(env.tmp / "events.db").execute("SELECT * FROM events")
    )
    for text in (marker, "RATIONALE-MARKER", "REASON-MARKER", SECRET):
        assert text.encode() not in raw, text


# --- tool rules, profiles, YOLO -----------------------------------------------------------------


def test_the_rule_defaults_to_ask_and_can_be_ask_or_never_only(tmp_path):
    fresh = ToolConfig.first_run_defaults(tmp_path / "t.json")
    assert fresh.policy_of(PROPOSE_PROCEDURE_TOOL_NAME) == "ask"
    saved_always = ToolConfig(mode="custom", enabled_tools={"propose_procedure"}, ask_tools=set())
    assert saved_always.policy_of(PROPOSE_PROCEDURE_TOOL_NAME) == "ask"
    assert ToolConfig(mode="trust_all").policy_of(PROPOSE_PROCEDURE_TOOL_NAME) == "ask"
    # An existing custom install that never listed the new tool: Never (fail closed).
    old = ToolConfig(mode="custom", enabled_tools={"file_read"}, ask_tools=set())
    assert old.policy_of(PROPOSE_PROCEDURE_TOOL_NAME) == "never"
    assert not tool_policy.always_allowed(PROPOSE_PROCEDURE_TOOL_NAME)
    table = {
        p: tool_profiles.PROFILE_TABLES[p][PROPOSE_PROCEDURE_TOOL_NAME]
        for p in ("minimal", "dev", "research")
    }
    assert table == {"minimal": "never", "dev": "ask", "research": "ask"}


def test_yolo_never_answers_it_in_any_window():
    assert PROPOSE_PROCEDURE_TOOL_NAME in tiers.PERSON_TOOLS
    assert PROPOSE_PROCEDURE_TOOL_NAME not in tiers.INTERNAL_TOOLS
    assert tiers.tool_class(PROPOSE_PROCEDURE_TOOL_NAME) == tiers.EXEC
    assert PROPOSE_PROCEDURE_TOOL_NAME not in tiers.EXEC_AUTO_TOOLS
    rules = ToolConfig(
        mode="custom",
        enabled_tools={"propose_procedure", "file_write"},
        ask_tools={"propose_procedure", "file_write"},
    )
    targets = yolo_targets(rules, names={"propose_procedure", "file_write"}, exec_exposed=True)
    assert {"name": "propose_procedure", "reason": "person_tool"} in targets["excluded"]
    for scope in ("internal", "external", "exec"):
        assert PROPOSE_PROCEDURE_TOOL_NAME not in targets["scopes"][scope]["tools"]


def test_get_tools_never_hands_out_the_unbound_copy():
    assert PROPOSE_PROCEDURE_TOOL_NAME in tool_registry.WRITE_TOOLS
    assert PROPOSE_PROCEDURE_TOOL_NAME in tool_registry.HOST_BOUND_TOOLS
    for level in (None, "normal", "critical"):
        names = {
            getattr(t, "tool_name", None) for t in tool_registry.get_tools(level, include_exec=True)
        }
        assert PROPOSE_PROCEDURE_TOOL_NAME not in names
    names = {t.tool_name for t in ToolConfig(mode="trust_all").get_allowed_tools("normal")}
    assert PROPOSE_PROCEDURE_TOOL_NAME not in names
    with pytest.raises(Exception, match="E_NOT_BOUND"):
        unbound_propose_procedure(key="k", body="b", rationale="r")


def test_the_server_offers_one_bound_tool_and_hides_it_at_warning(env):
    from dashboard.stira_server import _stira_tools

    bound = (*create_memory_tools(env.store), env.tool)
    names = {"memory_write", PROPOSE_PROCEDURE_TOOL_NAME}
    normal = [getattr(t, "tool_name", None) for t in _stira_tools(bound, names, "normal")]
    assert normal.count(PROPOSE_PROCEDURE_TOOL_NAME) == 1
    warning = [getattr(t, "tool_name", None) for t in _stira_tools(bound, names, "warning")]
    assert PROPOSE_PROCEDURE_TOOL_NAME not in warning
