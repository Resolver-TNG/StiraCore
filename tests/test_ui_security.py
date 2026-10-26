"""Tests for stiracore.observability.security (Issue #185)."""

import copy
import importlib.util
import tempfile
from pathlib import Path

import pytest
from flask import Flask, jsonify, render_template_string

from stiracore.core import NeuralState
from stiracore.observability.security import (
    install_security,
    mask_secrets,
    resolve_bind_host,
)

NO = {"X-Test-No-Csrf": "1"}


@pytest.fixture
def app():
    app = Flask(__name__)
    calls = {"act": 0, "flaky": 0}
    app.extensions["calls"] = calls

    @app.post("/api/act")
    def act():
        calls["act"] += 1
        return jsonify(ok=True)

    @app.post("/api/flaky")
    def flaky():
        calls["flaky"] += 1
        return (jsonify(error="fail"), 500) if calls["flaky"] == 1 else jsonify(ok=True)

    @app.post("/api/hook")
    def hook():
        return jsonify(ok=True)

    @app.get("/api/read")
    def read():
        calls["read"] = calls.get("read", 0) + 1
        return jsonify(ok=True)

    @app.get("/page")
    def page():
        return render_template_string('<meta name="csrf-token" content="{{ csrf_token() }}">')

    install_security(
        app, csrf_exempt_endpoints=("hook",), idempotent_endpoints=("act", "/api/flaky")
    )
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def token(app):
    return app.config["STIRA_CSRF_TOKEN"]


def test_csrf_missing_and_wrong(client):
    assert client.post("/api/act", headers=NO).status_code == 403
    bad = {"X-CSRF-Token": "nope"}
    assert client.post("/api/act", headers=bad).status_code == 403


def test_csrf_correct(app, client):
    assert client.post("/api/act", headers={"X-CSRF-Token": token(app)}).status_code == 200


def test_cross_origin_rejected_even_with_token(app, client):
    h = {"X-CSRF-Token": token(app), "Origin": "https://evil.test"}
    assert client.post("/api/act", headers=h).status_code == 403
    h = {"X-CSRF-Token": token(app), "Sec-Fetch-Site": "cross-site"}
    assert client.post("/api/act", headers=h).status_code == 403
    h = {"X-CSRF-Token": token(app), "Origin": "http://localhost"}
    assert client.post("/api/act", headers=h).status_code == 200


def test_host_rejected_for_get(client):
    assert client.get("/page", headers={"Host": "evil.test"}).status_code == 403
    assert client.get("/page", headers={"Host": "127.0.0.1:5000"}).status_code == 200


def test_exempt_endpoint(client):
    assert client.post("/api/hook", headers=NO).status_code == 200
    h = {**NO, "Origin": "https://evil.test"}
    assert client.post("/api/hook", headers=h).status_code == 403
    h = {**NO, "Host": "evil.test"}
    assert client.post("/api/hook", headers=h).status_code == 403


def test_csrf_endpoint_and_template(app, client):
    response = client.get("/api/csrf")
    assert response.json == {"token": token(app)}
    assert "Access-Control-Allow-Origin" not in response.headers
    assert token(app) in client.get("/page").get_data(as_text=True)


def test_security_headers(client):
    response = client.get("/api/csrf")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in client.get("/page").headers


def test_idempotency(app, client):
    calls = app.extensions["calls"]
    h = {"Idempotency-Key": "k1"}
    assert client.post("/api/act", headers=h).status_code == 200
    duplicate = client.post("/api/act", headers=h)
    assert duplicate.status_code == 409
    assert duplicate.json["code"] == "duplicate_request"
    # Says only what is known: received; maybe still running (#185 review).
    assert "already received" in duplicate.json["error"]
    assert "may still be running" in duplicate.json["error"]
    assert calls["act"] == 1
    client.post("/api/act")
    client.post("/api/act")
    assert calls["act"] == 3
    h = {"Idempotency-Key": "k2"}
    assert client.post("/api/flaky", headers=h).status_code == 500
    assert client.post("/api/flaky", headers=h).status_code == 200
    assert calls["flaky"] == 2


