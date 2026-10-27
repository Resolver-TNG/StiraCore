"""#453: the real-browser check of static SVG on the Stage board, run when a browser is found.

``scripts/check_board_svg_browser.mjs`` starts the offline fake model and the real chat server,
drives headless Chrome over ``--remote-debugging-pipe`` and checks, with the setting on and off:
0 requests to the probe URLs the hostile SVG points at, no script run (canary, dialogs, console),
the pictures' pixels, the CSP header, and a control (the raw SVG in the DOM *does* reach the
probes). This test runs it and fails on any failed check.

Skipped, with the reason, when:

* no Chrome / Chromium is found (``CHROME``, else the platform's usual places: see
  ``findChrome`` in the script) or ``node`` is missing;
* on Windows (the script talks to Chrome over pipes on fds 3/4 and uses ``/dev/null``);
* ``STIRA_BROWSER_CHECK=0`` (opt out, e.g. a slow machine).

``STIRA_REQUIRE_BROWSER=1`` turns "no browser" into a failure (as ``STIRA_REQUIRE_AXE`` does).
The server port is ``STIRA_PORT`` when set, otherwise a free one. About 30 s.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_board_svg_browser.mjs"

# The checks that make acceptance criteria 3 and 8 of #453 (so a script edit cannot drop them).
REQUIRED_ON = (
    "csp header unchanged",
    "no element of the SVG in the page DOM (svg/script/foreignObject/iframe)",
    "canary untouched",
    "every picture is our data: SVG, decoded",
    "coffee cup pixels (white cup, brown coffee, grey saucer, clear corner)",
    "bar chart pixels (blue bar, orange bar, white gap)",
    "diagram pixels (three boxes, dark arrow)",
    "hostile picture: only the green rect (no style / set / animate fill)",
    "re-reading every <img> SVG in the browser: nothing outside the allow-list",
    "canary still untouched after DOMParser",
    "no request to any probe URL (whole session)",
    "no request other than the page APIs while the reply was shown",
    "no console error / exception while the reply was shown",
    "every security log entry is a CSP report (the policy working)",
    "no dialog opened",
    "control: the raw SVG in the DOM does reach the probes (the monitor works)",
    "log: the reply is drawn without pictures (SVG blocks stay code)",
)
REQUIRED_OFF = (
    "off: no picture, SVG blocks stay code",
    "no request to any probe URL (whole session)",
    "canary untouched",
    "log: the reply is drawn without pictures (SVG blocks stay code)",
)


def _skip_or_fail(reason: str):
    if os.environ.get("STIRA_REQUIRE_BROWSER") == "1":
        pytest.fail(f"STIRA_REQUIRE_BROWSER=1 but {reason}")
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def browser_env(tmp_path):
    if os.environ.get("STIRA_BROWSER_CHECK") == "0":
        pytest.skip("STIRA_BROWSER_CHECK=0")
    if sys.platform == "win32":
        _skip_or_fail("the browser check is POSIX only (Chrome over pipes on fds 3/4)")
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found")
    found = subprocess.run(
        [node, str(SCRIPT), "--which-chrome"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
        check=False,
    ).stdout.strip()
    if not found:
        _skip_or_fail("no Chrome / Chromium found (set CHROME to its executable)")
    env = {
        **os.environ,
        "CHROME": found,
        "PYTHON": sys.executable,
        "SHOTS": "0",
        "STIRA_PORT": os.environ.get("STIRA_PORT") or str(_free_port()),
    }
    return node, env, tmp_path / "out"


def test_board_svg_in_a_real_browser(browser_env):
    node, env, out = browser_env
    run = subprocess.run(
        [node, str(SCRIPT), str(out)],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=ROOT,
        env=env,
        check=False,
    )
    try:
        report = json.loads(run.stdout)
    except ValueError:
        pytest.fail(f"no report (exit {run.returncode}): {run.stdout[-1500:]}{run.stderr[-1500:]}")
    assert report["failed"] == [], report["failed"]
    assert run.returncode == 0, run.stderr[-1500:]
    on, off = report["reports"]
    assert (on["label"], off["label"]) == ("on", "off")
    for name in REQUIRED_ON:
        assert on["checks"].get(name) == "ok", (name, on["checks"].get(name))
    for name in REQUIRED_OFF:
        assert off["checks"].get(name) == "ok", (name, off["checks"].get(name))
    assert on["requests"]["other"] == [] and off["requests"]["other"] == []
    assert len(on["board"]["imgs"]) == 4 and off["board"]["imgs"] == []
