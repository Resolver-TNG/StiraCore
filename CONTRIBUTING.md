# Contributing to StiraCore

English | [日本語](CONTRIBUTING_ja.md)

Thank you for your interest. StiraCore is pre-alpha; interfaces still change.

## How contributions are taken in

- **Issues are welcome**: bugs, documentation gaps, questions and proposals.
- **Pull requests are welcome too.** For now this repository is a snapshot of the
  maintainer's development repository, so an accepted pull request is **re-applied there**
  and comes back in the next sync. Your authorship is kept with a `Co-authored-by:` trailer.
- Open an issue first for a large change or a new feature, so the design can be agreed
  before you write code. Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Development setup

Python 3.11 or later ([install](docs/install.md) covers Linux, macOS and Windows). In a venv:
`python -m pip install -e '.[dashboard,dev]'`.

Checks to run before a pull request (the same gates as CI):

```bash
python -m pytest tests/ dashboard/test_multimodal.py
node --test tests/*_test.cjs
ruff check .
ruff format --check .
python scripts/i18n_extract.py --check
npm install --no-save --no-package-lock jsdom axe-core
STIRA_REQUIRE_AXE=1 python -m pytest tests/test_a11y_axe.py tests/test_ui_contrast.py -q
```

## Design rules

- **Behaviour that costs or widens is off by default.** Anything that spends tokens or widens
  permissions is opt-in, unless an ADR decides otherwise with its measured cost (e.g.
  [ADR 0026](docs/adr/0026-memory-index-handoff-small-windows.md): the Memory Map and the
  handoff note). **Show it and let people choose:** a feature that only shows something and
  approves or runs nothing until a person presses or picks it (e.g. the YOLO button, static
  SVG on the Stage board) may be shown by default — with a way to turn it off
  ([ADR 0012 addendum 2026-10-24](docs/adr/0012-yolo-mode.md)).
- **Permissions never widen without the user's approval** (tool rules, approvals, YOLO).
- **Untrusted text stays data.** Tool results, fetched pages and memory are never promoted to
  instructions or control authority ([ADR 0022](docs/adr/0022-trust-labels-and-taint.md)).
- **No secrets in logs, prompts or tests.** Credentials are referenced by variable name.
- **Design decisions come with an ADR** in [docs/adr/](docs/adr/) (index:
  [docs/README.md](docs/README.md)). Background: [design philosophy](docs/design-philosophy.md).
- **Keep the docs in sync.** A new setting updates the tables in
  [configuration.md](docs/configuration.md) / [configuration_ja.md](docs/configuration_ja.md);
  UI text goes through both i18n catalogs (ja / en). `tests/test_docs_sync.py` checks this.

## Commits, pull requests and sign-off

- Small, focused changes with tests. The PR description says why, and lists the checks you
  ran with their results. A dummy secret in a test must look like one (`sk-test-…`).
- `#123`-style numbers in the docs and code are from the maintainer's private development
  tracker, not this repository's issues (see the README).
- Code is Apache-2.0 (inbound = outbound); `dashboard/stage/assets/` is CC BY 4.0. No CLA:
  we use the [DCO](https://developercertificate.org/), so sign off every commit with
  `git commit -s` (a `Signed-off-by:` line).

## Conduct

Be respectful and constructive: assume good faith, keep discussion on the work, no harassment.
