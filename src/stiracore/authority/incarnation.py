"""The one process incarnation (ADR 0011 §5 "Incarnation").

128 bits from a CSPRNG, drawn once per process and again in a forked child; never
persisted and never read back from storage. The heartbeat daemon (``INCARNATION``), the
authority service and the YOLO desk all use this value, so a permit, a generation and a
host operation of one process carry the same identity.
"""

from __future__ import annotations

import os
import secrets
import threading

__all__ = ["current"]

_LOCK = threading.Lock()
_VALUE = secrets.token_hex(16)
_PID = os.getpid()


def _redraw() -> None:
    global _VALUE, _PID
    _VALUE = secrets.token_hex(16)
    _PID = os.getpid()


def current() -> str:
    """The incarnation of this process (a fork that skipped the hook still redraws)."""
    if _PID != os.getpid():
        with _LOCK:
            if _PID != os.getpid():
                _redraw()
    return _VALUE


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_redraw)
