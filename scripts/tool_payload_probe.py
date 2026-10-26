#!/usr/bin/env python3
"""Measure what one minimal turn sends to the model: tools, system, messages (#368).

Starts the real chat server entry point (``tests/stira_headless_harness.py``) on a
temporary data directory with its own ``BedrockModel`` pointed at
``tests/fake_bedrock_endpoint.py`` (no cloud call, dummy credentials), sends one
minimal chat turn and prints the size of each part of the ConverseStream request
body the server really sent (JSON characters; ``ensure_ascii=False``).

Conditions:

* ``default``: a fresh install (first-run tool rules), exec tools hidden;
* ``exec``: ``STIRA_ENABLE_EXEC_TOOLS=1`` and every tool ON (a pre-#190 ``trust_all``
  file, migrated on start: ON -> Always, exec ON -> Ask) — the shape of the
  2026-10-17 measurement in #368.

The server gets only ``PASSED_ENV`` from the caller's environment (no credential, API
key or STIRA_* setting) plus the offline endpoint. Its output is read from a pipe and
written to the temporary directory only masked (line by line); only on a failed start
is its end shown.

    PYTHONPATH="$WT/src:$WT" python scripts/tool_payload_probe.py [--json OUT]

Characters are not tokens: compare conditions by their ratio. Every turn is a
fake-endpoint call (no Bedrock charge).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from stiracore.observability.masking import mask_secrets
from tests.fake_bedrock_endpoint import FakeBedrock, offline_env

HARNESS = ROOT / "tests" / "stira_headless_harness.py"
PROMPT = "起動確認です。一言だけ挨拶して。"
LEGACY_TRUST_ALL = {"mode": "trust_all", "enabled_tools": [], "shell_allowlist": []}
# The only variables taken from the caller's environment: no credential, API key,
# provider selection or STIRA_* setting reaches the measured server (a key such as
# PERPLEXITY_API_KEY would also add a tool and change the measurement).
PASSED_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _log_tail(log: Path) -> str:
    """The end of the server's output: masked first, then cut (a cut never splits a
    header from its value before masking)."""
    try:
        return mask_secrets(log.read_text("utf-8", errors="replace"))[-3000:]
    except OSError:
        return "(no server output)"


def _start_masked(command: list[str], *, cwd: str, env: dict[str, str], log: Path):
    """Start ``command``; its stdout and stderr reach ``log`` only masked, line by line.

    The raw output is read from a pipe and never written anywhere. Returns the process
    and the thread that writes the log (``_stop`` ends both).
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def pump() -> None:
        with log.open("w", encoding="utf-8") as handle:
            for line in process.stdout:
                handle.write(mask_secrets(line))
                handle.flush()

    thread = threading.Thread(target=pump, name="probe-log", daemon=True)
    thread.start()
    return process, thread


def _stop(process: subprocess.Popen, thread: threading.Thread) -> None:
    process.terminate()
    try:
        process.wait(20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(10)
    thread.join(10)
    if process.stdout is not None:
        process.stdout.close()


def _request(base: str, method: str, path: str, body=None, token: str = ""):
    req = urllib.request.Request(base + path, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
        req.add_header("X-CSRF-Token", token)
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.status, response.read().decode("utf-8")


def measure_request(
    condition: str, extra_env: dict[str, str] | None = None, gateway: dict | None = None
) -> dict:
    """One fresh server, one minimal turn; the first ConverseStream request body.

    ``gateway``: a ``gateway-config.json`` written before the start (e.g.
    ``{"tools_short_schemas": true}``); none by default (a fresh install).
    """
    with tempfile.TemporaryDirectory() as tmp, FakeBedrock() as fake:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        if gateway:
            (data_dir / "gateway-config.json").write_text(json.dumps(gateway), "utf-8")
        if condition == "exec":
            (data_dir / "tool-config.json").write_text(json.dumps(LEGACY_TRUST_ALL), "utf-8")
        port = _free_port()
        env = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
        env.update(
            offline_env(fake.url),
            PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            STIRA_PORT=str(port),
            STIRA_HEARTBEAT="0",
            PYTHONUNBUFFERED="1",
        )
        if condition == "exec":
            env["STIRA_ENABLE_EXEC_TOOLS"] = "1"
        env.update(extra_env or {})
        log = Path(tmp) / "server.log"
        process, pump = _start_masked(
            [sys.executable, str(HARNESS), str(data_dir)], cwd=tmp, env=env, log=log
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("server did not start:\n" + _log_tail(log))
                try:
                    if _request(base, "GET", "/api/gateway/status")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            token = json.loads(_request(base, "GET", "/api/csrf")[1])["token"]
            status, _ = _request(base, "POST", "/api/chat", {"message": PROMPT}, token)
            if status != 200:
                raise SystemExit(f"chat failed: {status}")
        finally:
            _stop(process, pump)
        bodies = [r["body"] for r in fake.requests if r["path"].endswith("/converse-stream")]
        if not bodies:
            raise SystemExit("no model request was recorded")
        return bodies[0]


def measure(
    condition: str, extra_env: dict[str, str] | None = None, gateway: dict | None = None
) -> dict:
    """The sizes of each part of the first model request of a minimal turn."""
    body = measure_request(condition, extra_env, gateway)
    tools = (body.get("toolConfig") or {}).get("tools") or []
    specs = [t["toolSpec"] for t in tools if "toolSpec" in t]
    return {
        "condition": condition,
        "total_chars": _size(body),
        "tool_config_chars": _size(body.get("toolConfig") or {}),
        "system_chars": _size(body.get("system") or []),
        "messages_chars": _size(body.get("messages") or []),
        "tool_count": len(specs),
        "tools": {spec["name"]: _size(spec) for spec in specs},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", help="write the measurements to this file")
    parser.add_argument("--condition", choices=("default", "exec"), action="append")
    parser.add_argument(
        "--short-schemas",
        action="store_true",
        help='start with "tools_short_schemas": true in gateway-config.json (#368)',
    )
    args = parser.parse_args()
    gateway = {"tools_short_schemas": True} if args.short_schemas else None
    rows = [
        measure(condition, gateway=gateway) for condition in (args.condition or ("default", "exec"))
    ]
    print("| condition | tools | toolConfig | system | messages | total (JSON chars) |")
    print("|---|---:|---:|---:|---:|---:|")
    for row in rows:
        print(
            f"| {row['condition']} | {row['tool_count']} | {row['tool_config_chars']:,} | "
            f"{row['system_chars']:,} | {row['messages_chars']:,} | {row['total_chars']:,} |"
        )
    for row in rows:
        listed = ", ".join(f"{name} {size}" for name, size in row["tools"].items())
        print(f"\n{row['condition']}: {listed}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")


if __name__ == "__main__":
    main()
