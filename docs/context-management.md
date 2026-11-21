# Context management (#258)

Large tool results (a file read, a long search result, an HTTP body) stay in the
conversation for the rest of the session and are re-sent with every model call.
Context management keeps a short preview in the conversation, stores the
original on disk so the model can read it back on demand, and summarizes the
oldest part of the history when it approaches the context window. The design
decision and the rejected options are in
[ADR 0005](adr/0005-context-management.md); the measurement is in
research/context-management-2026-10-15.md.

## Turning it on

Opt-in, off by default. In the gateway config:

```json
{
  "context_management": true,
  "context_management_options": {"offload_threshold_tokens": 1500, "preview_tokens": 750}
}
```

or start the server with `STIRA_CONTEXT_MANAGEMENT=1` (`0` turns a configured
`true` off). Library use: `NXPAgent(..., context_management=ContextManagementSettings(enabled=True),
context_stash_dir=<directory>)`.

| Option | Default | Meaning |
|---|---|---|
| `offload_threshold_tokens` | 1500 | a tool result above this (SDK token estimate) is offloaded |
| `preview_tokens` | 750 | size of the preview left in the conversation (must be below the threshold) |
| `compression_threshold` | 0.85 | summarize when the projected input reaches this share of the context window |
| `summary_ratio` | 0.8 | share of the history summarized at once (0.1–0.8) |
| `preserve_recent` | 4 | messages always kept verbatim |
| `context_window_tokens` | unset | window for the utilization; unset = model config, capability row, the window a local server reports (see "Window from a local server"), else 200,000 |
| `retention_hours` | 24 | stash entries older than this are deleted |
| `retrieval_max_tokens` | 10000 | longest read-back returned to the model |

Unknown option names and out-of-range values are refused when the config is
loaded. Defaults are the official Strands harness values, kept as settings; see
the ADR for why each was kept.

## What happens

- **Offload.** When a tool returns more than the threshold, the model receives
  `[Offloaded: N blocks, ~T tokens]`, short guidance, the first
  `preview_tokens` of the text and `[Stored references:]` with keys such as
  `tooluse_abc_0`. The full result never enters the history.
- **Read-back.** The tool `retrieve_offloaded_content` (`reference`, optional
  `pattern`, `line_range`, `context_lines`) returns matching lines, a span, or
  the whole entry (capped at `retrieval_max_tokens`). It is added only while the
  feature is on, after the other tools (name-sorted when prompt caching is on),
  so the tool set is the same every turn.
- **Summary.** Before a model call whose projected input reaches the threshold,
  the oldest `summary_ratio` of the history (tool-use/tool-result pairs are never
  split; at least `preserve_recent` messages stay) is replaced by one user
  message starting `## Conversation Summary`. The summarizer is told to keep
  identifiers, references, decisions, open items, pending tool calls and
  approvals, and not to restate or reinterpret the persona. It uses the
  configured model, without prompt-cache settings. Injected-tag look-alikes in
  the summary are defanged.
- It replaces the SDK's default sliding window (40 messages) for that agent.
  With the option off nothing changes: same tools, same system prompt, same
  sliding window.

## Where the originals are kept

- `<context_stash_dir>/<agent_id>/offloader/<toolUseId>_<n>`. The chat server uses
  `.local/stira/context-stash/`. The directory is created with mode 0700.
