"""#746: context management in long autonomous turns (offline, scripted models).

* the chat server's stash (``<data dir>/context-stash/``) works when ``workspace_dir``
  points at a project outside the data directory, without becoming a write root;
* a refusal has a reason code (no path, no secret) and a warning that names the fix;
* inside one turn's tool loop, the provider's reported input of the previous call
  triggers the summary even when the SDK's own estimate is low.
"""

from __future__ import annotations

import logging
import os

from strands import tool

from stiracore.agent import nxp_agent
from stiracore.agent.context_management import (
    OFFLOAD_MARKER,
    REFUSAL_REASONS,
    STIRA_SUMMARIZATION_PROMPT,
    ContextManagementSettings,
    status_view,
)
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.config.data_dir import build_policy, writable_names
from stiracore.config.models import AgentConfig
from stiracore.core import NeuralState
from stiracore.identity_paths import WritePathPolicy
from tests.provider_fakes import FakeModel, text_turn, tool_turn

SECRET = "AKIAABCDEFGHIJKLMNOP"
BIG = "\n".join(f"line {i}: value-{i * 7}" for i in range(1, 2001)) + f"\napi_key={SECRET}\n"
SUMMARY = "## Conversation Summary\n* Files were read; values were reported."
ON = ContextManagementSettings(enabled=True)


@tool
def big_read(path: str) -> str:
    """Read a large file."""
    return BIG


@tool
def small_read(path: str) -> str:
    """Read a small file."""
    return "tiny file"


TOOLS = (small_read, big_read)


def _agent(model, *, settings=ON, **kwargs):
    return nxp_agent.NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        tools=TOOLS,
        stimulus_classifier=RuleBasedClassifier(),
        context_management=settings,
        **kwargs,
    )


def _tool_result_text(request, index=-1):
    return request["messages"][index]["content"][0]["toolResult"]["content"][0]["text"]


def _moved_workspace(tmp_path):
    """A server data directory whose ``workspace_dir`` is a project elsewhere."""
    data_dir = tmp_path / "data"
    for name in writable_names("stira"):
        (data_dir / name).mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    config = AgentConfig(path=tmp_path / "agent-config.json", workspace_dir=str(project))
    policy, _floor = build_policy(config, data_dir, names=writable_names("stira"))
    return data_dir, project, policy


# ------------------------------------------------------------------ stash location


def test_data_dir_stash_is_on_when_the_workspace_is_moved(tmp_path):
    data_dir, _project, policy = _moved_workspace(tmp_path)
    stash = data_dir / "context-stash"
    model = FakeModel([tool_turn("big_read", {"path": "x"}, "tu-1"), text_turn("done")])
    agent = _agent(model, write_policy=policy, context_stash_dir=stash, agent_id="stira")
    agent("read it")

    assert agent.last_context_management["mode"] == "on"
    assert agent.context_refusal_reason is None
    assert _tool_result_text(model.requests[1]).startswith(OFFLOAD_MARKER)
    (entry,) = [p for p in stash.rglob("*") if p.is_file()]
    stored = entry.read_bytes()
    assert b"line 1500: value-10500" in stored
    assert SECRET.encode() not in stored and b"api_key=***" in stored  # masked (#185)
    if os.name != "nt":
        assert (stash / "stira").stat().st_mode & 0o077 == 0
    # The offloader's own location; the agent's file tools still may not write there.
    assert not policy.within_roots(stash / "stira" / "x")
    assert policy.check(str(stash / "stira" / "offloader" / "tu-1_0")).code == (
        "outside_write_roots"
    )
    assert policy.check(str(stash / "note.md")).allowed is False


def test_internal_stash_is_only_the_checked_context_stash_directory(tmp_path):
    data_dir, project, policy = _moved_workspace(tmp_path)

    def mode(stash_dir, write_policy=policy):
        model = FakeModel([text_turn("ok")])
        agent = _agent(model, write_policy=write_policy, context_stash_dir=stash_dir)
        agent("hello")
        return agent.last_context_management["mode"], agent.context_refusal_reason

    # Elsewhere in the data directory (the floor), and outside every root: refused.
    assert mode(data_dir / "memory") == ("refused", "stash_outside_write_roots")
    assert mode(tmp_path / "elsewhere") == ("refused", "stash_outside_write_roots")
    assert not (tmp_path / "elsewhere").exists()
    # A context-stash/ that is a symlink is never followed (the exception is ignored).
    other = tmp_path / "data2"
    (other / "workspace").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (other / "context-stash").symlink_to(target, target_is_directory=True)
    config = AgentConfig(path=tmp_path / "a.json", workspace_dir=str(project))
    linked, _floor = build_policy(config, other, names=writable_names("stira"))
    assert mode(other / "context-stash", linked) == ("refused", "stash_outside_write_roots")
    assert list(target.iterdir()) == []
    # A policy without a data directory keeps the old rule (write roots only).
    plain = WritePathPolicy((), base_dir=project, write_roots=["@workspace"])
    assert mode(data_dir / "context-stash", plain) == ("refused", "stash_outside_write_roots")


# ------------------------------------------------------------------ refusal is visible


