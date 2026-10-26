"""Offline Discord protocol tests with an injected fake discord.py module."""

import asyncio
import builtins
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from stiracore.core import NeuralState, StiraEngine
from stiracore.integrations import discord_bot
from stiracore.integrations.runtime import ChatSession, ChatSessions


class FakeEmbed:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.fields = []

    def add_field(self, **kwargs):
        self.fields.append(kwargs)


class FakeClient:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.change_presence = AsyncMock()

    def event(self, handler):
        setattr(self, handler.__name__, handler)
        return handler


def fake_discord():
    return SimpleNamespace(
        Embed=FakeEmbed,
        Client=FakeClient,
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        AllowedMentions=SimpleNamespace(none=lambda: "none"),
        Status=SimpleNamespace(online="online", idle="idle", dnd="dnd"),
        Game=lambda **kwargs: kwargs,
    )


@pytest.fixture
def bot():
    state = NeuralState()
    agent = Mock(state=state, engine=StiraEngine(state=state), return_value="reply")
    session = ChatSession(agent)
    sessions = ChatSessions("discord", factory=Mock(return_value=session))
    module = fake_discord()
    return SimpleNamespace(
        agent=agent,
        session=session,
        state=state,
        sessions=sessions,
        module=module,
        client=discord_bot.create_client(module, sessions),
    )


def message(text="hello", *, bot=False, channel_id=1):
    return SimpleNamespace(
        author=SimpleNamespace(bot=bot),
        content=text,
        guild=SimpleNamespace(id=2),
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_message_offloaded_and_embed(bot):
    main_thread = threading.get_ident()

    def reply(prompt):
        assert threading.get_ident() != main_thread
        return "reply"

    bot.agent.side_effect = reply
    msg = message()
    await bot.client.on_message(msg)
    bot.agent.assert_called_once_with("hello")
    assert msg.channel.send.call_args_list[0].args == ("reply",)
    embed = msg.channel.send.call_args_list[1].kwargs["embed"]
    assert len(embed.fields) == 6
    assert embed.color == discord_bot.COLORS["normal"]
    assert all("█" in field["value"] and "░" in field["value"] for field in embed.fields)
    assert bot.client.options["intents"].message_content
    assert bot.client.options["allowed_mentions"] == "none"
    bot.sessions.factory.assert_called_once_with("discord", "2:1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stability,level,presence",
    [(0.8, "normal", "online"), (0.4, "warning", "idle"), (0.2, "critical", "dnd")],
)
async def test_status_and_breaker_presence(bot, stability, level, presence):
    bot.state.stability = stability
    msg = message("!status")
    await bot.client.on_message(msg)
    bot.agent.assert_not_called()
    embed = msg.channel.send.call_args.kwargs["embed"]
    assert embed.color == discord_bot.COLORS[level]
    assert level.upper() in embed.description
    assert bot.client.change_presence.call_args.kwargs["status"] == presence


@pytest.mark.asyncio
@pytest.mark.parametrize("text,is_bot", [("hi", True), (" ", False)])
async def test_ignored_messages(bot, text, is_bot):
    msg = message(text, bot=is_bot)
    await bot.client.on_message(msg)
    bot.agent.assert_not_called()
    msg.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_long_reply_and_failure(bot):
    bot.agent.return_value = "x" * 4500
    msg = message()
    await bot.client.on_message(msg)
    assert [len(c.args[0]) for c in msg.channel.send.call_args_list if c.args] == [2000, 2000, 500]
    bot.agent.side_effect = RuntimeError("private provider detail")
    msg = message()
    await bot.client.on_message(msg)
    assert "private provider detail" not in str(msg.channel.send.call_args_list)
    assert "Unable to complete" in msg.channel.send.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_same_channel_turns_are_serialized(bot):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def reply(prompt):
        calls.append(prompt)
        if prompt == "first":
            entered.set()
            assert release.wait(5)
        return prompt

    bot.agent.side_effect = reply
    first = asyncio.create_task(bot.client.on_message(message("first")))
    assert await asyncio.to_thread(entered.wait, 5)
    second = asyncio.create_task(bot.client.on_message(message("second")))
    await asyncio.sleep(0)
    assert calls == ["first"]
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["first", "second"]


def test_missing_token_skip(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert discord_bot.main() == 0


def test_missing_extra_skip(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    original = builtins.__import__

    def no_discord(name, *args, **kwargs):
        if name == "discord":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_discord)
    assert discord_bot.main() == 0
