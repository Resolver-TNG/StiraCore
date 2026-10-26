"""#148: admin/API permission regression tests across every route.

Builds the real chat server app (offline: no model is ever called) and the observer
dashboard, then checks every rule against a fixed inventory. Each state-changing
route must refuse requests without the CSRF token, with a wrong token, from
another Origin, flagged cross-site by the browser, or for a foreign Host — and
the view must not run. Refusals are audited without secrets.

There is no user authentication: the servers bind to loopback only
(resolve_bind_host) and the per-process CSRF token is the capability. One
process serves one agent, so no route takes an agent selector.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore

ROOT = Path(__file__).resolve().parents[1]
NO_CSRF = {"X-Test-No-Csrf": "1"}  # root conftest otherwise attaches the token

# (rule, method) -> (area, kind). kind: "read" (Host check only), "mutate"
# (Host + Sec-Fetch-Site + Origin + CSRF), "signed" (CSRF-exempt, HMAC-verified).
STIRA_ROUTES = {
    ("/", "GET"): ("page", "read"),
    ("/chat", "GET"): ("page", "read"),
    ("/memory", "GET"): ("page", "read"),
    ("/observability", "GET"): ("page", "read"),
    ("/settings", "GET"): ("page", "read"),
    ("/tools", "GET"): ("page", "read"),
    ("/static/<path:filename>", "GET"): ("page", "read"),
    ("/api/csrf", "GET"): ("security", "read"),
    ("/api/chat", "POST"): ("chat", "mutate"),
    # #452: the explicit stop of the running turn (closing the page never stops it).
    ("/api/chat/cancel", "POST"): ("chat", "mutate"),
    # #350: drops the conversation sent to the model (nothing else); Idempotency-Key required.
    ("/api/chat/session/new", "POST"): ("chat", "mutate"),
    # ADR 0012 phase 7c: the YOLO values, with and without the allowance.
    ("/api/yolo/settings", "GET"): ("yolo", "read"),
    ("/api/yolo/settings", "POST"): ("yolo", "mutate"),
    ("/api/chat/history", "GET"): ("chat", "read"),
    ("/api/gateway/status", "GET"): ("observe", "read"),
    ("/api/hitl/pending", "GET"): ("hitl", "read"),
    ("/api/hitl/history", "GET"): ("hitl", "read"),
    ("/approvals", "GET"): ("page", "read"),
    ("/schedules", "GET"): ("page", "read"),
    ("/api/scheduler", "GET"): ("scheduler", "read"),
    ("/api/scheduler/jobs", "POST"): ("scheduler", "mutate"),
    ("/api/scheduler/jobs/<job_id>", "POST"): ("scheduler", "mutate"),
    ("/api/scheduler/jobs/<job_id>/pause", "POST"): ("scheduler", "mutate"),
    ("/api/scheduler/jobs/<job_id>/resume", "POST"): ("scheduler", "mutate"),
    ("/api/scheduler/runs/<run_id>/cancel", "POST"): ("scheduler", "mutate"),
    ("/api/hitl/<request_id>/decision", "POST"): ("hitl", "mutate"),
    # ADR 0025: the session allowance list and its narrowings (revoke one, revoke all).
    ("/api/allowances", "GET"): ("hitl", "read"),
    ("/api/allowances/<allow_id>/revoke", "POST"): ("hitl", "mutate"),
    ("/api/allowances/revoke-all", "POST"): ("hitl", "mutate"),
    # ADR 0011 W2 (#296): the authority service (no route clears a latch).
    ("/api/authority/status", "GET"): ("authority", "read"),
    ("/api/authority/outbox", "GET"): ("authority", "read"),
    ("/api/authority/kill", "POST"): ("authority", "mutate"),
    ("/api/authority/outbox/ack", "POST"): ("authority", "mutate"),
    ("/api/config", "GET"): ("config", "read"),
    ("/api/config/jev", "GET"): ("config", "read"),
    ("/api/config/jev", "POST"): ("config", "mutate"),
    ("/api/config/models", "GET"): ("config", "read"),
    ("/api/config/models", "POST"): ("config", "mutate"),
    ("/api/config/models/test", "POST"): ("config", "mutate"),  # outbound connection test
    ("/api/config/hub", "GET"): ("config", "read"),  # settings hub snapshot (#227)
    ("/api/config/hub", "POST"): ("config", "mutate"),  # draft save with conflict check
    ("/api/config/hub/validate", "POST"): ("config", "read"),  # validation only, no write
    ("/api/config/history", "GET"): ("config", "read"),  # change history (no secret values)
    # ADR 0025 §9: the Make Always dialog's view of one card (issues a one-time save token)
    ("/api/config/hub/card/<request_id>", "GET"): ("config", "read"),
    ("/api/config/history/<entry_id>/rollback", "POST"): ("config", "mutate"),
    ("/api/config/providers", "GET"): ("config", "read"),
    ("/api/config/soul", "GET"): ("config", "read"),
    ("/api/config/tools", "GET"): ("config", "read"),
    ("/api/config/tools", "POST"): ("config", "mutate"),
    ("/api/tools/config", "GET"): ("config", "read"),
    ("/api/tools/config", "POST"): ("config", "mutate"),
    ("/api/tools/list", "GET"): ("config", "read"),
    # ADR 0015 Addendum rev 4 (#449): page_check's localhost ports (adding: typed confirm).
    ("/api/tools/page-check-ports", "GET"): ("config", "read"),
    ("/api/tools/page-check-ports", "POST"): ("config", "mutate"),
    ("/api/memory/tree", "GET"): ("memory", "read"),
    # #463: the procedural review screen (approve: confirm + turn lock; reject narrows).
    ("/api/memory/procedural/review", "GET"): ("memory", "read"),
    ("/api/memory/procedural/proposals/<key>/approve", "POST"): ("memory", "mutate"),
    ("/api/memory/procedural/proposals/<key>/reject", "POST"): ("memory", "mutate"),
    ("/api/observability/events", "GET"): ("observe", "read"),
    ("/api/observability/snapshot", "GET"): ("observe", "read"),
    ("/api/state/history", "GET"): ("observe", "read"),
    # Registered twice (chat_api + observability); chat_api's handler wins.
    ("/api/turns", "GET"): ("observe", "read"),
    ("/api/turns/<turn_id>/events", "GET"): ("observe", "read"),
    ("/api/turns/<turn_id>/tools/<tool_use_id>/<kind>", "GET"): ("observe", "read"),
    # #446: the Stage board's activity mode (allow-listed codes, counts, tool names).
    ("/api/activity", "GET"): ("observe", "read"),
    ("/api/webhooks/<provider>", "POST"): ("integration", "signed"),
    # Permission cards and the write-environment doctor (#244).
    ("/api/diagnostics/write-failures", "GET"): ("observe", "read"),
    ("/api/diagnostics/write-failures/<card_id>/retry", "POST"): ("diagnostics", "mutate"),
    ("/api/diagnostics/write-failures/<card_id>/dismiss", "POST"): ("diagnostics", "mutate"),
    ("/api/diagnostics/doctor", "GET"): ("observe", "read"),
    ("/api/diagnostics/doctor", "POST"): ("diagnostics", "mutate"),
    # #454 (ADR 0020): workspace artifacts (list, detail, one version, a diff).
    ("/api/artifacts", "GET"): ("artifacts", "read"),
    ("/api/artifacts/<artifact_id>", "GET"): ("artifacts", "read"),
    ("/api/artifacts/<artifact_id>/versions/<int:version>", "GET"): ("artifacts", "read"),
    ("/api/artifacts/<artifact_id>/diff", "GET"): ("artifacts", "read"),
    # The sandboxed preview document (ADR 0020 §6) and a person's labels (§7).
    ("/api/artifacts/<artifact_id>/versions/<int:version>/raw", "GET"): ("artifacts", "read"),
    ("/api/artifacts/<artifact_id>/labels", "POST"): ("artifacts", "mutate"),
    ("/artifacts", "GET"): ("page", "read"),
    # ADR 0023 phase 0 (#460-0): the containment observation, read only (no write route).
    ("/api/containment", "GET"): ("observe", "read"),
    # ADR 0022 phase 0 (#458): the taint observation, read only (no write route).
    ("/api/taint", "GET"): ("observe", "read"),
}
OBSERVER_ROUTES = {
    **{
        key: value
        for key, value in STIRA_ROUTES.items()
        if key[0].startswith(("/api/config", "/api/tools", "/api/csrf", "/api/state/history"))
        or key[0] in {"/", "/observability", "/settings", "/tools", "/static/<path:filename>"}
    },
    ("/chat", "GET"): ("page", "read"),
    ("/api/history", "GET"): ("observe", "read"),
    ("/api/state", "GET"): ("observe", "read"),
    ("/api/stimulus", "POST"): ("observe", "mutate"),
    ("/api/webhooks/<provider>", "POST"): ("integration", "signed"),
}
# ADR 0011 W4 (#296): only with gateway autonomy.stage_c (default off: none of them exists).
GRANT_ROUTES = {
    ("/api/grants", "GET"): ("grants", "read"),
    ("/api/grants/<grant_id>", "GET"): ("grants", "read"),
    # The issuer-only goal view (typed SHOW + the one-time view token).
    ("/api/grants/<grant_id>/goal", "GET"): ("grants", "read"),
    ("/api/grants/issue", "POST"): ("grants", "mutate"),
    ("/api/grants/<grant_id>/revoke", "POST"): ("grants", "mutate"),
    ("/api/grants/<grant_id>/pause", "POST"): ("grants", "mutate"),
    ("/api/grants/<grant_id>/resume", "POST"): ("grants", "mutate"),
    ("/api/grants/<grant_id>/verify", "POST"): ("grants", "mutate"),
    # The host command / dialog channel: the page polls the operation's state.
    ("/api/grants/operations/<op_id>", "GET"): ("grants", "read"),
    ("/api/grants/operations/<op_id>/confirm", "POST"): ("grants", "mutate"),
    ("/api/grants/operations/<op_id>/cancel", "POST"): ("grants", "mutate"),
}
# W5: only with autonomy.stage_c_exec as well.
EXEC_ROUTES = {("/api/grants/<grant_id>/step", "POST"): ("grants", "mutate")}
URL_VALUES = {
    "<grant_id>": "c" * 32,
    "<op_id>": "d" * 32,
    "<allow_id>": "a" * 32,
    "<path:filename>": "css/tokens.css",
    "<request_id>": "r1",
    "<job_id>": "j1",
    "<run_id>": "run1",
    "<turn_id>": "t1",
    "<tool_use_id>": "u1",
    "<kind>": "input",
    "<provider>": "generic",
    "<card_id>": "0123456789abcdef",
    "<key>": "rule-1",
    "<artifact_id>": "a0123456789abcdef0123",
    "<int:version>": "1",
}


def _url(rule: str) -> str:
    for placeholder, value in URL_VALUES.items():
        rule = rule.replace(placeholder, value)
    return rule


@pytest.fixture
def stira_app(tmp_path, monkeypatch):
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
    return stira_server.create_app(config_store=store)


@pytest.fixture
def observer_app(tmp_path):
    spec = importlib.util.spec_from_file_location("observer_app", ROOT / "dashboard" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_app(
        tmp_path / "state.json", tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "t.json"
    )


def _inventory(app) -> dict:
    out = {}
    for rule in app.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out[(rule.rule, method)] = rule.endpoint
    return out


def _spy(app) -> dict:
    """Replace every view with a counting stub: a refusal provably ran no handler,
    and the positive control causes no side effect (no turn, write or network)."""
    calls: dict[str, int] = {}
    for endpoint in list(app.view_functions):

        def stub(*args, _endpoint=endpoint, **kwargs):
            calls[_endpoint] = calls.get(_endpoint, 0) + 1
            return "", 204

        app.view_functions[endpoint] = stub
    return calls


def _token(app) -> str:
    return app.config["STIRA_CSRF_TOKEN"]


ATTACKS = {
    "no-token": lambda token: NO_CSRF,
    "wrong-token": lambda token: {"X-CSRF-Token": "x" * len(token)},
    "cross-origin": lambda token: {"X-CSRF-Token": token, "Origin": "https://evil.example"},
    "null-origin": lambda token: {"X-CSRF-Token": token, "Origin": "null"},
    # Malformed Origin used to raise in urlsplit (HTTP 500); now a refusal.
    "malformed-origin": lambda token: {"X-CSRF-Token": token, "Origin": "http://[::1"},
    "cross-site-fetch": lambda token: {"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
    "foreign-host": lambda token: {"X-CSRF-Token": token, "Host": "evil.example"},
}


def test_route_inventory_is_classified(stira_app, observer_app):
    """New routes fail here until classified (and added to docs/admin-api-permissions.md)."""
    for app, table in ((stira_app, STIRA_ROUTES), (observer_app, OBSERVER_ROUTES)):
        assert set(_inventory(app)) == set(table)
    # One process = one agent: no route selects an agent.
    assert not [r for (r, _m) in STIRA_ROUTES if "agent" in r]


def _check_mutations(app, table):
    calls = _spy(app)
    client = app.test_client()
    token = _token(app)
    checked = 0
    for (rule, method), (_area, kind) in table.items():
        if kind == "read":
            continue
        url = _url(rule)
        for name, headers in ATTACKS.items():
            if kind == "signed" and name in {"no-token", "wrong-token"}:
                continue  # CSRF-exempt; the HMAC signature is its credential (below)
            before = dict(calls)
            response = client.open(url, method=method, json={}, headers=headers(token))
            assert response.status_code == 403, (rule, name, response.status_code)
            assert calls == before, f"{rule} ran its handler for {name}"
            checked += 1
        # Positive control: the legitimate same-origin request reaches the handler.
        before = sum(calls.values())
        client.open(
            url,
            method=method,
            json={},
            headers={"X-CSRF-Token": token, "Origin": "http://localhost"},
        )
        assert sum(calls.values()) == before + 1, f"{rule}: control request did not reach handler"
    return checked


def test_every_stira_mutation_refuses_unauthenticated_and_cross_origin(stira_app):
    # 7c: /api/yolo/settings; #452: /api/chat/cancel; #494: /api/config/breaker removed;
    # #463: the two procedural review routes; #454: the artifact labels; ADR 0025: the two
    # allowance revoke routes; ADR 0011 W2: the authority kill and outbox ack
    assert _check_mutations(stira_app, STIRA_ROUTES) == 28 * 7 + 5  # +page-check-ports


def test_every_observer_mutation_refuses_unauthenticated_and_cross_origin(observer_app):
    assert _check_mutations(observer_app, OBSERVER_ROUTES) == 9 * 7 + 5  # #494, page-check-ports


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
API_READ_ATTACKS = {
    "cross-origin": {"Origin": "https://evil.example"},
    "other-port": {"Origin": "http://localhost:9999"},
    "null-origin": {"Origin": "null"},
    "cross-site-fetch": {"Sec-Fetch-Site": "cross-site"},
}


@pytest.mark.parametrize("which", ["stira", "observer"])
def test_every_unsafe_route_is_guarded_whatever_its_classification(which, stira_app, observer_app):
    """#185: walks the live url_map, not the table. Every POST/PUT/PATCH/DELETE rule
    (also the "read" ones such as hub validation, and any route added later) refuses a
    missing token and a cross-origin request before its view runs. The only CSRF
    exemption is the HMAC-signed webhook."""
    app = stira_app if which == "stira" else observer_app
    assert app.extensions["stira_security"].csrf_exempt_endpoints == {"webhooks.receive"}
    calls = _spy(app)
    client = app.test_client()
    token = _token(app)
    unsafe = [(r, m, e) for (r, m), e in _inventory(app).items() if m in UNSAFE]
    assert unsafe and {m for _r, m, _e in unsafe} <= UNSAFE
    for rule, method, endpoint in unsafe:
        attacks = ["cross-origin", "cross-site-fetch", "foreign-host"]
        if endpoint != "webhooks.receive":
            attacks += ["no-token", "wrong-token"]
        for name in attacks:
            response = client.open(_url(rule), method=method, json={}, headers=ATTACKS[name](token))
            assert response.status_code == 403, (rule, name)
    assert calls == {}


@pytest.mark.parametrize("which", ["stira", "observer"])
def test_every_api_read_and_stream_is_same_origin_only(which, stira_app, observer_app):
    """#185: GET /api/* (JSON, SSE streams, the CSRF token) refuses a foreign Origin or
    a cross-site fetch before its view runs; no CORS header is ever sent."""
    app = stira_app if which == "stira" else observer_app
    calls = _spy(app)
    client = app.test_client()
    reads = [r for (r, m) in _inventory(app) if m == "GET" and r.startswith("/api/")]
    assert "/api/observability/events" in reads or which == "observer"
    for rule in reads:
        for name, headers in API_READ_ATTACKS.items():
            response = client.get(_url(rule), headers=headers)
            assert response.status_code == 403, (rule, name)
            assert "Access-Control-Allow-Origin" not in response.headers
    assert calls == {}
    for rule in reads:  # control: same-origin reads reach the view
        client.get(_url(rule), headers={"Origin": "http://localhost"})
    assert sum(calls.values()) == len(reads)


def test_unsigned_webhook_never_runs_a_turn(stira_app, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "s" * 32)
    client = stira_app.test_client()
    response = client.post("/api/webhooks/generic", json={"a": 1}, headers=NO_CSRF)
    assert response.status_code == 401
    bad = {**NO_CSRF, "X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert client.post("/api/webhooks/generic", json={"a": 1}, headers=bad).status_code == 401


def test_reads_refuse_a_foreign_host(stira_app):
    """DNS rebinding: every route, including GETs and the token itself, checks Host."""
    client = stira_app.test_client()
    csrf = client.get("/api/csrf")
    assert csrf.status_code == 200 and "Access-Control-Allow-Origin" not in csrf.headers
    calls = _spy(stira_app)
    for rule, method in STIRA_ROUTES:
        response = client.open(_url(rule), method=method, headers={"Host": "evil.example"})
        assert response.status_code == 403, rule
    assert calls == {}


def test_config_change_without_token_leaves_files_untouched(stira_app, tmp_path):
    client = stira_app.test_client()
    store = stira_app.extensions["config_store"]
    before = {p: p.read_bytes() if p.exists() else None for p in (store.gateway_path,)}
    body = {"jev_enabled": True}
    assert client.post("/api/config/jev", json=body, headers=NO_CSRF).status_code == 403
    assert {p: p.read_bytes() if p.exists() else None for p in before} == before


def test_refusals_are_audited_without_secrets(stira_app, caplog):
    client = stira_app.test_client()
    token = _token(stira_app)
    secret_body = {"message": "TOP-SECRET-BODY", "api_key": "sk-" + "a" * 24}
    with caplog.at_level(logging.WARNING, logger="stiracore.observability.security"):
        client.post("/api/chat?access_token=QUERY-SECRET", json=secret_body, headers=NO_CSRF)
        client.post(
            "/api/config/jev",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "https://evil.example\x1b[2J/x"},
        )
        client.post("/api/hitl/r1/decision", json={}, headers={"X-CSRF-Token": "x" * 10})
        client.get("/probe%1b[2J%0aFAKE", headers={"Host": "evil.example"})
    records = [r.getMessage() for r in caplog.records if "security: denied" in r.getMessage()]
    assert len(records) == 4, records
    log = "\n".join(records)
    assert "POST /api/chat endpoint=chat_api.chat" in log
    assert "Missing or invalid CSRF token" in log
    assert "origin=invalid" in log  # malformed Origin: refused (not 500), never echoed
    for secret in (token, "TOP-SECRET-BODY", "QUERY-SECRET", "sk-", "x" * 10):
        assert secret not in log
    assert "GET /probe?[2J?FAKE endpoint= reason='Host not allowed.'" in log
    assert "host=evil.example" in log
    assert "\x1b" not in log and log.count("\n") == 3  # one line per refusal


def test_hitl_decision_requires_the_displayed_binding(stira_app):
    """Approvals must quote the pending args_hash (#146); unknown ids are 404."""
    client = stira_app.test_client()
    missing = client.post("/api/hitl/nope/decision", json={"action": "approve"})
    assert missing.status_code == 400  # args_hash required before lookup
    unknown = client.post(
        "/api/hitl/nope/decision", json={"action": "approve", "args_hash": "0" * 64}
    )
    assert unknown.status_code == 404
    assert json.loads(unknown.get_data(as_text=True))["error"]


# --- #148 round 2: bind, Host/Origin forms, GET is read-only, agent scope ------------
# Measured against the current design (no user authentication; loopback + Origin/Host
# + per-process CSRF token). These fix today's behaviour; they change no design.

LOOPBACK_BINDS = ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"]
REMOTE_BINDS = ["0.0.0.0", "::", "192.168.0.10", "10.0.0.1", "example.com", "localhost.evil"]


@pytest.mark.parametrize("host", LOOPBACK_BINDS)
def test_loopback_binds_are_accepted(monkeypatch, host):
    from stiracore.observability.security import resolve_bind_host

    monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    monkeypatch.setenv("STIRA_HOST", host)
    assert resolve_bind_host() == host


@pytest.mark.parametrize("host", REMOTE_BINDS)
@pytest.mark.parametrize("allow", [None, "0", "true", "yes"])
def test_non_loopback_binds_are_refused_without_the_exact_opt_in(monkeypatch, host, allow):
    """Only STIRA_ALLOW_REMOTE=1 (exactly) opens a non-loopback bind."""
    from stiracore.observability.security import resolve_bind_host

    monkeypatch.setenv("STIRA_HOST", host)
    if allow is None:
        monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    else:
        monkeypatch.setenv("STIRA_ALLOW_REMOTE", allow)
    with pytest.raises(SystemExit, match="no authentication"):
        resolve_bind_host()


def test_stira_entry_point_refuses_a_remote_bind_before_serving(monkeypatch):
    from dashboard import stira_server

    served = []

    # The entry point serves through ``serve(application, host, port)`` (since #339).
    monkeypatch.setattr(
        stira_server, "serve", lambda application, host, port: served.append((host, port))
    )
    monkeypatch.setattr(stira_server, "_install_log_masking", lambda *a, **k: [])
    monkeypatch.setattr(stira_server.signal, "signal", lambda *a, **k: None)
    monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    monkeypatch.setenv("STIRA_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        stira_server.main(app_factory=lambda lifecycle: object())
    assert served == []
    monkeypatch.setenv("STIRA_HOST", "127.0.0.1")
    stira_server.main(app_factory=lambda lifecycle: object())
    assert served and served[0][0] == "127.0.0.1"


def test_every_server_entry_point_binds_through_resolve_bind_host_or_loopback():
    """Static guard: each ``.run(host=...)`` or ``serve(app, host, port)`` in dashboard/ and scripts/ either asks
    resolve_bind_host() or names a loopback literal (the offline harnesses)."""
    import ast

    from stiracore.observability.security import _is_loopback

    found = []
    for path in sorted([*ROOT.glob("dashboard/*.py"), *ROOT.glob("scripts/*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) == "run":
                host = next((k.value for k in node.keywords if k.arg == "host"), None)
            elif getattr(node.func, "id", None) == "serve" and len(node.args) >= 2:
                # stira_server.serve(application, host, port)
                host = node.args[1]
            else:
                continue
            if host is None:
                continue
            found.append(path.name)
            asks = (
                isinstance(host, ast.Call) and getattr(host.func, "id", "") == "resolve_bind_host"
            ) or (
                # a name bound earlier from resolve_bind_host() (#185 refuses before start-up)
                isinstance(host, ast.Name)
                and "resolve_bind_host()" in path.read_text(encoding="utf-8")
            )
            literal = isinstance(host, ast.Constant) and _is_loopback(str(host.value))
            assert asks or literal, f"{path.name}: binds without resolve_bind_host"
    assert {"stira_server.py", "app.py"} <= set(found)


# Host header forms (measured). The port is ignored; the name must be one of the
# loopback names exactly (no trailing dot, no other loopback address, no suffix).
HOSTS = {
    "localhost": 204,
    "LOCALHOST:5555": 204,
    "127.0.0.1:1": 204,
    "[::1]:5555": 204,
    "127.0.0.2": 403,
    "0.0.0.0": 403,
    "localhost.": 204,  # one trailing dot is ignored (#338)
    "127.0.0.1.nip.io": 403,
    "[::ffff:127.0.0.1]": 403,
    "evil.example:80@localhost": 403,
    "": 403,
}


def test_host_header_forms(stira_app):
    calls = _spy(stira_app)
    client = stira_app.test_client()
    measured = {
        host: client.get("/api/gateway/status", headers={"Host": host}).status_code
        for host in HOSTS
    }
    assert measured == HOSTS
    assert calls == {"chat_api.gateway_status": 5}


def test_allowed_hosts_extend_by_name_only(monkeypatch, tmp_path):
    """STIRA_ALLOWED_HOSTS adds the names given; it never opens other names."""
    from flask import Flask

    from stiracore.observability.security import install_security

    monkeypatch.setenv("STIRA_ALLOWED_HOSTS", "stira.internal, other.internal:8443")
    app = Flask(__name__)
    install_security(app)
    app.add_url_rule("/p", "p", lambda: ("", 204))
    client = app.test_client()
    status = {
        host: client.get("/p", headers={"Host": host}).status_code
        for host in ("stira.internal", "other.internal", "localhost", "evil.internal")
    }
    assert status == {
        "stira.internal": 204,
        "other.internal": 204,
        "localhost": 204,
        "evil.internal": 403,
    }


# Origin forms on a state-changing route (token present, Host localhost:5555).
# Measured: the Origin's host:port must equal the Host header exactly. (The scheme is
# #338's; this table does not pin it.)
ORIGINS = {
    "http://localhost:5555": 204,
    "http://localhost:5555/path": 403,  # an Origin has no path (#338)
    "http://localhost:5556": 403,
    "http://127.0.0.1:5555": 403,
    "http://LOCALHOST:5555": 403,
    "http://user@localhost:5555": 403,
    "file://": 403,
}


def test_origin_forms(stira_app):
    calls = _spy(stira_app)
    client = stira_app.test_client()
    token = _token(stira_app)
    measured = {
        origin: client.post(
            "/api/config/jev",
            json={},
            headers={"Host": "localhost:5555", "Origin": origin, "X-CSRF-Token": token},
        ).status_code
        for origin in ORIGINS
    }
    assert measured == ORIGINS
    assert calls == {"config_api.post_config_jev": 1}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_unsafe_methods_need_the_token_before_routing(stira_app, method):
    calls = _spy(stira_app)
    client = stira_app.test_client()
    for rule, _m in STIRA_ROUTES:
        url = _url(rule)
        assert client.open(url, method=method, json={}, headers=NO_CSRF).status_code == 403
        assert client.open(url, method=method, json={}).status_code == 405  # not routed
    assert calls == {}


def test_no_route_grants_cors(stira_app, observer_app):
    """No route answers a foreign Origin with CORS headers (reads stay same-origin)."""
    for app, table in ((stira_app, STIRA_ROUTES), (observer_app, OBSERVER_ROUTES)):
        _spy(app)
        client = app.test_client()
        for rule, method in table:
            for verb in (method, "OPTIONS"):
                response = client.open(
                    _url(rule),
                    method=verb,
                    headers={
                        "Origin": "https://evil.example",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                names = [name.lower() for name, _value in response.headers]
                assert not [n for n in names if n.startswith("access-control-")]


def _tree_digest(root: Path) -> dict:
    import hashlib

    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("which", ["stira", "observer"])
def test_get_routes_change_no_file(request, tmp_path, which):
    """A GET-only client (a browser link, or the agent's web_fetch) changes no stored
    state: the real views run twice and every file under the data and config
    directories is byte-identical (after one warm-up pass that records first-run files)."""
    app = request.getfixturevalue(f"{which}_app")
    table = STIRA_ROUTES if which == "stira" else OBSERVER_ROUTES
    client = app.test_client()
    gets = [rule for rule, method in table if method == "GET"]

    def sweep():
        for rule in gets:
            response = client.get(_url(rule))
            assert response.status_code < 500, (rule, response.status_code)
            response.close()  # SSE: do not hold the stream

    sweep()
    before = _tree_digest(tmp_path)
    sweep()
    sweep()
    assert _tree_digest(tmp_path) == before


def test_memory_view_has_no_agent_selector(stira_app, tmp_path):
    """One process = one agent: query parameters cannot show another agent's memory."""
    from stiracore.tools.memory_store import MemoryLayer, MemoryStore

    MemoryStore(tmp_path / "data" / "memory", "other-agent").write(
        MemoryLayer.EPISODIC, "theirs", "OTHER-AGENT-MEMORY"
    )
    MemoryStore(tmp_path / "data" / "memory", "stira").write(
        MemoryLayer.EPISODIC, "mine", "OWN-MEMORY"
    )
    client = stira_app.test_client()
    for query in ("", "?agent=other-agent", "?agent_id=other-agent", "?agent_id=../other-agent"):
        text = client.get("/api/memory/tree" + query).get_data(as_text=True)
        assert "OWN-MEMORY" in text and "OTHER-AGENT-MEMORY" not in text, query


def test_an_approval_of_another_agents_request_runs_nothing(tmp_path):
    """Agent scope at execution: a request recorded for another agent in the same store
    can be decided, but its resume is refused (and revoked); no tool runs."""
    from test_hitl_binding import CALL, ExternalService, _app
    from test_hitl_persistence import Host, _pause_then_shut_down

    service = ExternalService()
    (paused,) = _pause_then_shut_down(tmp_path, service, calls=[CALL])
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    root = host.store.get(paused["id"])
    foreign = host.store.submit(
        request_id="foreign-1",
        agent_id="other-agent",
        tool_name=root.tool_name,
        tool_input=CALL["input"],
        expires_at=root.expires_at,
        tool_use_id=root.tool_use_id,
        correlation_id=root.correlation_id,
        origin=root.origin,
        checkpoint_id=root.checkpoint_id,
    )
    assert foreign
    card = next(p for p in host.broker.pending() if p["id"] == "foreign-1")
    client = _app(host.broker)
    response = client.post(
        "/api/hitl/foreign-1/decision", json={"action": "approve", "args_hash": card["args_hash"]}
    )
    assert response.status_code == 202
    (outcome,) = host.resumes
    assert (outcome.resumed, outcome.reason) == (False, "the request belongs to another agent")
    assert service.applied == []
    assert host.store.get("foreign-1").status == "cancelled"


def test_http_clients_cannot_claim_a_system_or_agent_principal(tmp_path):
    """Every dashboard decision is a ``local:`` (human) principal whatever the body says."""
    from test_hitl_binding import CALL, ExternalService, _app
    from test_hitl_persistence import Host, _pause_then_shut_down

    from stiracore.observability.scheduler_api import _principal

    assert _principal({"actor": "system"}) == "local:system"
    for actor in ("system:scheduler", "agent:stira", "remote:x", "", None, "x" * 49):
        assert _principal({"actor": actor}) == "local:operator"
    service = ExternalService()
    (paused,) = _pause_then_shut_down(tmp_path, service, calls=[CALL])
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    body = {
        "action": "approve",
        "args_hash": paused["args_hash"],
        "resolver": "system",
        "approver": "system:heartbeat",
    }
    response = _app(host.broker).post(f"/api/hitl/{paused['id']}/decision", json=body)
    assert response.status_code == 202
    assert host.store.get(paused["id"]).approver == "local:operator"


def test_default_rules_keep_the_agent_off_its_own_admin_api(tmp_path):
    """The agent's own HTTP tools are the server-side path to the loopback API (the CSRF
    token only stops browsers). On a new install the only tool that can POST is Never
    and the GET-only fetch is Ask (a human sees the URL); GETs change nothing (above)."""
    from stiracore.tools.tool_config import ToolConfig

    config = ToolConfig.load(tmp_path / "tool-config.json")  # genuinely new install
    assert config.policy_of("http_request") == "never"
    assert config.policy_of("web_fetch") == "ask"
    assert config.policy_of("web_search") == "ask"


@pytest.fixture
def stira_app_stage_c(tmp_path, monkeypatch):
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
    gateway = tmp_path / "gw.json"
    gateway.write_text(json.dumps({"autonomy": {"stage_c": True}}), encoding="utf-8")
    store = ConfigStore(gateway, tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


def test_with_stage_c_the_grant_routes_are_classified_and_guarded(stira_app_stage_c):
    """ADR 0011 W4: with autonomy.stage_c the grant routes exist (and only they are new);
    every one of their mutations refuses a missing token, a cross-origin request and a
    foreign host before its view runs; their reads are same-origin only."""
    app = stira_app_stage_c
    assert set(_inventory(app)) == set(STIRA_ROUTES) | set(GRANT_ROUTES)
    assert _check_mutations(app, GRANT_ROUTES) == 7 * 7
    calls = _spy(app)
    client = app.test_client()
    reads = [r for (r, m), (_a, kind) in GRANT_ROUTES.items() if m == "GET"]
    for rule in reads:
        for name, headers in API_READ_ATTACKS.items():
            response = client.get(_url(rule), headers=headers)
            assert response.status_code == 403, (rule, name)
        assert client.get(_url(rule), headers={"Host": "evil.example"}).status_code == 403
    assert calls == {}


@pytest.fixture
def stira_app_stage_c_exec(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

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
    gateway = tmp_path / "gw.json"
    gateway.write_text(
        json.dumps({"autonomy": {"stage_c": True, "stage_c_exec": True}}), encoding="utf-8"
    )
    store = ConfigStore(gateway, tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


def test_with_the_executor_only_the_step_route_is_new_and_it_is_guarded(stira_app_stage_c_exec):
    """ADR 0011 W5: autonomy.stage_c_exec adds exactly POST /api/grants/<id>/step; it refuses
    a missing token, a cross-origin request and a foreign host before its view runs."""
    app = stira_app_stage_c_exec
    assert set(_inventory(app)) == set(STIRA_ROUTES) | set(GRANT_ROUTES) | set(EXEC_ROUTES)
    assert _check_mutations(app, EXEC_ROUTES) == 7
