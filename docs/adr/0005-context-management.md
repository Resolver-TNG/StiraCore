# ADR 0005: Context management with public SDK components

**Path:** `docs/adr/0005-context-management.md`
**Status:** Accepted (opt-in, off by default)
**Issue:** #258 (follows #254)
**Decision:** Offload large tool results with the SDK's `ContextOffloader` into a
durable, masked Stira stash, and summarize at a utilization threshold with the
SDK's `SummarizingConversationManager`. Do not use the experimental
`ContextManager` yet; do not depend on `strands-harness`.

## Context

The official Strands harness reduces cost mainly through context management: a
tool result over ~1500 tokens is replaced by a ~750-token preview and the
original is stashed for read-back; at ~85 % utilization the history is
summarized, keeping the 4 most recent messages. All of the machinery is in the
Strands SDK that Stira already uses (strands-agents 1.57.1), so the issue asks to
reproduce it on the current SDK.

Stira constraints that shape the choice:

- `NXPAgent` builds a new Strands `Agent` every turn (tools follow the breaker).
  Anything kept in an Agent-scoped object (in-memory stores, per-Agent cycle
  counters) is lost every turn.
- Writes by the harness follow the write roots (#243); secrets are masked (#185);
  identity/SOUL and pending approvals must not be lost; the prompt-cache prefix
  (#254) must stay stable.
- Default off; thresholds are settings, not constants.

## Options (read in the installed SDK source)

1. **`strands._context_manager.ContextManager`** (`Agent(context_manager="auto")`,
   public alias `strands.experimental.context_manager`). Exactly the official
   pipeline: eager head+tail truncation of tool results (1500/750), an L1 stash
   of *every* message, `retrieve_context` (10k-token cap), summarize all but 4
   at 85 %, emergency truncation, overflow retries.
   - The stash namespace is `context/<agent.session_id>/…`. Without an SDK
     session manager the session id is a random value per `Agent`, i.e. per
     Stira turn; there is no public way to pin it.
   - On every new `Agent` it back-fills the stash from the history it was given.
     In Stira that history already holds the *previews*, so the back-fill
     stores (or, with a pinned session id, overwrites the original with) the
     preview. Verified with a spike on 1.57.1: after an Agent rebuild,
     `retrieve_context` searched the 157-line preview instead of the 2000-line
     original, with and without pinning the session id.
   - It stores every text block (user messages included) at rest, not only the
     large tool results.
   - Making it work needs two private overrides (`agent._session_id`,
     `ContextManager._backfill_stash`) on an API marked experimental, whose
     preset values "may change between releases".
2. **`ContextOffloader` (`strands.vended_plugins.context_offloader`) +
   `SummarizingConversationManager` (`strands.agent.conversation_manager`)** —
   both public. The offloader intercepts a tool result in `AfterToolCallEvent`,
   *before* it enters the history, stores only that result's blocks, and returns
   a preview with references; `retrieve_offloaded_content` reads them back
   (pattern / line range / whole). It accepts any `strands.storage.Storage`. The
   conversation manager summarizes the oldest share of the history when the
   projected input reaches a ratio of the context window (`proactive_compression`),
   never splitting tool-use/tool-result pairs, and also handles overflow.
3. **Own implementation** of truncation, stash and summary. More code to own
   for what the SDK already provides.
4. **`strands-harness` (`create_harness`)**: out of scope (issue); its defaults
   enable shell/write/code execution without approval.

## Decision

Option 2, assembled per turn by `stiracore.agent.context_management`:

- `ContextOffloader(storage=StashStorage(...), max_result_tokens, preview_tokens,
  evict_after_cycles=None)`. `StashStorage` is a `Storage` over
  `LocalFileStorage` at `<context_stash_dir>/<agent_id>/` (mode 0700): the same
  directory every turn and after a restart, so a reference stays valid across
  rebuilt Agents. The SDK's cycle-based eviction is per Agent and therefore
  disabled; retention is time-based instead.
- `SummarizingConversationManager(summary_ratio, preserve_recent_messages,
  proactive_compression={"compression_threshold": …})` with a Stira summarizer
  prompt (keep identifiers, references, open items, pending tool calls and
  approvals; do not restate or reinterpret the persona). The summary call uses
  the configured model without prompt-cache settings (an auxiliary call only
  writes cache entries), and the summary text has the `<system-reminder>` /
  `<recalled-memory>` look-alikes defanged, like user text (#254).
- A small hook caps `retrieve_offloaded_content` results at
  `retrieval_max_tokens` (the SDK returns the whole entry; the official stash
  tool caps at 10k).

Why: public APIs only (one protected method is overridden only to pick the
summary model; if it disappears the SDK default — the turn's model — is used);
only large tool results are written to disk; works with per-turn Agents without
touching SDK internals; the offloader keeps the original result when a write
fails (fail-safe).

## Defaults and their reasoning

| Setting | Default | Reason |
|---|---|---|
| `offload_threshold_tokens` | 1500 | official value; Stira's own tools rarely exceed it (memory reads, short answers), so it hits file reads, search and HTTP results — the target |
| `preview_tokens` | 750 | official value; enough to judge whether to read back |
| `compression_threshold` | 0.85 | official value; summarizing earlier breaks the cache prefix more often (#254) |
| `summary_ratio` | 0.8 | closest to the official "all but the recent ones" (the SDK accepts 0.1–0.8) |
| `preserve_recent` | 4 | official value |
| `context_window_tokens` | model config → capability row → 200k | Sonnet 5.5 is not in the SDK table; 200k (the SDK fallback) is conservative for 1M models: it summarizes earlier, never later |
| `retention_hours` | 24 | a stash entry is only useful while the conversation that references it lives; the chat server's conversation is in memory, so a day covers it, and secrets (even masked) do not stay longer |
| `retrieval_max_tokens` | 10000 | official stash-tool cap |

The official values were not tuned for Stira's conversational load (short tool
results, long dialogue); they are starting points, and the measurement in
`docs/research/context-management-2026-10-15.md` is the evidence so far.

## Differences from the official harness (accepted)

- Preview is the head of the result (offloader), not head+tail.
- Summary replaces the oldest 80 %, not "all but 4"; there is no emergency
  truncation step beyond the conversation manager's overflow handling.
- The read-back tool is `retrieve_offloaded_content` (`retrieve_context` there).
- Media blocks are never written to disk; a result with media is kept whole.

## Safety

- Default off. On, but the stash directory is missing, outside the write roots
  or on the identity floor → refused (warned once), nothing is offloaded.
- Text is secret-masked before it is written; a secret beyond the preview is
  read back masked. Keys are validated by `LocalFileStorage` (no `..`, no
  backslashes), so a model-supplied reference cannot leave the stash.
- Entries older than `retention_hours` are deleted at the start of a managed
  turn (at most once a minute); `StashStorage.purge()` deletes all.
- No tool is added while off: the tool set, its order and the static system
  prompt are byte-identical to before (#254 hashes). On, the retrieval tool is
  appended after the name-sorted tools, the same every turn.

## Open points (need a decision)

- `SoulCompactionGuard` × prompt cache: once a summary has shrunk the history,
  the guard appends its live runtime JSON to the system prompt on every later
  turn (by design since #101), so the system cache segment — and everything
  after it — is rewritten every turn from then on. Moving that block into the
  per-call runtime block (or making it stable) changes where identity
  protection data sits, so it is not done here.
- Making context management (and #254 caching) the default.
- Revisit option 1 when `ContextManager` leaves `experimental` or Stira keeps
  one Agent per session.