def test_refusal_has_a_reason_code_and_a_warning_that_names_the_fix(tmp_path, caplog):
    project = tmp_path / f"project-{SECRET}"
    project.mkdir()
    policy = WritePathPolicy((), base_dir=project, write_roots=["@workspace"])
    model = FakeModel([text_turn("ok"), text_turn("ok")])
    agent = _agent(model, write_policy=policy, context_stash_dir=tmp_path / f"stash-{SECRET}")
    with caplog.at_level(logging.WARNING, logger="stiracore.agent.nxp_agent"):
        agent("hello")
        agent("again")

    assert agent.last_context_management == {"mode": "refused"}
    assert agent.context_refusal_reason == "stash_outside_write_roots"
    view = status_view(agent)
    assert view == {"enabled": True, "mode": "refused", "reason": "stash_outside_write_roots"}
    assert view["reason"] in REFUSAL_REASONS
    warnings = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(warnings) == 1  # once, not every turn
    assert "stash_outside_write_roots" in warnings[0] and "context-stash" in warnings[0]
    # No path and no secret in the status or the log line.
    for text in (str(view), warnings[0]):
        assert SECRET not in text and str(tmp_path) not in text


def test_status_view_reports_only_allowlisted_values(tmp_path):
    off = _agent(FakeModel([text_turn("ok")]), settings=ContextManagementSettings())
    assert status_view(off) == {"enabled": False, "mode": None, "reason": None}
    off("hello")
    assert status_view(off) == {"enabled": False, "mode": "off", "reason": None}

    missing = _agent(FakeModel([text_turn("ok")]))
    missing("hello")
    assert status_view(missing) == {"enabled": True, "mode": "refused", "reason": "no_stash_dir"}

    on = _agent(FakeModel([text_turn("ok")]), context_stash_dir=tmp_path / "stash")
    on("hello")
    assert status_view(on) == {"enabled": True, "mode": "on", "reason": None}
    # A value outside the allowlists is never passed on.
    on._last_context_management = {"mode": "/etc/passwd"}
    on._context_refusal = "free text"
    assert status_view(on) == {"enabled": True, "mode": None, "reason": None}


# ------------------------------------------------------------------ tool loop


class ReportingFake(FakeModel):
    """Scripted turns with the usage a provider reports; answers the summarizer call."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if system_prompt == STIRA_SUMMARIZATION_PROMPT:
            usage = {"inputTokens": 50, "outputTokens": 5, "totalTokens": 55}
            self.script.insert(0, text_turn(SUMMARY, usage=usage))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def _reported(turn, input_tokens):
    usage = {"inputTokens": input_tokens, "outputTokens": 5, "totalTokens": input_tokens + 5}
    return [*turn, {"usage": usage}]


def _loop_script(reported):
    """One turn: a tool call per reported input size, then the final text."""
    calls = [
        _reported(tool_turn("small_read", {"path": f"f{i}"}, f"tu-{i}"), tokens)
        for i, tokens in enumerate(reported)
    ]
    return [*calls, _reported(text_turn("done"), 700)]


def _summary_positions(model):
    return [
        index
        for index, request in enumerate(model.requests)
        if request["system_prompt"] == STIRA_SUMMARIZATION_PROMPT
    ]


def test_reported_input_past_the_threshold_summarizes_once_inside_the_turn(tmp_path):
    # The history itself is tiny (the SDK's estimate stays far below the threshold);
    # only the provider's reported input grows: 600 -> 9,000 of a 10,000 window.
    settings = ContextManagementSettings(
        enabled=True, context_window_tokens=10_000, compression_threshold=0.85, preserve_recent=2
    )
    model = ReportingFake(_loop_script([600, 1_200, 2_400, 4_800, 9_000, 600, 700]))
    agent = _agent(model, settings=settings, context_stash_dir=tmp_path / "stash")
    agent("work through the files")

    # Calls 0-4 are the loop up to the 9,000-token report; the summary comes next,
    # before the sixth model call, and only once.
    assert _summary_positions(model) == [5]
    after = model.requests[6]["messages"]
    assert after[0]["content"][0]["text"].startswith(SUMMARY)
    record = agent.last_context_management
    assert record["mode"] == "on" and record["summaries"] == 1
    assert record["summarized_messages"] >= 1
    # Tool-use / tool-result pairs are not split by the summary.
    for index, message in enumerate(after):
        if any("toolResult" in block for block in message["content"]):
            assert any("toolUse" in block for block in after[index - 1]["content"])


def test_reported_input_below_the_threshold_does_not_summarize(tmp_path):
    settings = ContextManagementSettings(
        enabled=True, context_window_tokens=10_000, compression_threshold=0.85, preserve_recent=2
    )
    model = ReportingFake(_loop_script([600, 1_200, 2_400, 4_800, 8_000, 8_200, 8_400]))
    agent = _agent(model, settings=settings, context_stash_dir=tmp_path / "stash")
    agent("work through the files")
    assert _summary_positions(model) == []
    assert agent.last_context_management["summaries"] == 0


def test_gateway_status_carries_the_refusal_reason(monkeypatch):
    """#746: the status field is the allow-listed view: enabled, mode, reason (a code)."""
    from stiracore.agent.context_management import status_view

    class _Settings:
        enabled = True

    class _Agent:
        def __init__(self):
            self.context_management = _Settings()
            self.last_context_management = {"mode": "refused"}
            self.context_refusal_reason = "stash_outside_write_roots"

    assert status_view(_Agent()) == {
        "enabled": True,
        "mode": "refused",
        "reason": "stash_outside_write_roots",
    }
