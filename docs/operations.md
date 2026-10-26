# Operations runbook

English | [日本語](operations_ja.md)

How to run the chat server (`dashboard/stira_server.py`) day to day and how to get
out of the fail-closed states. Settings, environment variables and the data-directory
layout: [configuration.md](configuration.md). Every procedure below keeps the defaults
and never weakens a gate; when in doubt, the safe outcome is "nothing runs until a person
decides again".

## 1. Start, stop, status

```bash
cd <repository>                      # configuration is read relative to the working directory
scripts/stira_ctl.sh start         # status | stop | restart | distill  (Windows: stira_ctl.ps1)
curl -s http://127.0.0.1:5555/api/gateway/status
```

`STIRA_PORT` changes the port; `start` refuses a port that is already in use. **One server
per data directory** is enforced (#285): the server takes an OS lock on
`.local/stira/server.lock` before it opens anything there, and a second
`stira_server` on the same directory exits with code 3 and
`Stira: another server (pid N) owns the data directory …; not starting` (any port). A
lock left by a crashed or killed server is released by the kernel, so the next start
takes it. `stira_ctl.sh` follows the same owner: `start` refuses while the owner is
alive (also when the PID file is gone) and reports a start the server refused instead of
leaving a dead PID behind; `status` names the owner when nothing answers on `STIRA_PORT`;
`stop` falls back to the owner when the PID file is missing.

The lock file is opened without following a symlink (`O_NOFOLLOW`) and must be a
regular file with one link, owned by the user running the server; otherwise the
server refuses to start (exit code 3, `… lock file … is unsafe (…)`) and writes nothing
to it. Remove such a file while no server runs. **Windows has no `O_NOFOLLOW`**: there a
symlink is followed and only the regular-file/one-link checks apply. The server is
still allowed to start on Windows (refusing it would make the server unusable there);
symlink protection and the owner check are unverified on Windows and to be revisited
once it has been tested on a Windows machine. `heartbeat.lock` and `scheduler.lock` use
the same implementation (`stiracore.owner_lock`, #322): the same checks, nothing
written to a refused file, the same Windows limitation. A refused one does not stop the
server: the heartbeat or the scheduler stays off (`Heartbeat: ⚠️ heartbeat.lock is
unsafe (…); not started`, status `last_error: UnsafeLockFileError`). Not covered by the
locks: anyone who can replace the data directory itself (the lock files' parent) can make
a second server lock another file — keeping `.local/stira/` writable only by the
operator is the operator's job (host guidance, #318).

`stira_ctl.sh` treats a PID as the owner only if it is a chat server process of this
checkout **and** has `server.lock` open (`lsof`); the PID note and the command line alone
can be stale or reused. Having the file open is not the kernel lock itself (a server that
is being refused has it open for a moment). When ownership cannot be shown — no `lsof`,
no `server.lock` (e.g. a server older than #285), or a lock file that is a symlink or not
a regular file — `status`/`start` do not trust the note and **`stop` refuses** and says
why. `scripts/stira_ctl.sh stop --force-unverified` is the separate, explicit pre-#285
behaviour (PID file + command line only); `restart` never uses it.

Start-up and stop are not atomic against `SIGTERM` at every instruction: a resource is
registered for the stop right after it is created (lock, stores) or right before it is
started (heartbeat, scheduler); a signal landing in between relies on that resource's
own error path and on process exit.

`stop` sends `SIGTERM`. The server turns it into a normal exit and runs the bounded
stops first (approval broker closed, scheduler and heartbeat stopped; at most 4 s each and
9 s in total; once, however often it is asked), then prints `StiraCore: stopped (approvals: closed, scheduler: …, heartbeat: …)` to
`.local/stira.out.log`. The same happens for a `SIGTERM` during start-up: whatever exists
by then is stopped, and the stores and the lock are released when that finished cleanly.
A turn waiting on an approval is rejected, but the request stays
pending in the store and is offered as restored after the next start (#240). After 10 s
`stop` falls back to `SIGKILL` (as before; nothing above runs then, the restart recovers).
On Windows `stira_ctl.ps1` stops the process tree with `taskkill /F`: the bounded stops
do not run there (unverified otherwise; the restart recovers in the same way).

Stop the server before moving, deleting or restoring anything in `.local/stira/`.

`/api/gateway/status` (read-only) shows the breaker, the provider and whether its
credentials are configured (names only), `heartbeat`, `prompt_cache`,
`context_management` and `diagnostics` (doctor summary, open permission cards).

## 2. Opt-in features

All are off by default and read at start-up (restart after changing them).

| Feature | Turn on | Check | Doc |
|---|---|---|---|
| Heartbeat tick (model-free) | `"hb_enabled": true` or `STIRA_HEARTBEAT=1` | `/api/gateway/status` → `heartbeat.running` | [heartbeat-daemon.md](heartbeat-daemon.md) |
| Scheduler | `STIRA_SCHEDULER=1` | `GET /api/scheduler`, `/schedules` | [scheduler.md](scheduler.md) |
| Prompt cache (Bedrock Claude) | `"prompt_cache": true` or `STIRA_PROMPT_CACHE=1` | `/api/gateway/status` → `prompt_cache.active` | [prompt-cache.md](prompt-cache.md) |
| Context management | `"context_management": true` or `STIRA_CONTEXT_MANAGEMENT=1` | `/api/gateway/status` → `context_management.mode` | [context-management.md](context-management.md) |
| Exec-class tools | `STIRA_ENABLE_EXEC_TOOLS=1` (every call still asks) | `/settings#sec-tools` | [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| Stage page (read-only view; approvals via the existing API) | `STIRA_STAGE=1` | `/stage/` (a "Stage" nav item) | [stage.md](stage.md) |

`0` (or any other non-empty value) in `STIRA_HEARTBEAT`, `STIRA_PROMPT_CACHE` or
`STIRA_CONTEXT_MANAGEMENT` turns a configured `true` off.

None of these distills, reviews or forgets memory. Memory maintenance is a manual command
that you may run from an OS timer: [memory-maintenance.md](memory-maintenance.md).

## 3. `stira doctor`

```bash
stira doctor                 # or: python -m stiracore.diagnostics doctor
stira doctor --json
stira doctor --root ~/work --no-write-test
```

Checks every write root of `.local/stira/agent-config.json` (or `--config`) for lock
flags, SIP, read-only mounts, ACL deny entries, quarantine and cloud-provider attributes,
then creates, renames and removes one temporary file through a directory descriptor (a
symlink on the path means the write test is skipped). TCC locations are only warned about
and never read. The doctor changes nothing; fixing a permission is always a person's job.
With the default config (`.local/stira/agent-config.json`) and no `workspace_dir`,
`@workspace` is `.local/stira/workspace/` and the data directory is on the floor, as in
the server (#271): a root inside it is reported `floor` and not write-tested.
Exit status: 0 no errors (warnings allowed), 1 errors, 2 bad config (unreadable file, or the write
policy cannot be built). It also warns when emotion classification runs on the rule-based
fallback (Sokudan not installed: `pip install -e ".[jev]"`). The settings page has a read-only
Diagnostics section that shows the report. The server runs it at start-up; re-run
from the chat sidebar or `POST /api/diagnostics/doctor`. Details:
[diagnostics.md](diagnostics.md).

## 4. Tool rules are locked

**Symptom:** every tool is Never; the settings page says the rules are locked
(`GET /api/tools/list` → `tools_locked` names the reason, e.g. `head_unreadable`, an older
generation, a digest mismatch).

**Cause:** `tool-config.json` and `tool-config.json.head` disagree (hand edit, an older
copy, a deleted file, an unreadable history), the rules were locked after a restore
(`restored_backup`, §8), or the opt-in anchor saw an older or different generation
(`anchor_*`, §8). The lock latches: putting the old files
back or deleting `tool-config.json.locked` does **not** unlock.

**Recovery:**

1. Find out why the files changed (who edited the data directory?).
2. On `/settings#sec-tools`, set the rules you want and save; the page asks for the
   recovery word `RECOVER`. API: `POST /api/tools/config` with the rules,
   `"confirm": true` and `"recover": "RECOVER"` (an empty save or one without the word is
   refused with 409 `tool_rules_locked`).
3. A recovery counts as loosening: it needs the breaker at NORMAL, the confirmation, the
   typed names of write/external tools set to Always, and a successful history entry
   (`recovered_lock`).

Saving any rule change revokes every open approval (they were asked under another
generation). Details: [ADR 0010 §3–§6](adr/0010-tool-policy-gate-stack.md).

### Tool rules cannot be saved: `secret_command_saved` (#270)

The saved shell allowlist holds a command that looks like it contains a secret (written by
an install from before the check). It is never written again: every save of the tool rules
is refused (400 `secret_command_saved`, with the count, never the value) until it is
removed. On `/settings#sec-tools` the entry is shown masked and marked; "Remove
secret-looking commands" (works while `shell` is off) and then Save. Other groups
(classifier, breaker, write roots) still save. Rotate the secret: it was on disk.

## 5. Approvals across a restart

- A pending approval survives a restart (`hitl-approvals.db`); approving it on
  `/approvals` resumes the paused tool batch, re-checked against the live rule, breaker
  and floor. A missing or newer checkpoint, a binding mismatch or a busy agent refuses the
  resume and cancels the approval.
- If the server stopped between writing the head and committing (or rows were deleted
  from the end), the store **cancels every unfinished request** at the next start ("a fresh
  human decision is required"). Ask again; nothing comes back usable on its own.
- No environment variable approves anything (`STIRA_YOLO_ALLOW` only offers the YOLO button; a window approves only after a person confirmed it — [yolo-quickstart.md](yolo-quickstart.md)).

### Approval store unavailable

**Symptom:** approval cards and decisions answer 503 `store_unavailable`; every new
approval request is rejected (fail-closed). `GET /api/hitl/pending` → `store` shows the
error.

**Recovery:** stop the server, keep the files for inspection, then move **all four**
aside together — `hitl-approvals.db`, `hitl-approvals.db-wal`, `hitl-approvals.db-shm`,
`hitl-approvals.db.head` — and start again. The new store is empty: every pending
approval is gone and has to be asked again. Never delete or edit the head alone (that
keeps the store unhealthy). Keep `hitl-operations.db` (the ledger) as it is. Opening a
store in Python to inspect it (`ApprovalStore(path).verify_chain()`) can itself record a
recovery; do it on a copy. Details: [ADR 0006](adr/0006-hitl-persistence.md).

## 6. Scheduler

- Jobs exist only when a person created them through the API with `"confirm": true`;
  they may use read-only tools only, and on the chat server the `health_check` and
  `memory_harvest` actions are registered (`memory_harvest`: rule distiller only, staging
  only, [memory-maintenance](memory-maintenance.md) pattern C).
- **After a recovery** (the server stopped between head and append, or the last line was
  deleted) every job is paused by `system`. Check the jobs on `/schedules` and resume the
  ones you want (`POST /api/scheduler/jobs/<id>/resume` with `"confirm": true`). Resume is
  refused at WARNING and above.
- **Log unavailable** (`GET /api/scheduler` → `status.error: log_unavailable`; the log failed
  verification): nothing runs and every change answers 503. Stop the server, keep the
  files for inspection, move `scheduler-events.jsonl` **and** `scheduler-events.jsonl.head`
  aside together and start again: the scheduler starts without jobs. Moving only one of
  them keeps it unavailable.

Details: [ADR 0008](adr/0008-scheduler.md).

## 7. EventStore (`events.db`)

The hash-chained observability store derives no permission; a broken chain only stops
recording (turns keep working). It is not reported on `/api/gateway/status`; check it on a
copy with `SQLiteEventStore(path).verify_chain()` / `chain_status()`. To start a fresh
chain: stop the server and move `events.db`, `events.db-wal`, `events.db-shm` and
`events.db.head` aside together. A recovery event (`chain.recovered`) after a crash is
expected and records that the last transaction may be missing. Details:
[ADR 0009](adr/0009-eventstore-chain.md).

**Where turn events live.** On the chat server, `turns.db` (written by `TurnEventRecorder`) is the
one source of truth for turn events: turns, tool calls, approvals and breaker transitions.
`/chat`, `/api/turns`, `/api/turns/<id>/events`, the Stage and `/observability` (with
`/api/observability/events` and `/api/observability/snapshot`) all read it, so the counts
agree. The hash-chained `events.db` is not a copy of them: the chat server writes only its
audit records there (`/new`, #350). The generic `create_sse_blueprint` streams such a chained
store for an app that writes one; no app here uses it (the chat server reads `turns.db`; the observer
dashboard serves no stream). `/observability` on the chat server shows
the tokens of each turn and the model they belong to; model rates are not configured, so the
cost reads as unpriced (marked `*`), never as free. The observer dashboard
(`dashboard/app.py`) serves no event stream; its `/observability` says so and does not
connect (#278).

## 8. Backup and restore

**Backup:** stop the server (so SQLite has no open WAL), then copy the whole
`.local/stira/` directory, including every `.head`, `-wal`, `-shm` and `.locked` file,
and the configuration files if you run from another directory. The copy contains private
conversation data (checkpoints, turns, memory, stash): store it like the original.

**Restore is a rollback of permissions.** A consistent older copy (file, head and history
from the same moment) passes every integrity check inside the data directory, so older —
possibly looser — tool rules would come back silently, and any marker kept in the data
directory goes back with them
(#273). The restore procedure
therefore makes a person approve the rules again before anything runs. Restore like this:

1. Stop the server.
2. Restore `tool-config.json`, `tool-config.json.head`, `tool-config.json.locked` (if any)
   and `settings-history.jsonl` together, or none of them.
3. Do **not** restore the approval store: move the current `hitl-approvals.db*` and its
   `.head` aside as in §5, so nothing pending or approved comes back.
4. Do **not** roll back `hitl-operations.db`: keep the current ledger. Measured
   ([ADR 0006](adr/0006-hitl-persistence.md#addendum-273-a-ledger-restored-from-a-backup)):
   with an older ledger, replaying a paused batch asks again for a call that already ran,
   and approving that card runs the side effect a second time.
5. Restore memory (`memory/`), `neural-state.json` and the rest as needed; `events.db`
   and `scheduler-events.jsonl` only together with their `.head` (and, for the scheduler,
   check every job afterwards).
6. **Lock the restored rules** (from the directory the server starts in):

   ```bash
   stira tool-rules lock          # or: python -m stiracore.diagnostics tool-rules lock
   stira tool-rules status        # exit 1 = locked (expected here); --file <tool-config.json>
   ```

   This writes `tool-config.json.locked` (`restored_backup`): every tool is Never until a
   person saves the rules with the recovery word (§4). It changes no rule value.
7. Start the server, set the rules you want on `/settings#sec-tools` and save with
   `RECOVER` (§4) before the first turn.

**Optional: an anchor outside the data directory.** With
`STIRA_TOOL_RULES_ANCHOR_DIR=<directory>` in the service environment (not in a file inside
the data directory), the highest rule generation and its digest are also kept there, and
these lock on their own, even if step 6 is forgotten:

| Reason | When |
|---|---|
| `anchor_rollback` | the rules are an older generation than the anchor, or the data directory was reset |
| `anchor_mismatch` | same generation, other rules |
| `anchor_foreign_install` | the rules and head were written by another install (or another path, or before `init-anchor`): their install ID is not the anchor's, whatever the generation |
| `anchor_generation_jump` | the rules are more than one generation newer than the anchor |
| `anchor_unconfirmed_generation` | one generation newer, but looser than the rules the anchor recorded (or the anchor has no snapshot or no install ID) |
| `anchor_missing` | the variable is set but there is no anchor (never created, or deleted) |
| `anchor_unsafe` | the anchor is a symlink, has a second hard link or another owner |
| `anchor_unreadable` | the anchor is not valid |
| `anchor_misconfigured` | the directory is inside the data directory (also through a symlink) |
| `anchor_unwritable` | a needed anchor update failed |

A save whose anchor update failed (500 `persist_failed`) leaves the rules one generation
ahead: the next load completes it only if it loosens nothing (the same loosening check as
every save); a loosening one stays locked until `RECOVER`. Next to each anchor is its lock
file (`<anchor>.lock`): every process takes it before changing the anchor, so concurrent
writers never move it back. When it cannot be taken within 5 s, a save fails, and a load
that has to move the anchor up (one generation ahead) locks (`anchor_unwritable`); a load
whose rules already match the anchor does not take it. An anchor found missing or
unusable under the lock is never written again by a load or a normal save (they lock with
`anchor_missing` / `anchor_unreadable` / `anchor_unsafe`); only `init-anchor` and a
`RECOVER` save write it.

A load **never creates** an anchor. Turning it on (a person, once):

```bash
# server stopped; the rules exist and are not locked (start once without the variable
# on a brand-new install)
export STIRA_TOOL_RULES_ANCHOR_DIR="$HOME/.config/stira/anchor"   # outside the data dir
stira tool-rules init-anchor [--data-dir <data directory>]       # refuses if inside it
stira tool-rules status                                          # anchor generation: N
```

Then set the same variable in the server's environment and start it. `init-anchor` refuses
an existing anchor, locked rules and a directory inside the data directory. After an
anchor went missing (or with the variable set before `init-anchor`), the rules are locked
and **only a `RECOVER` save** (§4) writes the anchor again. A save whose anchor cannot be
written fails (500 `persist_failed`; nothing is replaced when the directory is not
writable) and a lock stays. Choose a directory the data directory's backups do not
contain and that the server's OS user can write. Restoring the anchor from the same backup
removes the protection: it guards against restores, not against someone who can also write
the anchor. Off by default.

## 9. What not to do

- Do not edit `tool-config.json`, a `.head` file or a database by hand to "fix" a lock —
  it makes the lock permanent or the store unhealthy.
- Do not set `STIRA_ALLOW_REMOTE=1` without authentication and TLS in front. The
  server cannot check them: it only logs a WARNING and serves the `Host` names listed in
  `STIRA_ALLOWED_HOSTS` (see [admin-api-permissions.md](admin-api-permissions.md)).
- Do not try to give the agent write access to the data directory: since
  #271 it is on the write
  floor except `workspace/`, `memory/agents/stira/memory/episodic/` and
  `context-stash/`, and a write root or an approval does not change that (by design).

## 10. Upgrading past #271 (agent workspace moved)

Before #271 the default `@workspace` was `.local/stira/` itself. It now is
`.local/stira/workspace/` (created at start-up), and the rest of the data directory is
read-only for `file_write` / `editor`. Nothing is moved or deleted: files the agent had
written next to the stores stay where they are and stay readable, but the agent can no
longer change them, and a relative path such as `notes/x.md` now lands in
`workspace/notes/x.md`. To keep working on them, stop the server and move *your own*
files (not the stores, logs, `.head`/`-wal`/`-shm` files or `checkpoints/` listed in
[configuration.md](configuration.md#6-data-directory-repositorylocalstira--checked)) into
`.local/stira/workspace/`. Alternatively set `workspace_dir` to a folder outside the
data directory (the episodic layer and the context stash are then not file-tool write
roots; `memory_write` still writes episodic memory).

**"@workspace is disabled" at start-up.** `.local/stira/workspace/` is not a real
directory (a symlink, a file, or it could not be created). The server never follows it:
it starts without a work area (`@workspace` is dropped from the write roots, relative
paths are refused; the episodic layer and the stash stay writable if they pass the same
check). Fix it while the server is stopped: remove the link or file and let the server
create the directory, or choose a workspace explicitly with `workspace_dir` in
`.local/stira/agent-config.json` (settings hub or by hand; the file is on the floor, so
the agent cannot change it). An explicit `workspace_dir` is trusted as the place a person
chose; if it lies inside the data directory, the floor still wins and nothing there is
writable.

## 11. Upgrading past #312 (file tools no longer read the data directory)

The data directory is now on the **read** floor too
([ADR 0010, addendum #312](adr/0010-tool-policy-gate-stack.md#addendum-312-the-read-floor)):
`file_read`, `view_image`, `image_reader`, `editor` `view` and `lsp_*` read only
`workspace/`, `memory/agents/<agent>/memory/episodic/` and `context-stash/` inside
`.local/stira/`. Everything else there — the tool rules, approvals, checkpoints,
`neural-state.json`, the configs, the semantic and procedural layers, the logs and stores,
also files the agent wrote next to them before #271 — is refused with `floor`, approved or
not, effective at once (there is no warning-only period). Files outside the data
directory read as before.

What changes in practice: a prompt, skill or schedule that told the agent to read a
data-directory file by path (for example `../neural-state.json`, or your own notes left
next to the stores) now gets a `floor` refusal. Move your own files into
`.local/stira/workspace/` (server stopped, as in §10), or point `workspace_dir` at a
folder outside the data directory. Memory stays available through the memory tools
(`memory_read`, `memory_search`), and offloaded tool results through
`retrieve_offloaded_content`; neither goes through file tools. The settings page lists
where file tools may read next to where they may write (`/settings#sec-tools`, floor).

## 12. Forgetting is archived; undo is explicit only (#294)

`distill` (and `python -m stiracore.control.distillation_cli`) previews Semantic
forgetting by default: each candidate is listed under `forgetting.items` with its key,
current and target stage, `last_accessed`, age, size and the reason it was selected. The
preview changes nothing. `forgetting.selection` states the basis: **`last_accessed`
only**. Semantic memory is not part of the prompt, so `last_accessed` moves only when the
memory tools (`memory_read`, `memory_search`) or Harvest's duplicate search touch an
entry — a useful memory that is simply never searched is compressed first. Read the
preview before applying.

The age is converted to stage thresholds with the entry's half-life
(`retention_half_life_days`, default 30 days), and an entry tagged `retention: invariant`
is not selected but listed under `forgetting.protected`. Untagged entries are selected on
the same days as before. Only a human sets a tag, with `stira memory tag --apply` (#173,
[memory-maintenance.md](memory-maintenance.md#importance-tags-retention-invariant--derived-and-a-half-life-173)).

`--apply-forgetting` compresses the listed entries (head and tail are kept). Before each
overwrite the whole original Markdown (frontmatter included) is written to
`memory/agents/<agent>/memory/forgotten/<id>.json` with its key, source (`source_id`),
stage, time and the SHA-256 of the original and of the compressed text, and read back.
**No archive, no forgetting:** when that directory is not a real directory, the audit log
is damaged, or a record cannot be written and read back intact, the entry (and every
later one) stays as it is and the command exits with 1. The compressed entry carries
`forgetting_archive_id`.

The only way back is explicit:

```bash
.venv/bin/python -m stiracore.control.distillation_cli \
  --base-dir .local/stira/memory --list-forgotten            # ids, keys, digests (no text)
.venv/bin/python -m stiracore.control.distillation_cli \
  --base-dir .local/stira/memory --undo-forgetting <id>
```

Undo restores the original byte for byte, but only while the entry is exactly what that
step wrote: an entry edited since, or compressed again by a later stage, is refused (undo
the later step first). The restored entry counts as read now, so the next preview does
not select it at once. The record is removed after a successful undo. Harvest marks stay
as they were: the episode is still marked harvested and the restored entry keeps its
original `source_id` / `harvest_version`, so nothing is distilled twice.

Harvest marks are metadata and do not reorder Episodic memory (#292): the episode is
rewritten with its modification time kept (set on the open temporary file before it is
fsynced and atomically renamed into place), so `list_entries` and the prompt's Episodic
section still show the newest turns after any number of `distill` runs, staging reviews
or undos. The order is by modification time, not creation: editing an episode in any
other way still moves it to the front. Episodes marked by an older version keep the time
of that run; new turns are listed first again as they arrive. Limits: a change to an
episode while `distill` rewrites it is not detected, so run `distill` with the server
stopped (one writer per data directory, §1 / #285). A hard kill during a write can leave a
`.memory-*.tmp` file in `episodic/`; it is not a memory (not listed, searched, indexed or
harvested) and is not removed automatically — delete it with the server stopped.

Every step is recorded in `forgotten/forgetting-log.jsonl`, a hash-chained JSONL of ids,
keys, stages and digests (`forget.started` / `forget.applied` / `forget.refused`,
`undo.started` / `undo.applied` / `undo.refused`), never content. It is evidence only;
a broken chain refuses further forgetting and undo. If the line *after* a change cannot
be written, the change is still reported as done — `applied` with
`forgetting.audit_incomplete` and the item's `audit_error`, or `"restored": true` with
`audit_error` — the command exits with 1 and the record is kept. The next
`--apply-forgetting` or `--undo-forgetting` run completes the log first
(`forget.recovered` / `undo.recovered`, listed under `reconciled`) and removes records
that are no longer needed (`forget.abandoned`: the entry was never overwritten).

The archive is on the read and write floor of the file tools (inside the data directory,
outside `workspace/`, `episodic/` and `context-stash/`), is not a memory layer (the memory
tools, Harvest and the Semantic index do not see it) and is created owner-only (`0700` /
`0600` on POSIX; on Windows the data directory's ACL applies). Records and the log are
opened without following links (relative to the directory, `O_NOFOLLOW`) and must be
regular files owned by you without group/other permissions, otherwise forgetting and undo
are refused; the directory is fsynced after a record is created or removed. A record can
only restore a Semantic entry of the same agent: a key with `..`, an absolute path,
backslashes or a link inside `semantic/` is refused. On Windows (no `O_NOFOLLOW`) links
are refused by a check before the open (best effort).

**Deleting a memory for good: `stira memory forget` (#294).** A record holds the full
original, including anything secret it contained, so forgetting is not deletion. A
delete request finds the text in every copy — the entry, what was derived from it,
`staging/`, the search index, `semantic/index.md`, the rejection ledger, checkpoints,
`context-stash/`, `turns.db`, **and these archive records** — and with `--apply` removes
all of them, irreversibly
([memory-maintenance.md](memory-maintenance.md#retention-delete-requests-and-provenance-294)).
Backups and the hash-chained logs are not edited (they are listed). The audit log keeps
only digests and keys.

**Known residual risks (not fixed).**
- The archive keeps the original as plain text. This is not a new kind of exposure (the
  memory itself is stored as plain Markdown) and it is protected the same way: `0700` /
  `0600` and the file tools' floor. Encryption at rest is future work.
- Between the digest check and the write, another process editing the same entry is not
  excluded: single-writer protection relies on the data-directory owner lock (#285).
  Do not run `--apply-forgetting` / `--undo-forgetting` against a data directory that a
  running server or another command is writing.
- The audit log has no head anchor and no key: whoever can write it can rewrite it and
  recompute the chain (including an edit that keeps the size), and a removed last line is
  not detected. It shows accidents and partial runs, not a determined local attacker.
- The memory store's own path checks (`MemoryStore.read_text` / `write_text` and the
  other entry paths) are separate from the open: a last component swapped for a link
  after the check, or a link placed in an ancestor directory, is not excluded there. Both
  need write access to the data directory; keeping that boundary is the operator's
  responsibility (one owner per data directory, #285; host guidance, #318). Only the
  archive and its log use descriptor-based, no-follow opens; extending that to every
  memory path is follow-up work. Since #291, entry reads (`read`, `list_entries`, `search`) open
  without following a final link and re-check the path against the opened file; a
  swapped name is skipped by listings and refused by reads.
- A partial line left in the audit log by an interrupted write (for example a full disk)
  makes every later apply/undo refuse (fail-closed); reconciliation cannot complete it.
  Free the space, inspect the log, and move it aside together with the records only
  after reviewing them.

## 13. What the logs and stores mask (#291)

Secret masking (`observability/masking.py`) is applied to: the server log on stderr
(`.local/stira.out.log`; message, arguments and traceback; `stira_server`'s `main()`
masks every log record when it is created, so handlers added by `logging.basicConfig` or
a library, before or after start-up, get masked text too), turn events (SSE `ui_event`, `/api/turns`, `turns.db`), tool payload pages,
`events.db`, the approval store, episodic memory (also the Slack/Discord bots'), and the
chat history (`/api/chat/history` and the turns handed to an external `memory_store`:
the message and the reply are masked when the turn is kept, and again on the way out).
The chat client never receives an
exception's text (a fixed error message); the Slack/Discord bots log only its type. The
start-up banner prints no paths.

**Not masked today** (a design decision, #291): the reply streamed to the chat (SSE
`delta`, so a reply can read differently after a reload), the Slack/Discord replies, and checkpoints
(`checkpoints/*.json` hold the conversation as the model saw it, so that a paused turn
can be replayed). `tests/test_output_masking.py` lists every output; the unmasked ones
are strict expected failures there. Treat `.local/stira/` as sensitive.

Known limits of the memory entry reads (#291, accepted in the #330 review):

- `MemoryStore._open_entry` opens the final component with `O_NOFOLLOW` and then
  re-checks the path (no link in any component, same inode as the opened file). It does
  not walk the directories with descriptors (`dir_fd`): a parent directory swapped for a
  link and swapped back around the open is not excluded. That needs write access to the
  data directory, so it rests on the same operating boundary as above (one owner per
  data directory, #285; host guidance, #318).
- Where the platform has no `O_NOFOLLOW` (Windows), the flag is 0: only the re-check
  after the open applies.

## 14. Page polling and the access log (#369)

The pages read a few read-only endpoints (`/api/turns`, `/api/hitl/pending`,
`/api/gateway/status`, `/api/scheduler`, ...) on a timer. They go through
`static/js/poll.js`: a hidden browser tab does not poll at all (it polls once when shown
again); a failing server is asked less and less (the interval doubles per failure, at most
every 30 s); a poll that finds nothing new widens step by step (the Stage: 1 s → 10 s, the
approval queue: 1 s → 5 s, status: 5 s → 15 s) and is short again at once when something
changes, while a turn runs or an approval waits, and when an approval request arrives on the
page's own chat stream. An approval request from another tab or channel is therefore seen
within the idle maximum (5 s) instead of 1–2 s. Measured on a fresh server with a fake model
(one tab, idle): see the pull request of #369.

The access log (`.local/stira.out.log`) no longer carries the successful (`200`/`304`)
`GET` lines of these polls; a failing poll and every other request are still logged.
`STIRA_ACCESS_LOG_POLLING=1` brings the poll lines back for debugging
([configuration.md](configuration.md)). Nothing here adds a route or a permission.
