"""ADR 0015 Addendum rev 6, slice S1 (#765): the axis check (A1–A10), observation only.

Scripted guard, injected clock, a real event store, a real engine and a real write policy.
Nothing here starts a browser.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.core import NeuralState, StiraConfig, StiraEngine
from stiracore.identity_paths import WritePathPolicy
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import page_check as pc
from stiracore.tools import page_check_guard as pg
from stiracore.tools import tool_policy, tool_profiles
from stiracore.tools.tool_config import ToolConfig
from stiracore.yolo import tiers

ROOT = Path(__file__).resolve().parents[1]
GOOD = {
    "status": "ok",
    "reason": None,
    "loaded": True,
    "network": {"blocked": 7, "proxy_refused": 9},
    "blocked": {"outside_workspace": 2},
}
ALL = ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10")
LIGHT = ("A2", "A4", "A5", "A6", "A7", "A8", "A9", "A10")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _verified_guard(platform="darwin"):
    identity = pg.BrowserIdentity(
        stat=lambda path: SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=3),
        version=lambda path: "Chromium 1.0",
    )
    guard = pg.Guard(
        platform=platform,
        runner=lambda browser, port: dict(GOOD),
        identity=identity,
        find_browser=lambda: "/x/chrome",
    )
    guard.measure()
    return guard


@pytest.fixture
def world(tmp_path):
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    rules = SimpleNamespace(integrity_error=None, generation=4)
    world = SimpleNamespace(
        clock=Clock(),
        guard=_verified_guard(),
        rules=rules,
        rule="always",
        policy=WritePathPolicy(base_dir=ws, write_roots=[ws]),
        store=SQLiteEventStore(tmp_path / "events.db"),
        engine=StiraEngine(state=NeuralState(), config=StiraConfig()),
        ws=ws,
    )
    world.axis = pg.Axis(clock=world.clock, guard=world.guard)
    world.scope = lambda **change: pg.AxisScope(
        **{
            "policy": world.policy,
            "tool_config": lambda: world.rules,
            "event_store": world.store,
            "engine": world.engine,
            "file_read_rule": lambda: world.rule,
            "browser": "/x/chrome",
            **change,
        }
    )
    return world


def _failed(result):
    return [name for name, ok in result.items if not ok]


def test_axis_passes_when_every_invariant_holds(world):
    assert world.axis.current().code == "not_run"
    result = world.axis.check(world.scope(), full=True)
    assert result == pg.AxisResult("pass", tuple((name, True) for name in ALL), 1000.0)
    assert world.axis.current() == result
    world.clock.now += 5
    light = world.axis.check(world.scope())
    assert (light.code, tuple(name for name, _ in light.items)) == ("pass", LIGHT)
    assert world.axis.current() == result  # a light pass leaves the full result


def test_axis_fails_closed_on_exception(world):
    def broken():
        raise RuntimeError("/secret/path")

    result = world.axis.check(world.scope(tool_config=broken), full=True)
    assert (result.code, result.items) == ("axis_unreadable", ())
    assert world.axis.current().code == "axis_unreadable"
    # An unreadable light check replaces a pass at once.
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert world.axis.check(world.scope(file_read_rule=broken)).code == "axis_unreadable"
    assert world.axis.current().code == "axis_unreadable"
    # A scope with no readers fails every item it cannot read; never an exception.
    empty = pg.Axis(clock=world.clock, guard=world.guard).check(pg.AxisScope(), full=True)
    assert empty.code != "pass" and set(_failed(empty)) >= {"A5", "A6", "A7", "A9", "A10"}
    # A broken clock, a broken guard, a stored value outside the fixed codes.
    assert pg.Axis(clock=broken, guard=world.guard).check(world.scope()).code == "axis_unreadable"
    assert pg.Axis(clock=broken).current().code == "axis_unreadable"
    assert pg.Axis(clock=world.clock, guard=object()).check(world.scope()).code == (
        "axis_unreadable"
    )
    odd = pg.Axis(clock=world.clock, guard=world.guard)
    odd.check(world.scope(), full=True)
    odd._result = pg.AxisResult("http://127.0.0.1/x", (), 1000.0)
    assert odd.current().code == "axis_unreadable"
    assert "/secret" not in repr(result)


def test_axis_guard_states_fail_a1(world):
    not_run = pg.Guard(platform="darwin", runner=lambda b, p: None, find_browser=lambda: None)
    result = pg.Axis(clock=world.clock, guard=not_run).check(world.scope(), full=True)
    assert result.code == "browser_guard_unverified" and "A1" in _failed(result)
    linux = pg.Axis(clock=world.clock, guard=_verified_guard("linux")).check(
        world.scope(), full=True
    )
    assert linux.code == "platform_unmeasured" and {"A1", "A3"} <= set(_failed(linux))
    # A4: a call ended browser_guard_unverified — the light check sees it at once.
    assert world.axis.check(world.scope(), full=True).code == "pass"
    world.guard.note_call("browser_guard_unverified")
    dropped = world.axis.check(world.scope())
    assert (dropped.code, _failed(dropped)) == ("browser_guard_unverified", ["A4"])
    assert world.axis.current().code == "browser_guard_unverified"
    # A2: another browser file than the measured one. G runs again for it: until it has
    # ended the code is ``guard_pending`` (#778), which the gate measures again.
    other = pg.Axis(clock=world.clock, guard=_verified_guard())
    assert other.check(world.scope(), full=True).code == "pass"
    changed = other.check(world.scope(browser="/y/chrome"))
    assert (changed.code, _failed(changed)) == ("guard_pending", ["A2"])


def test_axis_stale_after_max_age(world):
    assert pg.AXIS_MAX_AGE_S == 600 == pc.AXIS_MAX_AGE_S
    assert world.axis.check(world.scope(), full=True).code == "pass"
    world.clock.now += pg.AXIS_MAX_AGE_S
    assert world.axis.current().code == "pass"
    world.clock.now += 0.5
    assert world.axis.current().code == "axis_stale"
    # The next check is a full one (A1 and A3 are in it) and the result is fresh again.
    again = world.axis.check(world.scope())
    assert (again.code, tuple(name for name, _ in again.items)) == ("pass", ALL)
    assert world.axis.current().code == "pass"
    world.clock.now -= 10  # a clock that went back is not "recent"
    assert world.axis.current().code == "axis_stale"


def test_axis_rules_locked(world, tmp_path):
    assert world.axis.check(world.scope(), full=True).code == "pass"
    world.rules = ToolConfig.locked(tmp_path / "tool-config.json", "edited")
    locked = world.axis.check(world.scope())
    assert (locked.code, _failed(locked)) == ("rules_locked", ["A5"])
    assert world.axis.current().code == "rules_locked"
    assert world.axis.check(world.scope(), full=True).code == "rules_locked"  # until a save
    # A person's save: a newer generation is capped until the next full check re-reads it.
    world.rules = SimpleNamespace(integrity_error=None, generation=5)
    assert world.axis.check(world.scope()).code == "rules_changed"
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert world.axis.check(world.scope()).code == "pass"
    assert world.axis.check(world.scope(tool_config=lambda: None)).code == "rules_locked"


def test_axis_write_floor_changed(world, tmp_path):
    assert world.axis.check(world.scope(), full=True).code == "pass"
    data = tmp_path / "data"
    (data / "workspace").mkdir(parents=True)
    wider = WritePathPolicy(
        base_dir=world.ws,
        write_roots=[world.ws],
        floor_tree=data,
        floor_tree_writable=["workspace"],
    )
    changed = world.axis.check(world.scope(policy=wider))
    assert (changed.code, _failed(changed)) == ("write_floor_changed", ["A7"])
    # Sticky until restart: the original policy does not bring it back, nor a full check.
    assert world.axis.check(world.scope()).code == "write_floor_changed"
    assert world.axis.check(world.scope(), full=True).code == "write_floor_changed"
    assert world.axis.current().code == "write_floor_changed"
    # The digest is one 64-hex value; it names no path.
    assert re.fullmatch(r"[0-9a-f]{64}", world.axis._floor)
    assert pg._floor_digest(world.policy) != pg._floor_digest(wider)


def test_axis_record_unhealthy(world):
    verified = []
    real = world.store.verify_chain

    def counting():
        verified.append(1)
        return real()

    world.store.verify_chain = counting
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert world.axis.check(world.scope()).code == "pass"
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert verified == [1]  # verify_chain at start only; later the healthy flag
    sick = SimpleNamespace(healthy=False, verify_chain=lambda: (True, None))
    result = world.axis.check(world.scope(event_store=sick))
    assert (result.code, _failed(result)) == ("record_unhealthy", ["A6"])
    assert world.axis.current().code == "record_unhealthy"
    # A chain that does not verify at start fails, although the flag still says healthy.
    broken = SimpleNamespace(healthy=True, verify_chain=lambda: (False, "row 3"))
    fresh = pg.Axis(clock=world.clock, guard=world.guard)
    assert fresh.check(world.scope(event_store=broken), full=True).code == "record_unhealthy"
    assert pg.Axis(clock=world.clock, guard=world.guard).check(
        world.scope(event_store=None), full=True
    ).code == ("record_unhealthy")
    # In a grant: the round ledger too.
    ledger = SimpleNamespace(unhealthy_reason=None, verify=lambda: None)
    grant = {"grant_tools": frozenset({"file_read", "editor"}), "ledger": ledger}
    third = pg.Axis(clock=world.clock, guard=world.guard)
    assert third.check(world.scope(**grant), full=True).code == "pass"
    ledger.verify = lambda: "chain_mismatch"
    assert third.check(world.scope(**grant)).code == "pass"  # verify: round boundary only
    assert third.check(world.scope(**grant), full=True).code == "ledger_unhealthy"
    ledger.verify, ledger.unhealthy_reason = (lambda: None), "write_failed"
    assert third.check(world.scope(**grant)).code == "ledger_unhealthy"


def test_axis_file_read_and_breaker(world):
    assert world.axis.check(world.scope(), full=True).code == "pass"
    world.rule = "ask"
    capped = world.axis.check(world.scope())
    assert (capped.code, _failed(capped)) == ("file_read_not_always", ["A9"])
    world.rule = "always"
    out = world.axis.check(world.scope(grant_tools=frozenset({"editor"})))
    assert (out.code, _failed(out)) == ("not_in_scope", ["A9"])
    world.engine.state.stability = 0.2
    hot = world.axis.check(world.scope())
    assert (hot.code, _failed(hot)) == ("breaker_not_normal", ["A10"])
    world.engine.state.stability = 0.7
    assert world.axis.check(world.scope(), full=True).code == "pass"  # not sticky
    for result in (capped, out, hot):
        assert result.code in pg.AXIS_CODES and dict(result.items)["A8"] is True


def test_axis_a9_in_a_grant_needs_file_read_always(world, monkeypatch):
    """#784 fix round: in a grant A9 is ``file_read`` in the scope **and** its live rule
    Always — at issue (the service's default reader), at the boundary and in the light
    check. A page reads workspace files; with Ask ``file_read`` itself would need a card."""
    from stiracore.grants.service import _default_page_axis
    from stiracore.tools import page_look as pl

    monkeypatch.setattr(pc, "axis", world.axis)
    monkeypatch.setitem(pl._AXIS, "scope", world.scope)
    tools = frozenset({"file_read", "page_look"})
    assert _default_page_axis(tools) == "pass"  # issue: a full check
    assert pl.grant_axis(tools) == "pass"  # a call: the light check
    for rule in ("ask", "never", None):
        world.rule = rule
        assert _default_page_axis(tools) == "file_read_not_always"
        assert pl.grant_axis(tools, None, full=True) == "file_read_not_always"
        light = world.axis.check(world.scope(grant_tools=tools))
        assert (light.code, _failed(light)) == ("file_read_not_always", ["A9"])
    world.rule = "always"
    assert pl.grant_axis(tools, None, full=True) == "pass"
    assert pl.grant_axis(frozenset({"page_look"})) == "not_in_scope"
    no_reader = world.scope(grant_tools=tools, file_read_rule=None)
    assert world.axis.check(no_reader).code == "file_read_not_always"


def test_axis_has_no_writer():
    # The results are immutable values and the public surface only measures or reads.
    assert dataclasses.fields(pg.AxisResult) and pg.AxisResult.__dataclass_params__.frozen
    assert pg.GuardState.__dataclass_params__.frozen and pg.AxisScope.__dataclass_params__.frozen
    assert {n for n in vars(pg.Axis) if not n.startswith("_")} == {"check", "current"}
    writer = re.compile(r"^(set|put|store|write|update|load|restore|override|force)", re.IGNORECASE)
    public = [n for n in vars(pg) if not n.startswith("_")]
    assert [n for n in public if writer.match(n)] == []
    assert [n for n in vars(pg.Guard) if not n.startswith("_") and writer.match(n)] == []
    # No tool argument names the guard, the axis, a rule, a port or a reviewer.
    properties = pc.page_check.tool_spec["inputSchema"]["json"]["properties"]
    assert set(properties) == {"target", "actions", "wait_ms"}
    # The module reads no file, setting or environment for its state, and does not log.
    source = (ROOT / "src/stiracore/tools/page_check_guard.py").read_text(encoding="utf-8")
    for reader in ("environ", "json.load", "read_text", "getenv", "logging", "print("):
        assert reader not in source, reader
    # Nothing else in the server reaches into the state, and only the tool module and the
    # doctor CLI may start a measurement. Routes and settings can only read.
    private = re.compile(r"page_check_guard\._|\b_GUARD\b|\bpc\.(axis|pg)\._")
    starters = re.compile(r"\b(measure_guard|start_guard_measurement|ensure_guard)\b")
    may_import = {
        "src/stiracore/tools/page_check.py",
        "src/stiracore/diagnostics/doctor.py",  # reads guard_report()
        "src/stiracore/diagnostics/__main__.py",
        "src/stiracore/self_protect.py",  # names the file in the never-editable list; imports nothing
    }
    may_start = may_import - {"src/stiracore/diagnostics/doctor.py"} | {
        "dashboard/stira_server.py",  # the start-up call (off the request path)
    }
    reaching, starting, importing = [], [], []
    for base in ("src", "dashboard"):
        for path in sorted((ROOT / base).rglob("*.py")):
            name = path.relative_to(ROOT).as_posix()
            if name == "src/stiracore/tools/page_check_guard.py":
                continue
            text = path.read_text(encoding="utf-8")
            if private.search(text):
                reaching.append(name)
            if starters.search(text) and name not in may_start:
                starting.append(name)
            if "page_check_guard" in text and name not in may_import:
                importing.append(name)
    assert (reaching, starting, importing) == ([], [], [])


def test_page_check_permissions_unchanged(world):
    """C1, C4, C5 of the addendum equal today — also while the guard is verified and the
    axis passes: nothing consumes the result."""

    def permissions():
        return (
            tool_policy.NO_ALWAYS_TOOLS,
            tool_policy.CONDITIONAL_ALWAYS_TOOLS,
            tool_policy.always_allowed("page_check"),
            tool_policy.always_capped("page_check"),
            tool_policy.required_ask({"page_check"}, set()),
            tiers.PERSON_TOOLS,
            tuple(tool_profiles.PROFILE_TABLES[p]["page_check"] for p in tool_profiles.PROFILE_IDS),
        )

    today = (
        # C1; S4 moved page_look to the conditional set, page_check's own entry is unchanged.
        # S6 adds page_review (Ask / Never until S7).
        frozenset({"page_check", "page_review", "propose_procedure"}),
        frozenset({"run_checks", "page_look"}),
        False,
        False,
        {"page_check"},
        # C4; S3 adds page_look, S6 page_review.
        frozenset(
            {
                "diagram",
                "delegate",
                "page_check",
                "page_look",
                "page_review",
                "propose_procedure",
                "run_checks",
            }
        ),
        ("never", "ask", "ask"),  # C5
    )
    assert permissions() == today
    assert world.axis.check(world.scope(), full=True).code == "pass"
    assert world.guard.state().verified
    assert permissions() == today
    # page_review (S6) is Ask / Never and a person's card; no grant can name it (S7).
    assert tool_profiles.PROFILE_TABLES["minimal"]["page_review"] == "never"
    assert "page_review" in tiers.PERSON_TOOLS
    from stiracore.grants import schema

    assert "page_review" not in schema.TOOLS
    assert "page_check" not in schema.TOOLS