def test_idempotency_key_is_bound_to_its_body_and_scoped_to_the_path(app, client):
    """Same key + other body: a distinct 409 that runs nothing; the key is scoped to
    method and concrete path, so it never collides across routes or request ids."""
    calls = app.extensions["calls"]
    h = {"Idempotency-Key": "k-body"}
    assert client.post("/api/act", json={"m": "A"}, headers=h).status_code == 200
    reused = client.post("/api/act", json={"m": "B"}, headers=h)
    assert reused.status_code == 409 and reused.json["code"] == "idempotency_key_reused"
    replay = client.post("/api/act", json={"m": "A"}, headers=h)
    assert replay.status_code == 409 and replay.json["code"] == "duplicate_request"
    assert calls["act"] == 1
    # Another idempotent route with the same key is another request.
    assert client.post("/api/flaky", json={"m": "A"}, headers=h).status_code == 500
    assert calls["flaky"] == 1


def test_idempotency_scope_includes_the_concrete_path():
    app = Flask(__name__)
    ran = []

    @app.post("/api/items/<item>/decide")
    def decide(item):
        ran.append(item)
        return jsonify(ok=True)

    install_security(app, idempotent_endpoints=("/api/items/*/decide",))
    client = app.test_client()
    h = {"Idempotency-Key": "same"}
    assert client.post("/api/items/a/decide", json={}, headers=h).status_code == 200
    assert client.post("/api/items/b/decide", json={}, headers=h).status_code == 200
    assert client.post("/api/items/a/decide", json={}, headers=h).status_code == 409
    assert ran == ["a", "b"]


def test_idempotency_runs_after_blueprint_body_limits():
    """The body digest is taken after every before_request hook, so a blueprint that
    raises its own body limit (chat images) is honoured, not the app default."""
    from flask import Blueprint

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16
    bp = Blueprint("big", __name__)

    @bp.before_request
    def allow_big():
        from flask import request

        request.max_content_length = 4096

    @bp.post("/api/big")
    def big():
        return jsonify(ok=True)

    app.register_blueprint(bp)
    install_security(app, idempotent_endpoints=("/api/big",))
    response = app.test_client().post(
        "/api/big", data=b"x" * 1000, headers={"Idempotency-Key": "b1"}
    )
    assert response.status_code == 200


ORIGIN_CASES = {
    # (Origin, Host) -> allowed
    ("http://localhost", "localhost"): True,
    ("http://LOCALHOST", "localhost"): True,  # hostnames are case-insensitive
    ("http://localhost.", "localhost"): True,  # one trailing root dot
    ("http://localhost:80", "localhost"): True,  # effective port
    ("http://localhost:5555", "localhost:5555"): True,
    ("http://localhost", "localhost:5555"): False,  # 80 != 5555
    ("http://localhost:5556", "localhost:5555"): False,
    ("https://localhost:5555", "localhost:5555"): False,  # scheme differs from HTTP
    ("//localhost:5555", "localhost:5555"): False,  # malformed (scheme-relative)
    ("localhost:5555", "localhost:5555"): False,
    ("http://user@localhost:5555", "localhost:5555"): False,  # userinfo
    ("http://user:pw@localhost:5555", "localhost:5555"): False,
    ("http://localhost:5555/", "localhost:5555"): False,  # path
    ("http://localhost:5555?x", "localhost:5555"): False,
    ("http://localhost:5555#x", "localhost:5555"): False,
    ("http://localhost:abc", "localhost"): False,
    ("http://localhost:99999", "localhost"): False,
    ("ftp://localhost", "localhost"): False,
    ("null", "localhost"): False,
    ("", "localhost"): False,
    (" http://localhost", "localhost"): False,
    ("http://localhost.evil.example", "localhost"): False,
    ("http://evil.example", "localhost"): False,
    ("http://[::1]:5555", "[::1]:5555"): True,
    ("http://[::1", "[::1]:5555"): False,
    ("http://127.0.0.1:5555", "localhost:5555"): False,  # another origin, same machine
}


@pytest.mark.parametrize(("origin", "host"), list(ORIGIN_CASES))
def test_origin_is_matched_by_scheme_host_and_port(app, client, origin, host):
    calls = app.extensions["calls"]
    before = calls["act"]
    response = client.post("/api/act", headers={"Origin": origin, "Host": host})
    read = client.get("/api/read", headers={"Origin": origin, "Host": host})
    if ORIGIN_CASES[(origin, host)]:
        assert (response.status_code, read.status_code) == (200, 200)
        assert calls["act"] == before + 1
    else:
        assert (response.status_code, read.status_code) == (403, 403)
        assert calls["act"] == before


