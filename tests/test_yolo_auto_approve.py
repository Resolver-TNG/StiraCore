"""ADR 0012 phase 5a: the approver is connected — while a window is ``active`` the Ask of
an **internal** tool in a human chat / Stage turn is approved automatically (§2 step 5,
§6a), the cap counts used / reserved slots, the breaker pauses and resumes the window
(§6), and nothing else changes: external tools, exec, other origins, Never, the floors,
the breaker's write block and CRITICAL keep going to a person or stay refused.

The service runs on fake clocks and the fake audit chain of the phase-3 tests (the real
row contract, receipts, health); the gate tests run the real ``CheckpointHooks`` and
``ApprovalBindingVerifier``. Offline, no model call.
"""

from __future__ import annotations

import json
import logging
import secrets
from types import SimpleNamespace

import pytest

from stiracore.agent.checkpoint_hooks import (
    YOLO_GRANTS_KEY,
    ApprovalBindingVerifier,
    CheckpointHooks,
)
from stiracore.core import HITLAction, StiraConfig, StiraEngine, hitl_reasons
from stiracore.core.approval import args_hash
from stiracore.core.engine import HITL_APPROVED_TOOL_IDS_KEY, HITLDecision
from stiracore.tools import tool_registry
from stiracore.tools.tool_consent import EXEC_TOOLS
from stiracore.yolo import origin, runtime
from stiracore.yolo import requests as yolo_requests
from stiracore.yolo import state as sm
from stiracore.yolo.audit_schema import KNOWN_TOOLS
from stiracore.yolo.requests import DWELL_SEC, FLAP_WINDOW_SEC, WINDOW_SEC, AutoGrant
from stiracore.yolo.tiers import (
    EXEC,
    EXTERNAL,
    EXTERNAL_TOOLS,
    INTERNAL,
    INTERNAL_TOOLS,
    PERSON_TOOLS,
    scope_refusal,
    tool_class,
)
from stiracore.yolo.window import WindowMarker
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.test_yolo_window import _open, _service

DIGEST = "a" * 64
AGENT = "nxp-agent"  # StiraEngine's default agent id: the gate's engine in _gate


class Breaker:
    """A breaker level the test moves (read live by the service)."""

    def __init__(self, level="normal"):
        self.level = level

    def __call__(self):
        return self.level


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


@pytest.fixture
def breaker():
    return Breaker()


@pytest.fixture
def yolo(audit, clock, tmp_path, breaker):
    return _service(audit, clock, WindowMarker(tmp_path), breaker=breaker)


@pytest.fixture
def human():
    with origin.human_turn(AGENT):
        yield


def _approve(yolo, tool="file_write", level="normal", **kw):
    return yolo.approve(
        tool_name=tool,
        args_digest=kw.pop("digest", DIGEST),
        tool_use_id=kw.pop("tool_use_id", secrets.token_hex(8)),
        level=level,
        agent_id=kw.pop("agent_id", AGENT),
        **kw,
    )


def _window(yolo):
    return yolo.status()["window"]


# --- §4: the classification table ---------------------------------------------------------


def _registered_names():
    names = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    # The chat server's bound memory tools (created per store, not in the registry).
    return names | {"memory_read", "memory_write", "memory_search", "memory_list"}


def test_every_registered_tool_is_in_exactly_one_class():
    classes = {
        "internal": INTERNAL_TOOLS,
        "external": EXTERNAL_TOOLS,
        "exec": EXEC_TOOLS | PERSON_TOOLS,
    }
    for name in _registered_names():
        found = [kind for kind, names in classes.items() if name in names]
        assert len(found) == 1, (name, found)
        assert tool_class(name) == found[0]
    assert not INTERNAL_TOOLS & EXTERNAL_TOOLS and not INTERNAL_TOOLS & EXEC_TOOLS
    # Every internal tool can be written to a row in clear (§11: registry tools as enum).
    assert INTERNAL_TOOLS <= KNOWN_TOOLS


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        *[(name, INTERNAL) for name in sorted(INTERNAL_TOOLS)],
        *[(name, EXTERNAL) for name in sorted(EXTERNAL_TOOLS)],
        *[(name, EXEC) for name in sorted(EXEC_TOOLS)],
        # Starts processes (graphviz, the OS ``open``), writes where ``title`` points.
        ("diagram", EXEC),
        # Runs the agent's own JavaScript in a browser: a card in every window (ADR 0015 Q2).
        ("page_check", EXEC),
        # Stages a procedural rule for review: a card in every window (#463).
        ("propose_procedure", EXEC),
        # Unclassified = external: MCP / new / odd names, and anything not a str.
        ("mcp_github_create_issue", EXTERNAL),
        ("File_Write", EXTERNAL),
        ("file_write ", EXTERNAL),
        ("", EXTERNAL),
        (None, EXTERNAL),
        (b"file_write", EXTERNAL),
    ],
)
def test_the_class_table(name, expected):
    assert tool_class(name) == expected


@pytest.mark.parametrize(
    ("tool", "tier", "approved"),
    [
        *[(name, "internal", True) for name in sorted(INTERNAL_TOOLS)],
        *[(name, "external", True) for name in sorted(INTERNAL_TOOLS)],
        # Phase 6: external tools in a window of tier external only (§10).
        *[(name, "external", True) for name in sorted(EXTERNAL_TOOLS)],
        *[(name, "internal", False) for name in sorted(EXTERNAL_TOOLS)],
        # Exec: never, in any tier (§9 option B).
        *[(name, "external", False) for name in sorted(EXEC_TOOLS)],
        ("diagram", "external", False),
        ("page_check", "internal", False),
        ("page_check", "external", False),
        ("propose_procedure", "internal", False),
        ("propose_procedure", "external", False),
        ("mcp_anything", "external", False),
    ],
)
def test_what_is_approved_automatically(yolo, human, tool, tier, approved):
    _open(yolo, tier)
    grant, code = _approve(yolo, tool)
    assert (grant is not None) is approved, code
    if not approved:
        assert code == "tier_not_covered"


