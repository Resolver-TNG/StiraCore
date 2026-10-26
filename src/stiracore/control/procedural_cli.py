"""Operator CLI for procedural-memory proposals (#144), like ``staging_cli`` (#293).

    python -m stiracore.control.procedural_cli --base-dir DIR list
    python -m stiracore.control.procedural_cli --base-dir DIR show KEY
    python -m stiracore.control.procedural_cli --base-dir DIR propose KEY \
        --source-turn ID --file PATH   (``-`` reads stdin)
    python -m stiracore.control.procedural_cli --base-dir DIR approve KEY
    python -m stiracore.control.procedural_cli --base-dir DIR reject KEY
    python -m stiracore.control.procedural_cli --base-dir DIR stage-legacy KEY

A proposal waits under ``procedural/staging/`` and is not used by the prompt;
``approve`` moves it to ``procedural/<key>`` and records ``source_turn``,
``approved_by`` and ``approved_at``. The approver is the OS account the CLI
runs as (``local:<account>``): on POSIX taken from the process UID
(``pwd.getpwuid(os.getuid())``), never from ``LOGNAME`` / ``USER``; on Windows,
which has no UID, from ``os.getlogin()`` (the OS call, not the environment; not
verified on a real Windows machine). If it cannot be determined, nothing is
approved (``E_APPROVER_UNAVAILABLE``). There is no option to name another
approver. ``show`` is the only command that prints content; ``list`` prints keys,
provenance, a SHA-256 and the length, plus the entries the prompt skips because
they have no provenance (``stage-legacy`` moves a pre-#144 one back to staging,
to be read with ``show`` and approved). The prompt reads procedural rules when
the server starts, so an approval takes effect at the next start. The breaker is
**not** checked here, unlike the review screen (which approves only at NORMAL, #463):
the level lives in the running server's engine and is not persisted (``neural-state.json``
holds the emotional state only, and this CLI may run with the server stopped), so there
is no live level to read. A CLI approval never changes a running prompt: the server
reads it at its next start (whatever the breaker level is then). Content (and
``source_turn``) that ``mask_secrets`` would change is refused.

Output is JSON on stdout; errors go to stderr. Exit codes: 0 ok, 1 refused
(nothing written), 3 written but its source was not removed (``approve``: the
rule is approved and the proposal is still in staging; ``stage-legacy``: the
proposal exists and the original is still there) — repeat the same command to
remove the leftover. Works on the files directly and calls no model. It is an
operator command: no agent tool runs it (exec-class tools are hidden unless
``STIRA_ENABLE_EXEC_TOOLS`` is set; with them enabled the agent runs as the
same OS user and this gate is not a boundary — run the agent as a different OS
user than the approver, see docs/tool-execution-paths.md), and the agent's file
tools cannot write ``memory/procedural`` (#144) or anything else outside the
data directory's writable names (#271).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from stiracore.control.procedural_review import CleanupError, ProceduralReview
from stiracore.procedural_provenance import provenance_problem, valid_field
from stiracore.tools.memory_store import MemoryStore

EXIT_REFUSED = 1
EXIT_CLEANUP = 3
E_APPROVER_UNAVAILABLE = "E_APPROVER_UNAVAILABLE"


def _account_name() -> str:
    """The OS account of this process; never an environment variable."""
    if os.name == "posix":
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    # Windows has no UID. os.getlogin() calls GetUserNameW (the process token's
    # user), not USERNAME. Not verified on a real Windows machine.
    return os.getlogin()


def operator_principal() -> str:
    """``local:<account>``, decided by the host. Raises ``ValueError`` (fail-closed)."""
    try:
        name = _account_name()
    except Exception as error:  # noqa: BLE001 - KeyError (no passwd entry), OSError, ...
        raise ValueError(
            f"{E_APPROVER_UNAVAILABLE} ({type(error).__name__}): the OS account could not "
            "be determined; nothing approved"
        ) from None
    principal = f"local:{name}"
    if not isinstance(name, str) or not name.strip() or not valid_field(principal):
        raise ValueError(f"{E_APPROVER_UNAVAILABLE}: unusable account name; nothing approved")
    return principal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review procedural-memory proposals")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="stira")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list proposals and unapproved entries (no content)")
    for name, text in (
        ("show", "print one proposal"),
        ("approve", "approve a proposal (approver = local OS user)"),
        ("reject", "delete a proposal"),
        ("stage-legacy", "move an entry without provenance back to staging"),
        ("propose", "stage a new proposal"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("key", help="procedural key, with or without 'staging/'")
        if name == "propose":
            command.add_argument("--source-turn", required=True, help="turn or episode id")
            command.add_argument("--file", required=True, help="content file, '-' for stdin")
    return parser


def _fingerprint(entry) -> dict:
    data = entry.content.encode("utf-8")
    return {"sha256": hashlib.sha256(data).hexdigest(), "length": len(entry.content)}


def _summary(entry) -> dict:
    metadata = entry.metadata
    return {
        "key": entry.key,
        **_fingerprint(entry),
        "source_turn": metadata.get("source_turn"),
        "proposed_at": metadata.get("proposed_at"),
        "approved_by": metadata.get("approved_by"),
        "approved_at": metadata.get("approved_at"),
    }


def _content(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        review = ProceduralReview(MemoryStore(args.base_dir, args.agent_id))
        if args.command == "list":
            staged = review.list_staged()
            skipped = review.unapproved()
            payload: dict = {
                "count": len(staged),
                "entries": [_summary(entry) for entry in staged],
                "unapproved": [
                    {
                        "key": entry.key,
                        "missing": provenance_problem(entry.metadata),
                        **_fingerprint(entry),
                    }
                    for entry in skipped
                ],
            }
        elif args.command == "show":
            entry = review.staged(args.key)
            payload = {**_summary(entry), "content": entry.content}
        elif args.command == "propose":
            entry = review.propose(args.key, _content(args.file), source_turn=args.source_turn)
            payload = {"proposed": entry.key}
        elif args.command == "approve":
            approver = operator_principal()  # before anything is read or written
            result = review.approve(args.key, approved_by=approver)
            payload = {
                "approved": result.entry.key,
                "written": result.written,
                **_summary(result.entry),
                "effective": "next start",
            }
        elif args.command == "reject":
            payload = {"rejected": review.reject(args.key)}
        else:
            payload = {"staged": review.stage_legacy(args.key).key}
    except CleanupError as error:
        # Written: not a refusal. Report what exists and how to finish.
        print(f"procedural: {error}; repeat the command to remove it", file=sys.stderr)
        key = "approved" if args.command == "approve" else "staged"
        payload = {key: error.entry.key, "leftover": error.leftover, "error": error.code}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return EXIT_CLEANUP
    except (KeyError, ValueError, OSError) as error:
        message = error.args[0] if error.args else type(error).__name__
        if isinstance(error, OSError):
            message = f"E_IO ({type(error).__name__})"
        print(f"procedural: {message}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