def test_no_origin_and_same_origin_fetch_metadata_still_pass(app, client):
    for headers in ({}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}):
        assert client.post("/api/act", headers=headers).status_code == 200, headers
        assert client.get("/api/read", headers=headers).status_code == 200, headers


def test_host_spoofing_is_refused_before_the_origin_check(app, client):
    for host in ("evil.example", "localhost@evil.example", "localhost.evil.example"):
        response = client.post("/api/act", headers={"Host": host, "Origin": f"http://{host}"})
        assert response.status_code == 403, host
        assert response.json == {"error": "Host not allowed."}
    # A trailing dot names the same host.
    assert client.get("/api/read", headers={"Host": "localhost."}).status_code == 200


def test_proxy_public_origin_is_trusted_only_when_configured(monkeypatch):
    """Behind a TLS proxy the browser's Origin is https://public while the request is
    plain HTTP: it is accepted only if STIRA_ALLOWED_ORIGINS names it exactly."""
    monkeypatch.setenv("STIRA_ALLOWED_HOSTS", "stira.example.test")
    headers = {"Host": "stira.example.test", "Origin": "https://stira.example.test"}
    for configured, expected in (
        ("", 403),
        ("https://stira.example.test:8443", 403),
        ("http://stira.example.test", 403),
        ("https://Stira.Example.Test/", 200),  # normalized; a trailing slash is tolerated
        ("not a url, https://stira.example.test", 200),  # malformed entries are ignored
    ):
        monkeypatch.setenv("STIRA_ALLOWED_ORIGINS", configured)
        plain = Flask(__name__)
        plain.add_url_rule("/api/x", "x", lambda: "ok", methods=["POST"])
        install_security(plain)
        client = plain.test_client()
        assert client.post("/api/x", headers=headers).status_code == expected, configured


def test_mask_secrets():
    original = {
        "api_key": "abc",
        "Authorization": "Bearer xyz",
        "nested": [{"password": "p", "note": "id AKIAABCDEFGHIJKLMNOP here"}],
        "msg": "use Bearer abc.def and sk-abcdefghijkl",
    }
    snapshot = copy.deepcopy(original)
    masked = mask_secrets(original)
    assert original == snapshot
    assert masked["api_key"] == "***"
    assert masked["Authorization"] == "***"
    assert masked["nested"][0]["password"] == "***"
    note = masked["nested"][0]["note"]
    assert "AKIAABCDEFGHIJKLMNOP" not in note
    assert note.startswith("id AKIA")
    assert "abc.def" not in masked["msg"]
    assert "Bearer ***" in masked["msg"]
    assert "sk-abcdefghijkl" not in masked["msg"]


