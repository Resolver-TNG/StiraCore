"""ADR 0025 PR 1: the session-allowance base (table, tokens, CAS, audit rows, races).

Offline, no model. Races are forced at named boundaries (``AllowanceService.hooks``, a
paused lock holder), never with ``sleep``. Every name below is listed in the PR's
invariant table (ADR 0025 §10).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from stiracore.core.engine import StiraEngine
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools.tool_config import rules_loosening
from stiracore.yolo import session_allow as sa
from stiracore.yolo.allow_class import (
    ORIGIN_CHAT,
    TargetClass,
    allow_again_eligibility,
    class_contains,
)
from stiracore.yolo.allow_service import AllowanceService
from stiracore.yolo.audit_schema import AuditContractError, build
from stiracore.yolo.host_ops import ClockReading

# --- fixtures -------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> ClockReading:
        return ClockReading(wall=self.t, boot=self.t, mono=self.t)


class Policy:
    """A minimal ``WritePathPolicy`` stand-in: one write root (the workspace)."""

    def __init__(self, root) -> None:
        self.workspace = str(root)
        self.write_roots = [str(root)]
        self.read_roots = [str(root)]
        self.protected_dirs = ()

    def absolute(self, text):
        from pathlib import Path

        path = Path(text).expanduser()
        return str(path if path.is_absolute() else Path(self.workspace) / path)

    def within_roots(self, path):
        import os
        from pathlib import Path

        root = Path(os.path.realpath(self.workspace))
        resolved = Path(os.path.realpath(path))
        return resolved == root or root in resolved.parents


class Env:
    """A service over a real hash-chained event store, with mutable rule / roots /
    breaker readers."""

    def __init__(self, tmp_path, *, hooks=None) -> None:
        self.root = tmp_path / "ws"
        (self.root / "game" / "sub").mkdir(parents=True)
        self.policy = Policy(self.root)
        self.clock = Clock()
        self.table = sa.AllowanceTable(clock=self.clock)
        self.store = SQLiteEventStore(tmp_path / "events.db", namespace="audit")
        self.chain = AuditChain(self.store)
        self.rule = ("ask", 7)
        self.level = "normal"
        self.digest = "d" * 64
        self.store_lock = threading.RLock()
        self.fail_record = False
        self.service = AllowanceService(
            self.table,
            record=self._record,
            ledger=self.chain,
            actor="local:tester",
            breaker=lambda: self.level,
            rules=lambda tool: self.rule,
            roots=lambda: self.digest,
            store_lock=self.store_lock,
            enabled=True,
            hooks=hooks,
        )
        self.decided = 0

    def _record(self, event_type, **fields):
        if self.fail_record:
            raise OSError("disk full")
        return self.chain.record_yolo(event_type, **fields)

    def decide(self) -> bool:
        self.decided += 1
        return True

    def call(self, name="a.py", directory="game"):
        return {"path": str(self.root / directory / name), "content": "x"}

    def offer(self, request_id="r1", tool_input=None):
        return self.service.offer(
            request_id=request_id,
            args_hash="h" * 64,
            tool="file_write",
            tool_input=tool_input or self.call(),
            policy=self.policy,
            origin=ORIGIN_CHAT,
        )

    def press(self, token, request_id="r1", tool_input=None):
        return self.service.press(
            token,
            request_id=request_id,
            args_hash="h" * 64,
            tool="file_write",
            tool_input=tool_input or self.call(),
            policy=self.policy,
            origin=ORIGIN_CHAT,
            decide=self.decide,
        )

    def allowed(self):
        view = self.offer()
        assert view["offered"], view
        result = self.press(view["token"])
        assert result.status == "allowed", result
        return result

    def answer(self, tool_input=None, args="a" * 64):
        return self.service.answer(
            tool="file_write",
            tool_input=tool_input or self.call("b.py"),
            policy=self.policy,
            origin=ORIGIN_CHAT,
            args_hash=args,
        )

    def consume(self, binding, tool_input=None, args="a" * 64):
        return self.service.consume(
            binding,
            tool="file_write",
            tool_input=tool_input or self.call("b.py"),
            policy=self.policy,
            origin=ORIGIN_CHAT,
            args_hash=args,
        )

    def rows(self, event_type):
        return [e.attributes for e in self.store.get_events(limit=500, event_type=event_type)]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# --- one code constant turns both buttons on (PR 3); a service built off refuses ----------


def test_buttons_on_in_pr3_and_off_without_the_constant():
    assert sa.ENABLED is True  # PR 3: both buttons together (a code constant, not a setting)
    service = AllowanceService(
        sa.AllowanceTable(),
        record=None,
        ledger=None,
        actor="local:x",
        breaker=lambda: "normal",
        rules=lambda tool: ("ask", 1),
        roots=lambda: "d" * 64,
        enabled=False,
    )
    assert service.offer(
        request_id="r", args_hash="h", tool="file_write", tool_input={}, policy=None, origin=None
    ) == {"offered": False, "reason": "not_enabled"}
    result = service.press(
        "t",
        request_id="r",
        args_hash="h",
        tool="file_write",
        tool_input={},
        policy=None,
        origin=None,
        decide=lambda: pytest.fail("never decided"),
    )
    assert (result.status, result.code) == ("refused", "not_enabled")
    assert service.answer(tool="x", tool_input={}, policy=None, origin=None, args_hash="h") is None


# --- eligibility (§3) -------------------------------------------------------------------


def test_eligibility_classes(tmp_path):
    e = Env(tmp_path)
    target = allow_again_eligibility("file_write", e.call(), e.policy, ORIGIN_CHAT)
    assert isinstance(target, TargetClass)
    assert target.class_kind == "dir_subtree"
    assert target.class_key.endswith("game")
    whole = allow_again_eligibility(
        "file_write", {"path": str(e.root / "x.txt"), "content": ""}, e.policy, ORIGIN_CHAT
    )
    assert whole.whole_root is True
    assert allow_again_eligibility(
        "memory_write", {"layer": "episodic", "key": "k", "content": "c"}, None, ORIGIN_CHAT
    ) == TargetClass("memory_write", "layer", "episodic")
    fetched = allow_again_eligibility(
        "web_fetch", {"url": "https://Docs.Python.org/3/"}, None, ORIGIN_CHAT
    )
    assert (fetched.class_kind, fetched.class_key) == ("host", "docs.python.org")
    get = allow_again_eligibility(
        "http_request", {"method": "GET", "url": "https://a.example/x"}, None, ORIGIN_CHAT
    )
    assert get.class_kind == "host_read"


@pytest.mark.parametrize(
    ("tool", "tool_input", "reason"),
    [
        ("shell", {"command": "ls"}, "exec"),
        ("run_checks", {}, "person_only"),
        ("file_read", {"path": "a"}, "read_unbound"),
        ("editor", {"command": "view", "path": "a"}, "read_unbound"),
        ("webhook_send", {"url": "https://a.example"}, "sends_data"),
        ("http_request", {"method": "POST", "url": "https://a.example"}, "sends_data"),
        ("web_fetch", {"url": "https://a.example/?api_key=sk-abcdefabcdefabcdef1234"}, None),
        ("some_mcp_tool", {}, "unclassified"),
        ("memory_write", {"layer": "semantic", "key": "k", "content": "c"}, "scope_unknown"),
    ],
)
def test_eligibility_not_offered(tmp_path, tool, tool_input, reason):
    e = Env(tmp_path)
    found = allow_again_eligibility(tool, tool_input, e.policy, ORIGIN_CHAT)
    assert isinstance(found, str)
    if reason is not None:
        assert found == reason


def test_secret_in_args_not_offered_for_internal_tools(tmp_path):
    """Invariant 4: a token in a memory text is never offered."""
    found = allow_again_eligibility(
        "memory_write",
        {"layer": "episodic", "key": "k", "content": "token AKIAABCDEFGHIJKLMNOP"},
        None,
        ORIGIN_CHAT,
    )
    assert found == "secret_in_args"


def test_outside_roots_and_symlink_not_offered(tmp_path):
    e = Env(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert (
        allow_again_eligibility(
            "file_write", {"path": str(outside / "a"), "content": ""}, e.policy, ORIGIN_CHAT
        )
        == "scope_outside"
    )
    (e.root / "link").symlink_to(e.root / "game", target_is_directory=True)
    assert (
        allow_again_eligibility(
            "file_write", {"path": str(e.root / "link" / "a"), "content": ""}, e.policy, ORIGIN_CHAT
        )
        == "scope_unknown"
    )


def test_other_origins_not_offered(tmp_path):
    e = Env(tmp_path)
    assert allow_again_eligibility("file_write", e.call(), e.policy, "job") == "not_this_session"


def test_class_contains_subtree_only(tmp_path):
    e = Env(tmp_path)
    base = allow_again_eligibility("file_write", e.call(), e.policy, ORIGIN_CHAT)
    below = allow_again_eligibility("file_write", e.call("b", "game/sub"), e.policy, ORIGIN_CHAT)
    beside = allow_again_eligibility("file_write", e.call("b", "."), e.policy, ORIGIN_CHAT)
    assert class_contains(base, below)
    assert not class_contains(base, beside)
    old = TargetClass(base.tool, base.class_kind, base.class_key, class_version=0)
    assert not class_contains(old, below)  # invariant 5: another class_version never matches


# --- issuance, use, consumption (§4) ------------------------------------------------------


def test_press_use_consume_and_reference_chain(env):
    """§7 reference chain: call row → issued_ref → issued row → decision_receipt_ref."""
    result = env.allowed()
    assert env.decided == 1
    binding = env.answer()
    assert binding is not None
    ran, code, intent = env.consume(binding)
    assert (ran, code) == (True, None) and intent
    assert env.table.get(result.allow_id).uses == 1
    started = env.rows("audit.tool.started")
    assert len(started) == 1 and started[0]["authorizer_kind"] == "session_allow"
    issued = env.store.receipt(started[0]["issued_ref"])
    assert issued["event_type"] == "audit.allow.issued"
    assert issued["attributes"]["allow_id"] == result.allow_id
    decided = env.store.receipt(issued["attributes"]["decision_receipt_ref"])
    assert decided["event_type"] == "audit.allow.decided"
    assert env.store.verify_chain()[0]


def test_binding_single_use(env):
    env.allowed()
    binding = env.answer()
    assert env.consume(binding)[0] is True
    ran, code, _ = env.consume(binding)
    assert (ran, code) == (False, "binding_spent")


def test_another_directory_goes_to_a_person(env):
    env.allowed()
    assert env.answer(env.call("b", ".")) is None
    assert env.table.active()  # a plain mismatch ends nothing


def test_second_and_delayed_press_with_one_token(env):
    view = env.offer()
    assert env.press(view["token"]).status == "allowed"
    second = env.press(view["token"])
    assert (second.status, second.code) == ("refused", "op_token_used")
    assert env.decided == 1


def test_newer_view_voids_older_token(env):
    first = env.offer()
    second = env.offer()
    assert env.press(first["token"]).code == "stale_view"
    assert env.press(second["token"]).status == "allowed"


def test_rule_save_then_delayed_click_approves_nothing(env):
    """Invariant 2: card shown → rule save (still Ask) → delayed click → stale_view."""
    view = env.offer()
    sa_table_bump = env.table.bump_fence  # a rule save's publish_end("rule_changed")
    sa_table_bump("rule_changed")
    env.rule = ("ask", 8)
    result = env.press(view["token"])
    assert (result.status, result.code) == ("refused", "stale_view")
    assert env.decided == 0 and not env.table.active()


def test_revoke_all_then_delayed_click_approves_nothing(env):
    view = env.offer()
    env.table.bump_fence("stop")
    result = env.press(view["token"])
    assert (result.status, result.code) == ("refused", "stale_view")
    assert env.decided == 0


def test_class_changed_approves_nothing(env, tmp_path):
    view = env.offer()
    result = env.press(view["token"], tool_input=env.call("a.py", "game/sub"))
    assert result.code in ("stale_view", "class_changed")
    assert env.decided == 0


def test_stop_during_issued_write_leaves_no_allowance(tmp_path):
    """Invariant 2: a stop published between the decision and the commit wins."""
    e = Env(tmp_path)
    e.service.hooks["after_decided"] = lambda: e.table.bump_fence("stop")
    view = e.offer()
    result = e.press(view["token"])
    assert result.status == "approved_once"
    assert result.code == "issue_aborted_fence"
    assert e.decided == 1 and not e.table.active()
    assert e.answer() is None
    assert any(r["end_reason"] == "issue_aborted_fence" for r in e.rows("audit.allow.ended"))


def test_new_session_during_issuance_from_another_thread(tmp_path):
    """The issuance is stopped at its boundary; ``/new`` runs in another thread."""
    e = Env(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def pause():
        reached.set()
        assert release.wait(5)

    e.service.hooks["before_commit"] = pause
    view = e.offer()
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", e.press(view["token"])))
    worker.start()
    assert reached.wait(5)
    e.table.new_session()
    release.set()
    worker.join(5)
    assert out["r"].status == "approved_once"
    assert not e.table.active()


def test_revoke_between_use_and_consumption(env):
    result = env.allowed()
    binding = env.answer()
    assert env.table.revoke(result.allow_id)
    ran, code, _ = env.consume(binding)
    assert (ran, code) == (False, "ended")
    assert env.rows("audit.tool.void")


def test_departure_between_consume_evidence_and_cas(tmp_path):
    """Invariant 3: a WARNING committed by another thread's engine evaluation between the
    consume step's evidence and its CAS → not run (``on_breaker`` suppressed)."""
    e = Env(tmp_path)
    engine = StiraEngine(on_breaker=None)
    engine.on_departure = e.table.bump_departure
    e.allowed()
    binding = e.answer()
    reached, release = threading.Event(), threading.Event()

    def pause():
        reached.set()
        assert release.wait(5)

    e.service.hooks["before_consume_cas"] = pause
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", e.consume(binding)))
    worker.start()
    assert reached.wait(5)

    def warn():
        with engine.state_lock:
            engine.state.stability = 0.4
        engine.pre_turn()

    other = threading.Thread(target=warn)
    other.start()
    other.join(5)
    release.set()
    worker.join(5)
    assert out["r"][0] is False and out["r"][1] in ("ended", "moved")