# --- scope: only inside the workspace / write roots (PR #435 review M1) --------------------


def test_every_internal_tool_has_a_scope_rule(tmp_path, monkeypatch):
    """A new internal tool without a rule in ``scope_refusal`` is never answered: its
    calls are ``scope_unknown`` — this test makes the omission visible."""
    from stiracore.yolo import tiers

    ruled = tiers._PATHLESS | set(tiers._WORKSPACE_PATHS) | set(tiers._CWD_PATHS) | {"journal"}
    assert ruled == INTERNAL_TOOLS
    policy = _scope(tmp_path)
    monkeypatch.chdir(tmp_path)
    inside = {"path": "a.md", "image_path": "a.png", "file_path": "a.py", "action": "read"}
    for name in sorted(INTERNAL_TOOLS):
        assert scope_refusal(name, dict(inside), policy) is None, name


@pytest.mark.parametrize(
    ("name", "args", "code"),
    [
        ("file_read", {"path": "notes/a.md"}, None),
        ("file_read", {"path": "/etc/hosts"}, "scope_outside"),
        ("file_read", {"path": "~/.ssh/id_rsa"}, "scope_outside"),
        ("file_read", {"path": "../escape.md"}, "scope_outside"),
        ("file_read", {"path": "link/x"}, "scope_outside"),  # a symlink to outside
        ("file_read", {"path": ""}, "scope_unknown"),
        ("file_read", {"path": "a\0b"}, "scope_unknown"),
        ("file_read", {"path": 3}, "scope_unknown"),
        # a list or a glob names files the check never resolves (strands_tools.file_read
        # splits on "," and expands globs): a person decides
        ("file_read", {"path": "notes.md,/etc/hosts"}, "scope_unknown"),
        ("file_read", {"path": "~/.ssh/*"}, "scope_unknown"),
        ("file_read", {"path": "docs/?.md"}, "scope_unknown"),
        ("file_read", {"path": "docs/[a-z].md"}, "scope_unknown"),
        ("file_read", {}, "scope_unknown"),
        ("editor", {"command": "view", "path": "/etc/hosts"}, "scope_outside"),
        ("image_reader", {"image_path": "/tmp/x.png"}, "scope_outside"),
        ("view_image", {"path": "pics/x.png"}, None),
        ("lsp_hover", {"file_path": "a.py"}, None),  # relative: the cwd (= workspace)
        ("lsp_hover", {"file_path": "/etc/x.py"}, "scope_outside"),
        ("journal", {"action": "write"}, None),
        ("journal", {"action": "write", "date": "2026-10-19"}, None),
        ("journal", {"action": "write", "date": "sub/2026-10-19"}, "scope_unknown"),
        ("journal", {"action": "write", "date": "../../x"}, "scope_unknown"),
        ("journal", {"action": "write", "date": 20261019}, "scope_unknown"),
        ("memory_write", {"layer": "semantic", "key": "k", "content": "x"}, None),
        ("current_time", {}, None),
        ("mcp_unknown", {"path": "a.md"}, "scope_unknown"),
        (None, {}, "scope_unknown"),
    ],
)
def test_the_scope_table(tmp_path, monkeypatch, name, args, code):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "out").mkdir()
    (workspace / "link").symlink_to(tmp_path / "out")
    monkeypatch.chdir(workspace)
    assert scope_refusal(name, args, _scope(workspace)) == code


def test_no_policy_or_odd_input_is_never_in_scope():
    assert scope_refusal("file_read", {"path": "a.md"}, None) == "scope_unknown"
    assert scope_refusal("file_read", ["a.md"], _scope()) == "scope_unknown"

    class Sneaky(str):
        pass

    assert scope_refusal("file_read", {"path": Sneaky("a.md")}, _scope()) == "scope_unknown"
    # Pathless tools need no policy.
    assert scope_refusal("stop", None, None) is None


def test_without_an_allowlist_the_workspace_is_the_scope(tmp_path):
    from stiracore.identity_paths import WritePathPolicy

    policy = WritePathPolicy(base_dir=tmp_path)  # write_roots=None (library default)
    assert scope_refusal("file_read", {"path": "a.md"}, policy) is None
    assert scope_refusal("file_read", {"path": "/etc/hosts"}, policy) == "scope_outside"
    empty = WritePathPolicy(base_dir=tmp_path, write_roots=[])  # no root at all
    assert scope_refusal("file_read", {"path": "a.md"}, empty) == "scope_outside"


def test_the_gate_sends_an_outside_call_to_a_person_without_asking_yolo(yolo, human, tmp_path):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo, scope=_scope(tmp_path / "ws"))
    event = _call(hooks, "file_read", {"path": str(tmp_path / "secret")})
    assert event.cancel_tool.startswith("HITL rejected") and asked[0][0] == "file_read"
    assert _window(yolo)["auto_approved"] == 0 and _window(yolo)["reserved"] == 0
    # No scope policy at all (a host that names no workspace): every path tool asks.
    hooks.scope_policy = None
    _call(hooks, "file_write", {"path": "a.md", "content": "x"})
    assert [name for name, _ in asked] == ["file_read", "file_write"]
    assert _window(yolo)["auto_approved"] == 0


# --- the human-turn mark: its turn and its agent only (PR #435 review M2) -------------------


