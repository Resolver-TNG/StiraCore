import logging
from types import SimpleNamespace

from stiracore.agent.nxp_agent import _tool_name
from stiracore.tools import tool_registry


def _loaded_names(breaker_level=None) -> set[str]:
    return {
        name
        for tool in tool_registry.get_tools(breaker_level)
        if (name := _tool_name(tool)) is not None
    }


def test_standard_function_tools_are_registered() -> None:
    names = _loaded_names()

    assert {"file_read", "file_write", "image_reader", "http_request"} <= names


def test_existing_standard_tools_remain_registered() -> None:
    names = _loaded_names()

    assert "current_time" in names
    # Exec-class tools stay registered but are hidden by default (#163); think
    # joined them because it re-runs parent tools without NXP hooks (#145).
    assert {"cron", "graph", "use_agent", "swarm", "think"} <= set(tool_registry.WRITE_TOOLS)
    assert {"cron", "graph", "use_agent", "swarm", "think"}.isdisjoint(names)


def test_standard_tool_read_write_classification() -> None:
    assert {"file_read", "image_reader", "stop", "handoff_to_user"} <= set(tool_registry.READ_TOOLS)
    assert {"file_write", "editor", "http_request"} <= set(tool_registry.WRITE_TOOLS)


def test_warning_breaker_hides_new_write_tools() -> None:
    names = _loaded_names("warning")

    assert {"file_read", "image_reader"} <= names
    assert {"file_write", "editor", "http_request"}.isdisjoint(names)


def test_successful_import_that_cannot_register_logs_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        tool_registry,
        "import_module",
        lambda _module_name: SimpleNamespace(broken_tool=None),
    )

    with caplog.at_level(logging.WARNING):
        registered = tool_registry._load_standard_tool({}, "broken_tool")

    assert registered is False
    assert "Imported strands_tools.broken_tool but could not register it" in caplog.text


def test_standard_tools_reach_actual_strands_registry() -> None:
    from unittest.mock import Mock

    from strands import Agent

    agent = Agent(model=Mock(), tools=tool_registry.get_tools(), callback_handler=None)
    assert set(agent.tool_names) == _loaded_names()
    assert {"file_read", "file_write", "image_reader", "http_request", "editor"} <= set(
        agent.tool_names
    )


def test_restricted_tools_reach_actual_strands_registry() -> None:
    from unittest.mock import Mock

    from strands import Agent

    agent = Agent(model=Mock(), tools=tool_registry.get_tools("warning"), callback_handler=None)
    assert set(agent.tool_names) == _loaded_names("warning")
    assert {"file_write", "http_request", "editor"}.isdisjoint(agent.tool_names)
