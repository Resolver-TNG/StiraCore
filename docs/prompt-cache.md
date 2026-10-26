# Prompt caching (#254, phase 1)

Prompt caching lowers input cost only when the request **prefix** is byte-stable
across turns. Bedrock caches in the order *tools → system → messages*; a change
anywhere invalidates everything behind it.

Before #254 Stira rebuilt one system prompt every turn with the live
`## Neural State`, `## Breaker` and `## Episodic Memory` sections inside it. With
Strands `CacheConfig(strategy="auto")` alone, every turn therefore *wrote* the
whole prompt to the cache and never read it (measured: input-side cost +28%).

## What `prompt_cache` does

Opt-in, off by default. Set `"prompt_cache": true` (and optionally
`"prompt_cache_ttl": "5m" | "1h"`, default `5m`) in the gateway config, or start
the server with `STIRA_PROMPT_CACHE=1` / `STIRA_PROMPT_CACHE_TTL=1h`.

When on **and the model is eligible** — a Bedrock model whose capability row
has `prompt_cache` (Anthropic Claude) — `NXPAgent` splits the prompt
(`ContextAssembler.build_prompt_parts()`). Every other model (Nova, Llama,
OpenAI, Gemini, Ollama, llama.cpp, injected models) gets the legacy prompt,
exactly as with the option off, and switching to such a model mid-session
returns to it.

