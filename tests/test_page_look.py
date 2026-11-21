"""ADR 0015 Addendum rev 6, slice S3: ``page_look`` (workspace file targets only, Ask /
Never) and R1, the screenshot registry. No browser is started here: ``run_page_check`` is
replaced by a recorder, and R1 runs on an injected clock."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from stiracore.core.engine import HITL_APPROVED_TOOL_IDS_KEY
from stiracore.identity_paths import WritePathPolicy
from stiracore.observability import audit_chain
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import page_check as pc
from stiracore.tools import page_look as pl
from stiracore.tools import page_shot_registry as r1

RUN = "20261114-010203-abcdef"


@pytest.fixture
def ws(tmp_path):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    (root / "index.html").write_text("<p>Secret Page Words</p>", encoding="utf-8")
    return root


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    def fake_run_page_check(target, policy, **kwargs):
        seen.append((target, kwargs))
        return {"status": "ok", "screenshots": []}

    monkeypatch.setattr(pc, "run_page_check", fake_run_page_check)
    return seen


def _stream(tool, tool_use, invocation_state=None):
    async def collect():
        return [event async for event in tool.stream(tool_use, invocation_state or {})]

    return asyncio.run(collect())


@pytest.mark.parametrize(
    "target",
    [
        "http://localhost:8080/index.html",
        "https://localhost/",
        "http://127.0.0.1:5173",
        "HTTP://LOCALHOST:8080",
        "http:localhost:8080",
        "  http://[::1]:3000/",
        "ws://localhost:9000",
        "http://example.com/",
    ],
)
def test_page_look_refuses_localhost(ws, calls, target):
    result = pl.run_page_look(target, WritePathPolicy(base_dir=ws, write_roots=[ws]))
    assert result["status"] == "refused"
    assert result["reason"] == pl.LOCALHOST_NOT_ALLOWED
    assert calls == []  # refused before run_page_check (and so before a browser starts)


def test_page_look_file_target_never_gets_a_port(ws, calls, monkeypatch):
    monkeypatch.setattr(pc, "allowed_local_ports", lambda: (8080,))
    pl.run_page_look("index.html", WritePathPolicy(base_dir=ws, write_roots=[ws]))
    assert calls == [
        ("index.html", {"browser": None, "wait_ms": 1000, "actions": None, "local_ports": ()})
    ]


def test_page_look_ignores_extra_inputs(ws, calls, monkeypatch):
    spec = pl.page_look.tool_spec
    properties = spec["inputSchema"]["json"]["properties"]
    assert set(properties) == {"target", "actions", "wait_ms"}
    assert spec["name"] == "page_look"
    monkeypatch.setattr(pc, "allowed_local_ports", lambda: (8080,))
    tool = pl.WorkspacePageLook(pl.page_look, WritePathPolicy(base_dir=ws, write_roots=[ws]))
    extra = {
        "target": "index.html",
        "wait_ms": 0,
        "local_ports": [8080],
        "port": 8080,
        "rule": "always",
        "reviewer": "http://127.0.0.1:1/v1",
        "budget": 10**9,
        "axis": "pass",
    }
    events = _stream(
        tool,
        {"toolUseId": "t1", "name": "page_look", "input": extra},
        {HITL_APPROVED_TOOL_IDS_KEY: {"t1"}},
    )
    assert len(events) == 1
    assert calls == [
        ("index.html", {"browser": None, "wait_ms": 0, "actions": None, "local_ports": ()})
    ]


def _store_rows(store, status="ok"):
    return [
        e.attributes
        for e in store.get_events(limit=10, event_type=f"audit.page.page_look.{status}")
    ]


def test_page_look_record_allow_list(tmp_path, ws):
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        chain = AuditChain(store)
        # A field outside the allow-list (or a value outside its form) writes nothing.
        for bad in (
            {"path": "index.html"},
            {"url": "http://localhost:1/"},
            {"title": "Secret Page Words"},
            {"target": "index.html"},
            {"axis": "maybe"},
            {"counts": {"text": 1}},
            {"shots": ["not-a-digest"]},
        ):
            with pytest.raises(ValueError):
                chain.record_page("page_look", "ok", **bad)
        with pytest.raises(ValueError):
            chain.record_page("page_check", "ok")  # page_check keeps today's rows
        assert _store_rows(store) == []

        data = b"\x89PNG fake"
        registry = r1.ShotRegistry(clock=lambda: 0.0)
        registry.note(RUN, 1, data)
        result = {
            "status": "ok",
            "title": "Secret Page Words",
            "text": "Secret Page Words",
            "open_yourself": {"path": "index.html", "url": "file:///private/ws/index.html"},
            "steps": [{"action": "click"}, {"action": "type"}],
            "page_errors": {"count": 1, "messages": ["Secret Page Words"]},
            "console_errors": {"count": 0},
            "network": {"blocked": 2, "hosts": ["example.com"]},
            "screenshots": [f".stira-verify/{RUN}/1.png"],
            "elapsed_ms": 120,
        }
        arguments = {
            "target": "index.html",
            "actions": [{"click": "#secret-selector"}, {"type": "typed secret", "into": "#x"}],
        }
        pl.set_audit(chain.record_page)
        try:
            pl.shot_registry, original = registry, pl.shot_registry
            try:
                pl.record_call(arguments, result)
            finally:
                pl.shot_registry = original
        finally:
            pl.set_audit(None)
        (row,) = _store_rows(store)
        assert set(row) <= audit_chain.PAGE_FIELDS  # the schema: nothing else is kept
        assert {"axis", "guard", "target", "counts", "shots", "actions"} <= set(row)
        assert row["target"].startswith("page:") and len(row["target"]) == 69
        assert row["shots"] == [hashlib.sha256(data).hexdigest()]
        assert row["counts"]["steps"] == 2 and row["counts"]["network_blocked"] == 2
        assert row["actions"] == {"click": 1, "type": 1}
        text = json.dumps(row)
        for leak in (
            "Secret Page Words",
            "secret-selector",
            "typed secret",
            "index.html",
            "file://",
            "example.com",
            "/private/",
            RUN,
        ):
            assert leak not in text, leak
    finally:
        store.close()


def test_page_look_record_field_forms(tmp_path):
    """Each allow-listed field keeps its fixed form: a value outside it writes nothing."""
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        chain = AuditChain(store)
        for bad in (
            {"grant": "g1"},
            {"grant": "A" * 32},  # upper case: not the grant id's form
            {"grant": "ab" * 16 + "\n"},
            {"grant": 7},
            {"origin": "webhook"},
            {"origin": "Secret Page Words"},
            {"answered_by": "model"},
            {"round": -1},
            {"round": True},
            {"guard": "fine"},
            {"actions": {"#secret-selector": 1}},
            {"actions": {"click": -1}},
        ):
            with pytest.raises(ValueError):
                chain.record_page("page_look", "ok", **bad)
        assert _store_rows(store) == []
        chain.record_page(
            "page_look", "ok", grant="ab" * 16, origin="grant", answered_by="grant", round=3
        )
        (row,) = _store_rows(store)
        assert (row["grant"], row["origin"], row["answered_by"], row["round"]) == (
            "ab" * 16,
            "grant",
            "grant",
            3,
        )
        for origin in sorted(audit_chain.PAGE_ORIGINS):
            chain.record_page("page_look", "ok", origin=origin)
        assert audit_chain.PAGE_ORIGINS == {"chat_person", "yolo_window", "grant"}
    finally:
        store.close()


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError, KeyError, TypeError])
def test_page_look_audit_failure_leaves_the_call_unchanged(ws, calls, caplog, error, monkeypatch):
    """The record must never break the call: a raising writer changes nothing in the result
    and leaves one log line with the error's type (never its text). The call is one the
    rule answered (a person's answer writes no row)."""
    from stiracore.core.engine import RULE_ALWAYS_TOOL_IDS_KEY
    from stiracore.yolo import origin

    monkeypatch.setattr(pl, "live_cap_reason", lambda: None)
    tool = pl.WorkspacePageLook(pl.page_look, WritePathPolicy(base_dir=ws, write_roots=[ws]))
    use = {"toolUseId": "t1", "name": "page_look", "input": {"target": "index.html"}}

    def stream():
        with origin.human_turn("someone"):
            assert pl.reserve("t1") is True
            return _stream(tool, use, {RULE_ALWAYS_TOOL_IDS_KEY: {"t1"}})

    expected = stream()
    assert len(expected) == 1

    def broken(*args, **kwargs):
        raise error("Secret Page Words")

    pl.set_audit(broken)
    try:
        with caplog.at_level("ERROR", logger=pl.__name__):
            events = stream()
    finally:
        pl.set_audit(None)
    assert events == expected and len(calls) == 2
    (line,) = [r.getMessage() for r in caplog.records if r.name == pl.__name__]
    assert error.__name__ in line and "Secret Page Words" not in line


def test_page_tools_are_outside_content_and_untrusted():
    """C24: both page tools' results are outside content for the containment observer and
    carry the ``program`` (untrusted) source label."""
    from stiracore import labels
    from stiracore.middleware import containment_observer

    for name in ("page_check", "page_look"):
        assert name in containment_observer._OUTSIDE_CONTENT_TOOLS, name
        assert labels.tool_source(name) == "program", name
        assert labels.tool_source(name) not in labels.TRUSTED_SOURCES


@pytest.mark.parametrize("registered", [True, False])
def test_the_server_wires_the_page_look_audit(tmp_path, monkeypatch, registered):
    """The chat server gives ``page_look`` the audit chain's ``record_page`` when the tool is
    registered (a browser was found), and nothing when it is not."""
    pytest.importorskip("flask")
    from stiracore.tools import file_tools, tool_registry
    from tests.test_containment_server import _app

    monkeypatch.setattr(pl, "_AUDIT", {"record": None})
    monkeypatch.setattr(file_tools, "_READ_AUDIT", {"record": None})
    if registered:
        monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_look", pl.page_look)
    else:
        monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_look", raising=False)
    _app(tmp_path, monkeypatch)
    record = pl._AUDIT["record"]
    if not registered:
        assert record is None
        return
    assert isinstance(record.__self__, AuditChain)
    assert record.__func__ is AuditChain.record_page
    # A call no person's card answered lands in the server's own event store.
    pl.record_call({"target": "http://localhost:1/"}, pl.run_page_look("http://localhost:1/", None))
    store = SQLiteEventStore(tmp_path / "data" / "events.db")
    try:
        (row,) = _store_rows(store, "refused")
    finally:
        store.close()
    assert row["reason"] == pl.LOCALHOST_NOT_ALLOWED and set(row) <= audit_chain.PAGE_FIELDS


def test_page_look_person_answer_writes_no_row(ws, calls):
    rows: list = []
    pl.set_audit(lambda *a, **k: rows.append((a, k)))
    try:
        tool = pl.WorkspacePageLook(pl.page_look, WritePathPolicy(base_dir=ws, write_roots=[ws]))
        use = {"toolUseId": "t1", "name": "page_look", "input": {"target": "index.html"}}
        _stream(tool, use, {HITL_APPROVED_TOOL_IDS_KEY: {"t1"}})
        assert rows == [] and len(calls) == 1
        # No gate answered this call (no person's card, no rule answer): refused, no
        # browser, and the row says so (no ``answered_by``).
        (event,) = _stream(tool, use, {})
        assert len(calls) == 1
        assert pl.UNANSWERED_CALL in json.dumps(event.tool_result)
        ((args, fields),) = rows
        assert args == ("page_look", "refused")
        assert fields["reason"] == pl.UNANSWERED_CALL and "answered_by" not in fields
    finally:
        pl.set_audit(None)


def test_page_look_offered_only_with_the_workspace_policy(ws, monkeypatch):
    """C6 and C24: no write policy, no tool; hidden at WARNING like page_check; never in a
    grant scope, the job tools or the admin-unreachable list."""
    from stiracore.grants import schema
    from stiracore.scheduler import JOB_READ_ONLY_TOOLS
    from stiracore.tools import tool_registry
    from stiracore.yolo.admin_guard import ADMIN_UNREACHABLE_TOOLS

    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_look", pl.page_look)
    policy = WritePathPolicy(base_dir=ws, write_roots=[ws])

    def names(**kw):
        return [t.tool_name for t in tool_registry.get_tools(**kw)]

    assert "page_look" not in names()
    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}
    assert isinstance(tools["page_look"], pl.WorkspacePageLook)
    assert "page_look" not in names(breaker_level="warning", write_policy=policy)
    # S5 (#784): nameable in a grant scope; still never a job tool
    assert "page_look" in schema.PAGE_TOOLS and "page_look" not in JOB_READ_ONLY_TOOLS
    assert "page_look" not in ADMIN_UNREACHABLE_TOOLS  # a running call still refuses admin