- It must not be on the identity floor, and it must be either inside the agent's
  write roots (#243) or the chat server's own `<data dir>/context-stash/` (#746).
  The offloader writes there itself, not through a file tool, so the server's
  stash works when `workspace_dir` points at a project outside the data
  directory; it is not added to the write roots, and file tools are still
  refused there. It counts only while `context-stash/` is a real directory (a
  symbolic link is never followed). The agent id must be a plain name (letters,
  digits, `_ . -`). Otherwise the feature is refused (one warning that names the
  fix, nothing offloaded or summarized); see "Visibility" for the reason codes.
  Without a stash directory it is refused too: an offload whose original
  cannot be read back would lose data.
- Text and JSON are secret-masked (#185) before they are written. A secret that
  was outside the preview is read back as `***`. Images and documents are never
  written; a result containing one is kept in the conversation unchanged.
- The same directory is used every turn and after a restart, so a reference
  stays readable as long as the entry exists. Entries older than
  `retention_hours` are deleted at the start of a managed turn (at most once a
  minute). `agent.context_stash.purge()` deletes all of them.

## Identity, approvals and the breaker

- SOUL and identity live in the system prompt, which is rebuilt every turn and
  is never part of the summarized history. `SoulCompactionGuard` sees a summary
  as a history reduction and re-injects its protected runtime data, as with any
  other reduction (docs/soul-compaction.md).
- Pending tool calls at the end of the history stay verbatim (they are among the
  most recent messages, and pairs are never split), so the pending-call check
  before each turn still finds them. HITL approvals and the operation ledger are
  outside the conversation and are not touched.
- A call paused on an approval is resumed from its checkpoint after a restart
  (docs/hitl-approvals.md) with context management on as well: the approved call
  runs once, and when its result takes the restored history past the threshold,
  the summary is made before the next model call and keeps that call and its
  result verbatim (`tests/test_context_management_gaps.py`).
- The breaker, HITL and write-path checks apply to tool calls as before; the
  read-back tool only reads this agent's stash.

## Visibility

- `agent.last_context_management`: `mode` (`off` / `on` / `refused` /
  `unsupported` for stateful models), and when on `offloaded_results`,
  `summaries`, `summarized_messages` (this turn), `history_messages`,
  `stash_entries`.
- `agent.context_refusal_reason` (#746): while the mode is `refused`, one of
  `no_stash_dir`, `agent_id_not_plain`, `stash_outside_write_roots`,
  `stash_unusable`; otherwise `None`. The warning in the server log carries the
  same code and a fixed hint, never a path. `stiracore.agent.context_management.status_view(agent)`
  returns `enabled`, `mode` and `reason` with allowlisted values only.
- `/api/gateway/status` → `context_management` (`enabled`, last `mode`, and the
  refusal `reason` code while `refused`; `mode` is `null` before the first turn).
- `turn.completed` → `context` (#746), only while the feature is not off: `mode`,
  `reason` (a code, while `refused`), `summaries` and `offloaded_results` (in the
  history after the turn), `summarized_messages` (replaced in this turn),
  `history_messages`, and `max_input_tokens` (the largest input of one model call
  of the turn). A mode, a code and numbers only.
- The chat page (#746): after a turn that ended `refused`, a line below the context
  gauge says that context management is on but not working, with the reason code put
  in words and the fix (a "⚠" sign and a sentence, in Japanese and English). It reads
  `/api/gateway/status` → `context_management`; a reason outside the four codes reads
  "unknown". It goes away after a turn that is not refused.
- `/api/gateway/status` → `context_window` (#350, read-only): `tokens`, `source`
  (`setting` / `model` / `capability` / `default`) and `compression_threshold`, from
  `resolve_context_window_source` — the same resolution the summary uses, also while the
  feature is off (then nothing is summarized; the gauge says "auto-summary off").
  Known gap: a window taken from a local server (`server`, see "Window from a local
  server") is used by the summary but this field still reads 200,000 / `default`.
- Each chat turn reports `context_tokens`: the prompt of its **last** model call (Strands'
  per-cycle usage, normalized per provider), next to `input_tokens`, which sums every call of
  the turn (a turn with tool calls reads higher). The context gauge in the chat page and the
  Stage uses `context_tokens ÷ tokens` (docs/stage.md, docs/stage-main-ui.md §12).
- The turn inspector shows the tool result the model received (the preview with
  references).

## Prompt cache (#254)

Offloading happens before a result enters the history, so the history still
extends the previous turn's and the cached prefix stays valid; the tool
definitions (with the read-back tool) and the static system prompt are the same
every turn. A summary rewrites the history once. Known, needs a decision: after
the first summary `SoulCompactionGuard` appends its live runtime block to the
system prompt on every later turn, which rewrites the system cache segment each
turn from then on (ADR 0005, open points).

## Window from a local server (#746)

When no window is set, recorded on the model or in the capability table, and the
provider is `openai` / `openai_responses` with a local `base_url` (loopback or
RFC 1918, decided from the URL text as for the output cap, #745), the agent asks
the server once: `GET <base_url>/models`, and uses `meta.n_ctx` of the configured
model (or of the only model listed). llama.cpp-class servers report it. The
source is then `server`; without it the window stays 200,000 (`default`).

- Asked only in a turn with context management on. No API key is sent, no proxy
  is used, redirects are not followed, the wait is 2 seconds and the answer
  (also "nothing reported") is kept for 5 minutes. A server that needs a key for
  `/models`, or reports no `n_ctx`, keeps the default: set `context_window_tokens`.
- Only a positive integer is taken from the answer; nothing of it is logged.
- Hosted endpoints, and `ollama` / `llamacpp` providers, are not asked.

## Long unattended work (#746)

Measured in a 24-hour trial on a local model: inside one turn the input grew from
about 6.6 k tokens (first call) to 55–130 k over 30–90 tool-loop calls, against
a 131 k window, and nothing was summarized because the feature was `refused`
(the workspace had been moved; that case is `on` now).

- Turn context management on explicitly; it is off by default. After the first
  turn, check that `/api/gateway/status` → `context_management.mode` is `on`.
  `refused` means history is managed as before: look for the one
  `context_management is enabled but refused (<code>)` line in the server log.
- Set `context_window_tokens` to the window the server really has (a local
  server's `n_ctx`). Unset, an unknown model falls back to 200,000, which puts
  the threshold above a 131 k window — unless the local server reports its
  window (previous section).
- Start a fresh chat session per task and keep the state that must survive in a
  notes file in the workspace. A summary keeps identifiers and open items, not
  every detail.
- Inside a tool loop the summary is triggered by the input the provider reported
  for the previous call, so a loop that grows past the threshold is summarized
  before its next call even when the local estimate is low.
- Three numbers to watch: the input of a turn's last model call, usually its
  largest (`context_tokens`; every call is a `model.completed` event in
  `turns.db`; the largest of a turn is `turn.completed.context.max_input_tokens`),
  the summaries per turn (`last_context_management.summaries`, also in
  `turn.completed.context`), and the mode.

## Limits

- References are keyed by the provider's tool-use id. A provider that reuses ids
  within a conversation would overwrite an earlier entry (Bedrock ids are unique).
- Token counts are the SDK estimate (tiktoken if installed, else characters / 4);
  the summary trigger uses the provider's reported usage of the previous call.
- Only the agent's own turns are managed; delegated sub-agents keep the SDK
  default.
- A summary costs one extra model call. Read-backs cost calls too; see the
  measurement for the trade-off.
- Stash keys: a reference with a `..` segment or a backslash is refused, and an
  absolute key is taken relative to the stash, so neither names a file outside
  it. An agent directory (`context-stash/<agent_id>`) that is a symbolic link to
  somewhere else is refused (`stash_outside_write_roots`). Retention and
  `purge()` skip symbolic links and never delete through one.
- Known gap: a symbolic link *inside* the stash directory is followed. The
  read-back tool returns the file it points to, and a write below a symlinked
  subdirectory lands at its target. Creating such a link needs write access to
  the stash directory itself; on the chat server the agent's file tools are
  refused there. Pinned as strict xfails in `tests/test_context_management_gaps.py`.
- Known gap: the preview and the read-back text are returned as stored. Text in
  them that imitates `<system-reminder>` / `<recalled-memory>` is not defanged
  (tool results are not rewritten, [prompt-cache.md](prompt-cache.md)). It
  reaches the model only inside the tool result; with prompt caching on, the
  one genuine block is still the last block of the request, which is what the
  static note tells the model to trust. The read-back case is a strict xfail in
  the same file.
