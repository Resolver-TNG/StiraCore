"""Manual entry point for memory distillation and maintenance previews."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

from stiracore.config import AgentConfig
from stiracore.config.data_dir_lock import (
    _SYMLINK_ERRNOS,
    LOCK_FILENAME,
    DataDirLockError,
    DataDirOwnerLock,
    UnsafeLockFileError,
    _check_descriptor,
    read_owner_pid,
)
from stiracore.config.resolver import model_spec_from
from stiracore.control.distillers import JevDistiller, LLMDistiller, RuleBasedDistiller
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.memory_maintenance import SELECTION_BASIS, MemoryMaintenance
from stiracore.control.staging_review import is_staged_key
from stiracore.core.amygdala import AmygdalaPass
from stiracore.heartbeat.daemon import _CONTENTION_ERRNOS, _try_lock, _unlock
from stiracore.memory_rating import scoring_metadata
from stiracore.tools.memory_store import MemoryLayer, MemoryStore


def _build_distiller(config: AgentConfig, region: str):
    amygdala = AmygdalaPass()
    if config.distiller_mode == "rule":
        return RuleBasedDistiller(amygdala)
    if config.distiller_mode == "llm":
        spec = model_spec_from(
            config.distiller_provider,
            config.distiller_model_id,
            region=region,
            base_url=config.distiller_base_url,
            api_key_env=config.distiller_api_key_env,
        )
        return LLMDistiller(spec, region=region, amygdala=amygdala)
    return JevDistiller()


class _ModelCallLimit(RuntimeError):
    """The run's model-call budget is spent; the episode stays unscored (retryable)."""


class _CallBudget:
    """Let at most ``limit`` episodes reach a model-calling distiller in one run.

    Counts ``distill`` invocations (one model call each, before any SDK retry).
    Episodes past the limit raise before the model is called; Harvest leaves them
    unscored for the next run.
    """

    def __init__(self, inner, limit: int) -> None:
        self.inner = inner
        self.version = inner.version
        self.limit = limit
        self.calls = 0
        self.refused = 0

    def distill(self, content, metadata):
        if self.calls >= self.limit:
            self.refused += 1
            raise _ModelCallLimit("model call limit reached")
        self.calls += 1
        return self.inner.distill(content, metadata)


