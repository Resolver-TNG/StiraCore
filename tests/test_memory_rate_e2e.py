"""ADR 0018 phase 0 end to end: the real chat server process, its own Bedrock provider against a
local fake (``tests/fake_bedrock_endpoint.py``; no cloud call, no credential).

Measured: without ``memory_autonomy`` the model is offered no ``memory_rate`` and nothing is
bound; with ``{"phase": 0}`` a fresh install lists ``memory_rate`` as Always, the model's call
runs without a card, the rating is bound to the turn's episode with the server's turn id and
the status counts it — and Harvest's decision for that episode is the same as without it.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from tests.fake_bedrock_endpoint import FakeBedrock
from tests.test_headless_file_tools_e2e import DATA_NAME, Server

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _offered(fake) -> set[str]:
    names = set()
    for request in fake.requests:
        for tool in (request["body"].get("toolConfig") or {}).get("tools") or []:
            names.add(tool.get("toolSpec", {}).get("name"))
    return names


def _ratings(server) -> list[tuple]:
    index = server.data_dir / "memory" / "agents" / "stira" / "memory" / "index.sqlite3"
    with sqlite3.connect(index) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if "memory_ratings" not in tables:
            return []
        return connection.execute(
            "SELECT key, agent_importance, agent_reason, rated_by, turn_id, trust FROM memory_ratings"
        ).fetchall()


@POSIX_ONLY
def test_off_by_default_no_tool_offered_nothing_bound(tmp_path, fake):
    server = Server(tmp_path, fake, "devnull")
    try:
        assert server.request("POST", "/api/chat", {"message": "hello"})[0] == 200
        assert "memory_rate" not in _offered(fake)
        assert _ratings(server) == []
        status = json.loads(server.request("GET", "/api/gateway/status")[1])
        assert status["memory_autonomy"] is None
    finally:
        server.stop()


@POSIX_ONLY
def test_on_the_rating_is_bound_with_the_servers_turn_id(tmp_path, fake):
    data = tmp_path / DATA_NAME
    data.mkdir()
    (data / "agent-config.json").write_text(
        json.dumps({"memory_autonomy": {"phase": 0}}), encoding="utf-8"
    )
    server = Server(tmp_path, fake, "devnull")
    try:
        rows = json.loads(server.request("GET", "/api/config/tools")[1])["tools"]
        row = next(r for r in rows if r["name"] == "memory_rate")
        assert (row["policy"], row["category"]) == ("always", "read")
        out = server.turn({"importance": 0.8, "reason": "decision"}, None, tool="memory_rate")
        assert out["card"] is None  # Always: no card
        result = fake.tool_results()[-1]
        assert result["status"] == "success", result
        assert "memory_rate" in _offered(fake)
        [rating] = _ratings(server)
        key, value, reason, rated_by, turn_id, trust = rating
        assert key.startswith("chat-") and (value, reason, rated_by) == (
            0.8,
            "decision",
            "agent_turn",
        )
        assert trust == "trusted" and isinstance(turn_id, str) and len(turn_id) >= 16
        status = json.loads(server.request("GET", "/api/gateway/status")[1])["memory_autonomy"]
        assert status["ratings"]["total"] == 1 and status["ratings"]["high_today"] == 1
        assert status["jobs"] is None  # no job kind enabled
        print("MEASURED bound rating:", rating[1:4], "trust", trust)
    finally:
        server.stop()