def test_breaker_round_trip_refused(env):
    """A NORMAL → WARNING → NORMAL round trip leaves a different counter behind."""
    engine = StiraEngine()
    engine.on_departure = env.table.bump_departure
    env.allowed()
    engine.state.stability = 0.4
    engine.pre_turn()
    engine.state.stability = 0.9
    engine.pre_turn()
    assert env.answer() is None
    assert not env.table.active()


def test_raising_breaker_read_ends_allowance_for_good(env):
    env.allowed()

    def boom():
        raise RuntimeError("breaker unreadable")

    env.service._breaker = boom
    assert env.answer() is None
    env.service._breaker = lambda: "normal"
    assert env.answer() is None  # gone at the next NORMAL too


def test_engine_publishes_departure_inside_state_lock():
    seen = []
    engine = StiraEngine()

    def hook():
        seen.append(engine.state_lock._is_owned())

    engine.on_departure = hook
    engine.state.stability = 0.4
    engine.pre_turn()
    assert seen and all(seen)


def test_engine_normal_publishes_nothing():
    engine = StiraEngine()
    engine.on_departure = lambda: pytest.fail("no departure at NORMAL")
    engine.pre_turn()
    engine.breaker_status()


def test_roots_hand_edit_back_and_forth_refused(env):
    env.allowed()
    env.digest = "e" * 64
    assert env.answer() is None
    env.digest = "d" * 64  # changed back
    assert env.answer() is None
    assert not env.table.active()


