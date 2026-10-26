"""Headless chat server with ``STIRA_ENABLE_EXEC_TOOLS=1``: exec tools run only when approved (#362).

The real entry point (``tests/stira_headless_harness.py``) with stdin ``/dev/null`` and
``BYPASS_TOOL_CONSENT=true`` / ``STRANDS_NON_INTERACTIVE=true`` in its environment (both
ignored by Stira). The model is the server's own ``BedrockModel`` against
``tests/fake_bedrock_endpoint.py`` (no cloud call); the inner agents of ``think``,
``use_agent``, ``swarm`` and ``graph`` use the same fake. ``cron`` sees a fake ``crontab``
first on ``PATH`` that writes a file in the test directory (the real crontab is never
touched); ``shell`` runs harmless commands in a sandbox directory.

Measured for every exposed exec tool: Ask rule → a card, nothing read from the terminal;
approve → run once (``Command execution cancelled by user`` before #362); reject → not
run; WARNING breaker → refused without a card; CRITICAL (``halt``) → a card, reject → not
run. A ``shell`` call inside an approved ``batch`` needs its own card.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.fake_bedrock_endpoint import FakeBedrock, offline_env, strip_cloud_env

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "stira_headless_harness.py"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process/stdin semantics")
EXEC_TOOLS = ("shell", "cron", "batch", "use_agent", "think", "swarm", "graph")
# Both upstream consent switches; Stira ignores them (#242, #362).
CONSENT_ENV = {"BYPASS_TOOL_CONSENT": "true", "STRANDS_NON_INTERACTIVE": "true"}
CRONTAB = """#!/bin/sh
# Test stand-in for crontab(1): reads/writes $STIRA_TEST_CRONTAB only.
if [ "$1" = "-l" ]; then
  if [ -f "$STIRA_TEST_CRONTAB" ]; then cat "$STIRA_TEST_CRONTAB"; exit 0; fi
  echo "no crontab for test" >&2; exit 1
