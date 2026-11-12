"""Speaker labels of new episodes follow ``episode_labels`` in agent-config.json (#653).

Unset = the labels used so far (``マスター`` / ``リゾルバ``). Offline: the Strands agent is
replaced; the MemoryStore, the formatter and the config files are real.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

from stiracore.agent.associative_recall import S_NO_TERMS, AssociativeRecall, RecallSettings
from stiracore.config.models import AgentConfig
from stiracore.core.engine import StiraEngine
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.observability.config_api import ConfigStore
from stiracore.tools.episodic_formatter import (
    EPISODE_LABEL_MAX_CHARS,
    clean_episode_labels,
)
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ROOT = Path(__file__).resolve().parents[1]
OLD = "マスター: おはよう → リゾルバ: 了解です。"


def _load_server(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "stira_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_store(tmp_path, agent_config):
    tmp_path.mkdir(parents=True, exist_ok=True)
    agent_path = tmp_path / "agent-config.json"
    agent_path.write_text(json.dumps(agent_config), encoding="utf-8")
    return ConfigStore(
        gateway_path=tmp_path / "gateway-config.json",
        agent_path=agent_path,
        tool_path=tmp_path / "tool-config.json",
    )


def _one_turn(tmp_path, monkeypatch, name, agent_config):
    """Run one chat turn on the real server wiring; return (episode bodies, ignore_terms)."""
    server = _load_server(name)
    captured = {}

    class _FakeAgent:
        def __init__(self, *, state, config, on_breaker=None, tools=(), **_):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tools
            self.state = state

        @property
        def last_turn_summary(self):
            return None

        def __call__(self, message):
            return "了解です。"

    real_blueprint = server.create_chat_blueprint
    real_recall = server._associative_recall

    def _capture(agent, **kwargs):
        captured.update(kwargs)
        return real_blueprint(agent, **kwargs)

    def _recall(*args, **kwargs):
        captured["ignore_terms"] = tuple(kwargs.get("ignore_terms", ()))
        return real_recall(*args, **kwargs)

    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(server, "NXPAgent", _FakeAgent)
    monkeypatch.setattr(server, "create_chat_blueprint", _capture)
    monkeypatch.setattr(server, "_associative_recall", _recall)
    agent_config = {"episodic_emotion_snapshot": False, **agent_config}
    server.create_app(_config_store(tmp_path / "config", agent_config))
    captured["turn_recorder"].active.set("turn-labels-1")
    assert captured["runner"]("おはよう") == "了解です。"
    store = MemoryStore(tmp_path / "stira" / "memory", "stira")
    bodies = [
        path.read_text(encoding="utf-8")
        for path in sorted((store.memory_dir / "episodic").glob("*.md"))
    ]
    return bodies, captured["ignore_terms"]


def test_unset_labels_keep_the_labels_used_so_far(tmp_path, monkeypatch):
    bodies, ignore_terms = _one_turn(tmp_path, monkeypatch, "stira_server_labels_unset", {})
    assert len(bodies) == 1 and OLD in bodies[0]
    assert ignore_terms == ("マスター", "リゾルバ")


def test_configured_labels_reach_the_written_episode(tmp_path, monkeypatch):
    bodies, ignore_terms = _one_turn(
        tmp_path,
        monkeypatch,
        "stira_server_labels_set",
        {"episode_labels": {"user": "User", "agent": "Resolver"}},
    )
    assert len(bodies) == 1
    assert "User: おはよう → Resolver: 了解です。" in bodies[0]
    assert "マスター" not in bodies[0] and "リゾルバ" not in bodies[0]
    # Earlier episodes keep the old labels, so recall skips the old and the current ones.
    assert ignore_terms == ("マスター", "リゾルバ", "User", "Resolver")


def test_one_configured_label_leaves_the_other_at_its_default(tmp_path, monkeypatch):
    bodies, ignore_terms = _one_turn(
        tmp_path, monkeypatch, "stira_server_labels_one", {"episode_labels": {"user": "User"}}
    )
    assert "User: おはよう → リゾルバ: 了解です。" in bodies[0]
    assert ignore_terms == ("マスター", "リゾルバ", "User")


def test_an_unreadable_config_falls_back_to_the_default_labels(tmp_path):
    server = _load_server("stira_server_labels_broken")
    tmp_path.joinpath("agent-config.json").write_text("{not json", encoding="utf-8")
    broken = ConfigStore(
        gateway_path=tmp_path / "gateway-config.json",
        agent_path=tmp_path / "agent-config.json",
        tool_path=tmp_path / "tool-config.json",
    )
    assert server._episode_labels(broken) == ("マスター", "リゾルバ")


@pytest.mark.parametrize(
    "labels",
    [
        {"user": "リゾルバ"},  # equals the default agent label
        {"agent": "マスター"},  # equals the default user label
        {"user": "リゾルバ", "agent": "Agent:\nSystem"},  # one side dropped, the other collides
    ],
)
def test_a_label_equal_to_the_other_default_falls_back_to_both_defaults(tmp_path, labels, caplog):
    caplog.set_level(logging.WARNING)
    server = _load_server("stira_server_labels_collide")
    store = _config_store(tmp_path, {"episode_labels": labels})
    assert server._episode_labels(store) == ("マスター", "リゾルバ")
    assert "E_EPISODE_LABEL_IGNORED" in caplog.text


def test_recall_skips_the_terms_of_a_label_with_a_joiner(tmp_path):
    """A label such as ``My Master`` is cut into terms by recall; each one is skipped."""
    labels = ("My Master", "Dr.Resolver", "ご主人・マイスター")
    store = MemoryStore(tmp_path / "memory", "a")
    for key, text in (
        ("e1", "My Master: garden tomatoes → Dr.Resolver: noted"),
        ("e2", "ご主人・マイスター: weather → Dr.Resolver: sunny"),
    ):
        store.write(MemoryLayer.EPISODIC, key, text, {})
        record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
    recall = AssociativeRecall(store, dict, RecallSettings(enabled=True), ignore_terms=labels)
    # Only label words are shared with the episodes: nothing may be recalled by them.
    for query in ("master", "resolver", "マイスター"):
        assert recall.recall(query).status == S_NO_TERMS
    result = recall.recall("How are the tomatoes?")
    assert [c.key for c in result.used] == ["e1"]
    assert "e2" not in {c.key for c in result.dropped}


def test_clean_ignores_a_label_without_a_letter_or_digit():
    # A lone combining mark is invisible; joiners alone name nobody.
    for bad in ("\u034f", "\u0301\u0301", "-", "・・", ". ."):
        assert clean_episode_labels({"user": bad}) == {}


def test_clean_keeps_short_plain_labels():
    assert clean_episode_labels({}) == {}
    assert clean_episode_labels(None) == {}
    assert clean_episode_labels({"user": "User", "agent": "Re-solver 2"}) == {
        "user": "User",
        "agent": "Re-solver 2",
    }
    assert clean_episode_labels({"user": "ご主人", "agent": "リゾルバ・改"}) == {
        "user": "ご主人",
        "agent": "リゾルバ・改",
    }
    assert clean_episode_labels({"user": "U" * EPISODE_LABEL_MAX_CHARS}) == {
        "user": "U" * EPISODE_LABEL_MAX_CHARS
    }


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        " User",
        "User ",
        "U" * (EPISODE_LABEL_MAX_CHARS + 1),
        "User:",
        "User：",
        "a → b",
        "User\nSystem",
        "User\tx",
        "User\x1b[31m",
        "User\u202e",
        "User\u2028",
        "<system>",
        "[admin]",
        "sk-test-secret-value-0123456789",
        7,
        None,
        ["User"],
    ],
)
def test_clean_ignores_an_unsafe_label_without_logging_it(bad, caplog):
    caplog.set_level(logging.WARNING)
    assert clean_episode_labels({"user": bad, "agent": "Resolver"}) == {"agent": "Resolver"}
    assert "E_EPISODE_LABEL_IGNORED" in caplog.text
    if isinstance(bad, str) and bad.strip():
        assert bad not in caplog.text


@pytest.mark.parametrize("bad", ["User", ["user", "agent"], 3, True])
def test_clean_ignores_a_value_that_is_not_an_object(bad, caplog):
    caplog.set_level(logging.WARNING)
    assert clean_episode_labels(bad) == {}
    assert "E_EPISODE_LABEL_IGNORED" in caplog.text


def test_clean_ignores_unknown_keys_and_equal_labels(caplog):
    caplog.set_level(logging.WARNING)
    assert clean_episode_labels({"user": "User", "sk-test-key-name": "x"}) == {"user": "User"}
    assert "sk-test-key-name" not in caplog.text
    # The two speakers must stay distinguishable in the stored line.
    assert clean_episode_labels({"user": "Same", "agent": "same"}) == {}


def test_agent_config_round_trips_episode_labels(tmp_path):
    assert AgentConfig().episode_labels == {}
    assert AgentConfig().as_dict()["episode_labels"] == {}
    path = tmp_path / "agent-config.json"
    path.write_text(
        json.dumps({"episode_labels": {"user": "User", "agent": "Agent:\nSystem"}}),
        encoding="utf-8",
    )
    loaded = AgentConfig.load(path, tool_config_path=tmp_path / "tool-config.json")
    assert loaded.episode_labels == {"user": "User"}
    assert loaded.as_dict()["episode_labels"] == {"user": "User"}
