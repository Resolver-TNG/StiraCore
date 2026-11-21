"""R1, the screenshot registry of the page tools (ADR 0015 Addendum rev 6, S3).

``run -> [(index, sha256, size)]`` for the PNGs ``run_page_check`` wrote in this process.
Memory only: never a file, a setting, a route or a tool argument. At most
:data:`MAX_RUNS` runs, each for :data:`MAX_AGE_S` seconds on an injected monotonic clock.
It keeps a run id, numbers and hashes — no path, no pixels — and, for a run a grant's
``page_look`` call made, that grant's id (S5: :meth:`ShotRegistry.tag`, once per run).

``page_review`` (S6) compares a file's size and SHA-256 with what is kept here before it
sends it; a grant's narrowed ``view_image`` does the same and reads only the runs tagged
with its own grant (``page_look.grant_shot``).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

MAX_RUNS = 64
MAX_AGE_S = 30 * 60.0
MAX_SHOTS_PER_RUN = 4  # ``page_check.SCREENSHOT_LIMIT``
# ``YYYYmmdd-HHMMSS-`` + 6 hex: the directory name ``page_check`` makes per call.
RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
GRANT_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Shot:
    index: int
    sha256: str
    size: int


class ShotRegistry:
    """``note`` is called where the PNG is written; ``shots`` only reads."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_runs: int = MAX_RUNS,
        max_age_s: float = MAX_AGE_S,
    ) -> None:
        self._clock = clock
        self._max_runs = int(max_runs)
        self._max_age_s = float(max_age_s)
        self._lock = threading.Lock()
        # run -> (first noted at, shots); insertion order = age order.
        self._runs: OrderedDict[str, tuple[float, list[Shot]]] = OrderedDict()
        # run -> the grant whose call made it (S5); a run without an entry is no grant's.
        self._grants: dict[str, str] = {}

    def _drop(self, run: str) -> None:
        self._runs.pop(run, None)
        self._grants.pop(run, None)

    def _expire(self, now: float) -> None:
        for run in [
            r for r, (at, _s) in self._runs.items() if not 0 <= now - at <= self._max_age_s
        ]:
            self._drop(run)

    def tag(self, run: object, grant: object) -> bool:
        """Mark a kept run as made by a call of ``grant`` (32 hex). Once only: a run that
        already has a tag keeps it. ``False`` (nothing changed) for anything else."""
        if not isinstance(run, str) or not isinstance(grant, str) or not GRANT_ID.fullmatch(grant):
            return False
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            if run not in self._runs or run in self._grants:
                return False
            self._grants[run] = grant
        return True

    def grant_of(self, run: object) -> str | None:
        """The grant a kept run is tagged with (``None``: none, or an unknown run)."""
        if not isinstance(run, str):
            return None
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            return self._grants.get(run)

    def note(self, run: object, index: object, data: object) -> bool:
        """Keep one written PNG. ``False`` (nothing kept) for anything unexpected."""
        if not isinstance(run, str) or not RUN_ID.fullmatch(run):
            return False
        if type(index) is not int or not 1 <= index <= MAX_SHOTS_PER_RUN:
            return False
        if not isinstance(data, bytes):
            return False
        shot = Shot(index, hashlib.sha256(data).hexdigest(), len(data))
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            entry = self._runs.get(run)
            if entry is None:
                entry = (now, [])
                self._runs[run] = entry
            if any(kept.index == index for kept in entry[1]):
                return False  # a file is written once (``xb``); never replaced here
            entry[1].append(shot)
            while len(self._runs) > self._max_runs:
                self._drop(next(iter(self._runs)))
        return True

    def shots(self, run: object) -> tuple[Shot, ...] | None:
        """The shots of ``run`` by index, or ``None`` for an unknown or expired run."""
        if not isinstance(run, str):
            return None
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            entry = self._runs.get(run)
            if entry is None:
                return None
            return tuple(sorted(entry[1], key=lambda shot: shot.index))

    def __len__(self) -> int:
        now = float(self._clock())
        with self._lock:
            self._expire(now)
            return len(self._runs)


registry = ShotRegistry()


def run_of(shot_path: object) -> str | None:
    """The run id in a screenshot path ``.stira-verify/<run>/<n>.png`` of a result."""
    if not isinstance(shot_path, str):
        return None
    parts = shot_path.replace("\\", "/").split("/")
    if len(parts) == 3 and RUN_ID.fullmatch(parts[1]):
        return parts[1]
    return None
