# Repository inspection (Stage workbench, stage 1) / リポジトリの検査（作業机・第 1 段階）

Issue #671. A read-only library module, `stiracore.diagnostics.repo_inspect`, ported from
Stira-KIRO's workbench (`backend/repos.py`, v0.7.x). Stage 1 has **no UI, no route, no
settings file, no i18n and no fetch**: the caller passes the paths, the scan roots, the scan
depth and the read policy as arguments. The sticky notes on the Stage and the fetch button
are later stages.

## What it reports

For each repository (`RepositoryInspector.inspect` / `inspect_all`), allow-listed fields only:

| Field | Meaning |
|---|---|
| `id`, `name`, `path`, `root` | `local-<sha256 12>` of the resolved path; names/paths masked and cut |
| `branch`, `detached`, `upstream` | current branch (`(detached)`), its upstream |
| `ahead`, `behind` | against the upstream (`ahead` = unpushed commits) |
| `changes` | counts: `staged`, `unstaged`, `untracked`, `conflicted`, `total` |
| `defaultBranch`, `defaultBranchSource`, `compareBase`, `defaultBranchBehind` | `origin/HEAD` (or `main`), compared with `origin/<default>` when it exists |
| `branches` | `[{name, ahead, behind}]` against `compareBase` (≤ 100; `null` on git < 2.41) |
| `worktrees`, `worktreeList` | counts and records: `prunable`, `locked`, `detached`, `main` |
| `stashCount`, `lastCommit` | stash entries; last commit short hash and committer date |
| `originUrl`, `github` | the origin URL without userinfo, masked; `owner/repo` for GitHub |
| `fetchHeadUpdatedAt` | mtime of `FETCH_HEAD` (this module never fetches) |
| `issues`, `status` | fixed codes (`behind_upstream`, `unpushed_commits`, `uncommitted_changes`, `default_branch_behind`, `prunable_worktree`, `locked_worktree`, `detached_worktree`, `inspection_failed`) and `green`/`yellow`/`red` |
| `error` | only on failure: a fixed text (`deadline`, `filter drivers changed during inspection`, …) or git's stderr with paths outside the read roots replaced by `[outside read roots]`, masked, one line, ≤ 240 chars |

Not read or reported: commit subjects and bodies, diffs, file names, file contents, the
`locked`/`prunable` reason texts, configuration values other than `remote.origin.url`.
Branch and ref names pass `mask_secrets`; a name it would change is shown as `[masked]`.
A linked worktree whose path the read policy refuses is listed as `[outside read roots]`.
Userinfo is dropped from URLs with a scheme and from scp-like remotes
(`user:token@host:path` → `host:path`, the part before the first `/`).

## Git commands it runs

Only these argument lists (`GIT_COMMANDS`), `shell=False`, as
`git <HARDENING_ARGS> [filter overrides] -C <repo> --git-dir=<git dir> --work-tree=<repo> <command>`
(the checked, resolved paths; on the command line they win over `core.worktree`):

| Name | Arguments |
|---|---|
| `dirs` | `rev-parse --path-format=absolute --git-dir --git-common-dir --show-toplevel` (runs first; all three must pass `check_read` and equal the checked paths) |
| `filters` | `config --null --name-only --get-regexp ^filter\.` (before and again after `status`) |
| `status` | `status --porcelain=v2 --branch --ignore-submodules=all --untracked-files=normal --ahead-behind` |
| `origin_head` | `symbolic-ref --quiet --short refs/remotes/origin/HEAD` |
| `verify` | `rev-parse --verify --quiet refs/remotes/origin/<default>` |
| `branches` | `for-each-ref --format=%(refname:short)%00%(ahead-behind:<base>) refs/heads` |
| `worktrees` | `worktree list --porcelain -z` (git ≥ 2.36) |
| `stash` | `rev-list --walk-reflogs --count refs/stash` |
| `last_commit` | `log -1 --no-show-signature --format=%h%x00%cI` |
| `origin_url` | `config --get remote.origin.url` |

