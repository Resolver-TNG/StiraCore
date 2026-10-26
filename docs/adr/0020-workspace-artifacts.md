# ADR 0020: Workspace artifacts — register what the agent writes, show versions, diffs and a person's labels

**Path:** `docs/adr/0020-workspace-artifacts.md`
**Status:** Accepted for implementation (2026-10-21, as specified by #454). Built in two steps:
PR 1 = registration, versions and the read API on the running server; PR 2 = the list page, the
isolated preview and the person-only labels.
**Issue:** Refs #454. Related: #59 (the artifact layer, `tools/artifact_store.py`), ADR 0015
(`page_check`, §8 "never serve workspace files from the dashboard origin"), #449, #451, #446,
#192, #182, #271 (data-directory floor), #312 (read floor), ADR 0013 (audit contract).

## Context

Asked for a hockey game, the agent wrote `hockey_game.html` → `_v2` → `_yolo` → `_v3` into its
workspace within an hour. The person had to find the files and hand them to a browser; which one
is latest, stable or experimental, and what changed between them, was not visible. The #59
artifact layer exists as code (`ArtifactStore`, `artifact_api.py`, `artifacts.js`) but was never
registered on the server (`/api/artifacts` and `/artifacts` answered 404), and nothing the file
tools wrote reached it.

## Decisions

### 1. A snapshot registry beside the workspace, not the slug store of #59

`ArtifactStore` keys artifacts by a slug (`[a-zA-Z0-9_-]`), keeps each version's full content in
JSON and loads every artifact's content to list them. The workspace is keyed by relative path and
the files already live there. So the registry (`tools/workspace_artifacts.py`) records **facts**
per version (relative path, turn id, create/update, size, SHA-256, kind, time) in
`<data dir>/artifacts/index.db` and keeps a **content-addressed snapshot** of a version's bytes in
`<data dir>/artifacts/blobs/<sha256>` when it is small enough (§4). `ArtifactStore` stays as a
library and is not wired. The registry is a fourth layer next to the three-layer memory, never
part of it.

`<data dir>/artifacts/` is on the data-directory floor (#271/#312): agent file tools can neither
read nor write the index, the snapshots or the labels.

### 2. What is registered

A successful mutating `file_write` / `editor` call (an `AfterToolCallEvent` hook; editor's
`view` / `find_line` are not mutations). The path is judged with the **write side's** functions
(`WritePathPolicy.absolute` / `check`, plus `check_read`): registered only when

* the path as given, anchored like the write (`absolute`), is **inside the workspace**
  (lexically, after `normpath`), and its resolved path is the same place: no component below
  the workspace is a symlink (each one `lstat`-checked);
* the write policy allows it now (`check` → `ok`: not on the floor, inside the write roots) and
  the read floor allows it;
* no component starts with `.` (`.env`, `.git/…`, `.stira-verify/…`), and the relative path is
  unchanged by `mask_secrets`;
* the file is a regular file opened without following a final symlink, at most `MAX_FILE_BYTES`.

Anything else is skipped with a fixed reason code (`outside_workspace`, `symlink`, `floor`,
`hidden`, `secret_name`, `not_regular`, `too_large`, …) counted in memory; no path is logged.
The same eligibility is applied again when listing or showing (a path that became protected or a
symlink later is not shown). Files written by `shell` / other exec tools are not registered
(exec is hidden by default; a later step).

### 3. Versions

Each registration of a changed SHA-256 is a new version (`unchanged` otherwise). At most
`MAX_VERSIONS` (50) per path; older version rows go and their snapshots are deleted when no other
version refers to them. Text diffs (`difflib.unified_diff`, line based) are computed on request
from two stored snapshots, bounded in input and output; binary files have no diff.

### 4. Limits (no unbounded work on the server)

`MAX_ARTIFACTS` paths (1000; a new path beyond it is refused and counted, shown as a warning —
no automatic deletion), `MAX_FILE_BYTES` (8 MiB) per registration, `MAX_SNAPSHOT_BYTES` (1 MiB;
bigger files keep a hash-only history), `MAX_STORE_BYTES` (256 MiB of snapshots; beyond it new
versions are hash-only, with a warning), list `limit` ≤ 200, diff inputs ≤ 1 MiB and output ≤
2000 lines.

### 5. No body in events or the audit

The index holds facts only. Nothing of a registration goes to `events.db` or the turn store. The
label change (§7) is one `audit.artifact.labeled` row with the artifact id, the label codes and
the version — no path, no content.

### 6. Preview (PR 2): a sandboxed document, which refines ADR 0015 §8

**Operator approved (2026-10-21):** this section updates ADR 0015 §8 (a note there points here).

ADR 0015 §8 rules out serving workspace files **on the dashboard's origin** (the agent's
JavaScript would run with the dashboard's session). The preview does not do that:

* it serves the **registered snapshot**, never the live workspace file;
* the response carries `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; …`
  (an opaque origin even when opened top-level; no network: `connect-src`/`img-src` etc. limited
  to `data:`/`blob:`), `X-Content-Type-Options: nosniff`, and may be framed only by the dashboard
  (`frame-ancestors 'self'`);
* the page embeds it in `<iframe sandbox="allow-scripts">` (no `allow-same-origin`, no forms,
  popups or top navigation).

The document therefore has a unique opaque origin: no access to the parent's DOM, storage or the
CSRF token, and every same-origin API read refuses its `Origin: null`. Images use `<img>`;
Markdown, text and code are shown as text (`textContent`). Residual: WebRTC is not governed by
CSP; a page could still try a STUN request (it can only send what it contains itself). Only
single-file pages work (no `'self'` sources: a sibling `game.js` does not load).

The `page_check` result of the same file (#449) is shown beside the preview: the same hook keeps,
for a registered artifact, the codes and counts `page_check.for_log` keeps (status, error
counts, blocked requests, assertions; never the page's title, text, messages or hosts), with the
version that was latest at the check (last `MAX_CHECKS` = 5).

### 7. Labels (PR 2): a person's, never the agent's

Labels (`stable` / `experimental` / `latest`, a fixed set) are set only through
`POST /api/artifacts/<id>/labels` (same-origin + CSRF like every dashboard write). No agent tool
reads or writes them; the index is on the data-directory floor. Every change is one audit row
(§5).

**Residual, kept as is (operator decision, 2026-10-21).** The floor binds the file tools, not
exec tools. With exec tools enabled (`STIRA_ENABLE_EXEC_TOOLS`, off by default) the agent's
`shell` runs as the server's OS account and could change the label index during a turn. Such a
change has no `audit.artifact.labeled` row. This is the same residual as for every other
data-directory file under exec; it is not closed here.

### 8. Links from the Stage (#535)

The activity board (#446) and the progress report (#451) link to `/artifacts?turn=<turn id>`
when that turn wrote artifacts: `GET /api/activity` adds `artifacts: n` to the turn's end row
and its done `file_write` / `editor` rows, `GET /api/progress/current` adds it to the report
(the report's own turn). `n` is counted at read time from the registry (`turn_counts`, at most
32 turns per read, paths visible under the current write policy only); nothing is written into
the turn events or the audit, and no path or body leaves the registry. A turn with none has no
`artifacts` and no link. The link is a same-origin path with the opaque turn id only.

## Consequences

* The person sees what the agent made, by turn and kind, with versions and diffs, without
  looking for files.
* Snapshots duplicate small files that also live in the workspace (bounded by §4).
* Residuals: registration is a check at one instant (TOCTOU as for the write policy); WebRTC in
  the preview (§6); files made by exec tools are not seen; with exec tools enabled, the agent's
  `shell` can change labels without an audit row (§7).
