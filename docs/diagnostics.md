# Write diagnostics: permission cards and `stira doctor` (#244)

When a write fails, Stira tells you **what** failed, **why**, **whose permission** is
needed and **how a person fixes it**. Stira's diagnosis never changes an OS protection.
Design: [ADR 0007](adr/0007-write-diagnostics.md).

## Permission cards

A failed `file_write` / `editor` call is classified from the errno, Stira's own refusal
texts and read-only probes of the path:

| Category | Typical cause | Retry offered |
|---|---|---|
| `locked_flag` | `uchg` / `schg` flag, Windows read-only attribute, Linux `chattr +i` | yes |
| `protected_by_os` | macOS privacy (TCC: Desktop, Documents, Downloads, iCloud Drive, `/Volumes/*` …) or SIP | TCC: yes, SIP: no |
| `readonly_volume` | read-only mount (`statvfs`), `EROFS` | yes |
| `wrong_cwd` | relative/empty path resolved against `/` | yes |
| `permission_denied` | file mode / owner, ACL deny entry | yes |
| `outside_write_roots` | outside `write_roots` (#243) — change the setting, restart | no |
| `identity_protected` | SOUL / IPD / protected directories, TCC database — by design | no |
| `consent_blocked` | Stira's approval gate / breaker refused the call | no |
| `other` | anything else with an errno | yes |

The chat page shows a card in the same frame as approval cards: operation and path,
category and evidence (e.g. `flag:uchg(file)`, `tcc:~/Documents`, `statfs:rdonly`), the
estimated responsible process (terminal app, launching app, or the executable itself under
launchd), the steps for a person, and a System Settings link where one exists. Paths are
masked (home → `~`, user name → `<user>`, secrets).

**Fixed it → retry once** is single-use (a second click or tab gets HTTP 409). The server
re-checks the cause read-only; unless it is still there, the page sends one ordinary chat
message asking the agent to retry — through every gate again. TCC locations are never
probed (they could open a consent dialog), so their re-check is "unverified".

The model only sees a short note: the category and that a person must fix it. Tool calls
whose input names `xattr`, `chflags`, `sudo`, `tccutil`, `csrutil` or the TCC database **in
plain text** are refused on the command-capable routes (shell, batch, inner agents, MCP
command tools), at every breaker level, and no approval lifts that refusal. It is a denylist
of names, not a sandbox: a name computed at run time (`getattr(os, "ch" + "flags")`, base64,
variables, aliases) is not detected. Exec-class tools are disabled by default; containing them
is the exec sandbox's job (#167).

**Single user.** The dashboard is a local, single-user server (one process, one agent, bound
to loopback, CSRF token as the capability). Permission cards are not separated per session:
any page of that server can list, retry or dismiss them.

Paths shown on cards and in the status are minimised: `@workspace/…`, `~/…`, system and
application locations as they are, anything else `…/<last two parts>`.

Recommended for macOS: keep the agent's work folder outside protected folders (for example
`~/projects/...`) instead of granting access. Full Disk Access is never recommended.

## `stira doctor`

```bash
stira doctor            # text report (also: python -m stiracore.diagnostics doctor)
stira doctor --json     # JSON report
stira doctor --root ~/projects/work --no-write-test
```

For every write root of the agent config (`--config`, default
`.local/stira/agent-config.json`): TCC locations get a warning and are not read; other
roots are checked for lock flags, SIP, a read-only mount, ACL deny entries, quarantine and
iCloud/file-provider attributes, then a write test creates, renames and removes one temporary
file — relative to a directory descriptor reached without following any symlink (a symlink on
the path, a root that changed during the checks, or a platform without `O_NOFOLLOW`/`dir_fd`
support: the write test is *skipped*; a root configured through a symlink — e.g. `/tmp` on
macOS — is therefore skipped, use the real path). The identity floor (and, in `stira doctor`,
the configured write policy) always applies; if the policy cannot be built, no write test runs. An unresolvable root (symlink loop) is reported and the
other roots are still checked. Nothing else is changed — not the config, not a flag, not an attribute; a missing root
is reported, not created.

