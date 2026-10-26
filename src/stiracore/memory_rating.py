"""Importance ratings bound to a memory's exact text (ADR 0018 §1, phase 0 — observe only).

Two things live here:

* **The harness-owned rule score.** Every turn's episode carries ``importance`` (the
  AmygdalaPass score of the original turn, #293) in its front matter. The episodic layer is a
  write root of the agent's file tools, so that front matter is a *claim* (#506). The value
  counts only while the server's episode writer (``record_episode``) is still the author of the
  file's exact text: :func:`harness_rule_importance` returns it only when the verified origin
  (``memory_origin.verified_origin``: the SHA-256 of the whole file, recorded in
  ``index.sqlite3`` on the data-directory floor) says ``extracted``. Otherwise there is none:
  Harvest re-scores the stored text (the default rule score) and recall uses the neutral 0.5.
  :func:`scoring_metadata` is what Harvest hands its distiller.
* **The agent's rating** (``memory_rate``). A row of the ``memory_ratings`` table of
  ``index.sqlite3`` (created on the first binding, never when a store is opened), bound to the
  SHA-256 of the entry's whole text like ``origin``: a row whose digest no longer matches is
  treated as absent (:func:`rating_row`). The model supplies at most ``importance``,
  ``reason`` and ``half_life_days`` (:func:`clean_rating`: refused when not a finite number,
  clamped into the bounds with ``capped = clamped``); who, when, which turn and the turn's
  trust are stamped by the harness.

**Phase 0: nothing reads a rating to decide anything.** Harvest routing, recall, forgetting and
retention do not import this module's rating readers; ``tests/test_memory_autonomy_phase0.py``
checks it. :func:`effect_of` is the phase-1/2 formula (trust cap, derived half-life) kept next
to the data it reads; nothing calls it outside the tests.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from stiracore.memory_origin import ORIGIN_EXTRACTED, verified_origin

__all__ = [
    "CAPPED_CODES",
    "HALF_LIFE_MAX_DAYS",
    "HALF_LIFE_MIN_DAYS",
    "HIGH_DAILY_QUOTA",
    "HIGH_RATING",
    "QUOTA_VALUE",
    "RATED_BY",
    "REASONS",
    "SCHEMA",
    "SCORE_KEYS",
    "TRUSTS",
    "UNTRUSTED_CAP",
    "RatingRefused",
    "bind_rating",
    "clean_rating",
    "derived_half_life",
    "effect_of",
    "harness_rule_importance",
    "high_ratings_on",
    "rating_counts",
    "rating_row",
    "rebind_rating",
    "scoring_metadata",
]

SCHEMA = "importance/v1"
TABLE = "memory_ratings"
# A code, not free text: the field cannot carry instructions (ADR 0018 §1a).
REASONS = ("decision", "error_fix", "preference", "fact", "procedure_hint", "routine", "other")
RATED_BY = ("agent_turn", "agent_job", "human")
TRUSTS = ("trusted", "untrusted", "unknown")
CAPPED_CODES = ("clamped", "quota", "untrusted")
HALF_LIFE_MIN_DAYS = 7
HALF_LIFE_MAX_DAYS = 180
HIGH_RATING = 0.8
HIGH_DAILY_QUOTA = 10
QUOTA_VALUE = 0.79
UNTRUSTED_CAP = 0.5
# agent_importance -> derived half-life (days), ADR 0018 §1d.
_DERIVED = ((0.2, 7), (0.4, 14), (0.6, 30), (0.8, 60))
_DERIVED_TOP = 120
# Front-matter fields written by the original-turn scorer (#293).
SCORE_KEYS = ("importance", "importance_basis", "importance_version")


class RatingRefused(ValueError):
    """A rating was refused; ``code`` is a fixed ``E_*`` code (never a message)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- the harness-owned rule score (#506) ----------------------------------------------------


def harness_rule_importance(store: Any, layer: Any, key: str, text: str | None = None):
    """The original-turn score the server's episode writer recorded for this exact text.

    ``None`` when the entry's whole text is not what ``record_episode`` wrote (edited by a
    file tool, written by ``memory_write`` or before #294) or the value is not a finite float
    in [0, 1]. ``text`` is the raw file text the caller already read (default: re-read).
    """
    from stiracore.tools.memory_store import MemoryStore

    if text is None:
        text = store.read_text(layer, key)
    if text is None:
        return None
    verified = verified_origin(store, layer, key, text)
    if verified is None or verified.get("origin") != ORIGIN_EXTRACTED:
        return None
    try:
        _content, metadata = MemoryStore._parse(text)
    except ValueError:
        return None
    value = metadata.get("importance")
    if isinstance(value, float) and math.isfinite(value) and 0.0 <= value <= 1.0:
        return value
    return None


def scoring_metadata(store: Any, entry: Any) -> dict[str, Any]:
    """The front matter Harvest hands its distiller, without forged score fields.

    ``importance`` / ``importance_basis`` / ``importance_version`` stay only when the file is
    still exactly what the server's episode writer wrote **and** what ``entry`` was parsed
    from; otherwise they are dropped, so the distiller re-scores the stored text (its default
    rule score). ``importance_error`` is kept either way: it can only send a turn to review.
    """
    metadata = dict(entry.metadata)
    if not any(name in metadata for name in SCORE_KEYS):
        return metadata
    layer = entry.layer
    text = store.read_text(layer, entry.key)
    keep = False
    if text is not None and harness_rule_importance(store, layer, entry.key, text) is not None:
        try:
            keep = store._parse(text) == (entry.content, entry.metadata)
        except ValueError:
            keep = False
    if keep:
        return metadata
    return {name: value for name, value in metadata.items() if name not in SCORE_KEYS}


# -- the agent's rating ---------------------------------------------------------------------


def derived_half_life(value: float) -> int:
    """The half-life (days) a rating implies when none was given (ADR 0018 §1d)."""
    for below, days in _DERIVED:
        if value < below:
            return days
    return _DERIVED_TOP


def clean_rating(importance: Any, reason: Any, half_life_days: Any = None) -> dict[str, Any]:
    """Validate and bound the model's three values. Raises :class:`RatingRefused`.

    Returns ``{"importance", "reason", "half_life_days", "capped": [codes]}``. Out-of-range
    numbers are clamped (recorded as ``clamped``), never silently changed.
    """
    if isinstance(reason, str) and reason.strip().lower() == "invariant":
        # invariant is a person's decision (ADR 0018 Q2); the agent may only propose it.
        raise RatingRefused("E_INVARIANT_NOT_ALLOWED")
    if not isinstance(reason, str) or reason not in REASONS:
        raise RatingRefused("E_AGENT_REASON_INVALID")
    if isinstance(importance, bool) or not isinstance(importance, (int, float)):
        raise RatingRefused("E_AGENT_IMPORTANCE_INVALID")
    value = float(importance)
    if not math.isfinite(value):
        raise RatingRefused("E_AGENT_IMPORTANCE_INVALID")
    capped = []
    if not 0.0 <= value <= 1.0:
        value = min(1.0, max(0.0, value))
        capped.append("clamped")
    value = round(value, 2)
    if half_life_days is None:
        half_life = derived_half_life(value)
    else:
        if isinstance(half_life_days, bool) or not isinstance(half_life_days, (int, float)):
            raise RatingRefused("E_HALF_LIFE_INVALID")
        if not math.isfinite(float(half_life_days)) or float(half_life_days) != int(half_life_days):
            raise RatingRefused("E_HALF_LIFE_INVALID")
        half_life = int(half_life_days)
        if not HALF_LIFE_MIN_DAYS <= half_life <= HALF_LIFE_MAX_DAYS:
            half_life = min(HALF_LIFE_MAX_DAYS, max(HALF_LIFE_MIN_DAYS, half_life))
            if "clamped" not in capped:
                capped.append("clamped")
    return {"importance": value, "reason": reason, "half_life_days": half_life, "capped": capped}


def _ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            layer TEXT NOT NULL,
            key TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            rule_importance REAL,
            agent_importance REAL NOT NULL,
            agent_reason TEXT NOT NULL,
            agent_half_life_days INTEGER NOT NULL,
            rated_by TEXT NOT NULL,
            rated_at TEXT NOT NULL,
            rated_day TEXT NOT NULL,
            turn_id TEXT,
            model_ref TEXT,
            trust TEXT NOT NULL,
            capped TEXT,
            schema TEXT NOT NULL,
            PRIMARY KEY (layer, key)
        )
        """
    )


def _table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def bind_rating(
    store: Any,
    layer: Any,
    key: str,
    text: str,
    *,
    rating: dict[str, Any],
    rated_by: str,
    trust: str,
    turn_id: str | None = None,
    model_ref: str | None = None,
    rule_importance: float | None = None,
    extra_capped: tuple[str, ...] = (),
    at: datetime | None = None,
) -> dict[str, Any]:
    """Bind a cleaned rating (:func:`clean_rating`) to ``text`` — the exact Markdown the
    server just wrote (never a re-read another writer could have changed in between).

    Host-side only: the stamps (``rated_by``, ``trust``, ``turn_id``, ``model_ref``,
    ``rule_importance``) come from the server. Returns the stored row."""
    if rated_by not in RATED_BY:
        raise ValueError("unknown rated_by")
    if trust not in TRUSTS:
        raise ValueError("unknown trust")
    if not isinstance(text, str):
        raise ValueError("a binding needs the text it is bound to")  # noqa: TRY004
    moment = (at or datetime.now(UTC)).astimezone(UTC)
    capped = [code for code in CAPPED_CODES if code in set(rating["capped"]) | set(extra_capped)]
    row = {
        "layer": layer.value,
        "key": key,
        "text_sha256": _sha(text),
        "rule_importance": rule_importance,
        "agent_importance": float(rating["importance"]),
        "agent_reason": rating["reason"],
        "agent_half_life_days": int(rating["half_life_days"]),
        "rated_by": rated_by,
        "rated_at": moment.isoformat(timespec="seconds"),
        "rated_day": moment.strftime("%Y-%m-%d"),
        "turn_id": turn_id,
        "model_ref": model_ref,
        "trust": trust,
        "capped": ",".join(capped) or None,
        "schema": SCHEMA,
    }
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with store._connection() as connection:
        _ensure_table(connection)
        connection.execute(
            f"INSERT OR REPLACE INTO {TABLE} ({columns}) VALUES ({marks})", tuple(row.values())
        )
    return row


def _row(store: Any, layer: Any, key: str) -> dict[str, Any] | None:
    try:
        with store._connection() as connection:
            if not _table_exists(connection):
                return None
            connection.row_factory = sqlite3.Row
            found = connection.execute(
                f"SELECT * FROM {TABLE} WHERE layer = ? AND key = ?", (layer.value, key)
            ).fetchone()
    except sqlite3.Error:
        return None
    return dict(found) if found is not None else None


def rating_row(store: Any, layer: Any, key: str, text: str | None = None) -> dict | None:
    """The bound rating of ``key`` while its text is what was rated, else ``None``."""
    if text is None:
        text = store.read_text(layer, key)
    if text is None:
        return None
    row = _row(store, layer, key)
    if row is None or row.get("text_sha256") != _sha(text) or row.get("schema") != SCHEMA:
        return None
    return row


def rebind_rating(store: Any, layer: Any, key: str, before: str, after: str) -> bool:
    """A server writer rewrote ``key`` itself (Harvest's mark): a row that verified against
    ``before`` moves to ``after``; any other row keeps its old digest (= absent)."""
    row = _row(store, layer, key)
    if row is None or row.get("text_sha256") != _sha(before):
        return False
    with store._connection() as connection:
        connection.execute(
            f"UPDATE {TABLE} SET text_sha256 = ? WHERE layer = ? AND key = ? AND text_sha256 = ?",
            (_sha(after), layer.value, key, _sha(before)),
        )
    return True


def high_ratings_on(store: Any, day: str, rated_by: str = "agent_turn") -> int:
    """Rows rated ``>= HIGH_RATING`` by ``rated_by`` on the UTC ``day`` (any binding state)."""
    try:
        with store._connection() as connection:
            if not _table_exists(connection):
                return 0
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {TABLE} WHERE rated_day = ? AND rated_by = ?"
                    " AND agent_importance >= ?",
                    (day, rated_by, HIGH_RATING),
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return HIGH_DAILY_QUOTA  # unreadable: count as full (fail closed for the quota)


def rating_counts(store: Any, day: str) -> dict[str, int]:
    """Counts for the status (no keys, no values)."""
    counts = {"total": 0, "today": 0, "high_today": 0, "untrusted_today": 0}
    try:
        with store._connection() as connection:
            if not _table_exists(connection):
                return counts
            counts["total"] = int(connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
            counts["today"], counts["high_today"], counts["untrusted_today"] = (
                int(value or 0)
                for value in connection.execute(
                    f"SELECT COUNT(*), SUM(agent_importance >= ?), SUM(trust != 'trusted')"
                    f" FROM {TABLE} WHERE rated_day = ?",
                    (HIGH_RATING, day),
                ).fetchone()
            )
    except sqlite3.Error:
        return counts
    return counts


def effect_of(row: dict[str, Any]) -> dict[str, Any]:
    """**Phase 1/2 only — nothing calls it in phase 0.** What a bound row would mean:

    ``routing`` = ``max(rule, agent)`` (not capped: it only sends more to review);
    ``recall`` and the half-life use ``min(agent, 0.5)`` unless the turn was ``trusted``
    (ADR 0018 §1d B, §1e; commander decisions Q1, Q8)."""
    agent = float(row["agent_importance"])
    rule = row.get("rule_importance")
    trusted = row.get("trust") == "trusted"
    capped = agent if trusted else min(agent, UNTRUSTED_CAP)
    half_life = int(row["agent_half_life_days"])
    if not trusted:
        half_life = min(half_life, derived_half_life(capped))
    return {
        "routing": max(agent, float(rule)) if isinstance(rule, (int, float)) else agent,
        "recall": capped,
        "half_life_days": half_life,
    }
