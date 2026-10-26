# Model calls on the turn events (#445)

Every model call of a chat turn is on the turn's events (`/api/turns/<id>/events`, `turns.db`):

| Event | Data |
| --- | --- |
| `model.started` | `call_id`, `model`, `started_at` |
| `model.progress` | `call_id`, `model`, `elapsed_ms` — every 10 s while the call runs |
| `model.completed` | `call_id`, `model`, `duration_ms`, `usage` (`input_tokens`, `output_tokens`), `stop`, `status` (and `retry`, `exception_type` when set) |

They come from the dashboard hooks (`observability/hooks.py`, `BeforeModelCallEvent` /
`AfterModelCallEvent`) through `UIEventSink`; `model.progress` is written by a small ticker
(`observability/model_progress.py`) that runs only while a call is open. A long call that
streams nothing visible (for example the model writing a large tool argument) therefore
shows that it is still running. The chat stream sends `model.progress` to the page at once
(it does not wait for the next chunk).

The events hold ids, the model id, numbers and fixed codes only: never the prompt, the
reply, a tool argument or a secret. A call's usage stays out of `/observability`'s cost
table (the turn's total in `turn.completed` is what it sums); the page shows the call as a
`model_call_start` / `model_call_end` span with its duration.

Screens: the chat shows "生成中 N 秒 / Generating N s" under the running reply (a page that
did not send the turn — another tab, a reload — shows it on its status line next to ■ Stop,
read from `/api/turns/<id>/events?after_seq=`, and between model calls the seconds since the
turn began, #555); the Stage's
text window says how long the model has been generating (from 5 s, in 5 s steps). From
60 s on both add a note to consider the output limit or splitting the request (display only).
Delta streaming and `turn.completed` are unchanged.
