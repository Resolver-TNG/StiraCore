"""``stira memory retention|forget|restore|removed`` (#294).

Also ``python -m stiracore.control.memory_cli ...``. Operator commands: the agent
has no tool that runs them (exec-class tools are off by default).

* ``retention`` — report of every store (read only). ``--apply`` archives and removes
  what is over the ``memory_retention`` limits of ``agent-config.json`` (none by
  default, except memory-job plan files older than 90 days).
* ``forget --id <layer>/<key> | --query <text>`` — where the request is found
  (read only). ``--apply`` deletes every reached copy, the archived copies included
  (irreversible), as a resumable job; ``--resume <job>`` finishes a failed one.
* ``restore <id>`` — put back what one ``retention --apply`` removed (explicit,
  audited). A delete has nothing to restore.
* ``removed`` / ``jobs`` — list removal records / delete jobs (no text).
* ``tag --id <layer>/<key> --retention invariant|derived|none [--half-life-days N]`` —
  a memory's importance tag for forgetting (#173; preview by default). ``--apply``
  writes it under the owner lock and audits it (digests only). Only a human sets a tag:
  the agent has no tool for it, an Episodic tag counts only when this command recorded
  it, and only this command lifts it (an edit of the file does not:
  :mod:`stiracore.memory_importance`).

Exit codes: 0 done, 1 something was refused or failed, 2 usage, 3 the data directory
is owned by a running server (``--apply`` and ``restore`` hold its owner lock, #285).
An error no command formats (a link refused by the pre-apply recovery, #402) prints
``{"refused": {"code": "E_... (Type)"}}`` and exits 1: never its message or a traceback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
from stiracore.control.forgetting_archive import ForgettingArchive, ForgettingArchiveError
from stiracore.control.memory_forget import (
    ForgetRequest,
    _error_code,
    apply_forget,
    list_jobs,
    plan_forget,
    resume_forget,
)
from stiracore.control.memory_layout import (
    DataLayout,
    public_reasons,
    read_bytes,
    recover_for_apply,
)
from stiracore.control.memory_removal import describe, restore
from stiracore.control.memory_retention import (
    RetentionPolicy,
    apply_retention,
    retention_report,
)
from stiracore.control.removal_archive import RemovalArchive
from stiracore.memory_importance import (
    DERIVED,
    HALF_LIFE_MAX_DAYS,
    HALF_LIFE_MIN_DAYS,
    INVARIANT,
    bind_retention,
    binding_state,
    retention_binding,
    retention_of,
    tag_metadata,
)
from stiracore.memory_origin import record_origin, verified_origin
from stiracore.tools.memory_store import MemoryLayer, MemoryStore


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="the server's data directory (e.g. .local/stira)",
    )
    parser.add_argument("--agent-id", default="stira")


def add_parser(commands) -> None:
    memory = commands.add_parser(
        "memory", help="memory retention report, delete requests, restore, tags (#294, #173)"
    )
    sub = memory.add_subparsers(dest="memory_command", required=True)
    retention = sub.add_parser(
        "retention", help="sizes, ages and over-limit candidates of every store (read only)"
    )
    _common(retention)
    retention.add_argument(
        "--config", type=Path, help="agent-config.json (default: <data-dir>/agent-config.json)"
    )
    retention.add_argument(
        "--apply",
        action="store_true",
        help="archive, then remove what is over the configured limits (undo: restore <id>)",
    )
    forget = sub.add_parser(
        "forget", help="find (and with --apply remove) every copy of a memory or phrase"
    )
    _common(forget)
    forget.add_argument(
        "--id", action="append", default=[], metavar="LAYER/KEY", help="e.g. episodic/chat-1"
    )
    forget.add_argument(
        "--query", action="append", default=[], metavar="TEXT", help="literal, case-insensitive"
    )
    forget.add_argument(
        "--apply",
        action="store_true",
        help="delete every reached copy, archived copies included (irreversible; a job)",
    )
    forget.add_argument(
        "--resume",
        metavar="JOB_ID",
        help="finish a delete job (give the same --id/--query again: the job keeps digests)",
    )
    undo = sub.add_parser("restore", help="put back what one --apply removed (audited)")
    _common(undo)
    undo.add_argument("removal_id", metavar="ID")
    listing = sub.add_parser("removed", help="list removal records (no text)")
    _common(listing)
    jobs = sub.add_parser("jobs", help="list delete jobs: state, attempts, counts (no text)")
    _common(jobs)
    tag = sub.add_parser(
        "tag",
        help="a memory's retention tag for forgetting: invariant / derived / none (#173)",
    )
    _common(tag)
    tag.add_argument(
        "--id", required=True, metavar="LAYER/KEY", help="semantic/<key> or episodic/<key>"
    )
    tag.add_argument(
        "--retention",
        required=True,
        choices=(INVARIANT, DERIVED, "none"),
        help="invariant: never forgotten by maintenance; derived: fades; none: remove the tag",
    )
    tag.add_argument(
        "--half-life-days",
        type=int,
        metavar="N",
        help=f"derived Semantic entries: half-life ({HALF_LIFE_MIN_DAYS}-{HALF_LIFE_MAX_DAYS})",
    )
    tag.add_argument(
        "--apply", action="store_true", help="write the tag (owner lock; audited, digests only)"
    )


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _locked(layout: DataLayout, action):
    lock = DataDirOwnerLock(layout.data_dir)
    try:
        lock.acquire()
    except DataDirLockError as error:
        owner = getattr(error, "owner_pid", None)
        _print({"refused": {"code": "E_DATA_DIR_OWNED", "owner_pid": owner}})
        return 3
    try:
        return action()
    finally:
        lock.release()


def _stash_hours(data_dir: Path) -> float | None:
    from stiracore.agent.context_management import ContextManagementSettings
    from stiracore.config.models import GatewayConfig

    path = data_dir / "gateway-config.json"
    try:
        gateway = GatewayConfig.load(path)
        settings = ContextManagementSettings.from_options(
            gateway.context_management, gateway.context_management_options
        )
    except (OSError, ValueError, TypeError):
        return None
    return settings.retention_hours


def _retention(args, layout: DataLayout) -> int:
    from stiracore.config.models import AgentConfig

    path = args.config or layout.data_dir / "agent-config.json"
    try:
        config = AgentConfig.load(path)  # read only; a missing file = no limits
        policy = RetentionPolicy.from_options(config.memory_retention)
    except (OSError, ValueError, TypeError) as error:
        print(f"stira memory retention: cannot read {path.name}: {error}", file=sys.stderr)
        return 2
    report, _plan = retention_report(layout, policy, stash_hours=_stash_hours(layout.data_dir))
    if not args.apply:
        _print({"dry_run": True, **report})
        return 0

    def run() -> int:
        # Planned again under the lock: nothing changed between plan and apply.
        recover_for_apply(layout, layout.index, layout.turns)
        fresh, fresh_plan = retention_report(layout, policy)
        result = apply_retention(layout, fresh_plan)
        _print(
            {
                "dry_run": False,
                "will_remove": fresh["will_remove"],
                # Area labels and digests only (the report shows the locations).
                "skipped": public_reasons(layout, fresh["skipped"]),
                "result": result,
            }
        )
        return 0 if result["applied"] else 1

    return _locked(layout, run)


def _forget(args, layout: DataLayout) -> int:
    if args.resume is not None:
        if args.apply:
            print("stira memory forget: --resume takes no --apply", file=sys.stderr)
            return 2

        def resume() -> int:
            recover_for_apply(layout, layout.index, layout.turns)
            result = resume_forget(
                layout,
                args.resume,
                at=datetime.now(tz=UTC),
                queries=tuple(args.query),
                ids=tuple(args.id),
            )
            _print({"dry_run": False, "result": result})
            ok = result["error"] is None and result["job"]["state"] == "completed"
            return 0 if ok else 1

        return _locked(layout, resume)
    try:
        request = ForgetRequest.parse(args.id, args.query)
    except ValueError as error:
        print(f"stira memory forget: {error}", file=sys.stderr)
        return 2
    if not args.apply:
        _print({"dry_run": True, **plan_forget(layout, request).report()})
        return 0

    def run() -> int:
        recover_for_apply(layout, layout.index, layout.turns)  # under the lock
        plan = plan_forget(layout, request)
        result = apply_forget(layout, plan, at=datetime.now(tz=UTC))
        # Locations are shown by the dry run only; the job output keeps to counts,
        # fixed labels and digests (as the job file).
        _print({"dry_run": False, "counts": plan.report()["counts"], "result": result})
        ok = result["error"] is None and result["job"]["state"] == "completed"
        return 0 if ok else 1

    return _locked(layout, run)


def _restore(args, layout: DataLayout) -> int:
    def run() -> int:
        recover_for_apply(layout, layout.index, layout.turns)
        result = restore(layout, args.removal_id)
        _print(
            {
                "restore": {
                    "id": result.record_id,
                    "restored": result.restored,
                    "skipped": result.skipped,
                    "refused": result.refused,
                    "cleanup_error": result.cleanup_error,
                }
            }
        )
        return 0 if result.refused is None and result.cleanup_error is None else 1

    return _locked(layout, run)


def _removed(layout: DataLayout) -> int:
    archive = RemovalArchive(layout)
    records = []
    for removal_id in archive.ids():
        try:
            record = archive.load(removal_id)
        except ForgettingArchiveError as error:
            records.append({"id": removal_id, "error": str(error)})
            continue
        records.append({**record.summary(), "items": describe(record)})
    _print({"removed": records})
    return 0


def _episodic_binding(layout: DataLayout, key: str):
    """The recorded ``(kind, sha256)`` of an episode, read only (creates nothing)."""
    from stiracore.control.memory_retention import _retention_bindings

    return _retention_bindings(layout).get(key)


_TAG_LAYERS = {"semantic": MemoryLayer.SEMANTIC, "episodic": MemoryLayer.EPISODIC}


def _tag_target(args) -> tuple[MemoryLayer, str, str | None, int | None]:
    """``(layer, key, kind, half_life)``; ``ValueError`` for a usage error."""
    layer_name, _, key = str(args.id).partition("/")
    if layer_name not in _TAG_LAYERS:
        raise ValueError("--id must be semantic/<key> or episodic/<key>")
    MemoryStore._validate_key(key)
    layer = _TAG_LAYERS[layer_name]
    kind = None if args.retention == "none" else args.retention
    half_life = args.half_life_days
    if half_life is not None and layer is not MemoryLayer.SEMANTIC:
        raise ValueError("a half-life applies to Semantic entries only")
    tag_metadata({}, kind, half_life)  # validates the combination and the range
    return layer, key, kind, half_life


def _tag(args, layout: DataLayout) -> int:
    try:
        layer, key, kind, half_life = _tag_target(args)
    except ValueError as error:
        print(f"stira memory tag: {error}", file=sys.stderr)
        return 2
    # Output and audit name the entry by digest: a key can carry a memory's words.
    target = {
        "layer": layer.value,
        "key_sha256": hashlib.sha256(f"{layer.value}/{key}".encode()).hexdigest(),
    }
    new = retention_of(tag_metadata({}, kind, half_life)).as_dict()
    if not args.apply:
        data = read_bytes(layout, layout.layer_dir(layer) / f"{key}.md")  # creates nothing
        if data is None:
            _print({"refused": {"code": "E_NOT_FOUND", **target}})
            return 1
        text = data.decode("utf-8", errors="replace")
        _content, metadata = MemoryStore._parse(text)
        current = retention_of(metadata).as_dict()
        preview = {"dry_run": True, **target, "current": current, "new": new}
        if layer is MemoryLayer.EPISODIC:  # only the record counts there (read only)
            bound = _episodic_binding(layout, key)
            preview["recorded"] = (
                None if bound is None else {"kind": bound[0], "text": binding_state(bound, text)}
            )
        _print(preview)
        return 0

    def run() -> int:
        store = MemoryStore(layout.memory_base, layout.agent_id)
        text = store.read_text(layer, key)
        if text is None:
            _print({"refused": {"code": "E_NOT_FOUND", **target}})
            return 1
        content, metadata = store._parse(text)
        archive = ForgettingArchive(store)
        try:
            archive_id = metadata.get("forgetting_archive_id")
            undoable = archive_id and archive.directory.is_dir() and archive_id in archive.ids()
            if layer is MemoryLayer.SEMANTIC and undoable:
                # Tagging rewrites the compressed bytes, and undo needs them unchanged.
                archive.close()
                _print({"refused": {"code": "E_FORGETTING_UNDOABLE", **target}})
                return 1
            archive.prepare()  # an unusable audit log refuses before anything changes
            previous = retention_of(metadata)
            origin = verified_origin(store, layer, key, text)
            new_metadata = tag_metadata(metadata, kind, half_life)
            expected = store.render(content, new_metadata)
            store.write(layer, key, content, new_metadata, preserve_mtime=True)
            if layer is MemoryLayer.EPISODIC:
                if origin:  # this rewrite is the operator's: the #294 binding moves with it
                    record_origin(store, layer, key, origin["origin"], origin["source"])
                # Bound to the text written here, not to a re-read of the file.
                bind_retention(store, layer, key, kind, expected)
        except ForgettingArchiveError as error:
            archive.close()
            _print({"refused": {"code": _error_code(error), **target}})
            return 1
        body = {
            **target,
            "previous": previous.kind if previous.tagged else "untagged",
            "retention": kind or "none",
        }
        if half_life is not None:
            body["half_life_days"] = half_life
        tagged: dict = {**body, "new": new}
        if layer is MemoryLayer.EPISODIC:
            current = store.read_text(layer, key)
            tagged["bound"] = kind is None or (
                current is not None
                and binding_state(retention_binding(store, layer, key), current) == "verified"
            )
        try:
            archive.audit("retention.tagged", **body)
        except ForgettingArchiveError as error:  # written, but not logged: say so
            tagged["audit_error"] = _error_code(error)
        finally:
            archive.close()
        _print({"dry_run": False, "tagged": tagged})
        return 1 if "audit_error" in tagged else 0

    return _locked(layout, run)


def run(args: argparse.Namespace) -> int:
    try:
        layout = DataLayout.at(args.data_dir, args.agent_id)
    except ValueError as error:
        print(f"stira memory: {error}", file=sys.stderr)
        return 2
    try:
        return _dispatch(args, layout)
    except Exception as error:  # noqa: BLE001 - every message can hold a path
        # Raised before (or outside) the commands' own formatting, e.g. the pre-apply
        # recovery refusing a link (#402): a path below the data directory can carry the
        # very words a delete is for (an agent id, a key), so neither stdout nor stderr
        # gets the message or a traceback — a fixed code and the exception type only.
        _print({"refused": {"code": _error_code(error)}})
        return 1


def _dispatch(args: argparse.Namespace, layout: DataLayout) -> int:
    command = args.memory_command
    if command == "retention":
        return _retention(args, layout)
    if command == "forget":
        return _forget(args, layout)
    if command == "restore":
        return _restore(args, layout)
    if command == "tag":
        return _tag(args, layout)
    if command == "jobs":
        _print({"jobs": list_jobs(layout)})
        return 0
    return _removed(layout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stira")
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser(commands)
    args = parser.parse_args(["memory", *(argv if argv is not None else sys.argv[1:])])
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
