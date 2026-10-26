"""The process-wide authority boundary (ADR 0011 §5, §13 item 2; #296).

* :mod:`.boundary` — permits, single-use ``consume``, the epoch, the lock-free kill flag,
  the unhealthy latch and the in-flight set (in memory, the leaf lock);
* :mod:`.epoch_store` — the persisted ``authority-epoch`` file (``0600``, fsync, seq +
  previous hash, atomic replace);
* :mod:`.service` — :class:`AuthorityService`, the one instance per server process
  (:func:`install` / :func:`current`), which persists the epoch and takes the epoch
  sources (rules save, breaker transitions, kill switch, YOLO);
* :mod:`.incarnation` — the one process incarnation (the heartbeat's ``INCARNATION``).

Nothing here starts work. No caller outside YOLO consumes a permit yet.
See ``docs/authority-service.md``.
"""

from .boundary import (
    GRANT_KINDS,
    MAX_IN_FLIGHT,
    AuthorityBoundary,
    AuthorityError,
    ClockPair,
    GrantRecord,
    InFlight,
    Permit,
    Receipt,
    TtlPair,
    require_kind,
)
from .service import AuthorityService, current, install, uninstall

__all__ = [
    "GRANT_KINDS",
    "MAX_IN_FLIGHT",
    "AuthorityBoundary",
    "AuthorityError",
    "AuthorityService",
    "ClockPair",
    "GrantRecord",
    "InFlight",
    "Permit",
    "Receipt",
    "TtlPair",
    "current",
    "install",
    "require_kind",
    "uninstall",
]
