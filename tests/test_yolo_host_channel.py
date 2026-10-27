"""ADR 0012 §8b (phase 2): the host channel — the Unix socket with kernel peer checks, the
host command on a TTY, the fixed text, and the desktop dialog (never shown in tests: the
runner is a fake; ``conftest.py`` also makes every real ``osascript`` call a "no").
"""

from __future__ import annotations

import json
import os
import secrets
import selectors
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.yolo import host_channel, host_cli, host_dialog
from stiracore.yolo.host_channel import (
    HostChannelServer,
    HostDesk,
    PeerRefused,
    check_peer,
    choose_channel,
    descends_from,
    describe,
    read_descriptor,
)
from stiracore.yolo.host_ops import HostOperations
from stiracore.yolo.requests import YoloRequests
from tests.test_yolo_host_ops import SUMMARY, B, Clocks, G

ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="the host channel is POSIX only"
)
NOT_IN_ANY_TREE = 4_000_000  # a pid that is no ancestor of anything here


def _desk(clock=None):
    clock = clock or Clocks()
    incarnation = secrets.token_hex(16)
    ops = HostOperations(incarnation=incarnation, clock=clock)
    yolo = YoloRequests(
        allowance="external",
        incarnation=incarnation,
        record=lambda *a, **k: "op",
        actor="local:tester",
        clocks_supported=True,
        clock=clock,
    )
    return HostDesk(ops, yolo)


@pytest.fixture
def channel(tmp_path):
    servers = []

    def make(**kw):
        desk = _desk()
        server = HostChannelServer(desk, tmp_path, **kw)
        servers.append(server)
        return desk, server

    yield make
    for server in servers:
        server.close()


