"""Exec-class tools are hidden unless explicitly enabled (#163)."""

from __future__ import annotations

import logging

import pytest

from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig


class _Tool:
    def __init__(self, name: str) -> None:
        self.tool_name = name


@pytest.fixture
def fake_registry(monkeypatch):
    read = {"current_time": _Tool("current_time")}
    names = ["shell", "batch", "cron", "use_agent", "swarm", "graph", "think", "http_request"]
    write = {n: _Tool(n) for n in names}
    monkeypatch.setattr(tool_registry, "READ_TOOLS", read)
    monkeypatch.setattr(tool_registry, "WRITE_TOOLS", write)
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    return read, write


def _names(tools) -> set[str]:
    return {t.tool_name for t in tools}


def test_default_excludes_exec_tools(fake_registry):
    names = _names(tool_registry.get_tools())
    assert names.isdisjoint(tool_registry.EXEC_TOOL_NAMES)
    assert {"current_time", "http_request"} <= names
    # think re-runs parent tools in a hook-less inner Agent (#145): exec class.
    assert "think" not in names


def test_include_exec_true_exposes_and_warns(fake_registry, caplog):
    with caplog.at_level(logging.WARNING, logger=tool_registry.__name__):
        names = _names(tool_registry.get_tools(include_exec=True))
    assert tool_registry.EXEC_TOOL_NAMES <= names
    assert "Exec-class tools are enabled" in caplog.text


def test_env_var_enables_exec(fake_registry, monkeypatch):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, "1")
    assert "shell" in _names(tool_registry.get_tools())
    assert "shell" not in _names(tool_registry.get_tools(include_exec=False))


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_falsey_env_values_keep_exec_hidden(fake_registry, monkeypatch, value):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, value)
    assert "shell" not in _names(tool_registry.get_tools())


def test_exec_tools_stay_hidden_when_breaker_blocks_writes(fake_registry):
    names = _names(tool_registry.get_tools("critical", include_exec=True))
    assert names.isdisjoint(tool_registry.EXEC_TOOL_NAMES)


def test_tool_config_trust_all_does_not_reintroduce_exec(fake_registry, tmp_path):
    config = ToolConfig(mode="trust_all", path=tmp_path / "tool-config.json")
    names = _names(config.get_allowed_tools())
    assert names.isdisjoint(tool_registry.EXEC_TOOL_NAMES)
    assert "http_request" in names


def test_tool_config_follows_env_opt_in(fake_registry, monkeypatch, tmp_path):
    monkeypatch.setenv(tool_registry.EXEC_ENV_VAR, "true")
    config = ToolConfig(mode="trust_all", path=tmp_path / "tool-config.json")
    assert "shell" in _names(config.get_allowed_tools())


def test_real_registry_has_no_module_registrations():
    # sqlite3 / the memory_tools module used to be registered as tools by mistake.
    assert "sqlite3" not in tool_registry.WRITE_TOOLS
    assert "memory_tools" not in tool_registry.WRITE_TOOLS
