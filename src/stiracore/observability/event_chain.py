"""Hash-chain primitives shared by the observability EventStore (#171, ADR 0009).

The rules are the ones ADR 0006 (approval store) and ADR 0008 (scheduler log) already
use, written once so that a store moving onto the shared EventStore inherits them:

* ``hash = sha256(prev_hash || "\\n" || canonical_json(row))``. The genesis hash is bound
  to the store's namespace, so a chain (or its head) cannot be passed off as another
  store's.
* A sidecar head (``<db>.head``) names the state *before* and *after* the transaction
  that wrote it. It is written atomically (``0600``, ``fsync`` + directory ``fsync``)
  **before** COMMIT, under the database write lock. On open: database == ``next`` is
  consistent; database == ``prev`` is an interrupted write *or* a deleted tail (the two
  cannot be told apart, so it is recorded and never treated as "nothing happened");
  anything else is tampering and the store stays unhealthy for the life of the instance.

Nothing in this module reads or writes a database; it only hashes and anchors.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stiracore._platform import fchmod_private
from stiracore.core.approval import canonical_json
from stiracore.durable_dir import fsync_dir

__all__ = [
    "CHAIN_VERSION",
    "ChainState",
    "EventChainError",
    "genesis_hash",
    "read_head",
    "row_hash",
    "validate_namespace",
    "write_head",
]

CHAIN_VERSION = 1
_NAMESPACE = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_STATE_KEYS = frozenset({"seq", "hash", "anchor_seq", "anchor_hash"})
_HEAD_KEYS = frozenset({"v", "ns", "prev", "next"})


class EventChainError(RuntimeError):
    """The chain cannot be trusted or extended; the store refuses writes."""


def validate_namespace(namespace: Any) -> str:
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("namespace must match [a-z][a-z0-9_.-]{0,31}.")
    return namespace


def genesis_hash(namespace: str) -> str:
    material = f"stira-eventstore/v{CHAIN_VERSION}\n{validate_namespace(namespace)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def row_hash(prev_hash: str, values: Sequence[Any]) -> str:
    """Hash of one row: the exact stored column values, in a fixed order.

    Raises TypeError/ValueError for values canonical JSON cannot represent exactly
    (e.g. a BLOB or NaN written into the table by something else).
    """
    material = prev_hash + "\n" + canonical_json(list(values))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_index(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


@dataclass(frozen=True)
class ChainState:
    """Tail (``seq``/``hash``) and start (``anchor_*``) of a verified chain.

    With no rows, the tail is the anchor. ``clear()`` moves the anchor to the tail, so
    the next row still links to the last deleted one.
    """

    seq: int
    hash: str
    anchor_seq: int
    anchor_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> ChainState:
        if (
            not isinstance(value, dict)
            or set(value) != _STATE_KEYS
            or not all(_is_index(value[k]) for k in ("seq", "anchor_seq"))
            or not all(_is_digest(value[k]) for k in ("hash", "anchor_hash"))
            or value["seq"] < value["anchor_seq"]
        ):
            raise ValueError("chain state is malformed")
        return cls(value["seq"], value["hash"], value["anchor_seq"], value["anchor_hash"])


def read_head(path: Path, namespace: str) -> tuple[ChainState | None, ChainState] | None:
    """Return ``(prev, next)`` from the head file, ``None`` if it does not exist.

    A head that is not valid JSON, has other keys or types, or belongs to another
    namespace raises :class:`EventChainError` (tampering, never "no head").
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise EventChainError(f"head anchor unreadable ({type(error).__name__})") from None
    try:
        head = json.loads(raw)
        if not isinstance(head, dict) or set(head) != _HEAD_KEYS:
            raise ValueError
        if type(head["v"]) is not int or head["v"] != CHAIN_VERSION:
            raise ValueError
        prev = None if head["prev"] is None else ChainState.from_dict(head["prev"])
        nxt = ChainState.from_dict(head["next"])
    except ValueError:
        raise EventChainError("head anchor is malformed") from None
    if head["ns"] != namespace:
        raise EventChainError("head anchor belongs to another namespace")
    return prev, nxt


def write_head(path: Path, namespace: str, prev: ChainState | None, nxt: ChainState) -> None:
    """Atomically replace the head (``0600``), then make the rename durable."""
    text = json.dumps(
        {
            "v": CHAIN_VERSION,
            "ns": namespace,
            "prev": None if prev is None else prev.to_dict(),
            "next": nxt.to_dict(),
        },
        sort_keys=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".eventstore-head-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fchmod_private(handle.fileno(), 0o600)  # no-op on Windows (#540)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    fsync_dir(path.parent)  # POSIX only; Windows cannot open a directory (#543)