def test_roots_save_paused_between_bump_and_replace(tmp_path):
    """A roots save holds the store lock from its bump to its replace: an evidence step
    waits (bounded) and never reads the half-saved state."""
    e = Env(tmp_path)
    e.allowed()
    e.service.store_lock_timeout = 0.2
    holding, done = threading.Event(), threading.Event()

    def save():
        with e.store_lock:
            e.table.bump_roots()
            holding.set()
            assert done.wait(5)

    saver = threading.Thread(target=save)
    saver.start()
    assert holding.wait(5)
    assert e.answer() is None  # timed out: a failed read, everything ended
    done.set()
    saver.join(5)
    assert not e.table.active()


def test_time_ends_after_two_hours(env):
    env.allowed()
    env.clock.t += sa.DURATION_SEC
    assert env.answer() is None


def test_clock_anomaly_ends_everything(env):
    env.allowed()
    assert env.answer() is not None
    env.clock.t -= 60
    assert env.answer() is None
    assert not env.table.active()


def test_list_full_at_twenty(env):
    for index in range(sa.MAX_PER_SESSION):
        view = env.offer(request_id=f"r{index}")
        assert env.press(view["token"], request_id=f"r{index}").status == "allowed"
    view = env.offer(request_id="r99")
    assert env.press(view["token"], request_id="r99").code == "list_full"