def test_page_look_registers_shots(ws):
    now = [0.0]
    registry = r1.ShotRegistry(clock=lambda: now[0], max_runs=2, max_age_s=60.0)
    data = b"png-bytes"
    assert registry.note(RUN, 1, data) is True
    assert registry.note(RUN, 1, b"other") is False  # written once, never replaced
    for bad in (("../x", 1, data), (RUN, 0, data), (RUN, 5, data), (RUN, 2, "text")):
        assert registry.note(*bad) is False
    (shot,) = registry.shots(RUN)
    assert (shot.index, shot.sha256, shot.size) == (1, hashlib.sha256(data).hexdigest(), 9)
    # At most max_runs runs: the oldest goes first.
    registry.note("20261114-010204-000001", 1, data)
    registry.note("20261114-010205-000002", 1, data)
    assert registry.shots(RUN) is None and len(registry) == 2
    # Expiry on the injected clock.
    now[0] = 61.0
    assert registry.shots("20261114-010205-000002") is None and len(registry) == 0
    assert r1.MAX_RUNS == 64 and r1.MAX_AGE_S == 30 * 60
    assert r1.run_of(f".stira-verify/{RUN}/2.png") == RUN and r1.run_of("/etc/passwd") is None


def test_save_screenshot_fills_the_registry(ws, monkeypatch):
    """Both page tools write through ``_save_screenshot``: each PNG it writes is noted."""
    registry = r1.ShotRegistry(clock=lambda: 0.0)
    monkeypatch.setattr(pc, "shot_registry", registry)
    policy = WritePathPolicy(base_dir=ws, write_roots=[ws])
    path, error = pc._save_screenshot(policy, b"first", run=RUN, index=1)
    assert error is None and path == f".stira-verify/{RUN}/1.png"
    pc._save_screenshot(policy, b"second", run=RUN, index=2)
    shots = registry.shots(RUN)
    assert [s.index for s in shots] == [1, 2]
    assert shots[1].sha256 == hashlib.sha256(b"second").hexdigest()
    assert (ws / path).read_bytes() == b"first"