fi
if [ "$1" = "-" ]; then cat > "$STIRA_TEST_CRONTAB"; exit 0; fi
exit 2
"""


def _free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in (5555, 5612):  # ports of other local servers
            return port


class Server:
    def __init__(
        self,
        tmp_path: Path,
        fake: FakeBedrock,
        stability: float | None = None,
        consent_env: dict[str, str] | None = None,
    ):
        consent_env = CONSENT_ENV if consent_env is None else consent_env
        self.root = tmp_path
        self.data_dir = tmp_path / "data"
        self.sandbox = tmp_path / "sandbox"
        self.sandbox.mkdir(exist_ok=True)
        self.crontab = tmp_path / "crontab.txt"
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir(exist_ok=True)
        (fakebin / "crontab").write_text(CRONTAB, encoding="utf-8")
        (fakebin / "crontab").chmod(0o755)
        path = os.pathsep.join([str(fakebin), os.environ.get("PATH", "")])
        assert shutil.which("crontab", path=path) == str(fakebin / "crontab")
        if stability is not None:
            state_file = self.data_dir / "neural-state.json"
            state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
            state_file.write_text(json.dumps({**state, "stability": stability}), "utf-8")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = tmp_path / "server.log"
        inherited = strip_cloud_env(dict(os.environ))
        for name in CONSENT_ENV:
            inherited.pop(name, None)  # plain_env really has neither switch (review m)
        env = {
            **inherited,
            **offline_env(fake.url),
            "PATH": path,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
            "STIRA_PORT": str(self.port),
            "STIRA_HEARTBEAT": "0",
            "STIRA_ENABLE_EXEC_TOOLS": "1",
            "STIRA_TEST_CRONTAB": str(self.crontab),
            **consent_env,
            "PYTHONUNBUFFERED": "1",
        }
        for name in ("STIRA_SCHEDULER", "STIRA_PROMPT_CACHE"):
            env.pop(name, None)
        self.process = subprocess.Popen(
            [sys.executable, str(HARNESS), str(self.data_dir)],
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
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

    def rules(self) -> dict[str, str]:
        tools = json.loads(self.request("GET", "/api/config/tools")[1])["tools"]
        return {row["name"]: row["policy"] for row in tools}

    def set_rule(self, name: str, policy: str) -> None:
        body = {"name": name, "policy": policy, "confirm": True}
        status, text = self.request("POST", "/api/config/tools", body)
        assert status == 200, text

    def breaker(self) -> str:
        status = json.loads(self.request("GET", "/api/gateway/status")[1])
        return str(status["breaker"]["level"]).lower()

    def turn(self, tool: str, args: dict, decisions: list[str]) -> list[dict]:
        """One chat turn whose model calls ``tool(args)``; decide each card in order."""
        out: dict = {}

        def chat() -> None:
            message = f"CALL {tool} " + json.dumps(args, ensure_ascii=False)
            out["status"], out["body"] = self.request("POST", "/api/chat", {"message": message})

        worker = threading.Thread(target=chat)
        worker.start()
        cards: list[dict] = []
        seen: set[str] = set()
        deadline = time.monotonic() + 60
        while worker.is_alive() and time.monotonic() < deadline:
            pending = json.loads(self.request("GET", "/api/hitl/pending")[1])["requests"]
            card = next((r for r in pending if r["id"] not in seen), None)
            if card is None:
                time.sleep(0.1)
                continue
            seen.add(card["id"])
            cards.append(card)
            decision = decisions[len(cards) - 1] if len(cards) <= len(decisions) else "reject"
            body = {"action": decision}
            if decision != "reject":
                body["args_hash"] = card["args_hash"]
            status, text = self.request("POST", f"/api/hitl/{card['id']}/decision", body)
            assert status == 202, text
        worker.join(120)
        assert not worker.is_alive(), "turn did not finish"
        assert out["status"] == 200, out
        return cards

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _text(result: dict) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []))


def _outer_result(fake: FakeBedrock, tool: str, start: int) -> dict:
    """The tool result the outer agent sent back for its call of ``tool`` (after ``start``)."""
    for request in fake.requests[start:]:
        messages = request["body"].get("messages") or []
        if len(messages) < 2:
            continue
        uses = {
            block["toolUse"]["toolUseId"]
            for block in messages[-2].get("content", [])
            if "toolUse" in block and block["toolUse"].get("name") == tool
        }
        for block in messages[-1].get("content", []):
            if "toolResult" in block and block["toolResult"].get("toolUseId") in uses:
                return block["toolResult"]
    raise AssertionError(f"no tool result for {tool}")


def _system_seen(fake: FakeBedrock, marker: str, start: int) -> int:
    """Requests of an agent whose own system prompt starts with ``marker`` (a child).

    Starts-with, not contains: the chat server's system prompt quotes recent episodes, which
    can hold the marker from an earlier turn's arguments.
    """
    count = 0
    for request in fake.requests[start:]:
        system = request["body"].get("system") or []
        texts = [block.get("text", "") for block in system if isinstance(block, dict)]
        count += any(text.startswith(marker) for text in texts)
    return count


def _count(path: Path) -> int:
    return len(path.read_text("utf-8")) if path.exists() else 0


def _scenarios(server: Server, tag: str) -> dict[str, dict]:
    """Per exec tool: its turns (args, cards to approve) and how many times it ran.

    Every count is exact (review M2): ``shell`` appends one byte per run, ``cron`` one
    crontab line, ``batch`` runs one inner ``shell`` (approved on its own card), the
    agent tools count requests carrying the child's own system prompt, and ``graph`` is
    created and then executed (one node request).
    """
    sandbox = server.sandbox

    def append(name: str) -> dict:
        return {"command": f"printf x >> {name}", "work_dir": str(sandbox)}

    def seen(marker: str):
        return lambda fake, start: _system_seen(fake, marker, start)

    graph_id = f"g362{tag}"
    return {
        "shell": {
            "turns": [(append(f"shell-{tag}.txt"), 1)],
            "count": lambda fake, start: _count(sandbox / f"shell-{tag}.txt"),
        },
        "cron": {
            "turns": [({"action": "add", "schedule": "0 0 1 1 *", "command": f"true {tag}"}, 1)],
            "count": lambda fake, start: (
                server.crontab.read_text("utf-8").count(f"true {tag}\n")
                if server.crontab.exists()
                else 0
            ),
        },
        "batch": {
            "turns": [
                ({"invocations": [{"name": "shell", "arguments": append(f"batch-{tag}.txt")}]}, 2)
            ],
            "count": lambda fake, start: _count(sandbox / f"batch-{tag}.txt"),
        },
        "use_agent": {
            "turns": [({"prompt": "hello", "system_prompt": f"M362-use_agent-{tag}"}, 1)],
            "count": seen(f"M362-use_agent-{tag}"),
        },
        "think": {
            "turns": [
                ({"thought": "hello", "cycle_count": 1, "system_prompt": f"M362-think-{tag}"}, 1)
            ],
            "count": seen(f"M362-think-{tag}"),
        },
        "swarm": {
            "turns": [
                (
                    {
                        "task": "hello",
                        "agents": [{"name": "solo", "system_prompt": f"M362-swarm-{tag}"}],
                    },
                    1,
                )
            ],
            "count": seen(f"M362-swarm-{tag}"),
        },
        "graph": {
            "turns": [
                (
                    {
                        "action": "create",
                        "graph_id": graph_id,
                        "topology": {
                            "nodes": [
                                {"id": "n1", "role": "r", "system_prompt": f"M362-graph-{tag}"}
                            ],
                            "edges": [],
                        },
                    },
                    1,
                ),
                ({"action": "execute", "graph_id": graph_id, "task": "hello"}, 1),
            ],
            "count": seen(f"M362-graph-{tag}"),
        },
    }


def _run(server: Server, fake: FakeBedrock, tool: str, tag: str, decision: str):
    """Run ``tool``'s scenario; ``reject`` stops after the first (rejected) turn."""
    scenario = _scenarios(server, tag)[tool]
    start = len(fake.requests)
    cards: list[dict] = []
    result: dict = {}
    for args, approvals in scenario["turns"]:
        turn_start = len(fake.requests)
        cards += server.turn(tool, args, [decision] * approvals)
        result = _outer_result(fake, tool, turn_start)
        if decision == "reject":
            break
    return cards, result, scenario["count"](fake, start)


