"""Integration coverage for agent-scoped memory tools and registry policy."""

from stiracore.tools import MemoryLayer, MemoryStore, create_memory_tools, tool_registry


def _tools_by_name(tools):
    return {tool.tool_name: tool for tool in tools}


def test_bound_memory_tools_write_list_read_and_search(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira-test")
    tools = _tools_by_name(create_memory_tools(store))

    assert (
        tools["memory_write"](
            layer="episodic",
            key="first-conversation",
            content="Resolver remembers the constellation Lyra.",
        )
        == "Memory written: episodic/first-conversation"
    )

    listed = tools["memory_list"](layer="episodic")
    assert "episodic/first-conversation" in listed

    read = tools["memory_read"](layer="episodic", key="first-conversation")
    assert "Resolver remembers the constellation Lyra." in read

    searched = tools["memory_search"](query="LYRA")
    assert "episodic/first-conversation" in searched
    assert "Resolver remembers the constellation Lyra." in searched

    entry = store.read(MemoryLayer.EPISODIC, "first-conversation")
    assert entry is not None
    assert entry.content == "Resolver remembers the constellation Lyra."


def test_standard_memory_is_excluded_without_knowledge_base(monkeypatch):
    monkeypatch.delenv("STRANDS_KNOWLEDGE_BASE_ID", raising=False)

    registered_names = {
        tool.tool_name
        for tool in tool_registry.get_tools()
        if isinstance(getattr(tool, "tool_name", None), str)
    }

    assert "memory" not in registered_names


def test_stira_memory_tools_follow_breaker_write_policy(tmp_path):
    from dashboard.stira_server import _stira_tools

    store = MemoryStore(tmp_path / "memory", "stira-test")
    bound_tools = tuple(create_memory_tools(store))
    write_names = {"memory_write"}

    normal_names = set(_tools_by_name(_stira_tools(bound_tools, write_names)))
    warning_names = set(
        _tools_by_name(_stira_tools(bound_tools, write_names, breaker_level="warning"))
    )

    assert {"memory_read", "memory_write", "memory_search", "memory_list"} <= normal_names
    assert {"memory_read", "memory_search", "memory_list"} <= warning_names
    assert "memory_write" not in warning_names
