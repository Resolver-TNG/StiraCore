"""Count the page polls of a server per page (#369): requests per second, idle, one tab.

    python scripts/measure_polling.py stira [SECONDS] [PAGE,...]
    python scripts/measure_polling.py observer [SECONDS]

Starts the server on a fresh data directory with the offline fake model
(``tests/fake_bedrock_endpoint.py``; no cloud call), opens each page in headless Chrome
(DevTools endpoint) and counts the API ``GET`` lines in the server's access log. Poll lines
are hidden from that log by default (``STIRA_ACCESS_LOG_POLLING``), so the script starts the
server with ``STIRA_ACCESS_LOG_POLLING=1``. Each page is watched for two windows of SECONDS
(default 60): "first" includes the widening of the intervals, "steady" is the window right
after it (the intervals have reached their idle maximum). Static files are not counted.
Port: STIRA_PORT or 5714 (the DevTools port is the next one); Chrome: CHROME or the macOS path.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

PORT = int(os.environ.get("STIRA_PORT", "5714"))
DEBUG_PORT = PORT + 1
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PAGES = {
    "stage": "/stage/",
    "chat": "/chat",
    "approvals": "/approvals",
    "schedules": "/schedules",
    "observability": "/observability",
}
GET = re.compile(r'"GET (/api/\S*) HTTP')


def _count(log: Path, start: int) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for line in log.read_text(encoding="utf-8").splitlines()[start:]:
        match = GET.search(line)
        if match:
            counts[match.group(1)] += 1
    return counts


def _lines(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines())


def _start_server(mode: str, tmp: Path, log: Path):
    env = dict(os.environ)
    env.update(
        PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}",
        STIRA_PORT=str(PORT),
        STIRA_HEARTBEAT="0",
        STIRA_STAGE="1",
        STIRA_ACCESS_LOG_POLLING="1",
        PYTHONUNBUFFERED="1",
    )
    fake = None
    if mode == "stira":
        from tests.fake_bedrock_endpoint import FakeBedrock, offline_env, strip_cloud_env

        fake = FakeBedrock().__enter__()
        env = {**strip_cloud_env(env), **offline_env(fake.url)}
        harness = str(ROOT / "tests" / "stira_headless_harness.py")
        cmd = [sys.executable, harness, str(tmp / "d")]
        cwd = tmp
    else:
        env["STIRA_STATE_PATH"] = str(tmp / "state.json")
        code = (
            "from app import create_app; "
            f"create_app().run(host='127.0.0.1', port={PORT}, debug=False, use_reloader=False)"
        )
        cmd = [sys.executable, "-c", code]
        cwd = ROOT / "dashboard"
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT
    )
    for _ in range(300):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2)
            return proc, fake
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise SystemExit("server did not start:\n" + log.read_text(encoding="utf-8")[-2000:])


def _open(tab_url: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{DEBUG_PORT}/json/new?{tab_url}", method="PUT"
    )
    return json.load(urllib.request.urlopen(request))["id"]


def _close(tab_id: str) -> None:
    urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/close/{tab_id}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stira"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    wanted = sys.argv[3].split(",") if len(sys.argv) > 3 else list(PAGES)
    pages = (
        {"observer": "/"} if mode == "observer" else {k: v for k, v in PAGES.items() if k in wanted}
    )
    tmp = Path(tempfile.mkdtemp(prefix="measure-polling-"))
    log = tmp / "server.log"
    proc, fake = _start_server(mode, tmp, log)
    chrome = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-first-run",
            "--disable-gpu",
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={tmp / 'chrome'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.3)
        for name, path in pages.items():
            tab = _open(f"http://127.0.0.1:{PORT}{path}")
            for window in ("first", "steady"):
                start = _lines(log)
                time.sleep(seconds)
                counts = _count(log, start)
                total = sum(counts.values())
                print(
                    f"{mode} {name} {window}: {total} API requests / {seconds} s = "
                    f"{total / seconds:.2f} req/s {dict(counts)}",
                    flush=True,
                )
            _close(tab)
    finally:
        chrome.terminate()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if fake is not None:
            fake.__exit__(None, None, None)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
