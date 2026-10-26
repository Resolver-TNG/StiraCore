# Trust labels and taint — phase 0 (observe only)

ADR 0022 ([adr/0022-trust-labels-and-taint.md](adr/0022-trust-labels-and-taint.md)), #458.
Phase 0 **only observes**: no prompt, tool result, gate decision or cached prompt byte changes.

## Switch

* `agent-config.json`: `"taint": {"phase": 0}` turns it on; absent or `{}` = off.
* `STIRA_TAINT_OBSERVE=0` (`false` / `no` / `off`) forces it off. No value turns it on.
* Read at start-up.

## What is computed

* **Source** of each input, from fixed tables in `stiracore/labels.py` (the tool name,
  the turn's origin, a recalled-memory block in the prompt). Nothing in a text sets a label.
  A tool missing from the table is `external`.
* **Context label** per turn: the union of the conversation — `trusted` only if every source
  is `system` / `operator` / `model`; `sensitive` when a tool result holds a value
  `mask_secrets` would change (S1) or a credentialed call returned (S2).
* **`untrusted_recall_only`** (ADR 0022 Q3): the context was untrusted only because of recalled
  memory (memory has no trust record yet in phase 0).
* **`flow_would_hold`**: for each egress call (external tools, MCP, unclassified), the code
  the flow layer of ADR 0022 §6 would have given: `secret_in_args`,
  `credential_by_reference`, `sensitive_context`, `untrusted_send`, `long_payload`. No
  destination list exists yet, so none is listed. Nothing is held.

## Where to read it

* One `taint.observed` event per turn: codes and counts only (no text, argument, URL or host).
* `turn.summary` gains `taint` (same fields).
* `GET /api/taint` (read only): the switch, the vocabulary, the last turn and totals.
* Weekly summary: `python -m stiracore.control.taint_cli --data-dir DIR summary [--days 7]`.

## Known limits of phase 0

* Bot and webhook turns are both counted as `external` (either way untrusted).
* Compaction summaries and stash read-backs do not inherit labels yet (phase 1).