def test_the_mark_covers_only_its_running_turn_and_its_agent():
    import asyncio
    import contextvars
    import threading

    assert origin.covered(AGENT) is False and origin.current() is None
    seen = {}

    async def later():
        seen["task"] = origin.covered(AGENT)

    async def main():
        with origin.human_turn(AGENT):
            assert origin.covered(AGENT) and origin.current() == origin.HUMAN_TURN
            assert origin.covered("another") is False and origin.covered(None) is False
            task = asyncio.get_running_loop().create_task(later())  # inherits the mark
            copied = contextvars.copy_context()
            thread = threading.Thread(target=lambda: seen.update(thread=origin.covered(AGENT)))
            thread.start()
            thread.join()
        await task  # runs after the turn: its mark is dead
        seen["copied"] = copied.run(origin.covered, AGENT)

    asyncio.run(main())
    assert seen == {"thread": False, "task": False, "copied": False}
    # Nested turns: the inner one ending leaves the outer one covered.
    with origin.human_turn(AGENT):
        with origin.human_turn(AGENT):
            pass
        assert origin.covered(AGENT)
    assert origin.current() is None


def test_approve_refuses_a_mark_of_another_agent_or_a_finished_turn(yolo, audit):
    import contextvars

    _open(yolo)
    with origin.human_turn(AGENT):
        copied = contextvars.copy_context()
        assert _approve(yolo, agent_id="another-agent") == (None, "origin_not_covered")
    assert copied.run(_approve, yolo) == (None, "origin_not_covered")
    assert audit.of("auto_approved") == []


# --- §6a: one automatic approval, end to end ---------------------------------------------


def test_an_approval_writes_its_row_and_is_consumed_once(yolo, audit, human):
    _open(yolo)
    grant, code = _approve(yolo, "file_write", tool_use_id="tu-1")
    assert code == "approved" and isinstance(grant, AutoGrant)
    row = audit.of("auto_approved")[0]
    assert row["operation"] == "file_write" and row["authorizer_kind"] == "yolo"
    assert row["origin_kind"] == "turn" and row["generation_from"] == row["generation_to"] == 1
    assert row["operation_id"] == grant.intent
    assert _window(yolo)["reserved"] == 1 and _window(yolo)["used"] == 0
    assert _window(yolo)["auto_approved"] == 1
    assert yolo.consume(grant) == (True, "consumed")
    assert audit.of("consumed")[0]["intent_ref"] == grant.intent
    window = _window(yolo)
    assert window["used"] == 1 and window["reserved"] == 0
    # Double consumption: refused, nothing counted twice, no slot returned twice.
    assert yolo.consume(grant) == (False, "permit_consumed")
    window = _window(yolo)
    assert window["used"] == 1 and window["reserved"] == 0


def test_a_rebuilt_permit_is_refused(yolo, human):
    from dataclasses import replace

    _open(yolo)
    grant, _ = _approve(yolo)
    forged = replace(grant, permit=replace(grant.permit, generation=grant.permit.generation + 5))
    assert yolo.consume(forged)[0] is False
    assert yolo.consume(grant) == (True, "consumed")  # the real one still works once


def test_no_window_no_approval_and_origins_other_than_a_human_turn(yolo):
    with origin.human_turn(AGENT):
        assert _approve(yolo) == (None, "no_window")
    _open(yolo)
    # Webhook, bot, resume, job, sub-agent: the mark is unset → never covered.
    assert _approve(yolo) == (None, "origin_not_covered")
    with origin.human_turn(AGENT):
        assert _approve(yolo)[1] == "approved"
    assert origin.current() is None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"digest": "x"}, "unbindable"),
        ({"digest": None}, "unbindable"),
        ({"tool_use_id": ""}, "unbindable"),
        ({"tool_use_id": 7}, "unbindable"),
    ],
)
def test_an_unbindable_call_goes_to_a_person(yolo, human, kwargs, code):
    _open(yolo)
    assert _approve(yolo, **kwargs) == (None, code)


# --- slots and the cap (§6a Slots) -------------------------------------------------------


def test_temporarily_full_goes_to_a_card_and_the_window_stays_active(audit, clock, tmp_path):
    yolo = _service(audit, clock, WindowMarker(tmp_path), cap=2)
    with origin.human_turn(AGENT):
        _open(yolo)
        first, _ = _approve(yolo)
        second, _ = _approve(yolo)
        assert first and second
        assert _approve(yolo) == (None, "cap_full")  # used 0 + reserved 2 == cap
        assert _window(yolo)["state"] == "active"
        assert yolo.consume(first)[0] is True
        assert _approve(yolo) == (None, "cap_full")  # used 1 + reserved 1 == cap
        assert _window(yolo)["state"] == "active"
        # A permit reserved before the cap was reached stays valid: it takes the last slot.
        assert yolo.consume(second)[0] is True
    status = yolo.status()
    assert status["window"] is None or status["window"]["state"] == "ended"
    assert status["last_end"]["reason"] == "expired_cap"
    assert status["last_end"]["class"] == "normal" and not status["latched"]
    ended = audit.of("ended")[-1]
    assert ended["end_reason"] == "expired_cap" and ended["used"] == 2


def test_a_voided_permit_returns_its_slot(audit, clock, tmp_path):
    yolo = _service(audit, clock, WindowMarker(tmp_path), cap=1)
    with origin.human_turn(AGENT):
        _open(yolo)
        grant, _ = _approve(yolo)
        assert _approve(yolo) == (None, "cap_full")
        assert yolo.void(grant) is True
        assert yolo.void(grant) is False  # once
        assert audit.of("void")[0]["reason_code"] == "binding_mismatch"
        again, code = _approve(yolo)
        assert code == "approved"
        assert yolo.consume(grant)[0] is False  # voided → never consumable
        assert yolo.consume(again)[0] is True
    assert yolo.status()["last_end"]["reason"] == "expired_cap"


def test_exactly_one_call_takes_the_last_slot(audit, clock, tmp_path):
    import threading

    yolo = _service(audit, clock, WindowMarker(tmp_path), cap=1)
    with origin.human_turn(AGENT):
        _open(yolo)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        with origin.human_turn(AGENT):
            barrier.wait()
            results.append(_approve(yolo))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    granted = [grant for grant, _code in results if grant is not None]
    assert len(granted) == 1
    assert sorted({code for _g, code in results}) in (["approved", "cap_full"],)


