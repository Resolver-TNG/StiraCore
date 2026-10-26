"""#291 (review of #289): where a secret in model text, a tool result or an exception goes.

``callback_handler=None`` only keeps the SDK from printing; it is not masking. This
runs the real ``stira_server`` on the offline FakeModel with three fake secrets —
one in the model's reply, one in a tool result, one in an exception raised by the
model — and checks every output: server log/stderr, stdout, SSE (turn events and the
error body), the turn/event stores, tool payload pages, episodic memory, checkpoints,
chat history, and the Slack/Discord transports.

Outputs marked ``xfail(strict=True)`` are **not masked today** and are the design
decision recorded in #291 (the reply is what the human asked for; a checkpoint must
replay the real conversation). They turn into failures the day they start masking,
so the inventory cannot go stale silently.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from stiracore.core import NeuralState, StiraEngine
from stiracore.observability.masking import MaskingFilter, MaskingFormatter, mask_secrets
from stiracore.tools.memory_store import MemoryLayer
from tests.provider_fakes import FakeModel, text_turn, tool_turn

# Fake credentials in formats the masker knows; none of them is real.
REPLY_SECRET = "ghp_" + "Zq7Xw3Pm" * 5
TOOL_SECRET = "AKIA" + "QWERTYUIOPASDFGH"
ERROR_SECRET = "xoxb-" + "1234567890-abcdefABCDEF1234"
SECRETS = (REPLY_SECRET, TOOL_SECRET, ERROR_SECRET)


def _leaks(text: str) -> list[str]:
    return [secret for secret in SECRETS if secret in text]


def test_the_fake_secrets_are_ones_the_masker_hides():
    for secret in SECRETS:
        assert secret not in mask_secrets(f"value {secret} end")


def _sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def _dump(path) -> str:
    """A file's bytes as text; SQLite databases with every row of every table."""
    text = path.read_bytes().decode("utf-8", "replace")
    if path.suffix == ".db":
        connection = sqlite3.connect(path)
        try:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master")]
            for table in tables:
                with_rows = connection.execute(f'SELECT * FROM "{table}"')
                text += json.dumps([list(map(str, row)) for row in with_rows])
        except sqlite3.DatabaseError:
            pass
        finally:
            connection.close()
    return text


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One server, two turns: a tool turn with a secret reply, then a failing turn."""
    from dashboard import stira_server

    monkeypatch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("masking")
    data = root / "data"
    model = FakeModel(
        [
            tool_turn("memory_read", {"layer": "semantic", "key": "aws"}, "use-1"),
            text_turn(f"your token is {REPLY_SECRET}"),
            [{"raise": RuntimeError(f"upstream said {ERROR_SECRET}")}],
        ]
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setattr(stira_server, "model_kwargs", lambda *a, **k: {"model": model})
    log = io.StringIO()
    factory = logging.getLogRecordFactory()
    # The server's own log wiring (main() does this before the app exists).
    handlers = stira_server._install_log_masking(log)
    try:
        app = stira_server.create_app()
        store = next(
            cell.cell_contents
            for cell in app.view_functions["memory_tree"].__closure__ or ()
            if hasattr(cell.cell_contents, "list_entries")
        )
        store.write(MemoryLayer.SEMANTIC, "aws", f"deploy key {TOOL_SECRET}", {})
        client = app.test_client()
        first = client.post("/api/chat", json={"message": "read it"}).get_data(as_text=True)
        second = client.post("/api/chat", json={"message": "again"}).get_data(as_text=True)
        turns = client.get("/api/turns").json["turns"]
        events = {
            turn["turn_id"]: client.get(f"/api/turns/{turn['turn_id']}/events").json["events"]
            for turn in turns
        }
        payload = client.get(f"/api/turns/{turns[-1]['turn_id']}/tools/use-1/output")
        history = client.get("/api/chat/history").get_data(as_text=True)
        files = {
            str(path.relative_to(data)): _dump(path) for path in data.rglob("*") if path.is_file()
        }
    finally:
        for handler in handlers:
            for name in ("", "werkzeug"):
                logging.getLogger(name).removeHandler(handler)
        # Process-wide wiring: undo it for the rest of the test session.
        logging.setLogRecordFactory(factory)
        loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
        from flask.logging import default_handler

        for handler in {
            default_handler,
            *(h for lg in loggers for h in getattr(lg, "handlers", ())),
        }:
            for masking in [f for f in handler.filters if isinstance(f, MaskingFilter)]:
                handler.removeFilter(masking)
        monkeypatch.undo()
    yield SimpleNamespace(
        model=model,
        first=first,
        second=second,
        events=events,
        payload=payload,
        history=history,
        files=files,
        log=log.getvalue(),
    )


def test_the_scenario_really_carried_every_secret(run):
    """Controls: the model saw the tool secret, and the failing turn was logged."""
    tool_results = json.dumps(run.model.requests[1]["messages"])
    assert TOOL_SECRET in tool_results
    assert "NXPAgent chat failed" in run.log
    assert ("delta", {"text": f"your token is {REPLY_SECRET}"}) in _sse(run.first)


def test_server_log_masks_the_exception_and_its_traceback(run):
    assert "RuntimeError" in run.log
    assert _leaks(run.log) == []


def test_the_client_gets_a_generic_error_never_the_exception_text(run):
    errors = [data for event, data in _sse(run.second) if event == "error"]
    assert errors == [{"error": "応答処理に失敗しました / Response processing failed"}]
    assert _leaks(run.second) == []


def test_turn_events_over_sse_and_the_turn_api_are_masked(run):
    ui_events = [data for event, data in _sse(run.first) if event == "ui_event"]
    assert any(e["type"] == "tool_call.updated" for e in ui_events)
    assert _leaks(json.dumps(ui_events)) == []
    assert _leaks(json.dumps(run.events)) == []


def test_tool_payload_pages_are_masked(run):
    assert run.payload.status_code == 200, run.payload.json
    text = json.dumps(run.payload.json)
    assert "deploy key" in text
    assert _leaks(text) == []


@pytest.mark.parametrize("name", ["turns.db", "events.db"])
def test_turn_and_event_stores_are_masked(run, name):
    assert name in run.files
    assert _leaks(run.files[name]) == []


def test_episodic_memory_written_from_the_turn_is_masked(run):
    episodes = {k: v for k, v in run.files.items() if "/episodic/" in k}
    assert episodes
    assert _leaks(json.dumps(episodes)) == []


DESIGN = "not masked today: design decision recorded in #291"


@pytest.mark.xfail(strict=True, reason=DESIGN)
def test_reply_text_streamed_to_the_chat_is_masked(run):
    deltas = [data for event, data in _sse(run.first) if event == "delta"]
    assert _leaks(json.dumps(deltas)) == []


def test_chat_history_is_masked(run):
    """#291 follow-up: the history shown after a reload is masked (the live reply is not)."""
    assert "your token is" in run.history
    assert _leaks(run.history) == []


