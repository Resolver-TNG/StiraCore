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
| `context_window_tokens` | unset | window for the utilization; unset = model config, capability row, else 200,000 |
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
- It must be inside the agent's write roots (#243), not on the identity floor,
  and the agent id must be a plain name (letters, digits, `_ . -`); otherwise the feature is refused (one warning, nothing offloaded).
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
- The breaker, HITL and write-path checks apply to tool calls as before; the
  read-back tool only reads this agent's stash.

## Visibility

- `agent.last_context_management`: `mode` (`off` / `on` / `refused` /
  `unsupported` for stateful models), and when on `offloaded_results`,
  `summaries`, `summarized_messages` (this turn), `history_messages`,
  `stash_entries`.
- `/api/gateway/status` → `context_management` (`enabled`, last `mode`).
- `/api/gateway/status` → `context_window` (#350, read-only): `tokens`, `source`
  (`setting` / `model` / `capability` / `default`) and `compression_threshold`, from
  `resolve_context_window_source` — the same resolution the summary uses, also while the
  feature is off (then nothing is summarized; the gauge says "auto-summary off").
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

## Limits

- References are keyed by the provider's tool-use id. A provider that reuses ids
  within a conversation would overwrite an earlier entry (Bedrock ids are unique).
- Token counts are the SDK estimate (tiktoken if installed, else characters / 4);
  the summary trigger uses the provider's reported usage of the previous call.
- Only the agent's own turns are managed; delegated sub-agents keep the SDK
  default.
- A summary costs one extra model call. Read-backs cost calls too; see the
  measurement for the trade-off.