# --- generations, stale permits, audit failures ------------------------------------------


def test_a_pause_between_the_row_and_the_permit_makes_it_stale(yolo, audit, breaker, human):
    _open(yolo)

    def pause_meanwhile():
        breaker.level = "warning"
        yolo.tick()

    audit.before["audit.yolo.auto_approved"] = pause_meanwhile
    assert _approve(yolo) == (None, "permit_stale")
    assert audit.of("void")[0]["reason_code"] == "permit_stale"
    window = _window(yolo)
    assert window["state"] == "paused" and window["reserved"] == 0


def test_a_pause_and_resume_between_the_row_and_the_permit_makes_it_stale(
    yolo, audit, breaker, clock, human
):
    """Step (3) compares the generation, not only the state: a window that paused and
    resumed while the row was written is ``active`` again, at another generation."""
    _open(yolo)

    def pause_and_resume():
        breaker.level = "warning"
        yolo.tick()
        breaker.level = "normal"
        yolo.tick()
        clock.tick(DWELL_SEC + 1)
        yolo.tick()
        assert _window(yolo)["state"] == "active"

    audit.before["audit.yolo.auto_approved"] = pause_and_resume
    assert _approve(yolo) == (None, "permit_stale")
    assert _window(yolo)["reserved"] == 0


def test_a_permit_issued_before_a_pause_is_refused_at_consume(yolo, audit, breaker, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    breaker.level = "warning"
    yolo.tick()
    assert yolo.consume(grant) == (False, "permit_stale")
    assert _window(yolo)["reserved"] == 0 and _window(yolo)["used"] == 0
    assert audit.of("void")[-1]["reason_code"] == "consume_refused"


def test_a_permit_of_an_older_generation_never_works_after_a_resume(
    yolo, audit, breaker, clock, human
):
    _open(yolo)
    grant, _ = _approve(yolo)
    breaker.level = "warning"
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)
    yolo.tick()
    assert _window(yolo)["state"] == "active"
    assert yolo.consume(grant) == (False, "permit_stale")


@pytest.mark.parametrize("fault", ["fail", "tamper"])
def test_an_audit_failure_approves_nothing_and_revokes(yolo, audit, human, fault):
    _open(yolo)
    getattr(audit, fault).add("audit.yolo.auto_approved")
    assert _approve(yolo) == (None, "audit_failed")
    status = yolo.status()
    assert status["last_end"]["reason"] == "revoked_audit_failed"
    assert status["latched"] is True
    assert _approve(yolo) == (None, "no_window")