# --- audit failure (§7) --------------------------------------------------------------------


def test_audit_failure_at_decision_row_approves_nothing(env):
    view = env.offer()
    env.fail_record = True
    result = env.press(view["token"])
    assert (result.status, result.code) == ("refused", "audit_failed")
    assert env.decided == 0
    env.fail_record = False
    assert env.offer()["offered"] is False  # the buttons stay hidden until restart


def test_audit_failure_at_issued_row(tmp_path):
    e = Env(tmp_path)

    def fail_next():
        e.fail_record = True

    e.service.hooks["after_decided"] = fail_next
    result = e.press(e.offer()["token"])
    assert result.status == "approved_once" and result.code == "issue_aborted_audit"
    assert not e.table.active()


def test_audit_failure_at_call_row_refuses(env):
    env.allowed()
    binding = env.answer()
    env.fail_record = True
    ran, code, _ = env.consume(binding)
    assert (ran, code) == (False, "audit_failed")
    assert not env.table.active()


def test_once_approved_press_call_needs_its_rows(env):
    result = env.allowed()
    ok, intent = env.service.record_press_call(
        request_id="r1", tool="file_write", args_hash="h" * 64, decision_ref=result.decision_ref
    )
    assert ok and intent
    env.fail_record = True
    ok, _ = env.service.record_press_call(
        request_id="r2", tool="file_write", args_hash="h" * 64, decision_ref=result.decision_ref
    )
    assert ok is False


def test_unhealthy_writer_refuses_press(env):
    env.chain = SimpleNamespace(yolo_healthy=lambda: False)
    env.service._ledger = env.chain
    view = env.table.issue_view_token(
        request_id="r1", args_hash="h" * 64, target=TargetClass("x", "tool", "x"), rule_generation=7
    )
    assert env.press(view).code == "audit_failed"
    assert env.decided == 0


# --- rows: no free text (invariant 4) -------------------------------------------------------


def test_rows_carry_no_clear_path_or_host(env):
    env.allowed()
    env.consume(env.answer())
    env.table.bump_fence("stop")
    env.service.flush_ended()
    assert env.rows("audit.allow.ended")
    text = repr([e.attributes for e in env.store.get_events(limit=500)])
    assert str(env.root) not in text and "game" not in text


def test_allow_rows_have_no_window_id_and_refuse_unknown_fields(env):
    env.allowed()
    for row in env.rows("audit.allow.issued"):
        assert "window_id" not in row
    with pytest.raises(AuditContractError):
        build("audit.allow.ended", path="/tmp/x")


# --- restart / rollback (invariant 1) ---------------------------------------------------------


def test_new_process_accepts_no_old_token(env, tmp_path):
    view = env.offer()
    save = env.table.issue_save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    fresh = sa.AllowanceTable(clock=env.clock)  # a restart: a new incarnation, no records
    assert fresh.incarnation != env.table.incarnation
    record, code = fresh.reserve(
        view["token"],
        request_id="r1",
        args_hash="h" * 64,
        target=allow_again_eligibility("file_write", env.call(), env.policy, ORIGIN_CHAT),
        rule_generation=7,
        roots_digest_value="d" * 64,
    )
    assert record is None and code == "stale_view"
    assert (
        fresh.spend_save_token(
            save, request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
        )
        == "stale_confirmation"
    )
    assert fresh.active() == []


# --- Make Always save token (§9) ---------------------------------------------------------------


def test_save_token_waits_for_terminal_result(env):
    env.allowed()
    token, code = env.service.save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert (token, code) == (None, "call_not_finished")
    env.service.terminal("r1", "committed")
    token, code = env.service.save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert code is None
    kw = {"request_id": "r1", "tool": "file_write", "hub_version": "v1", "rule_generation": 7}
    assert env.table.spend_save_token(token, **kw) is None
    assert env.table.spend_save_token(token, **kw) == "op_token_used"


