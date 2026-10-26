"""Start-up smoke test for the chat server (#160): launch, GET /api/config == 200, stop.

    python scripts/smoke_server.py [--port N] [--timeout SECONDS]

Starts the real server entry point (``stira_server.main`` with the default
``create_app``) on an unused loopback port, in a private temporary data directory,
and passes when, before ``--timeout``:

- ``/api/config`` and ``/api/gateway/status`` answer HTTP 200 with JSON, and
- ``/api/config/hub`` reports a workspace inside that temporary directory (whose
  name is random), while the started process is alive -- so a 200 from some
  other process on the port is never taken for this server's, and
- after SIGTERM the server exits with code 0 by itself (no forced kill).

Isolation: the data directory, logs, ``HOME`` and temp directory all live in one
temporary directory that is removed afterwards, pass or fail; the checkout's
``.local/`` is not touched. The child sees an allow-list of environment variables
only (no credentials, no ``AWS_*`` from the caller, no proxy settings). HTTP goes
to 127.0.0.1 only, without proxies, without following redirects, with a body
limit. On failure, the last lines of the server's stderr are printed masked.

No model is called: the model client is built without contacting the provider and
the Sokudan classifier loads on the first chat turn, not at start-up. Exit code:
0 pass, 1 fail.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
MAX_BODY = 1024 * 1024
TAIL_LINES = 20
TAIL_LINE_CHARS = 400
# Waiting for the server's own bounded stops (9 s, ADR 0008) before SIGKILL.
STOP_TIMEOUT = 15.0
# Variables passed through from the caller; everything else is dropped.
PASS_THROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # Windows: the runtime needs these to start and to use sockets.
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def child_env(base: dict[str, str], *, port: int, scratch: Path) -> dict[str, str]:
    """The child's whole environment: the pass-through names plus fixed values."""
    env = {name: base[name] for name in PASS_THROUGH if name in base}
    home = scratch / "home"
    tmp = scratch / "tmp"
    no_aws = scratch / "no-aws"
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(tmp),
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "STIRA_PORT": str(port),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # No cloud account and no download at start-up.
            "AWS_CONFIG_FILE": str(no_aws / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(no_aws / "credentials"),
            "AWS_EC2_METADATA_DISABLED": "true",
            "HF_HUB_OFFLINE": "1",
        }
    )
    return env


def child_command(data_dir: Path) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--child", str(data_dir)]


def run_child(data_dir: Path) -> None:
    """The server entry point with the data and config files in ``data_dir``."""
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from dashboard import stira_server
    from stiracore.observability.config_api import ConfigStore

    data_dir.mkdir(parents=True, exist_ok=True)
    stira_server.DATA_DIR = data_dir
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT is the stop request
        signal.signal(signal.SIGBREAK, stira_server._exit_on_sigterm)
    stira_server.main(app_factory=lambda lc: stira_server.create_app(store, lifecycle=lc))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx then surfaces as HTTPError; it is never followed


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != HOST or parts.port is None:
        raise ValueError(f"smoke only talks to http://{HOST}:<port>, not {url!r}")


def get_json(url: str, timeout: float) -> tuple[int, object]:
    """(status, parsed JSON or None). 3xx is not followed; bodies over MAX_BODY fail."""
    check_url(url)
    try:
        with OPENER.open(url, timeout=max(0.1, timeout)) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                return response.status, None
            return response.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None
    except (OSError, ValueError):
        return 0, None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def masked_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line[:TAIL_LINE_CHARS] for line in text.splitlines()[-TAIL_LINES:]]
    try:
        mask_secrets = _masking().mask_secrets
    except Exception:  # noqa: BLE001 - never print unmasked
        return f"({len(lines)} lines withheld: masking unavailable)"
    return "\n".join(mask_secrets(line) for line in lines)


