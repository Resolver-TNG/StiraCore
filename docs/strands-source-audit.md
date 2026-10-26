# Strands SDK Source Audit

**Version:** 1.57.1
**Audited:** 2026-09-27 19:31

## Agent Constructor
Parameters: self, model, messages, tools, system_prompt, structured_output_model, callback_handler, conversation_manager, record_direct_tool_call, load_tools_from_directory, trace_attributes, agent_id, name, description, state, context_manager, plugins, hooks, interventions, session_manager, memory_manager, structured_output_prompt, tool_executor, retry_strategy, concurrent_invocation_mode, checkpointing, sandbox, storage, background_tasks

**hooks parameter:** YES
**callback_handler:** YES

## Agent.__call__
Parameters: self, prompt, invocation_state, structured_output_model, structured_output_prompt, idempotency_token, limits, cancel_signal, kwargs

## Hook Events (18 discovered)

| Event | Parameters |
|---|---|
| AfterInvocationEvent | agent, invocation_state, result, resume |
| AfterModelCallEvent | agent, invocation_state, stop_response, exception, retry |
| AfterMultiAgentInvocationEvent | source, invocation_state |
| AfterNodeCallEvent | source, node_id, invocation_state |
| AfterToolCallEvent | agent, selected_tool, tool_use, invocation_state, result, exception, cancel_message, duration, retry |
| AfterToolsEvent | agent, message, invocation_state, end_turn |
| AgentInitializedEvent | agent |
| BaseHookEvent |  |
| BeforeInvocationEvent | agent, invocation_state, messages, cancel |
| BeforeModelCallEvent | agent, invocation_state, projected_input_tokens, cancel |
| BeforeMultiAgentInvocationEvent | source, invocation_state |
| BeforeNodeCallEvent | source, node_id, invocation_state, cancel_node |
| BeforeToolCallEvent | agent, selected_tool, tool_use, invocation_state, cancel_tool |
| BeforeToolsEvent | agent, message, invocation_state, cancel |
| HookEvent | agent |
| MessageAddedEvent | agent, message |
| MessageUpdatedEvent | agent, tracking_id, message |
| MultiAgentInitializedEvent | source, invocation_state |

## Telemetry
Methods: setup_console_exporter, setup_meter, setup_otlp_exporter

## Recommended Integration
1. Use HookProvider with hooks=[] parameter
2. Key events: BeforeModelCallEvent, AfterToolCallEvent, AfterInvocationEvent
3. Use callback_handler for streaming text
4. Telemetry: StrandsTelemetry.setup_otlp_exporter() for Langfuse
