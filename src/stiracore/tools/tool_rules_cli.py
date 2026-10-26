"""``stira tool-rules status|lock|init-anchor`` — the tool-rule steps of a restore (#273).

A restored data directory can bring back older, looser tool rules whose file and head
agree with each other, so nothing locks them on its own (ADR 0010, addendum #273).

* ``lock`` puts the lock marker next to the rules: the next load — the next start, or
  the next call of a running server — is locked (every tool Never) until a person saves
  the rules again with the recovery word ``RECOVER`` on ``/settings#sec-tools``.
* ``init-anchor`` is the only way, besides a RECOVER save, to create the opt-in anchor
  (``STIRA_TOOL_RULES_ANCHOR_DIR``): it records the current rules, and only when they
  check out, nothing is locked and no anchor exists yet (a missing anchor is never
  re-created by a load; it locks with ``anchor_missing``).
* ``status`` only reads.

These are host commands for a person at the server's console. The agent has no tool for
them: the exec class (``shell``…) is hidden unless ``STIRA_ENABLE_EXEC_TOOLS`` is set and
then asks for every call, and the files they write (rules, head, marker, anchor) are on
the agent's write floor (``ConfigStore.protected_files``). Nothing here prints rule
values.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from . import tool_config as tc

RESTORED_REASON = "restored_backup"


class UnsafePath(ValueError):
    """The ``--file`` argument is refused (fixed phrase, no file content)."""


def add_parser(commands) -> None:
    rules = commands.add_parser(
        "tool-rules", help="tool-rule integrity: status, lock after a restore, init anchor"
    )
    sub = rules.add_subparsers(dest="tool_rules_command", required=True)
    for name, text in (
        ("status", "show generation, head, lock marker and anchor (changes nothing)"),
        ("lock", "lock the rules until a human RECOVER save (run after restoring a backup)"),
        ("init-anchor", f"create the {tc.ANCHOR_ENV} anchor from the current, unlocked rules"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument(
            "--file", type=Path, default=tc.DEFAULT_PATH, help="tool-config.json of the server"
        )
        command.add_argument(
            "--data-dir",
            type=Path,
            action="append",
            default=[],
            help="the server's data directory (the anchor must be outside it; repeatable)",
        )
        command.add_argument("--json", action="store_true", help="print JSON")


def checked_path(path: Path, *, must_exist: bool) -> Path:
    """``--file``: its directory must exist (a real directory); the file itself, when it
    exists (or must), a regular file and not a symlink; so must the lock marker."""
    path = Path(path).expanduser()
    parent = path.parent
    try:
        if not stat.S_ISDIR(os.stat(parent).st_mode):
            raise UnsafePath("the directory of --file is not a directory")
    except FileNotFoundError:
        raise UnsafePath("the directory of --file does not exist") from None
    for candidate, required in ((path, must_exist), (tc.latch_path(path), False)):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            if required:
                raise UnsafePath(f"{candidate.name} does not exist") from None
            continue
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath(f"{candidate.name} is a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"{candidate.name} is not a regular file")
    return path


def _generation_of(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("generation") if isinstance(data, dict) else None
    return value if type(value) is int else None


def _marker_reason(path: Path) -> str | None:
    marker = tc.latch_path(path)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("reason") or "latched"
    except (OSError, ValueError, AttributeError):
        return "latched"


def status(path: Path, data_roots=()) -> dict:
    """Read-only: what a load would see, without recording or latching anything."""
    try:
        head = tc._read_head(path)
        head_state = head["generation"] if head else None
    except ValueError:
        head_state = "unreadable"
    anchor: dict | None = None
    if tc.anchor_dir() is not None:
        try:
            entry = tc.read_anchor(path)
            anchor = {"generation": entry["generation"] if entry else None}
        except tc.AnchorUnavailable as error:
            anchor = {"generation": error.reason}
        anchor["inside_data_dir"] = tc.anchor_inside_data_dir(path, data_roots)
    reason = tc.ToolConfig.check_only(path)
    if reason is None and anchor is not None and anchor["inside_data_dir"]:
        reason = "anchor_misconfigured"
    return {
        "file": str(path),
        "exists": path.exists(),
        "generation": _generation_of(path),
        "head_generation": head_state,
        "lock_marker": _marker_reason(path),
        "anchor": anchor,
        "locked": reason is not None,
        "lock_reason": reason,
    }


def lock(path: Path, reason: str = RESTORED_REASON) -> tuple[bool, str]:
    """Write the lock marker (kept if one exists). Returns (newly locked, reason)."""
    path = checked_path(path, must_exist=False)
    current = _marker_reason(path)
    if current is not None:
        return False, current
    tc._atomic_write(tc.latch_path(path), {"reason": reason})
    return True, reason


def init_anchor(path: Path, data_roots=()) -> int:
    """Create the anchor from the current rules; returns their generation.

    Refused (ValueError, fixed phrase) when the variable is unset, the anchor would be
    inside a data root, an anchor (even an unusable one) already exists, the rules are
    locked or absent, or they do not check out. Never re-creates an anchor: after one
    went missing, only a RECOVER save writes it again.
    """
    path = checked_path(path, must_exist=True)
    with tc._RULES_LOCK, tc._anchor_file_lock(path):  # also against a running server
        return _init_anchor(path, data_roots)


def _init_anchor(path: Path, data_roots) -> int:
    target = tc.anchor_path(path)
    if target is None:
        raise ValueError(f"{tc.ANCHOR_ENV} is not set")
    if tc.anchor_inside_data_dir(path, data_roots):
        raise ValueError(f"{tc.ANCHOR_ENV} is inside the data directory")
    if os.path.lexists(target):
        raise ValueError("an anchor already exists; a RECOVER save is the only way to replace it")
    if tc.latched_reason(path) is not None:
        raise ValueError("the rules are locked; recover them with RECOVER (that writes the anchor)")
    config, reason = tc.ToolConfig._load_checked(path, record=False)
    if reason is not None or config is None or config.generation < 1:
        raise ValueError("the rules do not check out; recover them with RECOVER")
    # The checked rules (with ``page_check_ports``), as the head and every load digest
    # them: ``as_dict`` would leave the ports out and the next load would lock.
    rules = config.rules_dict()
    # A new install identity, bound to the head first (same generation, same rules), then
    # kept in the anchor: a file + head from anywhere else no longer matches (#410 review).
    config.install_id = tc.new_install_id()
    config._write_head()
    digest = tc._rules_digest(rules, config.generation)
    tc._write_anchor(path, config.generation, digest, rules, config.install_id, create_only=True)
    return config.generation


def run(args: argparse.Namespace) -> int:
    roots = tuple(args.data_dir or ())
    try:
        path = checked_path(args.file, must_exist=args.tool_rules_command == "init-anchor")
    except UnsafePath as error:
        print(f"stira tool-rules: {error}", file=sys.stderr)
        return 2
    if args.tool_rules_command == "status":
        report = status(path, roots)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            state = f"LOCKED ({report['lock_reason']})" if report["locked"] else "not locked"
            print(f"tool rules: {state}")
            print(f"  generation: {report['generation']}  head: {report['head_generation']}")
            if report["anchor"] is not None:
                print(f"  anchor generation: {report['anchor']['generation']}")
                if report["anchor"]["inside_data_dir"]:
                    print(f"  ERROR: {tc.ANCHOR_ENV} is inside the data directory")
            else:
                print(f"  anchor: off ({tc.ANCHOR_ENV} not set)")
        return 1 if report["locked"] else 0
    if args.tool_rules_command == "init-anchor":
        try:
            generation = init_anchor(path, roots)
        except (ValueError, OSError) as error:
            print(f"stira tool-rules: {error}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"anchored": True, "generation": generation}))
        else:
            print(f"anchor created at generation {generation}.")
        return 0
    try:
        created, reason = lock(path)
    except (UnsafePath, OSError) as error:
        print(f"stira tool-rules: could not lock: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"locked": True, "created": created, "reason": reason}))
    else:
        verb = "locked" if created else "already locked"
        print(f"tool rules {verb} ({reason}): every tool is Never until a person saves the")
        print("rules with the recovery word RECOVER on /settings#sec-tools (operations.md §4).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stira", description="StiraCore tool rules")
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser(commands)
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
