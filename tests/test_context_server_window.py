"""#746: the window of a local OpenAI-compatible server comes from its ``meta.n_ctx``.

Offline: the ``/models`` answer is a fake ``fetch``; the cache clock is injected.
"""

from __future__ import annotations

import pytest

from stiracore.agent.context_management import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ContextManagementSettings,
    resolve_context_window_source,
)
from stiracore.providers import server_window
from stiracore.providers.capabilities import UNKNOWN
from stiracore.providers.server_window import reported_context_window, window_from_models
from stiracore.providers.spec import ModelSpec
from tests.test_context_autonomy import ReportingFake, _agent, _loop_script, _summary_positions

LOCAL = ModelSpec("openai", "local-model", options={"base_url": "http://127.0.0.1:8080/v1"})
N_CTX = {"object": "list", "data": [{"id": "local-model", "meta": {"n_ctx": 131_072}}]}
NO_N_CTX = {"object": "list", "data": [{"id": "local-model", "owned_by": "llamacpp"}]}


@pytest.fixture(autouse=True)
def _fresh_cache():
    server_window.clear_cache()
    yield
    server_window.clear_cache()


class _Fetch:
    def __init__(self, answer):
        self.answer = answer
        self.urls: list[str] = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_local_endpoint_that_reports_n_ctx_gets_that_window():
    fetch = _Fetch(N_CTX)
    assert reported_context_window(LOCAL, fetch=fetch) == 131_072
    assert fetch.urls == ["http://127.0.0.1:8080/v1/models"]
    settings = ContextManagementSettings(enabled=True)
    assert resolve_context_window_source(
        settings, object(), UNKNOWN, server_window=lambda: reported_context_window(LOCAL)
    ) == (131_072, "server")


def test_a_local_endpoint_without_n_ctx_keeps_the_default():
    fetch = _Fetch(NO_N_CTX)
    assert reported_context_window(LOCAL, fetch=fetch) is None
    settings = ContextManagementSettings(enabled=True)
    assert resolve_context_window_source(
        settings, object(), UNKNOWN, server_window=lambda: reported_context_window(LOCAL)
    ) == (DEFAULT_CONTEXT_WINDOW_TOKENS, "default")
    assert DEFAULT_CONTEXT_WINDOW_TOKENS == 200_000


@pytest.mark.parametrize(
    "spec",
    [
        ModelSpec("openai", "gpt-x", options={"base_url": "https://api.example.com/v1"}),
        ModelSpec("openai", "gpt-x"),
        ModelSpec("openai", "m", options={"base_url": "http://user:sk-test-1@127.0.0.1/v1"}),
        ModelSpec("openai", "m", options={"base_url": "ftp://127.0.0.1/v1"}),
        ModelSpec("bedrock", "some.model-v1"),
        None,
    ],
    ids=["hosted", "no-base-url", "userinfo", "not-http", "other-provider", "no-spec"],
)
def test_nothing_is_requested_outside_the_local_rule(spec):
    fetch = _Fetch(N_CTX)
    assert reported_context_window(spec, fetch=fetch) is None
    assert fetch.urls == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"data": "x"},
        {"data": [{"id": "local-model", "meta": {"n_ctx": True}}]},
        {"data": [{"id": "local-model", "meta": {"n_ctx": 0}}]},
        {"data": [{"id": "local-model", "meta": {"n_ctx": "131072"}}]},
        {"data": [{"id": "local-model", "meta": {"n_ctx": 10**12}}]},
        {"data": [{"id": "a", "meta": {"n_ctx": 1000}}, {"id": "b", "meta": {"n_ctx": 2000}}]},
    ],
)
def test_only_a_plausible_integer_is_a_window(payload):
    assert window_from_models(payload, "local-model") is None


def test_the_named_model_wins_and_a_single_unnamed_model_counts():
    two = {"data": [{"id": "a", "meta": {"n_ctx": 1000}}, {"id": "b", "meta": {"n_ctx": 2000}}]}
    assert window_from_models(two, "b") == 2000
    # llama.cpp lists the one loaded model under its file name, whatever id was configured.
    assert window_from_models({"data": [{"id": "/m/x.gguf", "meta": {"n_ctx": 4096}}]}, "y") == 4096


def test_the_answer_is_cached_and_a_failure_is_not_retried_at_once():
    now = [0.0]
    fetch = _Fetch(N_CTX)
    for _ in range(3):
        assert reported_context_window(LOCAL, fetch=fetch, clock=lambda: now[0]) == 131_072
    assert len(fetch.urls) == 1
    now[0] = server_window.CACHE_TTL_SEC + 1
    assert reported_context_window(LOCAL, fetch=fetch, clock=lambda: now[0]) == 131_072
    assert len(fetch.urls) == 2

    server_window.clear_cache()
    down = _Fetch(OSError("connection refused"))
    assert reported_context_window(LOCAL, fetch=down, clock=lambda: now[0]) is None
    assert reported_context_window(LOCAL, fetch=down, clock=lambda: now[0]) is None
    assert len(down.urls) == 1


def test_cached_only_never_sends_a_request():
    fetch = _Fetch(N_CTX)
    assert reported_context_window(LOCAL, cached_only=True, fetch=fetch) is None
    assert fetch.urls == []
    reported_context_window(LOCAL, fetch=fetch)
    assert reported_context_window(LOCAL, cached_only=True, fetch=_Fetch(OSError())) == 131_072


def test_a_configured_or_recorded_window_is_not_replaced():
    def never():
        raise AssertionError("the server must not be asked")

    settings = ContextManagementSettings(enabled=True, context_window_tokens=50_000)
    assert resolve_context_window_source(settings, object(), UNKNOWN, server_window=never) == (
        50_000,
        "setting",
    )


# ------------------------------------------------------------- scripted agent


def _local_agent(monkeypatch, tmp_path, answer, reported):
    monkeypatch.setattr(server_window, "_http_get_json", _Fetch(answer))
    settings = ContextManagementSettings(enabled=True, preserve_recent=2)
    model = ReportingFake(_loop_script(reported))
    agent = _agent(model, settings=settings, context_stash_dir=tmp_path / "stash")
    agent.model_spec = ModelSpec(
        "openai", "local-model", options={"base_url": "http://127.0.0.1:8080/v1"}
    )
    agent("work through the files")
    return agent, model


def test_the_summary_threshold_follows_the_reported_window(monkeypatch, tmp_path):
    answer = {"data": [{"id": "local-model", "meta": {"n_ctx": 10_000}}]}
    agent, model = _local_agent(
        monkeypatch, tmp_path, answer, [600, 1_200, 2_400, 4_800, 9_000, 600, 700]
    )
    # 9,000 of the server's 10,000 is past 85 %: one summary before the next call.
    assert _summary_positions(model) == [5]
    assert agent.last_context_management["summaries"] == 1
    assert agent.server_context_window(cached_only=True) == 10_000


def test_without_a_reported_window_the_default_stays(monkeypatch, tmp_path):
    agent, model = _local_agent(
        monkeypatch, tmp_path, NO_N_CTX, [600, 1_200, 2_400, 4_800, 9_000, 600, 700]
    )
    assert _summary_positions(model) == []  # 9,000 of 200,000
    assert agent.server_context_window(cached_only=True) is None
