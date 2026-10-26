# ADR 0010: Always / Ask / Never tool rules and the effective gate stack

**Path:** `docs/adr/0010-tool-policy-gate-stack.md`
**Status:** Accepted (open points are marked **Open**; the implementation uses the
fail-closed option for each)
**Issue:** #190. Related: #163 (exec tools hidden by default), #167 (exec epic), #185
(Origin/CSRF), #189 / ADR 0006 (approval cards, durable approvals), #146 (approval
binding), #227 (settings hub), #243 (write roots), #244 (OS protections), #254/#259
(prompt-cache prefix stability).
**Decision:** Each registry tool has one rule, **Always**, **Ask** or **Never**. A call is
decided by three layers, *rule → breaker → floor*, and the strictest decision wins
(`allow < prompt < forbidden`). The settings hub shows every layer, the layers that
decided, isolation as its own field, and the floor read-only.

## Context

Before #190 the settings hub had ON/OFF switches. "OFF" removed a tool, "ON" exposed it,
and the only per-call human approval was the CRITICAL-HALT gate in `CheckpointHooks`.
There was no way to say "allowed, but ask me every time", and the page could not say
*why* a tool was stopped (the switch, the breaker, or a protection no switch can change).

## Decisions

### 1. Meaning of the three values

| Rule | Rule decision | Runtime |
|---|---|---|
| Always | `allow` | Exposed; runs without a per-call approval. Breaker and floor still apply. |
| Ask | `prompt` | Exposed (same tool list, so the cached prefix is unchanged); **every call** goes through the HITL gate (`CheckpointHooks`), bound to agent / tool / argument hash / expiry (#146). No approver connected = rejected. |
| Never | `forbidden` | Removed from the tool list, and refused at the call (`BeforeToolCall`) as defense in depth. |

Ask is enforced in the existing gate (`CheckpointHooks.before_tool`) through an optional
`policy_check` (`NXPAgent(tool_policy=...)`). The chat server reads the saved rule
from `tool-config.json` at **every call**. A reader that fails, or answers anything but
`always`/`ask`/`never`, counts as **Never**.

### 2. Composition order and monotonicity

`tool_policy.tool_stack` computes, per tool:

* **rule** — the saved rule;
* **breaker** — as the engine really enforces it. NORMAL / COHERENCE: `allow`.
  WARNING: `forbidden` for write and exec tools (WARNING requires confirmation and there
  is no confirmation route at that level, so `NXPAgent._turn_tools` hides them —
  stricter than Ask), `allow` for reads. CRITICAL with `critical_action="halt"`
  (stira): `prompt` for every write/exec call whatever the rule (the CRITICAL-HALT
  gate asks a human), `allow` for reads; `"readonly"`: `forbidden` for write/exec,
  `allow` for reads (reads stay available at CRITICAL, ADR 0002). When the engine's
  action is not known (no live engine) or the level is unknown: `forbidden` for every
  tool. **Open (needs a decision):** the issue text says "CRITICAL は全件停止表示";
  showing that would contradict what the engine does (reads run at CRITICAL), so the
  page shows the real behaviour; stopping reads at CRITICAL would change ADR 0002;
* **floor** — `forbidden` when the exec class is not exposed (#163), otherwise `allow`
  with per-argument constraints (SOUL/identity paths read-only, write roots, OS
  protection commands #244). No approval lifts them.

The effective decision is `max` by rank and `decided_by` lists every layer that reached
it. Invariant (contract test): a lower layer's `allow` never overrides a higher layer's
`forbidden`. At runtime each layer is enforced on its own, so the invariant also holds
for approvals:

* an Ask approval answers the **rule** only: at WARNING / CRITICAL read-only a
  write-capable Ask tool is refused without asking (the approval would otherwise carry
  `hitl_approved` past `IdentityGuard`'s write block);
* the floor is checked before asking (`path_check`), so a futile prompt is never shown,
  and `IdentityGuard.path_denial` refuses again at execution whatever was approved.

"WARNING is at least Ask" is read as: every write/exec tool shows at least `prompt`
(it shows `forbidden`). Reads are not limited at WARNING (unchanged behaviour). **Open:**
whether reads should also become Ask at WARNING (a behaviour change; not done).

### 3. Migration of the existing ON/OFF

No file rewrite: `ToolConfig` gains `ask_tools` (default empty) and the rule is derived.

* OFF (not in `enabled_tools` in `custom` mode) → **Never**.
* ON → **Always**, *unless* the tool is exec class (`shell`, `batch`, `cron`,
  `use_agent`, `swarm`, `graph`, `think`) → **Ask**. An existing ON never becomes an
  unconditional automatic execution: exec tools stay hidden unless
  `STIRA_ENABLE_EXEC_TOOLS` is set (#163), and even then every call asks.
* `trust_all` → every tool ON (same as before); exec class → Ask.

**Format 2, generation and head (review #269).** The file carries `schema_version: 2`
and a `generation` that every save increments. The last generation, a digest and the
full rules of that generation are also written to `tool-config.json.head`. Loading:

* **genuinely new install** (no file, no head, no lock marker, and no settings-history
  *file* at all): safe defaults — reads Always, local writes **Ask**, external sends and
  exec tools Never, never "everything Always" — recorded at once (format 2 + head);
* a **pre-#190 file with no head, no lock marker and no history file** (an empty or
  tool-free history counts as evidence): migrated once (ON → Always, OFF → Never, exec
  ON → Ask) and recorded as format 2 + head;
* anything else that does not check out locks the rules — every tool Never, including
  tools the registry does not govern (memory tools): a pre-#190 file after migration, a
  missing field, an older generation, a digest mismatch (hand edit), a missing or
  unreadable head, a deleted file (with a head or a history), an **unreadable history**
  (not "no history"), malformed JSON;
* generation = head + 1 (an interrupted save, or a hand edit that bumped the number) is
  completed only when it **loosens nothing in any field** compared with the full rules in
  the head (`rules_loosening`: `mode` custom < trust_all, each tool's rule, a
  `shell_allowlist` command added; an unknown or uncomparable field counts as looser);
  a looser one, a head without that snapshot, or a failed head repair stays locked.

**The lock latches** (in the process, and through `tool-config.json.locked` on disk; a
marker read after a restart latches in that process too, so deleting only the marker
does not unlock):
putting the original files back does not unlock. Only a human save with the typed
recovery word (`recover: "RECOVER"`; the settings page asks for it) overwrites locked
rules and clears the lock; an empty save (`POST /api/tools/config {}`) or a save
without the word is refused (409 `tool_rules_locked`). A recovery is also a loosening
(from all-Never), so the confirmation, NORMAL breaker, audit and typed tool names apply.

**The agent cannot write these files.** `tool-config.json`, its head and lock marker,
the settings history, `gateway-config.json`, `agent-config.json` and the approval state
(`tool-policy-approvals.json`, the HITL stores, the neural state) are in the write floor
(`read_only_paths`, #243) of the chat server: `file_write`/`editor` refuse them at
the floor, before any approval is asked, and no approval lifts it. **Residual risk:** a
process outside Stira that can delete the file, the head *and* the history and then
place a pre-#190 file makes the next start look like a first migration. That needs
write access to the data directory outside the agent's tools; the floor is what keeps
the agent itself from doing it.

### 4. Changing rules (settings APIs)

All routes are behind #185 (`install_security`: loopback Host, same Origin, CSRF token)
and the config blueprint's own Origin check.

* **One loosening check for every save path** (new and legacy APIs, hub save, history
  rollback, completing an interrupted save): `tool_config.rules_loosening` — every
  field with an order of strictness (`mode` custom < trust_all, each tool's rule, a
  `shell_allowlist` command added; unknown fields count as looser). Saving over locked
  rules (a recovery, even with no value change) is always a loosening: NORMAL breaker,
  confirmation and a successful audit first (the audit records `recovered_lock`).
  On every path (hub save and rollback, the legacy `/api/config/tools` and the
  `/api/tools/config` APIs) the confirmation, the breaker refusal, the audit order and
  the names to type are all derived from **one** decision per save,
  `settings_hub.tool_rule_change` → `rules_loosening(..., locked=...)` +
  `newly_always_writes` (#270: the tool APIs used to compute it twice); a save over
  locked rules needs all three whatever it changes.
* A shell-allowlist command that `mask_secrets` would change (a token in it) is
  **refused** on every path (`secret_command`), never stored even masked. A command
  already in an older file is loaded (a warning with the count, never the value) and is
  not copied into the head snapshot, but it is **never written again** (#270): every
  save that writes the tool rules — an unrelated tightening, a preset, a rollback, a
  RECOVER save, `ToolConfig.save()` itself — is refused (`secret_command_saved`, with
  the count) until it is removed; a save that leaves the tool rules alone (classifier,
  breaker, write roots) still goes through. The removal itself is a tightening and
  saves. The settings hub marks these entries (`secret_allowlist`: index + masked text;
  the page shows the masked text and a "remove" button outside the shell editor, so it
  works while `shell` is off). A pre-#190 file drops them on its one-time migration.
* **Tightening** (Always→Ask→Never) is always possible, also during a breaker.
* Making a **write/external tool Always** additionally needs its internal name typed
  (`confirm_always: [names]`, the dialog asks for them).
* Loosening is **audited before it is committed** (refused with `audit_failed` if the
  history cannot be written), and the breaker is **re-checked right before the file is
  replaced**, under the same config lock; a refusal there leaves an `aborted:<id>`
  history note. The hub saves the tool rules before the other groups, so a refusal
  changes nothing. Tightening is committed first and audited best-effort.
* **Loosening** (Never→Ask/Always, Ask→Always) needs an explicit confirmation — the
  hub's typed confirmation (`acknowledged_dangers: ["tool_policy_loosened"]`), or
  `confirm: true` on `/api/config/tools` and `/api/tools/config` — and is **refused
  (403 `breaker_active`) while the breaker is not NORMAL**, confirmed or not. This holds
  for the hub save, a history rollback and the legacy routes.
* Every save is recorded in the settings history (`source` `manual` / `preset:*` /
  `rollback:*` / `api:tools`). A preset never turns an Ask into Always.
* Tool definitions, their order and the static system prompt do not change (Ask keeps
  the tool exposed; Never removes it as OFF did), so #254's cached prefix is unaffected.

**Lock order.** A save takes `ConfigStore.lock` → the candidate `ToolConfig.lock` →
`precommit` → the breaker provider (`engine.breaker_status()`, which takes no lock; it
reads the turn snapshot). Nothing that holds `engine.state_lock` (pre_turn, post_turn,
heartbeat ticks, `on_breaker`) takes `ConfigStore.lock` or a tool-config lock; the rule
reader takes `ConfigStore.lock` and then the approval store's lock, and the store never
calls back. No path acquires them in reverse (regression test: a save completes while
another thread holds `engine.state_lock`).

### 5. Persistent permissions: scope, expiry, revocation

The only persistent permission is an **Always** rule. Its grant is stated in full on the
page and in the API (`grant`): scope = this tool, any arguments, this server's
`tool-config.json`; expiry = until revoked (no automatic expiry); revoke at
`/settings#sec-tools` (or a history rollback); still gated by breaker and floor.
A grant whose scope cannot be bounded is not created: exec-class tools cannot be Always
(`always_not_allowed`). A HITL approval is never persistent: it is single-use, bound to
the exact call, and expires (#146, ADR 0006).

**Open:** time-limited Always (an expiry date that falls back to Ask). Not implemented;
until then the page states "until revoked" explicitly.

### 6. Generation: a rule changed while a call waits

* The gate binds an approval to the rule **generation** it was asked under (not the
  value): any save in between — including Ask → Never → Ask — voids it. The final
  verifier (`ApprovalBindingVerifier`, the last BeforeToolCall hook) re-checks the
  generation at the execution boundary.
* Durable approvals: the chat server reader records the generation the open approvals were
  asked under (`tool-policy-approvals.json`). When it differs (a save while running, or
  while the server was down), every pending or approved-but-unused approval in the store
  (`ApprovalStore.open_records()`, unbounded — not a page of the history) is revoked first; if one cannot be revoked the reader fails and the gate treats the
  call as Never.
* If the breaker got stricter while the human was deciding (live state vs the turn
  snapshot), the approval is not used.
* A Never rule refuses the call at execution, including a batch resumed after a restart
  (ADR 0006); the resumed tool list is also filtered by the saved rules.
* A breaker transition that rebuilds the tool list mid-turn applies the saved rules too
  (previously it re-exposed tools switched OFF until the next turn).
* A durable approval restored after a restart is short-lived (`hitl_timeout_sec`) and is
  checked against the live rule when the resumed call runs.

### 7. Isolation is a separate field

`isolation` is reported apart from the rule: `path_confined` for `file_write`/`editor`
(Stira's write-path policy on the host — not an OS sandbox) and `none` for everything
else. The page never says or implies "approved = isolated" (#167 Phase 3 owns OS
sandboxing).

### 8. Where the logic lives

All decisions are computed on the server (`stiracore.tools.tool_policy`); the page
only mirrors the saved rule to mark unsaved drafts, and the server re-checks everything
(#167: no second policy engine in the browser).

## Consequences

* Turning a tool ON through the hub or the API now asks for a confirmation and is not
  possible during a breaker.
* `/api/config/tools`, `/api/tools/list` and `/api/config/hub` rows gain `policy`,
  `always_allowed`, `stack`, `effective`, `isolation`, `grant`; the hub gains a
  read-only `floor` list. Existing fields are unchanged.
* Delegated sub-agents (`create_nxp_subagent`) inherit the parent's rule reader, so a
  child never runs a governed tool the parent's rules forbid or would ask about (a child
  has no approver: Ask is a rejection there). Since #368 the child is also not *sent*
  those tools: `create_nxp_subagent` keeps only the tools whose rule is Always when the
  child is built (an unreadable rule counts as Never; a tool whose name cannot be
  resolved is left out); the call-time gate is unchanged.
* Tool definitions: the rule reader never changes the tool list the model sees (Ask,
  Always and Never all keep the same specs and static system prompt; regression test).
  Never is additionally filtered out of the list by the host exactly like OFF before
  #190 (#227), and the breaker filter is unchanged. The rules change the list only when
  a rule is saved (or the rules lock); between saves only the breaker filter and the
  registry's fail-closed drops (e.g. a consent bridge that no longer holds) change it
  (#259, unchanged; measured, #368: [tool-definitions.md](../tool-definitions.md)).
* Not in scope: argument-pattern rules (e.g. `shell(git push *)`), recording the
  deciding layer in the EventStore (#171), category-wide approval switches.

## Addendum (#271): the data directory is a floor tree

**Context.** #190 put the files that decide what the agent may do on the floor
(`_policy_files()`), but the default `@workspace` *was* the data directory, so every other
file there — the approval store's `.head`, `events.db` (+ `-wal`/`-shm`/`.head`), the
scheduler log and its head, locks, `turns.db`, `checkpoints/`, the semantic layer and the
index — could be written by `file_write` / `editor` after an Ask approval. The keyless hash
chains (ADR 0008 / 0009) only detect tampering by someone who cannot write both the log
and its head; the agent could.

**Options.** (1) Add the missing names to `_policy_files()`; (2) put the data directory on
the floor with a few writable subdirectories; (3) give the agent a workspace of its own.
(1) alone repeats the failure: the next store file is missed again, so it was rejected.

**Decision: (2) + (3).** `WritePathPolicy(floor_tree=<data dir>, floor_tree_writable=…)`
puts the whole data directory — existing and new files, and any ancestor of it — on the
floor. The only exceptions are strictly below `workspace/` (the new default
`@workspace`), `memory/agents/<agent>/memory/episodic/` and `context-stash/` (the stash
must be inside the write roots, ADR 0005). An exception must be a real directory (a
symlinked one is ignored), the resolved path must match its components exactly (a
case/Unicode variant stays on the floor: over-deny only), and an existing file with a
second hard link is refused. Identity names, `read_only_paths` and `protected_dirs` still
apply inside the exceptions. The episodic layer and the stash are added as write roots
only while `workspace_dir` is unset and `@workspace` is configured — exactly where the old
default already covered them; an explicit `workspace_dir` adds nothing. The floor is part
of the shared policy object, so the HITL gate, `IdentityGuard`, the file-tool wrappers
(also in hook-less inner agents), `StiraWriteSandbox` (re-check at write time),
delegates (`narrow`) and `with_floor` all keep it. The settings page lists it as a
read-only floor item; `stira doctor` mirrors it for the default config.

**Compatibility.** Applied to existing installs too, not only new ones: leaving the hole
open behind a warning would keep the audit chains forgeable, and the change only narrows
what the agent may write. Nothing is moved or deleted; files the agent had written in the
data directory stay readable and become read-only for it, and relative paths now land in
`workspace/`. Migration (move your own files into `workspace/`, or set `workspace_dir`):
[operations.md §10](../operations.md#10-upgrading-past-271-agent-workspace-moved).
Tool definitions and their order are unchanged (#254).

**Review round 1.** The exception names are fixed lexically
(`stiracore.config.data_dir.writable_names`), never derived from resolved paths: a
symlink placed before start-up (`workspace -> checkpoints`, `workspace -> .`) used to be
promoted to an exception. Each name is walked with `lstat` from the data directory; a
symlink, a non-directory or a missing component drops the exception (warning; its contents
stay on the floor), and `.`/empty names are rejected. The server and `stira doctor` build
the policy with the same function (`build_policy`); the doctor recognises the default
config by file identity, not by spelling. The settings page lists only the exceptions that
are inside the effective roots. Between check and write: both the editor sandbox and the
upstream `file_write` (its module-level `open` is replaced by the consent bridge; the tool
is refused if that patch does not hold) open through `open_checked` — no final symlink,
the policy re-asked after the open (a parent swapped for a symlink resolves elsewhere),
the same inode, one link, truncate last, and a file it created is removed again on refusal.
A window remains between that re-check and the write on the same descriptor; it is not an
OS sandbox (#167).

**Review round 2.** A default `workspace/` that fails the check is never resolved
into a root (a symlink to an outside directory would otherwise become the write root):
`@workspace` is dropped from the roots, relative paths anchor at the data directory (on
the floor), and the server starts with a warning. A person chooses another place with an
explicit `workspace_dir` (operations.md §10); inside the data directory the floor still
wins. The start-up doctor and `stira doctor` check the effective roots (episodic, stash).

**Open (needs a decision).** Whether the context stash should be written only by its own
store: the agent's file tools can write there (as before #271), which lets the agent plant
an entry the read-back tool returns as an offloaded result. Refusing it needs ADR 0005's
"inside the write roots" rule for the stash to change.

## Addendum (#312): the read floor

**Context.** #271 put the data directory on the *write* floor only. The read tools had no
floor at all, and after #306 a relative path anchors at `<data dir>/workspace/`, so
`../tool-config.json`, `../tool-policy-approvals.json`, `../neural-state.json` and
`../checkpoints/<id>.json` (past conversations) were a short path away (absolute paths had
always worked). Writing them was impossible, so no permission grew, but the agent could
read the pending approval state, infer the policy, and fetch conversations back that
context management had dropped. No key value is stored there (`api_key_env` holds a
variable name only).

**Options.** (1) Do not let file tools read the data directory, except the folders the
agent writes; (2) let them read it but redact the results; (3) leave it as it is.
(2) needs a redaction rule per store format and still tells the agent what exists; (3)
keeps the hole.

**Decision: (1), applied at once (no warning-only period).** The read floor is the same
judgement as the write floor — one policy object, one tree, one list of names
(`WritePathPolicy.check_read`, `_in_floor_tree(read=True)`,
`config.data_dir.readable_names` = `writable_names`): inside the data directory file tools
may read only `workspace/`, `memory/agents/<agent>/memory/episodic/` and
`context-stash/` (each only after `name_problem` passed it: a symlinked or missing one is
not readable either); everything else there — the tool rules and their head/lock,
approvals, `hitl-*.db`, `events.db`, the scheduler log, `checkpoints/`, `neural-state.json`,
the gateway/agent configs, the semantic and procedural layers, the index, `turns.db`, new
and missing paths — is `floor`. Two differences from the write floor, both about writes
only: an ancestor of the data directory is readable (reading a directory above it
replaces nothing), and an exception directory itself is readable (listing `workspace/`).
Outside the data directory reads were unchanged (there were no read roots; reading a file
elsewhere on the machine still worked) — superseded by the #457 addendum below (read roots
and the secret-path floor). No approval lifts the read floor, like the write floor; it is
not a tool rule.

**Where.** `file_read`, `view_image` and `image_reader` (`WorkspaceAnchoredReadTool`),
`lsp_diagnostics` / `lsp_hover` (the same wrapper without re-anchoring) and `editor` `view`
(`IdentityProtectedTool`) check the path before the tool runs, and run each step with the
policy as the active read floor (`file_tools.read_floor`, a `ContextVar` that
`asyncio.to_thread` copies into the worker). Every open of these tools goes through
`file_tools.open_for_read`: the floor is asked before the open, the resolved path is
opened without following a final symlink, and after the open the **descriptor itself** is
judged: its `(st_dev, st_ino)` must not be one of the protected files
(`floor_identities()`: an `lstat` walk of the data directory that never follows a symlink
and skips the exception directories — the same set a hard link of a store file would
share; a walk that fails refuses). The name re-check and a `stat` comparison come on top,
but no longer decide alone: a path swapped back and forth between the checks (review
round 1) cannot make a protected descriptor pass. Inside the data directory, a path that
passes a symlink (also a symlinked exception pointing outside) is refused by name.
`image_reader`'s module-level `open`, its `Image.open` (the second, by-name open for
the format) and its `os.path.exists` are replaced (the tool is not registered if the patch
does not hold): the read goes through that opener, the format is detected from the very
bytes it returned (no second open, #315), and under a floor the existence check asks no
name (the checked open answers). `lsp_diagnostics` gives the analyzers the checked bytes,
not a name to open (#315): Ruff on stdin (`--stdin-filename`), Pyright as the open
document of a one-shot `pyright-langserver --stdio` session (its CLI cannot read a file's
text from stdin; without the language server Pyright is skipped); `lsp_hover` gives Jedi
`code=`. The editor sandbox reads through it and lists a
directory by descriptor (`O_DIRECTORY | O_NOFOLLOW`, `os.scandir(fd)`, entries with
`lstat` semantics, symlinks never followed or walked): a directory whose identity is on
the floor shows only its real exception subdirectories (the editor probes a path by
listing its parent, which may be the data directory), any other one hides hard links of
protected files. A refusal is
`reason: "floor"` with the path as given and **no** `exists` / `is_file` / `is_directory`
fields: it never tells whether a protected file exists.

**Not changed.** The memory tools (`memory_read`, `memory_search`, …) read through the
host's `MemoryStore`, not through file tools; the context stash's read-back
(`retrieve_offloaded_content`, ADR 0005) reads through its own store. Shell-class tools
(`shell`, …, exposed only with `STIRA_ENABLE_EXEC_TOOLS`) are not covered (#167). The bare
tools without a policy (library use) have no data directory and no read floor.

Each analyzer runs under one deadline (30 s; writes in their own thread), with bounded
output (LSP header 8 KiB, frame 8 MiB, 16 MiB and 10 000 frames per call; Ruff's stdout
16 MiB) checked again after the process ended (anything beyond, also after the answer:
the result is discarded and nothing is shown), and an allow-listed environment (`PATH`,
`HOME`, `LANG`, `LANGUAGE` and the listed `LC_ALL`/`LC_CTYPE`/`LC_COLLATE`/`LC_MESSAGES`/
`LC_NUMERIC`/`LC_TIME`/`LC_MONETARY`, temp and cache directories, `VIRTUAL_ENV`, the Windows
basics; no API keys or tokens). On POSIX the analyzer runs in a process group led by a
sleeping sentinel that is reaped only after the group is killed, so the group is always
Stira's to kill — also after a normal exit — and every descendant still in it ends; the
end is kill → join the reader/writer threads (bounded) → close the pipes. **Limits (not
verified on every platform):** a descendant that leaves the group (`setsid`, `setpgid`)
is not ended, and if it keeps the output pipe open the result is refused (`output did
not end`) and that pipe is left to its reader thread; on **Windows** only the analyzer
process is killed (`CREATE_NEW_PROCESS_GROUP` + `kill()`) — ending its descendants is
unverified until tested on a Windows machine. The hook-based read floor as a whole
(patched upstream modules, analyzer processes) is a best-effort boundary, not an OS
sandbox.

**Residual risk (measured 2026-10-16, Pyright 1.1.414, Ruff 0.16.9).** The analyzers
still read *other* files by name, with no floor: their configuration in the file's
parents (`pyproject.toml`, `ruff.toml`, `pyrightconfig.json`), imported modules and stubs
(Pyright, Jedi). This was the same before #315 (the Pyright CLI gives the same rows) and
is not fixed here:

* **Leaks — a protected file that is valid Python.** From an allowed `workspace/probe.py`,
  `from .. import hook` (no symlink needed: a relative import leaves the workspace) or a
  workspace symlink `evil.py -> ../hook.py` makes Pyright show `hook`'s names, signatures
  and literal values (`reveal_type(hook.TOKEN)` → `Literal['<the value>']`; a missing
  name is reported as missing, so names can be probed). No store in the data directory is
  Python; a `.py` file placed there (by whoever can write to it) would be exposed this way.
* **Does not leak — the stores.** A JSON store reached as a module (`cfg.py ->
  ../tool-config.json`) parses as an expression that binds no name:
  `Module("cfg")` only (test with a real Pyright, skipped when it is not installed).
  SQLite and Markdown are not valid Python.
* **Ruff** lints one file and follows no import. A protected `ruff.toml` in a parent is
  read as configuration: an invalid one ends Ruff with code 2 (`Analyzer exited with code
  2`; stderr is not kept), a valid one silently changes the rules — its content is not
  shown.

Their output is still discarded unless the path afterwards names the checked inode, passes
the floor and is no protected file. The analyzers are given the path the checked open
resolved and opened, not an earlier resolution of the name the model gave (#315: a
directory moved and replaced by a symlink between the two resolutions no longer makes the
analyzers look up configuration and imports under a name that was never checked). The
identity walk costs one `lstat` per data-directory file outside the exceptions on every
checked open. Not an OS sandbox.

**The identity walk (#315, the rest).** `floor_identities()` walks by directory
descriptor on POSIX: the tree and every directory below it are opened relative to the
parent's descriptor with `O_DIRECTORY | O_NOFOLLOW` and must be the directory that was
listed (`fstat` = the listing's `lstat`); a directory swapped for a symlink or another
directory during the walk is not followed — the walk raises (`FloorWalkError`, a fixed
text without a path) and the read refuses. Before, the walk scanned by path string and
followed such a symlink out of the data directory (test
`tests/test_read_floor_windows.py`, failed before the change). Open descriptors are bounded
by the depth and closed on every exit. Without descriptor support (Windows) the walk goes
by path and compares each directory's `lstat` with the listed one before its scan (a
narrower window remains there; unverified on Windows). The walk is bounded: 200 000
entries (`FLOOR_WALK_MAX_ENTRIES`) and 10 s (`FLOOR_WALK_DEADLINE_SEC`), constants, no
setting; past either the read refuses (fail closed). The deadline (monotonic clock) is
checked at every entry, before and after every directory's scan (an empty directory counts
no entry) and once more before the walk returns (the last I/O may pass it). Measured (M1 Max, APFS): 0.006 s per
1 000 entries, 0.034 s per 10 000, 0.34 s per 100 000, on every checked open. The set is
not cached by time (a stale set would widen the race).

**Residual risks (not fixed).** (a) A walk cannot see a concurrent rename: a directory
moved from a not-yet-walked to an already-walked place (by a process with write access to
the data directory — not the file tools, which cannot write there) is missed. (b) A
protected file atomically replaced (a store's normal temp-and-rename save) leaves its old
inode protected by nothing if a hard link to it already exists outside the data directory:
that old content reads as an ordinary file. It needs a hard link planted by such a process
beforehand; the file tools create no links. Pinning the protected inodes at start-up was
not done: freed inode numbers are reused (ext4), so workspace files would be refused at
random, and the pinned set would not cover files created later. (c) The analyzers'
by-name reads of other files (above) and Jedi's import inference for `lsp_hover` (no
post-run check; it only reads `code=` for the file itself).

**Compatibility.** Narrows what the agent may read. Anything that read data-directory
files through file tools (other than the three folders) is now refused:
[operations.md §11](../operations.md#11-upgrading-past-312-file-tools-no-longer-read-the-data-directory).
Tool definitions and their order are unchanged (#254).

## Addendum (#457): read roots and the secret-path floor

**Context.** After #312 the read tools could still read anything outside Stira's data
directory: measured on main `608153a`, `file_read` read `/etc/hosts`, `~/.ssh/known_hosts`,
`~/.zshrc` and `~/.aws/config`; `~/.aws/credentials`, `~/.ssh/id_*`, `.env` and the tokens
under `~/.config` were one call away. Today the external tools are off, so nothing read can
leave; #450 (tool profiles), a YOLO external window or `shell` would each turn a wide read
range into an exfiltration path. An approved `shell` command also inherited the server's
whole environment (provider keys, `AWS_*`, tokens).

**Decision.** The read check (`WritePathPolicy.check_read`, the same function and the same
normalization as #243 / #312: `~`, workspace anchoring, `realpath` of the existing prefix,
casefold + NFC keys for deny, exact components or file identity for allow) gains two
layers after the data-directory floor:

1. **Secret-path floor** (`secret_path`, `E_READ_SECRET_PATH`): `identity_paths.SECRET_*` —
   home-relative (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/{gcloud,gh,op,…}`, `~/.netrc`,
   `~/.npmrc`, keychains, browser profiles for macOS / Linux / Windows, …), absolute
   (`/etc/shadow`, `/etc/master.passwd`, `/etc/sudoers`, `/etc/ssh`, `/Library/Keychains`,
   …) and name patterns of the final component (`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`,
   `*.pfx`, `id_*` without a dot or with `.pub`, `*.kdbx`, …). Judged on the lexical and the
   resolved path, case/Unicode-insensitively, by file identity for existing secret paths
   (another spelling, a bind mount, a hard link of a listed file) and, for a file with a
   second name, by a bounded walk of the secret directories (past its bound: refused).
   `AgentConfig.read_secret_paths` adds entries (a path or a name pattern); the built-in
   floor cannot be removed. It applies whatever the read roots, also with `"/"`, and to every
   `WritePathPolicy` (also without a data directory). The editor's listings hide entries with
   a secret name.
2. **Read roots** (`outside_read_roots`, `E_READ_OUTSIDE_ROOTS`): `AgentConfig.read_roots`,
   default `["@workspace"]`; with the default workspace the episodic layer and the context
   stash stay readable (`effective_read_roots`, the rule of `effective_write_roots`). A broad
   root (`/`, `~`) is allowed — the floor above stays — but on the settings page every root a
   save adds is the danger `read_roots_widened`: confirmed, recorded in the settings history
   before the save (a loosening), refused while a tool call runs under the YOLO host gate.
   A delegate inherits the parent's read roots and secret entries (`narrow`).

**What a refusal looks like.** The code and a fixed sentence (the floor's text now carries
`E_READ_FLOOR`), the path as given, and a diagnosis with `reason` + `code` and **no**
`exists` / `is_file` / `is_directory`: decided before anything is opened, so a refused path
answers the same whether it exists or not (tested by creating and deleting the file). A path
inside the roots that does not exist is still `path_not_found`.

**Audit.** Every refusal is one `audit.read.denied` row on the chained `events.db`
(`AuditChain.record_read_refusal`): the tool name, `path:<HMAC-SHA256>` of the absolute path
(a key of this process only, as `run_checks` / YOLO) and the code — never the path, a file
name or content. A failing audit write is logged and never opens the read.

**Exec children.** The scoped `os` of the exec modules (#362) runs `execvp` / `execv` with
`child_env.child_environment()`: an allowlist (`PATH`, `HOME`, `USER`, `SHELL`, `TERM`,
locale, `TMPDIR`, …), and even a listed name is dropped when it looks like a key/token or its
value is one `mask_secrets` would change. An approved `shell` command no longer sees
`AWS_*`, `*_API_KEY`, `*_TOKEN` or any other variable of the server.

**Residual risk.** A hard link of a file inside a large secret directory is found only by
the bounded walk; `cron` starts `crontab` with the server environment (a fixed program, not
the model's command); a `shell` command can still read any file the OS user can (the read
roots govern file tools, not exec — #167's sandbox); the list of secret paths is a list,
not a classification (new tools keep their tokens elsewhere: add them with
`read_secret_paths`).

## Addendum (#536): the secret-path floor applies to writes

**Context.** #457 added the secret-path floor to reads only; `WritePathPolicy.check` (the
write side, #243) did not call it. With `allow_broad_write_roots` and a root of `/` or `~`,
`file_write` / `editor` could rewrite `~/.ssh/authorized_keys`, `~/.aws/credentials` or a
`.env`.

**Decision.** `WritePathPolicy.check` calls the same `on_secret_floor` (the same table:
`SECRET_*` plus `read_secret_paths`; the same keys: `~`, workspace anchoring, `realpath` of
the existing prefix, casefold + NFC, file identity, the bounded hard-link walk) on the
lexical and the resolved target, its side files and every directory `makedirs` would create.
Order: the write floor (`floor`) → **secret-path floor** (`secret_path`,
`E_WRITE_SECRET_PATH`) → write roots. No root (also a broad one) and no approval lifts it;
it applies to every `WritePathPolicy`, also without an allowlist. The shared decision
(`check_tool_write`) feeds IdentityGuard, the HITL gate and the tool wrapper alike.

* **What a refusal looks like:** the code and a fixed sentence — never the matched entry nor
  whether the path exists (the judgement is the same for a missing path, a new file is
  judged on its existing ancestors). The write-failure card says `secret_path` (no retry).
* **Audit:** one `audit.write.denied` row per refused call (`AuditChain.record_write_refusal`,
  written by the write-failure hook): the tool, `path:<HMAC>` of the absolute path and the
  code.
* **Containment (ADR 0023):** `E_WRITE_SECRET_PATH` is a G1 signal like the read code.
* **Settings:** every write root a save adds is the danger `write_roots_widened` —
  confirmed and recorded in the settings history before the save, as `read_roots_widened`.

**Residual risk.** As for reads: shell/MCP code is out of scope (#167); the list is a list.

## Addendum (#273): restoring a backup brings back older rules

**Context.** §3's checks compare `tool-config.json` with its head, the lock marker and the
settings history — all files in the data directory. Restoring the data directory (or just
the rules, head and history together) from a backup gives a set that agrees with itself:
the older, possibly looser rules load without a lock (reproduced in the issue: a write
tool Always → Never → restore → Always again, generation 2, no lock). Any marker kept in
the data directory is restored with it, so no check inside it can tell "restored" from
"current". ADR 0011 §6 states the same for the scheduler and makes every start
conservative instead of relying on such a marker.

**Options.** (1) Compare with the last generation in `settings-history.jsonl` — restored
together with the rules, so it only catches a partial restore (and the history does not
record generations today). (2) Keep the highest generation outside the data directory.
(3) Lock the rules on **every** start (ADR 0011 §6 taken literally) — every restart would
need a `RECOVER` save, which breaks the always-on hosts ADR 0011 already has to resume by
hand, for a rare event. (4) Make the restore procedure itself put the lock, so a person
re-approves the rules after a restore.

**Decision: (4) for everyone, (2) as an opt-in; (1) and (3) are not done.**

* **Restore command.** `stira tool-rules lock` (also `python -m stiracore.diagnostics
  tool-rules lock`) writes `tool-config.json.locked` with reason `restored_backup`; the next
  load — the next start, or the next call of a running server — is locked (every tool
  Never) and only the existing `RECOVER` save (§3) unlocks it. It writes nothing else and
  changes no rule value. `stira tool-rules status` reads only (generation, head, marker,
  anchor; exit 1 when a load would lock). The runbook makes it a restore step
  ([operations.md §8](../operations.md#8-backup-and-restore)).
* **Anchor (opt-in, default off).** `STIRA_TOOL_RULES_ANCHOR_DIR` names a directory outside
  the data directory holding `{generation, digest, rules, install_id}` in a file named by a
  hash of the rules' path (the path itself is not stored). `install_id` is a random ID
  made by `init-anchor` (or by the first save over an anchor that has none) and written
  into the head of every save; it is not a secret and holds no path or name. It is created only by a person — `stira tool-rules
  init-anchor` (the rules must exist, check out and be unlocked; an existing anchor is
  never replaced) — or by a `RECOVER` save; **a load never creates it**. A load locks when
  the anchor is missing (`anchor_missing`), is a symlink, has a second link or another
  owner (`anchor_unsafe`: read with `O_NOFOLLOW` + `fstat`, the owner-lock helper
  `owner_lock.read_private_file`; Windows has no `O_NOFOLLOW`, as for the owner locks), is invalid (`anchor_unreadable`), or lies inside a data
  root — the rules' directory, the server's registered data directory, resolved through
  symlinks (`anchor_misconfigured`); when the file's generation is **older** than the
  anchor or the data directory looks new while the anchor exists (`anchor_rollback`; the
  first-run defaults are not written); when the head's `install_id` is not the anchor's
  (`anchor_foreign_install`: a file + head from another install, another path or from
  before `init-anchor`, **whatever its generation and rules**, also one step ahead with
  the same rules); when the generation is the **same** with other rules
  (`anchor_mismatch`); when it is **more than one** above the anchor
  (`anchor_generation_jump`: each save numbers itself one above the anchor); when it is
  **one** above but `rules_loosening` (the one loosening check, §3) finds it looser than
  the rules snapshot the anchor keeps, or the anchor has no snapshot or no `install_id`
  (`anchor_unconfirmed_generation`); and when a newer generation's anchor update fails
  (`anchor_unwritable`). Only a one-step, non-loosening generation of this install — a
  save whose anchor update failed — moves the anchor up on a load. The anchor keeps no
  snapshot when a secret-looking command would have to be left out, or when it would
  exceed 64 KiB (a newer generation then locks). An anchor written before the identity
  existed still loads at its own generation; the next save gives it and the head one ID.
  Recording loads, saves and `init-anchor` hold one in-process lock; across processes,
  every anchor write — and, with the anchor on, a whole save (numbering, rules, head,
  anchor) and `init-anchor` — holds an OS file lock next to the anchor (`<anchor>.lock`:
  `flock`; `msvcrt.locking` on Windows, unverified), and re-reads the anchor under it: a
  write below the generation it holds (or the same one with other rules) is refused. So
  the check and the replace are one step for every process and the anchor never moves
  back (measured with two processes interleaved at the anchor write). The lock file is
  opened like the owner locks (no final symlink, regular file, one link, this user's);
  anything else, or not obtaining it within 5 s, fails closed: a load that has to move
  the anchor up locks (`anchor_unwritable`; a load whose rules match the anchor writes
  nothing and takes no lock), a save fails before replacing anything. An anchor that is
  missing or unusable when read again under the lock is written only by `init-anchor` or
  a `RECOVER` save of locked rules; a load or a normal save locks with that reason
  (`anchor_missing` / `anchor_unreadable` / `anchor_unsafe`) and writes nothing. The lock
  file is on the write floor with the anchor. A save checks the anchor before writing: a missing/unsafe one
  needs the recovery word; it probes the anchor directory before replacing anything, and
  if the anchor still cannot be written after the rules were replaced the save **fails**
  (`AnchorWriteError`, an `OSError`: 500 `persist_failed`) and a lock stays latched. A save
  numbers its generation above the anchor. The anchor file is on the chat server write floor
  (`protected_files`).
* **The commands are host commands.** No registered tool runs them; the exec class that
  could is hidden unless `STIRA_ENABLE_EXEC_TOOLS` and then asks per call; every file they
  write (marker, anchor) is on the write floor. `--file` must name a regular file (or a
  missing one in an existing directory), never a symlink; the marker likewise.
* **Default behaviour is unchanged** (no anchor: the reproduction still loads unlocked; a
  test pins that so the residual stays visible). Nothing loosens: every new outcome is a
  lock that the existing recovery clears.

**Residual risk.** Without the anchor, a restore that skips the runbook step is not
detected. With it: restoring the anchor from the same backup removes the protection
(deleting it locks); moving the data directory to another path looks like a missing
anchor (locked until `RECOVER`); the digest is keyless and `install_id` is not a secret,
so someone who can write the data directory *and* read the anchor can forge a generation
one step ahead that loosens nothing (tampering, not a restore — out of scope here as in
§3; a forged looser or further-ahead one locks). Whoever can hold the anchor's lock file
(the server's OS user) can stop every anchor write: that only locks the rules
(fail-closed), it never loosens them. The approval store, the scheduler log and `events.db` have the same "consistent
older copy" property; for them the runbook moves the approval store aside, keeps the
current ledger (measured: ADR 0006 addendum #273) and re-checks every job.

## Addendum (#362): an approved exec call runs, and only that call

**Context.** §3 makes every exec-class call Ask, but the upstream consent bridge (#242)
covered `file_write` only. An approved `shell` or `cron` call then reached the upstream
terminal prompt: a headless server (stdin `/dev/null`) read an empty line and the call
failed in milliseconds with `Command execution cancelled by user. Input: ` (measured
2026-10-17, and reproduced offline on `464dc52`: `shell` and `cron` fail after
approval; `batch`, `use_agent`, `think`, `swarm` and `graph` have no upstream prompt
and ran). With `BYPASS_TOOL_CONSENT=true` or `STRANDS_NON_INTERACTIVE=true` in the
process environment the upstream tools skipped the prompt for *any* caller, and nothing
but the hooks stood between a direct `stream()` and the command.

**Decision.** The exec class is gated like the file tools, with one addition:

* **Gate mark only after a human approval.** `ApprovalBindingVerifier` (the last
  `BeforeToolCall` hook) issues the single-use mark for an exec tool
  (`tool_consent.HUMAN_APPROVAL_TOOLS`) only when this call's HITL binding was
  verified and its ledger entry claimed — whatever the rule reader answered (an
  Always that should not exist, no reader at all: refused, "exec-class tools run only
  after a human approved this exact call"). Breaker and floor apply first as before
  (§2): WARNING refuses without a card; CRITICAL with `halt` asks for every call.
  **ADR 0012 revision 8 (phase 7, 2026-10-19):** "a human approval" becomes "a human
  approval, or a YOLO automatic approval of this exact `shell` / `batch` call in a window
  whose person chose *Include exec* at the press" (ADR 0012 §9 option C); the mark is
  bound and single-use exactly as here, and `cron` and the child-agent tools still need a
  person.
* **Execution boundary.** Every exec tool is registered behind `ExecGatedTool`
  (`tools/exec_gate.py`; same name, spec and position, #254): it consumes the mark for
  exactly this `toolUseId` and argument hash, runs a snapshot of those arguments,
  re-checks the bridge and holds the consent grant only while one upstream step runs.
  A direct `stream()`, a replay of the same call, an inner agent without Stira's hooks
  (the children of `think` / `use_agent` / `swarm` / `graph`, #145) and a call inside an
  approved `batch` that was not approved itself (its own card) are refused.
* **Bridge.** `CONSENT_TOOL_MODULES` lists every exec module. Where the module has a
  terminal prompt (`shell`, `cron`) it is replaced by one that never reads the terminal,
  and the module's `os.environ` answers `BYPASS_TOOL_CONSENT` (and, for `shell`,
  `STRANDS_NON_INTERACTIVE`, which also turns off its terminal echo and stdin
  forwarding) only inside that tool's grant; the real environment is ignored. A module
  that must have a prompt and has none, or that reaches the real environment or the
  prompt module by another name (`from os import environ`, `import os as _os`,
  `get_user_input_async`), is refused and **not registered** (fail closed).
* **Reasons.** A refusal names its cause (no gate approval, arguments changed, bridge
  not confirmed, no human approval, outside the approval gate); an upstream "cancelled
  by user. Input: " with an empty reason is never shown alone.

* **Entry guard.** The function each exec module runs (`DecoratedFunctionTool._tool_func`,
  or `batch`'s module function) is replaced by one that runs only inside a grant for
  *that* tool, entered once per grant. So the upstream tool refuses even when something
  other than Stira's registry loaded it (a child agent built with the raw module), and a
  child inside an approved `use_agent` step cannot start `use_agent` again with the
  parent's grant.
* **Pinned upstream release.** The bridge for the exec class is accepted only for an
  audited `strands-agents-tools` release (`VERIFIED_EXEC_UPSTREAM`, now `0.8.9`); any
  other release drops every exec tool (logged, fail closed) until its exec modules are
  audited again (`test_pinned_upstream_source_audit`: AST scan for environment and stdin
  access, the prompt, function-level imports, `__import__`/`exec`/`eval`). `file_write`
  is not pinned (unchanged from #242).

**What is guaranteed for child agents (review of #397).** `think`, `use_agent`, `swarm`
and `graph` run a child agent with the parent's model and tools from Stira's registry,
but with none of Stira's hooks, so the child has no approval route. Measured with the real
upstream tools and the fake endpoint (`test_children_of_approved_exec_tools_cannot_run_exec_tools`,
`test_child_that_loads_upstream_tools_itself_runs_nothing`), after a human approved the
parent:

| Child calls | Result |
|---|---|
| an exec tool from the registry (`shell`, `cron`, `batch`, `use_agent`, …) | refused (no gate mark), no card, no side effect, no grandchild agent |
| an exec tool it loaded itself from `strands_tools` (raw module) | refused at the entry point (no grant for that tool, or the grant already entered) |
| `file_write` / `editor` | refused (no gate mark, #242/#145) |
| a non-exec, non-file tool (`http_request`, `web_fetch`, `webhook_send`, memory tools, MCP tools, …) | **runs without being asked again** — not guaranteed (#145) |
| code inside the child's own model provider (`model_provider` paths in the approved arguments) | runs as part of the approved call — the approval covers those arguments |

**Scope of "bridge confirmed".** `_unrouted_path` compares the module's globals by
identity: it catches `from os import environ`, `import os as x`, the prompt module and its
functions bound under another name. It does **not** see a relay (`env_get =
os.environ.get`, an object holding `os`), an import inside a function, or what the
libraries a tool calls read (`console_util`, `create_model`, the SDK). For the exec class
those are covered by the release pin and the source audit, not by the runtime check; for
`file_write` the runtime check and the #242 tests are all there is.

**Not changed.** Exec tools stay hidden unless `STIRA_ENABLE_EXEC_TOOLS`, Ask by default
and never Always (§3, §5); `isolation` stays `none` (§7): an approved command runs as the
server's OS user, not in a sandbox (#167, #318). The approval checks the arguments the
model sent; it does not inspect what an approved command or child agent does with
ungated tools (table above).

Measured offline (fake Bedrock endpoint, headless server, `STIRA_ENABLE_EXEC_TOOLS=1`):
`tests/test_headless_exec_tools_e2e.py`; unit level: `tests/test_exec_tool_consent.py`.

## Addendum (#365): notes from ADR 0012 (YOLO)

Only with `STIRA_YOLO_ALLOW` ([ADR 0012](0012-yolo-mode.md)); without it nothing here changes.

* **§1 (Ask).** While a YOLO window a person confirmed is active, the gate's approver may
  answer the Ask of an internal tool (and, in an "External allowed" window, of an external
  tool whose arguments carry no secret) in a chat / Stage turn, with an approval bound like
  a card's and recorded as `audit.yolo.auto_approved`. Never, Always, the floors (§2,
  #271, #312), the breaker's write block and CRITICAL are unchanged; the rules themselves
  are not edited.
* **§4 (changing rules).** While allowed, a loosening save (tool rules, hub, rollback) is a
  confirmed operation (428 `host_confirmation_required`, then a person's confirmation in the
  page or on the host) and is refused while an agent tool call runs (409
  `tool_call_running`). Tightening saves are unchanged.
* **§5 (persistent permissions).** A YOLO window is not a persistent permission: memory
  only, six hours at most, ended by Stop, `/new`, the server's stop and every restart.
* **#362 addendum.** No change: YOLO never approves the exec class (ADR 0012 Open (2), as
  implemented); an exec call keeps its card in every tier.

## Addendum (#508): the agent is told which tools are disabled, and why

**Context.** §1 removes a Never tool from the tool list, so the agent could not tell a tool
a person switched off from one that does not exist: asked about the build, it reported
`page_check` (#449) and `propose_procedure` (#463) as missing features, and it credited a
YOLO window with calls that ran under Always rules (0 automatic approvals in the window).

**Decision.** The chat server appends a short **Tool Availability** section to the
system prompt (`tools/tool_availability.py`, built in `stira_server.
_tool_availability_provider`). It lists, sorted by name, every tool the server will not run
for a static reason — name, one line of purpose, reason codes — and then the current
permissions:

| Code | Meaning | Who can enable it |
|---|---|---|
| `policy_never` | the saved rule is Never (the `/api/config/tools` row's `policy`) | a person, Settings → Tools |
| `rules_locked` | the rules failed their check (§3), every tool Never | a person reviews and saves them |
| `exec_not_enabled` | exec class not exposed (#163) | a person, `STIRA_ENABLE_EXEC_TOOLS=1` at start-up |
| `dependency_missing` | `page_check` without a browser (ADR 0015) | a person installs one / `STIRA_BROWSER_PATH` |
| `not_configured` | `run_checks` off (ADR 0019), `memory_rate` without `memory_autonomy` (ADR 0018) | a person, the configuration file, restart |
| `unsupported_host` | `page_check` / `run_checks` on Windows or as root | — |

then `Tool rules now (N rule-governed tools): Always a / Ask b / Never c` (the rule as the
gate reads it: a capped Always counts as Ask) and — only with the YOLO allowance — the
window (none open / open, tier, exec, its fixed end) and one sentence: YOLO only approves
Ask calls; Always calls never needed an approval (ADR 0012 Addendum #508). The wording toward the model is only "you cannot use them;
you can ask the user to enable one (suggest the wording)".

* **No permission changes.** The section is text. A Never call is still refused by the gate
  (`rule_never` on the settings page); the refusal the model reads now names the code and
  "only a person can enable it (Settings → Tools)". No path lets the agent change a rule.
* **No schema.** Names, a one-line purpose (a curated line, else the first sentence of the
  description) and codes only; the tool list is unchanged. Measured with
  `scripts/tool_payload_probe.py` (the real ConverseStream request, offline): `default`
  system 3,911 → 5,423 JSON characters (request 17,529 → 19,041, +8.6%; tools 21 /
  13,474 unchanged), `exec` 3,910 → 4,523 (+1.8%); no YOLO allowance in either.
* **Cache stability (#254 / #259).** The section is a pure function of the rules this turn
  applies, the start-up switches and the window's id / tier / exec / end — no clock, no
  count, no pause state, no breaker level (breaker restrictions are in the Breaker
  section). The same settings give the same bytes; it changes on a rules save (when the
  tool list changes anyway) and when a YOLO window opens or ends. With prompt caching on
  it is the end of the cached static part (after SOUL and Procedural Rules); off, the end
  of the prompt.
* **Scope.** Registry tools (the `/api/config/tools` rows) plus the four host-bound or
  optional tools of this build (`page_check`, `run_checks`, `memory_rate`;
  `propose_procedure` is a registry row). Not listed: `memory` / `perplexity_search`
  without their keys, `delegate` (offered per turn, ADR 0016), breaker-hidden tools.

## Addendum (#547): an approved `http_request` POST runs, and only that call

**Context.** The upstream `http_request` asks `[y/*]` on the terminal before POST, PUT,
PATCH and DELETE unless `BYPASS_TOOL_CONSENT=true`. It was not in the consent bridge, so a
POST a person had approved on the card read an empty line and came back `HTTP request
cancelled by the user. Reason: ` (never sent), while `BYPASS_TOOL_CONSENT=true` in the
process environment let an Always-rule POST go out without anyone seeing it (both
reproduced on `d92dd05`, `tests/attack/test_a4_external_send.py`).

**Decision.** `http_request` joins `CONSENT_TOOL_MODULES` (prompt replaced, scoped
`os.environ`, not registered when the bridge cannot be confirmed). A call with a modifying
method (`tool_consent.modifying_http_call`, compared as the upstream does) gets its
single-use gate mark only under the same condition as an exec call: a verified HITL
binding of a **person's** approval (`needs_person`); with Always or a YOLO approval it is
refused with the reason (as before it was never sent, now the model is told why).
`HttpConsentGatedTool` (inside the own-dashboard guard) consumes the mark, runs the
approved arguments and holds the grant only while one upstream step runs. GET and every
other method ask nothing and run as before, without a mark.