def _history_client(reply, memory_store=None):
    from flask import Flask

    from stiracore.observability.chat_api import create_chat_blueprint

    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(object(), runner=lambda _m: reply, memory_store=memory_store)
    )
    return app.test_client()


def test_history_masks_the_message_and_the_reply_but_keeps_the_rest():
    client = _history_client(f"here {REPLY_SECRET} done")
    body = client.post("/api/chat", json={"message": f"key {TOOL_SECRET}"}).get_data(as_text=True)
    # The live reply is what the human asked for (unchanged, documented).
    assert REPLY_SECRET in body
    turns = client.get("/api/chat/history").get_json()["turns"]
    assert [(t["message"], t["response"]) for t in turns] == [
        (mask_secrets(f"key {TOOL_SECRET}"), mask_secrets(f"here {REPLY_SECRET} done"))
    ]
    assert _leaks(json.dumps(turns)) == []
    assert "input_tokens" in turns[0]  # usage fields untouched


class _TurnStore:
    """An external memory_store (library API): append_turn / get_recent_turns."""

    def __init__(self, preloaded=()):
        self.turns = list(preloaded)

    def append_turn(self, turn):
        self.turns.append(turn)

    def get_recent_turns(self, limit=50):
        return self.turns[-limit:]


def test_turns_handed_to_an_external_memory_store_are_masked():
    store = _TurnStore()
    client = _history_client(f"here {REPLY_SECRET}", memory_store=store)
    client.post("/api/chat", json={"message": f"key {TOOL_SECRET}"}).get_data(as_text=True)
    assert len(store.turns) == 1
    assert _leaks(json.dumps(store.turns)) == []