def test_save_token_delayed_across_a_stop(env):
    env.allowed()
    env.service.terminal("r1", "committed")
    token, _ = env.service.save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert token
    env.table.bump_fence("stop")
    assert (
        env.table.spend_save_token(
            token, request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
        )
        == "stale_confirmation"
    )


def test_save_token_after_another_save(env):
    env.allowed()
    env.service.terminal("r1", "committed")
    token, _ = env.service.save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert token
    assert (
        env.table.spend_save_token(
            token, request_id="r1", tool="file_write", hub_version="v2", rule_generation=8
        )
        == "stale_confirmation"
    )


def test_save_token_checked_in_the_save_precommit(tmp_path, monkeypatch):
    """The token is spent inside ``ToolConfig.save``'s precommit: a stale token refuses the
    save before the file is replaced."""
    from stiracore.tools.tool_config import ToolConfig

    table = sa.AllowanceTable()
    path = tmp_path / "tool-config.json"
    config = ToolConfig(path=path)
    config.save()
    before = path.read_text()
    token = table.issue_save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=config.generation
    )
    table.bump_fence("stop")

    def precommit():
        code = table.spend_save_token(
            token,
            request_id="r1",
            tool="file_write",
            hub_version="v1",
            rule_generation=config.generation - 0,
        )
        if code is not None:
            raise PermissionError(code)

    with pytest.raises(PermissionError, match="stale_confirmation"):
        config.save(precommit)
    assert path.read_text() == before


# --- the one loosening judgement (§8) -------------------------------------------------------------


def test_session_allow_is_a_reserved_key_outside_its_context():
    assert "allow_not_settable" in rules_loosening({}, {"session_allow": "active"})
    assert rules_loosening(
        {"session_allow": "none"}, {"session_allow": "active"}, context="card_allow_again"
    ) == ["session_allow"]
    assert (
        rules_loosening(
            {"session_allow": "active"}, {"session_allow": "none"}, context="card_allow_again"
        )
        == []
    )


# --- end triggers wired in PR 1 ---------------------------------------------------------------------


def test_rule_save_bumps_the_process_fence(tmp_path):
    from stiracore.tools.tool_config import ToolConfig

    before = sa.table().snapshot().fence
    ToolConfig(path=tmp_path / "tool-config.json").save()
    assert sa.table().snapshot().fence > before


def test_agent_roots_save_bumps_roots_only_when_changed(tmp_path):
    from stiracore.config.models import AgentConfig

    config = AgentConfig(path=tmp_path / "agent-config.json")
    config.save()
    start = sa.table().snapshot().roots_generation
    config.save()
    assert sa.table().snapshot().roots_generation == start
    config.write_roots = [*config.write_roots, str(tmp_path)]
    config.save()
    assert sa.table().snapshot().roots_generation == start + 1


# --- server entries refuse without the server's allowance service (409 not_enabled) ----------


def test_decision_route_refuses_allow_again(tmp_path):
    """No allowance service installed (a server built without it, or the constant off)."""
    from stiracore.yolo import allow_runtime

    allow_runtime._reset_for_tests()
    from test_hitl_binding import _app

    from stiracore.core.approval_store import ApprovalStore
    from stiracore.observability.chat_api import HITLApprovalBroker

    broker = HITLApprovalBroker(store=ApprovalStore(tmp_path / "a.db"))
    client = _app(broker)
    before = sa.table().snapshot()
    for body in (
        {"action": "approve", "args_hash": "h", "allow_again": True},
        {"action": "approve", "args_hash": "h", "view_token": "t"},
    ):
        response = client.post("/api/hitl/x/decision", json=body)
        assert (response.status_code, response.json["code"]) == (409, "not_enabled")
    assert sa.table().snapshot() == before  # nothing issued, nothing ended


def test_hub_refuses_card_origin_save_and_reserved_key(tmp_path):
    from test_settings_hub import _client, _store

    from stiracore.yolo import allow_runtime

    allow_runtime._reset_for_tests()  # no service: a card-origin save is refused

    client = _client(_store(tmp_path))
    for body in ({"source": "approval_card", "changes": {}}, {"save_token": "t", "changes": {}}):
        response = client.post("/api/config/hub", json=body)
        assert (response.status_code, response.json["code"]) == (409, "not_enabled")
    response = client.post("/api/config/hub", json={"changes": {"tools.session_allow": "active"}})
    assert (response.status_code, response.json["code"]) == (409, "allow_not_settable")


# --- review round 1 (PR #576) ------------------------------------------------------------


def test_press_call_receipt_bound_to_its_card_tool_and_args(env):
    """M1: a card's decision receipt cannot authorize another card / tool / args."""
    result = env.allowed()
    ref = result.decision_ref
    call = env.service.record_press_call
    assert call(request_id="other", tool="file_write", args_hash="h" * 64, decision_ref=ref)[0] is (
        False
    )
    assert call(request_id="r1", tool="memory_write", args_hash="h" * 64, decision_ref=ref)[0] is (
        False
    )
    assert call(request_id="r1", tool="file_write", args_hash="x" * 64, decision_ref=ref)[0] is (
        False
    )
    assert call(request_id="r1", tool="file_write", args_hash="h" * 64, decision_ref=ref)[0] is True
    # The same receipt is never used twice.
    assert call(request_id="r1", tool="file_write", args_hash="h" * 64, decision_ref=ref)[0] is (
        False
    )