def test_a_stop_after_the_permit_refuses_the_consume(yolo, audit, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    yolo.fence("stop")
    assert yolo.consume(grant) in ((False, "permit_stale"), (False, "window_ended"))
    assert _window(yolo) is None
    assert yolo.status()["last_end"]["reason"] == "revoked_stop"


def test_expiry_before_the_consume_refuses_it(yolo, clock, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    clock.tick(WINDOW_SEC)
    assert yolo.consume(grant)[0] is False
    assert yolo.status()["last_end"]["reason"] == "expired_time"


def test_a_consumed_row_that_fails_revokes(yolo, audit, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    audit.fail.add("audit.yolo.consumed")
    yolo.consume(grant)
    assert yolo.status()["last_end"]["reason"] == "revoked_audit_failed"


def test_a_consumed_row_that_fails_refuses_the_call(yolo, audit, human):
    """#601 A8: no ``consumed`` row → the call does not run (the boundary refuses)."""
    _open(yolo)
    grant, _ = _approve(yolo)
    audit.fail.add("audit.yolo.consumed")
    assert yolo.consume(grant) == (False, "audit_failed")
    assert yolo.status()["latched"] is True


def test_a_failed_consumed_row_at_the_cap_latches_across_a_restart(audit, clock, tmp_path):
    """#601 A8: the last slot's ``consumed`` row fails after the cap already ended the
    window: the call is refused and the end is ``revoked_audit_failed`` (abnormal, §11) —
    its ended row says so, the marker stays open, and a service built again from the same
    chain and marker is latched on that window until a person unlatches it."""
    marker = WindowMarker(tmp_path)
    yolo = _service(audit, clock, marker, cap=1)
    with origin.human_turn(AGENT):
        _open(yolo)
        grant, _ = _approve(yolo)
        audit.fail.add("audit.yolo.consumed")
        assert yolo.consume(grant) == (False, "audit_failed")
    expected = [
        {"subject": "window", "window_id": grant.window_id, "reason": "revoked_audit_failed"}
    ]
    status = yolo.status()
    assert status["last_end"]["reason"] == "revoked_audit_failed"
    assert status["last_end"]["class"] == "abnormal"
    assert status["latched"] is True and status["latches"] == expected
    ended = audit.of("ended")[-1]
    assert (ended["end_reason"], ended["end_class"]) == ("revoked_audit_failed", "abnormal")
    assert ended["window_id"] == grant.window_id and ended["used"] == 1
    audit.fail.discard("audit.yolo.consumed")
    restarted = _service(audit, clock, marker, cap=1)
    assert restarted.machine.latched is True
    assert restarted.status()["latches"] == expected
    with pytest.raises(sm.Refused) as error:
        restarted.press("internal")
    assert error.value.code == "yolo_latched"


# --- pause / resume (§6) -------------------------------------------------------------------


def test_the_breaker_pauses_and_a_dwell_resumes_by_cas(yolo, audit, breaker, clock, human):
    _open(yolo)
    breaker.level = "warning"
    yolo.tick()
    window = _window(yolo)
    assert window["state"] == "paused" and window["pause_reason"] == "breaker_warning"
    paused = audit.of("paused")[0]
    assert (paused["generation_from"], paused["generation_to"]) == (1, 2)
    assert _approve(yolo) == (None, "paused")  # a card while paused
    breaker.level = "normal"
    yolo.tick()  # the dwell starts
    clock.tick(DWELL_SEC - 5)
    yolo.tick()
    assert _window(yolo)["state"] == "paused" and 0 < _window(yolo)["resume_in_sec"] <= 5
    breaker.level = "coherence"  # any non-NORMAL sample restarts the dwell
    clock.tick(10)
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC - 1)
    yolo.tick()
    assert _window(yolo)["state"] == "paused"
    clock.tick(2)
    yolo.tick()
    assert _window(yolo)["state"] == "active"
    resumed = audit.of("resumed")[0]
    assert resumed["paused_ref"].startswith("op:" + paused["operation_id"])
    assert (resumed["generation_from"], resumed["generation_to"]) == (2, 3)
    assert _approve(yolo)[1] == "approved"


def test_a_status_poll_never_resumes_only_the_watchdog_does(yolo, audit, breaker, clock):
    """GET has no loosening side effect (PR #435 review): after the dwell a status look
    leaves the window paused and writes no ``resumed`` row; the next watchdog tick
    resumes it (at most ``WATCHDOG_SEC`` later)."""
    _open(yolo)
    breaker.level = "warning"
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)
    for _ in range(3):
        window = _window(yolo)
        assert window["state"] == "paused" and window["resume_in_sec"] == 0
    assert audit.of("resumed") == []
    yolo.tick()
    assert _window(yolo)["state"] == "active" and len(audit.of("resumed")) == 1


def test_outcome_rows_name_the_window_only(yolo, audit, human):
    """An external window's consumed / void rows carry no tier (none to mislabel)."""
    _open(yolo, "external")
    grant, _ = _approve(yolo)
    yolo.consume(grant)
    other, _ = _approve(yolo)
    yolo.void(other)
    for row in (audit.of("consumed")[0], audit.of("void")[0]):
        assert row["window_id"] == grant.window_id and "tier" not in row


@pytest.mark.parametrize("level", ["warning", "critical", "coherence", "unknown", None, 3])
def test_every_level_but_normal_pauses(audit, clock, tmp_path, level):
    holder = Breaker()
    yolo = _service(audit, clock, WindowMarker(tmp_path), breaker=holder)
    _open(yolo)
    holder.level = level
    yolo.tick()
    assert _window(yolo)["state"] == "paused"


def test_the_gates_stricter_snapshot_pauses_too(yolo, human):
    _open(yolo)
    assert _approve(yolo, level="warning") == (None, "paused")
    assert _window(yolo)["state"] == "paused"


def test_a_breaker_reader_that_raises_pauses(audit, clock, tmp_path):
    state = {"raise": False}

    def reader():
        if state["raise"]:
            raise RuntimeError("engine gone")
        return "normal"

    yolo = _service(audit, clock, WindowMarker(tmp_path), breaker=reader)
    _open(yolo)
    state["raise"] = True
    yolo.tick()
    assert _window(yolo)["pause_reason"] == "breaker_unknown"


def test_more_than_three_pauses_in_an_hour_revokes(yolo, breaker, clock, human):
    _open(yolo)
    for _ in range(3):
        breaker.level = "warning"
        yolo.tick()
        breaker.level = "normal"
        yolo.tick()
        clock.tick(DWELL_SEC + 1)
        yolo.tick()
        assert _window(yolo)["state"] == "active"
    assert _window(yolo)["pauses_last_hour"] == 3
    breaker.level = "warning"
    yolo.tick()
    status = yolo.status()
    assert status["last_end"]["reason"] == "revoked_flapping"
    assert status["last_end"]["class"] == "normal" and not status["latched"]


def test_pauses_older_than_an_hour_do_not_count(yolo, breaker, clock):
    _open(yolo)
    for _ in range(3):
        breaker.level = "warning"
        yolo.tick()
        breaker.level = "normal"
        yolo.tick()
        clock.tick(DWELL_SEC + 1)
        yolo.tick()
    clock.tick(FLAP_WINDOW_SEC)
    breaker.level = "warning"
    yolo.tick()
    assert _window(yolo)["state"] == "paused"


def _paused_ready(yolo, breaker, clock):
    """Paused, then NORMAL for the whole dwell: the next look resumes."""
    _open(yolo)
    breaker.level = "warning"
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)


@pytest.mark.parametrize("race", ["stop", "new_session"])
def test_a_stop_during_the_resume_row_wins(yolo, audit, breaker, clock, race):
    _paused_ready(yolo, breaker, clock)
    audit.before["audit.yolo.resumed"] = lambda: yolo.fence(race)
    yolo.tick()
    status = yolo.status()
    assert status["window"] is None or status["window"]["state"] == "ended"
    assert status["last_end"]["reason"] == f"revoked_{race}"
    assert audit.of("void")[-1]["reason_code"] == "permit_stale"


def test_expiry_during_the_resume_row_wins(yolo, audit, breaker, clock):
    _paused_ready(yolo, breaker, clock)
    audit.before["audit.yolo.resumed"] = lambda: clock.tick(WINDOW_SEC)
    yolo.tick()
    assert yolo.status()["last_end"]["reason"] == "expired_time"


def test_the_breaker_is_read_again_at_the_resume_cas(yolo, audit, breaker, clock):
    """The level is read under the lock at the CAS (not the one the dwell saw)."""
    _paused_ready(yolo, breaker, clock)

    def turn_warning():
        breaker.level = "warning"

    audit.before["audit.yolo.resumed"] = turn_warning
    yolo.tick()
    assert _window(yolo)["state"] == "paused"
    assert audit.of("void")[-1]["reason_code"] == "permit_stale"


