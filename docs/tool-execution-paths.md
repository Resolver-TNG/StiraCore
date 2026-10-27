# Tool execution paths and their safety decision points (#145)

Measured on main + #145 with the offline `FakeModel` (real Strands tool loop, no
cloud calls). The fixed tests are `tests/test_tool_path_contract.py` and
`tests/test_procedural_write_gate.py`. CI writes the measured matrix to the
`tool-contract-report` artifact (`STIRA_CONTRACT_REPORT`). The headless server process
(stdin `/dev/null` or an open pipe, `BYPASS_TOOL_CONSENT=true`, the real `BedrockModel`
against a local stand-in endpoint) is fixed by `tests/test_headless_file_tools_e2e.py`:
Ask → card, Never → refused without a card, and the #243 bypass spellings refused at
Always (measurement).

`identity_guard` and the file-tool wrapper inspect arguments and paths. They are
**not an OS sandbox** (see `identity_paths.py`).

## Decision points, in execution order

| # | Where | What it decides | Applies to |
|---|---|---|---|
| 1 | `tool_registry.get_tools(level, read_only_paths, protected_dirs, include_exec)` | Exec-class tools hidden unless opted in; WARNING+ drops `WRITE_TOOLS`; wraps `file_write`/`editor` in `IdentityProtectedTool` | standard, MCP (registered into `READ_TOOLS`/`WRITE_TOOLS`) |
| 2 | `tool_registry.register_mcp_tools(provider, read_tool_names=)` | Unknown MCP tools default to WRITE; read/write name collisions rejected | MCP |
| 3 | `NXPAgent._prepare_turn` | HALT keeps write tools listed so each call reaches the HITL gate (#4; no approver → rejected, the turn continues; only `NXPSubagent` stops before the model call); `REQUIRE_CONFIRMATION` (WARNING) or `BLOCK_WRITES` without HALT drops tools named in `blocked_tool_names` ∪ `WRITE_TOOLS`; unresolvable names are dropped | every NXPAgent / NXPSubagent turn |
| 4 | `CheckpointHooks.before_tool` (BeforeToolCall) | At CRITICAL+HALT asks HITL for names in `blocked_tool_names` ∪ `WRITE_TOOLS`; approve/edit/reject/respond. A call `IdentityGuard.path_denial` already denies (floor, outside write roots) is refused **without asking** (#243) | NXPAgent, GuardedDelegate |
| 5 | `IdentityGuard.check_tool_call` (BeforeToolCall) | Write-capable = name heuristic ∪ `blocked_tool_names` ∪ live `WRITE_TOOLS`. `path_denial`: the floor (identity paths, read-only paths, `protected_dirs`) over all arguments, plus the write roots for the `path` of `file_write`/`editor` — denied at every level (HITL never overrides); BLOCK_WRITES denies unless HITL approved; WARNING denies (no confirmation gateway) | NXPAgent, GuardedDelegate |
| 6 | `IdentityProtectedTool.stream` | `WritePathPolicy` at the execution boundary: SOUL/IPD basenames, configured read-only paths, `protected_dirs` subtrees (symlink/hard-link/case/NFD aware), write roots; also the directories `makedirs` would create. The tool receives the checked absolute path. `editor` is Stira's (SDK `file_editor` + `StiraWriteSandbox`, #245): no content cache, no `.bak`, and its sandbox re-checks every wrapper's policy and writes only the checked path, only inside an approved step (no final-symlink follow) | `file_write`, `editor` — also inside hook-less inner agents |
| 7 | `memory_tools.memory_write` | Only the episodic layer may be written by tools | memory tools |
| 8 | `delegation._register_delegation` / `create_nxp_subagent` | Delegate names join the parent's `blocked_tool_names`; child gets its own engine/breaker, copied policy, inherited `read_only_paths` and write policy (`write_roots=` may narrow, never widen), file tools re-wrapped; `handoff_to_agent`/`subagent` blocked | `register_subagent_tool`, `register_spawn_tool`, Swarm |
| 9 | `tools.delegate_tool.DelegateTool` (`delegate`, ADR 0016 phase 1, #486) | the chat server: rule Ask (a person's card; YOLO: `child_not_yet_covered`), offered only while `tool_registry.writes_allowed` holds for the breaker level (NORMAL / COHERENCE) and only in a chat / Stage turn a person's message started (`yolo.origin.started_by_person`; a webhook, bot, approval-resume turn or a window's automatic continue does not list it), write class for the breaker. The child (`create_nxp_subagent`) gets `requested ∩ the running parent turn's tools ∩ rule Always`, never exec / `delegate` / `subagent` / `handoff_to_*`; before its own gate `ChildLimitHook` refuses a call over the tool-call cap, after a stop, and a write-capable call while `writes_allowed` does not hold for the parent's level or its live stability (Q4, one criterion with the offer); depth 1 (called from anything but the parent's running turn: `refused_depth`), one at a time, caps on calls / time / children per turn; the parent's stop signal, the end of the YOLO window it started in and the server's stop set the child's cancel signal (the server's stop is seen from before the child is built); the summary, paths and tool names come back inside a `<delegate-output>` block marked as data (NFKC, no `Cf` characters, every `<` / `>` and look-alike as `&lt;` / `&gt;`) | the `delegate` child |

The chat server (`dashboard/stira_server.py::_stira_tools`) keeps write tools listed at
CRITICAL only because the HITL gate (#4) owns them, and passes SOUL/configured
read-only paths plus `memory/procedural` to #1/#6 (#144).

## Write path policy (#243)

`identity_paths.WritePathPolicy` is the one decision used by #4, #5 and #6:

1. Normalize: `~` expanded, relative paths anchored at the agent's workspace (never
   the process cwd). `..` after a directory that does not exist is refused (`makedirs`
   would create that directory although `resolve()` hides it). On Windows, alternate
   data streams (any `:` but the drive separator), trailing dots/spaces, the `\\?\` /
   `\\.\` / `\??\` namespaces and reserved device names are refused.
2. Resolve symlinks of the existing prefix. The tool is given this **resolved** path,
   so the checked target is the written target (editor writes no `.bak` since
   #245). This holds at one instant only: a concurrent replacement (TOCTOU) is
   narrowed (editor's sandbox re-checks right before writing and does not follow
   a final symlink) but not prevented — not an OS sandbox.
3. Floor — denied whatever is approved — for the target, its side files and **every
   directory `makedirs` would create**: SOUL/IPD basenames (also `SOUL.md:stream`,
   `SOUL.md.`), `read_only_paths` (and anything below one), `protected_dirs`. Compared
   with a casefold + NFC key (stream suffixes and trailing dots dropped; over-deny only)
   and by file identity of existing ancestors. Unsure volumes count as case-insensitive.
4. Write roots (`AgentConfig.write_roots`, default `["@workspace"]`) for the same
   candidates: outside every root is denied (`write_outside_action="deny"`; `ask` is
   reserved for a later HITL card). Inside = exact component prefix of the resolved
   root (never `WindowsPath.__eq__`, which folds case) or file identity of an existing
   ancestor. A root that is `/`, a drive root, the home directory or an ancestor of it is
   a construction error (the chat server: start-up error) unless `allow_broad_write_roots`;
   this deny check is conservative (casefold/NFC key and file identity with home and
   its ancestors, so `/Users/ALICE` or an NFD spelling of home is refused too).

"@workspace" is `AgentConfig.workspace_dir` when set, else the server's
`<data directory>/workspace/` (the chat server: `.local/stira/workspace/`, #271); its
effective absolute path is shown in the settings hub. With that default the episodic
layer (`memory/agents/stira/memory/episodic/`) and the context stash
(`context-stash/`, #258) are write roots too, as they were inside the old default.
**The whole data directory is a floor tree** (`WritePathPolicy(floor_tree=…,
floor_tree_writable=…)`): every store, side file (`.head`, `-wal`, `-shm`), log, lock,
checkpoint, the semantic layer and the index, and any new file there, is refused
whatever the roots or approvals say. Only strictly below those three subdirectories
can a file tool write, and only when the subdirectory is a real directory (a symlinked
one is ignored), the resolved path matches its components exactly (case/Unicode
variants stay on the floor) and the file has no second hard link. The names are fixed
(`stiracore.config.data_dir`, shared by the server and `stira doctor`) and checked
with `lstat` at start-up: a symlink, a missing directory or a non-directory anywhere on
the way is dropped with a warning, never followed into an exception. Writes then open
through `checked_open.open_checked` (editor sandbox and the upstream `file_write`
module's `open`): no final symlink, the policy is asked again after the open, the same
inode, one link, truncate last. The hub ("Where files may be written") offers *workspace only* (default)
/ *workspace + /tmp* / *custom*, refuses broad roots, records changes in the history,
and the chat server applies them at restart.

**Deny-by-default comes from `AgentConfig`.** Library callers that pass no policy keep
the floor only (no write roots; backward compatible). An `NXPAgent(write_policy=…)`
wraps every `file_write`/`editor` it holds with that policy (on each `tools`
assignment; nesting never weakens an existing wrapper); delegates inherit it and can
only narrow it.

**Plain OS-protection command names are refused (#244).** A command-capable tool call
whose input names `xattr`, `chflags`, `sudo`, `tccutil`, `csrutil` (or the TCC database) in
plain text is refused — names computed at run time are not detected (exec tools are off by
default; #167):
in `IdentityGuard.path_denial` (so the HITL gate never asks) and by
`OsProtectionGuardTool` around the exec-class tools in `get_tools` (final input, also in
inner agents). The TCC database is on the path floor for `file_write`/`editor`. A failed
write is classified and shown as a permission card instead — see
[diagnostics.md](diagnostics.md) and [ADR 0007](adr/0007-write-diagnostics.md).

### Memory writes (host-managed exception)

Agent-originated memory writes do not take a path and are **not** governed by write
roots; they have their own boundary, set by the host:
`memory_tools.memory_write(layer, key, content)` → only `episodic` (other layers:
"denied") → `MemoryStore.write` → `_path`: `key` must be a relative POSIX path without
`.`/`..`/`\`/NUL, under the host-configured `memory_dir/<layer>/`, no symlinked
component at or below the store root (its ancestors are resolved once when the store is
built, #296), resolved inside `memory_dir` → `_require_writable` (floor: SOUL/IPD and the
store's `read_only_paths`) → symlink re-check → `mkdir(parents)` below
`memory_dir/<layer>` → resolved parent inside `memory_dir` → `NamedTemporaryFile` in that
same parent → `os.replace` onto the checked path. So the temp file, the rename and the
created parents never leave `memory_dir/episodic/`, whatever the write roots are
(`test_memory_write_stays_inside_memory_dir_whatever_the_roots`). The key validation
(`MemoryStore._validate_key`) and the containment in `memory_dir`
(`path.resolve().is_relative_to(memory_dir)`) are enforced by `MemoryStore` itself, not
by `WritePathPolicy`. The only other host-managed side effect of a memory write is the
store's SQLite index `memory_dir/index.sqlite3` (plus SQLite's `-journal`/`-wal`/`-shm`
siblings in the same directory) — the single exception the test allows. Host-side writes
use `MemoryStore` directly and are trusted; procedural rules go through the approval CLI
(see "Procedural layer" below), and a procedural entry without its provenance is not read.
When `workspace_dir` moves the workspace or the roots shrink, the file tools follow the
new roots, while the memory store stays at the server's data directory.

Windows: case variants, streams and namespaces are covered by pure tests; 8.3 short
names rely on `realpath` — **all unverified on a real Windows machine**.

### Name aliases: what the floor treats as the same name (#243)

The floor compares names with a deny-only key (`path_key` / `_fold`) **and** by file
identity of existing ancestors (`samefile`). The key only matters where nothing exists
yet (a new file, a protected directory not created yet); it is never used to *allow*.
Measured 2026-10-23 on macOS with APFS, case-sensitive APFS and HFS+ disk images
(`tests/test_path_alias_normalization.py`; the HFS+ case is opt-in:
`STIRA_FS_IMAGE_TESTS=1`).

| Spelling (example) | Same name on | Floor |
|---|---|---|
| case (`memory/PROCEDURAL/x`, `soul.md`) | APFS, HFS+, NTFS (default) | refused (casefold) |
| NFC/NFD (`é` / `e`+U+0301) | APFS, HFS+ | refused (NFC) |
| long s / Kelvin sign (`ſOUL.md`, `Kx`) | APFS (measured) | refused (casefold/NFC) |
| dotless i (`ıPD.md`) | not on APFS/HFS+ (measured); possibly volumes that compare upper-cased names (unverified) | refused (mapped to `i`; over-deny elsewhere) |
| U+200C–U+200F, U+202A–U+202E, U+206A–U+206F, U+FEFF (`SO‌UL.md`) | HFS+ (measured) | refused (dropped) |
| trailing dot/space, `:stream` (`SOUL.md.`) | NTFS / Win32 | refused (dropped; Windows: invalid) |
| 8.3 short name (`PROCED~1`) | NTFS | `realpath` expands; on Windows a short-shaped component that still exists after resolving must be a name its directory lists (a real long name such as `NOTES~1` stays usable), else it is an unexpanded alias and refused (`invalid`; a listing error refuses too). An alias of a protected entry is also caught by file identity |
| symlink, hard link, `..`, `/private/tmp` | all | refused (resolve + file identity) |
| full width (`ＳＯＵＬ.md`), other scripts (Cyrillic `о`), U+200B | none measured | allowed (distinct names) |

Unsure volumes count as case-insensitive: on a case-sensitive volume
`memory/PROCEDURAL/` next to a protected `memory/procedural/` is refused too
(over-deny, measured on case-sensitive APFS). Out of scope: shell/MCP code and a
concurrent rename (TOCTOU).

## File tool observations (#307)

What the model gets back, so it can check that what it wrote is what it reads:

- **`file_write` success** (the execution-boundary wrapper, after the upstream tool ran
  and its approval grant was released): a `[Stira file observation] {...}` text block
  **computed from the bytes Stira asked to write** (`content` as UTF-8) — `path`,
  `bytes`, `chars`, `sha256`, `encoding` (`utf-8` / `utf-8-sig` / `null`), `newline`
  (`lf` / `crlf` / `cr` / `mixed` / `none`), `trailing_newline`,
  `source: "requested_bytes"` — and `verified`: the path is still the inode the step
  opened through `open_checked` (same device/inode, regular file, one link, no final
  symlink) and holds exactly these bytes. Otherwise `verified: false` with
  `verify_error` (`target_changed`, `content_differs`, `not_opened`,
  `opened_more_than_once`, `io_error`; `unverifiable_no_nofollow` where the OS has no
  `O_NOFOLLOW` — a final symlink to the renamed original would otherwise pass). The path is **never read back into the
  result**: a target swapped after the write cannot report another file's length or
  digest. Nested wrappers measure once; a refused write is never observed.
- **`file_read` success**: the text first (unchanged), then the same block for the bytes read.
- **`file_read` / `view_image` failure**: the first line names the shown path and a fixed
  reason; a `[Stira file diagnosis] {...}` block has `reason` (`invalid_path`,
  `path_not_found`, `is_directory`, `not_regular_file`, `too_large`, `binary`,
  `permission_denied`, `unsupported_format`, `image_too_large`, `invalid_image`,
  `io_error`, `floor`, `outside_read_roots`, `secret_path`), `resolved_path`, `exists`,
  `is_file`, `is_directory` (as the OS sees the path at diagnosis time; `limit` for
  `too_large`); through the workspace anchoring (#306) also `requested_path` and, for a
  relative path, `base_dir`. A policy refusal — `floor` (#312, `E_READ_FLOOR`),
  `outside_read_roots` (`E_READ_OUTSIDE_ROOTS`) or `secret_path` (`E_READ_SECRET_PATH`,
  #457) — has a `code` and **no** `exists`/`is_file`/`is_directory`, and its
  `resolved_path` is the path as checked (not a symlink target): the answer is the same
  whether the file exists or not. An unexpected exception is shown by its class name only.
  The read roots are not listed in a refusal (see "Reads" below).
- **Write refusals** of `IdentityGuard` (floor, write roots) end with
  `[refused by: <layer>; argument: "<name>"; matched: "<token>"]` — the argument
  (key path such as `edits[0].path`) and the token that triggered the refusal, narrowed
  to the matching token inside a longer command (#304). The write diagnosis (#244) adds
  `layer:<floor|write_roots|os_protection|rule|breaker|hitl|gate>` and
  `argument:<name>` to its evidence; a card shows only known argument names
  (`argument:other` for any other key), never the token (that stays in the masked
  message). A Never-rule refusal gets a note but no permission card (the human's own
  setting).
- **Body arguments** are not read as write targets (#304, #464). One declaration per tool
  (`identity_guard._BODY_ARGUMENTS`): `file_write` / `editor` — `content`, `file_text`,
  `new_str`, `old_str`; `memory_write` / `journal` — `content`; `propose_procedure` —
  `body`, `rationale`; `web_search` — `query` (only sent to the search endpoint, no
  target at all). Kept scanned on purpose: `speak`'s `text` (macOS `say` reads a leading
  `-o<path>` as its output file; a `text` starting with `-` is refused, `[refused by: input; argument: "text"]`), the prompts of tools that start another agent (`think`,
  `use_agent`, `swarm`, `graph`, `delegate`). Only a plain string under
  a declared key is skipped; every other argument, every undeclared tool and every
  command-capable tool is scanned in full. The target arguments stay checked: `path`;
  `layer` / `key` (the memory store keeps the file under `memory/episodic/`); `journal`'s
  `date` must be absent or `YYYY-MM-DD` (`[refused by: input; argument: "date"; ...]`),
  and its `task` is still scanned. A floor refusal on a body-like argument of an
  undeclared tool adds `("<name>" was read as a possible write target: it is not declared
  as body text for this tool)`.

**Shown paths.** Every path or name these blocks, first lines and refusal details show
— and the path in `file_write`'s own success/error text — passes one function,
`identity_paths.shown_text` (`file_tools.shown_path`): a value `mask_secrets` would
change is shown as `[masked]` as a whole, the home directory as `~` (the tools expand it
again), long values are cut.

### What is kept where

| Place | Body | SHA-256 | Paths |
|---|---|---|---|
| Tool result to the model (this turn) | `file_read` text | yes | shown (`~`, `[masked]`) |
| Conversation history, session, checkpoints (`checkpoints/*.json`), context stash — **current spec, open risk** (#294 retention; not readable by file tools except the stash, #312; written only by the host: #271 floor) | yes | yes | shown |
| Turn events (`turns.db`, #188 payload capture) | no — `[file body omitted: N chars, M bytes]` | no | shown, then masked again |
| EventStore (`events.db`), structured logs | no payloads at all | no | no |
| Exceptions in hook metadata | class name only | no | no |

The conversation keeps tool results as it keeps every message (the model needs them).
That checkpoints and history keep the body and the digest in plain text is the
**current spec and an open risk**, not something #307 changed: how long they are kept
is #294; the agent's file tools no longer read them (#312, "Reads" below). Tests open the real `events.db`, `turns.db` and checkpoint
files (`tests/test_file_tool_observations.py`).

**Threat model of the digest.** A SHA-256 of a short or guessable body (a PIN, a
password from a small set, a yes/no value) can be confirmed offline by a dictionary
check. It is returned only where the body itself already is or comes from: in the tool
result of the very call (for `file_read` next to the text, for `file_write` of the
content the model itself wrote). It never reaches a log; it stays in the conversation
history only as long as that turn's result does.

**Reads.** `file_read`/`view_image` resolve the path strictly (symlinks and `..`
physically, like `Path.resolve` on the write side; a missing component — also
`missing/../existing`, which the OS refuses too — is `path_not_found` with
`exists: false`, as before). Only a path that `stat` shows as a regular file is opened
at all (no device or FIFO is ever opened, as with the old `is_file()` check); it is
opened as the *resolved* path with `O_NOFOLLOW` (a final symlink swapped in after
resolving is refused, as in `open_checked`) and `O_NONBLOCK`, and only a descriptor that
`fstat` shows as a regular file is read. Where the OS has no `O_NOFOLLOW`/`O_NONBLOCK`
(Windows) reads keep working as before (no refusal) and a warning is logged at import:
a final symlink swapped in after resolving is then followed. Reads stay inside the
**read roots** (`AgentConfig.read_roots`, default: the workspace only; `E_READ_OUTSIDE_ROOTS`)
and never touch the **secret-path floor** (`~/.ssh`, `~/.aws`, `.env`, `*.pem`, `id_*`,
keychains, browser profiles, `/etc/shadow` …; `E_READ_SECRET_PATH`, also with a root of
`/`) — #457, ADR 0010 addendum; each refusal is one `audit.read.denied` row (tool, keyed
path hash, code). The data directory is on the **read floor** (#312, ADR 0010 addendum): with the
server's policy, `file_read`, `view_image`, `image_reader`, `editor` `view` and
`lsp_*` read only `workspace/`, the episodic layer and `context-stash/` there — the same
tree and names as the write floor (`WritePathPolicy.check_read`). Everything else
(`../tool-config.json`, approvals, checkpoints, `neural-state.json`, `turns.db`, new or
missing paths, case/Unicode variants, symlinks and hard links into it) is refused with
`floor` before anything is opened; after the open the descriptor's own
`(st_dev, st_ino)` must not be a protected file's (`floor_identities()`, a walk that
fails refuses), and the name is checked again (`file_tools.open_for_read`, active only
while a wrapped tool step runs). The editor lists directories by descriptor. No approval
lifts it; the memory tools and `retrieve_offloaded_content` read through their own
stores and are unchanged.

## Measured matrix

`filtered` = not offered to the model; `blocked:hitl` = CRITICAL HALT with no human
(callback absent → reject); `blocked:breaker` = denied by #5.

| Path | Case | NORMAL | WARNING | CRITICAL (halt, no human) | CRITICAL (halt, approved) | CRITICAL (readonly) |
|---|---|---|---|---|---|---|
| standard `file_write` | ordinary path | executed | filtered | blocked:hitl | executed | filtered |
| standard `file_write` | `SOUL.md` | blocked:identity | – | – | blocked:identity (HITL not asked since #243) | – |
| standard `file_write` | `memory/procedural/…` | blocked:identity (#6) | – | – | – | – |
| MCP unknown (`put_object`) | ordinary path | executed | filtered | blocked:hitl | executed | – |
| MCP unknown | `SOUL.md` | blocked:identity (**fixed in #145**, was executed) | – | – | – | – |
| MCP declared read | ordinary path | – | – | executed | – | – |
| delegation child (`file_write`) | parent NORMAL / child NORMAL | executed | – | – | – | – |
| delegation child | `SOUL.md` | blocked:identity | – | – | – | – |
| delegation child | child WARNING | – | blocked:breaker | – | – | – |
| delegation child | child CRITICAL | – | – | child halted | – | – |
| delegation tool | parent CRITICAL | – | – | blocked:hitl | – | – |
| write-shaped, unregistered (`notes_append`) | | executed | blocked:breaker (**fixed in #145**, was executed) | blocked:breaker | blocked:breaker, HITL not asked | – |
| unclassified (`do_thing`) | | executed | executed | **executed** | executed | – |

## Procedural layer (#144)

Procedural entries are put into the system prompt (static part, "Procedural Rules"), so a
rule may only come from a human approval. Write paths into `memory/procedural/`, measured
in `tests/test_procedural_write_gate.py` and `tests/test_procedural_review.py`:

| Path | Who | Decision |
|---|---|---|
| `memory_write` (layer `procedural`, also `staging/…`) | agent | denied (#7, episodic only) |
| `file_write` / `editor` into `procedural/` or `procedural/staging/` | agent | denied at every level, also HITL-approved (#5/#6 floor: `protected_dirs` + data-directory floor #271) |
| a tool named `procedural_approve` / `approve_procedure` / `procedural_reject` / `staging_approve` | agent | no such tool (unknown tool → error result) |
| `propose_procedure(key, body, rationale)` (#463) | agent | writes `procedural/staging/<key>` **only** (one lowercase component; `staging/…`, `../…` refused). `source_turn` is the server's turn id (the tool has no such argument; extra arguments such as `approved_by` / `review` / `source_turn` are dropped — measured). Refused, nothing written: `mask_secrets` would change the key, body or rationale; body > 4000 / rationale > 1000 characters; > 2 per turn, > 10 per hour (per process), > 20 pending. A key that holds an approved rule is staged as a revision (`revises_sha256`). Rule: Ask (fresh install) or Never — never Always; YOLO never answers it (`tiers.PERSON_TOOLS`); hidden at WARNING with the other writes. `tests/test_procedural_write_gate.py::test_463_agent_paths_stop_at_staging` |
| review screen `POST /api/memory/procedural/proposals/<key>/approve` (#463) | operator | the second path that writes provenance (same `ProceduralReview.approve` as the CLI, approver = the server's OS account `local:<account>` exactly like the CLI). CSRF, `confirm: true`, the shown SHA-256; refused while a turn holds the turn lock (the agent's tools only run inside a turn — measured from inside a model call) or an agent tool call runs (YOLO's tracker), and unless the breaker is **NORMAL** (409 `E_BREAKER_NOT_NORMAL`, checked under the turn lock before anything is read; audited as `refused` with the code only — `tests/test_procedural_approve_breaker.py`). A revision replaces the rule only with `replace: true` and only while the rule is the one proposed against |
| review screen `…/reject` (#463) | operator | allowed at every breaker level (it only narrows); deletes the proposal; the (masked) reason, the reviewer and the hash go to `procedural-reviews.jsonl` in the data directory (on the agent's write floor) |
| MCP write tool, literal absolute path into `procedural/` | agent (only if MCP is registered) | denied by #5 (argument scan) |
| MCP write tool, server-relative key (`procedural/x.md`) | agent (only if MCP is registered) | **not checked**: Stira cannot know where the MCP server writes; no execution-boundary wrapper for MCP |
| `shell`, literal absolute path | agent (only with `STIRA_ENABLE_EXEC_TOOLS=1`) | denied by #5 |
| `shell`, path relative to the server's cwd (e.g. `.local/stira/memory/…/procedural/x.md`) | agent (only with exec tools) | **not denied** (#5 anchors relative paths at the workspace, the shell resolves them against its cwd; measured: the file is written; strict xfail `test_shell_cwd_relative_write_into_procedural_is_denied`) |
| `shell` running `procedural_cli … approve` with a relative `--base-dir` | agent (only with exec tools) | **not denied** (strict xfail `test_shell_cannot_run_the_procedural_approval_cli`) |
| `procedural_cli propose` → `procedural/staging/<key>` | operator | allowed; not read by the prompt |
| `procedural_cli approve` → `procedural/<key>` | operator | writes provenance (with the review screen's approve, above); never overwrites a rule. **Does not check the breaker**: the level lives in the running server's engine (hysteresis, cooldown, COHERENCE) and is not persisted — `neural-state.json` holds the emotional state only, written at save points, and the CLI may run with the server stopped — so the CLI has no live level to read. Instead its approval never reaches a running prompt: it takes effect at the next start (a rule approved while the server is at WARNING enters the prompt only after a restart; at that start it is read whatever the breaker level is). The CLI is a person at the server's OS account — the same account can edit the files by hand — so a level check there would not be a boundary |
| `MemoryStore.write(PROCEDURAL, …)` / editing files by hand | host code, operator | trusted, but without provenance the entry is not read |

So the approval is a boundary only while exec-class tools are off (the default) and no
MCP server can write the data directory. That is the same assumption as `staging_cli`
(#293) and is not changed here; closing it needs the #167 sandbox (shell) and a
per-server write scope for MCP.

**With exec-class tools enabled (`STIRA_ENABLE_EXEC_TOOLS=1`) the approval boundary is
only the OS user's permissions.** The agent's shell runs as the same OS user as the
server, so it can run `procedural_cli approve` (which then records that user as the
approver) or write the files directly; IdentityGuard only sees literal paths, and the
per-call Ask confirmation is the last check (a human must read the command). This is a
basic limit of running exec tools without a sandbox (#167), not something this CLI can
fix. **If you enable exec tools, run the agent (the server) as a different OS user from
the one who approves, and give that user no write permission on the data directory's
`memory/` tree outside `episodic/`** (see the EC2 guide, #318). The default (exec tools
off) is what the tests measure as a boundary; the two strict xfails above stay until #167.

**Proposal → approval.** `procedural_cli propose KEY --source-turn ID --file PATH` stages
`procedural/staging/KEY` (with `source_turn`, `proposed_at`). `approve KEY` writes
`procedural/KEY` with `source_turn`, `approved_by`, `approved_at`, `review: approved` and
removes the proposal. The rule is created exclusively (complete temporary file, then a
hard link, which fails if anything is at the path): it never overwrites an existing rule,
and of two concurrent approvals of the same proposal exactly one writes (measured with
threads behind a barrier and with two processes); the other reports `E_TARGET_EXISTS`
(or, if it came after, cleans up the proposal with `written: false`). A file system
without hard links fails closed.

The approver is the OS account the CLI runs as, `local:<account>`: on POSIX from the
process UID (`pwd.getpwuid(os.getuid())`); `LOGNAME` / `USER` are not consulted
(measured: `LOGNAME=forged` still records the real account). Windows has no UID; there
the CLI uses `os.getlogin()` (the OS call `GetUserNameW`, not `USERNAME`) — **not
verified on a real Windows machine**. If the account cannot be determined, nothing is
approved (`E_APPROVER_UNAVAILABLE`, exit 1; there is no `local:unknown`). There is no
option or proposal field that sets the approver.

Exit codes: 0 ok; 1 refused, nothing written; 3 written but its source was not removed
(`approve`: the rule is approved and in effect from the next start, the proposal is still
in staging; `stage-legacy`: the proposal exists, the original is still there). Repeating
the same command removes the leftover (`approve` then reports `written: false`).

Content, `source_turn` and metadata that `mask_secrets` would change are refused
(`E_SECRET_IN_CONTENT`; the value is not echoed), at `propose`, `stage-legacy` and
`approve` — the same rule as for scheduled goals (ADR 0011): a rule is in every system
prompt. Only `show` prints content; `list` prints keys, provenance, the SHA-256 and the
length of each entry. `approved_by`, `approved_at`, `review`, `reviewed_at` (and `source_turn` in free-form
metadata) are reserved: `propose` refuses them (`E_RESERVED_KEY`) and `approve` refuses a
staged file that already carries one (`E_FORGED_RESERVED_KEY`), so a hand-placed
"pre-approved" proposal can only be rejected. Provenance is not a signature: whoever
can write `memory/procedural/` directly can also write these fields.

**Reading.** `ContextAssembler` reads a procedural entry only if it is not under
`staging/` and has a non-empty single-line `source_turn` and `approved_by`, a time-zone
aware ISO-8601 `approved_at` and `review: approved`. Skipped entries are logged as
`E_PROCEDURAL_UNAPPROVED` with their number only (no key, no content). Pending proposals
and skipped entries do not take the place of approved rules (the read window grows up to
1024 entries). Beyond that an older approved rule can be left out (measured: one rule
behind 1030 newer unreadable entries); this is logged as `E_PROCEDURAL_WINDOW_LIMIT`
(the window asks for one more entry to tell "all seen" from "more exist"). Reject stale
proposals and stage or remove legacy entries to stay below it. The rendering of the rules
and the tool definitions are unchanged (#254).
Rules are read when the server starts; an approval with `procedural_cli` takes effect at
the next start. An approval on the review screen (#463) also re-reads the static prompt
(SOUL, anchors, Procedural Rules) under the turn lock, so the rule is in the prompt from the
start of the next turn: measured at the provider boundary with prompt caching on
(`tests/test_procedure_proposals_e2e.py`): the cached static system block is byte-identical
on the turns before the approval and on the turns after it, the tool list does not change,
so the approval costs one cache write of the static prefix, like a restart.

**Existing data (migration).** Default: entries written before #144 have no provenance and
are not read (warning above). `procedural_cli list` shows them under `unapproved` (key and
the first missing field, hash and length, no content); `procedural_cli stage-legacy KEY`
moves one back to staging (`source_turn: legacy-pre-144`, `legacy: true`). Only entries
with none of this pipeline's fields qualify — code before #144 wrote none of
`source_turn`, `approved_*`, `review`, `reviewed_at`, `proposed_at`, `legacy` — so an
invalid entry written after #144 (e.g. a hand-made `approved_by`) is refused
(`E_NOT_LEGACY`) instead of being normalized; inspect and remove it by hand. The file's
modification time is not used (it can be set freely). A staged legacy entry is a proposal
like any other: **read it with `show KEY` before `approve KEY`**. There is no bulk
migration on purpose: each rule is looked at by a human once.

## Unprotected paths (open)

- **Unclassified tools run at CRITICAL.** A tool with no registry entry and no
  write-shaped name is treated as read at every level, and its arguments are not
  path-checked (`test_unclassified_tool_is_not_executed_at_critical`, strict xfail).
  Fail-closed needs a read allowlist first: the chat server's `memory_read`/`memory_search`/
  `memory_list` are not in `READ_TOOLS`. Policy decision pending.
- **`think` (and other inner-agent tools) bypass the hooks.** `strands_tools.think`
  re-runs the parent's tools in a bare `Agent` without IdentityGuard/HITL, so a
  write-shaped tool could modify `SOUL.md` from inside it (measured). Only the
  execution-boundary wrapper (#6) still holds. Since #145 `think` is exec-class
  (hidden unless `STIRA_ENABLE_EXEC_TOOLS=1`), like `use_agent`/`swarm`/`graph`. Since
  #362 an exec tool called from such an inner agent is refused too (no gate mark), and an
  exec call runs only inside a human approval of that exact call (ADR 0010 addendum #362).
  Since #457 the program an approved `shell` call starts gets an allowlisted environment
  (`tools/child_env.py`: `PATH`, `HOME`, locale, `TERM`, `TMPDIR` …), never the server's
  keys and tokens (`AWS_*`, `*_API_KEY`, `*_TOKEN`); measured in
  `tests/test_shell_child_env.py` with `env` inside an approved call.
- **The `delegate` child is the gated alternative (#486, ADR 0016 phase 1).** Its child
  is an `NXPSubagent` with its own `CheckpointHooks` / `IdentityGuard` / binding verifier
  and breaker; measured in `tests/test_delegate_tool.py`: an inner tool whose rule is
  Ask / Never is not given and, when the rule changes during the child, is refused at
  the call (the parent's live reader); a write to SOUL is refused; the parent's YOLO
  approver is never asked; a parent breaker below NORMAL refuses the child's writes;
  depth 2 / a second concurrent child / the caps end with a reason code; stop, server
  stop and the end of the window stop it (no thread left); the child's tokens are in
  the parent turn's usage. **Residual:** the child can paste what it may read into its
  summary — bounded by masking (`mask_secrets`, known patterns) and 2,000 characters,
  not by content; the read floor is what protects data files.
- **HITL gate and IdentityGuard classify differently.** #4 uses names only; #5 adds
  the name heuristic (the *path* decision is shared since #243).
- **Write roots only bind structured paths.** They apply to the `path` of
  `file_write`/`editor`; other write tools (MCP, shell when enabled) only get the
  floor scan of their arguments. Arbitrary code needs the #167 Phase 3 sandbox. Write-shaped unregistered tools are therefore denied at
  CRITICAL without an approval prompt (fail-closed, but an over-block).
- **Procedural approval assumes exec tools are off.** With `STIRA_ENABLE_EXEC_TOOLS=1`
  a shell command can write `memory/procedural/` through a cwd-relative path or run
  `procedural_cli approve`; an MCP server with its own root is not path-checked (see
  "Procedural layer" above, #144). The boundary is then the OS user: run the agent as a
  different OS user from the approver (#318), until the #167 sandbox.

## Name-based over-blocking inventory

Every registered write tool must have a reason in
`test_every_registered_write_tool_is_classified` (new tools fail the test until
classified).

| Tool(s) | Reason | Verdict |
|---|---|---|
| `file_write`, `editor`, `journal`, `diagram`, `memory_write`, `memory_delete` | local writes | justified |
| `shell`, `batch`, `cron` | exec | justified (exec class) |
| `use_agent`, `swarm`, `graph`, `think` | tool-using inner agents | justified (exec class) |
| `http_request`, `web_fetch`, `web_search`, `webhook_send`, `oauth_api_call`, `rss`, `speak`, `memory` | network egress / external side effects (ADR 0012's external class with `perplexity_search`: answered by YOLO only in a window of tier external, under §10 — phase 6) | justified (exfiltration) |
| `page_check` | runs the agent's own page in a closed browser, sends it a few input events and writes screenshots under the workspace (ADR 0015) | justified (local write; YOLO: a card in every window) |
| `run_checks` | runs the project's own checks (fixed kinds) on the agent's code in a throwaway copy of a workspace folder (ADR 0019; registered only when gateway `run_checks` is on) | justified (executes code; YOLO: a card in every window) |
| `propose_procedure` | stages `procedural/staging/<key>` for a person's review (#463; bound by the chat server only) | justified (memory write; Ask or Never only; YOLO: a card in every window) |

`memory_rate` (ADR 0018 phase 0; bound by the chat server only, when `memory_autonomy` is on)
is registered in the **read** class, not here: it takes no text, key, path or retention
argument; the harness binds one bounded number per turn to the turn's own episode in
`index.sqlite3` (data-directory floor), and nothing reads it yet. It is refused outside a
NORMAL breaker by the harness itself. Default rule Always; YOLO class internal. Details:
[memory-autonomy.md](memory-autonomy.md).
| `lsp_diagnostics`, `lsp_hover` | read-only analysis (pyright/ruff subprocess, jedi/AST; checked bytes, #315) | **over-block candidate** |

`think` was cited as an over-block in the issue; measurement shows it is not (see
above). The name heuristic (`IdentityGuard._is_write_tool`) also over-blocks pure
functions whose names contain a write verb, e.g. `edit_distance`, `create_summary`,
`remove_stopwords`, `shell_quote`, `move_cursor_hint` (fixed in
`test_name_heuristic_inventory`). This only matters at WARNING/CRITICAL or when an
argument names an identity path.

## Fixes made with these tests

- `IdentityProtectedTool` returned `{"tool_result": …}` as a plain dict; the SDK
  executor treats that as a malformed result in real agent loops. It now yields a
  `ToolResultEvent`, so a denied write is a normal error result.
- IdentityGuard counts live `WRITE_TOOLS` (incl. MCP registered after startup) as
  write-capable, and denies write-capable calls at WARNING.
- `think` is exec-class.

## `page_check` (verification browser, ADR 0015)

Phases 1 and 2, Addendum rev 3 (`type`, `wait_for`, assertions) and rev 4 (phase 3: one named
`localhost` origin) (#449). Registered only when a browser is found (`STIRA_BROWSER_PATH`, known
locations, `PATH`), not on Windows and not as root; `stira doctor` says why it is missing.
Offered only with the workspace write policy (`get_tools(write_policy=…)`); a write tool
(hidden at WARNING and above). New installs: **Ask**; an existing `custom` rule file: Never
until a person turns it on. **Ask or Never only in phase 1** (`tool_policy.NO_ALWAYS_TOOLS`):
the page runs JavaScript the agent wrote, and Always would also run it in turns no person
watches (jobs, webhooks, bots); the settings page offers no Always, the rule API answers
`always_not_allowed`, and a saved Always (or `trust_all`) is read as Ask with one warning at
start-up. **YOLO: a card in every window** (`PERSON_TOOLS`), until the launch guard is
measured at every start (phase 2 did not change the rule or the tier).

| What | How it is held |
|---|---|
| what may be opened | a workspace `.html` / `.htm` / `.svg` file (symlinks followed, read floor checked) — every other URL scheme (`javascript:`, `data:`, `https://`, …), `file://` outside the workspace and other files are refused before a browser starts. **Phase 3 (Addendum rev 4):** `http://localhost:<port>/…` for a port a **person** listed (`tool-config.json` `page_check_ports`, default none) — the host name `localhost` only (`127.0.0.1`, `[::1]`, `*.localhost`, user info → `host_refused`), ports 1024–65535 (`port_refused`), never the dashboard's own port (`own_dashboard`), not listed → `port_not_allowed`. The list is read from the rules at every call; the tool has no argument for it. Without exec tools the person starts the server (on `127.0.0.1`) |
| the localhost origin (phase 3) | the proxy forwards only `http://localhost:<port>/…` (origin-form, `Host` set, `Proxy-*` / `Keep-Alive` dropped, `Connection: close`) and `CONNECT localhost:<port>` (WebSocket) to the literal `127.0.0.1:<port>` — no name resolution, no IPv6 — and refuses (403) every other host and port, loopback included; a redirect leaving the origin is the browser's next request and is refused (a main-frame one is also aborted, `navigations`); a dead port → 502. Measured (Addendum rev 4): a LAN canary (TCP + UDP), another loopback port and a listed port that is not the target's got 0 connections / packets over 4 runs, while same-origin `fetch` and WebSocket worked; with the proxy flags removed they got 13 / 4 / 10 / 9 |
| who adds a port | only `POST /api/tools/page-check-ports` (settings page, CSRF / Origin / Host checked): adding needs the port typed in the confirmation (`confirm`, `confirm_ports`), a NORMAL breaker, no running agent tool call (with YOLO) and, with the YOLO allowance, the host confirmation; one settings-history row (not restorable by a rollback). The tool APIs and the hub keep the recorded ports (not an `as_dict` field); a hand edit that adds one locks the rules (`edited`); a recovery save drops them |
| network | `--proxy-server` to a listener inside Stira that answers 403 and records only `host[:port]`; `--proxy-bypass-list=<-loopback>` (loopback goes there too); WebRTC direct UDP off; host names never resolved locally. Measured in `tests/test_page_check.py`: fetch, img, iframe, prefetch, beacon, WebSocket and a WebRTC STUN request towards a LAN canary and a loopback canary on another port reach nothing (and do reach them with the proxy switches removed) |
| reads | `file://` sub-resources inside the workspace only (CDP `Fetch` interception; refused ones counted as `outside_workspace`, no path returned); a directory is refused (its listing would show file names) |
| frames and workers | auto-attached paused (`waitForDebuggerOnStart`); each session runs only after Chrome **confirmed** its `Fetch.enable` and `Target.setAutoAttach`. An error or no answer within 5 s ends the run and the browser (`status: error`, `reason: browser_guard_unverified`, `guard_failed` names the step) — fail closed. A dedicated worker has no `Fetch` domain (measured); its requests pause in the frame that owns it, so it needs the auto-attach only |
| navigation, popups, downloads, uploads | a document request to the network (navigation away, a network frame) is aborted and counted (`navigations`, host recorded; the page stays where it was, so later steps still act on it); a new page target is closed (counted); downloads denied (counted); file choosers never answered (counted); page loads ≤ 3 (`page_limit`) |
| input actions (phase 2) | `actions`: at most 20 of `click` (CSS selector or `[x, y]`), `key` (+ `hold_ms`), `key_down`, `key_up`, `move`, `wait_ms`, `screenshot`, `text` (CSS selector), checked **before** a browser starts (anything else, extra keys, a point outside 1280×800, an unknown key → `refused` / `invalid_action`; more than 20 → the first 20 run, `action_limit`). No action runs code: selectors are arguments of Stira's fixed page functions (`Runtime.callFunctionOn`, `silent`), never part of a script. Keys: one printable ASCII character or a named key (Enter, Tab, Escape, Backspace, Delete, Space, arrows, Home, End, PageUp, PageDown, Shift) — no Control / Alt / Meta and no modifiers, and only the Windows key code (a native key code made Chrome on macOS run a browser shortcut that opened a `chrome://help` page: measured, closed and counted as a popup). Real input events (`Input.dispatchMouseEvent` / `dispatchKeyEvent`). One `wait_ms` / `hold_ms` ≤ 10 s (`clamped`) |
| `type`, `wait_for`, assertions (Addendum rev 3) | `{"type": "…", "into"?: css}`: 1–200 characters with no control / format character, inserted at once with `Input.insertText` into `into` (focused first) or the focused element, only when it is editable (`not_found`, `not_editable`, `not_focused`, `bad_selector` otherwise; nothing inserted). `{"wait_for": {"selector": css, "visible"?: bool} \| {"text": "…"}, "timeout_ms"?: n}`: polled every 100 ms, default 5 s, cut at 10 s (`clamped`), `code: timeout` when not met, `waited_ms`. `{"expect": {"selector": css, <form>}}` with exactly one of `exists`, `visible`, `text_contains`, `text_equals`, `count`: a fixed page function returns the facts (count, visible, text of the first match), **Stira decides pass / fail** in Python (folded whitespace, case-sensitive; malformed facts or a bad selector never pass). Visible = a non-empty box not wholly left of / above the page, `visibility: visible`, no opacity-0 ancestor, `checkVisibility` where present; occlusion and `overflow` clipping are not checked. Each counts toward the 20 actions; the result has `assertions` (`passed`, `failed`, `not_run`, one `{i, form, pass}` per assertion) in Stira's block; a failed assertion does not change `status`. No page text is added: the probe's text stays in Stira |
| steps (phase 2) | one entry per action: `ok`, a `code` when not ok (`not_found`, `not_visible`, `bad_selector`, `screenshot_limit`, …) or `clamped`, `text_changed` (length + hash of the body text before / after, computed in the page; the text itself is not moved), `new_errors` (page + console errors during the step) |
| page text (phase 2) | `text` (the body after the actions) and `text` steps share **4,096 characters per call** (`output_truncated` when cut). Every string the page controls — title, text, step texts, first messages, missing file names, hosts — reaches the model only in a second content block: a fixed note (data, not instructions) and one `<page-content>` block, each string defanged (NFKC, no invisible characters, every `<` / `>` and look-alike → `&lt;` / `&gt;`, as `delegate`) and secret-masked, so the page can neither close the block nor imitate `<system-reminder>` / `<recalled-memory>`. Stira's JSON (status, codes, counts, steps, screenshot paths, `open_yourself`) is the first block |
| process | a fresh `--user-data-dir` per call, removed afterwards; Chrome's own sandbox on (`--no-sandbox` is never passed); CDP over `--remote-debugging-pipe` (no debugging port); the process group is killed on every exit; 30 s per call (`time_limit`) |
| result | counts and the first three short messages (page errors, console errors, missing files), the page's blocked hosts (≤ 10), the title (≤ 120 chars), up to 4 screenshots per call (`screenshot` steps, then a final one when there is room; `<run>/1.png`, `2.png`, … under `.stira-verify/`, last 5 runs kept, written through the write policy; read them with `view_image`; over the limit → `screenshot_limit`). No JavaScript evaluation by the agent. `status` is `ok` / `truncated` / `refused` / `error` with one `reason` (`time_limit`, `page_limit`, `output_truncated`, `screenshot_*`, `browser_guard_unverified`, `browser_error`, a refusal code), so a limit is never mistaken for a broken page |
| logs | the input keeps only `target` as `@workspace/<workspace-relative path>` or, phase 3, `@localhost:<port>/<path>` (never the query, the fragment or user info) (or `refused:<code>`: outside, a URL scheme, invalid), `wait_ms` and the actions as a count per kind (`{"count": 3, "kinds": {"click": 2, "invalid": 1}}`; never a selector, key, point, typed or expected text, count or timeout) — never an absolute path, a home directory or a user name; the output keeps the same `@workspace/` path of the opened page, the counts, the codes, the number of screenshots, the step counts (`count`, `failed`, `text_changed`) and the assertion counts (`passed`, `failed`, `not_run`; no per-assertion row) — no title, text, messages, hosts, other file names or screenshot path; the `<page-content>` block is not kept at all |

**Limit:** this closes the network of the browser Stira starts. A program of the same OS
user, or a browser exploit by the page, is out of scope (as everywhere in Stira). WebRTC over
TCP goes to the proxy like every other connection; it was not measured separately. The
browser's own background requests (component updates, …) also reach the proxy and are
refused; they are counted in `network.proxy_refused`, not listed as the page's hosts.
Measured on macOS only (Chrome 155); Linux (and the process-group end of its zygote /
sandbox helpers) is not measured yet. Between the `file://` check and Chrome's read a
same-user program could swap a symlink, and a hard link to a file outside the workspace is
not detectable by path; both need a same-user writer (out of scope, as ADR 0004).


## `run_checks` (verification runner, ADR 0019)

Phases 0 and 1 (#167, commander decisions 2026-10-21). **Off by default**: registered only
when `gateway-config.json` says `run_checks: {"phase": 1}` (absent = nothing registered,
nothing else changes; `STIRA_RUN_CHECKS=0` forces it off), not on Windows and not as root.
Offered only with the workspace write policy; a write tool (hidden at WARNING and above).
New installs with the feature on: **Ask**; an existing `custom` rule file: Never until a
person turns it on. **YOLO: a card in every window** while the rule is Ask (`PERSON_TOOLS`,
ADR 0019 Q2; answering it inside a window is a later decision, phase 1b).

**Always (Q3):** a person may save Always. It is **effective only while this host's
containment is verified `os_sandbox`** (the macOS profile's self-test passed, and it is
confirmed again before every run). On a `limits_only` host a saved Always is read as Ask:
the gate asks, the settings page shows the saved Always with the reason
`rule_always_capped`, the card's plan says `"always": "capped:limits_only"` and
`asks_because`, and `stira doctor` says so. After three runs in one turn failing on the same
ids, a saved Always is read as Ask for the rest of the turn (`repeated_failure`).

| What | How it is held |
|---|---|
| what runs | fixed kinds only: `pytest`, `unittest`, `node_test`, `ruff`, `compileall` (`auto` = every kind that applies; pytest preferred over unittest). The harness discovers what applies from files (no process: `stira checks discover <dir>` lists the kinds and argv) and **builds every argv**; the model chooses kinds, a workspace folder and optional targets under it (never starting with `-`; pytest `file::test`). No binary, flag or shell string from the model; `PYTEST_ADDOPTS`, `NODE_OPTIONS` and every variable outside the allow-list are absent |
| interpreter | `<folder>/.venv`, `<workspace>/.venv`, then Stira's own interpreter, then `python3` on `/usr/bin:/bin`; a venv used as the interpreter runs from where it is (read-only under the profile) and is not copied |
| card and binding | before the gate, the harness puts its `plan` into the call's input (`CheckpointHooks.input_preparers`): the resolved argv of every check (paths as `@workspace/…` / `~/…`), the folder, the containment mode, the limits, whether Always is effective and a `binding` digest. The card shows it and the approval's argument hash binds it (#146). At run time the plan is rebuilt; a different one refuses the call (`binding_changed`; a call without a plan: `binding_missing`) |
| where it runs | a **throwaway copy** of the folder in a temp directory (APFS clone on macOS); `.git`, caches, read-floor paths and symlinks that leave the folder are not copied; more than 20,000 files or 1 GB → `workspace_too_large`. The real tree is hashed (stat only) before and after: `real_tree_changed`. Files the run added / changed / removed in the copy are listed (≤ 20), never applied. No in-place mode (Q4) |
| environment | allow-list: `PATH` (fixed + the interpreter / `node` / `ruff` directories), `LANG`, `LC_ALL`, `TZ=UTC`, `HOME` / `TMPDIR` / `XDG_*` in the run's temp directory, `PYTHONDONTWRITEBYTECODE`, `PYTHONNOUSERSITE`, `CI=1`, proxy variables to Stira's refusing proxy (403, records `host[:port]`), `NO_PROXY` empty. No `AWS_*`, tokens, `SSH_AUTH_SOCK`, `GIT_*` |
| macOS (`os_sandbox`) | a generated `sandbox-exec` profile per run: network out only to the refusing proxy and Unix sockets, the resolver socket and `com.apple.dnssd.service` denied (no DNS), writes only under the run's temp directory and `/dev`, no reads of file data under the home directory except the run's directory and the interpreters' prefixes. A self-test under the same profile (loopback and LAN TCP, LAN UDP, a write outside, a home read, a name lookup; checked from outside too: canaries and the outside file) must fail every probe, at first use and before every run; a failed confirmation refuses the call (`containment_unverified`) and drops the host to `limits_only` |
| Linux / no profile (`limits_only`) | limits and the scrubbed environment only: **the network, DNS, home reads and writes outside the copy are not held** (`not_held` in every card and result). A write into the real tree by absolute path is detected (`real_tree_changed`), not prevented |
| limits | 5–300 s per check (`time_limit`), 300 s per call, `RLIMIT_CPU` = wall time (`cpu_limit`), `RLIMIT_NPROC` = the user's processes + 128 (per user, racy), `RLIMIT_FSIZE` 256 MB (`disk_limit`; Python sees EFBIG), the temp directory ≤ 1 GB (1 s watchdog), 1024 open files, no core files, at most 4 checks per call and 10 calls per turn (`call_limit`). Output: first + last 64 KB kept, the group killed at 64 MB (`output_limit`). **Memory: `RLIMIT_AS` 4 GB on Linux; not limited on macOS** (`setrlimit` refuses it; the limits say `not_limited_on_macos`) |
| processes | a sentinel leads the run's process group; the group is killed (`SIGKILL`) on every exit path, then the temp directory is removed. **Residual (measured):** a child that calls `setsid` survives; under the profile it still cannot reach the network or write outside (`tests/test_run_checks_containment.py`) |
| result | `status` `passed` / `failed` / `error` / `refused` / `limited` with one `reason`; per check the exit code, counts (JUnit for pytest / node, the summary for unittest, JSON for ruff, error lines for compileall), `consistent` (counts and exit code agree; if not, the exit code decides), the first 5 failures (≤ 800 characters), `output_tail` ≤ 2 KB. Every string from the program is defanged (NFKC, no invisible characters, `<` / `>` escaped) and secret-masked, and the whole result is one `<check-output>` block after a fixed note that it is data, not instructions |
| audit and logs | one `audit.checks.<status>` row per call: `kinds`, integer `counts`, `containment`, `reason_code`, `tree_changes` (a number), `real_tree_changed`, and `target` = `workspace:<keyed hash>` of the folder — never test ids, messages, output, file names or argv. The observation log keeps the input's kinds, `@workspace/<folder>` and the number of targets (never the plan), and the output's status, reason, containment and counts |

**Measured** (macOS 26, `tests/test_run_checks_containment.py`, the ADR's pre-registered
fixture set): passing / failing / collection error / hanging / CPU spin / fork 400 / output
flood / disk fill / network (urllib, raw TCP loopback and LAN, UDP, DNS) / credentials
(environment, a home file, `~/.ssh`, `~/.aws`, the home listing) / workspace escape
(absolute write, the real tree by absolute path, `..`, a `sys.path` entry, symlinks in the
copy) / forged JUnit / injected instruction — 100 % of the required outcomes on
`os_sandbox`, and each boundary has a mutation test that fails when it is switched off.
**Not measured:** memory on macOS (not limited), the keychain (needs a real item), Linux
limits and `PR_SET_CHILD_SUBREAPER`, ICMP / WebRTC-like channels, editable installs pointing
outside the copy (their imports fail under the profile: home reads are refused).
