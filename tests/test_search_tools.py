"""Offline Perplexity API and real Strands registration regression tests."""

import importlib.util
from unittest.mock import Mock, patch

import pytest
import requests
from strands import Agent

from stiracore.tools import tool_registry
from stiracore.tools.search_tools import perplexity_search


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-only-key")
    with patch("stiracore.tools.search_tools.requests.post") as mocked:
        mocked.return_value.status_code = 200
        mocked.return_value.json.return_value = {
            "choices": [{"message": {"content": "Answer [1] [2]"}}],
            "citations": ["https://example.com/one", "https://example.com/two"],
            "search_results": [
                {
                    "title": "One",
                    "url": "https://example.com/one",
                    "snippet": "First result",
                    "date": "2026-09-29",
                    "last_updated": "2026-09-30",
                },
                {"title": "Two", "url": "https://example.com/two"},
            ],
        }
        yield mocked


def test_search_parses_sources_and_preserves_citation_indices(post):
    result = perplexity_search("test query", count=1)
    assert result == {
        "status": "success",
        "answer": "Answer [1] [2]",
        "results": [
            {
                "title": "One",
                "url": "https://example.com/one",
                "snippet": "First result",
                "date": "2026-09-29",
                "last_updated": "2026-09-30",
            }
        ],
        "citations": ["https://example.com/one", "https://example.com/two"],
    }
    post.assert_called_once_with(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": "Bearer test-only-key"},
        json={"model": "sonar", "messages": [{"role": "user", "content": "test query"}]},
        timeout=(5, 60),
        allow_redirects=False,
    )


def test_default_count_caps_results(post):
    payload = post.return_value.json.return_value
    payload["search_results"] = [{"url": f"https://example.com/{i}"} for i in range(7)]
    assert len(perplexity_search("query")["results"]) == 5


def test_citations_only_response(post):
    del post.return_value.json.return_value["search_results"]
    assert perplexity_search("query", 1)["results"] == [{"url": "https://example.com/one"}]


def test_answer_without_sources(post):
    post.return_value.json.return_value = {"choices": [{"message": {"content": "No sources"}}]}
    result = perplexity_search("query")
    assert result["status"] == "success"
    assert result["results"] == result["citations"] == []


@pytest.mark.parametrize("key", [None, "", "  \t"])
def test_no_key_does_not_call_api(post, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PERPLEXITY_API_KEY", key)
    assert perplexity_search("query")["status"] == "error"
    post.assert_not_called()


@pytest.mark.parametrize(
    "query,count",
    [
        ("", 5),
        ("   ", 5),
        (None, 5),
        ("q", 0),
        ("q", -1),
        ("q", 21),
        ("q", True),
        ("q", 1.5),
        ("q", "5"),
    ],
)
def test_invalid_arguments_do_not_call_api(post, query, count):
    assert perplexity_search(query, count)["status"] == "error"
    post.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": 7}}]},
    ],
)
def test_malformed_answer(post, payload):
    post.return_value.json.return_value = payload
    assert perplexity_search("query") == {
        "status": "error",
        "error": "Perplexity API returned an invalid response",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("citations", None),
        ("citations", [1]),
        ("search_results", {}),
        ("search_results", [None]),
        ("search_results", [{}]),
        ("search_results", [{"url": 7}]),
        ("search_results", [{"url": "https://example.com", "title": []}]),
    ],
)
def test_malformed_sources(post, field, value):
    post.return_value.json.return_value[field] = value
    assert perplexity_search("query")["status"] == "error"


def test_invalid_json(post):
    post.return_value.json.side_effect = ValueError("private response content")
    assert perplexity_search("query") == {
        "status": "error",
        "error": "Perplexity API returned invalid JSON",
    }


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_failure_is_sanitized_without_retry(post, status):
    post.return_value.status_code = status
    assert perplexity_search("query") == {
        "status": "error",
        "error": f"Perplexity API returned HTTP {status}",
    }
    post.assert_called_once()
    post.return_value.json.assert_not_called()


@pytest.mark.parametrize(
    "error,message",
    [
        (requests.exceptions.Timeout, "Perplexity API request timed out"),
        (requests.exceptions.ConnectionError, "Perplexity API request failed"),
        (requests.exceptions.RequestException, "Perplexity API request failed"),
    ],
)
def test_network_failure_is_sanitized(post, error, message):
    post.side_effect = error("test-only-key private provider details")
    assert perplexity_search("query") == {"status": "error", "error": message}
    post.assert_called_once()


def _fresh_registry():
    # Execute the real module initialization without replacing global dictionaries
    # used by other test modules or disconnecting their MCP providers.
    spec = importlib.util.spec_from_file_location(
        "stiracore.tools._search_test_registry", tool_registry.__file__
    )
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    return registry


@pytest.mark.parametrize("key", [None, "", "  "])
def test_registration_skipped_without_key(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PERPLEXITY_API_KEY", key)
    registry = _fresh_registry()
    assert "perplexity_search" not in registry.READ_TOOLS
    assert "perplexity_search" not in registry.WRITE_TOOLS
    assert "file_read" in registry.READ_TOOLS


def test_configured_tool_is_read_only_and_sdk_executable(post, monkeypatch):
    registry = _fresh_registry()
    assert "perplexity_search" in registry.READ_TOOLS
    assert "perplexity_search" not in registry.WRITE_TOOLS
    agent = Agent(model=Mock(), tools=registry.get_tools("warning"), callback_handler=None)
    assert "perplexity_search" in agent.tool_names
    assert "file_write" not in agent.tool_names
    result = agent.tool.perplexity_search(query="query", count=1)
    assert result["status"] == "success"
    post.assert_called_once()

    monkeypatch.delenv("PERPLEXITY_API_KEY")
    assert registry.READ_TOOLS["perplexity_search"] not in registry.get_tools()
    post.reset_mock()
    assert perplexity_search("query")["status"] == "error"
    post.assert_not_called()
