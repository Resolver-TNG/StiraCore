"""Offline Slack transport tests: no optional SDK, credentials or network."""

import builtins
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.core import NeuralState, StiraEngine
from stiracore.integrations import slack_bot
from stiracore.integrations.runtime import ERROR_TEXT, ChatSession, ChatSessions


class FakeApp:
    def __init__(self):
        self.events = {}
        self.commands = {}

    def event(self, name):
        def register(handler):
            self.events[name] = handler
            return handler

        return register

    def command(self, name):
        def register(handler):
            self.commands[name] = handler
            return handler

        return register


@pytest.fixture
def slack():
    state = NeuralState()
    agent = Mock(state=state, engine=StiraEngine(state=state), return_value="reply")
    session = ChatSession(agent)
    sessions = ChatSessions("slack", factory=Mock(return_value=session))
    app = FakeApp()
    slack_bot.register_handlers(app, sessions)
    return SimpleNamespace(app=app, agent=agent, state=state, session=session, sessions=sessions)


def dispatch(slack, **overrides):
    event = {"channel": "C1", "ts": "1.0", "user": "U1", "text": "hello", **overrides}
    say = Mock()
    slack.app.events["message"](event=event, say=say, body={"team_id": "T1"})
    return say


def test_thread_response_and_delta(slack):
    def reply(text):
        slack.state.stability -= 0.1
        return "<@U1> response"

    slack.agent.side_effect = reply
    say = dispatch(slack, thread_ts="0.1")
    slack.agent.assert_called_once_with("hello")
    assert all(call.kwargs["thread_ts"] == "0.1" for call in say.call_args_list)
    first = say.call_args_list[0].kwargs
    assert first["blocks"][0]["text"] == {"type": "plain_text", "text": "<@U1> response"}
    assert "-0.100" in say.call_args_list[-1].kwargs["text"]
    slack.sessions.factory.assert_called_once_with("slack", "T1:C1")


@pytest.mark.parametrize("level,stability,icon", [("warning", 0.4, "🟡"), ("critical", 0.2, "🔴")])
def test_breaker_channel_notice_once(slack, level, stability, icon):
    slack.state.stability = stability
    say = dispatch(slack)
    notice = say.call_args_list[-1].kwargs
    assert "thread_ts" not in notice
    assert icon in notice["text"] and level.upper() in notice["text"]
    assert dispatch(slack).call_count == 2


@pytest.mark.parametrize(
    "event", [{"bot_id": "B"}, {"subtype": "message_changed"}, {"user": ""}, {"text": " "}]
)
def test_ignore_bot_edit_and_empty(slack, event):
    assert dispatch(slack, **event).call_count == 0
    slack.agent.assert_not_called()


def test_status_ack_first_without_model(slack):
    ack, respond = Mock(), Mock()
    original = slack.sessions.factory
    slack.sessions.factory = Mock(
        side_effect=lambda *args: (ack.assert_called_once(), original(*args))[1]
    )
    slack.app.commands["/stira"](
        ack=ack,
        respond=respond,
        command={"text": "status", "team_id": "T1", "channel_id": "C1"},
    )
    assert "stability=" in respond.call_args.kwargs["text"]
    assert respond.call_args.kwargs["response_type"] == "ephemeral"
    slack.agent.assert_not_called()
    slack.app.commands["/stira"](ack=Mock(), respond=respond, command={"text": "help"})
    assert "Usage" in respond.call_args.kwargs["text"]


def test_long_reply_chunked(slack):
    slack.agent.return_value = "x" * 7500
    say = dispatch(slack)
    parts = [
        c.kwargs["blocks"][0]["text"]["text"] for c in say.call_args_list if "blocks" in c.kwargs
    ]
    assert list(map(len, parts)) == [3000, 3000, 1500]


def test_failed_turn_still_saves_and_reports_breaker(slack):
    slack.agent.side_effect = RuntimeError("provider sensitive payload")
    slack.state.stability = 0.2
    slack.session.persistence = Mock()
    slack.session.store = Mock()
    say = dispatch(slack)
    assert ERROR_TEXT in str(say.call_args_list)
    assert "sensitive payload" not in str(say.call_args_list)
    assert "CRITICAL" in say.call_args_list[-1].kwargs["text"]
    slack.session.persistence.save.assert_called_once()
    slack.session.store.write.assert_not_called()


def test_missing_tokens_skip_before_sdk_import(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    assert slack_bot.main() == 0


def test_missing_extra_skips(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "test-app")
    original = builtins.__import__

    def no_slack(name, *args, **kwargs):
        if name == "slack_bolt":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_slack)
    assert slack_bot.main() == 0