def _call_limit(value: str) -> int:
    if not value.isdigit() or len(value) > 6:
        raise argparse.ArgumentTypeError("expected an integer from 0 to 999999")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Distill Episodic memory into Semantic memory")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--agent-id", default="stira")
    parser.add_argument("--config", type=Path, help="agent-config.json (required unless undo/list)")
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument(
        "--owner-lock",
        type=Path,
        metavar="DATA_DIR",
        help=(
            "hold DATA_DIR/server.lock (the server's owner lock, #285) for the whole run, "
            "so no server writes the same memory meanwhile; exits 3 when it is owned. "
            "--base-dir must be inside DATA_DIR"
        ),
    )
    parser.add_argument(
        "--max-model-calls",
        type=_call_limit,
        metavar="N",
        help=(
            "llm distiller: score at most N episodes (N model calls, SDK retries not "
            "counted) in this run; the rest stay for the next run and the exit code is 4. "
            "With --dry-run: exit 4 when the preview needs more (or cannot tell)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "count what a run with the same arguments would do and size its model calls; "
            "creates, writes and calls nothing (the rule distiller's routes are computed, "
            "it is model-free); with --owner-lock the lock is only probed"
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--apply-forgetting",
        action="store_true",
        help=(
            "apply deterministic Semantic compression after archiving each original "
            "(refused when the archive cannot be written); default is preview only"
        ),
    )
    action.add_argument(
        "--undo-forgetting",
        metavar="ID",
        help="restore the original of one forgetting step (no Harvest run); audited",
    )
    action.add_argument(
        "--list-forgotten",
        action="store_true",
        help="list archived forgetting steps (ids, keys, digests; no text); no Harvest run",
    )
    return parser


def _probe_owner_lock(data_dir: Path) -> int | None:
    """Hold the owner lock for a dry run without creating or writing ``server.lock``.

    No file: no server has opened the directory, nothing is created. Otherwise the
    file is opened read-only (no symlink, no FIFO wait), checked like the server's
    lock and locked; the returned descriptor is unlocked and closed by the caller.
    Windows (``msvcrt.locking`` on a read-only descriptor): unverified.
    """
    path = Path(data_dir) / LOCK_FILENAME
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in _SYMLINK_ERRNOS:
            raise UnsafeLockFileError(path, "a symbolic link") from None
        raise
    try:
        problem = _check_descriptor(fd)
        if problem is not None:
            raise UnsafeLockFileError(path, problem)
        _try_lock(fd)
    except OSError as error:
        os.close(fd)
        if error.errno in _CONTENTION_ERRNOS:
            raise DataDirLockError(path, read_owner_pid(path), None) from None
        raise
    except BaseException:
        os.close(fd)
        raise
    return fd


def _missing_store_parts(base_dir: Path, agent_id: str) -> list[str]:
    """What opening a :class:`MemoryStore` here would create (the dry run must not)."""
    MemoryStore._validate_component(agent_id, "agent_id")
    memory_dir = Path(base_dir) / "agents" / agent_id / "memory"
    if not memory_dir.is_dir():
        return ["memory"]
    names = [layer.value for layer in MemoryLayer] + ["index.sqlite3"]
    return [name for name in names if not (memory_dir / name).exists()]


def _dry_run(args: argparse.Namespace, config: AgentConfig, distiller) -> int:
    """Preview one run: episode counts, predicted routes (rule), model calls (llm).

    Creates and writes nothing: an incomplete store (a missing layer directory or
    index, which opening it would create) is not opened, and only what is known
    without it is reported. Episodes are listed without touching ``last_accessed``
    and no duplicate/rejection lookup is made (those searches would touch Semantic
    entries), so the routes are an upper bound of what a run would write.
    """
    mode = config.distiller_mode
    missing = _missing_store_parts(args.base_dir, args.agent_id)
    no_episodes = bool({"memory", MemoryLayer.EPISODIC.value} & set(missing))
    harvest: dict[str, object] = {"episodes": None, "already_harvested": None, "to_score": None}
    estimate: dict[str, object] = {"model_calls": 0, "prompt_chars": 0, "max_output_tokens": 0}
    counts: dict[str, object] = {
        "forgetting_candidates": None,
        "deletion_candidates": None,
        "staging_pending": None,
    }
    # What forgetting would select and why, and what retention: invariant protects (#173).
    forgetting_preview: dict[str, object] | None = None
    if no_episodes:
        harvest = {"episodes": 0, "already_harvested": 0, "to_score": 0}
    if missing:
        pending = [] if no_episodes else None
        if mode == "llm" and pending is None:
            estimate = {"model_calls": None, "prompt_chars": None, "max_output_tokens": None}
    else:
        store = MemoryStore(args.base_dir, args.agent_id)
        episodes = store.list_entries(MemoryLayer.EPISODIC, limit=100_000)
        pending = [
            entry
            for entry in episodes
            if not (
                entry.metadata.get("harvested")
                and entry.metadata.get("harvest_version") == distiller.version
            )
        ]
        harvest = {
            "episodes": len(episodes),
            "already_harvested": len(episodes) - len(pending),
            "to_score": len(pending),
        }
        maintenance = MemoryMaintenance(store)
        preview = maintenance.run_forgetting(apply=False)
        forgetting_preview = {
            "selection": SELECTION_BASIS,
            # Digests and labels, no keys (#173): a key can carry a memory's words.
            "items": [candidate.public_dict() for candidate in preview.candidates],
            "protected": list(preview.protected),
        }
        counts = {
            "forgetting_candidates": len(preview.candidates),
            "deletion_candidates": len(maintenance.find_deletion_candidates()),
            "staging_pending": sum(
                1
                for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=100_000)
                if is_staged_key(entry.key)
            ),
        }
    available = mode in ("rule", "llm")  # JevDistiller is a stub (#106): every episode fails
    if mode == "rule" and pending is not None:
        routes = {"discard": 0, "staging": 0, "knowledge": 0, "errors": 0}
        for entry in pending:
            try:
                route = distiller.distill(entry.content, scoring_metadata(store, entry)).route
            except Exception:  # noqa: BLE001 - counted, like the real sweep
                routes["errors"] += 1
                continue
            if route == "auto":
                route = "staging" if config.harvest_stage_auto else "knowledge"
            routes[route] += 1
        harvest["routes_before_duplicate_checks"] = routes
    elif mode == "llm" and pending is not None:
        estimate = {
            "model_calls": len(pending),
            "prompt_chars": sum(
                len(LLMDistiller.build_prompt(entry.content, scoring_metadata(store, entry)))
                for entry in pending
            ),
            "max_output_tokens": len(pending) * LLMDistiller.MAX_TOKENS,
        }
    if mode == "llm":
        estimate["model"] = distiller.version
    limit = args.max_model_calls
    calls = estimate["model_calls"]
    over_limit = limit is not None and (calls is None or calls > limit)
    payload = {
        "dry_run": True,
        "mode": mode,
        "distiller_available": available,
        "store": {"complete": not missing, "missing": missing},
        "harvest": harvest,
        "estimate": {**estimate, "limit": limit, "over_limit": over_limit},
        "forgetting_apply": args.apply_forgetting,
        "forgetting": forgetting_preview,
        **counts,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if over_limit:
        return 4
    return 0 if available or pending == [] else 1


def _undo(store: MemoryStore, archive_id: str) -> int:
    result = MemoryMaintenance(store).undo_forgetting(archive_id)
    payload = {
        "undo": {
            "id": result.archive_id,
            "key": result.key,
            "restored": result.restored,
            "refused": result.refused,
            # Restored, but the audit line is missing: rerun an undo/apply to complete it.
            "audit_error": result.audit_error,
            # Restored and logged, but the record is still there (removed by a rerun).
            "cleanup_error": result.cleanup_error,
            "reconciled": list(result.reconciled),
        }
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    clean = result.audit_error is None and result.cleanup_error is None
    unfinished = any("cleanup_error" in item for item in result.reconciled)
    return 0 if result.restored and clean and not unfinished else 1


def _list(store: MemoryStore) -> int:
    records = MemoryMaintenance(store).archive.summaries()
    print(json.dumps({"forgotten": records}, ensure_ascii=False, sort_keys=True))
    return 0


def _refusal(error: DataDirLockError) -> int:
    # Not silent: a timer sees exit 3 and this line (no memory was read or written).
    if isinstance(error, UnsafeLockFileError):  # a link or foreign file: nothing written
        refusal = {"code": "E_LOCK_FILE_UNSAFE", "owner_pid": None}
    else:
        refusal = {"code": "E_DATA_DIR_OWNED", "owner_pid": error.owner_pid}
    print(json.dumps({"refused": refusal}, sort_keys=True))
    return 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and (args.undo_forgetting is not None or args.list_forgotten):
        parser.error("--dry-run previews a distill run (not --undo-forgetting/--list-forgotten)")
    if args.owner_lock is not None and not args.base_dir.resolve().is_relative_to(
        args.owner_lock.resolve()
    ):
        parser.error("--base-dir must be inside the --owner-lock data directory")
    if args.dry_run:
        return _dry_run_main(parser, args)
    if args.owner_lock is None:
        return _run(parser, args)
    lock = DataDirOwnerLock(args.owner_lock)
    try:
        lock.acquire()
    except DataDirLockError as error:
        return _refusal(error)
    try:
        return _run(parser, args)
    finally:
        lock.release()


def _dry_run_main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None:
        parser.error("--config is required to distill")
    config = AgentConfig.load(args.config)  # read only; a missing file means the defaults
    distiller = _build_distiller(config, args.region)
    probe = None
    if args.owner_lock is not None:
        try:
            probe = _probe_owner_lock(args.owner_lock)
        except DataDirLockError as error:
            return _refusal(error)
    try:
        return _dry_run(args, config, distiller)
    finally:
        if probe is not None:
            try:
                _unlock(probe)
            finally:
                os.close(probe)


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    store = MemoryStore(args.base_dir, args.agent_id)
    if args.undo_forgetting is not None:
        return _undo(store, args.undo_forgetting)
    if args.list_forgotten:
        return _list(store)
    if args.config is None:
        parser.error("--config is required to distill")
    config = AgentConfig.load(args.config)
    distiller = _build_distiller(config, args.region)
    budget = None
    if config.distiller_mode == "llm" and args.max_model_calls is not None:
        distiller = budget = _CallBudget(distiller, args.max_model_calls)
    harvest = HarvestScheduler(
        store,
        None,
        args.agent_id,
        distiller=distiller,
        stage_auto=config.harvest_stage_auto,
    ).run_distillation()
    maintenance = MemoryMaintenance(store)
    forgetting = maintenance.run_forgetting(apply=args.apply_forgetting)
    deletion_candidates = maintenance.find_deletion_candidates()
    payload = {
        "mode": config.distiller_mode,
        "harvest": {
            "semantic": harvest.semantic,
            "knowledge": harvest.knowledge,
            "discard": harvest.discard,
            "skipped": harvest.skipped,
            "errors": harvest.errors,
        },
        "forgetting": {
            "candidates": len(forgetting.candidates),
            "applied": forgetting.applied,
            "refused": forgetting.refused,
            "error": forgetting.error,
            "audit_incomplete": forgetting.audit_incomplete,
            "reconciled": list(forgetting.reconciled),
            "selection": SELECTION_BASIS,
            "items": [candidate.as_dict() for candidate in forgetting.candidates],
            "protected": list(forgetting.protected),
        },
        "deletion_candidates": len(deletion_candidates),
        # Waiting for review: `python -m stiracore.control.staging_cli list` (#293).
        "staging_pending": harvest.staging_pending,
    }
    if budget is not None:
        payload["model_call_limit"] = {
            "limit": budget.limit,
            "calls": budget.calls,
            "refused": budget.refused,  # also counted in harvest.errors; left for the next run
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if budget is not None and budget.refused:
        return 4
    failed = (
        harvest.errors
        or forgetting.refused
        or forgetting.audit_incomplete
        or forgetting.error  # e.g. a damaged log with no candidates
        or any("cleanup_error" in item for item in forgetting.reconciled)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
