"""#463 end to end: the real chat server process, its own Bedrock provider against a local fake.

``tests/stira_headless_harness.py`` + ``tests/fake_bedrock_endpoint.py`` (no cloud call, no
credential), prompt caching on (``prompt_cache: true``), so what is measured is the request
the provider really sends: the system blocks with their ``cachePoint`` and the tool list.

Measured: on a fresh install ``propose_procedure`` is Ask (a person's card); approving the
card stages ``procedural/staging/<key>`` only, with the turn id of that chat turn; the
review route approves it (approver ``local:<account>``) and the rule is in the cached
static system block from the next turn on. The static block is byte-identical on the two
turns before the approval and on the two turns after it (one change, at the approval), and
the tool list is identical on all of them.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from stiracore.control import procedural_cli
from tests.fake_bedrock_endpoint import FakeBedrock
from tests.test_headless_file_tools_e2e import Server

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
BODY = "1. Read the target first.\n2. Create new files with editor create.\n3. Read back."


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _chat_requests(fake) -> list[dict]:
    """Request bodies of plain chat turns (the last message is the user's text)."""
    out = []
    for request in fake.requests:
        body = request["body"]
        last = (body.get("messages") or [{}])[-1]
        if any("toolResult" in block for block in last.get("content") or []):
            continue
        out.append(body)
    return out


def _static_block(body: dict) -> str:
    """The system text before the first cachePoint (the cached static part)."""
    texts = []
    for block in body.get("system") or []:
        if "cachePoint" in block:
            return "".join(texts)
        texts.append(block.get("text", ""))
    raise AssertionError(f"no cachePoint in the system blocks: {body.get('system')}")


def _tools(body: dict) -> str:
    return json.dumps((body.get("toolConfig") or {}).get("tools"), sort_keys=True)


@POSIX_ONLY
def test_propose_card_review_and_the_next_turn_prompt(tmp_path, fake):
    server = Server(tmp_path, fake, "devnull", gateway={"prompt_cache": True})
    try:
        rows = json.loads(server.request("GET", "/api/config/tools")[1])["tools"]
        assert next(r for r in rows if r["name"] == "propose_procedure")["policy"] == "ask"
        args = {"key": "safe_edit", "body": BODY, "rationale": "seen in two turns"}
        out = server.turn(args, "approve", tool="propose_procedure")
        card = out["card"]
        assert card is not None and card["tool_input"]["body"] == BODY
        result = fake.tool_results()[-1]
        assert result["status"] == "success", result
        memory = server.data_dir / "memory"
        written = sorted(p.relative_to(memory).as_posix() for p in memory.rglob("*.md"))
        procedural = [p for p in written if "/procedural/" in p]
        assert procedural == ["agents/stira/memory/procedural/staging/safe_edit.md"], written
        review = json.loads(server.request("GET", "/api/memory/procedural/review")[1])
        proposal = review["proposals"][0]
        assert proposal["source_turn"] == card["turn_id"]
        assert proposal["sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
        assert review["effective"] == "next_turn"

        before = len(_chat_requests(fake))
        for _ in range(2):
            assert server.request("POST", "/api/chat", {"message": "hello"})[0] == 200
        status, text = server.request(
            "POST",
            "/api/memory/procedural/proposals/safe_edit/approve",
            {"sha256": proposal["sha256"], "confirm": True},
        )
        assert status == 200, text
        approved = json.loads(text)
        assert approved["approved_by"] == procedural_cli.operator_principal()
        assert approved["effective"] == "next_turn"
        for _ in range(2):
            assert server.request("POST", "/api/chat", {"message": "hello"})[0] == 200
        turns = _chat_requests(fake)[before:]
        assert len(turns) == 4
        statics = [_static_block(body) for body in turns]
        assert statics[0] == statics[1] and statics[2] == statics[3]
        assert statics[1] != statics[2]
        assert "Create new files with editor create" not in statics[1]
        assert "Create new files with editor create" in statics[2]
        assert len({_tools(body) for body in turns}) == 1
        print(
            "MEASURED static block chars:",
            len(statics[0]),
            "->",
            len(statics[2]),
            "| tool list unchanged:",
            len({_tools(body) for body in turns}) == 1,
        )
    finally:
        server.stop()
