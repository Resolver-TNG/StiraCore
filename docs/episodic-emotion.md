# Episodic emotion snapshots and opt-in emotion-proximity recall (#364)

日本語: [episodic-emotion_ja.md](episodic-emotion_ja.md)

**What changes by default:** every new chat episode is `episodic-summary/v2` and has a
sidecar record of the emotion at the moment it was saved. **What does not change:** the
episode text, `memory_read` / `memory_search` output and matching, the prompt, Harvest,
Neural State, `EMOTION_MATRIX` and the classifier. Recall by emotion exists only as a
library function that nothing calls. No model call, no token.

**Opt-out:** `episodic_emotion_snapshot: false` in `agent-config.json`, or
`STIRA_EPISODIC_EMOTION=0` (a non-empty value wins over the config: `1`/`true`/`yes`/`on`
= on, anything else = off). The chat server reads the switch each time it writes an
episode; an unreadable `agent-config.json` counts as off. The Discord/Slack runtime reads
the config when a conversation starts and the variable on every turn. Off: new episodes
are `episodic-summary/v1`, no record is written. Records already written stay until they
are deleted (below).

## What is recorded

Right after the turn returned (after `post_turn`), `capture_emotion_snapshot(agent,
turn_id=...)` copies, read-only:

| Field | Source | Stored as |
|---|---|---|
| `state.trust` … `state.satisfaction` (6) | `agent.engine.state`, copied under `engine.state_lock` | number in [0, 1], **raw** (as captured, not rounded; ADR 0017 Q11), or `null`. Records written before this change hold 3 decimals |
| `category` | the turn's classifier verdict (`last_turn_summary`) | a key of `EMOTION_MATRIX`, else `null` (abstain, failed turn, unknown label) |
| `intensity` | same | finite number ≥ 0, 3 decimals, or `null` |
| `breaker` | the level `post_turn` evaluated | `normal` / `warning` / `critical` / `coherence` or `null` |
| `turn` | the engine's turn counter | integer ≥ 0 or `null` |
| `turn_id` | the chat UI turn (`turns.db`), when the turn came from the chat page | `[A-Za-z0-9_-]{1,64}` or `null` |

`stability` is one of the six dimensions (`snapshot.stability`). Every field is validated
on its own; an invalid one is `null`, never a guess. Not recorded: `bc_score`,
`confidence`, `emotion_transcription`, deltas, timestamps (the episode file has its own).

Wired in two places, one argument each: the chat server runner
(`dashboard/stira_server.py`) and the Discord/Slack runtime
(`integrations/runtime.ChatSession`, no `turn_id`). A turn that raises writes no episode
and records nothing. If the snapshot cannot be taken, the episode is v1 as before.

## Where it is kept, and why not in the frontmatter