def test_save_token_tool_comes_from_the_card(env):
    """M1: a terminal card never yields a save token for another tool."""
    env.allowed()
    env.service.terminal("r1", "committed")
    token, code = env.service.save_token(
        request_id="r1", tool="memory_write", hub_version="v1", rule_generation=7
    )
    assert (token, code) == (None, "tool_mismatch")
    token, code = env.service.save_token(
        request_id="r1", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert code is None and token
    kw = {"request_id": "r1", "hub_version": "v1", "rule_generation": 7}
    assert env.table.spend_save_token(token, tool="memory_write", **kw) == "stale_confirmation"


def test_unknown_card_terminal_gives_no_save_token(env):
    env.service.terminal("never-pressed", "committed")
    token, code = env.service.save_token(
        request_id="never-pressed", tool="file_write", hub_version="v1", rule_generation=7
    )
    assert token is None and code == "call_not_finished"


def test_clock_back_right_after_issuance_ends_everything(env):
    """M2: no ``answer()`` before the wall clock goes back; the issuance set the base."""
    env.allowed()
    env.clock.t -= 60
    assert env.answer() is None
    env.clock.t += 120
    assert env.answer() is None
    assert not env.table.active()


def test_clock_read_failing_at_reserve_ends_everything(env):
    view = env.offer()
    env.table._clock = lambda: (_ for _ in ()).throw(OSError("clock"))
    result = env.press(view["token"])
    assert result.status == "refused" and env.decided == 0
    assert env.table.snapshot().fence > 0


def test_consume_deadline_read_inside_the_cas(tmp_path):
    """M3: two hours pass after the consume step read its clock and before its CAS."""
    e = Env(tmp_path)
    e.allowed()
    binding = e.answer()

    def late():
        e.clock.t += sa.DURATION_SEC + 1

    e.service.hooks["after_clock"] = late
    ran, code, _ = e.consume(binding)
    assert (ran, code) == (False, "time")


def test_end_publication_never_writes_audit_in_the_callers_lock(tmp_path):
    """M4: a bump returns while the audit writer is blocked (no synchronous row I/O)."""
    e = Env(tmp_path)
    e.allowed()
    blocked, release = threading.Event(), threading.Event()
    original = e._record

    def slow(event_type, **fields):
        if event_type == "audit.allow.ended":
            blocked.set()
            assert release.wait(5)
        return original(event_type, **fields)

    e.service._record = slow
    engine = StiraEngine()
    engine.on_departure = e.table.bump_departure
    done = threading.Event()

    def trip():
        engine.state.stability = 0.4
        engine.pre_turn()
        done.set()

    worker = threading.Thread(target=trip)
    worker.start()
    assert done.wait(5), "the breaker evaluation waited for an audit row"
    assert blocked.wait(5)  # the row is written elsewhere, afterwards
    release.set()
    worker.join(5)
    e.service.flush_ended()
    assert any(r["end_reason"] == "breaker" for r in e.rows("audit.allow.ended"))


def test_reserved_records_count_against_the_limit(env):
    """minor 5: concurrent presses past their reserve never exceed 20."""
    target = allow_again_eligibility("file_write", env.call(), env.policy, ORIGIN_CHAT)
    for index in range(sa.MAX_PER_SESSION + 1):
        token = env.table.issue_view_token(
            request_id=f"r{index}", args_hash="h" * 64, target=target, rule_generation=7
        )
        record, code = env.table.reserve(
            token,
            request_id=f"r{index}",
            args_hash="h" * 64,
            target=target,
            rule_generation=7,
            roots_digest_value="d" * 64,
        )
        if index < sa.MAX_PER_SESSION:
            assert record is not None, code
        else:
            assert (record, code) == (None, "list_full")


def test_allow_list_refuses_only_the_unknown_field(env):
    env.allowed()
    env.table.bump_fence("stop")
    env.service.flush_ended()
    row = env.rows("audit.allow.ended")[0]
    fields = {**row, "event_id": f"op:{row['operation_id']}:transition"}
    build("audit.allow.ended", **fields)  # the valid row passes
    with pytest.raises(AuditContractError, match="not in the registry entry"):
        build("audit.allow.ended", **fields, path="x")


# --- review round 2 (PR #576): the press's own call respects the sticky audit failure ------


def test_press_call_refused_after_an_ended_row_failed(env):
    """Blocker: an ``audit.allow.ended`` row that fails makes the audit sticky-failed; the
    press's own once-approved call is then refused too (§5, §7)."""
    result = env.allowed()
    original = env._record

    def failing_ended(event_type, **fields):
        if event_type == "audit.allow.ended":
            raise OSError("disk full")
        return original(event_type, **fields)

    env.service._record = failing_ended
    assert env.table.revoke(result.allow_id)
    env.service.flush_ended()
    assert env.table.audit_failed is True
    ok, intent = env.service.record_press_call(
        request_id="r1", tool="file_write", args_hash="h" * 64, decision_ref=result.decision_ref
    )
    assert (ok, intent) == (False, None)


def test_press_call_refused_when_audit_fails_after_its_receipts(tmp_path):
    """The audit failure published by another thread between the receipt checks and the
    final confirmation wins (stopped at the boundary)."""
    e = Env(tmp_path)
    result = e.allowed()
    reached, release = threading.Event(), threading.Event()

    def pause():
        reached.set()
        assert release.wait(5)

    e.service.hooks["before_press_confirm"] = pause
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault(
            "r",
            e.service.record_press_call(
                request_id="r1",
                tool="file_write",
                args_hash="h" * 64,
                decision_ref=result.decision_ref,
            ),
        )
    )
    worker.start()
    assert reached.wait(5)
    other = threading.Thread(target=e.table.mark_audit_failed)
    other.start()
    other.join(5)
    release.set()
    worker.join(5)
    assert out["r"][0] is False


