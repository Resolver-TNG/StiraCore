"""#160: a port that cannot be bound ends with a Stira line and exit 4; the start-up
smoke (scripts/smoke_server.py) is isolated and only passes for its own server."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX signals / process groups")
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from dashboard import stira_server


def _load_smoke():
    spec = importlib.util.spec_from_file_location(
        "smoke_server_under_test", ROOT / "scripts" / "smoke_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def _listening():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    return holder, holder.getsockname()[1]


# --- stira_server: the real bind ------------------------------------------------


def test_a_taken_port_is_reported_without_the_host() -> None:
    holder, port = _listening()
    with holder, pytest.raises(stira_server.PortUnavailableError) as caught:
        stira_server.listen_socket("127.0.0.1", port)
    message = str(caught.value)
    assert f"port {port} is already in use" in message
    assert "STIRA_PORT" in message
    assert "127.0.0.1" not in message


def test_a_free_port_is_bound_and_listening() -> None:
    holder, port = _listening()
    holder.close()
    sock = stira_server.listen_socket("127.0.0.1", port)
    try:
        assert sock.getsockname()[1] == port
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        sock.close()


def test_other_bind_failures_are_reported_too() -> None:
    with pytest.raises(stira_server.PortUnavailableError) as caught:
        stira_server.listen_socket("no-such-host.invalid", 5)
    assert "cannot listen on port 5" in str(caught.value)
    assert "no-such-host" not in str(caught.value)


ENTRY = """
import socket, sys
sys.path[:0] = [{src!r}, {root!r}]
from flask import Flask
from dashboard import stira_server

port = int(sys.argv[1])
mode = sys.argv[2]
app = Flask("entry-test")

@app.get("/ping")
def ping():
    return {{"ok": True}}

def factory(lifecycle):
    if mode == "race":
        # The port is taken after start-up began, before the server binds.
        factory.sock = socket.socket()
        factory.sock.bind(("127.0.0.1", port))
        factory.sock.listen(1)
    return app

stira_server.PORT = port
stira_server.main(app_factory=factory)
"""


def _entry(tmp_path: Path, port: int, mode: str) -> subprocess.Popen:
    path = tmp_path / "entry.py"
    path.write_text(ENTRY.format(src=str(ROOT / "src"), root=str(ROOT)), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STIRA_")}
    return subprocess.Popen(
        [sys.executable, str(path), str(port), mode],
        cwd=tmp_path,
        env=dict(env, PYTHONUTF8="1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_entry_point_exits_4_when_the_port_is_taken(tmp_path: Path) -> None:
    holder, port = _listening()
    with holder:
        process = _entry(tmp_path, port, "plain")
        _out, err = process.communicate(timeout=120)
    assert process.returncode == 4, err
    assert f"Stira: port {port} is already in use" in err
    assert "Running on" not in err


def test_entry_point_exits_4_when_the_port_is_taken_during_start_up(tmp_path: Path) -> None:
    holder, port = _listening()
    holder.close()
    process = _entry(tmp_path, port, "race")
    _out, err = process.communicate(timeout=120)
    assert process.returncode == 4, err
    assert f"Stira: port {port} is already in use" in err


@POSIX_ONLY
def test_entry_point_serves_and_stops_on_sigterm(tmp_path: Path) -> None:
    holder, port = _listening()
    holder.close()
    process = _entry(tmp_path, port, "plain")
    try:
        smoke = _load_smoke()
        deadline = time.monotonic() + 60
        code = 0
        while time.monotonic() < deadline and code != 200:
            code, _ = smoke.get_json(f"http://127.0.0.1:{port}/ping", 2)
            time.sleep(0.2)
        assert code == 200
    finally:
        process.send_signal(signal.SIGTERM)
        _out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err


# --- scripts/smoke_server.py --------------------------------------------------------

FAKE = """
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

data_dir, mode, record = sys.argv[1], sys.argv[2], sys.argv[3]
with open(os.path.join(record, "env.json"), "w") as fh:
    json.dump(sorted(os.environ), fh)
with open(os.path.join(record, "pid"), "w") as fh:
    fh.write(str(os.getpid()))
if mode == "exit3":
    for i in range(30):
        print(f"noise {i}", file=sys.stderr)
    print("Stira: refused, token=" + "abcdEFGH1234ijklMNOP5678qrst", file=sys.stderr)
    sys.exit(3)
if mode == "hang":
    time.sleep(600)
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if mode == "redirect" and self.path == "/api/config":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:__REDIRECT__/api/config")
            self.end_headers()
            return
        if self.path == "/api/config/hub":
            where = "/elsewhere" if mode == "foreign" else data_dir
            body = {"workspace": os.path.join(where, "workspace")}
        elif mode == "big" and self.path == "/api/config":
            body = {"pad": "x" * 5000}
        else:
            body = {"path": self.path}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(os.environ["STIRA_PORT"])), Handler).serve_forever()
