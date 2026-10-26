"""#601 (tools, misc): reproductions of the pre-release review findings, offline.

Each test failed on main before its fix (measured): C9 inline-code URL token, C10
fallback ANSI-in-key mask, C11 sent OAuth token echo, C16 page_check launcher under
dash, C24/C25 ``init-anchor`` with saved ``page_check_ports``, C26 body deadline (incl.
a body without Content-Length cut at the deadline, the whole ``_download`` incl. close,
and a response without ``shutdown`` refused).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stiracore.integrations import http, oauth
from stiracore.tools import integration_tools, page_check, tool_rules_cli
from stiracore.tools import tool_config as tc
from stiracore.tools.episodic_formatter import EpisodicFormatter, fallback_format
from stiracore.tools.tool_config import ToolConfig

# --- C9: a URL inside inline code comes back, no NUL token stays ---------------------


@pytest.mark.parametrize(
    "message",
    [
        "see `https://example.com/a` ok",
        "run `curl https://example.com/" + "a" * 50 + "/" + "b" * 40 + "/c -o x` now",
        "stray \x007\x00 nul and `https://example.com/b`",
    ],
)
def test_inline_code_url_is_restored_without_tokens(message):
    content = EpisodicFormatter().format(message, "ok").content
    assert "\x00" not in content
    assert "https://example.com/" in content


def test_inline_code_with_a_url_keeps_its_length_limit():
    """The limit counts the code as written (the URL inside it included), not its token."""
    code = "`curl https://example.com/" + "a" * 40 + " -o out.txt`"
    content = EpisodicFormatter(inline_code_chars=24, user_budget=400).format(code, "ok").content
    assert "\x00" not in content
    span = re.search(r"`[^`]*`", content).group(0)
    assert len(span) == 24 and span.endswith("\u2026`") and span.startswith("`curl https://")


# --- C10: the fallback strips ANSI before it masks -----------------------------------


def test_fallback_masks_a_secret_whose_key_holds_an_ansi_escape():
    summary = fallback_format("pass\x1b[31mword=hunter2", "x")
    assert "hunter2" not in summary.content
    assert "[REDACTED]" in summary.content
    # The regular path already did (same input, same result).
    assert "hunter2" not in EpisodicFormatter().format("pass\x1b[31mword=hunter2", "x").content


# --- C11: the token this request sent is masked even if the store changed ------------


def test_oauth_masks_the_sent_token_after_the_stored_one_changed(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    session, response = MagicMock(), MagicMock()
    response.status_code = 200
    response.iter_content.return_value = [b"echo: old-access new-access safe"]
    session.request.return_value.__enter__.return_value = response
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(http.requests, "Session", factory)
    monkeypatch.setattr(http, "system_resolver", lambda host, port: ["93.184.216.34"])
    config = oauth.OAuthConfig(
        "https://auth.example/authorize",
        "https://auth.example/token",
        "client",
        "https://app.example/callback",
        ("https://api.example",),
    )
    conn = oauth.GenericOAuthConnector("generic", config, token_dir=tmp_path)
    conn._save({"access_token": "old-access"})
    monkeypatch.setattr(integration_tools, "CONNECTORS", {"generic": conn})
    real_access = conn.access_token

    def access_then_replaced():
        value = real_access()
        conn._save({"access_token": "new-access"})  # e.g. a refresh in another request
        return value

    conn.access_token = access_then_replaced
    result = integration_tools.oauth_api_call("generic", "GET", "https://api.example/items")
    assert result["status"] == "success"
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer old-access"
    text = result["content"][0]["text"]
    assert "old-access" not in text and "new-access" not in text and "safe" in text


def test_oauth_an_empty_stored_value_does_not_garble_the_reply(monkeypatch, tmp_path):
    """``str.replace("", …)`` would put the mask between every character."""
    from unittest.mock import MagicMock

    session, response = MagicMock(), MagicMock()
    response.status_code = 200
    response.iter_content.return_value = [b"echo live-access safe"]
    session.request.return_value.__enter__.return_value = response
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(http.requests, "Session", factory)
    monkeypatch.setattr(http, "system_resolver", lambda host, port: ["93.184.216.34"])
    config = oauth.OAuthConfig(
        "https://auth.example/authorize",
        "https://auth.example/token",
        "client",
        "https://app.example/callback",
        ("https://api.example",),
    )
    conn = oauth.GenericOAuthConnector("generic", config, token_dir=tmp_path)
    conn._save({"access_token": "live-access", "scope_note": ""})
    monkeypatch.setattr(integration_tools, "CONNECTORS", {"generic": conn})
    result = integration_tools.oauth_api_call("generic", "GET", "https://api.example/items")
    assert result["status"] == "success"
    assert result["content"][0]["text"] == "echo [REDACTED] safe"


# --- C16: the browser launcher does not depend on /bin/sh's fd syntax ----------------

FAKE_BROWSER = textwrap.dedent(
    """\
    import json, os, sys
    data = b""
    while b"\\0" not in data:
        chunk = os.read(3, 4096)
        if not chunk:
            sys.exit(3)
        data += chunk
    message = json.loads(data.split(b"\\0", 1)[0])
    open_fds = sorted(int(n) for n in os.listdir("/dev/fd") if int(n) >= 10)
    reply = {"id": message["id"], "result": {"argv": sys.argv[1:], "high": open_fds}}
    os.write(4, json.dumps(reply).encode() + b"\\0")
    """
)


_TWO_DIGIT_FD = re.compile(r"\d+[<>]&\d{2,}|\d{2,}[<>]&")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only (page_check phase 1)")
@pytest.mark.parametrize("shell", ["simulated-dash", "real-dash"])
def test_browser_pipe_on_fd_3_4_even_when_sh_is_dash(tmp_path, monkeypatch, shell):
    if shell == "real-dash" and not os.path.exists("/bin/dash"):
        pytest.skip("no /bin/dash here (the simulated case still runs)")
    fake = tmp_path / "fake-browser"
    fake.write_text(f"#!{sys.executable}\n{FAKE_BROWSER}", encoding="utf-8")
    fake.chmod(0o700)
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        # Debian/Ubuntu: /bin/sh is dash, which refuses a two-digit fd in a redirection
        # ("Bad fd number", measured with /bin/dash). Every /bin/sh call behaves so here.
        if argv[0] == "/bin/sh":
            if shell == "real-dash":
                argv = ["/bin/dash", *argv[1:]]
            elif len(argv) > 2 and _TWO_DIGIT_FD.search(argv[2]):
                argv = ["/bin/sh", "-c", "echo 'Bad fd number' >&2; exit 2"]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(page_check.subprocess, "Popen", popen)
    browser = page_check._Browser(str(fake), ["--flag-a", "--flag-b"])
    try:
        sent = browser.send("Browser.getVersion")
        reply = browser.inbox.get(timeout=10)
        assert reply is not None, "the browser did not start or its pipe is not on fd 3/4"
        assert reply["id"] == sent
        assert reply["result"]["argv"] == ["--flag-a", "--flag-b"]
        # The launcher's copies of the pipe ends (>= 10) are closed before the exec.
        assert reply["result"]["high"] == []
    finally:
        browser.close()


# --- C24/C25: init-anchor digests the checked rules (with page_check_ports) ----------


@pytest.fixture
def _clean_latch():
    with tc._LATCH_LOCK:
        tc._LATCHED.clear()
    yield
    with tc._LATCH_LOCK:
        tc._LATCHED.clear()


def test_init_anchor_with_saved_ports_loads_unlocked(tmp_path_factory, monkeypatch, _clean_latch):
    data = tmp_path_factory.mktemp("data")
    anchor = tmp_path_factory.mktemp("anchor-outside-data-dir")
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    path = data / "tool-config.json"
    ToolConfig(mode="custom", enabled_tools=set(), page_check_ports=[8080], path=path).save()
    monkeypatch.setenv(tc.ANCHOR_ENV, str(anchor))
    generation = tool_rules_cli.init_anchor(Path(path))
    with tc._LATCH_LOCK:
        tc._LATCHED.clear()  # a new process
    loaded = ToolConfig.load(path)
    assert loaded.integrity_error is None
    assert loaded.page_check_ports == [8080] and loaded.generation == generation
    stored = json.loads(tc.anchor_path(path).read_text(encoding="utf-8"))
    assert stored["rules"].get("page_check_ports") == [8080]
    # The next save (one generation above the anchor) still checks out.
    loaded.save()
    with tc._LATCH_LOCK:
        tc._LATCHED.clear()
    assert ToolConfig.load(path).integrity_error is None


# --- C26: a server dripping bytes under the read timeout ends at the body deadline ----


def test_web_body_read_has_a_total_deadline(monkeypatch):
    import socket
    import threading
    import time

    import requests

    from stiracore.tools import web_tools

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def drip():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 9999\r\n\r\n")
        try:
            end = time.monotonic() + 4  # main: no deadline, the read ends when this closes
            while not stop.is_set() and time.monotonic() < end:  # never a read timeout
                conn.sendall(b"x")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    monkeypatch.setattr(web_tools, "DOWNLOAD_DEADLINE_S", 1.0, raising=False)
    started = time.monotonic()
    try:
        url = f"http://127.0.0.1:{port}/"
        with (
            requests.get(url, stream=True, timeout=(5, 2)) as response,
            pytest.raises(web_tools.FetchError) as caught,
        ):
            web_tools._read_limited(response, 1 << 20)
        assert caught.value.code == web_tools.FETCH_TIMEOUT
        assert time.monotonic() - started < 3
    finally:
        stop.set()
        server.close()
        thread.join(timeout=3)


def _hold_open_server(head: bytes, body: bytes, hold_s: float):
    """A loopback server: ``head`` + ``body``, then the connection stays open ``hold_s``."""
    import socket
    import threading
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def run():
        conn, _ = server.accept()
        conn.recv(4096)
        try:
            conn.sendall(head + body)
            end = time.monotonic() + hold_s
            while not stop.is_set() and time.monotonic() < end:
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return server, stop, thread


@pytest.mark.parametrize("body", [b"", b"x" * 16_384], ids=["no-body", "one-full-chunk"])
def test_web_deadline_without_content_length_is_a_timeout_not_a_short_body(monkeypatch, body):
    """No Content-Length: the body ends when the connection closes. At the deadline the
    read must fail, never return what came so far (an end of file caused by the deadline
    is not the end of the body)."""
    import time

    import requests

    from stiracore.tools import web_tools

    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
    server, stop, thread = _hold_open_server(head, body, hold_s=6)
    port = server.getsockname()[1]
    monkeypatch.setattr(web_tools, "DOWNLOAD_DEADLINE_S", 1.0, raising=False)
    started = time.monotonic()
    try:
        url = f"http://127.0.0.1:{port}/"
        with (
            requests.get(url, stream=True, timeout=(5, 10)) as response,
            pytest.raises(web_tools.FetchError) as caught,
        ):
            web_tools._read_limited(response, 1 << 20)
        assert caught.value.code == web_tools.FETCH_TIMEOUT
        assert time.monotonic() - started < 4
    finally:
        stop.set()
        server.close()
        thread.join(timeout=3)


def test_web_a_response_without_shutdown_is_refused_before_reading(monkeypatch):
    """urllib3 < 2.3 has no ``HTTPResponse.shutdown``: neither the read nor the caller's
    ``close`` could be stopped at the deadline, so the body is not read at all."""
    from stiracore.tools import web_tools

    read = []

    class _Raw:  # no ``shutdown`` attribute
        pass

    class _Old:
        raw = _Raw()

        def iter_content(self, chunk_size):
            read.append(chunk_size)
            yield b"body"

    with pytest.raises(web_tools.FetchError) as caught:
        web_tools._read_limited(_Old(), 1 << 20)
    assert caught.value.code == web_tools.FETCH_NETWORK
    assert read == []


def test_web_download_ends_at_the_deadline_including_close(monkeypatch):
    """The real path (``_download``: ``with session.get(..., stream=True)``) with the real
    read timeout: a server sends a byte every 0.2 s (under the 20 s read timeout) and never
    completes the next chunk. The call — the context exit's ``close`` included — ends at
    the deadline as ``network_timeout``."""
    import socket
    import threading
    import time

    from stiracore.tools import web_tools

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def drip():
        conn, _ = server.accept()
        conn.recv(4096)
        try:
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 999999\r\n\r\n"
            )
            while not stop.is_set():  # until the test ends, never 16 KiB
                conn.sendall(b"x")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    monkeypatch.setattr(web_tools, "DOWNLOAD_DEADLINE_S", 1.0, raising=False)
    monkeypatch.setattr(web_tools, "_validate_url", lambda url: None)  # loopback test server
    started = time.monotonic()
    try:
        with pytest.raises(web_tools.FetchError) as caught:
            web_tools._download(f"http://127.0.0.1:{port}/")
        elapsed = time.monotonic() - started
        assert caught.value.code == web_tools.FETCH_TIMEOUT
        assert elapsed < 3, elapsed
    finally:
        stop.set()
        server.close()
        thread.join(timeout=3)


def test_web_a_complete_body_within_the_deadline_is_returned():
    from unittest.mock import MagicMock

    from stiracore.tools import web_tools

    response = MagicMock()
    response.iter_content.return_value = [b"ab", b"cd"]
    assert bytes(web_tools._read_limited(response, 10)) == b"abcd"
    response.iter_content.return_value = [b"x" * 8, b"x" * 8]
    with pytest.raises(web_tools.FetchError) as caught:
        web_tools._read_limited(response, 10)
    assert caught.value.code == web_tools.FETCH_TOO_LARGE
