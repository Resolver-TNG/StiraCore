"""Find and delete orphaned episode emotion records (#364).

    python -m stiracore.control.emotion_cli --base-dir DIR orphans          # dry run
    python -m stiracore.control.emotion_cli --base-dir DIR orphans --apply  # delete

An emotion record (``<memory_dir>/emotion/<digest>.json``) is an orphan when its episode
is gone (``no-episode``), no longer matches it (``stale``: rewritten, or not v2 any more),
its name is not the digest of its key (``misnamed``), it cannot be read (``unreadable``:
broken, oversized, a link), or it is a temporary file older than five minutes
(``temporary``). A directory there is reported (``not-a-file``) and never removed. An
episode that cannot be read is not judged.

The dry run (default) writes nothing. ``--apply`` deletes the orphans, each one logged to
``<memory_dir>/emotion-log.jsonl`` (file name, reason, time; never values or keys)
*before* it is removed; a log failure stops the run with exit 1. Output is JSON with
file names and reasons only. Exit codes: 0 done, 1 refused or failed, 2 usage error.
An operator command: no agent tool runs it, and the agent's file tools cannot reach
``emotion/`` (data-directory floor). Calls no model.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from stiracore.tools.episodic_emotion import delete_orphan_records, find_orphan_records
from stiracore.tools.memory_store import MemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Orphaned episode emotion records (#364)")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="stira")
    commands = parser.add_subparsers(dest="command", required=True)
    orphans = commands.add_parser("orphans", help="list (default) or delete orphaned records")
    orphans.add_argument("--apply", action="store_true", help="delete them (logged)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = MemoryStore(args.base_dir, args.agent_id)
        found = find_orphan_records(store)
        removed = delete_orphan_records(store, found) if args.apply else []
    except (OSError, ValueError) as error:
        print(f"emotion: E_EMOTION_ORPHANS ({type(error).__name__})", file=sys.stderr)
        return 1
    payload = {
        "applied": bool(args.apply),
        "orphans": [{"file": o.name, "reason": o.reason, "deletable": o.deletable} for o in found],
        "count": len(found),
        "deleted": len(removed),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
