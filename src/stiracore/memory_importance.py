"""Importance tags for forgetting: ``retention: invariant | derived`` and a half-life (#173).

Two front-matter fields of a memory entry:

* ``retention`` — ``invariant`` (never forgotten by maintenance: not compressed by
  ``--apply-forgetting``, never a duplicate/stale deletion candidate, held by a retention
  run) or ``derived`` (can be regenerated; fades with a half-life). **Missing means
  ``derived`` with the reference half-life**, which selects exactly what Stira selected
  before #173.
* ``retention_half_life_days`` — a ``derived`` entry's half-life in days (an integer from
  :data:`HALF_LIFE_MIN_DAYS` to :data:`HALF_LIFE_MAX_DAYS`); default
  :data:`REFERENCE_HALF_LIFE_DAYS`.

From days to an importance score: an entry not read for ``age`` days keeps the strength
``2 ** (-age / half_life)``. The forgetting stages are fixed points on that curve, defined
by the reference half-life (30 days) so that untagged entries keep the old day counts:

====================  ===========================  =======================  ============
stage                 strength at or below          half-lives               untagged
====================  ===========================  =======================  ============
``light`` (4000 ch)   1/2                           1                        30 days
``compact`` (2000)    1/8                           3                        90 days
``archive`` (800)     1/64                          6                        180 days
stale (deletion       2 ** (-365/30) ≈ 0.00022      365/30 ≈ 12.2            365 days
candidate)
====================  ===========================  =======================  ============

The comparison is done in integers (``age * 30 >= threshold * half_life``), so an
untagged entry crosses a stage on exactly the day it did before.

**Who sets a tag.** Only a human, with ``stira memory tag ... --apply`` (owner lock, audited)
— the agent can at most propose it in words. The Semantic layer is on the floor of the
agent's file tools and Harvest copies an allow-list of fields (never ``retention``), so a
Semantic tag is what a human (or a server writer) wrote. The Episodic layer is a write root
of the agent's file tools: there the front matter is a claim, and a tag counts only when it
is *recorded* in the search index (``index.sqlite3``, on the floor) by that command. The
record keeps two things apart: **that a human tagged the entry** (the kind — in force until
a human changes it with the same command) and **whether the text is still what was tagged**
(the SHA-256 of the file's whole text). An edit by anyone else — also removing the
front-matter field — does not lift the tag: the entry stays held and is reported as
``unverified_held`` for a human to look at (:func:`binding_state` = ``changed``). The agent
can neither add a tag (no record) nor remove one (the record is on the floor).

The tags label what maintenance may do; they grant nothing else. A delete request
(``stira memory forget``) removes an ``invariant`` entry like any other: that is the human
delete command the tag does not stop.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

RETENTION_FIELD = "retention"
HALF_LIFE_FIELD = "retention_half_life_days"
INVARIANT = "invariant"
DERIVED = "derived"
RETENTIONS = frozenset({INVARIANT, DERIVED})
REFERENCE_HALF_LIFE_DAYS = 30
HALF_LIFE_MIN_DAYS = 1
HALF_LIFE_MAX_DAYS = 3650
# A claim in Episodic front matter that no human bound (reports only; never stored).
RETENTION_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Retention:
    """The effective tag of one entry."""

    kind: str = DERIVED
    half_life_days: int = REFERENCE_HALF_LIFE_DAYS
    # The front matter set a tag (valid or not); False = untagged (the pre-#173 default).
    tagged: bool = False
    # The front matter held a value outside the contract; it was ignored (default used).
    invalid: bool = False

    @property
    def invariant(self) -> bool:
        return self.kind == INVARIANT

    def reaches(self, age_days: int, threshold_days: int) -> bool:
        """``age_days`` is at least ``threshold_days / 30`` half-lives (integer compare)."""
        return age_days * REFERENCE_HALF_LIFE_DAYS >= threshold_days * self.half_life_days

    def strength(self, age_days: int) -> float:
        """``2 ** (-age / half_life)``: 1.0 when just read, 0.5 after one half-life."""
        return 0.5 ** (max(0, age_days) / self.half_life_days)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "tagged": self.tagged}
        if self.kind == DERIVED:
            data["half_life_days"] = self.half_life_days
        if self.invalid:
            data["invalid"] = True
        return data


def _half_life(value: Any) -> int | None:
    if type(value) is not int or not HALF_LIFE_MIN_DAYS <= value <= HALF_LIFE_MAX_DAYS:
        return None
    return value


def retention_of(metadata: Any) -> Retention:
    """The tag in a front matter; anything outside the contract falls back to the default.

    Falling back to ``derived`` (with the reference half-life) is the safe side: an
    unreadable tag never protects an entry, and never makes it fade faster than before.
    """
    if not isinstance(metadata, dict):
        return Retention()
    kind = metadata.get(RETENTION_FIELD)
    raw_half_life = metadata.get(HALF_LIFE_FIELD)
    if kind is None and raw_half_life is None:
        return Retention()
    if kind is None:
        kind = DERIVED
    if not isinstance(kind, str) or kind not in RETENTIONS:
        return Retention(tagged=True, invalid=True)
    if kind == INVARIANT:
        # A half-life next to invariant is meaningless; it is ignored, the tag is not.
        return Retention(INVARIANT, tagged=True, invalid=raw_half_life is not None)
    if raw_half_life is None:
        return Retention(DERIVED, tagged=True)
    half_life = _half_life(raw_half_life)
    if half_life is None:
        return Retention(DERIVED, tagged=True, invalid=True)
    return Retention(DERIVED, half_life, tagged=True)


def tag_metadata(metadata: dict[str, Any], kind: str | None, half_life_days: int | None):
    """``metadata`` with the tag replaced (``kind=None``: both fields removed)."""
    clean = {
        name: value
        for name, value in metadata.items()
        if name not in (RETENTION_FIELD, HALF_LIFE_FIELD)
    }
    if kind is None:
        if half_life_days is not None:
            raise ValueError("a half-life needs --retention derived")
        return clean
    if kind not in RETENTIONS:
        raise ValueError(f"retention must be one of {sorted(RETENTIONS)} or none")
    clean[RETENTION_FIELD] = kind
    if half_life_days is not None:
        if kind != DERIVED:
            raise ValueError("a half-life applies to derived entries only")
        if _half_life(half_life_days) is None:
            raise ValueError(
                f"half-life must be an integer from {HALF_LIFE_MIN_DAYS} to {HALF_LIFE_MAX_DAYS}"
            )
        clean[HALF_LIFE_FIELD] = half_life_days
    return clean


# -- Episodic bindings (the front matter there is agent-writable) ---------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_COLUMNS = ("retention", "retention_sha256")


def bind_retention(store: Any, layer: Any, key: str, kind: str | None, text: str | None) -> None:
    """Record a human's tag for ``key``, bound to ``text`` (``kind=None`` clears it).

    ``text`` is the exact Markdown the caller wrote (and checked) — never a re-read of
    whatever the file holds now, which another writer could have changed in between.
    The two index columns are added here, on the first binding, not when a store is
    opened: a read-only preview (``distill --dry-run``) must not change the index.
    """
    if kind is not None and kind not in RETENTIONS:
        raise ValueError(f"unknown retention {kind!r}")
    if kind is not None and not isinstance(text, str):
        raise ValueError("a binding needs the text it is bound to")
    with store._connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_entries)")}
        for column in _COLUMNS:
            if column not in columns:
                connection.execute(f"ALTER TABLE memory_entries ADD COLUMN {column} TEXT")
        connection.execute(
            "UPDATE memory_entries SET retention = ?, retention_sha256 = ?"
            " WHERE layer = ? AND key = ?",
            (kind, None if kind is None else _sha(text), layer.value, key),
        )


def retention_binding(store: Any, layer: Any, key: str) -> tuple[str, str | None] | None:
    """``(kind, sha256)`` a human recorded for ``key``, or ``None`` (never tagged)."""
    try:
        with store._connection() as connection:
            row = connection.execute(
                "SELECT retention, retention_sha256 FROM memory_entries"
                " WHERE layer = ? AND key = ?",
                (layer.value, key),
            ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" not in str(error):
            raise
        return None  # never bound: the columns do not exist yet
    if row is None or not isinstance(row[0], str) or row[0] not in RETENTIONS:
        return None
    return row[0], row[1]


# -- reinforcement by recall (a hook; not wired yet) ---------------------------------------


def touch_recalled(store: Any, key: str, *, at: datetime | None = None) -> bool:
    """Restart the half-life clock of the Semantic entry ``key`` because it was recalled.

    The clock of a ``derived`` entry is its index ``last_accessed`` (the strength is
    ``2 ** (-age / half_life)`` counted from it), the same time ``memory_read`` and
    ``memory_search`` refresh. This sets it to ``at`` (default: now) and nothing else:

    * **Never backwards.** The clock only moves forward (``max(old, at)``): a stale or
      replayed ``at`` cannot make an entry fade faster.
    * **Never the tag.** ``retention`` / ``retention_half_life_days`` and the file are not
      touched (same bytes, same modification time): reinforcement shortens nothing and
      grants nothing; only ``stira memory tag`` changes a tag.
    * **Only an indexed Semantic entry.** No row is created; ``False`` when there is
      none (unknown key, entry not yet indexed). Half-lives apply to Semantic only.

    It is a host-side function, not an agent tool. Associative recall (#170) may call it
    later for the Semantic entries it actually used; nothing calls it yet.
    """
    from stiracore.tools.memory_store import MemoryLayer

    store._validate_key(key)
    moment = datetime.now(UTC) if at is None else at
    if moment.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    with store._connection() as connection:
        cursor = connection.execute(
            "UPDATE memory_entries SET last_accessed = MAX(COALESCE(last_accessed, 0), ?)"
            " WHERE layer = ? AND key = ?",
            (moment.timestamp(), MemoryLayer.SEMANTIC.value, key),
        )
        return cursor.rowcount > 0


BINDING_VERIFIED = "verified"
# A human's tag whose entry changed since: still in force, the text needs a human look.
BINDING_CHANGED = "changed"


def binding_state(bound: tuple[str, str | None] | None, text: str) -> str | None:
    """``verified`` (the text is what was tagged), ``changed``, or ``None`` (no tag)."""
    if not bound or not isinstance(bound[0], str) or bound[0] not in RETENTIONS:
        return None
    return BINDING_VERIFIED if bound[1] == _sha(text) else BINDING_CHANGED


__all__ = [
    "BINDING_CHANGED",
    "BINDING_VERIFIED",
    "DERIVED",
    "HALF_LIFE_FIELD",
    "HALF_LIFE_MAX_DAYS",
    "HALF_LIFE_MIN_DAYS",
    "INVARIANT",
    "REFERENCE_HALF_LIFE_DAYS",
    "RETENTIONS",
    "RETENTION_FIELD",
    "RETENTION_UNVERIFIED",
    "Retention",
    "bind_retention",
    "binding_state",
    "retention_binding",
    "retention_of",
    "tag_metadata",
    "touch_recalled",
]
