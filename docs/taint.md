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

* **`agent_address`** (a signal, not a gate): the results of `external` tools in the context
  are read for text that *talks to the agent* (`stiracore/middleware/agent_address.py`, rules in
  Japanese and English, no model call). Per turn the row carries `scanned` (external results
  with text that were read), `flagged` (results with at least one code) and `codes`
  (`agent_heading`, `override`, `authority_tag`, `no_confirmation`, `exfil_request`,
  `hidden_carrier`, `obfuscated`; counts of results). Only external results are read (not the
  workspace, memory, program or child results, whose text is not a stranger's by origin).
  Limits per turn: the newest 200 results, 500,000 characters (an oversize result is skipped, not a
  stop), and **only the first 50,000 characters of one result**. A weak sign (a role name,
  `curl | sh`) counts only next to a strong one; text that *discusses* injection (two words of the
  topic close together, no heading aimed at the agent) is not flagged.
  **It can be switched off on purpose:** adding two words of the topic anywhere in the result
  (within 400 characters of each other; a footer will do) or an example-quotation frame next
  to a plain request, writing it in another style or language, padding the first 50,000 characters,
  or hiding the payload behind more than five base64-looking runs gives no code. A hidden
  carrier, a spacing / width / zero-width disguise and a decoded base64 hit are not suppressed by
  the discussion filter, and a reading that runs out of budget (more hidden places than it reads)
  counts as a hidden carrier instead of staying silent. It also does not say whether the author can be trusted (the label says
  that), and it misses a request written as plain prose with no marker. Measured: on 114
  hand-written cases (the regression corpus, which the rules were tuned on) 58 of 60 addressed and
  0 of 54 ordinary texts were flagged; on 5,373 machine-written, machine-checked samples (not tuned
  on) about 51% of the addressed and 6.5% of the ordinary ones. Nothing is held and no approval
  card changes; a later phase can show it to the person.

## Where to read it

* One `taint.observed` event per turn: codes and counts only (no text, argument, URL or host), including the `agent_address` counts.
* `turn.summary` gains `taint` (same fields).
* `GET /api/taint` (read only): the switch, the vocabulary, the last turn and totals.
* Weekly summary: `python -m stiracore.control.taint_cli --data-dir DIR summary [--days 7]` (adds `agent_address_codes` and the `address_*` totals).

## Known limits of phase 0

* Bot and webhook turns are both counted as `external` (either way untrusted).
* Compaction summaries and stash read-backs do not inherit labels yet (phase 1).
