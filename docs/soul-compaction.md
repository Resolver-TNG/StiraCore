# SOUL reinjection after context compression (method B)

## NXP-owned lifecycle

`SoulCompactionGuard` belongs to each `NXPAgent`; it does not depend on the closed
PR #101 plugin entrypoint. ContextAssembler, NeuralState, IdentityGuard, Amygdala,
and breaker policy are unchanged. Conversation reduction remains SDK-owned.

1. NXP prepares its system prompt through the existing `system_prompt_provider`
   (normally `ContextAssembler.build_system_prompt`) or static `system_prompt`.
2. Immediately before the SDK invocation, `snapshot(SoulAgentState(...))` captures
   that trusted prompt and immutable JSON for NeuralState, L3+ identity anchors,
   protected layer numbers, breaker level/actions, and emotion transcription.
   It records message count and estimated tokens of the serialized history
   ([`estimate_tokens`](token-estimate.md)).
3. `_finish_turn` calls the existing engine `post_turn`, then
   `check_and_reinject(agent_state, messages)`. A decrease in either count or
   token estimate, or loss of the trusted prompt, triggers reinjection into the
   SDK system prompt. Runtime JSON is refreshed from the **post-turn** engine.
4. The same path runs on errors/cancellation, before session persistence. A HALT
   before prompt preparation does not reuse a previous turn's pending snapshot.
5. After the first reinjection, each new turn refreshes protection before SDK
   execution, including NXP's rebuilt delegates. GuardedDelegate streaming uses
   the same snapshot/finish path, with a separate guard for every child.

The guard protects the **whole prepared prompt** instead of parsing/reassembling
SOUL sections. It never infers identity from a conversation summary. The original
prompt is restored verbatim when missing; additional application text and
structured cache points are retained. Its own runtime block is replaced rather
than accumulated. Snapshot serialization is detached from live mutable values.

## Boundaries

- This is **turn-boundary protection**, not a before-every-model-call guarantee.
  A reduction hidden by later history growth in the same invocation may evade
  detection. Same-count reductions are detected when the token estimate shrinks.
  Equal-size rewrites are not classified as compression. Manual pruning also
  counts as reduction; this is intentionally conservative.
- It does not bracket SDK summarization/overflow retries with plugin hooks.
  Restoration after a mid-invocation loss is assured only at the end of that
  invocation, not before an internal retry/tool-loop inference.
- The guard does not change the SDK's default conversation manager or enable
  experimental auto summarization. It works with SDK-owned reduction policies.
  Opt-in context management (#258, docs/context-management.md) is such a
  policy: its summary is a reduction the guard detects like any other.
- ContextAssembler's existing token budget remains authoritative. The guard
  preserves its supplied prompt without further truncation; it cannot recover
  SOUL text the assembler already omitted. Configure a sufficient prompt budget.
- A restored prompt still consumes context tokens. An identity prompt larger
  than a model's window cannot be made to fit by compressing conversation alone.
- Runtime values are reference data, not a replacement policy authority. The
  guard never rewinds NeuralState, advances turn counters, changes anchors, or
  resets breaker cooldown. Concurrent calls/external state writers require the
  same synchronization as NXPAgent itself.

## Dependency separation

The default install no longer requires `strands-harness`. SDK minimum remains
`strands-agents>=1.57.1` from PR #102. That PR's optional official `make_subagent`
worker adapter still uses Harness, so it is lazy-loaded by `register_spawn_tool`
and available via `pip install 'stiracore[harness]'`. Agent.as_tool, Swarm,
session restore, and SOUL protection do not require that extra. The obsolete
`stiracore.harness` package and plugin-only tests/docs have been removed.

## Verification

Offline tests exercise real NXPAgent/SDK invocation and SDK sliding-window
reduction, a late reduction that deliberately removes system context, repeated
restoration, same-count shrinkage, unchanged/growing history, structured cache
points, post-turn emotions, CRITICAL policy, failure/HALT handling, streaming
children, and ContextAssembler-supplied SOUL. No cloud model calls are needed.