def test_press_approves_nothing_when_audit_fails_after_its_decision_row(tmp_path):
    e = Env(tmp_path)
    e.service.hooks["before_decide"] = e.table.mark_audit_failed
    result = e.press(e.offer()["token"])
    assert (result.status, result.code) == ("refused", "audit_failed")
    assert e.decided == 0


def test_every_grant_step_refuses_after_a_sticky_audit_failure(env):
    """The other "rows first, then a permission" steps: use, consume, view and save tokens."""
    result = env.allowed()
    binding = env.answer()
    env.service.terminal("r1", "committed")
    env.table.mark_audit_failed()
    assert env.consume(binding)[:2] == (False, "audit_failed")
    assert env.answer() is None
    assert env.offer(request_id="r2")["offered"] is False
    assert env.service.save_token(request_id="r1", hub_version="v1", rule_generation=7) == (
        None,
        "audit_failed",
    )
    assert env.table.get(result.allow_id).state == "ended"


# --- review round 3 (PR #576): one place publishes an unhealthy writer ------------------------


class _Unhealthy:
    """The same chain, but its writer now reports unhealthy (sticky in the real store)."""

    def __init__(self, chain) -> None:
        self._chain = chain

    def yolo_healthy(self) -> bool:
        return False

    def yolo_receipt(self, event_id):
        return self._chain.yolo_receipt(event_id)


def _paused_press_call(e, result, during):
    reached, release = threading.Event(), threading.Event()

    def pause():
        reached.set()
        assert release.wait(5)

    e.service.hooks["before_press_confirm"] = pause
    out = {}
    worker = threading.Thread(
        target=lambda: out.setdefault(
            "r",
            e.service.record_press_call(
                request_id="r1",
                tool="file_write",
                args_hash="h" * 64,
                decision_ref=result.decision_ref,
            ),
        )
    )
    worker.start()
    assert reached.wait(5)
    other = threading.Thread(target=during)
    other.start()
    other.join(5)
    release.set()
    worker.join(5)
    return out["r"]


def test_writer_unhealthy_seen_by_outcome_refuses_the_paused_press_call(tmp_path):
    """The review's order: receipts verified → paused → the writer turns unhealthy and
    another ``outcome()`` observes it → resumed → not run; the failure is published."""
    e = Env(tmp_path)
    result = e.allowed()

    def during():
        e.service._ledger = _Unhealthy(e.chain)
        e.service.outcome("op:" + "0" * 32 + ":intent", "committed")

    assert _paused_press_call(e, result, during)[0] is False
    assert e.table.audit_failed is True


def test_writer_unhealthy_unobserved_still_refuses_at_the_confirmation(tmp_path):
    """Nobody else looked: the confirmation reads the writer itself (and publishes)."""
    e = Env(tmp_path)
    result = e.allowed()

    def during():
        e.service._ledger = _Unhealthy(e.chain)

    assert _paused_press_call(e, result, during)[0] is False
    assert e.table.audit_failed is True


@pytest.mark.parametrize("step", ["outcome", "void_row", "write_abort", "ended_rows"])
def test_every_best_effort_row_publishes_an_unhealthy_writer(env, step):
    env.allowed()
    env.service._ledger = _Unhealthy(env.chain)
    if step == "outcome":
        env.service.outcome("op:" + "0" * 32 + ":intent", "failed")
    elif step == "void_row":
        env.service._void_row("op:" + "0" * 32 + ":intent", "refused")
    elif step == "write_abort":
        env.service._write_abort("0" * 32, "issue_aborted_fence")
    else:
        env.table.bump_fence("stop")
        env.service.flush_ended()
    assert env.table.audit_failed is True


def test_writer_unhealthy_before_the_consume_cas_refuses(tmp_path):
    e = Env(tmp_path)
    e.allowed()
    binding = e.answer()
    e.service.hooks["after_clock"] = lambda: setattr(e.service, "_ledger", _Unhealthy(e.chain))
    assert e.consume(binding)[:2] == (False, "audit_failed")
    assert e.table.audit_failed is True


def test_only_one_place_reads_the_writer_health():
    import inspect

    from stiracore.yolo import allow_service

    source = inspect.getsource(allow_service)
    assert source.count("yolo_healthy()") == 1
    assert "def _healthy" not in source


# --- review round 4 (PR #576): every grant transition goes through one gate -------------------


