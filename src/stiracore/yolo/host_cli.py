"""Host commands for a person at the server's console (ADR 0012 §8b (iv), phase 2).

* ``stira yolo pending`` / ``stira yolo confirm <id>`` / ``stira yolo deny <id>`` — the
  YOLO request (phase 3: a confirmation opens the window; nothing is approved
  automatically before phase 5).
* ``stira approvals pending`` / ``stira approvals decide <id> [--deny]`` — every other
  host-confirmed operation: ``RECOVER``, a loosening tool-rule, hub or rollback save, a
  scheduler create / resume / confirmed update, an unlatch (``UNLATCH``, phase 3).

``<id>`` is the short id the page shows (8 hex characters, or more). A confirmation
needs a **TTY** (stdin and stdout) and the typed word the server names (``YOLO``,
``APPROVE``, ``RECOVER`` or ``UNLATCH``); the text shown is the server's fixed format
(:func:`host_channel.describe`). A denial needs neither (it only narrows). The command
talks to the running server through its host channel (a Unix socket found through
``yolo-host-channel.json`` in the data directory); it only exists while the server runs
with ``STIRA_YOLO_ALLOW``. Exit status: 0 = done, 1 = refused / not confirmed, 2 = usage
or no server.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stiracore.config.data_dir import DataDirSettingError, explicit_data_dir

from .host_channel import PeerRefused, ask, describe, read_descriptor

__all__ = ["add_parsers", "default_data_dir", "run"]

_MESSAGES = {
    "host_peer_refused": "this process may not decide (another user, or the server's own tree)",
    "host_channel_dialog": "this server confirms on its desktop dialog, not by command",
    "host_operation_unknown": "no such operation (it ended, or the server restarted)",
    "host_operation_decided": "the operation was already decided",
    "host_operation_expired": "the operation expired",
    "host_operation_incarnation": "the server restarted; the operation is gone",
    "host_operation_mismatch": "the request was not understood",
    "host_request_invalid": "the request was not understood",
}


def default_data_dir(environ=None) -> Path:
    """``STIRA_DATA_DIR`` when set, else the checkout's default (as the server)."""
    environ = os.environ if environ is None else environ
    explicit = explicit_data_dir(environ)
    if explicit is not None:
        return explicit
    return Path(__file__).resolve().parents[3] / ".local" / "stira"


def add_parsers(commands) -> None:
    leaves = []
    yolo = commands.add_parser("yolo", help="YOLO request: list, confirm or deny on the host")
    sub = yolo.add_subparsers(dest="yolo_command", required=True)
    leaves.append(sub.add_parser("pending", help="list the pending YOLO request"))
    for name in ("confirm", "deny"):
        command = sub.add_parser(name, help=f"{name} a YOLO request (confirm needs a TTY)")
        command.add_argument("id", help="the request's short id (8+ hex characters)")
        leaves.append(command)
    approvals = commands.add_parser(
        "approvals", help="host-confirmed operations: list, approve or deny on the host"
    )
    sub = approvals.add_subparsers(dest="approvals_command", required=True)
    leaves.append(sub.add_parser("pending", help="list operations waiting for a host confirmation"))
    decide = sub.add_parser("decide", help="approve (needs a TTY) or --deny an operation")
    decide.add_argument("id", help="the operation's short id (8+ hex characters)")
    decide.add_argument("--deny", action="store_true", help="deny instead of approving")
    leaves.append(decide)
    for parser in leaves:
        parser.add_argument("--data-dir", type=Path, default=None, help="the server's data dir")


def _print_refusal(out, reply: dict) -> int:
    code = reply.get("code") if isinstance(reply, dict) else None
    text = _MESSAGES.get(code, "refused") if isinstance(code, str) else "refused"
    print(f"Stira: {text} ({code if isinstance(code, str) else 'error'})", file=out)
    return 1


def run(
    args,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    is_tty: Callable[[], bool] | None = None,
    read_line: Callable[[str], str] | None = None,
    transport: Callable[[dict], dict] | None = None,
) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if is_tty is None:

        def is_tty() -> bool:
            return stdin.isatty() and stdout.isatty()

    if read_line is None:
        read_line = input
    if transport is None:
        try:
            data_dir = args.data_dir or default_data_dir()
            socket_path = read_descriptor(data_dir)
        except (OSError, ValueError, PeerRefused, DataDirSettingError):
            print(
                "Stira: no running server with STIRA_YOLO_ALLOW found for this data directory",
                file=stderr,
            )
            return 2

        def transport(message: dict) -> dict:
            return ask(socket_path, message)

    yolo = args.command == "yolo"
    sub = args.yolo_command if yolo else args.approvals_command
    try:
        if sub == "pending":
            return _pending(transport, stdout, stderr, yolo)
        deny = sub == "deny" or bool(getattr(args, "deny", False))
        return _decide(transport, args.id, deny, yolo, stdout, stderr, is_tty, read_line)
    except (OSError, ValueError):
        print("Stira: the server's host channel did not answer", file=stderr)
        return 2


def _pending(transport, stdout, stderr, yolo: bool) -> int:
    reply = transport({"cmd": "pending"})
    if not reply.get("ok"):
        return _print_refusal(stderr, reply)
    items = [
        item
        for item in reply.get("items", [])
        if isinstance(item, dict) and (item.get("kind") == "yolo_request") == yolo
    ]
    if not items:
        print("Stira: nothing is waiting for a host confirmation", file=stdout)
        return 0
    for item in items:
        print("\n".join(describe(item)), file=stdout)
        print(file=stdout)
    return 0


def _decide(transport, ident: Any, deny: bool, yolo: bool, stdout, stderr, is_tty, read_line):
    reply = transport({"cmd": "show", "id": ident})
    if not reply.get("ok"):
        return _print_refusal(stderr, reply)
    item = reply.get("item") if isinstance(reply.get("item"), dict) else {}
    if (item.get("kind") == "yolo_request") != yolo:
        other = "stira approvals decide" if yolo else "stira yolo confirm"
        print(f"Stira: this is not that kind of operation; use `{other}`", file=stderr)
        return 1
    if item.get("state") != "pending":
        print(f"Stira: the operation is {item.get('state')!s:.16}, not pending", file=stderr)
        return 1
    print("\n".join(describe(item)), file=stdout)
    if not deny:
        if not is_tty():
            print("Stira: a confirmation needs a terminal (stdin and stdout)", file=stderr)
            return 1
        word = (
            item.get("word")
            if item.get("word") in ("YOLO", "EXEC", "APPROVE", "RECOVER", "UNLATCH")
            else None
        )
        if word is None:
            return _print_refusal(stderr, {"code": "host_request_invalid"})
        try:
            typed = read_line(f"確認するには {word} と入力 / Type {word} to confirm: ")
        except EOFError:
            typed = ""
        if typed.strip() != word:
            print("Stira: not confirmed (nothing was decided; it expires by itself)", file=stdout)
            return 1
    answer = transport(
        {
            "cmd": "decide",
            "id": item.get("id"),
            "incarnation": item.get("incarnation"),
            "decision": "deny" if deny else "confirm",
        }
    )
    if not answer.get("ok"):
        return _print_refusal(stderr, answer)
    result = answer.get("item") if isinstance(answer.get("item"), dict) else {}
    if deny:
        print("Stira: denied", file=stdout)
    elif yolo:
        print(
            "Stira: confirmed and recorded "
            f"(request {result.get('state')!s:.16}; the window's state is in the page).",
            file=stdout,
        )
    else:
        print("Stira: confirmed; the page applies it now", file=stdout)
    return 0