`<default>`/`<base>` come from the repository and must match `[A-Za-z0-9][A-Za-z0-9._/-]*`.

`HARDENING_ARGS`: `--no-pager --no-optional-locks` and `-c` for `core.fsmonitor=` (empty;
older git would run a command named `false`), `core.useBuiltinFSMonitor=false`,
`core.hooksPath=<devnull>`, `core.untrackedCache=false`, `core.pager=cat`, `core.askPass=`,
`credential.helper=`, `protocol.allow=never` (no transport, also no lazy fetch of a partial
clone), `diff.external=`, `log.showSignature=false`, `gc.auto=0`, `maintenance.auto=false`,
`submodule.recurse=false`, `status.submoduleSummary=false`, `core.quotePath=false`,
`safe.bareRepository=explicit`, `color.ui=never`. The `status` flags are fixed so that
`status.showUntrackedFiles=no` or `status.aheadBehind=false` in the repository cannot make
the report look clean. Every filter driver found by `filters` gets
`clean`/`smudge`/`process` emptied and `required=false`, so `status` runs no clean filter; a
driver name that cannot be overridden (`=` or control characters) or more than 64 drivers
refuses the repository.

Environment: only `PATH`, home/temp/Windows system variables and `XDG_CONFIG_HOME` are kept
(no inherited `GIT_*`), plus `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`,
`GIT_NO_LAZY_FETCH=1`, `GIT_PAGER=cat` and `GIT_CEILING_DIRECTORIES=<parent of the repo>`.
Each call has a 10 s timeout and a 1 MiB stdout cap (stderr 16 KiB); past the cap the process
is killed and the repository reports `error`. One failing repository never hides the others.
`inspect_all` has an overall deadline (60 s on the injected clock); repositories left after
it are reported as `inspection_failed` with `error: deadline`.

## Paths

* Configured `paths` and `scanRoots` must be absolute, not UNC (`\\server`, `//server`)
  and allowed by `WritePathPolicy.check_read` (read roots, secret-path floor, data-directory
  floor) before anything is opened. A configured path that is a symbolic link or junction is
  refused (`link`); below a scan root, links are skipped. `scan_depth` is 1–3; a repository
  is not descended into; at most 5 000 entries are listed (a directory is not read past
  the remaining budget) and 200 repositories returned.
* The Git directory (`.git`, or the `gitdir:` of a `.git` file, and its `commondir`) must
  also pass the read policy. A `.git` or `commondir` entry that is a link refuses the
  repository. git's own answer (`dirs`) is checked again after git resolved it.
* Problems are `{field, index, code}` only (`invalid`, `network_path`, `not_absolute`,
  `refused`, `link`, `missing`, `not_directory`, `not_repository`).

## Remaining risks

This is a check at one instant, not a sandbox.

* git follows symbolic links **inside** the Git directory (for example `refs`, `objects`,
  `config` or `packed-refs` being links); this module does not walk the Git directory, so
  such links can make git read outside the read roots. Only counts and allow-listed fields
  come back, but the reads happen.
* Only UNC spellings are recognized as network paths. A network share mounted as a local
  path (macOS `/Volumes/<SMB share>`, a Linux NFS/SMB mount, a mapped Windows drive letter)
  is not detected and is treated like a local directory.
* A directory replaced between the check and git's run (TOCTOU) is not prevented.
* **Filter override race (code execution, not only a read problem).** The filter overrides
  are built from the drivers listed before `status`. A `filter.<x>.clean` written into a
  configuration file git reads (the repository's `config`, `config.worktree`, or any file
  reached through `include.path` / `includeIf`, including files outside the read roots)
  between that listing and `status` is run by git: an arbitrary command with the server
  user's rights, once per tracked file that git has to re-check. It needs a local process
  that can already write those files, and it can happen once per uncached `inspect` (every
  call with `use_cache=False`; otherwise at most once per repository and 5 minutes).
  Decision (commander, 2026-11-22): **detect and discard** is what ships. Stage 1 lists the
  drivers again after `status`; a driver that is still present and was not in the first
  listing drops the result (`inspection_failed`, `filter drivers changed during inspection`),
  and a failed listing (anything but exit 0 or "no match") also refuses the repository.
  **What this does not catch:** a driver added and removed again inside the `status` window
  (the command has run by then), and a command that runs before the second listing at all —
  the check discards the answer, it does not undo the run.
  Options that were weighed: (a) document only; (b) detect and discard (**shipped**);
  (c) split `status` into plumbing commands that never run filters (**follow-up issue**, the
  real fix); (d) run git in an OS sandbox (not planned).