def test_history_masks_turns_an_external_store_already_held():
    """Turns stored before this change (or by another writer) are masked on the way out."""
    store = _TurnStore([{"message": "old", "response": f"was {ERROR_SECRET}"}])
    turns = _history_client("x", memory_store=store).get("/api/chat/history").get_json()
    assert turns["turns"] == [{"message": "old", "response": mask_secrets(f"was {ERROR_SECRET}")}]
    assert _leaks(json.dumps(turns)) == []


@pytest.mark.xfail(strict=True, reason=DESIGN)
def test_checkpoints_are_masked(run):
    checkpoints = {k: v for k, v in run.files.items() if k.startswith("checkpoints/")}
    assert checkpoints
    assert _leaks(json.dumps(checkpoints)) == []


def test_no_other_data_file_holds_a_secret(run):
    """Every other file: only the documented exceptions above may hold one."""
    documented = ("checkpoints/", "memory/agents/stira/memory/semantic/")
    leaking = {
        name: _leaks(text)
        for name, text in run.files.items()
        if _leaks(text) and not name.startswith(documented)
    }
    # memory/.../semantic holds the seeded entry itself (the tool's source, not an output).
    assert leaking == {}


# --- chat transports ----------------------------------------------------------------


def _session(reply):
    from stiracore.integrations.runtime import ChatSession, ChatSessions

    state = NeuralState()
    agent = Mock(state=state, engine=StiraEngine(state=state), side_effect=reply)
    return ChatSessions("test", factory=Mock(return_value=ChatSession(agent)))


class _SlackApp:
    def __init__(self):
        self.events = {}

    def event(self, name):
        return lambda handler: self.events.setdefault(name, handler)

    def command(self, name):
        return lambda handler: handler


def _slack(reply, caplog):
    from stiracore.integrations import slack_bot

    app = _SlackApp()
    slack_bot.register_handlers(app, _session(reply))
    say = Mock()
    with caplog.at_level(logging.DEBUG):
        app.events["message"](
            event={"channel": "C1", "ts": "1.0", "user": "U1", "text": "hi"},
            say=say,
            body={"team_id": "T1"},
        )
    return json.dumps([str(call) for call in say.call_args_list])


def _discord(reply, caplog):
    from stiracore.integrations import discord_bot
    from tests.test_discord_bot import fake_discord

    client = discord_bot.create_client(fake_discord(), _session(reply))
    channel = SimpleNamespace(send=AsyncMock(), id=1)
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False), content="hi", channel=channel, guild=None
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(client.on_message(message))
    return json.dumps([str(call) for call in channel.send.call_args_list])


def _raise(_text):
    raise RuntimeError(f"upstream said {ERROR_SECRET}")


