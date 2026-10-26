# Tool definitions sent to the model (#368)

Every model call carries the full definition (name, description, JSON schema) of every
tool the agent holds. With prompt caching off (the default, [prompt-cache.md](prompt-cache.md))
they are billed in full on every call; a turn that uses tools pays them once per model
call. This page records what is sent, what #368 changed, and how it was measured.

## What is sent (checked in the code, measured)

The tool list of a chat server turn is built in this order (`dashboard/stira_server.py`
`runner` → `_stira_tools` → `tool_registry.get_tools` → `filter_tools_by_config` →
`NXPAgent._turn_tools`):

| Step | Removes | When it changes |
|---|---|---|
| `get_tools` | exec class unless `STIRA_ENABLE_EXEC_TOOLS` (#163); a file tool whose consent bridge does not hold (#242); `memory` without a knowledge base; `perplexity_search` without a key; `page_check` without a browser (ADR 0015) or without the workspace write policy | at start-up (the bridge is re-checked per call, fail-closed) |
| `filter_tools_by_config` | every tool whose saved rule is **Never** (OFF); everything while the rules are locked | when a rule is saved (ADR 0010) |
| breaker filter | write/exec tools at WARNING, CRITICAL read-only | on a breaker transition (#259, unchanged) |

So **the chat server already does not send Never or hidden exec tools**, before and after #368.
Measured on the real server: in the fresh-install rules the external-send tools
(`http_request`, `webhook_send`, `oauth_api_call`) are Never and the exec class is
hidden — none of their definitions is in the request (`tests/test_tool_spec_trim.py`,
the real request through `tests/fake_bedrock_endpoint.py`). Ask keeps the tool listed
(a card asks every call); Always and Ask produce the same list (ADR 0010, unchanged).

Since #508 the system prompt names the tools left out here (Never, hidden exec,
`page_check` / `run_checks` / `memory_rate` not available) in a short Tool Availability
section — name, one line, reason code, who can enable it; never their schema (ADR 0010,
Addendum #508: +1,512 JSON characters of system prompt in `default`, +613 in `exec`).

Tools that are listed but **cannot complete** a call (as measured for #368):

* Before #397 (#362), `shell` and `cron` (every action but `list`) asked for consent on
  the terminal, so a call approved in Stira was cancelled upstream. #397 routes the
  exec class through Stira's gate and consent bridge: an approved call runs once. The
  shorter descriptions sit innermost, inside that gate (tested: an approved compact
  `shell` runs, a rejected or ungated one does not, the terminal is never read).
* `http_request` with POST/PUT/PATCH/DELETE asked on the terminal upstream, so a request
  approved in Stira came back "cancelled by the user". Since #547 the module is in the
  consent bridge: such a call runs once, only after a person approved that exact call
  (Ask rule, approval card); with Always (or a YOLO approval) it is refused with the
  reason, and `BYPASS_TOOL_CONSENT` is ignored. GET asks nothing and is unchanged.
  External sends are Never in the fresh-install rules.
* `handoff_to_user` with `breakout_of_loop: false` waits for input on the server's
  stdin, which a background server does not have (`docs/install.md` already says it
  needs an interactive input channel). Not changed here.
* `handoff_to_user` with `breakout_of_loop: true` is unchanged (it ends the turn) unless
  `progress.declared` is on (ADR 0014 phase 3, off by default): then, in a chat / Stage
  turn of a YOLO window with reports on, the call is the agent's milestone declaration —
  cancelled before its rule, card and the YOLO approver, its message ends the turn as the
  report's note.

### Delegated children

A child built by `create_nxp_subagent` has no approver, so an Ask tool is refused at
every call, like a Never one. Since #368 the child is built with only the tools whose
parent rule is **Always** (an unreadable rule counts as Never; a tool whose name
cannot be resolved is not given either, fail-closed); a child without rules keeps
every tool. Decided once, when the child is built: a rule saved later does not
rebuild it, and the child's own breaker filter runs each turn as before. The call-time
gate is unchanged: a call to a tool the child was not given never runs, and a kept
tool whose rule is tightened later (Ask, Never, unreadable) is refused at the call.
A breaker transition never brings an excluded tool back (tested through NORMAL →
WARNING → CRITICAL → recovery).

## Shorter descriptions (`tools/compact_specs.py`)

A few upstream `strands_tools` descriptions are long docstrings (internals, history,
several worked examples). Stira sends a short summary instead, for `current_time`,
`shell`, `cron`, `think`, `use_agent`, `swarm` and `graph`:

* only the top-level `description` changes; the name, the `inputSchema` (the upstream
  object itself, property descriptions included), the tool type, the execution and the
  position in the list are the upstream tool's (`CompactSpecTool` delegates everything
  else; a test compares every other field byte for byte against the uncompacted list);
* the OS-protection guard (#244) and the other wrappers stay outside it;
* inner agents (`use_agent`, `swarm`, `graph`, `think`) take the parent's registry
  entries, so they get the same short descriptions;
* `cron`'s upstream text told the model to run jobs with `BYPASS_TOOL_CONSENT=true`;
  the short one does not.

Only `current_time` is in the default tool list; the other six are exec class, so the
saving is large only when exec tools are exposed.

### Shorter property descriptions (opt-in, `tools_short_schemas`)

With `"tools_short_schemas": true` in `gateway-config.json` (or `context_profile`
`small` / `tiny`), the property descriptions inside the long upstream schemas of
`http_request`, `think`, `use_agent`, `swarm`, `graph`, `shell` and `cron` are sent
shorter (`SHORT_PROPERTY_DESCRIPTIONS` in `tools/compact_specs.py`). Off (the default)
nothing changes: the schema is the upstream object itself.

**Policy for the shortened text.** Replace a description only, never add or remove
one, and never change a property's name, type, enum, default, `required` or nesting
(the test compares the schema with every description removed, byte for byte). Keep
what the model cannot infer from the name and type: limits and defaults
(`cycle_count` 1-10, the swarm bounds, redirects, timeouts), which action needs which
argument, the shape of nested objects (graph `topology`, swarm `agents`) and the
cautions (`auth_env_var` must be listed in `HTTP_REQUEST_TOKEN_CONFIG`; disabling
`verify_ssl` may be restricted). Drop examples, history, internals and repetition of
the top-level description. The text is written in Stira (upstream sources are not
edited); a listed property that disappears upstream fails the test instead of being
silently ignored. The approval, the gates and the call are unchanged: the wrapper is
the same innermost `CompactSpecTool`, and arguments reach the upstream tool as sent.

Measured with `scripts/tool_payload_probe.py --short-schemas` (2026-10-23, JSON
characters of the first request; tokens estimated with `estimate_tokens`, 4 ASCII
characters or 1 CJK character per token):

| Condition | tools | toolConfig off → on | request off → on | est. tokens off → on |
|---|---:|---:|---:|---:|
| default | 24 | 15,604 → 15,604 | 21,170 → 21,170 | 7,465 → 7,465 (none of the seven is listed) |
| exec | 34 | 32,438 → 29,358 (90.5%) | 37,107 → 34,027 (91.7%) | 11,449 → 10,679 (93.3%) |

Per tool (exec, characters): `http_request` 4,109 → 3,783, `swarm` 2,297 → 1,781,
`think` 2,228 → 1,452, `graph` 2,226 → 1,600, `use_agent` 1,854 → 1,334, `shell`
1,261 → 1,102, `cron` 1,201 → 1,044. What remains of these schemas is structure
(property names, types, nested objects such as `http_request`'s auth settings), which
#368 keeps identical.

## Measurement (2026-10-18)

`scripts/tool_payload_probe.py`: the real entry point on a fresh data directory, its own
`BedrockModel` pointed at the local fake endpoint, one minimal turn
(「起動確認です。一言だけ挨拶して。」), JSON characters of the first ConverseStream
request body. `default` = fresh-install rules, exec hidden; `exec` =
`STIRA_ENABLE_EXEC_TOOLS=1` and every tool ON (a pre-#190 `trust_all` file, migrated:
ON → Always, exec → Ask), the shape of the 29-tool measurement in #368. The server
gets only an allow-list of the caller's environment (no key: `perplexity_search` is
absent, hence 18/28 tools).

| Condition | tools | toolConfig before → after | request before → after | after / before |
|---|---:|---:|---:|---:|
| default | 18 | 10,137 → 9,602 | 14,190 → 13,655 | **96.2%** (toolConfig 94.7%) |
| exec | 28 | 41,774 → 26,436 | 45,829 → 30,489 | **66.5%** (toolConfig 63.3%) |

Per tool (exec, characters): `swarm` 7,457 → 2,297, `use_agent` 5,928 → 1,854, `graph`
5,523 → 2,226, `shell` 2,811 → 1,261, `think` 2,508 → 2,228, `cron` 1,643 → 1,201,
`current_time` 1,048 → 513. Every other definition is unchanged.

Real Bedrock (Sonnet 5.5, `ap-northeast-1`, the `exec` condition with 29 tools — a
search key was set, so `perplexity_search` was listed — one turn each on a fresh data
directory, `prompt_cache` off): **input 19,371 → 13,376 tokens (69.1%)**.
The `default` condition was not run on Bedrock (two-call budget); its ratio above is
by characters.

What is left: in `default` the tool definitions are still ~70% of the request
(9.6k of 13.7k characters; the largest are `editor` 1,394, `speak` 990,
`perplexity_search` 701 when a key is set, `lsp_hover` 611). Further cuts would have to shorten property
descriptions inside `inputSchema` (`http_request` 3,007, `think` 1,840, `graph`
1,748, `swarm` 1,685 characters of schema) — not done: #368 keeps the argument schema
identical. Prompt caching is the larger lever for a fixed prefix
([prompt-cache.md](prompt-cache.md), "The default").

Reproduce: `PYTHONPATH="$WT/src:$WT" python scripts/tool_payload_probe.py` (no cloud
call; about 5 s).
