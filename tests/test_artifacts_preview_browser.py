"""#454 (ADR 0020 §6): the HTML preview is isolated — measured in a real browser.

The real chat server app (offline) on a loopback port, the artifacts page opened in headless
Chrome (DevTools protocol), an artifact whose script tries everything it should not: read
or change the parent page, read its cookie or storage, fetch the dashboard API (the CSRF
token), reach another local server by fetch / image / script / WebSocket. The frame reports
what it saw to the parent with ``postMessage`` (the only channel it has).

Skipped when no browser or ``websockets`` is available, unless ``STIRA_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import ClassVar
from unittest.mock import Mock

import pytest

from stiracore.config.data_dir import build_policy
from stiracore.config.models import AgentConfig
from stiracore.observability.config_api import ConfigStore

REQUIRE = os.environ.get("STIRA_REQUIRE_BROWSER") == "1"


def _browser() -> str | None:
    from stiracore.tools.page_check import browser_status

    status = browser_status()
    return status.path if getattr(status, "available", False) else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Canary(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self):
        _Canary.hits.append(self.path)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


PROBE = """<!doctype html><html><body><p>probe</p><script>
const CANARY = 'http://127.0.0.1:__PORT__';
const r = {};
try { r.parent_dom = parent.document.title; } catch (e) { r.parent_dom = 'blocked'; }
try { parent.document.body.dataset.pwned = '1'; r.parent_write = 'open'; }
catch (e) { r.parent_write = 'blocked'; }
try { r.cookie = document.cookie; } catch (e) { r.cookie = 'blocked'; }
try { localStorage.setItem('k', 'v'); r.storage = 'open'; } catch (e) { r.storage = 'blocked'; }
try { r.origin = self.origin; } catch (e) { r.origin = 'error'; }
const img = new Image(); img.src = CANARY + '/img';
const s = document.createElement('script'); s.src = CANARY + '/script';
document.body.appendChild(s);
try { new WebSocket('ws://127.0.0.1:__PORT__/ws'); r.ws = 'created'; }
catch (e) { r.ws = 'blocked'; }
Promise.all([
  fetch(CANARY + '/fetch').then(() => 'open', () => 'blocked').then(v => { r.fetch_external = v; }),
  fetch('/api/csrf').then(res => res.text(), () => 'blocked').then(v => { r.fetch_api = v; }),
]).then(() => setTimeout(() => parent.postMessage(r, '*'), 300));
</script></body></html>
"""


class _Cdp:
    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        self.ws = connect(url, max_size=None, open_timeout=10)
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=15))
            if message.get("id") == ident:
                if "error" in message:
                    raise AssertionError(message["error"])
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


@pytest.fixture
def served_app(tmp_path, monkeypatch):
    from werkzeug.serving import make_server

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
    app = stira_server.create_app(config_store=store)
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, port, stira_server.DATA_DIR
    server.shutdown()
    thread.join(5)
    stira_server.shutdown_app(app)


def test_the_html_preview_cannot_reach_the_parent_its_data_or_the_network(served_app):
    chrome = _browser()
    try:
        import websockets.sync.client  # noqa: F401
    except ImportError:
        chrome = None
    if chrome is None:
        if REQUIRE:
            pytest.fail("STIRA_REQUIRE_BROWSER=1 but no browser / websockets")
        pytest.skip("no browser or websockets")
    app, port, data_dir = served_app
    canary_port = _free_port()
    _Canary.hits = []
    canary = http.server.ThreadingHTTPServer(("127.0.0.1", canary_port), _Canary)
    threading.Thread(target=canary.serve_forever, daemon=True).start()

    policy, _floor = build_policy(AgentConfig(), data_dir)
    (policy.workspace / "probe.html").write_text(
        PROBE.replace("__PORT__", str(canary_port)), encoding="utf-8"
    )
    ident = app.extensions["artifacts"].register(policy, "probe.html", turn_id="t1")["id"]
    token = app.config["STIRA_CSRF_TOKEN"]

    debug_port = _free_port()
    profile = tempfile.mkdtemp(prefix="stira-artifacts-")
    browser = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={debug_port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp = None
    try:
        deadline = time.monotonic() + 20
        page_ws = None
        while page_ws is None and time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=2) as r:
                    pages = [t for t in json.load(r) if t.get("type") == "page"]
                page_ws = pages[0]["webSocketDebuggerUrl"] if pages else None
            except OSError:
                time.sleep(0.2)
        assert page_ws, "browser did not start"
        cdp = _Cdp(page_ws)
        cdp.call("Page.enable")
        cdp.call("Network.enable")
        base = f"http://127.0.0.1:{port}"
        cdp.call("Network.setCookie", name="stira_probe", value="PARENT-COOKIE", url=base)
        cdp.call(
            "Page.addScriptToEvaluateOnNewDocument",
            source=(
                "window.__probe = []; window.addEventListener('message', "
                "e => window.__probe.push(e.data));"
            ),
        )
        cdp.call("Page.navigate", url=f"{base}/artifacts?id={ident}")
        seen = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = cdp.evaluate(
                "JSON.stringify((window.__probe || []).filter(m => m && m.parent_dom))"
            )
            found = json.loads(value) if value else []
            if found:
                seen = found[0]
                break
            time.sleep(0.25)
        assert seen is not None, "the preview frame never reported"
        time.sleep(1.0)  # let any late request arrive
        assert seen["parent_dom"] == "blocked" and seen["parent_write"] == "blocked"
        assert seen["cookie"] == "blocked" and seen["storage"] == "blocked"
        assert seen["origin"] == "null"
        assert seen["fetch_external"] == "blocked" and seen["fetch_api"] == "blocked"
        assert token not in json.dumps(seen)
        assert _Canary.hits == [], _Canary.hits
        # The parent page is untouched and still has its cookie.
        assert cdp.evaluate("document.body.dataset.pwned || ''") == ""
        assert "PARENT-COOKIE" in (cdp.evaluate("document.cookie") or "")
        frame = cdp.evaluate(
            "(() => { const f = document.querySelector('iframe.artifact-frame');"
            " return f && [f.getAttribute('sandbox'), f.getAttribute('src')]; })()"
        )
        assert frame == ["allow-scripts", f"/api/artifacts/{ident}/versions/1/raw"]
    finally:
        if cdp is not None:
            cdp.close()
        browser.terminate()
        try:
            browser.wait(10)
        except subprocess.TimeoutExpired:
            browser.kill()
        canary.shutdown()
        subprocess.run(["rm", "-rf", profile], check=False)
