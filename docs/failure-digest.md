# Failure digest

English | [日本語](failure-digest_ja.md)

Which failure happened how often, and where to look: a deterministic count of the failures
recorded in one data directory, with representative event ids (#695). It calls no model,
runs only when an operator runs it, and writes nothing into the data directory — and
nothing to memory or a prompt.

```bash
python -m stiracore.control.failure_digest_cli --data-dir <data dir> \
    [--days 7] [--now 2026-11-10T00:00:00+00:00] [--format md|json] [--top 3] [--examples 3]
```

It prints Markdown (or JSON) to standard output. Exit codes: 0 done, 1 the data directory
or one of the two sources cannot be read (the digest of the other is still printed, with a
note), 2 usage error.

## What is counted

| Source | Rows | Pattern |
|---|---|---|
| `turns.db` (read from a copy in a temporary directory) | `tool_call.updated` with `status` `error` or `denied` | `tool:<status>:<error_class>[:<deny_code>]` |
| `authority-lifecycle*.jsonl` (read without rotation) | events of a grant, a goal, an iteration or a heartbeat start with a failure `reason_code` | `grant:<subject_kind>:<reason_code>` |

* `error_class` / `deny_code` are the fixed sets of #696 (`unknown_tool`, `invalid_input`,
  `refused`, `tool_failed`, `exception`; `rule_never`, `declared`, `other`). A row without
  one (written before #696) or with any other value is `unclassified`.
* A checkpoint the agent declared itself (`status: denied` with `deny_code: declared`) is
  not a failure: it states progress, no gate refused anything. Exactly that pair is left
  out and its number is in the header ("Excluded (not failures)"); any other row with
  `declared` (an `error`) is counted.
* Failure reason codes of a grant: the iteration ends except the successes
  (`iteration_done`, `iteration_claimed`) and the interruptions (`iteration_yielded_user_turn`,
  `iteration_shutdown`, `iteration_stopped`, `iteration_permit_stale`), plus every
  `blocked_*`, `halted_*` and `tick_skipped_*`.

## What is printed

The number of failures, what the largest `--top` patterns sum to (count and share of all
failures), and per pattern its count, share, first and last time and at most `--examples`
representative events, newest first: `event_id` and `turn_id` for a tool call, `event_id`
and `subject_id` (the grant id) for a grant, which has no turn.

**Codes, counts, times and ids only.** `turns.db` also holds masked previews and tool
names; each row is reduced to an allow-list (`failure_digest.TOOL_FIELDS`) as it is read.
The tool name is not read either: for an unknown tool it is text the model chose.

## Limits

* **Nothing is written into the data directory.** `turns.db` (and its `-wal`, if any) is
  copied to a private temporary directory, the copy is opened read only and removed. The
  copy holds the masked previews for that moment. It is a snapshot taken without a lock: a
  copy torn by a concurrent checkpoint is reported as `turns_db_unreadable` (run it again).
* **Counts are a lower bound.** A turn event that could not be recorded was dropped; the
  counter of those drops (`TurnEventRecorder.drop_counts`) lives in the running server
  process and cannot be read by this command. A rotated lifecycle segment is gone, and
  `tick_skipped_*` is written once per grant and reason in a process.
* One incident can appear as more than one pattern (an iteration's end and its goal's block).
* The pure function is `stiracore.observability.failure_digest.build`; it reads no clock
  and no store, so a caller that has the drop counts can pass them (`drop_counts=`).
