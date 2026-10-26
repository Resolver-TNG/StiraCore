"""The shared authority boundary, the minimum YOLO needs (ADR 0012 §13, ADR 0011 §5, #276).

Moved to :mod:`stiracore.authority.boundary` (W1, #296): the boundary is now one
process-wide service (:class:`stiracore.authority.AuthorityService`) that YOLO receives
by injection. This module re-exports the same names for older importers; nothing about
them changed:

* **``kind`` is a mandatory discriminator** of every grant record (``goal`` | ``yolo``);
  functions for one kind refuse the other (:func:`require_kind`).
* **Incarnation**: 128 bits from a CSPRNG, never read back from storage.
* **Authority lock**: a leaf lock, held only for the compare-and-set, never across I/O.
* **Authority epoch**: monotonic; it only moves up by one per narrowing.
* **Single-use ``consume``**: a permit is bound to ``(incarnation, kind, grant_id,
  generation, epoch)`` and to an ADR 0013 receipt reference.

YOLO windows (phases 5a–7) issue and consume these permits; no goal grant exists yet.
"""

from __future__ import annotations

from stiracore.authority.boundary import (
    GRANT_KINDS,
    AuthorityBoundary,
    AuthorityError,
    GrantRecord,
    Permit,
    Receipt,
    require_kind,
)

__all__ = [
    "GRANT_KINDS",
    "AuthorityBoundary",
    "AuthorityError",
    "GrantRecord",
    "Permit",
    "Receipt",
    "require_kind",
]