def test_issuance_commit_refuses_an_unpublished_unhealthy_writer(env):
    """The review's order: stopped at ``before_commit`` → the writer turns unhealthy
    (nobody publishes it) → resumed → no active allowance; the failure is sticky."""
    view = env.offer()
    env.service.hooks["before_commit"] = lambda: setattr(
        env.service, "_ledger", _Unhealthy(env.chain)
    )
    result = env.press(view["token"])
    assert result.status != "allowed"
    assert env.table.active() == []
    assert env.table.audit_failed is True


_GRANT_PATTERNS = (
    r'\.state\s*=\s*"active"',
    r"\.uses\s*\+=\s*1",
    r"self\._(views|saves|bindings|records)\[[^\]]+\]\s*=(?!=)",
    r"return\s+refusal\s+is\s+None",
)


def _table_functions():
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(sa.AllowanceTable))
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            found[node.name] = ast.get_source_segment(source, node)
    return found


def test_every_granting_function_enters_through_the_gate():
    """Structural: a function of the table that grants (creates a token, a record, a use
    binding, activates, counts a use, confirms) uses ``_grant_section`` — or is a
    ``*_locked`` helper whose every caller does. A new granting transition that bypasses
    the gate fails here."""
    import re

    functions = _table_functions()
    granting = {
        name
        for name, body in functions.items()
        if any(re.search(p, body) for p in _GRANT_PATTERNS)
        and name not in ("_grant_section", "_grant_gate_locked")
    }
    assert {"reserve", "commit", "issue_view_token", "issue_save_token"} <= granting
    for name in sorted(granting):
        body = functions[name]
        if "self._grant_section(" in body:
            continue
        assert name.endswith("_locked"), f"{name} grants without _grant_section"
        callers = [n for n, b in functions.items() if f"self.{name}(" in b and n != name]
        assert callers, name
        for caller in callers:
            assert "self._grant_section(" in functions[caller], f"{caller} → {name}"
    public = {n for n in granting if not n.startswith("_")}
    for name, body in functions.items():
        if "self._grant_section(" in body and not name.startswith("_"):
            public.add(name)
    assert public == set(sa.GRANT_TRANSITIONS)
    # The gate itself is entered only through the section.
    assert [n for n, b in functions.items() if "self._grant_gate_locked(" in b] == [
        "_grant_section"
    ]


def test_every_grant_transition_refuses_when_the_gate_refuses(env, monkeypatch):
    """Behavioural: with the gate refusing, no transition of ``GRANT_TRANSITIONS`` grants."""
    result = env.allowed()
    binding = env.answer()
    target = allow_again_eligibility("file_write", env.call(), env.policy, ORIGIN_CHAT)
    view = env.table.issue_view_token(
        request_id="g", args_hash="h" * 64, target=target, rule_generation=7
    )
    save = env.table.issue_save_token(
        request_id="g", tool="file_write", hub_version="v", rule_generation=7
    )
    reserved, _ = env.table.reserve(
        env.table.issue_view_token(
            request_id="g2", args_hash="h" * 64, target=target, rule_generation=7
        ),
        request_id="g2",
        args_hash="h" * 64,
        target=target,
        rule_generation=7,
        roots_digest_value="d" * 64,
    )
    view2 = env.table.issue_view_token(
        request_id="g5", args_hash="h" * 64, target=target, rule_generation=7
    )
    assert view and view2 and save and reserved and binding
    epoch = env.table.audit_epoch()
    monkeypatch.setattr(
        env.table, "_grant_gate_locked", lambda epoch: ("audit_failed", None), raising=True
    )
    t = env.table
    snap = t.snapshot()
    calls = {
        "issue_view_token": lambda: t.issue_view_token(
            request_id="g3", args_hash="h" * 64, target=target, rule_generation=7
        ),
        "reserve": lambda: t.reserve(
            view,
            request_id="g",
            args_hash="h" * 64,
            target=target,
            rule_generation=7,
            roots_digest_value="d" * 64,
        )[0],
        "spend_view": lambda: (
            t.spend_view(
                view2, request_id="g5", args_hash="h" * 64, target=target, rule_generation=7
            )
            is None
        ),
        "commit": lambda: (
            t.commit(reserved.allow_id, breaker_normal=True, issued_ref="x", decision_ref="y")
            is None
        ),
        "use": lambda: t.use(result.allow_id, snapshot=snap, call=target, args_hash="a" * 64)[0],
        "consume": lambda: t.consume(binding, snapshot=snap)[0],
        "issue_save_token": lambda: t.issue_save_token(
            request_id="g4", tool="file_write", hub_version="v", rule_generation=7
        ),
        "spend_save_token": lambda: (
            t.spend_save_token(
                save, request_id="g", tool="file_write", hub_version="v", rule_generation=7
            )
            is None
        ),
        "audit_still_healthy": lambda: t.audit_still_healthy(epoch),
    }
    assert set(calls) == set(sa.GRANT_TRANSITIONS)
    for name, call in calls.items():
        assert not call(), f"{name} granted with the gate refusing"


@pytest.mark.parametrize(
    "hook", ["after_reserve", "after_decided", "before_commit", "before_decide"]
)
def test_issuance_refuses_an_unhealthy_writer_at_every_boundary(env, hook):
    view = env.offer()
    env.service.hooks[hook] = lambda: setattr(env.service, "_ledger", _Unhealthy(env.chain))
    result = env.press(view["token"])
    assert result.status != "allowed"
    assert env.table.active() == [] and env.table.audit_failed is True
