# ADR 0007: Write-failure diagnosis, permission cards and `stira doctor`

**Path:** `docs/adr/0007-write-diagnostics.md`
**Status:** Accepted
**Issue:** #244. Related: #243 (`write_roots`), #189 (approval cards), #242/#246 (consent
gate), #245/#261 (`editor` → `file_editor` + `StiraWriteSandbox`), #185 (masking), #167 (exec
sandbox).
**Decision:** Failed `file_write`/`editor` results are classified after the fact (errno +
Stira's own refusal texts + read-only probes of the path), annotated for the model and shown
to a person as a permission card. A separate `stira doctor` checks every write root before
the agent runs. Nothing in this path changes an OS protection. Tool calls whose input names
`xattr`, `chflags`, `sudo`, `tccutil`, `csrutil` or the TCC database **in plain text** are
refused on the command-capable tool routes, and no approval lifts that refusal. Names computed
at run time are out of scope (exec tools are off by default; #167).

> **Note (2026-10-16).** On 2026-10-16 the owner authorized the design and development of
> (1) an autonomous goal loop that runs only while the user's explicit grant is active, with
> an expiry (default 12 h, at most 36 h), a budget and a scope, and (2) bounded
> self-scheduling: inside an envelope a human defined, the agent may propose and create
> jobs. Both stop when the emotional state worsens. Autonomy without a grant, always-on
> event-driven operation, self-expansion of permissions and the agent changing its own
> envelope stay out of scope. The design is ADR 0011 (Proposed: `docs/adr/0011-agent-self-scheduling.md`, PR #295, tracking
> #296). This ADR is unchanged: the person still decides
> and acts on OS protections; no grant lets the agent remove them.

## Context

On macOS a TCC denial, a `uchg` file, a SIP location and a read-only volume all surface as
`[Errno 1] Operation not permitted`; a relative path resolved against `/` looks like
`Read-only file system`. The person and the model both misread the cause, and the model's
natural "fix" — `chflags nouchg`, `xattr -d`, `sudo` — would have the agent remove OS
protections on its own. Fully autonomous operation is not authorized; the person decides
and acts.

## Decisions

### 1. Classify after the tool, not before

`WriteFailureHook` (`AfterToolCallEvent`) sees every failed result of the two path-write
tools, whoever produced it: the policy wrapper (`IdentityProtectedTool`), a hook
cancellation (`IdentityGuard`, the HITL gate), the gate mark/consent bridge, or the OS error
caught by the upstream tool / `StiraWriteSandbox`. One place covers `file_write` and
`editor` alike (tests run both). Stira's own refusals are recognised by their fixed texts
(`READ_ONLY_ERROR`, `OUTSIDE_WRITE_ROOTS_ERROR`, gate texts) — no re-check of the policy is
needed, so nothing new touches the path. OS errors are classified from the errno plus
read-only probes: `lstat().st_flags` (`uchg`/`schg`/`restricted`), `statvfs` (`ST_RDONLY`),
ACL (`acl_get_link_np`) and `os.access`, on the target and its nearest existing folder.

Categories: `locked_flag`, `protected_by_os` (TCC or SIP), `readonly_volume`, `wrong_cwd`,
`outside_write_roots`, `identity_protected`, `consent_blocked`, `permission_denied` (mode
or ACL; added because EACCES from ownership/mode is common and is neither of the others),
`other`. Editor content errors (`old_str` not found …) have no errno or refusal text and are
not classified.

Alternative rejected: classifying inside `IdentityProtectedTool`. It would miss hook
cancellations (the most common refusals) and the inner sandbox's errors.

### 2. TCC paths are recognised lexically and never touched

Desktop, Documents, Downloads, iCloud Drive (`~/Library/Mobile Documents`),
`~/Library/CloudStorage`, app containers, Mail/Messages/Safari, the TCC directories and
`/Volumes/*` are matched by name (case/NFC-folded `path_key`). Every probe refuses such a
path before any system call (`TCCPathError`), and `safe_resolve` resolves symlinks one
component at a time, stopping *before* a component that lies in a zone. Accessing them could
raise a consent dialog and block the process; the classifier and the doctor only warn.

### 3. The model gets a note; the person gets the steps

The note appended to the tool result names the category and evidence and says that a person
must fix it and that changing flags/attributes/privacy/SIP or using sudo is refused. The
human steps (including commands like `chflags nouchg <path>` *for the person to run*) appear
only on the card, never in what the model reads. Full Disk Access is never recommended; the
recommended fix for TCC is to keep the work folder outside protected folders.

### 4. Responsible process is an estimate

macOS attributes a terminal-launched process to the terminal app, a LaunchAgent to the
executable, an app-launched one to the app. `responsible_process` reads `TERM_PROGRAM`,
`__CFBundleIdentifier`, `XPC_SERVICE_NAME` and the real path of the interpreter. It is
labelled an estimate; whether a Homebrew (ad-hoc signed) update resets a grant is unverified.

### 5. Retry: once, by a person, through every gate

`WriteFailureBoard` keeps a bounded in-memory list of cards. "Fixed it → retry once" flips the
card to `retried` atomically *before* anything else runs (a second click or tab gets 409), then
re-checks the cause read-only (`clear` / `still_blocked` / `unverified`; TCC and unknown
causes are `unverified`, never probed). Unless still blocked, the page sends **one ordinary
chat message** asking the agent to retry. The server never re-executes the tool itself and
never sets `AfterToolCallEvent.retry`: a server-side re-run would need a fresh gate mark
outside the hook chain, i.e. an approval nobody gave. Categories that a retry cannot fix
(`identity_protected`, `outside_write_roots` — needs a settings change and restart — SIP,
the gate) offer no retry. A human HITL rejection gets no card at all.

### 6. OS-protection commands: a denylist on two layers

`os_protection_denial` runs first in `IdentityGuard.path_denial` (also consulted by the HITL
gate before it asks — so the request never reaches an approver) for command-capable tools
(the exec class plus names such as `run_command`). `OsProtectionGuardTool` wraps every
command-capable tool in `tool_registry.get_tools`, re-checks the final input (after any HITL
edit) and holds in hook-less inner agents. Tool names, specs and order are unchanged, so the
cached prompt prefix (#254) is unaffected. Writing the TCC database with `file_write`/`editor`
is on the path floor (`identity_paths`). Matching folds case (APFS resolves `SUDO`), quotes,
backslashes and full-width forms.

**Scope, stated precisely:** "no approval lifts it" holds for calls that *name* these
commands in plain text. A name computed at run time (`python3 -c 'getattr(os, "ch" +
"flags")(…)'`, `$(echo …)`, base64, variables, an alias or a symlink to the binary) passes
the denylist; both layers use the same check, so layering does not change that. This is
acceptable only because exec-class tools are disabled by default (`STIRA_ENABLE_EXEC_TOOLS`)
and is handed over to the exec sandbox (#167). The limit is pinned by
`test_computed_command_names_are_out_of_scope_for_the_denylist`.

### 6a. Single-user local dashboard (no per-session cards)

The permission board is one in-memory list per process; any page holding the CSRF token can
list, retry or dismiss any card. That matches the server model: one process serves one agent
for one person, bound to loopback (`resolve_bind_host`), with the per-process CSRF token as
the capability (`docs/admin-api-permissions.md`). Session separation is not implemented; a
multi-user deployment needs an authentication layer first, and then card ownership.

### 7. `stira doctor`

For every write root: TCC → warning only; otherwise flags, read-only mount, ACL, quarantine
and iCloud/file-provider attributes (`listxattr`, names only), then one write test
(`mkstemp` → `rename` → `unlink`, cleaned up on every path). A root that does not exist is
reported, never created; a probe file on the identity floor is skipped. It reads the agent
config and never writes it. Output: JSON, text, and a summary in `/api/gateway/status`
(`diagnostics.doctor` + `diagnostics.write_failures`). The chat server runs it once at start-up;
`POST /api/diagnostics/doctor` re-runs it. Linux (flags via `FS_IOC_GETFLAGS`, POSIX ACL,
SELinux/AppArmor presence) and Windows (read-only attribute, path length, OneDrive,
Controlled Folder Access zones) get minimal checks; anything else is `other`.

## Consequences

* Paths on cards and in the status are minimised: workspace → `@workspace/…`, home → `~/…`,
  system/application locations as they are, anything else → `…/<last two parts>`; user name →
  `<user>`, secrets masked. Environment-supplied principal names must be on
  an allow-list (else `unknown`). Logs carry exception class names only. The unmasked
  target stays in memory only for the read-only re-check.
* Every probe — classifier, re-check and doctor — resolves the path one component at a time
  and stops before a component inside a TCC zone (also behind a symlink). Zone matching uses
  the case/NFC-folded `path_key` (tested: NFD home, case variants, `/System/Volumes/Data`).
* The write test always applies a write policy: the caller's, the one `stira doctor` builds
  from the agent config, or — when none is given — at least the identity floor (SOUL/IPD,
  the TCC database and directory). A policy that cannot be built means no write test.
  The walk uses the root *as configured* (absolute, not resolved), so a root reached through
  a symlink that exists from the start is skipped too.
* The doctor's write test walks the root from `/` one component at a time
  (`openat(parent, name, O_DIRECTORY|O_NOFOLLOW)`): a symlink anywhere on the path — the root
  or an ancestor swapped in between — means **skipped**, never followed. The descriptor must
  be the inspected directory (`fstat` = recorded `(st_dev, st_ino)`), the path judgment
  (TCC, floor) is made again, and a second walk must reach the same directory; only then are
  create/rename/unlink done, all relative to that descriptor. Without `O_NOFOLLOW`,
  `O_DIRECTORY` or `dir_fd` support the test is skipped, not weakened. Remaining (accepted):
  renaming the inspected directory itself into a protected tree after the last walk.
* **Residual risk, read side only:** a component swapped for a symlink into a TCC zone
  *between* two `lstat` calls of `safe_resolve` can make the next `lstat` follow it. That is a
  metadata query (no content read, nothing written); it needs an attacker who can rewrite the
  path while it is diagnosed and is accepted for a diagnosis feature.
* Principal names come from small allow-lists (known terminals/editors); any other
  `TERM_PROGRAM`/`__CFBundleIdentifier` value and every launchd label are shown as `unknown`.
* The settings-page "diagnostics tab" is not part of this change: the chat sidebar shows the
  doctor summary and a re-run button; a full tab is follow-up work.
* Not verified on real Linux/Windows machines (CI cells only).