def test_resolve_bind_host(monkeypatch):
    monkeypatch.delenv("STIRA_HOST", raising=False)
    monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    assert resolve_bind_host() == "127.0.0.1"
    monkeypatch.setenv("STIRA_HOST", "::1")
    assert resolve_bind_host() == "::1"
    monkeypatch.setenv("STIRA_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        resolve_bind_host()
    monkeypatch.setenv("STIRA_ALLOW_REMOTE", "1")
    assert resolve_bind_host() == "0.0.0.0"


def test_api_reads_and_streams_are_same_origin_only(app, client):
    """#185: a foreign Origin (cross-origin fetch/EventSource) or a browser-flagged
    cross-site request never reaches an API read; pages stay linkable."""
    calls = app.extensions["calls"]
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost:9999"},  # same site, other port: still another origin
        {"Origin": "null"},
        {"Origin": "http://[::1"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = client.get("/api/read", headers=headers)
        assert response.status_code == 403, headers
        assert response.json == {"error": "Cross-origin API reads are not allowed."}
        assert "Access-Control-Allow-Origin" not in response.headers
    assert calls.get("read", 0) == 0
    for headers in ({}, {"Origin": "http://localhost"}, {"Sec-Fetch-Site": "same-origin"}):
        assert client.get("/api/read", headers=headers).status_code == 200, headers
    assert calls["read"] == 3
    # A link from another site still opens a page (navigation, not an API read).
    assert client.get("/page", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_remote_bind_refusal_names_auth_and_tls_and_warns_when_allowed(monkeypatch, caplog):
    monkeypatch.setenv("STIRA_HOST", "192.0.2.10")
    monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("STIRA_ALLOWED_HOSTS", raising=False)
    with pytest.raises(SystemExit) as refused:
        resolve_bind_host()
    assert "authentication or TLS" in str(refused.value)
    for value in ("0", "true", "yes", ""):  # only the exact opt-in counts
        monkeypatch.setenv("STIRA_ALLOW_REMOTE", value)
        with pytest.raises(SystemExit):
            resolve_bind_host()
    monkeypatch.setenv("STIRA_ALLOW_REMOTE", "1")
    with caplog.at_level("WARNING", logger="stiracore.observability.security"):
        assert resolve_bind_host() == "192.0.2.10"
    assert "no built-in authentication or TLS" in caplog.text
    assert "STIRA_ALLOWED_HOSTS is empty" in caplog.text


def test_remote_bind_still_serves_only_allowed_host_names(monkeypatch):
    """Exposed with STIRA_ALLOW_REMOTE=1, a request is served only for a Host name the
    operator listed (e.g. the proxy's public name); every other Host is refused."""
    monkeypatch.delenv("STIRA_ALLOWED_HOSTS", raising=False)
    plain = Flask(__name__)
    plain.add_url_rule("/api/x", "x", lambda: "ok")
    install_security(plain)
    assert plain.test_client().get("/api/x", headers={"Host": "192.0.2.10"}).status_code == 403
    monkeypatch.setenv("STIRA_ALLOWED_HOSTS", "stira.example.test")
    listed = Flask(__name__)
    listed.add_url_rule("/api/x", "x", lambda: "ok")
    install_security(listed)
    client = listed.test_client()
    assert client.get("/api/x", headers={"Host": "stira.example.test"}).status_code == 200
    assert client.get("/api/x", headers={"Host": "192.0.2.10"}).status_code == 403


def test_stira_refuses_a_remote_bind_before_building_the_app(monkeypatch):
    """The refusal comes first: no app, data-dir lock, store or thread is created."""
    from dashboard import stira_server

    monkeypatch.setenv("STIRA_HOST", "0.0.0.0")
    monkeypatch.delenv("STIRA_ALLOW_REMOTE", raising=False)
    built = []
    with pytest.raises(SystemExit) as refused:
        stira_server.main(app_factory=lambda lc: built.append(lc))
    assert "non-loopback" in str(refused.value)
    assert built == []


def test_dashboard_stimulus_requires_csrf():
    path = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    spec = importlib.util.spec_from_file_location("stira_dashboard_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        NeuralState(stability=0.9).save(state)
        client = module.create_app(state).test_client()
        response = client.post("/api/stimulus", json={"stimulus": "joy"}, headers=NO)
        assert response.status_code == 403


def test_chat_busy_releases_its_key_so_the_same_message_can_be_resent():
    """A 200 stream that only says "busy" ran nothing: the key is released. A turn that
    did run keeps its key (a replay is refused)."""
    import threading
    from types import SimpleNamespace

    from stiracore.observability.chat_api import create_chat_blueprint

    lock = threading.Lock()
    ran = []
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(), runner=lambda m: ran.append(m) or "ok", turn_lock=lock
        )
    )
    install_security(app, idempotent_endpoints=("/api/chat",))
    client = app.test_client()
    h = {"Idempotency-Key": "chat-1"}
    lock.acquire()
    try:
        busy = client.post("/api/chat", json={"message": "A"}, headers=h).get_data(as_text=True)
        assert "event: error" in busy and ran == []
        again = client.post("/api/chat", json={"message": "A"}, headers=h)
        assert again.status_code == 200  # not 409: the busy attempt was released
        again.get_data()
    finally:
        lock.release()
    done = client.post("/api/chat", json={"message": "A"}, headers=h).get_data(as_text=True)
    assert ran == ["A"] and "event: turn" in done
    replay = client.post("/api/chat", json={"message": "A"}, headers=h)
    assert replay.status_code == 409 and replay.json["code"] == "duplicate_request"
    assert ran == ["A"]
