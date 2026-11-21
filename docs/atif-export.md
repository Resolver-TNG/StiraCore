# ATIF export (one turn, codes only)

English | [日本語](atif-export_ja.md)

Writes one recorded turn as an ATIF (Agent Trajectory Interchange Format) JSON file, so an
outside tool that reads agent trajectories can be tried offline (#700). One way: nothing
is read back. The internal turn-event format is unchanged; the mapping lives in one
adapter (`stiracore.observability.atif_export`).

**Off unless an operator runs it.** No setting or environment variable turns it on, no
agent tool runs it and the server never calls it. It calls no model, sends nothing over
the network and writes nothing into the data directory.

```bash
python -m stiracore.control.atif_export_cli --data-dir <data dir> --out <new file> \
    [--turn-id <turn id>] [--include-untrusted]
```

* `--out` must not exist yet and must be outside the data directory (created with mode
  0600; never overwritten).
* Without `--turn-id` the last turn that has ended is taken.
* Exit codes: 0 written, 1 not written (a reason code on standard error), 2 usage error.

## What is in the file

**Ids, numbers and codes only — no body.** ATIF expects the prompt, the reply, tool
arguments and tool results. Stira's turn events hold no prompt or reply and only masked
previews of the others, and this export reads none of them:

| ATIF field | Value |
|---|---|
| `schema_version` | `ATIF-v1.4` (see Limits) |
| `session_id` | the turn id (turn events carry no session id) |
| `agent` | `name: stiracore`, the package version, the model id of `turn.completed` |
| `steps[].source` | `user` for `turn.started` (`system` when the turn is an automatic continuation), `agent` for each `model.completed` |
| `steps[].message` | always `""` |
| `steps[].metrics` | `prompt_tokens` / `completion_tokens` of the model call |
| `steps[].tool_calls[]` | `tool_call_id`, `function_name`, `arguments: {}` (always empty) |
| `steps[].observation.results[]` | `source_call_id`, and `content` = the **status code** of the call (`done`, `error`, `denied`, …), never its output |
| `final_metrics` | token totals of `turn.completed`, `total_steps` |
| `extra.stira` (top level, per step, in `final_metrics`) | `prompt_chars`, `image_count`, model call id / duration / stop code / status, per tool call `status` / `error_class` / `deny_code` / `duration_ms`, approval request ids and decision codes, breaker transitions, the turn's stop code and reason codes, `context_trust`, and how many events were not mapped or skipped |

Everything is an allow-list (`atif_export.build`): a value is taken only when it is a
number, a member of a fixed code set, or one token the harness wrote. A tool call id that
is not one token (or that `mask_secrets` would rewrite) becomes `call-<n>`. A tool name
that is not one identifier becomes `unnamed_tool`, and the name of a tool that does not
exist (`error_class: unknown_tool`) becomes `unknown_tool`, because it is text the model
chose. Every string of the result is passed through `mask_secrets` once more.

## Untrusted turns

When taint observation (ADR 0022 phase 0, [taint.md](taint.md)) recorded the turn's
context as `untrusted`, the turn is refused (`turn_untrusted`) unless `--include-untrusted`
is given; the output is codes only either way. Taint observation is off by default, so
most turns have no label: they are exported with `context_trust: unknown`.

## Reason codes (exit 1)

`data_dir_missing`, `turns_db_missing`, `turns_db_unreadable`, `turn_not_found`,
`out_exists`, `out_inside_data_dir`, `out_unwritable`; from the adapter: `no_events`,
`turn_id_mismatch`, `turn_id_unsafe`, `turn_not_completed`, `turn_untrusted`.

## Limits

* **The ATIF layout was not verified.** The field names are a minimal subset written
  without access to the specification; the file was not run through an ATIF validator or
  an outside tool. A reader that requires message text will find empty strings.
* **No export with bodies.** #700 proposes one for an explicit human action; it waits for
  the decision record on exports for training and outside use, which does not exist yet.
  When it is built, a turn without a taint label must be treated as untrusted.
* **Grant iterations are not exported.** They are recorded in the lifecycle log, not as
  turn events, and nothing links an iteration to a turn.
* `turns.db` is read from a copy in a private temporary directory (removed afterwards), a
  snapshot taken without a lock; a torn copy is reported as `turns_db_unreadable`.
* A dropped turn event (`TurnEventRecorder.drop_counts`) is missing from the file.

## What the turn events lack for a trajectory (input for #277)

Found by mapping one turn by hand:

| Missing | Effect on the export |
|---|---|
| Session / conversation id | `session_id` is the turn id; turns of one conversation cannot be joined |
| Subject (who started the turn: person, scheduler, grant) | only `auto_continue` is visible; every other turn is exported as `user` |
| Correlation id between a turn and a grant iteration / lifecycle event | grant iterations cannot be placed in a trajectory |
| Link from a tool call to the model call that asked for it | tool calls are attached to the preceding `model.completed` by order |
| Link from an approval to its tool call | approvals are listed at the top level, not on a step |
| Sub-agent (delegate) trajectory reference | not exported |
| Cache token counts and cost per model call | `metrics` has two token counts only |
