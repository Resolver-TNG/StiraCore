"""The host channel: where a person confirms a host-confirmed operation (ADR 0012 §8b).

Revision 6: the **default** confirmation is in the browser (``host_ops`` /
``requests``: a one-time token and a checkbox, phase 2c); the two host ways below are opt-in —
set ``STIRA_YOLO_HOST_CONFIRM`` and the host is required (the browser confirmation is then
refused). Chosen once at start-up (:func:`choose_channel`):

* **command** (Open (12) (iv)) — ``stira yolo confirm`` / ``stira approvals decide`` on a
  TTY of the host. The server listens on a Unix socket in a fresh ``0700`` directory
  (the socket ``0600``); its path is in ``yolo-host-channel.json`` in the data directory
  (on the agent's read and write floors). Every connection is checked with the
  **kernel's** peer credentials (``LOCAL_PEERCRED`` / ``LOCAL_PEERPID`` on macOS,
  ``SO_PEERCRED`` on Linux) — integers from the kernel, nothing the client sends:
  another OS user, **the server process itself** (every in-process agent tool, MCP client
  or HTTP library) and **its descendants** (stdio MCP servers, exec children that stayed
  in the tree) are refused (``host_peer_refused``). The principal is the peer's account
  (``local:<account>``), never a value from the request.
* **dialog** (Open (12) (ii)) — on the macOS desktop of the server's user: the server
  shows a native dialog with the fixed text of :func:`describe` and, on "Confirm", an OS
  authentication (LocalAuthentication: Touch ID or the account password), see
  :mod:`.host_dialog`. The command channel then lists and shows, but refuses decisions.

Residual (§8a): a process of the same OS user outside the server's tree (a shell, a
program that left the tree) can connect to the socket like the host command does; only
the dialog's OS authentication, or another OS user (#318), holds against it.

:func:`describe` is the one fixed format a person reads: enums, short ids and digests
only — never a tool argument, a job text or anything the agent wrote.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .host_ops import HostOperations, HostOpRefused
from .requests import WINDOW_HOURS, YoloRequests, wall_clock_text

try:  # POSIX only; the host channel is not offered elsewhere
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNELS",
    "DESCRIPTOR_NAME",
    "HostChannelServer",
    "HostDesk",
    "PeerRefused",
    "choose_channel",
    "describe",
    "gui_session",
    "peer_credentials",
    "principal_for",
]

CHANNELS = ("command", "dialog")
DESCRIPTOR_NAME = "yolo-host-channel.json"
_MAX_LINE = 8192
_HEX32 = re.compile(r"[0-9a-f]{32}")
_ACCOUNT = re.compile(r"[A-Za-z0-9._-]{1,48}")
# macOS <sys/un.h>: SOL_LOCAL, LOCAL_PEERCRED, LOCAL_PEERPID.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_LOCAL_PEERPID = 0x002


class HostChannelUnsupported(OSError):
    """The command channel needs POSIX (AF_UNIX, O_NOFOLLOW, peer credentials, uid).

    On Windows the server is refused before anything is created; the caller then marks
    the desk unavailable (fail-closed: host-confirmed operations are refused). #540
    """


def _platform_supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(socket, "AF_UNIX")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "getuid")
    )


class PeerRefused(Exception):
    """A connection whose kernel-reported peer may not decide (fixed reason, no data)."""


# --- the fixed text -------------------------------------------------------------------

_KIND_TEXT = {
    "yolo_request": ("YOLO の開始", "start YOLO"),
    "rules_loosen": ("ツール規則を緩める保存", "save looser tool rules"),
    "recover": ("ツール規則の RECOVER", "RECOVER the tool rules"),
    "scheduler_loosen": ("スケジュールの作成・再開・変更", "create / resume / change a job"),
    "yolo_unlatch": ("YOLO のラッチ解除", "unlatch YOLO"),
    "authority_unlatch": ("権限ラッチの解除", "clear an authority latch"),
}
_TIER_TEXT = {"internal": "内部のみ / internal only", "external": "外部もOK / external allowed"}


def _safe(value: Any, pattern: str = r"[a-z0-9_:#.+-]{1,48}") -> str:
    return value if type(value) is str and re.fullmatch(pattern, value) else "?"


def describe(view: dict) -> list[str]:
    """The fixed-format lines a person reads before deciding (built from enums / ids)."""
    kind = view.get("kind")
    ja, en = _KIND_TEXT.get(kind, ("不明な操作", "unknown operation"))
    lines = [f"Stira: {ja} / {en}", f"要求 / request: {_safe(view.get('short'), r'[0-9a-f]{8}')}"]
    if kind == "yolo_request":
        lines.append(f"範囲 / tier: {_TIER_TEXT.get(view.get('tier'), '?')}")
        if view.get("exec") is True:
            # Phase 7 (§9 option C): named explicitly on the host too.
            lines.append(
                "実行系も含む: コマンドが確認なしで実行され、Stira の保護(保護された場所・"
                "監査)を迂回しうる / includes exec: commands run without asking and can "
                "bypass Stira's protections (protected locations, audit)"
            )
        t0 = view.get("t0")
        t0 = t0 if type(t0) in (int, float) else None
        end = None if t0 is None else t0 + WINDOW_HOURS * 3600
        lines.append(
            f"期間 / window: {wall_clock_text(t0)} → {wall_clock_text(end)} "
            f"({WINDOW_HOURS} h, 押した時点から / from the press)"
        )
    else:
        summary = view.get("summary") if type(view.get("summary")) is dict else {}
        parts = []
        for key in ("action", "operation", "tool", "args", "card", "source", "job", "count"):
            if key in summary:
                value = summary[key]
                parts.append(f"{key}={value if type(value) is int else _safe(value)}")
        changes = summary.get("changes")
        if type(changes) is list:
            parts.append("changes=" + (",".join(_safe(t) for t in changes[:8]) or "-"))
        lines.append("内容 / what: " + (" ".join(parts) or "-"))
        lines.append(f"照合コード / code: {_safe(view.get('code'), r'[0-9a-f]{8}')}")
    expires = view.get("expires_in")
    lines.append(f"残り / expires in: {expires if type(expires) is int else '?'} s")
    lines.append("自分で要求していなければ拒否してください / Deny it if you did not ask for this.")
    return lines


# --- peer checks ----------------------------------------------------------------------


def peer_credentials(conn: socket.socket) -> tuple[int, int]:
    """``(pid, uid)`` of the connected peer, from the kernel (raises PeerRefused)."""
    try:
        if sys.platform == "darwin":
            cred = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 4 + 4 + 4 + 16 * 4)
            uid = struct.unpack_from("=II", cred)[1]
            pid = struct.unpack("=i", conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0]
            return pid, uid
        if sys.platform.startswith("linux"):
            pid, uid, _gid = struct.unpack(
                "=iII", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            return pid, uid
    except (OSError, struct.error):
        raise PeerRefused("peer credentials unavailable") from None
    raise PeerRefused("peer credentials unsupported")


def _parent(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return int(text) if out.returncode == 0 and text.isdigit() else None


def descends_from(pid: int, ancestor: int, parent: Callable[[int], int | None] = _parent) -> bool:
    """Whether ``pid`` is ``ancestor`` or below it. Unknown (a lookup that fails) → True."""
    seen = 0
    current = pid
    while seen < 128:
        if current == ancestor:
            return True
        if current <= 1:
            return False
        nxt = parent(current)
        if nxt is None:
            return True  # cannot tell: fail closed
        current, seen = nxt, seen + 1
    return True


def check_peer(
    pid: int,
    uid: int,
    *,
    own_pid: int,
    own_uid: int,
    parent: Callable[[int], int | None] = _parent,
) -> None:
    if type(pid) is not int or type(uid) is not int or pid <= 0:
        raise PeerRefused("peer unknown")
    if uid != own_uid:
        raise PeerRefused("another OS user")
    if descends_from(pid, own_pid, parent):
        raise PeerRefused("the server process or one of its children")


def principal_for(uid: int) -> str:
    try:
        name = pwd.getpwuid(uid).pw_name if pwd is not None else ""
    except (KeyError, OSError):
        return "local:unknown"
    return f"local:{name}" if _ACCOUNT.fullmatch(name) else "local:unknown"


# --- channel choice -------------------------------------------------------------------


def gui_session(environ=None, platform: str | None = None) -> bool:
    """macOS, not over SSH, and the console belongs to this user."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform != "darwin" or environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return False
    try:
        return os.stat("/dev/console").st_uid == os.getuid()
    except OSError:
        return False