def _client(socket_path, message):
    """``ask`` from a separate process (a child of this test process)."""
    code = (
        "import json, sys\n"
        "from stiracore.yolo.host_channel import ask\n"
        "print(json.dumps(ask(sys.argv[1], json.loads(sys.argv[2]))))\n"
    )
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
    done = subprocess.run(
        [sys.executable, "-c", code, str(socket_path), json.dumps(message)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


# --- peer checks ----------------------------------------------------------------------


def test_peer_checks_are_kernel_integers_and_refuse_the_server_tree():
    tree = {500: 400, 400: 300, 300: 1, 700: 1}
    parent = tree.get
    check_peer(700, 501, own_pid=300, own_uid=501, parent=parent)  # unrelated: accepted
    for pid in (300, 400, 500):  # the server and its descendants
        with pytest.raises(PeerRefused):
            check_peer(pid, 501, own_pid=300, own_uid=501, parent=parent)
    with pytest.raises(PeerRefused):
        check_peer(700, 502, own_pid=300, own_uid=501, parent=parent)  # another OS user
    with pytest.raises(PeerRefused):
        check_peer(800, 501, own_pid=300, own_uid=501, parent=parent)  # unknown: fail closed
    for bad in (True, 7.0, "700", 0, -1):
        with pytest.raises(PeerRefused):
            check_peer(bad, 501, own_pid=300, own_uid=501, parent=parent)
    assert descends_from(os.getpid(), os.getpid())
    assert not descends_from(os.getpid(), NOT_IN_ANY_TREE)


@posix_only
def test_a_connection_from_the_server_process_itself_is_refused(channel):
    _, server = channel()
    assert host_channel.ask(server.path, {"cmd": "pending"}) == {
        "ok": False,
        "code": "host_peer_refused",
    }


@posix_only
def test_a_child_of_the_server_process_is_refused(channel):
    _, server = channel()  # own pid = this process: the client below is its child
    assert _client(server.path, {"cmd": "pending"})["code"] == "host_peer_refused"


@posix_only
def test_a_process_outside_the_tree_lists_shows_and_decides(channel):
    desk, server = channel(own_pid=NOT_IN_ANY_TREE)
    op = desk.operations.request("rules_loosen", binding=B, generation=G, summary=SUMMARY)
    listed = _client(server.path, {"cmd": "pending"})
    assert listed["ok"] and [i["id"] for i in listed["items"]] == [op["id"]]
    shown = _client(server.path, {"cmd": "show", "id": op["short"]})["item"]
    assert shown["incarnation"] == desk.incarnation
    wrong = _client(
        server.path,
        {"cmd": "decide", "id": op["id"], "incarnation": "0" * 32, "decision": "confirm"},
    )
    assert wrong["code"] == "host_operation_incarnation"
    done = _client(
        server.path,
        {"cmd": "decide", "id": op["id"], "incarnation": desk.incarnation, "decision": "confirm"},
    )
    assert done["ok"] and done["item"]["state"] == "confirmed"
    replay = _client(
        server.path,
        {"cmd": "decide", "id": op["id"], "incarnation": desk.incarnation, "decision": "confirm"},
    )
    assert replay["code"] == "host_operation_decided" and replay["state"] == "confirmed"
    assert desk.operations.status(op["id"])["state"] == "confirmed"


@posix_only
def test_the_dialog_channel_refuses_decisions_by_command(channel):
    desk, server = channel(own_pid=NOT_IN_ANY_TREE, decisions=False)
    op = desk.operations.request("rules_loosen", binding=B, generation=G, summary=SUMMARY)
    answer = _client(
        server.path,
        {"cmd": "decide", "id": op["id"], "incarnation": desk.incarnation, "decision": "confirm"},
    )
    assert answer["code"] == "host_channel_dialog"
    assert desk.operations.status(op["id"])["state"] == "pending"


@posix_only
def test_socket_and_descriptor_modes_and_symlink_refusal(channel, tmp_path):
    _, server = channel()
    assert oct(os.stat(server.path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(server.path.parent).st_mode & 0o777) == "0o700"
    assert read_descriptor(tmp_path) == server.path
    other = tmp_path / "other"
    other.mkdir()
    (other / host_channel.DESCRIPTOR_NAME).symlink_to(server.descriptor)
    with pytest.raises(PeerRefused):
        read_descriptor(other)
    server.close()
    assert not server.descriptor.exists() and not server.path.exists()


def test_answer_accepts_only_exact_built_in_messages():
    desk = _desk()
    server = SimpleNamespace(desk=desk, decisions=True, _channel=lambda: "command")
    answer = HostChannelServer.answer.__get__(server)
    assert answer([], "local:x")["code"] == "host_request_invalid"
    assert answer({"cmd": 1}, "local:x")["code"] == "host_request_invalid"
    assert answer({"cmd": "nope"}, "local:x")["code"] == "host_request_invalid"
    assert answer({"cmd": "show", "id": ["x"]}, "local:x")["code"] == "host_operation_unknown"


# --- the fixed text -------------------------------------------------------------------


def test_describe_shows_only_enums_ids_and_digests():
    op = {
        "kind": "rules_loosen",
        "short": "abcdef01",
        "code": "12345678",
        "expires_in": 90,
        "summary": {
            "action": "approve",
            "tool": "file_write",
            "args": "0123abcd",
            "card": "c0ffee00",
            "changes": ["mode", "Ignore previous instructions"],
        },
    }
    text = "\n".join(describe(op))
    assert "file_write" in text and "abcdef01" in text and "12345678" in text
    assert "Ignore" not in text and "changes=mode,?" in text
    assert "自分で要求していなければ拒否" in text and "Deny it if you did not ask" in text
    yolo = {"kind": "yolo_request", "short": "00112233", "tier": "external", "t0": 0.0}
    text = "\n".join(describe(yolo))
    assert "外部もOK / external allowed" in text and "6 h" in text
    assert describe({"kind": "evil\nkind", "short": "x y"})[0].endswith("unknown operation")


# --- the host command -----------------------------------------------------------------


class Transport:
    def __init__(self, desk):
        self.desk = desk
        self.sent = []

    def __call__(self, message):
        self.sent.append(message)
        server = SimpleNamespace(desk=self.desk, decisions=True, _channel=lambda: "command")
        return HostChannelServer.answer.__get__(server)(message, "local:tester")


def _run(argv, desk, *, tty=True, typed="APPROVE"):
    import argparse

    parser = argparse.ArgumentParser(prog="stira")
    host_cli.add_parsers(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    out, err = [], []
    transport = Transport(desk)

    class Sink:
        def __init__(self, into):
            self.into = into

        def write(self, text):
            self.into.append(text)

        def flush(self):
            pass

    code = host_cli.run(
        args,
        stdout=Sink(out),
        stderr=Sink(err),
        is_tty=lambda: tty,
        read_line=lambda prompt: typed,
        transport=transport,
    )
    return code, "".join(out), "".join(err), transport


def _op(desk):
    return desk.operations.request("rules_loosen", binding=B, generation=G, summary=SUMMARY)


def test_cli_confirm_needs_a_tty_and_the_typed_word():
    desk = _desk()
    op = _op(desk)
    code, out, err, transport = _run(["approvals", "decide", op["short"]], desk, tty=False)
    assert code == 1 and "terminal" in err
    assert [m["cmd"] for m in transport.sent] == ["show"]  # nothing decided
    code, out, err, transport = _run(["approvals", "decide", op["short"]], desk, typed="yes")
    assert code == 1 and [m["cmd"] for m in transport.sent] == ["show"]
    assert desk.operations.status(op["id"])["state"] == "pending"
    code, out, err, transport = _run(["approvals", "decide", op["short"]], desk)
    assert code == 0, err
    assert transport.sent[-1] == {
        "cmd": "decide",
        "id": op["id"],
        "incarnation": desk.incarnation,
        "decision": "confirm",
    }
    assert desk.operations.status(op["id"])["state"] == "confirmed"
    assert "file_write" in out and op["short"] in out


def test_cli_deny_needs_no_tty_and_kinds_are_not_mixed():
    desk = _desk()
    op = _op(desk)
    code, out, err, _ = _run(["yolo", "confirm", op["short"]], desk)
    assert code == 1 and "stira approvals decide" in err
    code, out, err, _ = _run(["approvals", "decide", op["short"], "--deny"], desk, tty=False)
    assert code == 0 and desk.operations.status(op["id"])["state"] == "denied"
    request = desk.yolo.press("internal")
    code, out, err, _ = _run(["yolo", "confirm", request["short"]], desk, typed="YOLO")
    # No ledger or marker in this desk: the issuance fails closed (nothing opens).
    assert code == 0 and "confirmed and recorded" in out
    assert desk.yolo.machine.window is None


def test_cli_without_a_server_says_so(tmp_path, capsys):
    from stiracore.diagnostics.__main__ import main

    code = main(["approvals", "pending", "--data-dir", str(tmp_path)])
    assert code == 2
    assert "no running server" in capsys.readouterr().err


@posix_only
def test_the_real_command_on_a_pty_confirms_end_to_end(channel, tmp_path):
    """``stira approvals decide`` in its own process, on a pseudo-terminal, typing the word."""
    import pty  # POSIX only (termios): imported here so the module collects on Windows

    desk, _server = channel(own_pid=NOT_IN_ANY_TREE)
    op = _op(desk)
    master, slave = pty.openpty()
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "stiracore.diagnostics",
            "approvals",
            "decide",
            op["short"],
            "--data-dir",
            str(tmp_path),
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    seen = b""
    deadline = time.time() + 60
    watch = selectors.DefaultSelector()  # not select(): a full run has fds above 1024
    watch.register(master, selectors.EVENT_READ)
    try:
        while b"APPROVE to confirm" not in seen and time.time() < deadline:
            if watch.select(timeout=1):
                seen += os.read(master, 4096)
        assert b"APPROVE to confirm" in seen, seen[-500:]
        os.write(master, b"APPROVE\n")
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        watch.close()
        os.close(master)
    assert desk.operations.status(op["id"])["state"] == "confirmed"


# --- the desktop dialog (fake runner only) --------------------------------------------


def test_choose_channel():
    assert choose_channel("auto", gui=True, dialog_ready=True) == "dialog"
    assert choose_channel("auto", gui=True, dialog_ready=False) == "command"
    assert choose_channel("auto", gui=False, dialog_ready=True) == "command"
    assert choose_channel("command", gui=True, dialog_ready=True) == "command"
    assert choose_channel("dialog", gui=False, dialog_ready=True) == "command"
    assert host_channel.gui_session({"SSH_CONNECTION": "x"}, "darwin") is False
    assert host_channel.gui_session({}, "linux") is False


@pytest.mark.parametrize(
    ("setting", "gui", "ready", "effective", "warned"),
    [
        ("browser", True, True, "browser", True),
        ("command", True, True, "command", True),
        ("auto", False, True, "command", True),  # no desktop session: the command
        ("dialog", True, False, "command", True),  # dialog asked for, not available
        ("dialog", True, True, "dialog", False),  # the OS-authenticated dialog in use
        ("auto", True, True, "dialog", False),
    ],
)
def test_the_exec_warning_names_the_effective_channel_not_the_setting(
    tmp_path, monkeypatch, caplog, setting, gui, ready, effective, warned
):
    """Review of #432 (minor 2): only the dialog that was **actually selected** cannot be
    answered by an approved command; a ``dialog`` setting that fell back is warned about."""
    from stiracore.yolo import runtime
    from stiracore.yolo.allowance import YoloSettings

    monkeypatch.setenv("STIRA_ENABLE_EXEC_TOOLS", "1")
    monkeypatch.setattr(runtime, "gui_session", lambda: gui)
    monkeypatch.setattr(runtime.host_dialog, "dialog_ready", lambda runner: ready)
    runtime._reset_for_tests(YoloSettings("internal", host_confirm=setting))
    try:
        with caplog.at_level("WARNING", logger=runtime.__name__):
            desk = runtime.install_host(data_dir=tmp_path, record=None)
        assert desk.channel == effective
        lines = [r.getMessage() for r in caplog.records if "exec tools are enabled" in r.message]
        if warned:
            assert len(lines) == 1, lines
            assert f"channel in use is {effective!r}" in lines[0]
            assert f"STIRA_YOLO_HOST_CONFIRM={setting!r}" in lines[0]
        else:
            assert lines == []
    finally:
        runtime._reset_for_tests()


def test_no_exec_warning_without_exec_tools(tmp_path, monkeypatch, caplog):
    from stiracore.yolo import runtime
    from stiracore.yolo.allowance import YoloSettings

    monkeypatch.delenv("STIRA_ENABLE_EXEC_TOOLS", raising=False)
    runtime._reset_for_tests(YoloSettings("internal"))
    try:
        with caplog.at_level("WARNING", logger=runtime.__name__):
            runtime.install_host(data_dir=tmp_path, record=None)
        assert not [r for r in caplog.records if "exec tools are enabled" in r.message]
    finally:
        runtime._reset_for_tests()


def test_dialog_ready_runs_only_the_check_script():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return 0, "can"

    assert host_dialog.dialog_ready(runner, platform="darwin") is True
    assert calls == [["-e", host_dialog.CHECK_SCRIPT]]
    assert host_dialog.dialog_ready(runner, platform="linux") is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("answer", "decided"),
    [
        ((0, "confirm"), "confirmed"),
        ((0, "deny"), "denied"),
        ((0, "timeout"), "denied"),
        ((1, "confirm"), "denied"),
        ((0, ""), "denied"),
    ],
)
def test_the_dialog_answer_decides_and_anything_but_confirm_denies(answer, decided):
    desk = _desk()
    seen = []

    def runner(argv, timeout):
        seen.append((argv, timeout))
        return answer

    dialog = host_dialog.DialogConfirmer(
        desk.decide,
        incarnation=desk.incarnation,
        principal="local:tester",
        runner=runner,
        background=False,
    )
    op = _op(desk)
    dialog(op)
    assert desk.operations.status(op["id"])["state"] == decided
    argv, timeout = seen[0]
    assert argv[0] == "-e" and argv[1] == host_dialog.DIALOG_SCRIPT
    assert argv[2] == "\n".join(describe(op)) and argv[3] == str(op["expires_in"])
    assert timeout == op["expires_in"] + 15.0


def test_the_dialog_script_takes_the_text_only_as_an_argument():
    assert "argv[0]" in host_dialog.DIALOG_SCRIPT
    assert "evaluatePolicyLocalizedReasonReply" in host_dialog.DIALOG_SCRIPT
    assert "{" not in describe(_op(_desk()))[0]  # nothing that looks like code


def test_in_the_app_a_dialog_channel_asks_the_runner_and_never_a_real_dialog(
    tmp_path, monkeypatch, _no_yolo_host_dialog
):
    from stiracore.yolo import runtime
    from stiracore.yolo.allowance import YoloSettings

    runtime._reset_for_tests(YoloSettings("internal"))
    try:
        desk = runtime.install_host(data_dir=tmp_path, record=None, channel="dialog")
        dialog = desk.operations._on_created
        assert dialog is not None
        op = desk.operations.request("rules_loosen", binding=B, generation=G, summary=SUMMARY)
        deadline = time.time() + 5
        while desk.operations.status(op["id"])["state"] == "pending" and time.time() < deadline:
            time.sleep(0.01)
        # The conftest fake answered "no": a denial; it was asked with the dialog script.
        assert desk.operations.status(op["id"])["state"] == "denied"
        assert any(host_dialog.DIALOG_SCRIPT in call for call in _no_yolo_host_dialog)
    finally:
        runtime._reset_for_tests()


def _raw(socket_path, data: bytes) -> dict | None:
    """One raw request from this process (accepted when ``own_pid`` is outside its tree)."""
    import socket as _socket

    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(socket_path))
        sock.sendall(data)
        reply = b""
        while b"\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    return json.loads(reply.split(b"\n", 1)[0]) if reply else None


@posix_only
def test_a_deeply_nested_request_is_refused_and_the_server_keeps_serving(channel):
    """#601 A6: JSON nested past the decoder's recursion limit is an invalid request (the
    one accept thread survives)."""
    _, server = channel(own_pid=NOT_IN_ANY_TREE)
    assert _raw(server.path, b"[" * 5000 + b"\n") == {"ok": False, "code": "host_request_invalid"}
    assert _raw(server.path, b'{"cmd": "pending"}\n')["ok"] is True
    assert server._thread.is_alive()


@posix_only
def test_an_unexpected_error_in_one_request_never_stops_the_server(channel, monkeypatch):
    """#601 A5: an exception other than ``HostOpRefused`` answers that request with a fixed
    refusal (never its text); the next request is served."""
    desk, server = channel(own_pid=NOT_IN_ANY_TREE)

    def broken(_prefix):
        raise KeyError("secret-detail")

    monkeypatch.setattr(desk, "show", broken)
    reply = _raw(server.path, b'{"cmd": "show", "id": "abcdefgh"}\n')
    assert reply == {"ok": False, "code": "host_request_invalid"}
    assert _raw(server.path, b'{"cmd": "pending"}\n')["ok"] is True
    assert server._thread.is_alive()


@posix_only
def test_handle_itself_answers_a_deeply_nested_request(channel, caplog):
    """#601 A6 on its own: ``_handle`` (not the per-connection guard of ``_serve``) turns
    nesting past the decoder's limit into ``host_request_invalid`` and raises nothing."""
    import logging
    import socket as _socket

    _, server = channel(own_pid=NOT_IN_ANY_TREE)
    ours, theirs = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    with ours, theirs, caplog.at_level(logging.WARNING, logger=host_channel.__name__):
        theirs.sendall(b"[" * 5000 + b"\n")
        server._handle(ours)  # raises nothing
        theirs.settimeout(5)
        reply = theirs.recv(4096)
    assert json.loads(reply.split(b"\n", 1)[0]) == {"ok": False, "code": "host_request_invalid"}
    assert "request failed" not in caplog.text  # the outer guard was not needed


@posix_only
def test_a_peer_that_closes_before_the_refusal_never_stops_the_server(channel, monkeypatch):
    """#601 A5 (review M1): the request fails and its peer has already gone — the refusal
    cannot be sent (``_reply`` swallows ``OSError``) and the next request is served."""
    import socket as _socket

    desk, server = channel(own_pid=NOT_IN_ANY_TREE)
    entered = []

    def broken(_prefix):
        entered.append(True)
        time.sleep(0.3)  # the peer closes meanwhile
        raise KeyError("secret-detail")

    monkeypatch.setattr(desk, "show", broken)
    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
        sock.connect(str(server.path))
        sock.sendall(b'{"cmd": "show", "id": "abcdefgh"}\n')
        deadline = time.monotonic() + 5
        while not entered and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered  # closed below, while the request is still failing
    time.sleep(0.5)
    assert _raw(server.path, b'{"cmd": "pending"}\n')["ok"] is True
    assert server._thread.is_alive()
    ours, theirs = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    theirs.close()
    with ours:
        host_channel._reply(ours, {"ok": False, "code": "host_request_invalid"})  # no raise
