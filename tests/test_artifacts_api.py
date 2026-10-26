"""#454 (ADR 0020 §6–§7): the artifact page, the sandboxed preview and a person's labels.

The real chat server app, offline (no model is called). Measured: ``/artifacts`` renders on the
shell; the raw preview carries its own sandbox CSP (opaque origin, no network, framed only by
the dashboard) instead of the page CSP; every other response keeps the page CSP and
``X-Frame-Options: DENY``; a label change needs the CSRF token, takes a fixed set only, is
one ``audit.artifact.labeled`` row (id and codes, no path, no content) and one label-history
entry; an audit failure changes nothing; no agent tool reaches the store.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import Mock

import pytest

from stiracore.config.data_dir import build_policy
from stiracore.config.models import AgentConfig
from stiracore.observability.artifact_api import PREVIEW_CSP
from stiracore.observability.config_api import ConfigStore
from stiracore.observability.security import CSP

NO_CSRF = {"X-Test-No-Csrf": "1"}
MARKER = "LABEL-BODY-5e2d"


@pytest.fixture
def app(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    application = stira_server.create_app(config_store=store)
    yield application
    stira_server.shutdown_app(application)


def _register(app, name: str, text: str, turn: str = "turn-1") -> str:
    from dashboard import stira_server

    root = stira_server.DATA_DIR
    policy, _floor = build_policy(AgentConfig(), root)
    path = policy.workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    result = app.extensions["artifacts"].register(policy, name, turn_id=turn)
    assert result["status"] == "registered", result
    return result["id"]


def _events(app) -> list[tuple[str, dict]]:
    from dashboard import stira_server

    with sqlite3.connect(stira_server.DATA_DIR / "events.db") as db:
        rows = db.execute("SELECT event_type, attributes_json FROM events ORDER BY seq").fetchall()
    return [(kind, json.loads(attrs or "{}")) for kind, attrs in rows]


def test_the_page_renders_on_the_shell(app):
    response = app.test_client().get("/artifacts")
    assert response.status_code == 200
    assert b'id="app-shell"' in response.data and b"js/artifacts.js" in response.data
    assert b'href="/artifacts"' in response.data and b'aria-current="page"' in response.data


def test_the_raw_preview_is_a_sandboxed_document(app):
    ident = _register(app, "game.html", f"<script>document.title='{MARKER}'</script>\n")
    client = app.test_client()
    raw = client.get(f"/api/artifacts/{ident}/versions/1/raw")
    assert raw.status_code == 200 and raw.data.startswith(b"<script>")
    assert raw.headers["Content-Type"] == "text/html; charset=utf-8"
    csp = raw.headers["Content-Security-Policy"]
    assert csp == PREVIEW_CSP
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp
    for directive in ("connect-src 'none'", "default-src 'none'", "frame-ancestors 'self'"):
        assert directive in csp
    assert raw.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert raw.headers["X-Content-Type-Options"] == "nosniff"
    # Every other response keeps the dashboard's page policy.
    for url in ("/artifacts", f"/api/artifacts/{ident}", "/api/artifacts"):
        other = client.get(url)
        assert other.headers["Content-Security-Policy"] == CSP, url
        assert other.headers["X-Frame-Options"] == "DENY", url
    # A cross-origin read (also the sandboxed frame's own Origin: null) is refused.
    assert client.get(f"/api/artifacts/{ident}", headers={"Origin": "null"}).status_code == 403


def test_raw_serves_only_previewable_kinds(app):
    ident = _register(app, "blob.bin", "binary-ish\n")
    assert app.test_client().get(f"/api/artifacts/{ident}/versions/1/raw").status_code == 404


def test_labels_are_a_persons_change_with_an_audit_row(app):
    ident = _register(app, "hockey_game_v2.html", f"<p>{MARKER}</p>\n")
    client = app.test_client()
    url = f"/api/artifacts/{ident}/labels"
    assert client.post(url, json={"labels": ["stable"]}, headers=NO_CSRF).status_code == 403
    assert client.get(f"/api/artifacts/{ident}").get_json()["labels"] == []

    for bad in ({"labels": ["golden"]}, {"labels": "stable"}, {"labels": ["stable"], "x": 1}, []):
        assert client.post(url, json=bad).status_code == 400, bad
    saved = client.post(url, json={"labels": ["latest", "stable"]})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json() == {
        "id": ident,
        "labels": ["stable", "latest"],
        "previous": [],
        "version": 1,
        "changed": True,
    }
    detail = client.get(f"/api/artifacts/{ident}").get_json()
    assert detail["labels"] == ["stable", "latest"]
    assert [(h["labels"], h["previous"]) for h in detail["label_history"]] == [
        (["stable", "latest"], [])
    ]
    assert detail["label_history"][0]["by"].startswith("local:")
    listed = client.get("/api/artifacts").get_json()["artifacts"][0]
    assert listed["labels"] == ["stable", "latest"]

    rows = [attrs for kind, attrs in _events(app) if kind == "audit.artifact.labeled"]
    assert len(rows) == 1
    assert rows[0]["artifact_id"] == ident and rows[0]["labels"] == "stable,latest"
    assert rows[0]["previous"] == "" and rows[0]["version"] == 1
    dump = json.dumps(_events(app))
    assert "hockey_game" not in dump and MARKER not in dump

    # The same labels again: nothing written, no second row.
    assert client.post(url, json={"labels": ["stable", "latest"]}).get_json()["changed"] is False
    assert len([k for k, _ in _events(app) if k == "audit.artifact.labeled"]) == 1


def test_an_audit_failure_changes_nothing(app, monkeypatch):
    from stiracore.observability.audit_chain import AuditChain

    ident = _register(app, "a.md", "# a\n")

    def broken(self, *args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(AuditChain, "_emit", broken)
    response = app.test_client().post(f"/api/artifacts/{ident}/labels", json={"labels": ["stable"]})
    assert response.status_code == 503 and response.get_json()["error"] == "audit_failed"
    detail = app.test_client().get(f"/api/artifacts/{ident}").get_json()
    assert detail["labels"] == [] and detail["label_history"] == []


def test_no_agent_tool_reaches_the_store(app):
    from dashboard import stira_server
    from stiracore.tools import tool_registry

    policy, _floor = build_policy(AgentConfig(), stira_server.DATA_DIR)
    names = {tool.tool_name for tool in tool_registry.get_tools(write_policy=policy)}
    assert not [name for name in names if "artifact" in name or "label" in name]
    for target in ("artifacts/index.db", "artifacts/blobs/x"):
        path = str(stira_server.DATA_DIR / target)
        assert not policy.check(path).allowed and not policy.check_read(path).allowed
