"""Headless chat server process: file tools decided by Stira's approval policy only (#242, #243).

The real entry point (``tests/stira_headless_harness.py``) with stdin ``/dev/null``
(as ``scripts/stira_ctl.sh start``) or an open pipe nobody writes to, and
``BYPASS_TOOL_CONSENT=true`` in its environment. The model is the server's own
``BedrockModel`` against ``tests/fake_bedrock_endpoint.py`` (no cloud call), so the
upstream consent bridge, the gate stack and the approval cards run as in production.

Measured (both stdin kinds): fresh-install rule Ask → a card, nothing read from the
terminal; approve → written once; reject → nothing written; Never → refused at the call
without a card; Always → written without a card. The #243 bypass spellings (case,
NFD, ``..``, symlink, hard link) are refused at Always without a card and create
nothing.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.fake_bedrock_endpoint import FakeBedrock, offline_env, strip_cloud_env

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "stira_headless_harness.py"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process/stdin semantics")
# NFC "データ" differs from its NFD spelling; macOS keeps either as given (APFS
# compares them as one name), so a data directory under such a home is reachable twice.
DATA_NAME = unicodedata.normalize("NFC", "データ-é")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(
        self, tmp_path: Path, fake: FakeBedrock, stdin_kind: str, gateway: dict | None = None
    ) -> None:
        if gateway is not None:  # the file the server reads from its working directory
            config = tmp_path / ".local" / "stira" / "gateway-config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps(gateway), encoding="utf-8")
            # The harness' settings store reads the data directory's copy (production: one file).
            (tmp_path / DATA_NAME).mkdir()
            (tmp_path / DATA_NAME / "gateway-config.json").write_text(
                json.dumps(gateway), encoding="utf-8"
            )
        self.data_dir = tmp_path / DATA_NAME
        self.workspace = self.data_dir / "workspace"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = tmp_path / "server.log"
        env = {
            **strip_cloud_env(dict(os.environ)),
            **offline_env(fake.url),
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            "STIRA_PORT": str(self.port),
            "STIRA_HEARTBEAT": "0",
            "BYPASS_TOOL_CONSENT": "true",
            "PYTHONUNBUFFERED": "1",
        }
        for name in ("STIRA_SCHEDULER", "STIRA_PROMPT_CACHE", "STIRA_ENABLE_EXEC_TOOLS"):
            env.pop(name, None)
        self.process = subprocess.Popen(
            [sys.executable, str(HARNESS), str(self.data_dir)],
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL if stdin_kind == "devnull" else subprocess.PIPE,
            stdout=self.log.open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("server exited:\n" + self.log.read_text("utf-8")[-3000:])
            try:
                if self.request("GET", "/api/gateway/status")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("server did not start:\n" + self.log.read_text("utf-8")[-3000:])
        self.token = json.loads(self.request("GET", "/api/csrf")[1])["token"]

    def request(self, method: str, path: str, body=None, timeout: float = 120):
        req = urllib.request.Request(self.base + path, method=method)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
            req.add_header("X-CSRF-Token", getattr(self, "token", ""))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def set_rule(self, policy: str) -> None:
        body = {"name": "file_write", "policy": policy}
        if policy != "never":
            # Loosening is the operator's explicit, typed confirmation (#190).
            body.update(confirm=True, confirm_always=["file_write"])
        status, body = self.request("POST", "/api/config/tools", body)
        assert status == 200, body

    def rule(self) -> str:
        tools = json.loads(self.request("GET", "/api/config/tools")[1])["tools"]
        return next(row for row in tools if row["name"] == "file_write")["policy"]

    def turn(self, args: dict, decision: str | None, tool: str = "file_write") -> dict:
        """One chat turn whose model calls ``tool(args)``; decide its card, if any."""
        out: dict = {}

        def chat() -> None:
            message = f"CALL {tool} " + json.dumps(args, ensure_ascii=False)
            out["status"], out["body"] = self.request("POST", "/api/chat", {"message": message})

        worker = threading.Thread(target=chat)
        worker.start()
        card = None
        deadline = time.monotonic() + 60
        while card is None and worker.is_alive() and time.monotonic() < deadline:
            pending = json.loads(self.request("GET", "/api/hitl/pending")[1])["requests"]
            card = next((r for r in pending if r["tool_name"] == tool), None)
            time.sleep(0.1)
        if card is not None and decision is not None:
            body = {"action": decision}
            if decision != "reject":
                body["args_hash"] = card["args_hash"]
            status, text = self.request("POST", f"/api/hitl/{card['id']}/decision", body)
            assert status == 202, text
        worker.join(120)
        assert not worker.is_alive(), "turn did not finish"
        assert out["status"] == 200, out
        return {"card": card, "body": out["body"]}

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        if self.process.stdin:
            self.process.stdin.close()


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _start(tmp_path, fake, stdin_kind, gateway=None):
    return Server(tmp_path, fake, stdin_kind, gateway)


def _last_result(fake: FakeBedrock) -> dict:
    results = fake.tool_results()
    assert results, "the model never got a tool result"
    return results[-1]


def _result_text(result: dict) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []))


@POSIX_ONLY
@pytest.mark.parametrize("stdin_kind", ["devnull", "open_pipe"])
def test_headless_server_writes_only_as_the_approval_policy_says(tmp_path, fake, stdin_kind):
    server = _start(tmp_path, fake, stdin_kind)
    try:
        # Fresh install: local writes are Ask (#269).
        assert server.rule() == "ask"
        approved = server.turn({"path": "approved.txt", "content": "approved\n"}, "approve")
        assert approved["card"] is not None and approved["card"]["tool_input"]["path"] == (
            "approved.txt"
        )
        assert _last_result(fake)["status"] == "success", _last_result(fake)
        assert "cancelled" not in _result_text(_last_result(fake)).lower()
        assert (server.workspace / "approved.txt").read_text("utf-8") == "approved\n"

        rejected = server.turn({"path": "rejected.txt", "content": "no\n"}, "reject")
        assert rejected["card"] is not None
        assert not (server.workspace / "rejected.txt").exists()
        assert _last_result(fake)["status"] == "error"
        assert "HITL rejected" in _result_text(_last_result(fake))

        server.set_rule("never")
        never = server.turn({"path": "never.txt", "content": "no\n"}, "approve")
        assert never["card"] is None
        assert not (server.workspace / "never.txt").exists()
        assert _last_result(fake)["status"] == "error"
        assert "Never" in _result_text(_last_result(fake))

        server.set_rule("always")
        always = server.turn({"path": "always.txt", "content": "always\n"}, None)
        assert always["card"] is None
        assert (server.workspace / "always.txt").read_text("utf-8") == "always\n"
        assert _last_result(fake)["status"] == "success"

        # editor (Ask by default): create / str_replace approved, insert rejected.
        note = server.workspace / "note.md"
        server.turn(
            {"command": "create", "path": "note.md", "file_text": "one\ntwo\n"},
            "approve",
            tool="editor",
        )
        assert _last_result(fake)["status"] == "success", _last_result(fake)
        edit = {"command": "str_replace", "path": "note.md", "old_str": "two", "new_str": "TWO"}
        assert server.turn(edit, "approve", tool="editor")["card"] is not None
        assert _last_result(fake)["status"] == "success", _last_result(fake)
        insert = {"command": "insert", "path": "note.md", "insert_line": 1, "new_str": "x"}
        assert server.turn(insert, "reject", tool="editor")["card"] is not None
        assert _last_result(fake)["status"] == "error"
        assert note.read_text("utf-8") == "one\nTWO\n"

        # Exactly the allowed files, each written once (no replay, no stray file).
        assert sorted(p.name for p in server.workspace.iterdir()) == [
            "always.txt",
            "approved.txt",
            "note.md",
        ]
        assert server.process.poll() is None, "server died"
        # The terminal was never asked: the upstream prompt text is not in the log.
        assert "[y/*]" not in server.log.read_text("utf-8")
    finally:
        server.stop()


def _bypass_paths(server: Server) -> dict[str, str]:
    data = server.data_dir
    procedural = data / "memory" / "agents" / "stira" / "memory" / "procedural"
    nfd_data = Path(unicodedata.normalize("NFD", str(data)))
    return {
        "case_procedural": str(procedural.parent / "PROCEDURAL" / "new.md"),
        "case_data_file": str(data / "TOOL-CONFIG.json"),
        "case_workspace": str(data / "WORKSPACE" / "new.txt"),
        "nfd_data_dir": str(nfd_data / "tool-config.json"),
        "nfd_procedural": str(nfd_data / procedural.relative_to(data) / "new.md"),
        "dotdot_config": "../tool-config.json",
        "dotdot_procedural": "../memory/agents/stira/memory/procedural/new.md",
        "dotdot_missing": "missing/../../tool-config.json",
        "symlink_dir": "link-to-data/tool-config.json",
        "symlink_procedural": "link-to-data/memory/agents/stira/memory/procedural/new.md",
        "hard_link": "hard-link.json",
        "hard_link_store": "hard-link.db",
        "outside_roots": str(server.data_dir.parent / "outside.txt"),
    }


MARKER = b"bypass-marker-243\n"


def _files_holding_the_marker(directory: Path) -> list[str]:
    """Files whose whole content is what the bypass calls tried to write."""
    found = []
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == MARKER:
                found.append(str(path))
        except OSError:
            continue
    return found


@POSIX_ONLY
def test_headless_server_refuses_path_bypasses_without_asking(tmp_path, fake):
    server = _start(tmp_path, fake, "devnull")
    try:
        assert unicodedata.is_normalized("NFC", str(server.data_dir))
        (server.workspace / "link-to-data").symlink_to(server.data_dir, target_is_directory=True)
        # Always: nothing but the path policy stands between the call and the disk.
        server.set_rule("always")
        # After the save: the rules file is replaced by a rename, so an older link would
        # name a stale inode. The approval store is written in place.
        os.link(server.data_dir / "tool-config.json", server.workspace / "hard-link.json")
        os.link(server.data_dir / "hitl-approvals.db", server.workspace / "hard-link.db")
        approvals_db = (server.data_dir / "hitl-approvals.db").stat()
        tool_config = (server.data_dir / "tool-config.json").read_bytes()
        outcomes = {}
        for label, path in _bypass_paths(server).items():
            turn = server.turn({"path": path, "content": MARKER.decode()}, "approve")
            result = _last_result(fake)
            outcomes[label] = (turn["card"] is None, result["status"], _result_text(result))
        refused = {label: o for label, o in outcomes.items() if o[:2] != (True, "error")}
        assert refused == {}, refused
        for label, (_, _, text) in outcomes.items():
            assert re.search(r"refused by: (floor|write_roots|input)", text), (label, text)
        assert _files_holding_the_marker(tmp_path) == []
        assert (server.data_dir / "tool-config.json").read_bytes() == tool_config
        assert b"bypass-marker" not in (server.data_dir / "hitl-approvals.db").read_bytes()
        assert (server.data_dir / "hitl-approvals.db").stat().st_ino == approvals_db.st_ino
        procedural = server.data_dir / "memory" / "agents" / "stira" / "memory" / "procedural"
        assert not (procedural / "new.md").exists()
        assert not (server.data_dir.parent / "outside.txt").exists()
        assert not (server.data_dir / "missing").exists()
        # The policy still lets an ordinary workspace write through.
        server.turn({"path": "ok.txt", "content": "ok\n"}, None)
        assert (server.workspace / "ok.txt").read_text("utf-8") == "ok\n"
    finally:
        server.stop()


@POSIX_ONLY
def test_cut_off_file_write_is_never_run_even_under_always(tmp_path, fake):
    """#363: the real server, ``BedrockModel`` and gate stack against a stream that ends in
    ``stopReason: max_tokens`` in the middle of a ``file_write``'s arguments."""
    server = _start(tmp_path, fake, "devnull")
    try:
        server.set_rule("always")  # no card could stop it: only the max_tokens handling does
        # Control: a complete call under Always does run and does show a tool event.
        ran = server.turn({"path": "ok.txt", "content": "ok\n"}, None)
        assert (server.workspace / "ok.txt").read_text("utf-8") == "ok\n"
        assert "tool_call.created" in ran["body"]
        message = 'CUT file_write {"path": "game.html", "content": "<!doctype html><script>let a'
        status, body = server.request("POST", "/api/chat", {"message": message})
        assert status == 200
        assert not (server.workspace / "game.html").exists()
        assert not list(server.workspace.rglob("game.html"))
        assert "tool_call.created" not in body  # the call never reached the tool layer
        error = next(
            json.loads(block.split("data: ", 1)[1])
            for block in body.split("\n\n")
            if block.startswith("event: error")
        )
        assert error["code"] == "max_tokens"
        assert "出力の上限に達して途中で切れました" in error["error"]

        # The cap sent to Bedrock is the automatic one for the default model, not 8192.
        sent = fake.requests[-1]["body"]["inferenceConfig"]["maxTokens"]
        assert sent == 64_000  # 32000 + the thinking reserve of Sonnet 5.5's row (#416)

        # The failed call was billed by Bedrock: its metadata usage is recorded.
        turns = json.loads(server.request("GET", "/api/turns?limit=1")[1])["turns"]
        assert turns[0]["stop"] == "max_tokens" and turns[0]["error"] is True
        assert turns[0]["usage"]["output_tokens"] == sent
        assert turns[0]["usage_reason"] is None
        session = json.loads(server.request("GET", "/api/gateway/status")[1])["session"]
        # the control turn's 5 + 5 (tool call, then answer) plus the cut-off call
        assert session["total_output_tokens"] == sent + 10

        # The conversation goes on, and nothing half-written is left to run.
        status, body = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200 and "event: turn" in body
        assert not list(server.workspace.rglob("game.html"))
    finally:
        server.stop()