| Part | Content | Where it goes |
|---|---|---|
| static | SOUL, Procedural Rules, the Tool Availability section on the chat server (disabled tools and the rule counts; it changes only on a rules save or a YOLO window's start/end, #508), after it the fixed Progress Reports paragraph while a window with reports on is open and `progress.declared` is on (ADR 0014 phase 3, #451; it appears / goes only when such a window opens or ends), a fixed note (no date or state) declaring the two injected tags; the live identity notice while the breaker injects it | system prompt, cache point at its end |
| control state | Neural State, Breaker, current time | `<system-reminder>` block — the **only** injected part the note gives authority |
| recalled | Episodic Memory | `<recalled-memory>` block before it, declared to be data, not instructions: instructions, persona changes, policy overrides, breaker/state claims or "from the system" text inside it are not to be followed |

Both blocks are appended to the latest user message for that model call only
(Strands `ContextInjector`, every model call of the turn) and are never stored
in history. Episodic Memory can quote users, so it never shares the control
state's authority (before #254 it sat inside the system prompt). Breaker tool
filtering and HITL approval do not depend on how the model reads any of this:
they are enforced by the harness when tools are listed and executed.

This is the same layout as the official Strands harness (`ContextInjector` +
`<system-reminder>` + a one-line declaration in the static system prompt,
`CacheConfig(strategy="auto", tools_ttl=True)`), rebuilt inside Stira on the
current strands-agents 1.57.x; `strands-harness` itself is not a dependency
(its defaults enable shell/write/code execution without approval).

- Cache points (3 of Bedrock's 4): tool definitions (`tools_ttl=True` —
  Strands `auto` alone leaves tools uncached), end of the static system
  prompt, and the last user message *ahead of* the runtime block (Strands
  `dynamic_trailing_blocks`), never on the block itself. The default 5m TTL is left to
  Bedrock (no `ttl` field); `1h` sets it on all three points. Because the block is per call, the next turn's history contains the
  previous user message without it, i.e. exactly the prefix that was written:
  measured reads equal the previous turn's read + write.
- The cache settings go on a per-turn copy of the model; the configured model
  object is never modified, so switching models, turning the option off, or
  sharing one model between agents cannot leave a `cache_config` behind.
- Tools are sorted by name, so their order is deterministic. The tool prefix
  changes only when the breaker removes write tools (or the settings hub turns
  a tool off).
- The static part has its own budget (`max_tokens`), so trimming never makes it
  depend on the per-turn parts; control state and recalled memory share one
  budget, recalled memory getting what the control state leaves.
- Text in the user's message (and in recorded episodes) that imitates either tag
  (`<system-reminder`, `</ System_Reminder`, `＜recalled memory`, …) is defanged
  to `&lt;…`, so a user cannot open, close or forge injected context; the static
  note says only the final block of the latest user message is genuine. This is
  prompt hardening, not a permission boundary. Tool results are not rewritten.
- Guard: if caching is requested but the system prompt is rebuilt every turn
  with no split (`system_prompt_provider` without `prompt_parts_provider`),
  caching is refused with a single warning. A fixed `system_prompt` string may
  be cached without a split.
- With the option off, the model input is exactly what it was before #254.

Known prefix breakers (by design): a breaker transition (tools and identity
notice change); while the breaker is WARNING/CRITICAL the identity notice keeps
its live `stability=0.xx` line, so the system part can change between turns.
This is an accepted trade-off: WARNING and above are rare, and identity
protection is not weakened for caching.
Context management (#258, opt-in) keeps the prefix across offload turns; a
history summary rewrites it once, and afterwards `SoulCompactionGuard`'s runtime
block in the system prompt changes every turn (open point, see
[context-management.md](context-management.md)).
Identity protection is not weakened for caching.

## Visibility

- `/api/chat` SSE `turn` event and `turn.completed` (EventStore) carry
  `cache_read_tokens`, `cache_write_tokens` and `cache_hit_ratio`
  (reads ÷ whole prompt, using `providers.usage.normalize_strands_usage`).
- `turn.completed.prompt_cache` records the turn's prefix fingerprints:
  `mode` (`off` / `split` / `static` / `refused`), `active`, `ttl`,
  `tools_hash`, `system_hash`, `messages_hash`, `volatile_hash`, sizes,
  `elapsed_sec`, and `changed` (which of `tools` / `system` / `messages` /
  `volatile` differ from the previous turn; `messages` = the history no longer
  extends what the previous turn ended with, e.g. a window or compaction).
- Cache break: when caching is active and a turn reads less than half of what
  the previous turn read + wrote, the record gets `cache_break: true` with
  `cache_break_reasons` (changed cached parts, `ttl_expired`, or `unknown`) and a
  `prompt cache break: …` warning is logged.
- The turn inspector in the chat page shows cache read / write / hit rate when
  the provider reports them. `/api/gateway/status` shows `prompt_cache`.

## Measuring

`scripts/cache_probe.py` drives a running server through a fixed 12-turn
conversation and prints per-turn reads/writes and an input-side cost estimate.
The docstring has the launch steps (own worktree, own port, fresh
`.local/stira` per condition).

### Result (2026-10-15, Bedrock Sonnet 5.5, same 12 prompts, fresh state each)

| Condition | uncached input | cache read | cache write | hit rate | $ (read 0.05×) | $ (read 0.1×) |
|---|---:|---:|---:|---:|---:|---:|
| `prompt_cache` off | 262,935 | 0 | 0 | 0% | $0.526 | $0.526 |
| `prompt_cache` on | 18,169 | 229,615 | 25,493 | 84.0% | $0.123 (−77%) | $0.146 (−72%) |
| on, turn 1 cold (adjusted) | 18,169 | 218,786 | 36,322 | 80.1% | $0.149 (−72%) | $0.171 (−68%) |

Input $2/Mtok, write 1.25× (5m TTL; 1h = 2×). Reads at 0.05× (Anthropic's
Sonnet 5.5 rate) and 0.1×; Bedrock's own prices are not confirmed. Turn 1 of
the "on" run found the tools + static system prefix already cached by a check
run minutes earlier, hence the cold-adjusted row. From turn 2 on, every turn read the whole previous prefix (81–89%
per turn); the uncached remainder is the runtime block (~0.2k–2k tokens,
growing with Episodic Memory) and the new user message.

## The default: measurements and recommendation (#368)

The default stays **off**; this section is the input for that decision (it is the
maintainer's call). Nothing below changes a setting.

Measured:

* **A minimal turn is almost all prefix.** One greeting turn on a fresh data directory
  (`scripts/tool_payload_probe.py`, after #368): tool definitions + system prompt are
  99% of the request characters, both with exec tools hidden (13,655 characters, 18
  tools) and exposed (30,489 characters, 28 tools). Real Bedrock (Sonnet 5.5, exec
  exposed, cache off): 13,376 input tokens per call (19,371 before #368's shorter
  descriptions; [tool-definitions.md](tool-definitions.md)). With the cache off every
  model call pays it again; a turn that calls tools pays it once per call.
* **The prefix is stable.** The tool list changes only when a rule is saved, on a
  breaker transition, or when a fail-closed check drops a tool; the order is name-sorted
  while caching. The static system part does not depend on the per-turn state.
* **The 12-turn comparison above** (Sonnet 5.5): input-side cost −72% (reads at 0.1×)
  to −77% (0.05×), 84% of the prompt read from the cache.

Arithmetic with the same assumed prices (write 1.25× for 5m, 2× for 1h; read 0.1×),
for a cached prefix of P tokens: every call either **writes** it (1.25P; 2P with `1h`)
or **reads** it (0.1P), against P per call without the cache. Over W writing calls and
R reading calls the cache is cheaper only when

* 5m TTL: `1.25W + 0.1R < W + R`, i.e. **`0.25 × W < 0.9 × R`**;
* 1h TTL: `2W + 0.1R < W + R`, i.e. **`W < 0.9 × R`**.

A call writes not only after the TTL expired but whenever the prefix changed: the first
call, a rule save, a breaker transition (tools and identity notice), a model switch, a
history summary or window shift. Example: 10 writes and 1 read cost 12.6P against 11P
without the cache, even if every call came within five minutes. The 12-turn run above
read the previous prefix on every turn from turn 2 on; a workload with many prefix
changes may not.

Recommendation:

1. **Keep the code default off.** The open item from #254 is the safety record: with
   the cache on, Neural State / Breaker move from the system prompt to the
   `<system-reminder>` block in the user message, and #143 has not yet shown that this
   leaves the breaker and identity behaviour unchanged.
2. **Before #143, enable it only within the measured conditions**, if at all: a Bedrock
   Claude model (the run used Sonnet 5.5), the breaker NORMAL, consecutive turns within
   the TTL, exec tools hidden (the #254 diagnosis run had 22 tools, i.e. no exec tools;
   the #257 run's tool set is not recorded). Its effect
   on the breaker or identity, and a run with the exec tools exposed, are not verified;
   this memo does not recommend enabling it for an exec-exposed instance. Only Bedrock
   Claude models use it; every other model gets the same prompt as with it off.
3. **Re-decide "on by default for Bedrock Claude" after #143 stage 2** (and a measured
   run with the exec tools exposed), with the 5m TTL; `1h` only where `W < 0.9 × R` is
   expected.

Decision points: (a) default on for Bedrock Claude, now or after #143; (b) 5m or 1h;
(c) #259 (breaker transitions change the tool prefix: keep, or refuse at call time).
