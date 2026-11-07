# Linux CI in containers (a home Docker host, no GitHub minutes)

The Linux half of `.github/workflows/ci.yml`, runnable on any Docker host: the pytest files are split into **shards**,
each shard runs in **its own container** (own copy of the sources, own network namespace and temp dirs), and a
**misc** container runs the non-pytest steps (PowerShell control-script tests, Node tests, lint and format on 3.12,
the accessibility gate on 3.12, the provider extras).

| File | What |
|---|---|
| `Dockerfile` | Ubuntu 24.04 (the OS of `ubuntu-latest`) + git, lsof, Node 22, PowerShell, Python 3.11 / 3.12 via uv |
| `images.sh` | `list`, `build [3.11 3.12] [--no-cache]`, `prune` for the `stira-ci:py3.11` / `py3.12` images |
| `run_sharded.sh` | `run_sharded.sh <py> <source dir> [shards=4]` — runs the shards + misc and prints one result table |
| `normalize_modes.sh` | sets every tracked file to the mode git records (a NAS file system with inherited ACLs made all files executable and ruff EXE002 failed) |
| `nas_run.sh` | from your machine: `nas_run.sh [--py 3.11\|3.12\|both] [--ref origin/main] [--shards 4]` — clones the ref, ships it to the Docker host over ssh, runs it |

## Quick start

```bash
ci/linux/nas_run.sh --py 3.11 --ref origin/main      # STIRA_CI_HOST (default: nas), STIRA_CI_DIR (default: /volume3/docker/ci-work)
```

On the Docker host itself: `./images.sh build` once, then `./run_sharded.sh 3.11 /path/to/checkout 4`.

## Why containers and not `pytest -n`

`pytest-xdist` inside one container turned 6 minutes into **102 failures and 26 errors** (2026-11-07): many tests use fixed
ports, lock files, the repository directory or process-wide state. Separate containers share nothing, so the tests stay
untouched and the result equals the serial run.

## Guard against silently missing tests

`run_sharded.sh` finds the test files with `tests/**/test_*.py` (sub-directories such as `tests/attack/` included) and, at
the end, compares **the number of tests the shards ran with the number a plain `pytest --collect-only` sees**. A mismatch
fails the run. (The first version globbed `tests/test_*.py` only and silently skipped about 55 tests.)

## Things the tests need from the machine (each one cost a failed run)

| Missing | What broke |
|---|---|
| `lsof` | `scripts/stira_ctl.sh` verifies the data-directory owner with `lsof`; without it the *already owns* branch is never taken (3 tests) |
| `git config safe.directory` | the container user is not the owner of the mounted sources, `git ls-files` is refused (3 release-tool tests) — set in the scripts |
| a `.git` directory | the release-tool tests run git; a plain `git archive` has none |
| no macOS `._*` files | `tar` on macOS adds AppleDouble files; the tests that scan the sources fail on them (~80 tests). Use `COPYFILE_DISABLE=1` (done in `nas_run.sh`) |
| `cp -a /src/. /work/` | `/work` exists already (WORKDIR); `cp -a /src /work` made `/work/src` |
| file modes | on a NAS file system with inherited ACLs every file is executable and ruff's EXE002 fails the 3.12 lint; `normalize_modes.sh` resets the modes to git's |
| Debian instead of Ubuntu | different `ps`/userland details; the image is Ubuntu 24.04 like `ubuntu-latest` |

## Numbers (main 84612858, Python 3.11, 4 shards, Pentium Gold 8505 / 6 threads / SSD)

| | time |
|---|---|
| serial, one container | ~19 min (pytest only) |
| 4 shards + misc | **~8 min** (shards 4m45s – 7m33s; the slowest one decides) |
| GitHub-hosted ubuntu-latest | ~25 min |

CPU stays around 80% and memory under 20% on the host with 4 shards (1 CPU, 3 GB each) + 1 misc container.

## Not covered

macOS and Windows cannot run in a Linux container. `ci.yml` keeps macOS; Windows is out of CI for now (#662, #677).
Coverage (`--cov`) of the 3.12 job is not collected here.
