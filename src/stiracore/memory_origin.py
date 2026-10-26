"""Who wrote a memory's text (``origin``), kept apart from how it came in (``source``) (#294).

Two front-matter fields, set by the writers of new entries:

* ``origin`` — who wrote the sentence: ``person`` (typed by the person),
  ``extracted`` (taken from the conversation without rewriting: the person's words
  and the assistant's reply, as the Episodic summary of a turn does), ``model``
  (written by a model: the agent's ``memory_write``, a model-written summary),
  ``assistant`` (attributed to the assistant's output).
* ``source`` — the channel or writer (``web-chat``, ``memory_write``...).

Entries written before #294 have neither and count as ``unknown``; nothing rewrites
them.

**The Episodic front matter is a claim, not proof.** The episodic layer is a write
root of the agent's file tools, so the agent can rewrite an episode's front matter
(``origin: person``) while keeping its body. The value that counts is therefore
recorded by the server's writers (``record_episode``, ``memory_write``) in the search
index (``index.sqlite3``: on the data-directory floor, the file tools can neither read
nor write it), bound to the SHA-256 of the file's whole text: :func:`verified_origin`
returns it only while the file is byte-for-byte what was written; any change (also a
front-matter-only edit) makes it ``unknown``. Harvest copies only a verified
``origin``/``source`` to the Semantic entry (``semantic/`` is on the floor, so there the
front matter is what Harvest wrote). The fields label; they grant nothing.
"""

from __future__ import annotations

import hashlib
from typing import Any

ORIGIN_PERSON = "person"
ORIGIN_EXTRACTED = "extracted"
ORIGIN_MODEL = "model"
ORIGIN_ASSISTANT = "assistant"
ORIGIN_UNKNOWN = "unknown"
# A front-matter claim no server writer recorded (reports only; never stored).
ORIGIN_UNVERIFIED = "unverified"
ORIGINS = frozenset({ORIGIN_PERSON, ORIGIN_EXTRACTED, ORIGIN_MODEL, ORIGIN_ASSISTANT})


def origin_of(metadata: dict) -> str:
    """The entry's origin, ``unknown`` when missing or not one of :data:`ORIGINS`."""
    value = metadata.get("origin") if isinstance(metadata, dict) else None
    # A front-matter value can be any YAML shape (a list or a mapping is unhashable).
    return value if isinstance(value, str) and value in ORIGINS else ORIGIN_UNKNOWN


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_origin(store: Any, layer: Any, key: str, origin: str, source: str) -> None:
    """Bind ``origin``/``source`` to the entry's current text (server-side writers only)."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    text = store.read_text(layer, key)
    if text is None:
        return
    with store._connection() as connection:
        connection.execute(
            "UPDATE memory_entries SET origin = ?, origin_source = ?, origin_sha256 = ?"
            " WHERE layer = ? AND key = ?",
            (origin, source, _sha(text), layer.value, key),
        )


def verified_origin(store: Any, layer: Any, key: str, text: str | None = None) -> dict | None:
    """``{"origin", "source"}`` recorded by a server writer for this exact text, or ``None``."""
    if text is None:
        text = store.read_text(layer, key)
    if text is None:
        return None
    with store._connection() as connection:
        row = connection.execute(
            "SELECT origin, origin_source, origin_sha256 FROM memory_entries"
            " WHERE layer = ? AND key = ?",
            (layer.value, key),
        ).fetchone()
    if row is None or row[0] not in ORIGINS or row[2] != _sha(text):
        return None
    return {"origin": row[0], "source": row[1]}


__all__ = [
    "ORIGINS",
    "ORIGIN_ASSISTANT",
    "ORIGIN_EXTRACTED",
    "ORIGIN_MODEL",
    "ORIGIN_PERSON",
    "ORIGIN_UNKNOWN",
    "ORIGIN_UNVERIFIED",
    "origin_of",
    "record_origin",
    "verified_origin",
]
