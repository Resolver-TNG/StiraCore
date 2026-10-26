# Public release snapshot

English | [日本語](release_ja.md)

The public repository is created from a **snapshot without history** (#107): tracked files
minus [release-exclude.txt](../release-exclude.txt), committed once in a new `git init`.
The script builds it locally and **never pushes**.

```bash
python scripts/make_release_snapshot.py --persona-report        # temp dir outside the repo
python scripts/make_release_snapshot.py --out /tmp/rel --no-smoke  # quicker, no venv
python scripts/check_links.py                                   # links in the public set
```

What it does, in order:

1. Copies `git ls-files` minus the exclude list (untracked local state never enters).
   Markdown links to excluded files become plain text in the copy only.
2. `git init -b main` and one commit, "Initial public release".
3. Scan for secrets, internal IPs, home-directory paths, e-mail addresses and internal
   hosts. Personal and company names come from `~/.config/stira-release/denylist.txt`
   (one word per line, outside the repository); without it the script warns and skips
   them. The report lists kind, count and file paths — **never the matched values**.
   Hits under `tests/` are dummy values used by the masking and SSRF tests. Two kinds
   **fail the run**: a denylist word (`denylist_word_N`) and `tracker_link` — an issue or
   pull-request link of the owner's repositories (`github.com/Resolver-TNG/<repo>/issues/N`
   or `/pull/N`). Issue numbers are written as plain `#N` (they are private development
   tracker IDs, see the README).
4. Relative link check over the snapshot.
5. Clean temporary venv: `pip install -e .[dev]`, then `pytest tests/attack
   tests/test_server_startup_smoke.py` (no cloud, no AWS variables, empty `HOME`).
   [agent-setup.md](agent-setup.md) uses the same commands. Afterwards everything the smoke
   generated is removed (`git clean -fdx` and `git checkout -- .` in the snapshot:
   `__pycache__`, `*.egg-info`, `.pytest_cache`, `.ruff_cache`, `node_modules`, …), also
   with `--no-smoke`.
6. Previous-persona words: the snapshot must not name the game-derived persona that the
   sample persona Resolver replaced (the list is `PERSONA_WORDS` in `scripts/release_scan.py`,
   stored encoded so the file does not match itself). The report shows `persona_words` (count
   and paths); any hit fails the run.
7. `--persona-report`: lines with an earlier persona name (file:line), to plan a persona swap.
   `examples/personas/resolver/` is kept as is.
8. File-set check: the snapshot must hold exactly `git ls-files` minus the exclude list.
   `file_set.extra` / `file_set.missing` in the report list the differences; any fails the run.
   The report also records the development commit (`source_commit`, `source_date`,
   `source_dirty`), which `scripts/publish_snapshot.py` uses.

**Rename to StiraCore (no migration code).** The chat server's default data directory is now
`.local/stira/` and its agent id `stira`. To keep an existing directory, stop the server and
move it: `mv .local/<previous name> .local/stira`, then
`mv .local/stira/memory/agents/<previous id> .local/stira/memory/agents/stira`.

## Denylist (personal, company and internal names)

The file lives outside the repository and is never committed. Create it once:

```bash
mkdir -p ~/.config/stira-release
$EDITOR ~/.config/stira-release/denylist.txt
```

Write one word per line (personal names, company names, internal project, host or team
names). Matching is case-insensitive; empty lines and lines starting with `#` are ignored,
so a file of comments only is a valid template. The report shows `denylist_words` (how
many words were read) and hits as `denylist_word_N` (the line number among the words),
never the word itself. `denylist_words: 0` (a warning is printed) means the template has
not been filled in yet. Do not put secrets in it.

The JSON report is printed and saved as `release-report.json` next to the snapshot.
Exit code 1 means broken links, a previous-persona word, a blocking scan hit, a failing smoke
or a file-set difference. Creating the public repository, the first push and tagging are
manual steps for the maintainer.

## Publishing updates

`scripts/publish_snapshot.py` copies a snapshot into a **local clone** of the public
repository `Resolver-TNG/StiraCore`. It uses the standard library and git only. The default
is a dry run: it checks and compares, and leaves the clone as it is. `--commit` copies and
commits; `--push` (only together with `--commit --yes`) runs `git push origin main`, the
only step that uses the network, after printing what will be pushed. No force push.

It refuses (exit 1, with the reasons) unless: `release-report.json` has no broken links, no
previous-persona word, a passing smoke (a `--no-smoke` report needs `--allow-no-smoke`),
`denylist_words` ≥ 1, no blocking scan hit, no file-set difference and a committed
development tree; the snapshot directory is clean and at the reported commit; the clone's
`origin` is `Resolver-TNG/StiraCore` (`--expect-remote` to change), its working tree is clean
and it is on `main`. Deleted files are removed from the clone. The commit message is
`Sync from development snapshot <short SHA> (<date>)` with change counts as its only body.
Author and committer come from the clone's git config and must be a GitHub noreply address;
the script does not invent one.

**First publication (only after the maintainer's explicit go):**

1. On GitHub, create the empty public repository (no README, licence or `.gitignore`).
2. Clone it, `git switch -c main` if needed, and set `user.name` and a noreply `user.email`.
3. `python scripts/make_release_snapshot.py --out /tmp/rel` (with the smoke); check the report.
4. `python scripts/publish_snapshot.py --snapshot /tmp/rel --public-clone <clone>`, then the
   same with `--commit`.
5. Review the commit (`git show --stat`, a look through the tree) and push it by hand
   (`git push origin main`).
6. Settings: enable **Private vulnerability reporting** (Security → [SECURITY.md](../SECURITY.md)
   depends on it), set the description and topics. Tags and release notes are manual.

**Regular sync:** `git pull` in the clone, build a new snapshot, dry run, then
`--commit` and push (by hand, or `--commit --push --yes`).

**Pull requests on the public repository** are re-applied in the development repository,
keeping the author with a `Co-authored-by:` trailer, and come back with the next sync
([CONTRIBUTING.md](../CONTRIBUTING.md)). Close the public pull request with a short note once
the sync contains it.
