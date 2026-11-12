"""#651: the page's own JS shows the cost the server put on the events.

``tests/test_observability_cost_agrees.py`` checks the numbers the server sends. This file
runs the shipped ``observability.js`` (Cost Tracker) and ``chat.js`` (Session panel) in node
on the mock DOM (``tests/observability_cost_dom.cjs``, no jsdom) and reads what they display.

The page has one way in: the event stream (it never fetches the snapshot). Both endpoints
are fed through it here: the events parsed from ``/api/observability/events`` and the
``events`` of ``/api/observability/snapshot`` (the same contract, ``page_event``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from stiracore.observability import chat_api
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.providers.usage import Pricing
from tests.test_observability_cost_agrees import OTHER, SONNET, _app, _completed

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "observability_cost_dom.cjs"


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_api.yolo_runtime, "status", lambda: {"window": None})
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


def _stream(client, count: int) -> list[dict]:
    """The first ``count`` events of the SSE replay, as the browser would parse them."""
    response = client.get("/api/observability/events")
    chunks = iter(response.response)
    events: list[dict] = []
    try:
        while len(events) < count:
            chunk = next(chunks)
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            if chunk.startswith("id:"):
                events.append(json.loads(chunk.split("data: ", 1)[1]))
    finally:
        response.close()
    return events


def _pages(tmp_path, client, count: int) -> dict:
    """What the two pages display for the ``count`` turns recorded (run in node)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    snapshot = client.get("/api/observability/snapshot").get_json()["events"]
    assert len(snapshot) == count
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "feeds": {"sse": _stream(client, count), "snapshot": snapshot},
                "status": client.get("/api/gateway/status").get_json(),
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(SCRIPT), str(case)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"node exit {proc.returncode}:\n{proc.stdout}{proc.stderr}"
    shown = json.loads(proc.stdout)
    for feed in shown["feeds"].values():
        assert feed["log_rows"] == count, "every event reached the page"
    return shown


def test_configured_rates_reach_the_total_and_the_notice(tmp_path, recorder):
    # The chat page counts the turns since the app started: the app first, then the turns.
    client = _app(recorder, {SONNET: Pricing(3.0, 15.0)}).test_client()
    store = recorder.store
    _completed(store, "t1", SONNET, input_tokens=12_000, output_tokens=600)  # 0.045
    _completed(store, "t2", SONNET, input_tokens=300, output_tokens=7)  # 0.001005
    shown = _pages(tmp_path, client, 2)
    for name, feed in shown["feeds"].items():
        assert feed["cost"] == "$0.05", name  # priced: no "*"
        assert feed["models"] == [[SONNET, "$0.05"]], name
        assert feed["notice"] == shown["notices"]["priced"], name
    # The same sum on the chat page. The two pages format it differently (as before #651):
    # the Session panel shows six decimals, the Cost Tracker whole cents.
    assert shown["chat"] == "$0.046005"
    cents = f"${float(shown['chat'][1:]):.2f}"
    assert {feed["cost"] for feed in shown["feeds"].values()} == {cents}


def test_an_unpriced_turn_makes_a_marked_subtotal_never_a_free_one(tmp_path, recorder):
    client = _app(recorder, {SONNET: Pricing(3.0, 15.0)}).test_client()
    store = recorder.store
    _completed(store, "t1", SONNET, input_tokens=1_000_000, output_tokens=0)  # 3.00
    _completed(store, "t2", OTHER, input_tokens=10, output_tokens=1)  # no entry
    _completed(store, "t3", SONNET, input_tokens=10, output_tokens=1, cache_read_tokens=5)
    shown = _pages(tmp_path, client, 3)
    for name, feed in shown["feeds"].items():
        assert feed["cost"] == "$3.00 *", name
        assert feed["models"] == [[OTHER, "$0.00 *"], [SONNET, "$3.00 *"]], name
        assert feed["notice"] == shown["notices"]["unpriced"].replace("{unknown}", "2"), name
    assert shown["chat"] == "—"  # the chat page shows no partial sum


def test_without_rates_every_turn_is_marked_unpriced(tmp_path, recorder):
    client = _app(recorder, {}).test_client()
    _completed(recorder.store, "t1", SONNET, input_tokens=100, output_tokens=10)
    shown = _pages(tmp_path, client, 1)
    for name, feed in shown["feeds"].items():
        assert feed["cost"] == "$0.00 *", name
        assert feed["notice"] == shown["notices"]["unpriced"].replace("{unknown}", "1"), name
    assert shown["chat"] == "—"