def test_a_non_normal_sample_during_the_resume_row_restarts_the_dwell(yolo, audit, breaker, clock):
    """#601 A7: a WARNING seen (by a status poll) while the resume row is written, NORMAL
    again by the CAS: the dwell restarted, so the window stays paused."""
    _paused_ready(yolo, breaker, clock)

    def blip():
        breaker.level = "warning"
        yolo.status()
        breaker.level = "normal"

    audit.before["audit.yolo.resumed"] = blip
    yolo.tick()
    assert _window(yolo)["state"] == "paused"
    assert audit.of("void")[-1]["reason_code"] == "permit_stale"
    audit.before.pop("audit.yolo.resumed")
    clock.tick(DWELL_SEC + 1)
    yolo.tick()  # NORMAL for a whole new dwell: resumes
    assert _window(yolo)["state"] == "active"


def test_the_breaker_and_the_clocks_are_read_under_the_lock(yolo, breaker, clock):
    seen = []
    original = yolo._breaker

    def reader():
        seen.append(yolo._lock.locked())
        return original()

    _paused_ready(yolo, breaker, clock)
    yolo._breaker = reader
    yolo.tick()
    assert _window(yolo)["state"] == "active"
    assert seen and all(seen)


def test_a_failed_resume_row_revokes(yolo, audit, breaker, clock):
    _paused_ready(yolo, breaker, clock)
    audit.fail.add("audit.yolo.resumed")
    yolo.tick()
    status = yolo.status()
    assert status["last_end"]["reason"] == "revoked_audit_failed" and status["latched"]


def test_a_failed_pause_row_revokes(yolo, audit, breaker):
    _open(yolo)
    audit.fail.add("audit.yolo.paused")
    breaker.level = "warning"
    yolo.tick()
    status = yolo.status()
    assert status["last_end"]["reason"] == "revoked_audit_failed" and status["latched"]


def test_a_window_that_opened_paused_resumes_after_the_dwell(audit, clock, tmp_path):
    holder = Breaker("warning")
    yolo = _service(audit, clock, WindowMarker(tmp_path), breaker=holder)
    request_id = yolo.press("internal")["id"]
    yolo.decide(
        request_id,
        incarnation=yolo.incarnation,
        decision="confirm",
        principal="local:tester",
        channel="cli",
    )
    assert _window(yolo)["state"] == "paused"
    holder.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)
    yolo.tick()
    assert _window(yolo)["state"] == "active"
    started = audit.of("started")[0]
    assert audit.of("resumed")[0]["paused_ref"].startswith("op:" + started["operation_id"])


def test_resume_and_issue_are_judged_by_the_one_loosening_function(
    yolo, audit, breaker, clock, monkeypatch
):
    """With ``rules_loosening`` answering ``yolo_not_settable`` nothing loosens: the
    resume does not happen and an issuance does not open (the check is not decorative)."""
    from stiracore.tools import tool_config

    calls = []
    real = tool_config.rules_loosening

    def judged(before, after, **kw):
        calls.append(kw.get("context"))
        return real(before, after, **kw)

    monkeypatch.setattr(tool_config, "rules_loosening", judged)
    _paused_ready(yolo, breaker, clock)
    yolo.tick()
    assert calls == ["host_confirmed_request", "breaker_resume"]
    assert _window(yolo)["state"] == "active"

    breaker.level = "warning"
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)
    monkeypatch.setattr(tool_config, "rules_loosening", lambda *a, **k: ["yolo_not_settable"])
    yolo.tick()
    assert _window(yolo)["state"] == "paused"


def test_an_issuance_the_loosening_function_refuses_never_opens(
    audit, clock, tmp_path, monkeypatch
):
    from stiracore.tools import tool_config

    monkeypatch.setattr(tool_config, "rules_loosening", lambda *a, **k: ["yolo_not_settable"])
    yolo = _service(audit, clock, WindowMarker(tmp_path), breaker=Breaker())
    request_id = yolo.press("internal")["id"]
    yolo.decide(
        request_id,
        incarnation=yolo.incarnation,
        decision="confirm",
        principal="local:tester",
        channel="cli",
    )
    assert yolo.machine.window is None or yolo.machine.window.state == "ended"
    assert yolo.status()["latched"] is True


# --- the gate (CheckpointHooks + ApprovalBindingVerifier) ----------------------------------


class _Approver:
    """``runtime.APPROVER`` over one service (the gate's view)."""

    def __init__(self, yolo):
        self.yolo = yolo

    def approve(self, **kw):
        return self.yolo.approve(**kw)

    def consume(self, grant):
        return self.yolo.consume(grant)

    def void(self, grant, reason="binding_mismatch"):
        return self.yolo.void(grant, reason)


def _gate(yolo, rules=None, path_check=None, level=None, scope=None):
    engine = StiraEngine(config=StiraConfig())
    assert engine.agent_id == AGENT
    asked = []

    def hitl(request):
        asked.append((request.tool_name, request.reason))
        return HITLDecision(action=HITLAction.REJECT, reason="test person says no")

    engine.on_hitl_request = hitl
    rules = rules or {}

    def policy(name):
        return (rules.get(name, "ask"), 7)

    hooks = CheckpointHooks(
        engine,
        path_check=path_check,
        policy_check=policy,
        scope_policy=scope if scope is not None else _scope(),
    )
    hooks.set_auto_approver(_Approver(yolo))
    return hooks, engine, asked


def _scope(workspace=None):
    """A workspace-only ``WritePathPolicy`` (the chat server's default roots, ``@workspace``)."""
    import tempfile
    from pathlib import Path

    from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy

    base = Path(workspace or tempfile.mkdtemp(prefix="yolo-ws-")).resolve()
    return WritePathPolicy(base_dir=base, write_roots=[WORKSPACE_ROOT])


def _call(hooks, name, args=None, tool_use_id=None):
    event = SimpleNamespace(
        tool_use={
            "name": name,
            "toolUseId": tool_use_id or secrets.token_hex(8),
            "input": args if args is not None else {"path": "notes/a.md", "content": "x"},
        },
        cancel_tool=None,
        invocation_state={},
    )
    hooks.before_tool(event)
    return event


