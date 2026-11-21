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
| `visual_review` | `page_review(run)` (ADR 0015 rev 6, #774, [verification.md](verification.md)): remarks on one page run's screenshots. **Unset = off** (see below). Needs a `vision` model. The output is untrusted data. Recorded also when it inherits the chat model. | none |

### `visual_review`: off unless you set it

Unlike the other tasks, an unset `visual_review` is **off, not `inherit`**: no model is
built, the tool `page_review` is not registered (the tool definitions the chat model gets are
byte-identical), nothing is sent, and the task has no row on the settings page.

* To use the chat model as the reviewer, write `inherit` out. It is accepted only when the
  chat model's capability row says `vision`. If you switch the chat model on the settings
  page while the server runs, a review follows the new model only when it is of the same
  provider and has `vision` (a capability override in `agent-config.json` counts only for
  the model the server started with); otherwise `page_review` is refused
  `reviewer_changed` and nothing is sent until you switch back or restart.
* A routed model must have `vision` too. Otherwise the start is refused (`no_vision`).
* **Where the pixels go:** to this model's provider and nowhere else. A local model (e.g.
  `ollama:llama3.2-vision:11b`, or `inherit` with a local vision chat model) keeps them on
  the machine. A remote one receives whatever the page drew, which can include workspace
  text; pixels cannot be masked.
* It also needs a budget: `visual_review.daily_tokens` in `gateway-config.json` (a positive
  integer; **no default**). Without it every `page_review` call is refused `review_budget`.
* The model built for this task is capped at 800 output tokens. With `inherit` the chat
  model keeps its own token cap. In both cases Stira closes the answer's stream once it
  passes 3,200 characters (`review_unparsable`).
* No fallback: a reviewer that fails, times out (60 s; Stira stops waiting then, whatever
  the provider does) or answers outside the schema gives
  `review: {status: "error", code}`. The chat model is never asked instead.

```json
{"task_models": {"visual_review": "ollama:llama3.2-vision:11b"}, "visual_review": {"daily_tokens": 200000}}
```

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
  SDK is missing (`provider_unavailable`), the credentials are not set up
  (`credentials_not_configured`), or `visual_review` names a model without `vision`
  (`no_vision`; with `inherit`, the chat model).
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
`error` / `refused`) and a reason code. Never the text, never a URL. A `visual_review` call
(recorded also on `inherit`) adds three numbers: `images`, `bytes` (their total size) and
`rubric_version`. Never the pixels or a note.

## Settings page

Under **Output limit and cost → Model per task**, read-only: each task's effective model, and
whether it comes from the variable, the file or `inherit`. A saved setting that would refuse
the next start is shown with its code, also while a server runs (below the running rows). Change it in `gateway-config.json` or with the variable,
then restart.

## Residual risk

The routing is the operator's own choice in files and variables the operator controls. An
attacker who can edit them, or run code as the same OS user, is out of scope.
