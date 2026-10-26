"""Minimal review CLI for ``semantic/staging/`` (#293), until the review UI (#193).

    python -m stiracore.control.staging_cli --base-dir DIR list
    python -m stiracore.control.staging_cli --base-dir DIR show KEY
    python -m stiracore.control.staging_cli --base-dir DIR approve KEY
    python -m stiracore.control.staging_cli --base-dir DIR reject KEY [--reason TEXT]
    python -m stiracore.control.staging_cli --base-dir DIR reindex

``list`` prints keys and scores only (no content); ``show`` prints one entry.
Approval moves the entry to its final home (``staging/X`` -> ``X``), never
over an existing entry, and regenerates ``semantic/index.md``; rejection is
recorded so Harvest does not propose the same source again. Keys and the
agent id go through ``MemoryStore`` validation (no ``..``, absolute paths or
symbolic links). Output is JSON on stdout; errors go to stderr. Exit codes:
0 ok, 1 refused (nothing written), 3 approved but the index is stale (repeat
the approve or run ``reindex``). Works on the files directly and calls no
model. It is an operator command: no agent tool runs it (exec-class tools are
hidden unless ``STIRA_ENABLE_EXEC_TOOLS`` is set), and the agent's file tools
cannot write ``semantic/`` (data-directory floor, #271).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from stiracore.control.harvest import write_semantic_index
from stiracore.control.staging_review import STAGING_PREFIX, StagingReview
from stiracore.tools.memory_store import MemoryLayer, MemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review Semantic entries waiting in staging")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="stira")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list staged entries (no content)")
    commands.add_parser("reindex", help="regenerate semantic/index.md")
    for name, text in (
        ("show", "print one staged entry"),
        ("approve", "promote a staged entry"),
        ("reject", "reject a staged entry"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("key", help="staged key, with or without the 'staging/' prefix")
        if name == "reject":
            command.add_argument("--reason", default="")
    return parser


def _summary(entry) -> dict:
    metadata = entry.metadata
    return {
        "key": entry.key,
        "title": metadata.get("title", entry.key),
        "score": metadata.get("score"),
        # Score band: "staging" (>= review threshold) or "auto" held for review.
        "band": metadata.get("harvest_band", "staging"),
        "source_id": metadata.get("source_id"),
    }


def _staged_key(key: str) -> str:
    return key if key.startswith(STAGING_PREFIX) else f"{STAGING_PREFIX}{key}"


# Exit codes: 1 = refused (nothing written); 3 = approved, but semantic/index.md is stale.
EXIT_REFUSED = 1
EXIT_INDEX_STALE = 3


def _reindex(store: MemoryStore) -> str | None:
    """Regenerate the index; return a fixed error code instead of raising.

    Any failure counts (OSError, sqlite3.Error, ValueError, ...): after an
    approval it means "approved, index stale". Only the type name is printed.
    """
    try:
        write_semantic_index(store)
    except Exception as error:  # noqa: BLE001 - classified, never echoed
        print(
            f"staging: E_INDEX_WRITE ({type(error).__name__}): semantic/index.md was not "
            "updated; run `staging_cli reindex` (or repeat the approve) to repair it",
            file=sys.stderr,
        )
        return "E_INDEX_WRITE"
    return None


def _approve(store: MemoryStore, review: StagingReview, key: str) -> tuple[int, dict]:
    target = key[len(STAGING_PREFIX) :]
    staged = store.read(MemoryLayer.SEMANTIC, key)
    existing = store.read(MemoryLayer.SEMANTIC, target)
    if staged is None:
        if existing is not None and existing.metadata.get("review") == "approved":
            # Already approved (e.g. an earlier run whose index update failed): repair the index.
            error = _reindex(store)
            payload = {"approved": key, "key": target, "already_approved": True}
            if error:
                return EXIT_INDEX_STALE, {**payload, "index": "stale", "error": error}
            return 0, {**payload, "index": "updated"}
        raise KeyError(f"No staged entry: {key}")
    if existing is not None:
        raise ValueError(f"E_TARGET_EXISTS: {target} already exists; not overwritten")
    promoted = review.approve(key)
    error = _reindex(store)
    payload = {"approved": key, "key": promoted.key}
    if error:
        return EXIT_INDEX_STALE, {**payload, "index": "stale", "error": error}
    return 0, {**payload, "index": "updated"}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code = 0
    try:
        store = MemoryStore(args.base_dir, args.agent_id)
        review = StagingReview(store)
        if args.command == "list":
            entries = sorted(review.list_staged(), key=lambda entry: entry.key)
            payload: dict = {"count": len(entries), "entries": [_summary(e) for e in entries]}
        elif args.command == "reindex":
            error = _reindex(store)
            code, payload = (
                (EXIT_INDEX_STALE, {"index": "stale"}) if error else (0, {"index": "updated"})
            )
        elif args.command == "show":
            key = _staged_key(args.key)
            entry = store.read(MemoryLayer.SEMANTIC, key)
            if entry is None:
                raise KeyError(f"No staged entry: {key}")
            payload = {**_summary(entry), "content": entry.content}
        elif args.command == "approve":
            code, payload = _approve(store, review, _staged_key(args.key))
        else:
            key = _staged_key(args.key)
            record = review.reject(key, reason=args.reason)
            payload = {"rejected": record["key"]}
    except (KeyError, ValueError) as error:
        message = error.args[0] if error.args else type(error).__name__
        print(f"staging: {message}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