def choose_channel(setting: str, *, gui: bool, dialog_ready: bool) -> str:
    """``auto``: the dialog on the person's macOS desktop when it can authenticate, else
    the command. ``dialog`` forced but unavailable: the command (logged by the caller)."""
    if setting == "command":
        return "command"
    if gui and dialog_ready:
        return "dialog"
    return "command"


# --- the desk: both kinds of operation behind one lookup ------------------------------


class HostDesk:
    """What the host side sees: generic operations and the YOLO request."""

    def __init__(self, operations: HostOperations, yolo: YoloRequests | None) -> None:
        self.operations = operations
        self.yolo = yolo
        self.incarnation = operations.incarnation
        self.channel = operations.channel
        # False when the command channel could not listen: nothing can be confirmed.
        self.available = True

    def _resolve(self, prefix: Any) -> tuple[str, str]:
        found = []
        op = self.operations.resolve(prefix)
        if op is not None:
            found.append(("op", op))
        if self.yolo is not None and type(prefix) is str and len(prefix) >= 8:
            found += [("yolo", rid) for rid in self.yolo.ids() if rid.startswith(prefix)]
        if len(found) != 1:
            raise HostOpRefused("host_operation_unknown")
        return found[0]

    def pending(self) -> list[dict]:
        items = list(self.operations.pending())
        if self.yolo is not None:
            items += self.yolo.pending()
        return items

    def show(self, prefix: Any) -> dict:
        kind, ident = self._resolve(prefix)
        view = self.operations.status(ident) if kind == "op" else self.yolo.show(ident)
        return {**view, "incarnation": self.incarnation}

    def decide(self, ident: Any, *, incarnation, decision, principal, channel) -> dict:
        if type(ident) is not str or not _HEX32.fullmatch(ident):
            raise HostOpRefused("host_operation_unknown")
        kind, ident = self._resolve(ident)
        target = self.operations if kind == "op" else self.yolo
        return target.decide(
            ident,
            incarnation=incarnation,
            decision=decision,
            principal=principal,
            channel=channel,
        )

    def confirm_in_browser(
        self,
        ident: Any,
        *,
        token: Any,
        acknowledged: Any,
        external_acknowledged: Any,
        instance: Any,
        exec_acknowledged: Any = None,
    ) -> dict:
        """The dashboard's checkbox confirmation (browser channel, revision 6 / phase 2c;
        phase 7: ``exec_acknowledged`` for a request that includes exec)."""
        if type(ident) is not str or not _HEX32.fullmatch(ident):
            raise HostOpRefused("host_operation_unknown")
        kind, ident = self._resolve(ident)
        if kind == "op":
            return self.operations.confirm_in_browser(
                ident, token=token, acknowledged=acknowledged, instance=instance
            )
        return self.yolo.confirm_in_browser(
            ident,
            token=token,
            acknowledged=acknowledged,
            external_acknowledged=external_acknowledged,
            instance=instance,
            exec_acknowledged=exec_acknowledged,
        )

    def revoke(self, reason: str, *, human: bool = True) -> int:
        """Stop / ``/new``: every open operation is cancelled, the YOLO request fenced and
        the window ended (phase 3). A narrowing: never refused. Returns how many
        operations it ended."""
        # The window first: its narrowing depends on nothing that can fail.
        try:
            if self.yolo is not None:
                self.yolo.fence(reason, human=human)
        finally:
            count = self.operations.revoke_all()
        return count