def _execute(hooks, event):
    hooks.binding_verifier.before_tool(event)
    return event


def test_the_gate_approves_an_internal_ask_and_consumes_it_at_the_boundary(yolo, audit, human):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "file_write")
    assert event.cancel_tool is None and asked == []
    assert event.tool_use["toolUseId"] in event.invocation_state[YOLO_GRANTS_KEY]
    # Not a human approval for the breaker's write block (IdentityGuard).
    assert event.tool_use["toolUseId"] not in event.invocation_state.get(
        HITL_APPROVED_TOOL_IDS_KEY, set()
    )
    _execute(hooks, event)
    assert event.cancel_tool is None
    assert _window(yolo)["used"] == 1 and audit.of("consumed")


@pytest.mark.parametrize(
    ("tier", "people"),
    [
        # A window of tier internal: every external tool keeps its card (phase 6 too).
        ("internal", ["http_request", "web_fetch", "mcp_x", "shell", "use_agent"]),
        # Tier external (phase 6): the listed external tools are answered; the
        # unclassified rest and exec still go to a person.
        ("external", ["mcp_x", "shell", "use_agent"]),
    ],
)
def test_the_gate_sends_everything_else_to_a_person(yolo, human, tier, people):
    _open(yolo, tier)
    hooks, _engine, asked = _gate(yolo)
    names = ["http_request", "web_fetch", "mcp_x", "shell", "use_agent"]
    for name in names:
        event = _call(hooks, name, {"q": 1})
        if name in people:
            assert event.cancel_tool and event.cancel_tool.startswith("HITL rejected"), name
        else:
            assert event.cancel_tool is None, name
    assert [name for name, _ in asked] == people
    assert all(reason.startswith("Tool policy is Ask") for _n, reason in asked)
    assert _window(yolo)["auto_approved"] == len(names) - len(people)


def test_never_and_the_floor_are_refused_before_yolo_sees_them(yolo, human):
    _open(yolo)
    floor = SimpleNamespace(reason="SOUL.md is protected")
    hooks, _engine, asked = _gate(
        yolo,
        rules={"file_read": "never"},
        path_check=lambda name, args: floor if "SOUL" in str(args) else None,
    )
    never = _call(hooks, "file_read", {"path": "a"})
    assert never.cancel_tool.startswith("Tool policy is Never")
    floored = _call(hooks, "file_write", {"path": "SOUL.md", "content": "x"})
    assert floored.cancel_tool.startswith("SOUL.md is protected")
    assert asked == [] and _window(yolo)["auto_approved"] == 0


def test_the_breakers_write_block_is_never_lifted(yolo, human):
    _open(yolo)
    hooks, engine, asked = _gate(yolo)
    engine.state.stability = 0.4  # WARNING: writes blocked
    engine.pre_turn()
    event = _call(hooks, "file_write")
    assert event.cancel_tool.startswith("Writes are blocked")
    assert asked == [] and _window(yolo)["auto_approved"] == 0


def test_critical_halt_goes_to_a_person_and_pauses(yolo, human):
    _open(yolo)
    hooks, engine, asked = _gate(yolo)
    engine.config.critical_action = "halt"
    engine.state.stability = 0.1
    engine.pre_turn()
    event = _call(hooks, "file_write")
    assert asked == [("file_write", hitl_reasons.CRITICAL_HALT)]
    assert event.cancel_tool and event.cancel_tool.startswith("HITL rejected")
    assert _window(yolo)["auto_approved"] == 0


def test_a_card_shown_during_a_pause_stays_the_persons_after_a_resume(yolo, breaker, clock, human):
    """One decider per request (§6a): YOLO answers only before a card exists; a card shown
    while paused is decided by the person even if the window resumes meanwhile."""
    _open(yolo)
    breaker.level = "warning"
    yolo.tick()
    hooks, engine, asked = _gate(yolo)

    def person(request):
        asked.append((request.tool_name, request.reason))
        breaker.level = "normal"  # the window resumes while the card waits
        yolo.tick()
        clock.tick(DWELL_SEC + 1)
        yolo.tick()
        assert _window(yolo)["state"] == "active"
        return HITLDecision(action=HITLAction.REJECT, reason="the person said no")

    engine.on_hitl_request = person
    event = _call(hooks, "file_write")
    assert len(asked) == 1 and event.cancel_tool.startswith("HITL rejected")
    assert _window(yolo)["auto_approved"] == 0


def test_the_gate_never_asks_yolo_at_critical_halt(yolo, human):
    """The gate itself keeps CRITICAL_HALT away from the approver (§2, §5), independent
    of the approver's own pause: an approver that would say yes is never asked."""
    hooks, engine, asked = _gate(yolo)
    consulted = []

    class Yes:
        def approve(self, **kw):
            consulted.append(kw["tool_name"])
            raise AssertionError("YOLO must not be asked at CRITICAL_HALT")

    hooks.set_auto_approver(Yes())
    engine.config.critical_action = "halt"
    engine.state.stability = 0.1
    engine.pre_turn()
    _call(hooks, "file_write")
    assert consulted == [] and asked == [("file_write", hitl_reasons.CRITICAL_HALT)]


def test_a_later_refusal_voids_the_permit(yolo, audit, human):
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "file_write")
    event.cancel_tool = "a later hook refused"  # e.g. IdentityGuard
    _execute(hooks, event)
    assert _window(yolo)["reserved"] == 0 and _window(yolo)["used"] == 0
    assert audit.of("void")[-1]["reason_code"] == "binding_mismatch"


