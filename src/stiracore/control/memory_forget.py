"""Delete requests that reach every copy: ``stira memory forget`` (#294).

A memory system that deletes the entry but keeps its search-index row, a derived
summary, a staging copy, a checkpoint or a "previous value" in a history table has
not forgotten it. :func:`plan_forget` looks for the request (``--id <layer>/<key>``
and/or ``--query <text>``) in every place this data directory keeps text, and sorts
what it finds:

* **reached** (removed by ``--apply``): Episodic entries; Semantic entries including
  ``staging/`` and every entry derived from a removed episode (``source_id``); their
  rows in ``index.sqlite3`` and stale rows whose text matches; the emotion record of
  every removed episode (``emotion/<digest>.json``, #364); ``semantic/index.md``
  (regenerated); lines of ``staging_rejections.jsonl``; checkpoints; context-stash
  files; ``turns.db`` UI events and tool payloads; the plan files of memory jobs that name
  a forgotten entry or hold the text (``memory-plans/``, ADR 0018,
  :mod:`stiracore.control.memory_plans`); and the **archived copies**: the
  forgetting archive of #320 and the removal records of retention runs. SQLite files
  are compacted after rows go, so the text does not survive in free pages or the WAL.
* **not reached** (reported, never edited): hash-chained logs (``events.db``,
  ``scheduler-events.jsonl``, ``hitl-*.db``, ``forgetting-log.jsonl``) — editing a
  line breaks the chain; they are designed to hold masked summaries, ids and
  digests; the Procedural layer (changed through ``procedural_cli`` only); files the
  agent or the operator own (``workspace/``, ``distill-runs.*``, settings files);
  and anything outside the data directory (server logs, backups, provider-side
  prompt caches, the memory of a running process — ``--apply`` refuses while the
  server owns the directory).

**Forgetting and retention are reversible, a delete is not.** ``--apply-forgetting``
(#320) and ``retention --apply`` archive what they change and are undone only through
their explicit, audited paths. A delete request is the opposite on purpose: it keeps
no copy and also removes the archived copies, so nothing can bring the text back.

``--apply`` runs a resumable job (see "the delete job" below). Afterwards the same
search runs over the raw bytes of every file of the data directory and reports what
is left (``residual``), so a copy nobody knew about shows up instead of being kept.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from stiracore.control.forgetting_archive import ForgettingArchiveError, sha256_text
from stiracore.control.memory_layout import (
    DataLayout,
    UnsafePathError,
    connect_read_only,
    contains,
    memory_entries,
    needles_for,
    read_bytes,
    walk_files,
)
from stiracore.control.memory_layout import (
    located as _located,
)
from stiracore.control.memory_plans import PLANS_DIRNAME, names_any, plans_dir
from stiracore.control.memory_removal import RemovalPlan
from stiracore.control.removal_archive import RemovalArchive
from stiracore.control.staging_review import is_staged_key
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

__all__ = [
    "ForgetPlan",
    "ForgetRequest",
    "apply_forget",
    "list_jobs",
    "plan_forget",
    "residual",
    "resume_forget",
]

# Places that are never edited by a delete request, with the reason shown to the operator.
NOT_REACHED = {
    "events.db": "hash-chained observability log (masked events); editing breaks the chain",
    "scheduler-events.jsonl": "hash-chained scheduler log (masked summaries)",
    "hitl-approvals.db": "hash-chained approval store (masked arguments)",
    "hitl-operations.db": "ledger of approved side effects",
    "forgetting-log.jsonl": "hash-chained audit log (ids, keys, digests only)",
    "emotion-log.jsonl": "emotion-record clean-up log (record file names and reasons only)",
    "procedural": "Procedural layer: changed only through procedural_cli (human review)",
    "workspace": "the agent's work folder (the operator's files)",
    "distill-runs": "the example timer wrapper's own log (not written by Stira)",
    "settings": "settings and tool-rule files",
    "other": "not a known memory store",
}


@dataclass(frozen=True)
class ForgetRequest:
    ids: tuple[tuple[MemoryLayer, str], ...] = ()
    queries: tuple[str, ...] = ()

    @classmethod
    def parse(cls, ids: list[str] | tuple[str, ...], queries: list[str] | tuple[str, ...]):
        parsed = []
        for value in ids:
            layer_name, _, key = str(value).partition("/")
            try:
                layer = MemoryLayer(layer_name)
            except ValueError:
                raise ValueError(
                    "--id must be <episodic|semantic|procedural>/<key>, e.g. episodic/chat-1"
                ) from None
            MemoryStore._validate_key(key)
            parsed.append((layer, key))
        cleaned = tuple(q for q in (str(v) for v in queries) if q.strip())
        if len(cleaned) != len(tuple(queries)):
            raise ValueError("--query must not be empty")
        if any(len(q.strip()) < 3 for q in cleaned):
            raise ValueError(
                "--query must be at least 3 characters (a shorter one matches too much)"
            )
        if not parsed and not cleaned:
            raise ValueError("give --id and/or --query")
        return cls(tuple(parsed), cleaned)

    def digests(self) -> list[str]:
        """What the audit log records about the request: digests only, never the text."""
        return [sha256_text(q) for q in self.queries]


@dataclass
class ForgetPlan:
    request: ForgetRequest
    removal: RemovalPlan = field(default_factory=RemovalPlan)
    reached: dict[str, list[str]] = field(default_factory=dict)
    archive_copies: dict[str, list[str]] = field(default_factory=dict)
    not_reached: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Purge: forgetting records to drop, removal records to rewrite without their matches.
    purge_forgetting: list[str] = field(default_factory=list)
    purge_removal: dict[str, list[int]] = field(default_factory=dict)
    # What the request resolved to (also through derivation) and what was passed in.
    known: set[str] = field(default_factory=set)
    episodes: set[str] = field(default_factory=set)
    semantic_keys: set[str] = field(default_factory=set)
    emotion_paths: set[str] = field(default_factory=set)

    def resolved(self) -> set[str]:
        """Digests of everything this plan resolved: kept by the job for a resume."""
        return (
            set(self.known)
            | {_key_digest("episodic", key) for key in self.episodes}
            | {_key_digest("semantic", key) for key in self.semantic_keys}
            | {sha256_text(path) for path in self.emotion_paths}
        )

    def add(self, where: str, label: str) -> None:
        self.reached.setdefault(where, []).append(label)

    def report(self) -> dict[str, Any]:
        return {
            "request": {
                "ids": [f"{layer.value}/{key}" for layer, key in self.request.ids],
                "query_sha256": self.request.digests(),
            },
            "reached": {name: sorted(set(v)) for name, v in sorted(self.reached.items())},
            "counts": {name: len(set(v)) for name, v in sorted(self.reached.items())},
            "archive_copies": {
                name: sorted(set(v)) for name, v in sorted(self.archive_copies.items())
            },
            "not_reached": self.not_reached,
            "skipped": sorted(set(self.skipped)),
            "note": (
                "checkpoints hold the conversation of a resume: deleting one means a "
                "pending approval of that batch can no longer be resumed"
                if any(name == "checkpoint" for name in self.reached)
                else None
            ),
            "hint": (
                None
                if self.request.queries
                else "--id alone finds the entry and what was derived from it; add --query "
                "with a phrase of the conversation to reach checkpoints, turns.db and the "
                "context stash"
            ),
        }


def _id_keys(request: ForgetRequest) -> tuple[str, ...]:
    return tuple(f"{layer.value}/{key}" for layer, key in request.ids)


def _key_digest(layer: str, key: str) -> str:
    return sha256_text(f"{layer}/{key}")


def plan_forget(
    layout: DataLayout, request: ForgetRequest, *, known: frozenset[str] | set[str] = frozenset()
) -> ForgetPlan:
    """Find every copy of the request. Reads only; creates nothing.

    ``known``: digests of what an earlier attempt of the same job resolved (``layer/key``
    of entries, relative paths of emotion records). A resume plans with them, so a
    derived entry whose source is already gone — found the first time only through it
    (``source_id``) — is still found (the job keeps digests, never the keys).
    """
    plan = ForgetPlan(request)
    needles = needles_for(request.queries)
    wanted = set(request.ids)
    removal = plan.removal

    # The sets of what is forgotten are built from the request, the live layers *and*
    # the archived copies, independently of which files exist now: an entry that only
    # survives in the archive (a retention run, a forgetting step) is still the memory
    # the person asked about, and so is everything derived from it (``source_id``).
    archived = _archive_view(layout, plan)
    live_episodes = list(_entries(layout, plan, MemoryLayer.EPISODIC))
    live_semantic = list(_entries(layout, plan, MemoryLayer.SEMANTIC))
    plan.known = set(known)

    def is_known(layer: str, key: Any) -> bool:
        return bool(known) and _key_digest(layer, str(key)) in known

    episodes: set[str] = {key for layer, key in wanted if layer is MemoryLayer.EPISODIC}
    episodes |= {
        e.key for e in live_episodes if contains(e.text, needles) or is_known("episodic", e.key)
    }
    episodes |= {
        copy.key
        for copy in archived.memory
        if copy.layer is MemoryLayer.EPISODIC
        and (contains(copy.text, needles) or is_known("episodic", copy.key))
    }
    semantic_keys: set[str] = {key for layer, key in wanted if layer is MemoryLayer.SEMANTIC}
    for key, text, metadata in [
        *((e.key, e.text, e.metadata) for e in live_semantic if e.key != "index"),
        *((c.key, c.text, c.metadata) for c in archived.memory if c.layer is MemoryLayer.SEMANTIC),
    ]:
        source = str(metadata.get("source_id", ""))
        if (
            contains(text, needles)
            or source in episodes
            or is_known("semantic", key)
            or is_known("episodic", source)  # derived from a source already deleted
        ):
            semantic_keys.add(key)
    plan.episodes, plan.semantic_keys = set(episodes), set(semantic_keys)

    for entry in live_episodes:
        if entry.key in episodes:
            removal.memory.append((entry.layer, entry.key))
            plan.add("episodic", entry.key)
    index_text = ""
    for entry in live_semantic:
        if entry.key == "index":
            index_text = entry.text
        elif entry.key in semantic_keys:
            removal.memory.append((entry.layer, entry.key))
            plan.add("semantic_staging" if is_staged_key(entry.key) else "semantic", entry.key)
    live_removed = any(layer is MemoryLayer.SEMANTIC for layer, _key in removal.memory)
    if (
        live_removed
        or contains(index_text, needles)
        or any(_index_lists(index_text, key) for key in semantic_keys)
    ):
        removal.regenerate_index = True
        plan.add("semantic_index_md", "index")
    for entry in _entries(layout, plan, MemoryLayer.PROCEDURAL):
        if (entry.layer, entry.key) in wanted or contains(entry.text, needles):
            plan.not_reached.append(
                {"where": f"procedural/{entry.key}", "reason": NOT_REACHED["procedural"]}
            )

    # #364: the emotion values of a forgotten episode (no text, but what the person
    # asked to forget includes how that moment felt), also when only the record is
    # left. The job removes them before the episode.
    emotion_paths: set[str] = set()
    for key in sorted(episodes):
        record = layout.emotion_record(key)
        emotion_paths.add(layout.relative(record))
        try:
            present = record.is_file() and not record.is_symlink()
        except OSError:
            plan.skipped.append(f"{layout.relative(record.parent)} not readable")
            continue
        if present:
            removal.files.append(("emotion_record", record))
            plan.add("emotion_records", layout.relative(record))
    if known:  # an emotion record whose episode (and key) is already gone
        planned = {layout.relative(path) for _name, path in removal.files}
        errors: list[str] = []
        for path, _info in walk_files(layout, layout.memory_dir / "emotion", errors):
            relative = layout.relative(path)
            if relative not in planned and sha256_text(relative) in known:
                removal.files.append(("emotion_record", path))
                plan.add("emotion_records", relative)
                emotion_paths.add(relative)
        plan.skipped.extend(f"{where} not readable" for where in errors)
    plan.emotion_paths = set(emotion_paths)
    removed = {(layer.value, key) for layer, key in removal.memory}
    forgotten = {(MemoryLayer.EPISODIC, key) for key in episodes} | {
        (MemoryLayer.SEMANTIC, key) for key in semantic_keys
    }
    _plan_index(layout, plan, needles, removed, wanted | forgotten)
    _plan_rejections(layout, plan, needles, episodes, semantic_keys)
    _plan_memory_plans(layout, plan, needles, episodes | semantic_keys)
    if needles:
        for name, root in (("checkpoint", layout.checkpoints), ("context_stash", layout.stash)):
            # Dot files too (temporary files of an interrupted write hold the same text):
            # the plan and the residual check see exactly the same files.
            errors: list[str] = []
            for path, _info in walk_files(layout, root, errors):
                data = _read(layout, plan, path)
                if data is not None and contains(data.decode("utf-8", "replace"), needles):
                    removal.files.append((name, path))
                    plan.add(name, layout.relative(path))
            plan.skipped.extend(f"{where} not readable" for where in errors)
        _plan_turns(layout, plan, needles)
        _plan_strays(layout, plan, needles)
    _plan_archive(plan, archived, needles, episodes, semantic_keys, emotion_paths)
    plan.not_reached.extend(_unreached(layout, needles + _id_needles(request)))
    return plan


def _plan_memory_plans(layout, plan, needles, keys: set[str]) -> None:
    """Plan files (ADR 0018) that name a forgotten entry or hold the text: removed whole.

    Every file of ``memory-plans/`` is read (dot files too: an interrupted write), with or
    without ``--query``: a plan names entries by key, so ``--id`` alone reaches it.
    """
    errors: list[str] = []
    for path, _info in walk_files(layout, plans_dir(layout), errors):
        data = _read(layout, plan, path)
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        if contains(text, needles) or names_any(text, keys):
            plan.removal.files.append(("memory_plan", path))
            plan.add("memory_plans", layout.relative(path))
    plan.skipped.extend(f"{where} not readable" for where in errors)


def _index_lists(index_text: str, key: str) -> bool:
    """Whether ``semantic/index.md`` has the row of entry ``key`` (its path column)."""
    return f"| semantic/{key}.md |" in index_text


def _id_needles(request: ForgetRequest) -> tuple[str, ...]:
    return tuple(key.casefold() for _layer, key in request.ids)


def _entries(layout: DataLayout, plan: ForgetPlan, layer: MemoryLayer):
    try:
        yield from memory_entries(layout, layer)
    except UnsafePathError as error:
        plan.skipped.append(str(error))


def _read(layout: DataLayout, plan: ForgetPlan, path: Path) -> bytes | None:
    try:
        return read_bytes(layout, path)
    except UnsafePathError as error:
        plan.skipped.append(str(error))
        return None


def _plan_index(layout, plan, needles, removed, wanted) -> None:
    try:
        connection = connect_read_only(layout, layout.index)
    except UnsafePathError as error:
        plan.skipped.append(str(error))
        return
    if connection is None:
        return
    wanted_rows = {(layer.value, key) for layer, key in wanted}
    try:
        rows = connection.execute("SELECT layer, key, search_text FROM memory_entries").fetchall()
    except sqlite3.Error:
        plan.skipped.append("memory/.../index.sqlite3 not readable")
        return
    finally:
        connection.close()
    for row in rows:
        identity = (row["layer"], row["key"])
        if identity in removed:
            plan.add("search_index_rows", "/".join(identity))
        elif (
            identity in wanted_rows
            or contains(row["search_text"], needles)
            or _key_digest(*identity) in plan.known
        ):
            if identity[0] == MemoryLayer.PROCEDURAL.value:
                continue  # the entry stays (not reached), so does its row
            plan.removal.index_rows.append(identity)
            plan.add("search_index_rows", "/".join(identity))


def _plan_rejections(layout, plan, needles, episodes, semantic_keys) -> None:
    data = _read(layout, plan, layout.rejections)
    if data is None:
        return
    for number, line in enumerate(data.decode("utf-8", "replace").splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = {}
        if not isinstance(record, dict):
            record = {}
        if (
            record.get("source_id") in episodes
            or record.get("key") in semantic_keys
            or contains(line, needles)
            or _key_digest("episodic", str(record.get("source_id"))) in plan.known
            or _key_digest("semantic", str(record.get("key"))) in plan.known
        ):
            plan.removal.rejection_lines.append(number)
            plan.add("staging_rejections", f"line {number + 1}")


def _plan_turns(layout, plan, needles) -> None:
    try:
        connection = connect_read_only(layout, layout.turns)
    except UnsafePathError as error:
        plan.skipped.append(str(error))
        return
    if connection is None:
        return
    try:
        for row in connection.execute("SELECT id, turn_id, payload FROM ui_events"):
            if contains(row["payload"], needles):
                plan.removal.turn_events.append(row["id"])
                plan.add("turn_events", f"{row['turn_id']}#{row['id']}")
        for row in connection.execute("SELECT turn_id, tool_use_id, kind, text FROM tool_payloads"):
            if contains(row["text"], needles):
                key = (row["turn_id"], row["tool_use_id"], row["kind"])
                plan.removal.tool_payloads.append(key)
                plan.add("tool_payloads", "/".join(key))
    except sqlite3.Error:
        plan.skipped.append("turns.db not readable")
    finally:
        connection.close()


@dataclass(frozen=True)
class _ArchivedMemory:
    layer: MemoryLayer
    key: str
    text: str
    metadata: dict[str, Any]


@dataclass
class _ArchiveView:
    forgetting: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    removals: list[tuple[str, Any]] = field(default_factory=list)
    memory: list[_ArchivedMemory] = field(default_factory=list)


def _is_record_name(name: str) -> bool:
    """A #320 record, a removal record, a delete job or the audit log in ``forgotten/``."""
    from stiracore.control.forgetting_archive import _ID, AUDIT_LOG_NAME
    from stiracore.control.removal_archive import JOB_PREFIX, REMOVAL_PREFIX

    if name == AUDIT_LOG_NAME:
        return True
    stem = name[:-5] if name.endswith(".json") else ""
    for prefix in ("", REMOVAL_PREFIX, JOB_PREFIX):
        if stem.startswith(prefix) and _ID.fullmatch(stem[len(prefix) :]):
            return True
    return False


