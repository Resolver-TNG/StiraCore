# Small-window profile (#368)

[日本語](small-context_ja.md)

For local models with a small context window (4k–32k), `context_profile` sends less
memory and fewer tool definitions. Off by default: with `"default"` the system prompt and
the tool list are byte-identical to before.

## Setting it

`gateway-config.json`: `"context_profile": "small"` (or `"tiny"`), or the variable
`STIRA_CONTEXT_PROFILE=small` (an unknown name is ignored with a warning). Read at
start-up. The settings page shows the current profile read-only under "Output limit and
cost" ([configuration.md](configuration.md)).

## What a profile decides

One table (`stiracore.agent.context_profile.PROFILES`). A profile only sets the
**default** of the existing `ContextAssembler` parameters; a value the caller passes
explicitly wins. The numbers are provisional until measured on real small windows.

| Profile | System prompt budget (tokens) | Recent episodes | Procedural rules | Only usable tool definitions | Short tool schemas |
|---|---|---|---|---|---|
| `default` | unchanged (the chat server: 4000) | unchanged (5) | unchanged (5) | no (unless `tools_send_allowed_only`) | no (unless `tools_short_schemas`) |
| `small` | 3000 | 2 | 3 | yes | yes |
| `tiny` | 1800 | 1 | 2 | yes | yes |

Short tool schemas: shorter property descriptions in the long upstream schemas
(`http_request`, `think`, `use_agent`, `swarm`, `graph`, `shell`, `cron`); names, types
and `required` unchanged. With the fresh-install rules none of them is listed, so the
measurement below does not move ([tool-definitions.md](tool-definitions.md)).

The memory map and hand-off switches are not part of this table yet (reserved columns).

## Only usable tool definitions

With `small` / `tiny`, or `"tools_send_allowed_only": true` on its own, each turn sends
only the tools whose live rule is **Always** or **Ask**. A tool whose rule is **Never**,
whose rule cannot be read, or whose name cannot be resolved is left out. The rules are
read at the start of every turn, so a saved rule applies from the next turn.

* Removal only: a kept tool is the same object, with the same name, description and
  argument schema (tested).
* The gate is unchanged: a call is checked exactly as before, and a call to a tool that
  was not listed never runs.
* The chat server already leaves out Never tools (`filter_tools_by_config`, #227), so in the
  server the switch changes nothing with the usual rules; it matters for hosts that use
  `NXPAgent` with a rule reader but without that filter.

## Measured (offline, no model call)

A minimal turn ("起動確認です。一言だけ挨拶して。") through a fake model that records the
request. Japanese SOUL (6 lines) and 6 Japanese episodes; no Tool Availability section.
Tokens are estimated (no tokenizer installed): tool JSON characters ÷ 4 (ASCII), system
characters ÷ 1.5 (mostly Japanese). Real token counts depend on the model's tokenizer. The system size moves by a few characters between runs (the Neural State snapshot).

| Scenario | Profile | Tools | Tool JSON chars | System chars | Est. tokens |
|---|---|---|---|---|---|
| the chat server, fresh-install rules | default | 15 | 8,409 | 1,567 | 3,147 |
| the chat server, fresh-install rules | small | 15 | 8,409 | 1,000 | 2,769 |
| the chat server, fresh-install rules | tiny | 15 | 8,409 | 811 | 2,643 |
| Host without the server's rule filter, exec listed | default | 25 | 25,103 | 1,567 | 7,320 |
| Host without the server's rule filter, exec listed | small | 15 | 8,409 | 1,000 | 2,769 |
| Host without the server's rule filter, exec listed | tiny | 15 | 8,409 | 809 | 2,642 |

With the usual rules, the tool definitions (about 2,100 estimated tokens) are the larger
fixed part; the profile shrinks the memory part of the system prompt.