def test_swapped_arguments_after_the_approval_are_refused(yolo, human):
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "file_write", {"path": "a", "content": "x"})
    event.tool_use = {**event.tool_use, "input": {"path": "b", "content": "y"}}
    _execute(hooks, event)
    assert event.cancel_tool and "HITL rejected" in event.cancel_tool
    assert _window(yolo)["used"] == 0 and _window(yolo)["reserved"] == 0


def test_a_paused_window_at_the_boundary_refuses_the_call(yolo, breaker, human):
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "file_write")
    breaker.level = "warning"
    yolo.tick()
    _execute(hooks, event)
    assert event.cancel_tool and "no longer valid" in event.cancel_tool


def test_an_exec_call_with_a_yolo_grant_is_never_a_human_approval(yolo, human):
    """Defence in depth: even a grant smuggled into the state of an exec call does not
    make the verifier report a human approval (the exec gate mark needs one, #362)."""
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    grant, _ = _approve(yolo, "file_write")
    from stiracore.core.approval import HITL_BINDINGS_KEY, ApprovalBinding

    tool_input = {"command": "ls"}
    tool_use_id = "tu-exec"
    binding = ApprovalBinding(
        request_id="yolo-" + grant.intent,
        agent_id=hooks.engine.agent_id,
        tool_name="shell",
        args_hash=args_hash(tool_input),
        expires_at=float("inf"),
    )
    event = SimpleNamespace(
        tool_use={"name": "shell", "toolUseId": tool_use_id, "input": tool_input},
        cancel_tool=None,
        invocation_state={
            HITL_BINDINGS_KEY: {tool_use_id: binding},
            YOLO_GRANTS_KEY: {tool_use_id: grant},
            "stira.hitl.policy_generations": {tool_use_id: ("ask", 7)},
        },
    )
    verifier: ApprovalBindingVerifier = hooks.binding_verifier
    assert verifier._verify_binding(event) is False


def test_without_an_approver_the_gate_is_unchanged(yolo, human):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)
    hooks.set_auto_approver(None)
    event = _call(hooks, "file_write")
    assert asked == [("file_write", asked[0][1])] and event.cancel_tool.startswith("HITL")


def test_an_approver_that_raises_sends_the_call_to_a_person(yolo, human):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)

    class Broken:
        def approve(self, **kw):
            raise RuntimeError("boom")

    hooks.set_auto_approver(Broken())
    _call(hooks, "file_write")
    assert len(asked) == 1


# --- no secret reaches a row or a log line ---------------------------------------------------


def test_no_argument_or_secret_reaches_a_row_or_a_log(yolo, audit, human, caplog):
    secret = "AKIA" + "Z7Q3XK2M9WPLR4TB"
    text = f"token={secret} password=hunter2-{secrets.token_hex(4)}"
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    with caplog.at_level(logging.DEBUG):
        event = _call(hooks, "file_write", {"path": "notes/secret.md", "content": text})
        _execute(hooks, event)
    assert event.cancel_tool is None
    dumped = json.dumps(audit.rows, default=str) + caplog.text
    for needle in (secret, "hunter2", "notes/secret.md", event.tool_use["toolUseId"]):
        assert needle not in dumped, needle
    assert "Auto-approved (YOLO internal): file_write" in caplog.text


# --- status (additive) -------------------------------------------------------------------------


def test_status_reports_slots_pause_and_count(yolo, breaker, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    yolo.consume(grant)
    _approve(yolo)
    window = _window(yolo)
    assert (window["used"], window["reserved"], window["limit"]) == (1, 1, yolo.cap)
    assert window["auto_approved"] == 2 and window["paused"] is False
    breaker.level = "critical"
    yolo.tick()
    window = _window(yolo)
    assert window["paused"] is True and window["pause_reason"] == "breaker_critical"
    assert window["resume_in_sec"] is None


def test_the_ended_row_counts_the_slots(yolo, audit, human):
    _open(yolo)
    grant, _ = _approve(yolo)
    yolo.consume(grant)
    _approve(yolo)
    yolo.fence("stop")
    ended = audit.of("ended")[-1]
    assert ended["used"] == 1 and ended["reserved"] == 1


def test_the_module_constants_are_the_adr_values():
    assert DWELL_SEC == 600 and FLAP_WINDOW_SEC == 3600 and yolo_requests.FLAP_MAX == 3
    assert yolo_requests.DEFAULT_CAP == 2000
    assert sm.PAUSE_REASONS == {
        "breaker_warning",
        "breaker_coherence",
        "breaker_critical",
        "breaker_unknown",
    }


def test_runtime_approver_without_a_desk_answers_nothing():
    runtime._reset_for_tests()
    with origin.human_turn(AGENT):
        assert runtime.APPROVER.approve(
            tool_name="file_write",
            args_digest=DIGEST,
            tool_use_id="x",
            level="normal",
            agent_id=AGENT,
        ) == (None, "no_window")
    assert runtime.APPROVER.consume(object()) == (False, "window_ended")
    assert runtime.APPROVER.void(object()) is False


# --- §3: only the chat route marks a human turn ---------------------------------------------


def test_only_the_chat_route_marks_a_human_turn(monkeypatch):
    """The chat / Stage route runs its turn inside the human-turn mark; the webhook
    ingress (same agent, same runner) does not — YOLO never covers it (§3, Open (4))."""
    import threading

    from flask import Flask

    from stiracore.integrations.webhook import WebhookReceiver
    from stiracore.observability.chat_api import create_chat_blueprint
    from tests.test_webhook import signed

    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "test-secret")
    seen = []

    def runner(message):
        seen.append(origin.current())
        return "ok"

    lock = threading.Lock()
    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(runner, runner=runner, turn_lock=lock))
    app.register_blueprint(WebhookReceiver(runner=runner, turn_lock=lock))
    client = app.test_client()
    response = client.post("/api/chat", json={"message": "hello"})
    response.get_data()  # drain the stream
    assert signed(client).status_code == 200
    assert seen == [origin.HUMAN_TURN, None]
    assert origin.current() is None