def _plan_strays(layout: DataLayout, plan: ForgetPlan, needles: tuple[str, ...]) -> None:
    """Files in the reached stores that are not entries/records themselves (temporary
    files of an interrupted write, stray copies) and hold the request: removed too."""
    memory = layout.memory_dir
    known_root = {
        layout.index.name,
        layout.rejections.name,
        "emotion-log.jsonl",  # not reached: audit log (file names and reasons only)
    }
    candidates: list[Path] = []
    errors: list[str] = []
    try:
        names = sorted(os.listdir(memory)) if memory.is_dir() else []
    except OSError:
        names = []
        errors.append(layout.relative(memory))
    for name in names:
        path = memory / name
        if path.is_symlink() or not path.is_file():
            continue
        if name in known_root or name.startswith(f"{layout.index.name}-"):
            continue
        candidates.append(path)
    for layer in (MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC):
        for path, _info in walk_files(layout, layout.layer_dir(layer), errors):
            if not (path.suffix == ".md" and not path.name.startswith(".")):
                candidates.append(path)
    planned = {path for name, path in plan.removal.files if name == "emotion_record"}
    for path, _info in walk_files(layout, memory / "emotion", errors):
        if path not in planned:  # any other file there that holds the text, record or not
            candidates.append(path)
    for path, _info in walk_files(layout, layout.archive_dir, errors):
        if not _is_record_name(path.name):
            candidates.append(path)
    plan.skipped.extend(f"{where} not readable" for where in errors)
    for path in candidates:
        data = _read(layout, plan, path)
        if data is not None and contains(data.decode("utf-8", "replace"), needles):
            plan.removal.files.append(("stray_file", path))
            plan.add("stray_files", layout.relative(path))