def _masking():
    """The server's masking rules, loaded from its file alone (stdlib-only module).

    Not imported through the package, whose import also builds the tool registry.
    """
    import importlib.util

    path = ROOT / "src" / "stiracore" / "observability" / "masking.py"
    spec = importlib.util.spec_from_file_location("_stira_smoke_masking", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signal_group(process: subprocess.Popen, sig) -> None:
    try:
        if os.name == "nt":
            process.send_signal(sig)
        else:
            os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop(process: subprocess.Popen, timeout: float) -> tuple[int | None, bool]:
    """Ask the process group to stop; (exit code, forced). Forced = SIGKILL was needed."""
    forced = False
    if process.poll() is None:
        graceful = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM
        _signal_group(process, graceful)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
            if os.name == "nt":
                process.kill()
            else:
                _signal_group(process, signal.SIGKILL)
            process.wait(timeout=10)
    if os.name != "nt":
        # Nothing of the group may outlive the server.
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        else:
            forced = True
            _signal_group(process, signal.SIGKILL)
    return process.returncode, forced


@dataclass
class Result:
    port: int
    scratch: Path
    status: dict = field(default_factory=dict)
    identified: bool = False
    exit_code: int | None = None
    forced: bool = False
    removed: bool = False
    elapsed: float = 0.0
    stderr_tail: str = ""

    @property
    def served_and_stopped(self) -> bool:
        return (
            all(code == 200 for code in self.status.values())
            and self.identified
            and self.exit_code == 0
            and not self.forced
        )

    @property
    def passed(self) -> bool:
        return self.served_and_stopped and self.removed


PATHS = ("/api/config", "/api/gateway/status", "/api/config/hub")


def _is_ours(hub: object, data_dir: Path) -> bool:
    workspace = hub.get("workspace") if isinstance(hub, dict) else None
    if not isinstance(workspace, str):
        return False
    try:
        return Path(workspace).resolve().is_relative_to(data_dir.resolve())
    except (OSError, ValueError):
        return False


def run_smoke(port: int = 0, timeout: float = 120.0) -> Result:
    started = time.monotonic()
    deadline = started + timeout
    port = port or free_port()
    scratch = Path(tempfile.mkdtemp(prefix="stira-smoke-"))
    result = Result(port=port, scratch=scratch, status={path: 0 for path in PATHS})
    data_dir = scratch / "data"
    for sub in ("home", "tmp", "no-aws"):
        (scratch / sub).mkdir()
    err_path = scratch / "server.err.log"
    base = f"http://{HOST}:{port}"
    process = None
    previous = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        # Killed ourselves: still stop the child group and remove the directory.
        def _on_sigterm(signum, frame):
            raise SystemExit(128 + signum)

        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        with open(scratch / "server.out.log", "wb") as out, open(err_path, "wb") as err:
            process = subprocess.Popen(
                child_command(data_dir),
                cwd=scratch,
                env=child_env(dict(os.environ), port=port, scratch=scratch),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                **options,
            )
            while time.monotonic() < deadline and process.poll() is None:
                for path in PATHS:
                    if result.status[path] == 200 and path != "/api/config/hub":
                        continue
                    remaining = deadline - time.monotonic()
                    code, body = get_json(base + path, min(5.0, remaining))
                    # 200 counts only with a JSON body; other codes are shown as they are.
                    result.status[path] = 0 if code == 200 and body is None else code
                    if path == "/api/config/hub" and code == 200:
                        result.identified = process.poll() is None and _is_ours(body, data_dir)
                if result.identified and all(c == 200 for c in result.status.values()):
                    break
                time.sleep(0.25)
            result.elapsed = time.monotonic() - started
            result.exit_code, result.forced = stop(process, STOP_TIMEOUT)
        if not result.served_and_stopped:
            result.stderr_tail = masked_tail(err_path)
    finally:
        if process is not None and process.poll() is None:
            stop(process, 2.0)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        shutil.rmtree(scratch, ignore_errors=True)
        result.removed = not scratch.exists()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="default: an unused port")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds to wait")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child is not None:
        run_child(args.child)
        return 0
    result = run_smoke(args.port, args.timeout)
    for path in PATHS:
        print(f"smoke: GET {path} -> {result.status[path] or 'no answer'}")
    print(f"smoke: started process identified: {'yes' if result.identified else 'no'}")
    stop_text = "forced kill" if result.forced else f"exit {result.exit_code}"
    print(f"smoke: stop: {stop_text}; temporary directory removed: {result.removed}")
    verdict = "PASS" if result.passed else "FAIL"
    print(f"smoke: {verdict} in {result.elapsed:.1f}s (port {result.port})")
    if not result.passed:
        print(f"--- server stderr (last {TAIL_LINES} lines, masked) ---", file=sys.stderr)
        print(result.stderr_tail, file=sys.stderr)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