@POSIX_ONLY
@pytest.mark.parametrize(
    ("gateway", "expected"),
    [
        ({"max_output_tokens": 12_345}, 12_345),  # pinned in gateway-config.json
        ({"model_id": "vendor.some-new-model-v1"}, 8192),  # automatic, model without a limit
        ({"model_id": "global.anthropic.claude-opus-4-1-20250805-v1:0"}, 32_000),
    ],
)
def test_server_start_up_sends_the_cap_the_config_resolves_to(tmp_path, fake, gateway, expected):
    server = _start(tmp_path, fake, "devnull", gateway)
    try:
        status, body = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200 and "event: turn" in body
        assert fake.requests[-1]["body"]["inferenceConfig"]["maxTokens"] == expected
    finally:
        server.stop()


def _sse_error(body: str) -> dict:
    return next(
        json.loads(block.split("data: ", 1)[1])
        for block in body.split("\n\n")
        if block.startswith("event: error")
    )


@POSIX_ONLY
def test_a_turn_whose_thinking_uses_the_cap_says_so(tmp_path, fake):
    """#416: the real server and ``BedrockModel`` against a stream that spends the cap on a
    reasoning block (adaptive thinking) and stops with ``max_tokens`` before any reply."""
    server = _start(tmp_path, fake, "devnull")
    try:
        status, body = server.request("POST", "/api/chat", {"message": "THINK plan the game"})
        assert status == 200
        error = _sse_error(body)
        sent = fake.requests[-1]["body"]["inferenceConfig"]["maxTokens"]
        assert sent == 64_000  # 32000 + the thinking reserve of the default model's row
        assert (error["code"], error["cut"], error["limit"]) == ("max_tokens", "before_reply", sent)
        assert "返答を書き始める前に出力の上限に達しました" in error["error"]
        turns = json.loads(server.request("GET", "/api/turns?limit=1")[1])["turns"]
        assert turns[0]["stop"] == "max_tokens" and turns[0]["usage"]["output_tokens"] == sent
        # The thinking-only message kept in the history does not break the next turn.
        status, body = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200 and "event: turn" in body and "event: error" not in body
    finally:
        server.stop()


@POSIX_ONLY
@pytest.mark.parametrize(
    ("gateway", "accepted"),
    [
        ({"model_id": "vendor.some-new-model-v1"}, True),  # automatic: the provider's default
        ({"model_id": "vendor.some-new-model-v1", "max_output_tokens": 8193}, False),  # control
    ],
)
def test_unknown_model_default_fits_an_endpoint_that_enforces_its_limit(
    tmp_path, fake, gateway, accepted
):
    """#416: an unknown Bedrock model gets the provider's safe default (8192), which an
    endpoint enforcing an 8192 limit accepts; one token more is refused (the control shows
    the endpoint really enforces it)."""
    fake.limits["vendor.some-new-model-v1"] = 8192
    server = _start(tmp_path, fake, "devnull", gateway)
    try:
        status, body = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200
        assert fake.requests[-1]["path"].endswith("vendor.some-new-model-v1/converse-stream")
        assert ("event: error" not in body) is accepted, body[-500:]
        if accepted:
            assert "event: turn" in body
    finally:
        server.stop()
