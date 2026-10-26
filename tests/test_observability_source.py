"""/observability says where its events come from (#278 / #286); one handler per turn route.

- observer (dashboard/app.py) serves no event stream: the page shows that, never opens
  the stream and never asks for a reload (#278)
- The chat server's stream and snapshot read turns.db (the turn events' source of truth); the page
  says so (#286). tests/test_turns_observability.py holds the data checks
- The chat server's /api/turns and /api/turns/<id>/events are served once, by the chat API
  (turns.db); the event-store SSE blueprint has no copies of them (#286)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from stiracore.core import NeuralState

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "observability_source_dom.cjs"
TURN_PATHS = ("/api/turns", "/api/turns/<turn_id>/events")


@pytest.fixture
def observer(tmp_path):
    from dashboard.app import create_app

    path = tmp_path / "state.json"
    NeuralState().save(path)
    return create_app(path, tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(
        nxp_agent,
        "Agent",
        Mock(
            side_effect=lambda **kw: Mock(
                return_value="Hello",
                messages=list(kw.get("messages") or []),
                system_prompt=kw.get("system_prompt", ""),
            )
        ),
    )
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    return stira_server.create_app()


def _page(app) -> str:
    response = app.test_client().get("/observability")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_observer_page_states_that_it_has_no_stream(observer):
    html = _page(observer)
    assert 'data-event-source="absent"' in html
    assert 'data-i18n="observability.source_absent_notice"' in html
    # The page would otherwise poll a path this server does not serve.
    assert observer.test_client().get("/api/observability/events").status_code == 404


def test_stira_page_states_that_it_reads_turns_db(stira):
    html = _page(stira)
    assert 'data-event-source="turns"' in html
    assert 'data-i18n="observability.source_turns_notice"' in html
    snapshot = stira.test_client().get("/api/observability/snapshot").json
    assert snapshot["source"] == "turns.db" and snapshot["event_count"] == 0


def test_stira_turn_routes_have_one_handler_from_the_chat_api(stira):
    for path in TURN_PATHS:
        rules = [rule for rule in stira.url_map.iter_rules() if rule.rule == path]
        assert len(rules) == 1, (path, [rule.endpoint for rule in rules])
        assert rules[0].endpoint.startswith("chat"), rules[0].endpoint
    # The stream and snapshot stay where the page reads them.
    paths = {rule.rule for rule in stira.url_map.iter_rules()}
    assert {"/api/observability/events", "/api/observability/snapshot"} <= paths
    body = stira.test_client().get("/api/turns").json
    assert isinstance(body, dict) and "turns" in body  # the chat API's shape (turns.db)


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
    pytest.skip(reason)


@pytest.mark.parametrize(("fixture", "source"), [("observer", "absent"), ("stira", "turns")])
def test_page_script_follows_the_source_and_passes_axe(fixture, source, request, tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    page = tmp_path / "observability.html"
    page.write_text(_page(request.getfixturevalue(fixture)), encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(page), source],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, output


def test_page_script_shows_turns_db_events(stira, tmp_path):
    """The shipped script turns the turns.db event contract into log, timeline and cost."""
    import json as jsonlib

    from stiracore.observability.turns_view import page_event

    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")

    def ev(n, turn, kind, data):
        return page_event(
            n,
            {
                "event_id": f"e{n}",
                "turn_id": turn,
                "seq": n,
                "ts": 1_700_000_000.0 + n,
                "type": kind,
                "data": data,
            },
        )

    tool = {"tool_use_id": "u1", "name": "file_write", "started_at": 1_700_000_002.0}
    events = [
        ev(1, "a" * 32, "turn.started", {}),
        ev(2, "a" * 32, "tool_call.created", {**tool, "status": "running"}),
        ev(3, "a" * 32, "approval.requested", {"request_id": "r", "tool_name": "file_write"}),
        ev(4, "a" * 32, "approval.resolved", {"request_id": "r", "decision": "approve"}),
        ev(5, "a" * 32, "tool_call.updated", {**tool, "status": "done", "duration_ms": 900}),
        ev(6, "a" * 32, "breaker.transition", {"from": "NORMAL", "to": "WARNING"}),
        ev(
            7,
            "a" * 32,
            "turn.completed",
            {"usage": {"input_tokens": 1200, "output_tokens": 80}, "model": "m-1"},
        ),
        ev(8, "b" * 32, "turn.started", {}),
        ev(
            10,
            "b" * 32,
            "approval.resolved",
            {
                "request_id": "r2",
                "tool_name": "file_write",
                "decision": "deny",
                "resolver": "operator",
            },
        ),
        ev(
            11,
            "b" * 32,
            "tool_call.updated",
            {"tool_use_id": "u2", "name": "file_write", "status": "denied", "duration_ms": 3},
        ),
        ev(
            9,
            "b" * 32,
            "turn.completed",
            {"usage": {"input_tokens": 3400, "output_tokens": 20}, "model": "m-1"},
        ),
    ]
    page = tmp_path / "observability.html"
    page.write_text(_page(stira), encoding="utf-8")
    feed = tmp_path / "events.json"
    feed.write_text(jsonlib.dumps(events), encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "observability_turns_dom.cjs"), str(page), str(feed)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, output