# --- the socket server ----------------------------------------------------------------


def _reply(conn: socket.socket, body: dict) -> None:
    with contextlib.suppress(OSError):
        conn.sendall(json.dumps(body, ensure_ascii=True).encode("ascii") + b"\n")


class HostChannelServer:
    """A Unix socket for the host command. ``decisions`` False: list / show only."""

    def __init__(
        self,
        desk: HostDesk,
        data_dir: Path,
        *,
        decisions: bool = True,
        own_pid: int | None = None,
        parent: Callable[[int], int | None] = _parent,
    ) -> None:
        if not _platform_supported():
            raise HostChannelUnsupported("host command channel needs POSIX (#540)")
        self.desk = desk
        self.data_dir = Path(data_dir)
        self.decisions = decisions
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._parent = parent
        self._dir = Path(tempfile.mkdtemp(prefix="stira-hc-"))
        os.chmod(self._dir, 0o700)
        self.path = self._dir / "s"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self._sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="yolo-host-channel", daemon=True)
        self.descriptor = self.data_dir / DESCRIPTOR_NAME
        self._write_descriptor()
        self._thread.start()

    def _write_descriptor(self) -> None:
        body = json.dumps({"socket": str(self.path), "pid": os.getpid()}).encode("ascii")
        tmp = self.descriptor.with_name(self.descriptor.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.descriptor)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with contextlib.suppress(OSError):
            self._sock.close()
        with contextlib.suppress(OSError):
            data = json.loads(self.descriptor.read_text(encoding="ascii"))
            if data.get("socket") == str(self.path):
                self.descriptor.unlink()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with conn:
                try:
                    self._handle(conn)
                except Exception as error:  # noqa: BLE001 - one request never stops it
                    # #601 A5: a fixed refusal (never the error's text); the next
                    # connection is served. The log names the error's type only.
                    logger.warning("host channel: a request failed (%s)", type(error).__name__)
                    _reply(conn, {"ok": False, "code": "host_request_invalid"})

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        try:
            pid, uid = peer_credentials(conn)
            check_peer(pid, uid, own_pid=self._own_pid, own_uid=os.getuid(), parent=self._parent)
        except PeerRefused:
            _reply(conn, {"ok": False, "code": "host_peer_refused"})
            return
        data = b""
        try:
            while b"\n" not in data and len(data) <= _MAX_LINE:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            return
        try:
            message = json.loads(data.split(b"\n", 1)[0].decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # #601 A6: nesting past the decoder's limit is not understood either.
            _reply(conn, {"ok": False, "code": "host_request_invalid"})
            return
        _reply(conn, self.answer(message, principal_for(uid)))

    def answer(self, message: Any, principal: str) -> dict:
        """One request of the host command (exact built-in types only)."""
        if type(message) is not dict or type(message.get("cmd")) is not str:
            return {"ok": False, "code": "host_request_invalid"}
        cmd = message["cmd"]
        try:
            if cmd == "pending":
                return {"ok": True, "items": self.desk.pending(), "channel": self._channel()}
            if cmd == "show":
                return {"ok": True, "item": self.desk.show(message.get("id"))}
            if cmd == "decide":
                if not self.decisions:
                    return {"ok": False, "code": "host_channel_dialog"}
                item = self.desk.decide(
                    message.get("id"),
                    incarnation=message.get("incarnation"),
                    decision=message.get("decision"),
                    principal=principal,
                    channel="cli",
                )
                return {"ok": True, "item": item}
        except HostOpRefused as error:
            return {"ok": False, "code": error.code, "state": error.state}
        return {"ok": False, "code": "host_request_invalid"}

    def _channel(self) -> str:
        return "command" if self.decisions else "dialog"


def read_descriptor(data_dir: Path) -> Path:
    """The socket path for the host command; refuses a symlink / foreign / odd file."""
    path = Path(data_dir) / DESCRIPTOR_NAME
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise PeerRefused("descriptor is not a regular file of this user")
    data = json.loads(path.read_text(encoding="ascii"))
    sock = data.get("socket") if isinstance(data, dict) else None
    if type(sock) is not str or not os.path.isabs(sock):
        raise PeerRefused("descriptor has no socket path")
    sinfo = os.lstat(sock)
    if not stat.S_ISSOCK(sinfo.st_mode) or sinfo.st_uid != os.getuid():
        raise PeerRefused("socket is not a socket of this user")
    return Path(sock)


def ask(socket_path: Path, message: dict, timeout: float = 10.0) -> dict:
    """One round trip to the server's host channel (the host command's side)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps(message).encode("ascii") + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.split(b"\n", 1)[0].decode("ascii"))
