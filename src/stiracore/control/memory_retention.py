"""Retention report and opt-in, reversible retention runs: ``stira memory retention`` (#294).

**Nothing is deleted by default, and nothing runs by itself.** The report reads every
store of a data directory (counts, sizes, oldest/newest, and how many items are over
the limits you configured) and changes nothing. Limits come from
``memory_retention`` in ``agent-config.json``; without it every limit is unset and
nothing is a candidate. Only ``--apply`` acts, and only on the stores listed as
*reversible* below: every item is archived first (``forgotten/removed-<id>.json``,
see :mod:`stiracore.control.removal_archive`), the run is audited, and
``stira memory restore <id>`` puts it back. There is no timer or scheduler job for
this (ADR 0008 keeps scheduled jobs model-free; a retention job would be a Gateway
action created by a human, not part of this change).

Reversible with ``--apply``: Episodic entries (``max_age_days``, ``max_entries``,
optional ``expires_at``; episodes Harvest has not scored yet are held by default),
``turns.db`` turns (all their UI events and tool payloads) and tool payloads alone
(their own, usually shorter, limit: tool observations are kept apart from the
conversation), checkpoints (``max_age_days``, ``max_per_thread``; the newest of a
thread is never a candidate), plan files of memory jobs (``memory-plans/``, ADR 0018:
``max_age_days``, **default 90** even without ``memory_retention`` — the one limit that is
set by default, still applied only by ``--apply``). Report only: Semantic and ``staging/`` (a staged entry
waits for a human; retention never approves or rejects), Procedural, the search
index, the context stash (it has its own ``retention_hours``), the hash-chained logs
(``scheduler-events.jsonl``, ``events.db``: rotating them needs a chain segment
design), and the archive itself (a delete request, ``stira memory forget --apply``,
removes the copies it matches).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from stiracore.control.memory_layout import (
    DataLayout,
    UnsafePathError,
    connect_read_only,
    file_size,
    memory_entries,
    read_bytes,
    walk_files,
)
from stiracore.control.memory_plans import PLAN_MAX_AGE_DAYS_DEFAULT, plans_dir
from stiracore.control.memory_removal import RemovalPlan, execute
from stiracore.control.removal_archive import RemovalArchive
from stiracore.control.staging_review import is_staged_key
from stiracore.memory_importance import BINDING_CHANGED, RETENTION_FIELD, binding_state
from stiracore.memory_origin import ORIGIN_UNKNOWN, ORIGIN_UNVERIFIED, origin_of
from stiracore.tools.memory_store import MemoryLayer

__all__ = ["RETENTION_KEYS", "RetentionPolicy", "apply_retention", "retention_report"]

_INT_MAX = 1_000_000
_BYTES_MAX = 10**15  # *_bytes settings (a byte count is easily over a million)
RETENTION_KEYS: dict[str, dict[str, type]] = {
    "episodic": {
        "max_age_days": int,
        "max_entries": int,
        "keep_unharvested": bool,
        "honour_expires_at": bool,
    },
    "turns": {"max_age_days": int},
    "tool_payloads": {"max_age_days": int},
    "checkpoints": {"max_age_days": int, "max_per_thread": int},
    "memory_plans": {"max_age_days": int},
    "staging": {"warn_after_days": int},
    "scheduler_log": {"warn_bytes": int},
    "archive": {"warn_bytes": int},
}
_DEFAULTS = {
    ("episodic", "keep_unharvested"): True,
    ("episodic", "honour_expires_at"): False,
    ("memory_plans", "max_age_days"): PLAN_MAX_AGE_DAYS_DEFAULT,
}


@dataclass(frozen=True)
class RetentionPolicy:
    """Validated ``memory_retention`` settings; an empty mapping means no limits."""

    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_options(cls, options: Any) -> RetentionPolicy:
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("memory_retention must be an object")  # noqa: TRY004 - config contract
        clean: dict[str, dict[str, Any]] = {}
        for store, settings in options.items():
            if store not in RETENTION_KEYS:
                raise ValueError(f"memory_retention: unknown store {store!r}")
            if not isinstance(settings, dict):
                raise ValueError(f"memory_retention.{store} must be an object")  # noqa: TRY004
            allowed = RETENTION_KEYS[store]
            for name, value in settings.items():
                if name not in allowed:
                    raise ValueError(f"memory_retention.{store}: unknown setting {name!r}")
                kind = allowed[name]
                if value is None:
                    continue
                if kind is bool:
                    if type(value) is not bool:
                        raise ValueError(f"memory_retention.{store}.{name} must be a boolean")
                else:
                    top = _BYTES_MAX if name.endswith("_bytes") else _INT_MAX
                    if type(value) is not int or not 1 <= value <= top:
                        raise ValueError(
                            f"memory_retention.{store}.{name} must be an integer from 1 to {top}"
                        )
                clean.setdefault(store, {})[name] = value
        return cls(clean)

    def get(self, store: str, name: str) -> Any:
        value = self.values.get(store, {}).get(name)
        if value is None:
            return _DEFAULTS.get((store, name))
        return value

    def limits(self, store: str) -> dict[str, Any]:
        return {name: self.get(store, name) for name in RETENTION_KEYS[store]}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


def _cutoff(now: datetime, days: int | None) -> float | None:
    return None if days is None else (now - timedelta(days=days)).timestamp()


def _expired(metadata: dict[str, Any], now: datetime) -> bool:
    value = metadata.get("expires_at")
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return False
    else:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= now


def _bindings(layout, skipped: list[str]) -> dict[str, tuple[str, str, str]] | None:
    """Episodic ``key -> (origin, source, sha256)`` recorded by server writers (read only)."""
    try:
        connection = connect_read_only(layout, layout.index)
    except UnsafePathError as error:
        skipped.append(str(error))
        return None
    if connection is None:
        return {}
    try:
        rows = connection.execute(
            "SELECT key, origin, origin_source, origin_sha256 FROM memory_entries"
            " WHERE layer = 'episodic' AND origin IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:  # an index from before #294 (no columns): nothing verified
        return {}
    finally:
        connection.close()
    return {row["key"]: (row["origin"], row["origin_source"], row["origin_sha256"]) for row in rows}


def _retention_bindings(layout) -> dict[str, tuple[str, str]]:
    """Episodic ``key -> (retention, sha256)`` bound by ``stira memory tag`` (#173, read only).

    An unsafe index is already listed under ``skipped`` by :func:`_bindings`; nothing is
    then bound, so nothing is held (the safe side: a hold needs a verified tag).
    """
    try:
        connection = connect_read_only(layout, layout.index)
    except UnsafePathError:
        return {}
    if connection is None:
        return {}
    try:
        rows = connection.execute(
            "SELECT key, retention, retention_sha256 FROM memory_entries"
            " WHERE layer = 'episodic' AND retention IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:  # never tagged (no columns): nothing is bound
        return {}
    finally:
        connection.close()
    return {row["key"]: (row["retention"], row["retention_sha256"]) for row in rows}


def _provenance(entries, bindings=None) -> dict[str, dict[str, int]]:
    """Counts by ``origin`` (who wrote the text) and ``source`` (how it came in).

    ``unknown``: written before #294, by hand, or by anything that sets neither. With
    ``bindings`` (Episodic, whose front matter the agent can edit) only a value a
    server writer recorded for the file's exact text counts; a front-matter claim
    without it is ``unverified``.
    """
    found: dict[str, dict[str, int]] = {"origin": {}, "source": {}}
    for entry in entries:
        origin = origin_of(entry.metadata)
        source = entry.metadata.get("source")
        source = source if isinstance(source, str) and source else ORIGIN_UNKNOWN
        if bindings is not None:
            bound = bindings.get(entry.key)
            if bound and bound[2] == hashlib.sha256(entry.text.encode("utf-8")).hexdigest():
                origin, source = bound[0], bound[1] or ORIGIN_UNKNOWN
            else:
                origin = ORIGIN_UNVERIFIED if origin != ORIGIN_UNKNOWN else origin
                source = ORIGIN_UNVERIFIED if source != ORIGIN_UNKNOWN else source
        found["origin"][origin] = found["origin"].get(origin, 0) + 1
        found["source"][source] = found["source"].get(source, 0) + 1
    return {name: dict(sorted(counts.items())) for name, counts in found.items()}


# -- per store ----------------------------------------------------------------


def _episodic(layout, policy, now, plan: RemovalPlan, skipped: list[str]) -> dict[str, Any]:
    try:
        entries = sorted(memory_entries(layout, MemoryLayer.EPISODIC), key=lambda e: e.mtime)
    except UnsafePathError as error:
        skipped.append(str(error))
        entries = []
    max_age = policy.get("episodic", "max_age_days")
    max_entries = policy.get("episodic", "max_entries")
    keep_unharvested = policy.get("episodic", "keep_unharvested")
    honour = policy.get("episodic", "honour_expires_at")
    cutoff = _cutoff(now, max_age)
    over_count = max(0, len(entries) - max_entries) if max_entries else 0
    reasons = {"max_age_days": 0, "max_entries": 0, "expires_at": 0}
    held = 0
    provenance = _provenance(entries, _bindings(layout, skipped) or {})
    # #173: only a tag a human recorded counts (the front matter is agent-writable): a
    # claim without a record is ``unverified`` and holds nothing. A recorded invariant
    # stays in force when the file changed since (``unverified_held``): an edit by the
    # agent never lifts a human's protection; only ``stira memory tag`` does.
    tags = _retention_bindings(layout)
    retention = {"invariant": 0, "unverified": 0, "unverified_held": 0, "held_invariant": 0}
    expires_seen = 0
    for position, entry in enumerate(entries):
        bound = tags.get(entry.key)
        state = binding_state(bound, entry.text)
        invariant = state is not None and bound is not None and bound[0] == "invariant"
        retention["invariant"] += int(invariant)
        retention["unverified_held"] += int(invariant and state == BINDING_CHANGED)
        retention["unverified"] += int(state is None and RETENTION_FIELD in entry.metadata)
        expired = _expired(entry.metadata, now)
        expires_seen += int(expired)
        why = []
        if cutoff is not None and entry.mtime < cutoff:
            why.append("max_age_days")
        if position < over_count:
            why.append("max_entries")
        if honour and expired:
            why.append("expires_at")
        if not why:
            continue
        if invariant:
            retention["held_invariant"] += 1
            continue
        if keep_unharvested and not entry.metadata.get("harvested"):
            held += 1
            continue
        for reason in why:
            reasons[reason] += 1
        plan.memory.append((MemoryLayer.EPISODIC, entry.key))
        record = layout.emotion_record(entry.key)  # #364: goes (and comes back) with it
        if record.is_file() and not record.is_symlink():
            plan.files.append(("emotion_record", record))
    return {
        "count": len(entries),
        "bytes": sum(e.size for e in entries),
        "oldest": _iso(entries[0].mtime) if entries else None,
        "newest": _iso(entries[-1].mtime) if entries else None,
        "limits": policy.limits("episodic"),
        "candidates": sum(1 for layer, _ in plan.memory if layer is MemoryLayer.EPISODIC),
        "candidate_reasons": reasons,
        "held_unharvested": held,
        "expires_at_passed": expires_seen,
        "provenance": provenance,
        "retention": retention,
        "on_apply": "archive, then remove (restore with `stira memory restore <id>`)",
    }


def _semantic(layout, policy, now, skipped: list[str]) -> dict[str, Any]:
    try:
        entries = [e for e in memory_entries(layout, MemoryLayer.SEMANTIC) if e.key != "index"]
    except UnsafePathError as error:
        skipped.append(str(error))
        entries = []
    staged = sorted((e for e in entries if is_staged_key(e.key)), key=lambda e: e.mtime)
    warn = policy.get("staging", "warn_after_days")
    cutoff = _cutoff(now, warn)
    mtimes = sorted(e.mtime for e in entries)
    return {
        "count": len(entries),
        "bytes": sum(e.size for e in entries),
        "oldest": _iso(mtimes[0]) if mtimes else None,
        "newest": _iso(mtimes[-1]) if mtimes else None,
        "provenance": _provenance(entries),
        "forgetting_compressed": sum(1 for e in entries if e.metadata.get("forgetting_stage")),
        "staging": {
            "count": len(staged),
            "oldest": _iso(staged[0].mtime) if staged else None,
            "limits": policy.limits("staging"),
            "waiting_too_long": (
                sum(1 for e in staged if e.mtime < cutoff) if cutoff is not None else 0
            ),
            "on_apply": "report only: review with staging_cli (never approved/rejected here)",
        },
        "on_apply": "report only (forgetting: distillation_cli --apply-forgetting)",
    }


def _procedural(layout, skipped: list[str]) -> dict[str, Any]:
    try:
        entries = list(memory_entries(layout, MemoryLayer.PROCEDURAL))
    except UnsafePathError as error:
        skipped.append(str(error))
        entries = []
    return {
        "count": len(entries),
        "bytes": sum(e.size for e in entries),
        "on_apply": "report only (procedural_cli)",
    }


def _index(layout, skipped: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"bytes": file_size(layout, layout.index, "-journal", "-wal")}
    try:
        connection = connect_read_only(layout, layout.index)
    except UnsafePathError as error:
        skipped.append(str(error))
        return report
    if connection is None:
        return {**report, "rows": 0, "stale_rows": 0}
    try:
        rows = connection.execute("SELECT layer, key FROM memory_entries").fetchall()
    except sqlite3.Error:
        skipped.append("index.sqlite3 not readable")
        return report
    finally:
        connection.close()
    stale = 0
    for row in rows:
        try:
            path = layout.layer_dir(MemoryLayer(row["layer"])) / f"{row['key']}.md"
        except ValueError:
            stale += 1
            continue
        stale += int(not path.is_file())
    return {
        **report,
        "rows": len(rows),
        "stale_rows": stale,
        "on_apply": "report only (rebuildable; rows go with their entries)",
    }


def _turns(layout, policy, now, plan: RemovalPlan, skipped: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "bytes": file_size(layout, layout.turns, "-wal", "-shm"),
        "limits": {
            "turns": policy.limits("turns"),
            "tool_payloads": policy.limits("tool_payloads"),
        },
        "on_apply": "archive, then remove rows (restore with `stira memory restore <id>`)",
    }
    try:
        connection = connect_read_only(layout, layout.turns)
    except UnsafePathError as error:
        skipped.append(str(error))
        return report
    if connection is None:
        return {**report, "turns": 0, "ui_events": 0, "tool_payloads": 0, "candidates": {}}
    try:
        turns = connection.execute(
            "SELECT turn_id, MIN(ts) AS started, MAX(ts) AS ended, COUNT(*) AS n"
            " FROM ui_events GROUP BY turn_id"
        ).fetchall()
        payloads = connection.execute(
            "SELECT turn_id, tool_use_id, kind, LENGTH(CAST(text AS BLOB)) AS size"
            " FROM tool_payloads"
        ).fetchall()
        turn_cut = _cutoff(now, policy.get("turns", "max_age_days"))
        payload_cut = _cutoff(now, policy.get("tool_payloads", "max_age_days"))
        started = {row["turn_id"]: row["started"] for row in turns}
        old_turns = [t for t in turns if turn_cut is not None and t["started"] < turn_cut]
        old_ids = {t["turn_id"] for t in old_turns}
        for turn in old_turns:
            plan.turn_events.extend(
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM ui_events WHERE turn_id = ?", (turn["turn_id"],)
                )
            )
        payload_candidates = 0
        for row in payloads:
            begun = started.get(row["turn_id"])
            orphan = begun is None
            by_turn = row["turn_id"] in old_ids
            by_payload = payload_cut is not None and (orphan or begun < payload_cut)
            if by_turn or by_payload:
                plan.tool_payloads.append((row["turn_id"], row["tool_use_id"], row["kind"]))
                payload_candidates += 1
    except sqlite3.Error:
        skipped.append("turns.db not readable")
        return report
    finally:
        connection.close()
    return {
        **report,
        "turns": len(turns),
        "ui_events": sum(t["n"] for t in turns),
        "tool_payloads": len(payloads),
        "tool_payload_bytes": sum(row["size"] or 0 for row in payloads),
        "orphan_tool_payloads": sum(1 for row in payloads if row["turn_id"] not in started),
        "oldest": _iso(min((t["started"] for t in turns), default=None)),
        "newest": _iso(max((t["ended"] for t in turns), default=None)),
        "candidates": {
            "turns": len(old_turns),
            "ui_events": len(plan.turn_events),
            "tool_payloads": payload_candidates,
        },
    }


def _checkpoint_info(layout, path) -> tuple[str, float] | None:
    from stiracore.core.checkpoint import _decode

    data = read_bytes(layout, path)
    if data is None:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
        checkpoint = _decode(payload["checkpoint"])
        return str(checkpoint["thread_id"]), float(checkpoint["timestamp"])
    except (ValueError, KeyError, TypeError):
        return None


def _checkpoints(layout, policy, now, plan: RemovalPlan, skipped: list[str]) -> dict[str, Any]:
    threads: dict[str, list[tuple[float, Any]]] = {}
    unreadable = 0
    size = 0
    errors: list[str] = []
    for path, info in walk_files(layout, layout.checkpoints, errors):
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        size += info.st_size
        try:
            found = _checkpoint_info(layout, path)
        except UnsafePathError as error:
            skipped.append(str(error))
            continue
        if found is None:
            unreadable += 1
            continue
        threads.setdefault(found[0], []).append((found[1], path))
    skipped.extend(f"{where} not readable" for where in errors)
    cutoff = _cutoff(now, policy.get("checkpoints", "max_age_days"))
    keep = policy.get("checkpoints", "max_per_thread")
    candidates = 0
    for generations in threads.values():
        generations.sort(key=lambda item: item[0], reverse=True)
        for position, (timestamp, path) in enumerate(generations):
            if position == 0:
                continue  # the newest of a thread is what a resume needs
            if (keep is not None and position >= keep) or (
                cutoff is not None and timestamp < cutoff
            ):
                plan.files.append(("checkpoint", path))
                candidates += 1
    stamps = [t for generations in threads.values() for t, _ in generations]
    return {
        "count": len(stamps),
        "bytes": size,
        "threads": len(threads),
        "max_per_thread_found": max((len(g) for g in threads.values()), default=0),
        "store_cap_per_thread": 20,
        "unreadable": unreadable,
        "oldest": _iso(min(stamps, default=None)),
        "newest": _iso(max(stamps, default=None)),
        "limits": policy.limits("checkpoints"),
        "candidates": candidates,
        "on_apply": "archive, then remove (the newest of a thread is always kept)",
    }


def _memory_plans(layout, policy, now, plan: RemovalPlan, skipped: list[str]) -> dict[str, Any]:
    """Plan files of memory jobs (ADR 0018): age = modification time; dot files too."""
    errors: list[str] = []
    files = list(walk_files(layout, plans_dir(layout), errors))
    skipped.extend(f"{where} not readable" for where in errors)
    cutoff = _cutoff(now, policy.get("memory_plans", "max_age_days"))
    candidates = 0
    for path, info in files:
        if cutoff is not None and info.st_mtime < cutoff:
            plan.files.append(("memory_plan", path))
            candidates += 1
    mtimes = sorted(info.st_mtime for _path, info in files)
    return {
        "count": len(files),
        "bytes": sum(info.st_size for _path, info in files),
        "oldest": _iso(mtimes[0]) if mtimes else None,
        "newest": _iso(mtimes[-1]) if mtimes else None,
        "limits": policy.limits("memory_plans"),
        "candidates": candidates,
        "on_apply": "archive, then remove (a delete request removes plans naming its entries)",
    }


def _stash(layout, stash_hours: float | None, now, skipped: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    files = [
        (path, info)
        for path, info in walk_files(layout, layout.stash, errors)
        if not path.name.startswith(".")
    ]
    skipped.extend(f"{where} not readable" for where in errors)
    cutoff = None if stash_hours is None else now.timestamp() - stash_hours * 3600
    mtimes = sorted(info.st_mtime for _path, info in files)
    return {
        "count": len(files),
        "bytes": sum(info.st_size for _path, info in files),
        "oldest": _iso(mtimes[0]) if mtimes else None,
        "retention_hours": stash_hours,
        "past_retention": sum(1 for t in mtimes if cutoff is not None and t < cutoff),
        "on_apply": "report only (purged at turn start by context management, #258)",
    }


def _jsonl(layout, path, warn: int | None, skipped: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"bytes": file_size(layout, path, ".head")}
    try:
        data = read_bytes(layout, path)
    except UnsafePathError as error:
        skipped.append(str(error))
        return report
    lines = [] if data is None else [line for line in data.splitlines() if line.strip()]
    stamps = []
    for raw in (lines[:1] + lines[-1:]) if lines else []:
        try:
            value = json.loads(raw).get("ts")
        except (ValueError, AttributeError):
            continue
        if isinstance(value, (int, float)):
            stamps.append(float(value))
    return {
        **report,
        "lines": len(lines),
        "oldest": _iso(min(stamps, default=None)),
        "newest": _iso(max(stamps, default=None)),
        "warn_bytes": warn,
        "over_warn": warn is not None and report["bytes"] > warn,
        "on_apply": "report only (hash-chained with a head anchor: rotation is follow-up work)",
    }


def _events(layout, skipped: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {"bytes": file_size(layout, layout.events, "-wal", "-shm", ".head")}
    try:
        connection = connect_read_only(layout, layout.events)
    except UnsafePathError as error:
        skipped.append(str(error))
        return report
    if connection is not None:
        try:
            report["rows"] = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        except sqlite3.Error:
            report["rows"] = None
        finally:
            connection.close()
    report["on_apply"] = "report only (hash-chained with a head anchor)"
    return report


def _archive(layout, policy) -> dict[str, Any]:
    archive = RemovalArchive(layout)
    try:
        forgetting, removals, size = archive.forgetting_ids(), archive.ids(), archive.size()
    except Exception as error:  # noqa: BLE001 - reported, the report goes on
        return {"error": type(error).__name__}
    warn = policy.get("archive", "warn_bytes")
    stamps = sorted(identifier[:16] for identifier in [*forgetting, *removals])
    return {
        "forgetting_records": len(forgetting),
        "removal_records": len(removals),
        "bytes": size,
        "oldest_id_time": stamps[0] if stamps else None,
        "warn_bytes": warn,
        "over_warn": warn is not None and size > warn,
        "on_apply": "report only (holds full text; a delete request removes matching copies)",
    }


def _rejections(layout, skipped: list[str]) -> dict[str, Any]:
    try:
        data = read_bytes(layout, layout.rejections)
    except UnsafePathError as error:
        skipped.append(str(error))
        data = None
    lines = [] if data is None else [line for line in data.splitlines() if line.strip()]
    return {"lines": len(lines), "bytes": len(data or b""), "on_apply": "report only"}


# -- entry points ---------------------------------------------------------------


def retention_report(
    layout: DataLayout,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    stash_hours: float | None = None,
) -> tuple[dict[str, Any], RemovalPlan]:
    """Read every store; return the report and the plan ``--apply`` would execute."""
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    plan = RemovalPlan()
    skipped: list[str] = []
    stores = {
        "episodic": _episodic(layout, policy, now, plan, skipped),
        "semantic": _semantic(layout, policy, now, skipped),
        "procedural": _procedural(layout, skipped),
        "search_index": _index(layout, skipped),
        "turns_db": _turns(layout, policy, now, plan, skipped),
        "checkpoints": _checkpoints(layout, policy, now, plan, skipped),
        "memory_plans": _memory_plans(layout, policy, now, plan, skipped),
        "context_stash": _stash(layout, stash_hours, now, skipped),
        "scheduler_log": _jsonl(
            layout, layout.scheduler_log, policy.get("scheduler_log", "warn_bytes"), skipped
        ),
        "events_db": _events(layout, skipped),
        "archive": _archive(layout, policy),
        "staging_rejections": _rejections(layout, skipped),
    }
    plan.skipped = sorted(set(skipped))
    report = {
        "at": now.isoformat(timespec="seconds"),
        "policy_configured": bool(policy.values),
        "stores": stores,
        "will_remove": {
            "episodic": len(plan.memory),
            "turn_events": len(plan.turn_events),
            "tool_payloads": len(plan.tool_payloads),
            "checkpoints": sum(1 for name, _ in plan.files if name == "checkpoint"),
            "emotion_records": sum(1 for name, _ in plan.files if name == "emotion_record"),
            "memory_plans": sum(1 for name, _ in plan.files if name == "memory_plan"),
        },
        "skipped": sorted(set(skipped)),
    }
    return report, plan


def apply_retention(
    layout: DataLayout, plan: RemovalPlan, *, at: datetime | None = None
) -> dict[str, Any]:
    at = (at or datetime.now(tz=UTC)).astimezone(UTC)
    if plan.skipped:  # fail closed, as a delete request: a limit is applied only to what
        # was fully seen (a store that could not be read may hold what the plan relies on)
        return {
            "applied": False,
            "removal_id": None,
            "counts": {},
            # Code only: the reasons start with paths (shown by the report, not here).
            "error": f"E_NOT_INSPECTED: {len(plan.skipped)} store(s) could not be inspected",
        }
    if plan.empty():
        return {"applied": True, "removal_id": None, "counts": {}, "error": None}
    result = execute(layout, plan, purpose="retention", reason="retention limits", at=at)
    return {
        "applied": result.applied,
        "removal_id": result.record_id,
        "counts": result.counts,
        "error": result.error,
    }