* git also reads outside the read roots through plain configuration, not only through links:
  `include.path` / `includeIf` targets, `objects/info/alternates`, `worktrees/*/gitdir`, and the
  user's global configuration (`$HOME`, `XDG_CONFIG_HOME`, which this module keeps so that
  identity-independent settings still work).
* The 60-second deadline of `inspect_all` is checked **between** repositories. One repository
  can take up to eleven git calls of 10 seconds each, so a slow mount or a FIFO behind an
  `include.path` can overrun it by about two minutes.
* `error` carries git's stderr with paths outside the read roots replaced (a path containing
  a space is replaced up to the first space only), and it can name a file inside the
  repository that git complains about. A scp-style remote whose token contains `/` is not
  stripped by the userinfo rule and relies on `mask_secrets` alone.

## Cache

Results are cached per resolved path for 5 minutes (`STATUS_TTL_SEC`) on an injectable
monotonic clock; failures are cached too. The read policy is asked before a cached answer
is returned. At most 512 entries (`MAX_CACHE_ENTRIES`; expired, else the oldest, are
dropped). `clear_cache()` drops everything.

## 日本語の要約

`stiracore.diagnostics.repo_inspect` は、Git リポジトリを読み取り専用で調べるライブラリ
（#671 第 1 段階）。UI・ルート・設定ファイル・fetch は無い（後の段階）。実行する git は上の
表の固定の引数列だけで、シェルを使わず、`GIT_*` を引き継がず、`-c` で fsmonitor・フック・
フィルタ・通信・ページャ・署名検証・色を止め、`--git-dir` / `--work-tree` に検査済みのパスを
固定する（`core.worktree` で外へ移せない）。最初に `rev-parse` で git 側の解決結果も確かめる。
タイムアウト 10 秒、出力 1 MiB で打ち切り、`inspect_all` 全体は 60 秒まで。
パスは読み取りポリシー（`check_read`）を通ったものだけ。設定パス・走査中の項目・`.git`・
`commondir` のリンクは辿らない（`commondir` がリンクなら拒否）が、git 自身は `.git` 内部の
リンクを辿る。ネットワークパスとして検出するのは UNC 表記だけで、`/Volumes` の SMB や
Windows の割り当てドライブは検出しない。出力は表の欄だけで、コミットのメッセージ・本文・
差分は読まない。名前と URL は `mask_secrets` を通し、資格情報（scp 形式も）は除く。読み取り
ルート外の worktree のパスとエラー中のパスは `[outside read roots]` に置き換える。結果は 5 分
キャッシュ（時計は注入でき、返す前にポリシーを再確認、最大 512 件）。
**残るリスク**: フィルタ上書きの競合（ドライバの列挙と `status` の間に、git が読む設定ファイル
〔`config`・`config.worktree`・`include.path` 先〕へ `filter.<x>.clean` を書き足されると実行される
＝コード実行になりうる。書ける相手が前提で、キャッシュ無しの 1 回の検査ごとに 1 度）。
**決定（指揮官 2026-11-22）: 検知して結果を捨てる**（`status` 後に再列挙し、新しいドライバが残って
いれば捨てる。再列挙が失敗しても拒否する）。**捉えられないもの**: `status` の最中に足してすぐ
消されたドライバ（実行は済んでいる）。実行を防ぐ根本対策＝`status` を配管コマンドに分解する案は
**別 Issue（フォローアップ）**。ほかに、`include.path`・`alternates`・`worktrees/*/gitdir`・
グローバル設定経由の読み取り、60 秒の期限は repo 間でしか見ない点も残る。
