"""The round ledger of a data directory, printed once (ADR 0028 §5; #757).

    python -m stiracore.control.grant_rounds_cli --data-dir DIR [--grant ID] [--last N]
        [--format table|json]

Reads ``<data dir>/grant-rounds.jsonl`` (and ``grant-rounds.1.jsonl``) under the ledger's
own safe-open and chain rules — nothing is rotated, appended or repaired — and prints the
``round`` records as a table with a one-line summary (rounds, wall hours, reported tokens,
``new`` rounds), or every record as JSON. The output is the ledger's allow-list: codes
and counts only (no goal, path, model text, URL or secret is in the file).

An operator command: no agent tool runs it; it calls no model and writes nothing. Exit
codes: 0 done, 1 the data directory was not found or the ledger cannot be used (its fixed
``round_ledger_*`` code is printed; no record is shown from an unusable ledger), 2 usage
error. A ledger that looks unusable is read again (twice at most) before it is reported:
a running server's append or rotation can land between this reader's steps. If a code is
still shown while the server is writing, run the command again.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from stiracore.grants import round_ledger

_GRANT_ID = re.compile(r"[0-9a-f]{32}")
_READS = 3  # one read and at most two more when the ledger looks unusable
_COLUMNS = (
    ("seq", "seq"),
    ("grant", "grant"),
    ("round", "round"),
    ("origin", "origin"),
    ("end_code", "end"),
    ("detail", "detail"),
    ("progress", "progress"),
    ("wall_ms", "wall_ms"),
    ("model_calls", "calls"),
    ("writes", "writes"),
    ("files_changed", "files"),
    ("tokens_in", "in"),
    ("tokens_out", "out"),
    ("stop", "stop"),
)


def _cell(record: dict, key: str) -> str:
    value = record[key]
    if value is None:
        return "-"
    if key == "grant":
        return value[:8]
    if key == "round":
        return f"{value}/{record['of_max']}"
    return str(value)


def to_table(records: list[dict]) -> str:
    """The ``round`` records as a fixed-column table and the summary line."""
    rounds = [r for r in records if r["k"] == "round"]
    rows = [[title for _key, title in _COLUMNS]]
    rows += [[_cell(record, key) for key, _title in _COLUMNS] for record in rounds]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows
    ]
    total = round_ledger.summary(rounds)
    lines.append(
        "summary: rounds={rounds} hours={hours} tokens_in={tokens_in} tokens_out={tokens_out}"
        " new={new}".format(**{k: "-" if v is None else v for k, v in total.items()})
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="The round ledger of a data directory (#757)")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--grant", default=None, help="only this grant (its 32-hex id)")
    parser.add_argument("--last", type=int, default=None, help="only the newest N records")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.grant is not None and not _GRANT_ID.fullmatch(args.grant):
        parser.error("--grant must be a 32-hex grant id")
    if args.last is not None and not 1 <= args.last <= 100_000:
        parser.error("--last must be from 1 to 100000")
    if not args.data_dir.is_dir():
        print("error: the data directory was not found", file=sys.stderr)
        return 1
    # A running server may append or rotate between this reader's steps, which looks like
    # an unsafe file or a gap for one read: read again before reporting it (#761). A
    # ledger that is really unusable gives its code every time.
    for _attempt in range(_READS):
        try:
            ledger = round_ledger.RoundLedger(args.data_dir, rotate=False)
            reason = ledger.unhealthy_reason
        except OSError:
            ledger, reason = None, "round_ledger_unreadable"
        if reason is None:
            break
    records = [] if ledger is None or reason is not None else ledger.records(args.grant)
    if args.last is not None:
        records = records[-args.last :]
    if args.format == "json":
        answer = {
            "ledger": reason or "ok",
            "records": records,
            "summary": round_ledger.summary(records),
        }
        print(json.dumps(answer, indent=2))
    else:
        if reason is not None:
            print(f"ledger: {reason}")
        print(to_table(records), end="")
    return 1 if reason is not None else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
