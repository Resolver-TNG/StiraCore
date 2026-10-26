# Allow again from the approval card (ADR 0025)

[日本語](allow-again_ja.md) · Design: [ADR 0025](adr/0025-allow-again-from-the-approval-card.md) ·
Approvals: [hitl-approvals.md](hitl-approvals.md)

When a tool's rule is **Ask**, every call shows an approval card. If the agent then edits five
files in one project folder, you press "Approve once" five times. The card offers two more
buttons for that situation. Both are shown by default and need no setting:

| Button | What it does | How long |
|---|---|---|
| **Approve, and allow the same for this session** | Approves this call. Later calls of **the same tool on the same target** are approved automatically | until the conversation ends, **at most 2 hours** |
| **Approve, and set this tool to Always** | Approves this call. Once it has run, a dialog lets you save the tool as **Always** through the settings hub (type the tool name to confirm; the change goes to the history and can be undone) | permanent, until you change it back |

## What "the same" means

The card names the target under the button, e.g. `file_write · under projects/game/ · up to 2 h`:

- `file_write`, `editor` (writes): the **folder of the approved file and everything below it**,
  never more than its write root. A file at the top of the workspace says *the whole
  workspace*.
- `web_fetch`, `rss`: the **same host**. `http_request`: the same host, **GET / HEAD only**.
- `memory_write`: the same layer (episodic). Memory reads, `journal`, `current_time`, the searches:
  the tool itself.

A call of another tool or into another folder or host still shows a card. A file is checked
again when it is opened, so a folder that is swapped for a link to another place in between is
refused.

## When it is not offered

Only "Approve once" and "Reject" are shown, with a line saying why ("This kind of call is
confirmed every time (…)"). That happens for exec tools (`shell`, `batch`, …), MCP and
unclassified tools, path **reads** (`file_read`, `editor view`, …), calls that send data
(`webhook_send`, a POST), arguments that look like a secret, targets outside the workspace,
an edited call, a card restored after a restart, and while the breaker is not NORMAL.
Never rules, protected locations, the breaker's write block and CRITICAL are decided before an
allowance is looked at, so an allowance never gets around them.

## Seeing and ending allowances

The **"N allowances"** badge appears next to the YOLO badge, in the chat header and at the edge of
the Stage dialogue box. It opens a list showing each allowance's tool, target, time left and uses,
with a **Revoke** button on every row and **Revoke all** at the end. Each automatic approval adds
a line to the chat log: `↻ Approved by allowance: file_write · under projects/game/`. Allowances
end by themselves on any of these:

- a stop (the chat ■, the YOLO stop, "Revoke all");
- `/new`;
- the breaker leaving NORMAL;
- any save of tool rules or roots;
- 2 hours;
- a server restart (they are kept in memory only).

Every press, every automatic approval and every end is written to the audit log (`audit.allow.*`,
`audit.tool.*`; directories and hosts as keyed hashes only). If the audit log cannot be written,
nothing is approved automatically and the buttons stay hidden until a restart.

## Allow again vs YOLO

| | Allow again | YOLO ([yolo-quickstart.md](yolo-quickstart.md)) |
|---|---|---|
| Turned on by | a press on one card (always available) | the "Start YOLO" button and its confirmation (shown by default; `STIRA_YOLO_ALLOW=off` hides it) |
| Covers | **one tool on one target** (a folder, a host) | every Ask tool of a tier |
| Lasts | the conversation, at most **2 hours** | **6 hours** (paused by the breaker) |
| Exec tools | never | only with "Include exec" |

When both are active, YOLO answers first. An allowance never opens, extends or counts against a YOLO
window.