def test_chat_bot_episodes_are_masked(tmp_path):
    """The Slack/Discord session's own store (episodic memory) holds no secret."""
    from stiracore.integrations.runtime import ChatSession
    from stiracore.tools.memory_store import MemoryStore

    state = NeuralState()
    agent = Mock(state=state, engine=StiraEngine(state=state))
    agent.side_effect = lambda _text: f"token {REPLY_SECRET}"
    store = MemoryStore(tmp_path / "memory", "slack")
    session = ChatSession(agent, store=store, source="slack", emotion=False)
    result = session.runner(f"my key {TOOL_SECRET}")
    assert REPLY_SECRET in result.text  # the reply itself: documented as not masked
    files = [p for p in (tmp_path / "memory").rglob("*") if p.is_file()]
    assert any("episodic" in str(p) for p in files)
    assert _leaks("".join(p.read_text("utf-8", "replace") for p in files)) == []


def test_slack_never_sends_or_logs_the_exception_text(caplog):
    sent = _slack(_raise, caplog)
    assert "RuntimeError" in caplog.text  # the type only
    assert _leaks(sent + caplog.text) == []


def test_discord_never_sends_or_logs_the_exception_text(caplog):
    sent = _discord(_raise, caplog)
    assert "RuntimeError" in caplog.text
    assert _leaks(sent + caplog.text) == []


@pytest.mark.xfail(strict=True, reason=DESIGN)
def test_slack_reply_text_is_masked(caplog):
    assert _leaks(_slack(lambda _text: f"token {REPLY_SECRET}", caplog)) == []


@pytest.mark.xfail(strict=True, reason=DESIGN)
def test_discord_reply_text_is_masked(caplog):
    assert _leaks(_discord(lambda _text: f"token {REPLY_SECRET}", caplog)) == []


def test_masking_formatter_masks_message_arguments_and_traceback():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(MaskingFormatter("%(message)s"))
    logger = logging.getLogger("stira.test.masking")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise ValueError(f"bad {ERROR_SECRET}")
        except ValueError:
            logger.exception("failed with %s", REPLY_SECRET)
    finally:
        logger.removeHandler(handler)
    assert "ValueError" in stream.getvalue()
    assert _leaks(stream.getvalue()) == []


# --- review round (#330): handlers registered before or after the server's wiring ----

_BASIC_CONFIG_SCRIPT = r"""
import io, logging, sys
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(message)s")
before = logging.StreamHandler(sys.stdout)            # a library's own handler
logging.getLogger("lib.before").addHandler(before)
logging.getLogger("lib.before").propagate = False
from dashboard import stira_server
stira_server._install_log_masking(io.StringIO())
after = logging.StreamHandler(sys.stdout)             # registered after the wiring
logging.getLogger("lib.after").addHandler(after)
logging.getLogger("lib.after").propagate = False
secret, error = sys.argv[1], sys.argv[2]
logging.getLogger("app").warning("token %s", secret)
logging.getLogger("app").info("dict %(k)s", {"k": secret})
logging.getLogger("lib.before").warning("arg is an exception: %s", RuntimeError(secret))
logging.getLogger("lib.after").error("plain message with " + secret)
try:
    raise ValueError("upstream said " + error)
except ValueError:
    logging.getLogger("app").exception("failed")
logging.getLogger("app").warning("stack", stack_info=True)
print("MARKER-DONE")
"""


def test_handlers_from_basic_config_and_later_ones_never_print_a_secret():
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(
        [sys.executable, "-c", _BASIC_CONFIG_SCRIPT, REPLY_SECRET, ERROR_SECRET],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    out = done.stdout + done.stderr
    assert "MARKER-DONE" in out
    for expected in ("WARNING token", "INFO dict", "arg is an exception", "ValueError"):
        assert expected in out, expected
    assert _leaks(out) == []


def test_without_the_wiring_basic_config_would_print_it():
    """Control: the same handler, unmasked, writes the secret."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("stira.test.masking.control")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("token %s", REPLY_SECRET)
    finally:
        logger.removeHandler(handler)
    assert _leaks(stream.getvalue()) == [REPLY_SECRET]