Exit status (#267):

| Exit | Meaning |
|---|---|
| `0` | Nothing wrong, or warnings only (a warning never fails a script) |
| `1` | At least one error (a locked root, a read-only volume, a failed write test, …) |
| `2` | The configuration is unusable: the config file cannot be read, or the write policy cannot be built from it (e.g. a too-broad `write_roots` entry without the opt-in). The report is still printed; `config_error` is `policy_unbuildable` and `ok` is `false`. Usage errors from the command line also exit `2`. |

**Classifier check.** The report has a `checks` list (not tied to a write root). It carries
the emotion-classification mode: `classifier_sokudan` (info: Sokudan is installed),
`classifier_rule_chosen` (info: the settings choose the rule-based classifier),
`classifier_rule_only` (**warning**: `auto` and Sokudan is not installed, so the rule-based
fallback runs and emotions rarely react), `classifier_missing` (**warning**: Sokudan is chosen but
not installed). The warning says how to fix it: `pip install -e ".[jev]"`. The doctor only looks
the package up (`importlib.util.find_spec`); it never imports it or loads the model. Warnings
count in the summary but never change the exit status.

One note says whether the verification browser tool `page_check` (ADR 0015, #449) is offered:
`page_check_ready` (the browser path; the note says what the tool does: open a workspace page
with no network, a few input actions and checks — `type`, `wait_for`, five assertion forms —,
screenshots), `page_check_no_browser` (install Chrome, Chromium or
Edge, or set `STIRA_BROWSER_PATH`), `page_check_platform` (Windows, phase 1) or `page_check_root`
(Chrome would need `--no-sandbox`). Always `info`: the tool is optional. No browser is started.

One note says whether and how the verification runner `run_checks` (ADR 0019, #167) would be
contained: `run_checks_off` (info; gateway `run_checks: {"phase": 1}` turns it on),
`run_checks_ready` (info; macOS, the sandbox self-test passed; memory is not limited on
macOS), `run_checks_limits_only` (**warning**; e.g. Linux: "unprivileged user namespaces are
restricted on this host" — the network, home reads and writes outside are not held),
`run_checks_self_test_failed` (**warning**; names the probes that were not refused),
`run_checks_platform` (Windows) or `run_checks_root`. On a limits-only host the note also says
that a saved Always is read as Ask. Display only: the self-test runs Stira's own probe, never
the workspace's code. `stira checks discover <dir>` lists the kinds and argv that would run
(files only, nothing executed).

## Settings page: the Diagnostics section (#267)

`/settings` has a **Diagnostics / 診断** section (read-only; reachable from the section list).
It shows the last doctor report from `GET /api/diagnostics/doctor` — verdict, workspace, whose
OS permission is checked, the checks above, every write root with its findings and, for a
person to carry out, the fix text — in the page language. "Run again" calls the existing
`POST /api/diagnostics/doctor` (CSRF-protected; the same read-only check as the chat sidebar,
one temporary file per root). The section has no field and writes no setting; it adds no route.

## Known limits (tracked on #267, not fixed by the tab)

- Permission cards are not separated per session: the dashboard is single-user (ADR 0007 §6a).
  Separate them when the dashboard becomes multi-user or remote.
- The TCC check's `lstat` of the components can race with a swapped intermediate component on
  the read side (metadata queries only; ADR 0007).
- Command names computed at run time are not on the denylist; the exec sandbox (#167) covers them.

The chat server runs the doctor at start-up. `/api/gateway/status` carries the summary:

```json
"diagnostics": {
  "doctor": {"ran": true, "ok": true, "roots": 1, "errors": 0, "warnings": 0, "codes": []},
  "write_failures": {"open": 0, "latest": null}
}
```

Routes: `GET /api/diagnostics/write-failures`, `POST …/<card_id>/retry`,
`POST …/<card_id>/dismiss`, `GET|POST /api/diagnostics/doctor` (POSTs need the CSRF token;
see [admin-api-permissions.md](admin-api-permissions.md)).

Linux and Windows get minimal checks (flags/ACL/SELinux/AppArmor presence; read-only
attribute, path length, OneDrive, Controlled Folder Access zones), not verified on real
machines.