@POSIX_ONLY
@pytest.mark.parametrize("consent_env", [{}, CONSENT_ENV], ids=["plain_env", "bypass_env"])
def test_exec_tools_run_only_when_a_human_approves(tmp_path, fake, consent_env):
    server = Server(tmp_path, fake, consent_env=consent_env)
    try:
        rules = server.rules()
        exposed = [name for name in EXEC_TOOLS if name in rules]
        assert exposed == list(EXEC_TOOLS), rules  # all bridged, none dropped (#362)
        for name in exposed:
            assert rules[name] == "never"  # fresh install: exec tools are Never
            server.set_rule(name, "ask")
        outcomes = {}
        for name in exposed:
            for decision in ("approve", "reject"):
                cards, result, runs = _run(server, fake, name, decision[:2], decision)
                outcomes[(name, decision)] = (
                    [c["tool_name"] for c in cards],
                    result["status"],
                    runs,
                    _text(result),
                )
        table = {key: value[:3] for key, value in outcomes.items()}
        print("E2E-362 normal", json.dumps({f"{k[0]}:{k[1]}": v for k, v in table.items()}))
        approved_cards = {"batch": ["batch", "shell"], "graph": ["graph", "graph"]}
        expected = {
            **{(n, "approve"): (approved_cards.get(n, [n]), "success", 1) for n in exposed},
            **{(n, "reject"): ([n], "error", 0) for n in exposed},
        }
        assert table == expected, outcomes
        for name in exposed:
            text = outcomes[(name, "approve")][3]
            assert "cancelled by user" not in text.lower(), (name, text)
            text = outcomes[(name, "reject")][3]
            assert "HITL rejected" in text, (name, text)
        # A shell call inside an approved batch is a call of its own: rejecting its
        # card runs nothing.
        inner = {"command": "printf x >> inner.txt", "work_dir": str(server.sandbox)}
        batch = {"invocations": [{"name": "shell", "arguments": inner}]}
        cards = server.turn("batch", batch, ["approve", "reject"])
        assert [c["tool_name"] for c in cards] == ["batch", "shell"]
        assert _count(server.sandbox / "inner.txt") == 0
        log = server.log.read_text("utf-8")
        assert "[y/*]" not in log  # the terminal was never asked
        assert server.process.poll() is None, "server died"
    finally:
        server.stop()


@POSIX_ONLY
@pytest.mark.parametrize(("stability", "level"), [(0.4, "warning"), (0.2, "critical")])
def test_exec_tools_refused_while_the_breaker_is_active(tmp_path, fake, stability, level):
    server = Server(tmp_path, fake)
    try:
        for name in EXEC_TOOLS:
            server.set_rule(name, "ask")  # loosening needs a NORMAL breaker (ADR 0010 §4)
    finally:
        server.stop()
    server = Server(tmp_path, fake, stability=stability)
    try:
        assert server.breaker() == level
        assert all(server.rules()[name] == "ask" for name in EXEC_TOOLS)
        outcomes = {}
        for name in EXEC_TOOLS:
            for decision in ("reject", "approve"):
                if server.breaker() != level:
                    # Turns move the state (a refused call is a stimulus): start at it again.
                    server.stop()
                    server = Server(tmp_path, fake, stability=stability)
                before = server.breaker()
                cards, result, runs = _run(server, fake, name, level[:4] + decision[0], decision)
                outcomes[(name, decision)] = (
                    before,
                    len(cards),
                    result["status"],
                    runs,
                    _text(result)[:160],
                )
        print("E2E-362", level, json.dumps({f"{k[0]}:{k[1]}": v for k, v in outcomes.items()}))
        for name in EXEC_TOOLS:
            assert {outcomes[(name, d)][0] for d in ("reject", "approve")} == {level}
            _, count, status, runs, text = outcomes[(name, "reject")]
            assert (status, runs) == ("error", 0), (name, text)
            # WARNING refuses write/exec tools without a card.
            assert count == (0 if level == "warning" else 1), (name, text)
            _, count, status, runs, text = outcomes[(name, "approve")]
            if level == "warning":
                assert (count, status, runs) == (0, "error", 0), (name, text)
                assert "blocked by the circuit breaker" in text, (name, text)
            else:
                # CRITICAL with critical_action="halt" asks a human for every write/exec
                # call (ADR 0010 §2): only that approval runs it, once.
                assert (status, runs) == ("success", 1), (name, text)
    finally:
        server.stop()


