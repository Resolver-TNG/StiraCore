#!/usr/bin/env python3
"""Machine verdict of one Linux CI gate log (the output of ``ci/linux/nas_run.sh``).

Why: ``--py both`` prints a result block for 3.11 and then one for 3.12, and ruff runs only in the 3.12
``misc`` container. Reading the first ``LINUX CI: PASS`` by eye (2026-11-21) merged a branch whose 3.12
block failed a few minutes later. This script returns PASS only when the whole run is over and every
part of it is green:

* the wrapper's ``=== done rc=0`` line is there (the run has ended),
* one ``=== results (py...)`` block per expected Python version,
* in every block: every ``shardN rc=0`` (at least one), ``misc rc=0``, a coverage guard whose two
  numbers are equal, and ``LINUX CI: PASS``,
* no ``step failed`` and no ``LINUX CI: FAIL`` anywhere,
* when ``--sha`` is given, the ref line (``ref NAME = <short sha> ...``) matches it.

Exit status: 0 PASS, 1 FAIL, 2 INCOMPLETE (the run has not ended yet, or a block is missing without a failure).
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"
EXIT = {PASS: 0, FAIL: 1, INCOMPLETE: 2}

_DONE = re.compile(r"^=== done rc=(\d+)")
_BLOCK = re.compile(r"^=== results \(py([\d.]+)")
_SHARD = re.compile(r"^shard(\d+) rc=(\d+)")
_MISC = re.compile(r"^misc rc=(\d+)")
_GUARD = re.compile(
    r"coverage guard: shards accounted for (\d+) tests, plain collection sees (\d+)"
)
_REF = re.compile(r"^ref \S+ = ([0-9a-f]{7,40})\b")


def _split_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        m = _BLOCK.match(line)
        if m:
            blocks.append((m.group(1), []))
        elif _DONE.match(line):
            break
        elif blocks:
            blocks[-1][1].append(line)
    return blocks


def _check_block(version: str, body: list[str]) -> list[str]:
    why: list[str] = []
    shards = [int(m.group(2)) for line in body if (m := _SHARD.match(line))]
    if not shards:
        why.append(f"py{version}: no shard result")
    elif any(rc != 0 for rc in shards):
        why.append(f"py{version}: a shard failed (rc {shards})")
    misc = [int(m.group(1)) for line in body if (m := _MISC.match(line))]
    if misc != [0]:
        why.append(f"py{version}: misc rc {misc or 'missing'}")
    guards = [m.groups() for line in body if (m := _GUARD.search(line))]
    if not guards:
        why.append(f"py{version}: coverage guard line missing")
    elif any(a != b for a, b in guards):
        why.append(f"py{version}: coverage guard mismatch {guards}")
    if not any(line.strip() == "LINUX CI: PASS" for line in body):
        why.append(f"py{version}: no 'LINUX CI: PASS'")
    return why


def verdict(text: str, versions: int = 2, sha: str | None = None) -> dict:
    lines = text.splitlines()
    fail: list[str] = []
    wait: list[str] = []

    for line in lines:
        if "step failed" in line or line.startswith("LINUX CI: FAIL"):
            fail.append(line.strip()[:160])

    refs = [m.group(1) for line in lines if (m := _REF.match(line))]
    if sha:
        if not refs:
            fail.append("no 'ref ... = <sha>' line: cannot tell which commit was tested")
        elif not any(sha.startswith(r) or r.startswith(sha) for r in refs):
            fail.append(f"tested commit {refs[0]} is not the expected {sha[:12]}")

    done = [int(m.group(1)) for line in lines if (m := _DONE.match(line))]
    blocks = _split_blocks(lines)
    for version, body in blocks:
        fail.extend(_check_block(version, body))

    if done and done[-1] != 0:
        fail.append(f"wrapper finished with rc={done[-1]}")
    if not done:
        wait.append("no '=== done' line: the run has not ended")
    if len(blocks) != versions:
        (wait if not done else fail).append(f"{len(blocks)} result block(s), expected {versions}")

    if fail:
        result = FAIL
    elif wait:
        result = INCOMPLETE
    else:
        result = PASS
    return {
        "verdict": result,
        "reasons": fail + wait,
        "versions_seen": [v for v, _ in blocks],
        "tested_ref_sha": refs[0] if refs else None,
        "expected_sha": sha,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", type=Path)
    ap.add_argument(
        "--versions", type=int, default=2, help="expected number of Python versions (both = 2)"
    )
    ap.add_argument("--sha", default=None, help="commit the run must have tested")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)
    result = verdict(
        args.log.read_text(encoding="utf-8", errors="replace"), args.versions, args.sha
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['verdict']}" + "".join(f"\n  - {r}" for r in result["reasons"]))
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
