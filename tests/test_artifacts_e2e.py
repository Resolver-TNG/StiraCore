"""#454 (ADR 0020): workspace artifacts on the real chat server process, offline.

``tests/stira_headless_harness.py`` against ``tests/fake_bedrock_endpoint.py`` (the
server's own ``BedrockModel``; no cloud call). Measured: ``file_write`` and ``editor``
creations and changes in the workspace are registered with the turn that made them; a
change of the same path is a new version; ``/api/artifacts`` answers 200 on the running
server with the list, one artifact, one version and a diff; a write through a symlink or
outside the workspace is not registered; no body reaches ``events.db`` or the index.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from tests.fake_bedrock_endpoint import FakeBedrock
from tests.test_headless_file_tools_e2e import POSIX_ONLY, Server

MARKER = "ARTIFACT-BODY-91c4e"


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _always(server, name: str) -> None:
    body = {"name": name, "policy": "always", "confirm": True, "confirm_always": [name]}
    status, text = server.request("POST", "/api/config/tools", body)
    assert status == 200, text


def _get(server, path: str):
    status, text = server.request("GET", path)
    return status, json.loads(text)


def _turn_ids(server) -> list[str]:
    turns = _get(server, "/api/turns?limit=20")[1]["turns"]
    return [t["turn_id"] for t in turns]


@POSIX_ONLY
def test_file_write_and_editor_are_registered_as_versions(tmp_path, fake):
    server = Server(tmp_path, fake, "devnull")
    try:
        _always(server, "file_write")
        _always(server, "editor")
        status, empty = _get(server, "/api/artifacts")
        assert status == 200 and empty["artifacts"] == [] and empty["count"] == 0

        server.turn({"path": "hockey_game.html", "content": f"<h1>{MARKER}</h1>\n"}, None)
        first_turn = _turn_ids(server)[0]
        server.turn(
            {"path": "hockey_game.html", "content": f"<h1>{MARKER}</h1>\n<p>v2</p>\n"}, None
        )
        second_turn = _turn_ids(server)[0]
        server.turn(
            {
                "command": "str_replace",
                "path": "hockey_game.html",
                "old_str": "<p>v2</p>",
                "new_str": "<p>v3</p>",
            },
            None,
            tool="editor",
        )
        third_turn = _turn_ids(server)[0]
        server.turn(
            {"command": "create", "path": "notes/plan.md", "file_text": "# plan\n"},
            None,
            tool="editor",
        )
        assert (server.workspace / "hockey_game.html").read_text("utf-8").endswith("<p>v3</p>\n")

        status, listing = _get(server, "/api/artifacts")
        assert status == 200
        assert [(a["path"], a["kind"], a["version"]) for a in listing["artifacts"]] == [
            ("notes/plan.md", "markdown", 1),
            ("hockey_game.html", "html", 3),
        ]
        game = listing["artifacts"][1]
        status, detail = _get(server, f"/api/artifacts/{game['id']}")
        assert status == 200
        assert [(v["version"], v["op"], v["tool"], v["turn_id"]) for v in detail["versions"]] == [
            (3, "update", "editor", third_turn),
            (2, "update", "file_write", second_turn),
            (1, "create", "file_write", first_turn),
        ]
        status, one = _get(server, f"/api/artifacts/{game['id']}/versions/1")
        assert status == 200 and one["text"] == f"<h1>{MARKER}</h1>\n"
        status, diff = _get(server, f"/api/artifacts/{game['id']}/diff?from=2&to=3")
        assert status == 200 and "-<p>v2</p>" in diff["lines"] and "+<p>v3</p>" in diff["lines"]
        assert (
            _get(server, f"/api/artifacts?turn={first_turn}")[1]["artifacts"][0]["id"]
            == (game["id"])
        )
        assert [
            a["path"] for a in _get(server, "/api/artifacts?kind=markdown")[1]["artifacts"]
        ] == ["notes/plan.md"]
        assert _get(server, f"/api/artifacts/{game['id']}/versions/9")[0] == 404
        assert _get(server, "/api/artifacts/a" + "0" * 20)[0] == 404
        assert _get(server, "/api/artifacts?kind=exe")[0] == 400

        # A symlink inside the workspace (to a file outside it) and a path outside the
        # workspace: neither registered, whatever the write itself did.
        outside = tmp_path / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        os.symlink(outside, server.workspace / "link.txt")
        server.turn({"path": "link.txt", "content": "through the link\n"}, None)
        server.turn({"path": str(tmp_path / "elsewhere.txt"), "content": "x\n"}, None)
        paths = {a["path"] for a in _get(server, "/api/artifacts")[1]["artifacts"]}
        assert paths == {"notes/plan.md", "hockey_game.html"}

        # Facts only outside the snapshots: no body in the event store or the index.
        for name in ("events.db", "artifacts/index.db"):
            database = server.data_dir / name
            if database.exists():
                with sqlite3.connect(database) as db:
                    dump = "\n".join(db.iterdump())
                assert MARKER not in dump, name
    finally:
        server.stop()


@POSIX_ONLY
def test_an_approved_write_is_registered_and_a_rejected_one_is_not(tmp_path, fake):
    """Fresh install: file_write is Ask — a person's approval card first."""
    server = Server(tmp_path, fake, "devnull")
    try:
        approved = server.turn({"path": "ok.txt", "content": "approved\n"}, "approve")
        assert approved["card"] is not None
        rejected = server.turn({"path": "no.txt", "content": "rejected\n"}, "reject")
        assert rejected["card"] is not None
        listing = _get(server, "/api/artifacts")[1]["artifacts"]
        assert [(a["path"], a["version"]) for a in listing] == [("ok.txt", 1)]
    finally:
        server.stop()