The episodic layer is an exception of the data-directory floor: the agent's file tools
can read and write `episodic/` (#312), and `memory_read` / `memory_search` print an
entry's metadata to the model and index it for literal search. Emotion values are
personal data, handled like the rest of the data directory (not readable by the agent).
So they are **not** in the episode; it only says `format: episodic-summary/v2`.

The record is `<memory_dir>/emotion/<sha256(key)[:32]>.json` (next to `forgotten/`),
inside the data directory and therefore on the read and write floor of the file tools.
The directory is created `0700` (an existing one with group/other bits is set back to
`0700`), records are `0600` (POSIX), written atomically (temporary file, `fsync`,
`os.replace`) and read at most 4 KiB. **Symbolic links:** on POSIX a record is opened
with `O_NOFOLLOW`, so a link is never followed. Windows has no `O_NOFOLLOW`: a link is
refused by an `lstat` check before the open (best effort, not race-free), and permissions
are those of the data directory's ACL.

```json
{"schema": "episodic-emotion/v1", "key": "chat-…", "content_sha256": "…",
 "state": {"trust": 0.6123456, …}, "category": "joy", "intensity": 0.75,
 "breaker": "normal", "turn": 7, "turn_id": "…"}
```

A record belongs to an episode only if `key` and the SHA-256 of the stored body match.
`read_emotion_snapshot(store, entry)` returns all `null` with a `status`:

| `status` | Meaning |
|---|---|
| `stored` | a matching record |
| `v1` | a v1 or unversioned episode (nothing is opened) |
| `missing` | v2, but no readable record (write failed, symlink, oversized, broken) |
| `mismatch` | a record for another body (the episode was rewritten) |

The agent can rewrite an episode, but not the record: a rewritten body no longer
matches, so it cannot borrow another turn's emotion.

**What the binding covers, and what not.** The record is bound to the episode's **key and
body** only. The rest of the frontmatter (`source`, `importance`, `method`, Harvest marks,
…) is not covered: an episode whose body is unchanged but whose frontmatter was edited
(by the agent's file tools, or by Harvest marking it) still reads as `stored`. So the
snapshot proves "these values were recorded when this text was saved", nothing about the
other metadata. The values themselves cannot be changed by the agent (the record is on
the write floor); whoever can write the data directory can change anything.

## v1 compatibility

Old episodes are never rewritten or back-filled. They read as v1 with every field `null`
and take no part in emotion recall. `FORMAT_VERSION` (v1) is unchanged for callers that
pass no snapshot.

## Opt-in recall: `recall_by_emotion(store, current, *, limit=5, window=50)`

Ranks the `window` newest episodes (at most 1000) by closeness of their stored state to
`current` (a `NeuralState` or a mapping of the six dimensions). Episodes without a
complete stored snapshot are left out. Order: distance, then newer first, then key.
Returns `(entry, distance)`, never the stored values. It reads `window` episodes and
their records; no write, no access-time update, no model call. **Nothing in Stira calls
it** (a test checks this); `memory_search`, the prompt and Harvest are unchanged.

### Distance

Root-mean-square difference over the six dimensions:

```
d(a, b) = sqrt( Σ_i (a_i − b_i)² / 6 ),   i ∈ {trust, curiosity, vigilance, attachment, stability, satisfaction}
```

Every dimension is in [0, 1], so `d` is in [0, 1] (0: same state, 1: opposite corners).
All dimensions weigh the same; no normalization beyond the 1/6.

**Tolerance.** Stored values are raw since the emotion-proximity wiring (ADR 0017 Q11: "emotion
values go in as raw values"), so a new record adds no error. A record written before holds
3 decimals, so each of its dimensions is off by at most `e = 0.0005`. The RMS is a norm (scaled Euclidean), so by the triangle
inequality `|d(a', b) − d(a, b)| ≤ d(a', a) ≤ e = 0.0005` (`DISTANCE_TOLERANCE`). A test
checks this on 5000 random pairs.

### Not implemented

- **Polarity proximity as a bonus** ("prefer memories of the same valence"): decided against
  (ADR 0017 Q2). A valence index (`valence_index`, `polarity`) exists, but only as a guard of
  the recall signal below and as a metric.
- Weights per dimension, use in the prompt or a tool.

**Wired since ADR 0017 (opt-in, default off):** the associative recall's `emotion_proximity`
signal uses `mood_similarity` — the same RMS over the four mood dimensions only (curiosity,
vigilance, stability, satisfaction; trust/attachment stay in recall's bond term), as
`s = max(0, 1 − d / 0.2)` — to re-rank already relevant episodes, with its guards (see
[associative-recall.md](associative-recall.md)). `recall_by_emotion` itself still has no
caller.

A design for wiring proximity into associative recall (distance, polarity, bounds, evaluation)
is decided in [ADR 0017](adr/0017-emotion-proximity-recall.md) (Accepted 2026-10-20; phase 1, default off).

## Privacy and deletion

- The values never reach the model through Stira (not in the prompt, the memory tools,
  the search index or the LLM distiller's prompt) and are not readable by the file tools.
- The operator can read them (it is their data directory).
- Logs carry fixed codes and exception types only: `E_EMOTION_CAPTURE`,
  `E_EMOTION_TURN_ID`, `E_EMOTION_WRITE_FAILED`, `E_EMOTION_READ`, `E_EMOTION_INVALID`.
- **Deleting an episode deletes its record.** `MemoryStore.delete` removes the record
  first and then the episode; if the record cannot be removed, the call fails and the
  episode stays, so a retry deletes both. No other path removes or moves an episode
  today: forgetting (`--apply-forgetting`, `forgotten/`) handles Semantic entries only,
  and Harvest only adds marks to an episode (its body, so the record, stays matching).
- **Orphans** (an episode deleted outside `MemoryStore`, rewritten so it no longer
  matches, broken files, old temporary files) are found and removed by an operator
  command:

  ```bash
  python -m stiracore.control.emotion_cli --base-dir <data dir>/memory orphans          # dry run, writes nothing
  python -m stiracore.control.emotion_cli --base-dir <data dir>/memory orphans --apply  # delete
  ```

  Reasons: `no-episode`, `stale`, `misnamed`, `unreadable`, `temporary` (older than five
  minutes), `not-a-file` (a directory; reported, never removed). An episode that cannot
  be read is not judged. `--apply` appends one line per file to
  `<memory_dir>/emotion-log.jsonl` (time, file name, reason; `0600`) *before* removing it;
  if the log cannot be written, the run stops (exit 1) and nothing more is removed. The
  log is plain JSONL, not hash-chained. Output names files and reasons only, never values
  or episode keys. Run it with the server stopped or idle (no owner lock is taken).
- Opting out does not delete what was recorded. To remove every snapshot, delete
  `emotion/` (only the snapshots are lost; episodes then read as `missing`).

## Cost

One extra small file write (about 400 bytes) per turn, no model call, no token. Recall reads
at most `window` files plus their records, only when called.