"""


def _fake(tmp_path: Path, monkeypatch, mode: str, redirect_port: int = 0):
    smoke = _load_smoke()
    script = tmp_path / "fake.py"
    script.write_text(FAKE.replace("__REDIRECT__", str(redirect_port)), encoding="utf-8")
    record = tmp_path / "record"
    record.mkdir()
    monkeypatch.setattr(
        smoke,
        "child_command",
        lambda data_dir: [sys.executable, str(script), str(data_dir), mode, str(record)],
    )
    return smoke, record


def test_child_environment_is_an_allow_list(tmp_path: Path) -> None:
    smoke = _load_smoke()
    base = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/home/someone",
        "AWS_SECRET_ACCESS_KEY": "dummy-secret",
        "AWS_PROFILE": "someone",
        "GITHUB_TOKEN": "dummy-token",
        "STIRA_API_KEY_ENV": "OPENAI_API_KEY",
        "OPENAI_API_KEY": "dummy-key",
        "HTTPS_PROXY": "http://proxy.invalid:3128",
        "PYTHONPATH": "/somewhere",
    }
    env = smoke.child_env(base, port=5999, scratch=tmp_path)
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8"
    assert env["STIRA_PORT"] == "5999"
    assert env["HOME"] == str(tmp_path / "home")  # not the caller's home
    for name in ("AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "GITHUB_TOKEN", "OPENAI_API_KEY"):
        assert name not in env
    assert "STIRA_API_KEY_ENV" not in env and "HTTPS_PROXY" not in env
    assert "PYTHONPATH" not in env
    assert "dummy" not in json.dumps(env)


def test_urls_other_than_loopback_http_are_refused() -> None:
    smoke = _load_smoke()
    for url in (
        "http://example.com/api/config",
        "http://localhost:5000/api/config",
        "https://127.0.0.1:5000/api/config",
        "file:///etc/passwd",
        "http://127.0.0.1/api/config",
    ):
        with pytest.raises(ValueError):
            smoke.get_json(url, 1)


@POSIX_ONLY
def test_pass_needs_identity_and_a_clean_stop_and_hides_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMOKE_DUMMY_SECRET", "dummy-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy-secret-value")
    smoke, record = _fake(tmp_path, monkeypatch, "ok")
    result = smoke.run_smoke(timeout=30)
    assert result.passed, result
    assert result.identified and result.exit_code == 0 and not result.forced
    assert result.removed and not result.scratch.exists()
    seen = json.loads((record / "env.json").read_text())
    assert "SMOKE_DUMMY_SECRET" not in seen and "AWS_SECRET_ACCESS_KEY" not in seen


@POSIX_ONLY
def test_a_server_that_is_not_ours_fails(tmp_path, monkeypatch) -> None:
    smoke, _ = _fake(tmp_path, monkeypatch, "foreign")
    result = smoke.run_smoke(timeout=3)
    assert not result.passed and not result.identified
    assert result.status["/api/config"] == 200  # a 200 alone is not enough
    assert result.removed


@POSIX_ONLY
def test_redirects_are_not_followed(tmp_path, monkeypatch) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)
    Thread(target=target.serve_forever, daemon=True).start()
    try:
        smoke, _ = _fake(tmp_path, monkeypatch, "redirect", target.server_address[1])
        result = smoke.run_smoke(timeout=3)
    finally:
        target.shutdown()
        target.server_close()
    assert not result.passed
    assert result.status["/api/config"] == 302
    assert hits == []


@POSIX_ONLY
def test_bodies_over_the_limit_fail(tmp_path, monkeypatch) -> None:
    smoke, _ = _fake(tmp_path, monkeypatch, "big")
    monkeypatch.setattr(smoke, "MAX_BODY", 1000)
    result = smoke.run_smoke(timeout=3)
    assert not result.passed and result.status["/api/config"] == 0


@POSIX_ONLY
def test_a_forced_kill_is_a_failure(tmp_path, monkeypatch) -> None:
    smoke, _ = _fake(tmp_path, monkeypatch, "ignore-term")
    monkeypatch.setattr(smoke, "STOP_TIMEOUT", 1.0)
    result = smoke.run_smoke(timeout=30)
    assert all(code == 200 for code in result.status.values()) and result.identified
    assert result.forced and not result.passed
    assert result.removed


@POSIX_ONLY
def test_failure_shows_a_short_masked_stderr_tail(tmp_path, monkeypatch, capsys) -> None:
    smoke, _ = _fake(tmp_path, monkeypatch, "exit3")
    assert smoke.main(["--timeout", "10"]) == 1
    captured = capsys.readouterr()
    assert "smoke: FAIL" in captured.out and "exit 3" in captured.out
    tail = captured.err.split("masked) ---\n", 1)[1]
    assert len(tail.strip().splitlines()) == smoke.TAIL_LINES
    assert "Stira: refused" in tail
    assert "abcdEFGH1234ijklMNOP5678qrst" not in tail
    assert "noise 0" not in tail


@POSIX_ONLY
def test_killing_the_smoke_still_stops_the_server_and_cleans_up(tmp_path: Path) -> None:
    script = tmp_path / "fake.py"
    script.write_text(FAKE, encoding="utf-8")
    record = tmp_path / "record"
    record.mkdir()
    scratch_root = tmp_path / "t"
    scratch_root.mkdir()
    driver = textwrap.dedent(
        f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("s", {str(ROOT / "scripts" / "smoke_server.py")!r})
        s = importlib.util.module_from_spec(spec); sys.modules["s"] = s
        spec.loader.exec_module(s)
        s.child_command = lambda d: [sys.executable, {str(script)!r}, str(d), "hang", {str(record)!r}]
        s.run_smoke(timeout=120)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", driver], env=dict(os.environ, TMPDIR=str(scratch_root))
    )
    pid_file = record / "pid"
    deadline = time.monotonic() + 60
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    child = int(pid_file.read_text())
    assert list(scratch_root.iterdir())  # the scratch directory exists while running
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=30)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert list(scratch_root.iterdir()) == []


@POSIX_ONLY
def test_real_server_smoke_passes_without_touching_the_checkout() -> None:
    local = ROOT / ".local"
    before = sorted(p.name for p in local.iterdir()) if local.exists() else None
    result = _load_smoke().run_smoke(timeout=120)
    assert result.passed, result.stderr_tail
    after = sorted(p.name for p in local.iterdir()) if local.exists() else None
    assert after == before