def _child_results(fake: FakeBedrock, marker: str, start: int) -> list[dict]:
    """Tool results inside requests of the agent whose system prompt starts with ``marker``."""
    found = []
    for request in fake.requests[start:]:
        system = request["body"].get("system") or []
        if not any(
            isinstance(block, dict) and block.get("text", "").startswith(marker) for block in system
        ):
            continue
        last = (request["body"].get("messages") or [{}])[-1]
        found += [b["toolResult"] for b in last.get("content", []) if "toolResult" in b]
    return found


def _parent_turns(parent: str, child: str, marker: str, call: str) -> list[dict]:
    """The approved parent's turns whose child (system prompt ``child``) asks for ``call``."""
    tools = ["shell", "use_agent", "batch", "cron"]
    if parent == "think":
        return [{"thought": call, "cycle_count": 1, "system_prompt": child, "tools": tools}]
    if parent == "use_agent":
        return [{"prompt": call, "system_prompt": child, "tools": tools}]
    if parent == "swarm":
        return [{"task": call, "agents": [{"name": "c", "system_prompt": child, "tools": tools}]}]
    graph_id = f"gc{marker}"
    node = {"id": "n1", "role": "r", "system_prompt": child, "tools": tools}
    return [
        {"action": "create", "graph_id": graph_id, "topology": {"nodes": [node], "edges": []}},
        {"action": "execute", "graph_id": graph_id, "task": call},
    ]


@POSIX_ONLY
def test_children_of_approved_exec_tools_cannot_run_exec_tools(tmp_path, fake):
    """Review B (#145 boundary): a human approves think/use_agent/swarm/graph; its child
    agent (the parent's model and tools, none of Stira's hooks) then calls ``shell``,
    ``cron``, ``batch`` (no upstream prompt) and ``use_agent`` (a grandchild agent). The
    child has no approval route: no card appears, nothing runs, no grandchild starts."""
    server = Server(tmp_path, fake)
    try:
        for name in EXEC_TOOLS:
            server.set_rule(name, "ask")
        outcomes = {}
        for parent in ("think", "use_agent", "swarm", "graph"):
            for target in ("shell", "cron", "batch", "use_agent"):
                tag = f"{parent}-{target}".replace("_", "")
                child = f"M362-child-{tag}"
                marker_file = server.sandbox / f"child-{tag}.txt"
                args = {
                    "shell": {
                        "command": f"printf x >> {marker_file.name}",
                        "work_dir": str(server.sandbox),
                    },
                    "cron": {"action": "add", "schedule": "0 0 1 1 *", "command": f"true {tag}"},
                    "batch": {
                        "invocations": [
                            {
                                "name": "shell",
                                "arguments": {
                                    "command": f"printf x >> {marker_file.name}",
                                    "work_dir": str(server.sandbox),
                                },
                            }
                        ]
                    },
                    "use_agent": {"prompt": "hi", "system_prompt": f"M362-grandchild-{tag}"},
                }[target]
                call = f"CHILDCALL {target} {json.dumps(args)} ENDCALL"
                start = len(fake.requests)
                cards = []
                for turn_args in _parent_turns(parent, child, tag, call):
                    cards += server.turn(parent, turn_args, ["approve"])
                inner = _child_results(fake, child, start)
                outcomes[(parent, target)] = {
                    "cards": [c["tool_name"] for c in cards],
                    "child_calls": [r["status"] for r in inner],
                    "child_text": [_text(r)[:120] for r in inner],
                    "file": _count(marker_file),
                    "cron": server.crontab.exists()
                    and f"true {tag}" in server.crontab.read_text("utf-8"),
                    "grandchild": _system_seen(fake, f"M362-grandchild-{tag}", start),
                }
        print("E2E-362 children", json.dumps({f"{k[0]}>{k[1]}": v for k, v in outcomes.items()}))
        for (parent, target), row in outcomes.items():
            expected_cards = [parent] * (2 if parent == "graph" else 1)
            assert row["cards"] == expected_cards, (parent, target, row)  # no card for the child
            assert row["child_calls"] == ["error"], (parent, target, row)
            assert "no Stira gate approval" in row["child_text"][0], (parent, target, row)
            assert (row["file"], row["cron"], row["grandchild"]) == (0, False, 0), row
        assert "[y/*]" not in server.log.read_text("utf-8")
    finally:
        server.stop()