def _metadata(text: str) -> dict[str, Any]:
    try:
        return MemoryStore._parse(text)[1]
    except ValueError:
        return {}


def _archive_view(layout: DataLayout, plan: ForgetPlan) -> _ArchiveView:
    """Every archived copy (#320 forgetting records, retention records), read only."""
    view = _ArchiveView()
    archive = RemovalArchive(layout)
    try:
        forgetting = archive.forgetting_ids()
        removals = archive.ids()
    except ForgettingArchiveError as error:
        plan.skipped.append(f"archive: {error}")
        return view
    for archive_id in forgetting:
        try:
            record = archive.load_forgetting(archive_id)
        except ForgettingArchiveError as error:
            plan.skipped.append(f"forgotten/{archive_id}.json: {error}")
            continue
        view.forgetting.append((archive_id, record))
        text = str(record.get("original_text", ""))
        metadata = _metadata(text)
        if record.get("source_id"):
            metadata.setdefault("source_id", record.get("source_id"))
        view.memory.append(
            _ArchivedMemory(MemoryLayer.SEMANTIC, str(record.get("key", "")), text, metadata)
        )
    for removal_id in removals:
        try:
            record = archive.load(removal_id)
        except ForgettingArchiveError as error:
            plan.skipped.append(f"forgotten/removed-{removal_id}.json: {error}")
            continue
        view.removals.append((removal_id, record))
        for item in record.items:
            if item.get("store") == "memory":
                try:
                    layer = MemoryLayer(item.get("layer"))
                except ValueError:
                    continue
                text = str(item.get("text", ""))
                view.memory.append(
                    _ArchivedMemory(layer, str(item.get("key", "")), text, _metadata(text))
                )
    return view


