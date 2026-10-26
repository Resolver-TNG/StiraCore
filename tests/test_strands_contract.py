"""Offline Strands SDK contract tests (no Bedrock calls)."""

import inspect


def test_agent_accepts_hooks():
    from strands import Agent

    params = inspect.signature(Agent.__init__).parameters
    assert "hooks" in params


def test_agent_accepts_callback_handler():
    from strands import Agent

    params = inspect.signature(Agent.__init__).parameters
    assert "callback_handler" in params


def test_hook_afterinvocationevent_importable():
    from strands.hooks.events import AfterInvocationEvent

    sig = inspect.signature(AfterInvocationEvent)
    assert "agent" in sig.parameters
    assert "invocation_state" in sig.parameters
    assert "result" in sig.parameters


def test_hook_aftermodelcallevent_importable():
    from strands.hooks.events import AfterModelCallEvent

    sig = inspect.signature(AfterModelCallEvent)
    assert "agent" in sig.parameters
    assert "invocation_state" in sig.parameters
    assert "stop_response" in sig.parameters


def test_hook_aftermultiagentinvocationevent_importable():
    from strands.hooks.events import AfterMultiAgentInvocationEvent

    sig = inspect.signature(AfterMultiAgentInvocationEvent)
    assert "source" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_afternodecallevent_importable():
    from strands.hooks.events import AfterNodeCallEvent

    sig = inspect.signature(AfterNodeCallEvent)
    assert "source" in sig.parameters
    assert "node_id" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_aftertoolcallevent_importable():
    from strands.hooks.events import AfterToolCallEvent

    sig = inspect.signature(AfterToolCallEvent)
    assert "agent" in sig.parameters
    assert "selected_tool" in sig.parameters
    assert "tool_use" in sig.parameters


def test_hook_aftertoolsevent_importable():
    from strands.hooks.events import AfterToolsEvent

    sig = inspect.signature(AfterToolsEvent)
    assert "agent" in sig.parameters
    assert "message" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_agentinitializedevent_importable():
    from strands.hooks.events import AgentInitializedEvent

    sig = inspect.signature(AgentInitializedEvent)
    assert "agent" in sig.parameters


def test_hook_basehookevent_importable():
    from strands.hooks.events import BaseHookEvent

    inspect.signature(BaseHookEvent)


def test_hook_beforeinvocationevent_importable():
    from strands.hooks.events import BeforeInvocationEvent

    sig = inspect.signature(BeforeInvocationEvent)
    assert "agent" in sig.parameters
    assert "invocation_state" in sig.parameters
    assert "messages" in sig.parameters


def test_hook_beforemodelcallevent_importable():
    from strands.hooks.events import BeforeModelCallEvent

    sig = inspect.signature(BeforeModelCallEvent)
    assert "agent" in sig.parameters
    assert "invocation_state" in sig.parameters
    assert "projected_input_tokens" in sig.parameters


def test_hook_beforemultiagentinvocationevent_importable():
    from strands.hooks.events import BeforeMultiAgentInvocationEvent

    sig = inspect.signature(BeforeMultiAgentInvocationEvent)
    assert "source" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_beforenodecallevent_importable():
    from strands.hooks.events import BeforeNodeCallEvent

    sig = inspect.signature(BeforeNodeCallEvent)
    assert "source" in sig.parameters
    assert "node_id" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_beforetoolcallevent_importable():
    from strands.hooks.events import BeforeToolCallEvent

    sig = inspect.signature(BeforeToolCallEvent)
    assert "agent" in sig.parameters
    assert "selected_tool" in sig.parameters
    assert "tool_use" in sig.parameters


def test_hook_beforetoolsevent_importable():
    from strands.hooks.events import BeforeToolsEvent

    sig = inspect.signature(BeforeToolsEvent)
    assert "agent" in sig.parameters
    assert "message" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_hook_hookevent_importable():
    from strands.hooks.events import HookEvent

    sig = inspect.signature(HookEvent)
    assert "agent" in sig.parameters


def test_hook_messageaddedevent_importable():
    from strands.hooks.events import MessageAddedEvent

    sig = inspect.signature(MessageAddedEvent)
    assert "agent" in sig.parameters
    assert "message" in sig.parameters


def test_hook_messageupdatedevent_importable():
    from strands.hooks.events import MessageUpdatedEvent

    sig = inspect.signature(MessageUpdatedEvent)
    assert "agent" in sig.parameters
    assert "tracking_id" in sig.parameters
    assert "message" in sig.parameters


def test_hook_multiagentinitializedevent_importable():
    from strands.hooks.events import MultiAgentInitializedEvent

    sig = inspect.signature(MultiAgentInitializedEvent)
    assert "source" in sig.parameters
    assert "invocation_state" in sig.parameters


def test_telemetry_importable():
    from strands.telemetry import StrandsTelemetry

    assert hasattr(StrandsTelemetry, "setup_otlp_exporter")
