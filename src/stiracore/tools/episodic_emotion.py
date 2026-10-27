"""The emotional state at the time an episode was saved (#364), kept on the floor.

An ``episodic-summary/v2`` episode is a v1 episode whose post-turn emotion snapshot
was captured when it was written: the six Neural State dimensions, the classifier
category and intensity of that turn, the breaker level, the engine turn number and
the UI ``turn_id``. The snapshot is **not** in the episode's frontmatter. The episodic
layer is readable and writable by the agent's file tools and its metadata is shown by
``memory_read`` / ``memory_search``, while the rest of the data directory is on the
read and write floor (#271/#312). So the snapshot is a sidecar record in
``<memory_dir>/emotion/`` next to the layer directories (like ``forgotten/``, #294):

- the memory tools, the search index, Harvest and the prompt never see it (no tokens);
- the file tools cannot read or write it (the data-directory floor);
- the episode only says ``format: episodic-summary/v2``.

A record is bound to its episode by key and by the SHA-256 of the stored body; a
record that does not match (the agent rewrote the episode, another key) reads as
missing values (``None``), as does a v1 episode, which has no record. Old files are
never rewritten.

Reading the state is read-only: :func:`capture_emotion_snapshot` copies values under
``engine.state_lock`` and never calls ``apply_stimulus``, ``post_turn`` or the
classifier. Nothing here changes ``NeuralState``, ``EMOTION_MATRIX`` or classification.

Emotion-proximity recall (:func:`recall_by_emotion`) is an opt-in library function:
nothing calls it, the default memory search is unchanged, and it calls no model.
Design, formula and tolerance: ``docs/episodic-emotion.md``.

The mood distance, similarity and valence index (:func:`mood_similarity`,
:func:`polarity`) are what the opt-in ``emotion_proximity`` signal of associative recall
uses (#364, ADR 0017): pure functions of two states, no I/O.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stiracore.core.engine import BreakLevel
from stiracore.core.neural_state import BASELINES, DIMS, EMOTION_MATRIX

from .memory_store import MemoryEntry, MemoryLayer, MemoryStore

logger = logging.getLogger(__name__)

FORMAT_V1 = "episodic-summary/v1"
FORMAT_V2 = "episodic-summary/v2"
RECORD_SCHEMA = "episodic-emotion/v1"
EMOTION_DIRNAME = "emotion"
# The six dimensions are stored raw, as captured (#364, operator decision 2026-10-20: no
# rounding or bucketing). Records written before that carry 3 decimals (|error| <= 0.0005 per
# dimension, DISTANCE_TOLERANCE). The intensity is still rounded to PRECISION decimals.
PRECISION = 3
MAX_RECORD_BYTES = 4096
MAX_RECALL_WINDOW = 1000
DISTANCE_TOLERANCE = 0.5 * 10**-PRECISION
# Audit log of the orphan clean-up, next to ``emotion/`` (ids and reasons only, no values).
LOG_FILENAME = "emotion-log.jsonl"
TEMPORARY_PREFIX = ".emotion-"
# A temporary file younger than this may belong to a write in progress: left alone.
TEMPORARY_GRACE_SEC = 300.0

_TURN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BREAKER_LEVELS = frozenset(level.value for level in BreakLevel)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _unit(value: Any) -> float | None:
    """A finite number in [0, 1], kept raw (not rounded); anything else is ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _intensity(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        return None
    return round(number, PRECISION)


@dataclass(frozen=True)
class EmotionSnapshot:
    """One episode's emotion at save time. Every field may be ``None`` (unknown).

    ``status`` says where the values came from: ``"captured"`` (just taken),
    ``"stored"`` (a matching record), ``"v1"`` (an older episode, no record),
    ``"missing"`` (v2 but no readable record) or ``"mismatch"`` (a record that does
    not belong to this episode body).
    """

    state: Mapping[str, float | None] = field(default_factory=lambda: dict.fromkeys(DIMS))
    category: str | None = None
    intensity: float | None = None
    breaker: str | None = None
    turn: int | None = None
    turn_id: str | None = None
    status: str = "missing"

    @property
    def stability(self) -> float | None:
        return self.state.get("stability")

    @property
    def complete(self) -> bool:
        """All six dimensions are known (required for proximity recall)."""
        return all(self.state.get(dim) is not None for dim in DIMS)

    def as_record(self) -> dict[str, Any]:
        return {
            "state": {dim: self.state.get(dim) for dim in DIMS},
            "category": self.category,
            "intensity": self.intensity,
            "breaker": self.breaker,
            "turn": self.turn,
            "turn_id": self.turn_id,
        }


def normalize_snapshot(raw: Any, *, status: str = "captured") -> EmotionSnapshot:
    """Validate every field independently; an invalid one becomes ``None``."""
    raw = raw if isinstance(raw, Mapping) else {}
    state = raw.get("state")
    state = state if isinstance(state, Mapping) else {}
    category = raw.get("category")
    turn = raw.get("turn")
    turn_id = raw.get("turn_id")
    breaker = raw.get("breaker")
    return EmotionSnapshot(
        state={dim: _unit(state.get(dim)) for dim in DIMS},
        # Only a category the engine knows (read-only use of the matrix's keys).
        category=category if isinstance(category, str) and category in EMOTION_MATRIX else None,
        intensity=_intensity(raw.get("intensity")),
        breaker=breaker if isinstance(breaker, str) and breaker in _BREAKER_LEVELS else None,
        turn=turn if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0 else None,
        turn_id=turn_id if isinstance(turn_id, str) and _TURN_ID.match(turn_id) else None,
        status=status,
    )


def empty_snapshot(status: str) -> EmotionSnapshot:
    return EmotionSnapshot(status=status)


def emotion_snapshot_enabled(configured: Any = True) -> bool:
    """The opt-out switch (#364): ``STIRA_EPISODIC_EMOTION`` over the configured value.

    A non-empty ``STIRA_EPISODIC_EMOTION`` decides (``1``/``true``/``yes``/``on`` = on,
    any other value = off); otherwise ``configured`` (``AgentConfig.
    episodic_emotion_snapshot``, default on) must be exactly ``True``.
    """
    raw = os.environ.get("STIRA_EPISODIC_EMOTION")
    if raw is not None and raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return configured is True


def capture_emotion_snapshot(
    agent: Any, *, turn_id: str | Callable[[], str | None] | None = None
) -> EmotionSnapshot | None:
    """Copy the post-turn state of ``agent`` (an ``NXPAgent``). Read-only; never raises.

    Call it right after the turn returned: the six dimensions come from
    ``agent.engine.state`` (copied under ``engine.state_lock``), the category,
    intensity, breaker level and turn number from ``agent.last_turn_summary`` (the
    values ``post_turn`` returned). ``turn_id`` may be a callable, evaluated here so
    that its failure cannot fail the turn (the id is then ``None``). ``None`` when the agent has no
    engine state.
    """
    if callable(turn_id):
        try:
            turn_id = turn_id()
        except Exception as error:  # noqa: BLE001 - the rest of the snapshot is still valid
            logger.warning("Turn id unavailable (E_EMOTION_TURN_ID, %s)", type(error).__name__)
            turn_id = None
    try:
        engine = getattr(agent, "engine", None)
        state = getattr(engine, "state", None)
        if state is None:
            return None
        lock = getattr(engine, "state_lock", None)
        if lock is not None:
            with lock:
                values = {dim: getattr(state, dim, None) for dim in DIMS}
        else:
            values = {dim: getattr(state, dim, None) for dim in DIMS}
        summary = getattr(agent, "last_turn_summary", None)
        summary = summary if isinstance(summary, Mapping) else {}
        return normalize_snapshot(
            {
                "state": values,
                "category": summary.get("category"),
                "intensity": summary.get("intensity"),
                "breaker": summary.get("breaker"),
                "turn": summary.get("turn"),
                "turn_id": turn_id,
            }
        )
    except Exception as error:  # noqa: BLE001 - a snapshot must never fail the turn
        logger.warning("Emotion snapshot unavailable (E_EMOTION_CAPTURE, %s)", type(error).__name__)
        return None


# --------------------------------------------------------------------------
# Sidecar records (<memory_dir>/emotion/<digest>.json)
# --------------------------------------------------------------------------


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def emotion_directory(store: MemoryStore) -> Path:
    return Path(store.memory_dir) / EMOTION_DIRNAME


def emotion_record_path(store: MemoryStore, key: str) -> Path:
    """Where the record of episodic ``key`` lives (a digest, so no key reaches a path)."""
    MemoryStore._validate_key(key)
    return emotion_directory(store) / f"{_digest(key)[:32]}.json"


def write_emotion_snapshot(
    store: MemoryStore, entry: MemoryEntry, snapshot: EmotionSnapshot
) -> None:
    """Write the record for ``entry`` (atomic replace, ``0600`` in a ``0700`` directory)."""
    directory = _private_directory(store)
    path = emotion_record_path(store, entry.key)
    record = {
        "schema": RECORD_SCHEMA,
        "key": entry.key,
        "content_sha256": _digest(entry.content),
        **snapshot.as_record(),
    }
    text = json.dumps(record, ensure_ascii=True, sort_keys=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".emotion-", suffix=".tmp", delete=False
        ) as handle:  # mkstemp: 0600
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _private_directory(store: MemoryStore) -> Path:
    """``emotion/``, created ``0700``; an existing one with group/other bits is tightened."""
    directory = emotion_directory(store)
    MemoryStore._reject_symlinks(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    MemoryStore._reject_symlinks(directory)
    if os.name == "posix":
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError("emotion/ is not a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(directory, 0o700)
    return directory


def delete_emotion_snapshot(store: MemoryStore, key: str) -> bool:
    """Remove the record of episodic ``key`` (``MemoryStore.delete`` calls this).

    Returns whether a file was removed. A symbolic link at the record path is removed
    itself, never followed. Errors propagate: the caller keeps the episode then, so
    a retry deletes both.
    """
    path = emotion_record_path(store, key)
    MemoryStore._reject_symlinks(path.parent)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def _store_root(store: MemoryStore) -> Path | None:
    """The resolved root of a real store (its ancestors were resolved when it was built)."""
    return store.base_dir if isinstance(store, MemoryStore) else None


def _read_record(path: Path, root: Path | None = None) -> dict[str, Any] | None:
    MemoryStore._reject_symlinks(path.parent, root=root)
    if not _NOFOLLOW:
        # Windows has no O_NOFOLLOW: refuse a link by lstat first (best effort, racy).
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return None
        except FileNotFoundError:
            return None
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            return None
        raise
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            return None
        data = handle.read(MAX_RECORD_BYTES + 1)
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_emotion_snapshot(store: MemoryStore, entry: MemoryEntry) -> EmotionSnapshot:
    """The snapshot of an episode; unknown values are ``None``. Never raises.

    A v1 (or unversioned) episode reads as all ``None`` with ``status="v1"``, without
    opening anything. Read-only: no file is written and access times are not touched.
    """
    try:
        metadata = entry.metadata if isinstance(entry.metadata, Mapping) else {}
        if entry.layer is not MemoryLayer.EPISODIC or metadata.get("format") != FORMAT_V2:
            return empty_snapshot("v1")
        record = _read_record(emotion_record_path(store, entry.key), _store_root(store))
        if record is None or record.get("schema") != RECORD_SCHEMA:
            return empty_snapshot("missing")
        if record.get("key") != entry.key or record.get("content_sha256") != _digest(entry.content):
            return empty_snapshot("mismatch")
        return normalize_snapshot(record, status="stored")
    except Exception as error:  # noqa: BLE001 - documented as never raising
        # ValueError (key validation), MemorySymlinkError, OSError, a malformed entry...
        logger.warning("Emotion record unreadable (E_EMOTION_READ, %s)", type(error).__name__)
        return empty_snapshot("missing")


# --------------------------------------------------------------------------
# Orphaned records (operator clean-up, control/emotion_cli.py)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrphanRecord:
    """A file in ``emotion/`` that belongs to no current episode. Never the values."""

    name: str
    reason: str  # no-episode | stale | misnamed | unreadable | temporary | not-a-file
    deletable: bool = True


def find_orphan_records(store: MemoryStore, *, now: float | None = None) -> list[OrphanRecord]:
    """Classify every entry of ``emotion/`` that is not a matching record. Read-only.

    ``stale``: the episode exists but the record no longer matches it (rewritten,
    no longer v2). An episode that cannot be read is not judged (left alone). A
    temporary file is an orphan only after ``TEMPORARY_GRACE_SEC``. A directory is
    reported but never deleted.
    """
    directory = emotion_directory(store)
    MemoryStore._reject_symlinks(directory)
    if not directory.exists():
        return []
    now = time.time() if now is None else now
    orphans: list[OrphanRecord] = []
    for path in sorted(directory.iterdir()):
        info = os.lstat(path)
        name = path.name
        if stat.S_ISDIR(info.st_mode):
            orphans.append(OrphanRecord(name, "not-a-file", deletable=False))
            continue
        if name.startswith(TEMPORARY_PREFIX):
            if now - info.st_mtime >= TEMPORARY_GRACE_SEC:
                orphans.append(OrphanRecord(name, "temporary"))
            continue
        if not stat.S_ISREG(info.st_mode):
            orphans.append(OrphanRecord(name, "unreadable"))  # a link or a special file
            continue
        try:
            record = _read_record(path, _store_root(store))
        except (OSError, ValueError):
            record = None
        key = record.get("key") if isinstance(record, dict) else None
        if record is None or record.get("schema") != RECORD_SCHEMA or not isinstance(key, str):
            orphans.append(OrphanRecord(name, "unreadable"))
            continue
        try:
            expected = emotion_record_path(store, key).name
        except ValueError:
            expected = None
        if expected != name:
            orphans.append(OrphanRecord(name, "misnamed"))
            continue
        try:
            entry = store._read_entry(MemoryLayer.EPISODIC, key)  # no access-time update
        except Exception as error:  # noqa: BLE001 - an unreadable episode is not judged
            logger.warning("Episode unreadable; record kept (%s)", type(error).__name__)
            continue
        if entry is None:
            orphans.append(OrphanRecord(name, "no-episode"))
        elif read_emotion_snapshot(store, entry).status != "stored":
            orphans.append(OrphanRecord(name, "stale"))
    return orphans


def _append_log(path: Path, line: dict[str, Any]) -> None:
    MemoryStore._reject_symlinks(path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("emotion log is not a regular file")
        handle.write((json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def delete_orphan_records(store: MemoryStore, orphans: list[OrphanRecord]) -> list[str]:
    """Delete deletable orphans; each is logged to ``emotion-log.jsonl`` *before* removal.

    A log write failure stops the run (raises) with the remaining files kept, so a
    deletion is never unlogged. Returns the names removed.
    """
    directory = emotion_directory(store)
    MemoryStore._reject_symlinks(directory)
    log_path = Path(store.memory_dir) / LOG_FILENAME
    removed = []
    for orphan in orphans:
        if not orphan.deletable or "/" in orphan.name or orphan.name in ("", ".", ".."):
            continue
        _append_log(
            log_path,
            {
                "ts": time.time(),
                "action": "delete_orphan",
                "file": orphan.name,
                "reason": orphan.reason,
            },
        )
        try:
            os.unlink(directory / orphan.name)
        except FileNotFoundError:
            continue
        removed.append(orphan.name)
    return removed


# --------------------------------------------------------------------------
# Opt-in emotion-proximity recall
# --------------------------------------------------------------------------


def _vector(state: Any) -> dict[str, float]:
    values = {}
    for dim in DIMS:
        raw = state.get(dim) if isinstance(state, Mapping) else getattr(state, dim, None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            # ValueError like NeuralState's own validation (one error type for callers).
            raise ValueError(f"{dim} must be a finite number in [0, 1]")  # noqa: TRY004
        number = float(raw)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f"{dim} must be a finite number in [0, 1]")
        values[dim] = number
    return values


def emotion_distance(a: Any, b: Any) -> float:
    """Root-mean-square difference over the six dimensions, in [0, 1].

    ``d(a, b) = sqrt(sum((a_i - b_i)^2) / 6)``. ``a`` / ``b`` are a ``NeuralState`` or
    a mapping of the six dimensions. Stored values are raw; a record written before
    #364's proximity wiring holds 3 decimals, which moves ``d`` by at most
    ``DISTANCE_TOLERANCE`` (0.0005).
    """
    va, vb = _vector(a), _vector(b)
    return math.sqrt(sum((va[dim] - vb[dim]) ** 2 for dim in DIMS) / len(DIMS))


@dataclass(frozen=True)
class EmotionRecall:
    """A recalled episode and its distance. The stored values are deliberately not here."""

    entry: MemoryEntry
    distance: float


def recall_by_emotion(
    store: MemoryStore, current: Any, *, limit: int = 5, window: int = 50
) -> list[EmotionRecall]:
    """Opt-in: the ``window`` newest episodes ranked by closeness to ``current``.

    Only episodes with a complete stored snapshot take part (v1 and unmatched ones are
    left out). Order: distance, then newer first, then key. Reads ``window`` episodes
    and their records; no model call, no write, no access-time update. Nothing in
    Stira calls this; ``memory_search`` and the prompt are unchanged.
    """
    for name, value in (("limit", limit), ("window", window)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if window > MAX_RECALL_WINDOW:
        raise ValueError(f"window must be at most {MAX_RECALL_WINDOW}")
    target = _vector(current)
    ranked = []
    for entry in store.list_entries(MemoryLayer.EPISODIC, window):
        snapshot = read_emotion_snapshot(store, entry)
        if snapshot.status != "stored" or not snapshot.complete:
            continue
        distance = emotion_distance(target, snapshot.state)
        ranked.append((distance, -entry.modified_at.timestamp(), entry.key, entry))
    ranked.sort(key=lambda item: item[:3])
    return [EmotionRecall(entry, distance) for distance, _t, _k, entry in ranked[:limit]]


# --------------------------------------------------------------------------
# Mood distance and polarity (ADR 0017: the emotion-proximity recall signal)
# --------------------------------------------------------------------------

# The fast "mood" dimensions; trust/attachment stay in associative recall's bond term.
MOOD_DIMS = ("curiosity", "vigilance", "stability", "satisfaction")
PROXIMITY_RADIUS = 0.2  # s = max(0, 1 - d / R)
POLARITY_DEAD_ZONE = 0.05
NEGATIVE, NEUTRAL, POSITIVE, UNKNOWN = "negative", "neutral", "positive", "unknown"


def _mood(state: Any) -> dict[str, float] | None:
    """The four mood values of ``state`` (raw), or None if one is missing or invalid."""
    values = {}
    for dim in MOOD_DIMS:
        raw = state.get(dim) if isinstance(state, Mapping) else getattr(state, dim, None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return None
        values[dim] = number
    return values


def mood_distance(current: Any, stored: Any) -> float | None:
    """RMS difference over ``MOOD_DIMS`` (equal weights, raw scale); None if incomplete.

    ``d(c, e) = sqrt(sum_{i in M} (c_i - e_i)^2 / 4)``. Nothing is imputed: a missing
    or invalid dimension on either side gives None (no partial distance).
    """
    a, b = _mood(current), _mood(stored)
    if a is None or b is None:
        return None
    return math.sqrt(sum((a[dim] - b[dim]) ** 2 for dim in MOOD_DIMS) / len(MOOD_DIMS))


def mood_similarity(current: Any, stored: Any) -> float | None:
    """``max(0, 1 - d / PROXIMITY_RADIUS)`` in [0, 1]; None when the distance is unknown."""
    distance = mood_distance(current, stored)
    if distance is None:
        return None
    return max(0.0, 1.0 - distance / PROXIMITY_RADIUS)


def valence_index(state: Any) -> float | None:
    """``((sat - 0.5) + (stab - 0.7) - (vig - 0.1)) / 3`` from BASELINES; None if incomplete."""
    mood = _mood(state)
    if mood is None:
        return None
    return (
        (mood["satisfaction"] - BASELINES["satisfaction"])
        + (mood["stability"] - BASELINES["stability"])
        - (mood["vigilance"] - BASELINES["vigilance"])
    ) / 3


def polarity(state: Any) -> str:
    """``negative`` / ``neutral`` / ``positive`` (dead zone 0.05) or ``unknown``.

    A guard and a metric only (ADR 0017 Q2): never a bonus.
    """
    value = valence_index(state)
    if value is None:
        return UNKNOWN
    if value < -POLARITY_DEAD_ZONE:
        return NEGATIVE
    if value > POLARITY_DEAD_ZONE:
        return POSITIVE
    return NEUTRAL


__all__ = [
    "DISTANCE_TOLERANCE",
    "EMOTION_DIRNAME",
    "FORMAT_V1",
    "FORMAT_V2",
    "MOOD_DIMS",
    "PRECISION",
    "PROXIMITY_RADIUS",
    "RECORD_SCHEMA",
    "EmotionRecall",
    "EmotionSnapshot",
    "OrphanRecord",
    "capture_emotion_snapshot",
    "delete_emotion_snapshot",
    "delete_orphan_records",
    "emotion_distance",
    "emotion_record_path",
    "emotion_snapshot_enabled",
    "find_orphan_records",
    "mood_distance",
    "mood_similarity",
    "normalize_snapshot",
    "polarity",
    "read_emotion_snapshot",
    "recall_by_emotion",
    "valence_index",
    "write_emotion_snapshot",
]