def _plan_archive(plan, view, needles, episodes, semantic_keys, emotion_paths) -> None:
    for archive_id, record in view.forgetting:
        text = str(record.get("original_text", ""))
        if (
            record.get("key") in semantic_keys
            or record.get("source_id") in episodes
            or str(_metadata(text).get("source_id", "")) in episodes
            or contains(text, needles)
        ):
            plan.purge_forgetting.append(archive_id)
            plan.archive_copies.setdefault("forgetting_records", []).append(archive_id)
    for removal_id, record in view.removals:
        hits = [
            position
            for position, item in enumerate(record.items)
            if _item_matches(item, needles, episodes, semantic_keys, emotion_paths, plan.known)
        ]
        if hits:
            plan.purge_removal[removal_id] = hits
            plan.archive_copies.setdefault("removal_records", []).append(removal_id)


def _item_text(item: dict[str, Any]) -> str:
    """What an archived item holds, decoded (Base64 is an encoding, not a hiding place)."""
    if "data_b64" in item:
        import base64
        import binascii

        try:
            return base64.b64decode(str(item["data_b64"]), validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return json.dumps(item, ensure_ascii=False)  # undecodable: searched as stored
    if isinstance(item.get("row"), dict):
        return "\n".join(str(value) for value in item["row"].values())
    if "line" in item:
        return str(item["line"])
    return str(item.get("text", ""))


def _item_matches(
    item, needles, episodes, semantic_keys, emotion_paths=frozenset(), known=frozenset()
) -> bool:
    kind = item.get("store")
    if known:
        if kind == "memory" and _key_digest(str(item.get("layer")), str(item.get("key"))) in known:
            return True
        if "path" in item and sha256_text(str(item["path"])) in known:
            return True
    if kind == "memory_plan" and names_any(_item_text(item), episodes | semantic_keys):
        return True  # a retention copy of a plan file that names a forgotten entry
    if kind == "memory":
        text = str(item.get("text", ""))
        if item.get("layer") == "episodic" and item.get("key") in episodes:
            return True
        if item.get("layer") == "semantic" and (
            item.get("key") in semantic_keys
            or str(_metadata(text).get("source_id", "")) in episodes
        ):
            return True
    elif kind == "emotion_record" and item.get("path") in emotion_paths:
        return True
    elif kind == "staging_rejection":
        try:
            line = json.loads(str(item.get("line", "")))
        except ValueError:
            line = {}
        if isinstance(line, dict) and (
            line.get("source_id") in episodes or line.get("key") in semantic_keys
        ):
            return True
    text = _item_text(item)
    return contains(text, needles) or contains(json.dumps(item, ensure_ascii=False), needles)


def _classify(relative: str) -> str:
    name = relative.rsplit("/", 1)[-1]
    for known in ("events.db", "scheduler-events.jsonl", "hitl-approvals.db", "hitl-operations.db"):
        if name.startswith(known):
            return known
    if name in ("forgetting-log.jsonl", "emotion-log.jsonl"):
        return name
    if "/procedural/" in f"/{relative}":
        return "procedural"
    if relative.startswith("workspace/"):
        return "workspace"
    if name.startswith("distill-runs"):
        return "distill-runs"
    if name.endswith(".json") and "/" not in relative or name.startswith("settings-history"):
        return "settings"
    return "other"


def _handled(layout: DataLayout, relative: str) -> bool:
    """Files the planner itself reads (their matches are in ``reached``/archive)."""
    memory = layout.relative(layout.memory_dir)
    if relative.startswith(
        (
            f"{memory}/episodic/",
            f"{memory}/semantic/",
            f"{memory}/emotion/",
            f"{memory}/{PLANS_DIRNAME}/",
        )
    ):
        return True
    if relative.startswith(f"{memory}/forgotten/") and not relative.endswith(
        "forgetting-log.jsonl"
    ):
        return True
    handled = {
        layout.relative(layout.index),
        layout.relative(layout.rejections),
        layout.relative(layout.turns),
    }
    if relative in handled or any(relative.startswith(f"{h}-") for h in handled):
        return True  # also -wal / -shm / -journal
    return relative.startswith(("checkpoints/", "context-stash/"))


def _is_stray_place(layout: DataLayout, relative: str) -> bool:
    """A file directly in the memory directory that is none of the known files."""
    memory = layout.relative(layout.memory_dir)
    if not relative.startswith(f"{memory}/") or "/" in relative[len(memory) + 1 :]:
        return False
    name = relative.rsplit("/", 1)[-1]
    return name != "emotion-log.jsonl"


def _in_reached_area(layout: DataLayout, relative: str) -> bool:
    """A file or directory that is (or contains) a store a delete request reaches."""
    if _handled(layout, relative) or _handled(layout, f"{relative}/x"):
        return True
    if _is_stray_place(layout, relative):
        return True
    roots = (layout.memory_dir, layout.checkpoints, layout.stash, layout.turns, layout.index)
    return relative in ("", ".") or any(
        layout.relative(root) == relative or layout.relative(root).startswith(f"{relative}/")
        for root in roots
    )


def _unreached(layout: DataLayout, needles: tuple[str, ...]) -> list[dict[str, Any]]:
    """Other files of the data directory whose raw bytes contain the request.

    What cannot be read is listed too (never skipped silently): outside the reached
    stores as "not readable", inside them it is a store the planner could not see.
    """
    found = []
    if not needles:
        return found
    errors: list[str] = []
    for path, _info in walk_files(layout, layout.data_dir, errors):
        relative = layout.relative(path)
        if _handled(layout, relative) or _is_stray_place(layout, relative):
            continue
        try:
            data = read_bytes(layout, path)
        except UnsafePathError:
            errors.append(relative)
            continue
        if data is not None and contains(data.decode("utf-8", "replace"), needles):
            kind = _classify(relative)
            found.append({"where": relative, "reason": NOT_REACHED[kind]})
    for where in errors:
        if not _in_reached_area(layout, where):
            found.append({"where": where, "reason": NOT_READABLE})
    return found


NOT_READABLE = "not readable: could not be searched"
UNREADABLE_LEFT = "a reached store could not be read (the job fails)"
REACHED_LEFT = "a reached store still holds it (the job fails)"
ARCHIVE_LEFT = "archived copy (a delete request removes it; the job fails)"


def residual(
    layout: DataLayout, request: ForgetRequest, *, ids: bool = True
) -> list[dict[str, Any]]:
    """Every file of the data directory whose raw bytes still contain the request.

    With ``ids`` the requested keys are searched as well (informative only: a key can
    be a substring of another key); the job's verdict uses the queries only.
    """
    needles = needles_for(request.queries) + (_id_needles(request) if ids else ())
    found = []
    errors: list[str] = []
    for path, _info in walk_files(layout, layout.data_dir, errors):
        try:
            data = read_bytes(layout, path)
        except UnsafePathError:
            errors.append(layout.relative(path))
            continue
        if data is None or not contains(data.decode("utf-8", "replace"), needles):
            continue
        relative = layout.relative(path)
        if relative.startswith(f"{layout.relative(layout.archive_dir)}/removed-") or (
            relative.startswith(f"{layout.relative(layout.archive_dir)}/")
            and not relative.endswith("forgetting-log.jsonl")
        ):
            kind = ARCHIVE_LEFT
        elif _handled(layout, relative) or _is_stray_place(layout, relative):
            kind = REACHED_LEFT
        else:
            kind = NOT_REACHED[_classify(relative)]
        found.append({"where": relative, "reason": kind})
    for where in errors:  # what was not searched is not "clean"
        reason = UNREADABLE_LEFT if _in_reached_area(layout, where) else NOT_READABLE
        found.append({"where": where, "reason": reason})
    return found


# -- the delete job -----------------------------------------------------------
#
# A delete request is irreversible and reaches the archive too: unlike forgetting
# (#320) and retention runs, which archive first and are undone explicitly, a delete
# keeps no copy of what it removes. It runs as a job with a state file
# ``forgotten/delete-<id>.json`` (pending -> running -> completed | failed, attempts):
# an inventory of every location (kept in memory; the file holds their digests), executed
# step by step, saved after each step. Steps only ever remove, so a
# retry or a resume after a crash never brings anything back; an item whose digest
# changed since the inventory is left and the step fails (run a new request).
# Search sources go first (entries, then their index rows). Every step is checked
# afterwards; the derived steps (index regeneration, compaction) wait for failed
# removals and re-run on every attempt; then the request is planned again and what is
# still found is removed in a second round or fails the job; the raw-byte residual check
# of every file fails it too when a reached store still holds the text. The job file
# keeps digests only (query, ids, step locations) in every state, so a resume takes the
# same --id/--query again to locate the job's own steps; it removes only those, and only
# while their content still has the digest the job saw (no second round, #402).

JOB_STATES = ("pending", "running", "completed", "failed")
# Steps that unlink one whole file (by the digest the inventory saw).
_FILE_STEPS = ("checkpoint", "context_stash", "emotion_record", "stray_file", "memory_plan")


def _sha_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _inventory(layout: DataLayout, plan: ForgetPlan) -> list[dict[str, Any]]:
    """The job's steps, in execution order, with the digest of what each removes."""
    steps: list[dict[str, Any]] = []
    removal = plan.removal

    def add(store: str, target: dict[str, Any], digest: str | None = None) -> None:
        steps.append({"store": store, "target": target, "sha256": digest, "status": "pending"})

    for store_name, path in removal.files:
        if store_name == "emotion_record":  # before its episode, as MemoryStore.delete
            data = read_bytes(layout, path)
            if data is not None:
                add(store_name, {"path": layout.relative(path)}, _sha_bytes(data))
    for layer, key in removal.memory:
        data = read_bytes(layout, layout.layer_dir(layer) / f"{key}.md")
        if data is not None:
            add("memory", {"layer": layer.value, "key": key}, _sha_bytes(data))
    for layer, key in removal.memory:
        add("index_row", {"layer": layer.value, "key": key})
    for layer_name, key in removal.index_rows:
        add("index_row", {"layer": layer_name, "key": key})
    if removal.regenerate_index:
        add("semantic_index_md", {})
    data = read_bytes(layout, layout.rejections)
    if data is not None and removal.rejection_lines:
        lines = data.decode("utf-8", "replace").splitlines()
        for number in sorted(set(removal.rejection_lines)):
            add("staging_rejection", {"line": number + 1}, sha256_text(lines[number]))
    for store_name, path in removal.files:
        if store_name == "emotion_record":
            continue
        data = read_bytes(layout, path)
        if data is not None:
            add(store_name, {"path": layout.relative(path)}, _sha_bytes(data))
    if removal.turn_events or removal.tool_payloads:
        connection = connect_read_only(layout, layout.turns)
        if connection is not None:
            try:
                for row_id in removal.turn_events:
                    row = connection.execute(
                        "SELECT payload FROM ui_events WHERE id = ?", (row_id,)
                    ).fetchone()
                    if row is not None:
                        add("turn_event", {"id": row_id}, sha256_text(row[0]))
                for turn_id, tool_use_id, kind in removal.tool_payloads:
                    row = connection.execute(
                        "SELECT text FROM tool_payloads WHERE turn_id = ? AND tool_use_id = ?"
                        " AND kind = ?",
                        (turn_id, tool_use_id, kind),
                    ).fetchone()
                    if row is not None:
                        target = {"turn_id": turn_id, "tool_use_id": tool_use_id, "kind": kind}
                        add("tool_payload", target, sha256_text(row[0]))
            finally:
                connection.close()
    for archive_id in plan.purge_forgetting:
        add("forgetting_record", {"id": archive_id})
    for removal_id, positions in plan.purge_removal.items():
        add("removal_record", {"id": removal_id, "positions": sorted(positions)})
    if any(step["store"] in ("memory", "index_row") for step in steps):
        add("compact", {"file": "index"})
    if any(step["store"] in ("turn_event", "tool_payload") for step in steps):
        add("compact", {"file": "turns"})
    return steps


class _Changed(Exception):
    """The item is no longer what the inventory saw: left in place, step failed.

    Raised only with fixed messages (they are stored as the step's code).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _run_step(layout: DataLayout, store: MemoryStore, archive: RemovalArchive, step, at) -> None:
    from stiracore.control.memory_removal import _compact

    target = step["target"]
    kind = step["store"]
    if kind == "memory":
        layer = MemoryLayer(target["layer"])
        path = store._path(layer, target["key"])
        store._require_writable(path)
        data = read_bytes(layout, path)
        if data is None:
            return
        if _sha_bytes(data) != step["sha256"]:
            raise _Changed("entry_changed_since_inventory")
        path.unlink()
    elif kind == "index_row":
        with store._connection() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?",
                (target["layer"], target["key"]),
            )
    elif kind == "semantic_index_md":
        from stiracore.control.harvest import write_semantic_index

        write_semantic_index(store)
    elif kind == "staging_rejection":
        from stiracore.control.memory_removal import _atomic_write

        data = read_bytes(layout, layout.rejections)
        if data is None:
            return
        lines = data.decode("utf-8", "replace").splitlines()
        # One line per step (identical lines are one step each): the first line with the
        # step's digest, wherever it is now (an earlier line may have gone).
        found = next(
            (n for n, line in enumerate(lines) if sha256_text(line) == step["sha256"]), None
        )
        kept = lines if found is None else lines[:found] + lines[found + 1 :]
        if len(kept) != len(lines):
            _atomic_write(layout.rejections, ("\n".join(kept) + "\n" if kept else "").encode())
    elif kind in _FILE_STEPS:
        path = layout.data_dir / target["path"]
        if not path.resolve().is_relative_to(layout.data_dir):
            raise UnsafePathError("job names a path outside the data directory")
        data = read_bytes(layout, path)
        if data is None:
            return
        if _sha_bytes(data) != step["sha256"]:
            raise _Changed("file_changed_since_inventory")
        os.unlink(path)
    elif kind in ("turn_event", "tool_payload"):
        layout.check_inside(layout.turns)
        if not layout.turns.exists():
            return
        connection = sqlite3.connect(layout.turns, timeout=30)
        try:
            with connection:
                connection.execute("PRAGMA secure_delete = ON")
                if kind == "turn_event":
                    where, args = "id = ?", (target["id"],)
                    column, table = "payload", "ui_events"
                else:
                    where = "turn_id = ? AND tool_use_id = ? AND kind = ?"
                    args = (target["turn_id"], target["tool_use_id"], target["kind"])
                    column, table = "text", "tool_payloads"
                row = connection.execute(
                    f"SELECT {column} FROM {table} WHERE {where}", args
                ).fetchone()
                if row is None:
                    return
                if sha256_text(row[0]) != step["sha256"]:
                    raise _Changed("row_changed_since_inventory")
                connection.execute(f"DELETE FROM {table} WHERE {where}", args)
        finally:
            connection.close()
    elif kind == "forgetting_record":
        archive.remove_forgetting(target["id"])
        archive.audit("purge.applied", id=target["id"], record="forgetting", job=True)
    elif kind == "removal_record":
        try:
            record = archive.load(target["id"])
        except ForgettingArchiveError as error:
            if "no record" in str(error):
                return  # already rewritten or removed by an earlier attempt
            raise
        drop = set(target["positions"])
        kept = [item for n, item in enumerate(record.items) if n not in drop]
        replaced_by = archive.save(record.purpose, record.reason, kept, at).id if kept else None
        archive.remove(target["id"])
        archive.audit(
            "purge.applied",
            id=target["id"],
            record="removal",
            dropped=len(drop),
            replaced_by=replaced_by,
        )
    elif kind == "compact":
        path = store.index_path if target["file"] == "index" else layout.turns
        if path.exists():
            _compact(path, wal=target["file"] == "turns")
    else:  # pragma: no cover - the inventory writes only the kinds above
        raise ValueError(f"unknown step {kind}")


def _summary(steps: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for step in steps:
        counts = found.setdefault(step["store"], {"done": 0, "failed": 0, "pending": 0})
        counts[step["status"]] += 1
    return found


_DERIVED = frozenset({"semantic_index_md", "compact"})


def _leftovers(
    layout: DataLayout, request: ForgetRequest, known: frozenset[str] | set[str] = frozenset()
) -> tuple[ForgetPlan, list[dict[str, str]]]:
    """Plan the request again: what a reached store or the archive still holds.

    Returned as fixed labels and digests only (it is persisted in the job file and
    printed): a location can carry the very words being deleted.
    """
    plan = plan_forget(layout, request, known=known)
    left = [{"what": "reached", "area": name} for name in sorted(plan.reached)]
    left += [{"what": "archive", "area": name} for name in sorted(plan.archive_copies)]
    left += [
        {"what": "not_inspected", "where_sha256": sha256_text(reason)}
        for reason in sorted(set(plan.skipped))
    ]
    # The raw bytes of every file, with the planner's own idea of the stores: a reached
    # store or the archive that still holds the text (or that could not be read) fails
    # the job, not just a report.
    left += [
        {"what": "residual", **_located(layout, item["where"])}
        for item in residual(layout, request, ids=False)
        if item["reason"] in (REACHED_LEFT, ARCHIVE_LEFT, UNREADABLE_LEFT)
    ]
    return plan, left


def public_residual(layout: DataLayout, found: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Residual entries as the job output shows them: area, digest, reason — no path."""
    return [{**_located(layout, item["where"]), "reason": item["reason"]} for item in found]


_ERROR_CODES = (
    (UnsafePathError, "E_UNSAFE_PATH"),
    (PermissionError, "E_PERMISSION"),
    (FileNotFoundError, "E_MISSING"),
    (sqlite3.Error, "E_SQLITE"),
    (ForgettingArchiveError, "E_ARCHIVE"),
    (OSError, "E_OS"),
    (ValueError, "E_VALUE"),
)


def _error_code(error: BaseException) -> str:
    """A fixed code and the exception type, never its message (it can hold a path)."""
    if isinstance(error, _Changed):
        return f"E_CHANGED: {error.code}"
    for kind, code in _ERROR_CODES:
        if isinstance(error, kind):
            return f"{code} ({type(error).__name__})"
    return f"E_FAILED ({type(error).__name__})"


_DIGEST = re.compile(r"[0-9a-f]{64}")


def _known(job: dict[str, Any]) -> set[str]:
    return {str(v) for v in job.get("resolved_sha256", []) if _DIGEST.fullmatch(str(v))}


def _hashed(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _save(archive: RemovalArchive, job: dict[str, Any]) -> None:
    """Persist the job with digests only, in every state.

    Keys and paths can carry the words of what is deleted (slugs, titles), so the file
    holds the SHA-256 of each step's location and of each requested id, never the
    values; ``--resume`` takes the same ``--id``/``--query`` again and re-plans.
    """
    # Allow-list: only these fields, each of a known shape, are ever written.
    stored = {name: job[name] for name in _JOB_FIELDS if name in job}
    if "ids" in job:  # in memory during a run; the file only ever gets the digests
        stored["ids_sha256"] = sorted(sha256_text(value) for value in job["ids"])
    else:
        stored["ids_sha256"] = [str(v) for v in job.get("ids_sha256", [])]
    stored["steps"] = [
        {
            "store": step["store"],
            "target_sha256": step.get("target_sha256") or _hashed(step["target"]),
            "sha256": step.get("sha256"),
            "status": step["status"],
            "error": _stored_error(step.get("error")),
        }
        for step in job.get("steps", [])
    ]
    left = (job.get("verification") or {}).get("left", [])
    stored["verification"] = {
        "left": [
            {key: str(item[key]) for key in ("what", "area", "where_sha256") if key in item}
            for item in left
            if isinstance(item, dict)
        ]
    }
    archive.save_job(stored)


_JOB_FIELDS = (
    "format",
    "id",
    "state",
    "attempts",
    "created_at",
    "updated_at",
    "query_sha256",
    "summary",
    "resolved_sha256",
)
_STORED_ERROR = re.compile(r"E_[A-Z_]+(: [a-z_]+| \([A-Za-z_]+\))?|waits for the failed steps")


def _stored_error(value: Any) -> str | None:
    """Only codes produced by :func:`_error_code` (anything else is replaced)."""
    if value is None:
        return None
    return value if isinstance(value, str) and _STORED_ERROR.fullmatch(value) else "E_FAILED"


def _run_round(layout, store, archive, job, at) -> None:
    """Removal steps first; the derived ones (index, compaction) only if all of those
    succeeded, and on every attempt (they depend on what the removals left)."""
    for step in job["steps"]:
        if step["store"] in _DERIVED and step["status"] == "done":
            step["status"] = "pending"
    removal_failed = False
    for derived in (False, True):
        for step in job["steps"]:
            if step["status"] == "done" or (step["store"] in _DERIVED) != derived:
                continue
            if (derived and removal_failed) or _entry_kept(job, step):
                step["status"], step["error"] = "pending", "waits for the failed steps"
                removal_failed = removal_failed or not derived
                continue
            try:
                _run_step(layout, store, archive, step, at)
                _check_step(layout, store, job, step)
            except (_Changed, OSError, ValueError, sqlite3.Error, ForgettingArchiveError) as error:
                step["status"], step["error"] = "failed", _error_code(error)
                removal_failed = removal_failed or not derived
            else:
                step["status"], step["error"] = "done", None
            _save(archive, job)


def _entry_kept(job: dict[str, Any], step: dict[str, Any]) -> bool:
    """The index row of an entry left in place because it changed since the inventory
    stays too (it indexes the new content, which is not what the job removes)."""
    if step["store"] != "index_row":
        return False
    return any(
        other["store"] == "memory"
        and other["target"] == step["target"]
        and str(other.get("error") or "").startswith("E_CHANGED: entry_changed")
        for other in job["steps"]
    )


def _check_step(layout, store, job, step) -> None:
    """A step is done only when what it removed is gone (checked, not assumed)."""
    target = step["target"]
    if step["store"] == "memory":
        if store.read_text(MemoryLayer(target["layer"]), target["key"]) is not None:
            raise _Changed("entry_still_there")
    elif step["store"] == "semantic_index_md":
        text = store.read_text(MemoryLayer.SEMANTIC, "index") or ""
        keys = [
            s["target"]["key"]
            for s in job["steps"]
            if s["store"] == "memory" and s["target"]["layer"] == MemoryLayer.SEMANTIC.value
        ]
        if any(_index_lists(text, key) for key in keys):
            raise _Changed("index_md_still_lists_entry")
    elif step["store"] in _FILE_STEPS and (
        read_bytes(layout, layout.data_dir / target["path"]) is not None
    ):
        raise _Changed("file_still_there")


def _run_job(
    layout: DataLayout,
    archive: RemovalArchive,
    job: dict[str, Any],
    at,
    request: ForgetRequest,
    *,
    extend: bool = True,
) -> dict:
    """Run the job's steps; with ``extend`` (a new request, not a resume) copies the
    verification still finds are inventoried and removed in a second round."""
    store = MemoryStore(layout.memory_base, layout.agent_id)
    job["state"] = "running"
    job["attempts"] = int(job.get("attempts", 0)) + 1
    job["updated_at"] = at.isoformat()
    _save(archive, job)
    archive.audit("delete.started", id=job["id"], attempt=job["attempts"], steps=len(job["steps"]))
    left: list[dict[str, str]] = []
    for _round in range(2):
        _run_round(layout, store, archive, job, at)
        if any(step["status"] != "done" for step in job["steps"]):
            _again, left = _leftovers(layout, request, _known(job))  # for the record
            break
        # Verified, not assumed: the request is planned again over every store.
        again, left = _leftovers(layout, request, _known(job))
        if not extend:
            break  # a resume removes only what the job inventoried (what is left fails it)
        job["resolved_sha256"] = sorted(_known(job) | again.resolved())
        if not left:
            break
        extra = _inventory(layout, again)  # copies found now (missed, or new): next round
        known = {(s["store"], json.dumps(s["target"], sort_keys=True)) for s in job["steps"]}
        added = [
            s for s in extra if (s["store"], json.dumps(s["target"], sort_keys=True)) not in known
        ]
        job["steps"].extend(added)
        _save(archive, job)
        if not added:
            break
    summary = _summary(job["steps"])
    failed = sum(counts["failed"] + counts["pending"] for counts in summary.values())
    job["summary"] = summary
    job["verification"] = {"left": left} if left else {"left": []}
    job["updated_at"] = at.isoformat()
    if failed or left:
        job["state"] = "failed"
        _save(archive, job)
        archive.audit(
            "delete.failed", id=job["id"], attempt=job["attempts"], failed=failed, left=len(left)
        )
    else:
        job["state"] = "completed"
        job["steps"] = []  # nothing is left to do
        _save(archive, job)
        archive.audit("delete.completed", id=job["id"], attempt=job["attempts"], counts=summary)
    return job


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "state": job["state"],
        "attempts": job.get("attempts", 0),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "summary": job.get("summary") or _summary(job.get("steps", [])),
        "left": (job.get("verification") or {}).get("left", []),
        "failed_steps": [
            {"store": step["store"], "error": _stored_error(step.get("error"))}
            for step in job.get("steps", [])
            if step["status"] == "failed"
        ],
        # Not run because an earlier step failed (re-run by --resume).
        "waiting_steps": sorted(
            {step["store"] for step in job.get("steps", []) if step["status"] == "pending"}
        ),
    }


def apply_forget(layout: DataLayout, plan: ForgetPlan, *, at: datetime) -> dict[str, Any]:
    """Create the delete job for ``plan`` and run it. Irreversible; the caller holds the lock.

    Refused before anything changes when a store could not be inspected (``skipped``):
    a delete that cannot see everywhere is not started.
    """
    request = plan.request
    if plan.skipped:
        return {
            "job": None,
            "error": f"E_NOT_INSPECTED: {len(set(plan.skipped))} store(s) could not be "
            "inspected (the dry run lists them)",
            "residual": public_residual(layout, residual(layout, request)),
        }
    archive = RemovalArchive(layout)
    try:
        archive.prepare()
        job = {
            "format": 1,
            "id": archive.new_id(at),
            "state": "pending",
            "attempts": 0,
            "created_at": at.isoformat(),
            "updated_at": at.isoformat(),
            "query_sha256": request.digests(),
            "ids": list(_id_keys(request)),
            "steps": _inventory(layout, plan),
            # Digests of what the request resolved to (derived entries included): a
            # resume plans with them, so nothing found only through a source is lost.
            "resolved_sha256": sorted(plan.resolved()),
        }
        _save(archive, job)
        job = _run_job(layout, archive, job, at, request)
    except (ForgettingArchiveError, OSError, ValueError) as error:
        archive.close()
        return {
            "job": None,
            "error": _error_code(error),
            "residual": public_residual(layout, residual(layout, request)),
        }
    archive.close()
    return {
        "job": _job_view(job),
        "error": None,
        "residual": public_residual(layout, residual(layout, request)),
    }


def resume_forget(
    layout: DataLayout,
    job_id: str,
    *,
    at: datetime,
    queries: tuple[str, ...] = (),
    ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Finish a job: the steps it inventoried, compared with the digests it saw then.

    The job keeps digests only, so the same ``--id``/``--query`` must be given again
    (checked against them; refused otherwise — nothing could be verified). The request
    is planned again only to find the locations behind the job's digests: a step is run
    when its location digest is one of the job's, and it removes the item only if its
    content still has the digest the job saw (else it is left and the step fails). A
    copy the job never inventoried is not removed (the verification lists it and the job
    fails: a new delete request reaches it). Steps only remove: a resume never brings
    anything back.
    """
    archive = RemovalArchive(layout)
    try:
        archive.prepare()
        job = archive.load_job(job_id)
        if job.get("state") not in JOB_STATES:
            raise ForgettingArchiveError("delete job has an unknown state")
        if job["state"] == "completed":
            return {"job": _job_view(job), "error": None, "residual": []}
        request = ForgetRequest.parse(list(ids), list(queries))
        if sorted(sha256_text(q) for q in request.queries) != sorted(
            job.get("query_sha256", [])
        ) or sorted(sha256_text(i) for i in _id_keys(request)) != sorted(job.get("ids_sha256", [])):
            raise ValueError(
                "pass the same --id/--query as the delete request (the job keeps only "
                "their digests, and nothing can be verified without them)"
            )
        plan = plan_forget(layout, request, known=_known(job))
        if plan.skipped:
            return {
                "job": None,
                "error": f"E_NOT_INSPECTED: {len(set(plan.skipped))} store(s) could not be "
                "inspected (the dry run lists them)",
            }
        job["ids"] = list(_id_keys(request))
        job["steps"] = _resumed_steps(job, _inventory(layout, plan))
        job = _run_job(layout, archive, job, at, request, extend=False)
    except (ForgettingArchiveError, OSError, ValueError) as error:
        # Request errors are fixed text (they name options, never data); anything else
        # — a path can be in an OSError or UnsafePathError — is a code and type only.
        fixed = isinstance(error, ValueError) and not isinstance(error, UnsafePathError)
        return {"job": None, "error": str(error) if fixed else _error_code(error)}
    finally:
        archive.close()
    return {
        "job": _job_view(job),
        "error": None,
        "residual": public_residual(layout, residual(layout, request)),
    }


def _resumed_steps(job: dict[str, Any], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The job's own steps, located again, each with the content digest the job saw.

    ``current`` is the inventory of the request planned again (with the job's resolved
    digests). A step whose location digest the job does not have is dropped (never
    inventoried by this job); a kept step takes the job's stored content digest, so an
    item rewritten since the job's inventory no longer matches and is left in place. A
    staging-rejection line is identified by its content digest only (line numbers move),
    at most as many lines per digest as the job still had to remove. The
    derived steps (index regeneration, compaction) remove no item and are kept.
    """
    stored = {
        (str(step.get("store")), str(step.get("target_sha256"))): step
        for step in job.get("steps", [])
        if isinstance(step, dict)
    }
    # A rejection line has no stable place (line numbers move when an earlier line goes):
    # it is matched by content digest only, as many lines per digest as the job had.
    lines: dict[str, int] = {}
    for step in job.get("steps", []):
        if (
            isinstance(step, dict)
            and step.get("store") == "staging_rejection"
            and step.get("status") != "done"
            and _DIGEST.fullmatch(str(step.get("sha256")))
        ):
            lines[str(step["sha256"])] = lines.get(str(step["sha256"]), 0) + 1
    steps: list[dict[str, Any]] = []
    for step in current:
        if step["store"] in _DERIVED:
            steps.append(step)
            continue
        if step["store"] == "staging_rejection":
            if lines.get(step["sha256"], 0) > 0:  # one of the job's lines, maybe moved
                lines[step["sha256"]] -= 1
                steps.append(step)
            continue  # a line the job never inventoried (also at one of its numbers)
        seen = stored.get((step["store"], _hashed(step["target"])))
        if seen is None:
            continue
        steps.append({**step, "sha256": seen.get("sha256")})
    # A derived step of the job is kept even when the request planned again no longer
    # needs it (every row it follows is gone already): a compaction that failed or was
    # interrupted after the removals must still run, or the text can stay in free pages.
    present = {(step["store"], _hashed(step["target"])) for step in steps}
    for store_name, target in _DERIVED_TARGETS:
        location = (store_name, _hashed(target))
        if location in stored and location not in present:
            steps.append(
                {"store": store_name, "target": target, "sha256": None, "status": "pending"}
            )
    return steps


# Every target a derived step can have (the job file keeps only their digests).
_DERIVED_TARGETS = (
    ("semantic_index_md", {}),
    ("compact", {"file": "index"}),
    ("compact", {"file": "turns"}),
)


def list_jobs(layout: DataLayout) -> list[dict[str, Any]]:
    archive = RemovalArchive(layout)
    found = []
    for job_id in archive.job_ids():
        try:
            found.append(_job_view(archive.load_job(job_id)))
        except ForgettingArchiveError as error:
            found.append({"id": job_id, "error": str(error)})
    return found
