"""A minimal stand-in for ``pyright-langserver --stdio`` (tests only, #315).

It answers ``initialize``, keeps the text of ``textDocument/didOpen`` and answers a pull
``textDocument/diagnostic`` with one diagnostic per line of *that text* (``seen: <line>``),
so a test can tell which bytes were analysed. Before answering it asks the client for
its configuration (as Pyright does) and sends a log notification. It never opens the
document's file.

``FAKE_LSP_MODE`` (set by the test's launcher; ``FAKE_LSP_ARG`` is a file path):

* ``hang``: never answers the diagnostic request;
* ``noread``: after ``initialize`` never reads its input again (a writer would block);
* ``hugeheader`` / ``hugebody`` / ``flood``: an endless header line, a frame announcing
  100 MiB, endless small notifications — instead of answering ``initialize``;
* ``child``: starts a sleeping child, writes its PID to ``FAKE_LSP_ARG``, then hangs;
* ``env``: writes the names of its environment variables to ``FAKE_LSP_ARG`` (JSON);
* ``holder``: starts a sleeping child that keeps stdout open (PID to ``FAKE_LSP_ARG``),
  then answers normally and exits on ``exit``;
* ``tailheader``: answers normally, then sends a header line beyond every bound after
  ``shutdown``.
"""

from __future__ import annotations

import json
import os
import sys


def _read(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.partition(b":")
        if key.lower() == b"content-length":
            length = int(value)
    return json.loads(stream.read(length))


def _send(message):
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def _misbehave(mode: str) -> None:
    out = sys.stdout.buffer
    if mode == "hugeheader":
        while True:
            out.write(b"X" * 65536)
            out.flush()
    if mode == "hugebody":
        out.write(b"Content-Length: %d\r\n\r\n" % (100 * 1024 * 1024))
        while True:
            out.write(b" " * 65536)
            out.flush()
    if mode == "flood":
        while True:
            _send({"method": "window/logMessage", "params": {"type": 4, "message": "x"}})


def main() -> int:
    import subprocess
    import time

    mode = os.environ.get("FAKE_LSP_MODE", "")
    argument = os.environ.get("FAKE_LSP_ARG", "")
    if mode == "env":
        with open(argument, "w", encoding="utf-8") as handle:
            json.dump(sorted(os.environ), handle)
    texts: dict[str, str] = {}
    pending = None
    while True:
        message = _read(sys.stdin.buffer)
        if message is None:
            return 0
        method = message.get("method")
        if method == "initialize" and mode in ("hugeheader", "hugebody", "flood"):
            _misbehave(mode)
        if method == "initialize":
            _send({"id": message["id"], "result": {"capabilities": {"diagnosticProvider": {}}}})
            if mode == "holder":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
                with open(argument, "w", encoding="utf-8") as handle:
                    handle.write(str(child.pid))
            if mode == "noread":
                time.sleep(3600)
            if mode == "child":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
                with open(argument, "w", encoding="utf-8") as handle:
                    handle.write(str(child.pid))
                time.sleep(3600)
        elif method == "textDocument/didOpen":
            document = message["params"]["textDocument"]
            texts[document["uri"]] = document["text"]
        elif method == "textDocument/diagnostic":
            _send({"method": "window/logMessage", "params": {"type": 3, "message": "checking"}})
            _send({"id": 900, "method": "workspace/configuration", "params": {"items": [{}]}})
            pending = message
        elif message.get("id") == 900 and pending is not None:
            if os.environ.get("FAKE_LSP_MODE") == "hang":
                continue
            text = texts.get(pending["params"]["textDocument"]["uri"], "")
            items = [
                {
                    "range": {
                        "start": {"line": number, "character": 0},
                        "end": {"line": number, "character": max(1, len(line))},
                    },
                    "message": "seen: " + line,
                    "severity": 1,
                    "code": "fakeRule",
                }
                for number, line in enumerate(text.splitlines())
            ]
            _send({"id": pending["id"], "result": {"kind": "full", "items": items}})
            pending = None
        elif method == "shutdown":
            _send({"id": message["id"], "result": None})
            if mode == "tailheader":
                sys.stdout.buffer.write(b"X-Tail: " + b"y" * (64 * 1024) + b"\r\n")
                sys.stdout.buffer.flush()
        elif method == "exit":
            return 0


if __name__ == "__main__":
    sys.exit(main())
