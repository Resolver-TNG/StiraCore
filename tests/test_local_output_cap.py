"""#745: a local OpenAI-compatible endpoint takes the local output cap; the cap is shown.

No server and no network: the endpoint is only a URL in the configuration, and the model is
the scripted ``FakeModel``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask
from strands import tool
from strands.types.exceptions import MaxTokensReachedException

from stiracore.agent import nxp_agent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.config import GatewayConfig, resolve_config
from stiracore.config.resolver import model_kwargs
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.providers.capabilities import (
    AUTO_MAX_OUTPUT_TOKENS,
    CAP_SOURCES,
    HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT,
    describe_output_cap,
    is_local_endpoint,
    resolve_max_output_cap,
    resolve_max_output_tokens,
)
from tests.provider_fakes import FakeModel, text_turn, tool_turn, truncated_tool_turn

MODEL = "qwen3.8-flash-next-iq3_s"  # not in the capability table
LOOPBACK = "http://127.0.0.1:18080/v1"
PUBLIC = "https://api.example.com/v1"

# --- which endpoint is local (pure, no DNS) ---------------------------------


@pytest.mark.parametrize(
    "url",
    [
        LOOPBACK,
        "http://127.8.9.10/v1",
        "http://localhost:8080/v1",
        "http://LOCALHOST/v1",
        "http://[::1]:8080/v1",
        "http://10.1.2.3:8080/v1",
        "http://172.16.0.1/v1",
        "http://172.31.255.254/v1",
        "https://192.168.1.20:8443/v1",
    ],
)
def test_loopback_and_private_endpoints_are_local(url):
    assert is_local_endpoint(url) is True


@pytest.mark.parametrize(
    "url",
    [
        PUBLIC,
        "https://api.openai.com/v1",
        "http://8.8.8.8/v1",
        "http://172.32.0.1/v1",  # just outside 172.16/12
        "http://192.169.0.1/v1",
        "http://169.254.169.254/v1",  # link-local (cloud metadata) is not "private network"
        "http://100.64.0.1/v1",  # shared address space
        "http://0.0.0.0:8080/v1",
        "http://[fd00::1]/v1",  # only the ranges the rule names
        "http://localhost.example.com/v1",
        "http://127.0.0.1.example.com/v1",
        "http://10.0.0.1@api.example.com/v1",  # user info is not the host
        "not a url",
        "",
        None,
        8080,
    ],
)
def test_everything_else_is_not_local(url):
    assert is_local_endpoint(url) is False


# --- the cap and its source ---------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "openai_responses"])
def test_same_model_local_endpoint_takes_the_local_default_public_is_unchanged(provider):
    assert resolve_max_output_cap(None, provider, MODEL, base_url=LOOPBACK) == (
        AUTO_MAX_OUTPUT_TOKENS,
        "local_default",
    )
    for base_url in (PUBLIC, None):
        assert resolve_max_output_cap(None, provider, MODEL, base_url=base_url) == (
            HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT,
            "unknown_limit_default",
        )


def test_a_local_endpoint_changes_nothing_else():
    # A recorded limit still decides (a local proxy in front of a documented model).
    hosted = resolve_max_output_cap(None, "openai", "gpt-4.1")
    assert resolve_max_output_cap(None, "openai", "gpt-4.1", base_url=LOOPBACK) == hosted
    assert hosted[1] == "model_table"
    # Other hosted providers keep their rule also behind a local address.
    for provider in ("bedrock", "anthropic", "gemini", "litellm"):
        assert (
            resolve_max_output_tokens(None, provider, MODEL, base_url=LOOPBACK)
            == HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT
        )
    # An explicit value always wins.
    assert resolve_max_output_cap(6144, "openai", MODEL, base_url=LOOPBACK) == (6144, "setting")
    assert resolve_max_output_cap(None, "llamacpp", MODEL) == (32_000, "local_default")
    assert resolve_max_output_cap(None, "no-such-provider", MODEL, base_url=LOOPBACK) == (
        HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT,
        "unknown_limit_default",
    )


def test_a_spec_carries_its_own_endpoint():
    spec = model_kwargs("openai", MODEL, base_url=LOOPBACK, environ={})["model"]
    assert resolve_max_output_cap(None, spec) == (AUTO_MAX_OUTPUT_TOKENS, "local_default")
    # The keyword is for provider names only: it cannot make another spec local.
    key = {"api_key_env": "K", "environ": {"K": "x"}}
    hosted = model_kwargs("openai", MODEL, base_url=PUBLIC, **key)["model"]
    assert resolve_max_output_cap(None, hosted, base_url=LOOPBACK) == (
        HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT,
        "unknown_limit_default",
    )


def test_resolved_config_uses_the_endpoint():
    local = GatewayConfig(provider="openai", model_id=MODEL, base_url=LOOPBACK)
    assert resolve_config(local, environ={}).max_output_tokens == AUTO_MAX_OUTPUT_TOKENS
    public = GatewayConfig(provider="openai", model_id=MODEL, base_url=PUBLIC)
    assert resolve_config(public, environ={}).max_output_tokens == HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT
    pinned = GatewayConfig(
        provider="openai", model_id=MODEL, base_url=LOOPBACK, max_output_tokens=20_000
    )
    assert resolve_config(pinned, environ={}).max_output_tokens == 20_000
    # The environment's endpoint is the one that counts.
    moved = resolve_config(public, environ={"STIRA_BASE_URL": LOOPBACK})
    assert moved.max_output_tokens == AUTO_MAX_OUTPUT_TOKENS


def test_describe_output_cap_names_one_of_four_sources():
    assert describe_output_cap(32_000, "openai", MODEL, base_url=LOOPBACK) == {
        "value": 32_000,
        "source": "local_default",
    }
    assert describe_output_cap(16384, "openai", MODEL, base_url=PUBLIC) == {
        "value": 16384,
        "source": "unknown_limit_default",
    }
    assert describe_output_cap(32_000, "openai", "gpt-4.1")["source"] == "model_table"
    # A running cap that is not the automatic one was set by a person.
    assert describe_output_cap(20_000, "openai", MODEL, base_url=LOOPBACK) == {
        "value": 20_000,
        "source": "setting",
    }
    for bad in (None, 0, -1, True, "8192", 1.5):
        assert describe_output_cap(bad, "openai", MODEL) is None
    assert describe_output_cap(8192, None, None)["source"] in CAP_SOURCES


# --- a 600-line file_write (scripted model) -------------------------------------

LINES = [f"line {n:03d}: " + "x" * 110 for n in range(600)]
CONTENT = "\n".join(LINES)
# About 4 characters per token: over the hosted default, well under the local one.
CONTENT_TOKENS = len(CONTENT) // 4
assert HOSTED_UNKNOWN_LIMIT_MAX_OUTPUT < CONTENT_TOKENS < AUTO_MAX_OUTPUT_TOKENS


def _write_turn(cap: int) -> list:
    """What a server with this cap returns for the 300-line call: whole, or cut off."""
    args = {"path": "page.html", "content": CONTENT}
    if CONTENT_TOKENS <= cap:
        return tool_turn("file_write", args)
    raw = json.dumps(args)
    return truncated_tool_turn("file_write", raw[: cap * 4])


def _agent(script, calls, **kwargs):
    @tool
    def file_write(path: str, content: str) -> str:
        """Write a file."""
        calls.append((path, content))
        return "written"

    model = FakeModel(script)
    agent = nxp_agent.NXPAgent(
        model=model, tools=[file_write], stimulus_classifier=RuleBasedClassifier(), **kwargs
    )
    return agent, model


def test_a_600_line_file_write_runs_with_the_local_default():
    config = GatewayConfig(provider="openai", model_id=MODEL, base_url=LOOPBACK)
    cap = resolve_config(config, environ={}).max_output_tokens
    calls: list = []
    agent, _ = _agent([_write_turn(cap), text_turn("done")], calls, max_output_tokens=cap)
    agent("write the page")
    assert [path for path, _ in calls] == ["page.html"]
    assert calls[0][1].count("\n") == 599


def test_control_the_same_call_is_cut_off_at_the_hosted_default():
    config = GatewayConfig(provider="openai", model_id=MODEL, base_url=PUBLIC)
    cap = resolve_config(config, environ={}).max_output_tokens
    calls: list = []
    agent, _ = _agent([_write_turn(cap)], calls, max_output_tokens=cap)
    with pytest.raises(MaxTokensReachedException):
        agent("write the page")
    assert calls == []


# --- the note the model reads after a cut-off call --------------------------------

CUT_ARGS = '{"path": "game.html", "content": "<!doctype html><script>let score = 0;\\nfunction'
SECRET = "CANARY-sk-12345"


def _assistant_text(agent) -> str:
    last = [m for m in agent.agent.messages if m["role"] == "assistant"][-1]
    assert all("toolUse" not in block for block in last["content"])
    return "\n".join(block.get("text", "") for block in last["content"])


def test_the_cut_off_note_names_the_cause_and_the_remedy():
    calls: list = []
    agent, model = _agent(
        [truncated_tool_turn("file_write", CUT_ARGS), text_turn("ok, in parts")],
        calls,
        max_output_tokens=8192,
    )
    with pytest.raises(MaxTokensReachedException):
        agent("write a long game")
    note = _assistant_text(agent)
    assert "incomplete" in note  # the SDK's sentence stays
    assert "output limit" in note and "8192" in note
    assert "was not run" in note and "smaller pieces" in note
    assert note.count(nxp_agent.CUT_OFF_NOTE_MARKER) == 1
    # The half-written arguments are not copied into the note.
    assert "score" not in note and "game.html" not in note
    # The next call of the same session carries the note.
    agent("try again")
    sent = json.dumps(model.requests[1]["messages"])
    assert "smaller pieces" in sent and calls == []


def test_the_note_is_fixed_text_and_a_number_only():
    args = json.dumps({"path": "k.txt", "content": SECRET})[:-4]
    agent, _ = _agent([truncated_tool_turn("file_write", args)], [], max_output_tokens=8192)
    with pytest.raises(MaxTokensReachedException):
        agent("x")
    assert SECRET not in json.dumps(agent.agent.messages)


def test_a_cut_without_a_tool_call_gets_no_note():
    script = [[{"text": "Here is the"}, {"stop_reason": "max_tokens"}]]
    agent, _ = _agent(script, [])
    with pytest.raises(MaxTokensReachedException):
        agent("x")
    assert nxp_agent.CUT_OFF_NOTE_MARKER not in json.dumps(agent.agent.messages)


@pytest.mark.parametrize(
    "message",
    [None, {}, {"role": "user", "content": [{"text": "incomplete due to maximum token limits"}]}],
)
def test_annotating_something_else_changes_nothing(message):
    before = json.dumps(message)
    assert nxp_agent.annotate_cut_off_note(message, 8192) is False
    assert json.dumps(message) == before


# --- /api/gateway/status and the page's message ---------------------------------


def _client(agent):
    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(agent, runner=agent))
    return app.test_client()


def _events(body):
    out = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def _spec_agent(monkeypatch, base_url, cap, script=()):
    """An agent built from a ``ModelSpec`` (as the server does), with a scripted model."""
    model = FakeModel(list(script))
    monkeypatch.setattr(nxp_agent, "create_model", lambda spec, max_tokens=None: model)
    extra = {} if base_url == LOOPBACK else {"api_key_env": "K", "environ": {"K": SECRET}}
    spec = model_kwargs("openai", MODEL, base_url=base_url, **extra)["model"]
    return nxp_agent.NXPAgent(
        model=spec, stimulus_classifier=RuleBasedClassifier(), max_output_tokens=cap
    )


@pytest.mark.parametrize(
    ("base_url", "cap", "source"),
    [
        (LOOPBACK, 32_000, "local_default"),
        (PUBLIC, 16384, "unknown_limit_default"),
        (LOOPBACK, 20_000, "setting"),
    ],
)
def test_status_shows_the_cap_and_its_source_only(monkeypatch, base_url, cap, source):
    agent = _spec_agent(monkeypatch, base_url, cap)
    response = _client(agent).get("/api/gateway/status")
    shown = response.get_json()["max_output_tokens"]
    assert shown == {"value": cap, "source": source}
    assert source in CAP_SOURCES
    # Numbers and a code: no endpoint, key name or key value anywhere in the field.
    text = json.dumps(shown)
    for private in ("127.0.0.1", "18080", "example.com", "http", SECRET, '"K"', "/"):
        assert private not in text


def test_status_without_a_known_cap_is_null():
    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(SimpleNamespace(), runner=lambda _m: "ok"))
    status = app.test_client().get("/api/gateway/status").get_json()
    assert status["max_output_tokens"] is None


def test_the_cut_off_message_says_when_the_limit_is_an_automatic_default(monkeypatch):
    script = [truncated_tool_turn("file_write", CUT_ARGS)]
    agent = _spec_agent(monkeypatch, PUBLIC, 16384, script)
    events = _events(_client(agent).post("/api/chat", json={"message": "w"}).get_data(True))
    error = next(d for n, d in events if n == "error")
    assert (error["code"], error["limit"]) == ("max_tokens", 16384)
    assert error["limit_source"] == "unknown_limit_default"
    assert "automatic default 16384; set `max_output_tokens`" in error["error"]
    assert "自動の既定 16384" in error["error"]
    assert "cut off" in error["error"]


def test_the_cut_off_message_of_a_set_limit_is_unchanged(monkeypatch):
    script = [truncated_tool_turn("file_write", CUT_ARGS)]
    agent = _spec_agent(monkeypatch, LOOPBACK, 20_000, script)
    events = _events(_client(agent).post("/api/chat", json={"message": "w"}).get_data(True))
    error = next(d for n, d in events if n == "error")
    assert (error["limit"], error["limit_source"]) == (20_000, "setting")
    assert "automatic default" not in error["error"]
