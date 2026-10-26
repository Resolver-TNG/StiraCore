# Model per task (`task_models`)

[日本語](task-models_ja.md)

Some side tasks call a model besides the chat turn itself: the summary of context management
(#258), and the summary of a long tool result on request (#585, `web_fetch(summarize=True)`).
By default every one of them uses **the chat model** (`inherit`), exactly as before. You can
route a task to another model, for example a smaller one of the same provider.

## Tasks

| Task | What calls the model | Tools |
|---|---|---|
| `tool_summary` | `web_fetch(url, summarize=True, focus=…)` (#585, [tools/web-fetch.md](tools/web-fetch.md)). The output is untrusted data. Recorded also when it inherits the chat model. | none |
| `context_summary` | The conversation summary of context management (#258, opt-in `context_management`). | none |

Not routed (they keep their own settings or need the chat model):

| Caller | Why it stays out |
|---|---|
| LLM distiller (`distiller_mode: "llm"`) | Has its own model setting (`distiller_model_id`, ADR 0018). Two settings for one call would disagree. |
| `delegate` child agents (ADR 0016) | Run tools; a child on another model changes what can act, not only the cost. Needs its own decision. |
| Stimulus classifier (Sokudan / rules) | Not a chat model (a local classifier or rules). |
| Model switch, connection test, eval runner | They *are* the chat model choice, or a CLI with its own `--model`. |

## Setting it

Order, per task: the environment variable `STIRA_TASK_MODEL_<TASK>` (non-empty) wins, then
gateway `task_models`, then `inherit`. Read at start-up.

```json
{"task_models": {"tool_summary": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}}
```

```sh
STIRA_TASK_MODEL_TOOL_SUMMARY=global.anthropic.claude-haiku-4-5-20251001-v1:0
```

Values:

* `inherit`: the chat model (also the default).
* A bare model id: a model of **the chat's provider**. The chat's endpoint (`base_url`), key
  variable (`api_key_env`), organization variable and region are reused, so the data goes
  where the chat's data already goes.
* `provider:model` (e.g. `ollama:qwen3:8b`): another provider. None of the chat's endpoint or
  key settings carry over (Bedrock gets the region). Its credentials must already be set up
  (`credential_status` reports them configured: the SDK's default key variable, the AWS
  credential chain hints, or a local server). Stira does not look for new credentials.

## Fail closed

* At start-up, the server refuses to start (exit 2, a fixed code and the task name, never the
  value) when: the task or the `STIRA_TASK_MODEL_*` variable names an unknown task
  (`unknown_task`), the model is not in the capability table (`unknown_model`), the provider's
  SDK is missing (`provider_unavailable`), or the credentials are not set up
  (`credentials_not_configured`).
* When a routed model fails at call time the **task** fails (`model_failed` with a reason
  code). Stira never retries on the chat model or another model: the cost and the
  destination of the data do not change without you knowing. For `context_summary` this is
  the SDK's behaviour on a failed summary: the conversation is not shortened (a reactive
  overflow still ends the turn with the error).
* A task that takes no tools refuses a call that carries tool definitions
  (`tools_not_allowed`) before anything reaches a model — also when it inherits the chat
  model (the chat model is passed through behind the same boundary, the request otherwise
  unchanged).
* The variable name must be exactly `STIRA_TASK_MODEL_<TASK>` in upper case. Any other
  spelling (`STIRA_TASK_MODEL_tool_summary`, a lower-case prefix) refuses the start instead of
  being silently ignored.
* The fixed-code error does not chain the provider's or the builder's own exception, so its
  message (it can hold a key, a signed URL or page text) never reaches a traceback or a log.

## What is recorded

Each routed call — and each `tool_summary` call also when it inherits — adds one turn event
`task_model.call` (shown on `/observability`), one audit row `audit.task_model.call` and one
log line: the task, provider, model id, input and output tokens, the status (`success` /
`error` / `refused`) and a reason code. Never the text, never a URL.

## Settings page

Under **Output limit and cost → Model per task**, read-only: each task's effective model, and
whether it comes from the variable, the file or `inherit`. A saved setting that would refuse
the next start is shown with its code, also while a server runs (below the running rows). Change it in `gateway-config.json` or with the variable,
then restart.

## Residual risk

The routing is the operator's own choice in files and variables the operator controls. An
attacker who can edit them, or run code as the same OS user, is out of scope.
