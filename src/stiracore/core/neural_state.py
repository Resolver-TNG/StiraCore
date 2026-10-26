"""Neural State — six-dimensional emotion parameters as regularization.

These parameters are not optimization targets. They preserve personality
rather than optimizing it away for efficiency.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

DIMS = ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction")

EMOTION_MATRIX: dict[str, tuple[float, ...]] = {
    "joy": (0.02, 0.03, -0.02, 0.02, 0.02, 0.05),
    "threat": (-0.01, 0.00, 0.08, 0.01, -0.02, -0.03),
    "achievement": (0.01, 0.02, -0.03, 0.01, 0.03, 0.06),
    "loss": (-0.01, -0.01, 0.03, 0.04, -0.03, -0.04),
    "uncertainty": (0.00, 0.04, 0.04, 0.00, -0.01, -0.01),
    "connection": (0.03, 0.01, -0.05, 0.06, 0.02, 0.03),
}

# Decay rates per dimension, per hour.
DECAY_RATES: dict[str, float] = {
    "trust": 0.001,
    "curiosity": 0.008,
    "vigilance": 0.015,
    "attachment": 0.002,
    "stability": 0.005,
    "satisfaction": 0.010,
}

BASELINES: dict[str, float] = {
    "trust": 0.5,
    "curiosity": 0.5,
    "vigilance": 0.1,
    "attachment": 0.5,
    "stability": 0.7,
    "satisfaction": 0.5,
}


def migrate_v1_to_v2(data: dict) -> dict:
    """Return a migrated copy of a v1 document.

    Files without schema_version are v1. The legacy ``version`` field is
    independent of the persistence schema and is preserved.
    """
    version = data.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ValueError(f"Expected schema_version 1 for migration, got {version!r}")
    migrated = dict(data)
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def _clamped_float(value: object, default: float) -> float:
    """Normalize a dimension; invalid values fall back to its baseline."""
    if isinstance(value, bool):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if math.isnan(number):
        return float(default)
    return round(max(0.0, min(1.0, number)), 4)


def _finite_float(value: object, name: str) -> float:
    """Validate numeric API input without accepting booleans or strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004 - preserve the public ValueError validation contract
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


@dataclass
class NeuralState:
    """Mutable emotional state, with policy configured by StiraConfig."""

    trust: float = 0.5
    curiosity: float = 0.5
    vigilance: float = 0.1
    attachment: float = 0.5
    stability: float = 0.7
    satisfaction: float = 0.5

    version: int = 5
    last_stimulus_time: float = field(default_factory=time.time)
    last_decay_check: float = field(default_factory=time.time)
    emotion_transcription: str = ""

    bc_window: list[float] = field(default_factory=list)
    bc_score: float = 1.0

    def __post_init__(self) -> None:
        """Require finite dimensions in [0.0, 1.0], normalized to floats."""
        for dim in DIMS:
            number = _finite_float(getattr(self, dim), dim)
            if not 0.0 <= number <= 1.0:
                raise ValueError(f"{dim} must be a finite number in [0.0, 1.0]")
            setattr(self, dim, number)

    def apply_stimulus(
        self,
        stimulus: str,
        intensity: float = 1.0,
        *,
        now_fn: Callable[[], float] | None = None,
    ) -> dict[str, float]:
        """Apply a known emotional stimulus with finite intensity."""
        # Validate before changing any dimension or timestamp.
        intensity = _finite_float(intensity, "intensity")

        if stimulus not in EMOTION_MATRIX:
            logger.warning(
                "Unknown stimulus %r — ignored. Known: %s",
                stimulus,
                list(EMOTION_MATRIX),
            )
            return {}

        deltas: dict[str, float] = {}
        for dim, delta in zip(DIMS, EMOTION_MATRIX[stimulus]):
            old = getattr(self, dim)
            new = round(max(0.0, min(1.0, old + delta * intensity)), 4)
            setattr(self, dim, new)
            if abs(new - old) > 1e-6:
                deltas[dim] = round(new - old, 4)

        self.last_stimulus_time = (now_fn or time.time)()
        return deltas

    def apply_decay(
        self,
        *,
        now_fn: Callable[[], float] | None = None,
        min_interval_sec: float = 60.0,
    ) -> dict[str, float]:
        """Apply lazy decay using an optionally injectable clock."""
        now = (now_fn or time.time)()
        if now < self.last_decay_check:
            # The clock moved backwards (NTP step, restored state from a skewed
            # host): resynchronize without decaying, so decay neither stalls
            # until the old timestamp nor counts the same span twice (#147).
            self.last_decay_check = now
            return {}
        elapsed_hours = (now - self.last_decay_check) / 3600.0
        if elapsed_hours < min_interval_sec / 3600.0:
            return {}

        deltas: dict[str, float] = {}
        for dim, rate in DECAY_RATES.items():
            old = getattr(self, dim)
            baseline = BASELINES[dim]
            decay_amount = rate * elapsed_hours
            if old > baseline:
                new = max(baseline, old - decay_amount)
            elif old < baseline:
                new = min(baseline, old + decay_amount * 0.5)
            else:
                new = old
            # No rounding here: per-tick rounding swallowed decay at short intervals (#147).
            setattr(self, dim, new)
            if abs(new - old) > 1e-6:
                deltas[dim] = round(new - old, 6)

        self.last_decay_check = now
        return deltas

    def update_bc_window(self, *, window_size: int = 20) -> float:
        """Record stability and update behavioral coherence."""
        if type(window_size) is not int or window_size < 1:
            raise ValueError("window_size must be a positive integer")

        self.bc_window.append(self.stability)
        if len(self.bc_window) > window_size:
            self.bc_window = self.bc_window[-window_size:]

        if len(self.bc_window) >= 5:
            mean = sum(self.bc_window) / len(self.bc_window)
            variance = sum((x - mean) ** 2 for x in self.bc_window) / len(self.bc_window)
            self.bc_score = round(max(0.0, 1.0 - math.sqrt(variance) * 5), 4)

        return self.bc_score

    def clamp_all(self) -> None:
        """Normalize all dimensions to finite floats within [0.0, 1.0]."""
        for dim in DIMS:
            setattr(self, dim, _clamped_float(getattr(self, dim), BASELINES[dim]))

    def to_dict(self) -> dict:
        """Return state data including its persistence schema version."""
        return asdict(self) | {"schema_version": SCHEMA_VERSION}

    def save(self, path: Path | str) -> None:
        """Flush a temporary file, then atomically replace the destination."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, allow_nan=False)

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path | str) -> NeuralState:
        """Load, migrate known schemas, and sanitize persisted numeric state."""
        path = Path(path)
        if not path.exists():
            return cls()

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("NeuralState JSON must be an object")  # noqa: TRY004 - preserve the public ValueError validation contract

        schema_version = data.get("schema_version", 1)
        if type(schema_version) is not int or schema_version not in (1, SCHEMA_VERSION):
            raise ValueError(
                f"Unsupported NeuralState schema_version {schema_version!r}; "
                f"supported versions are 1 and {SCHEMA_VERSION}"
            )
        if schema_version == 1:
            data = migrate_v1_to_v2(data)

        known = set(cls.__dataclass_fields__)
        filtered = {key: value for key, value in data.items() if key in known}

        # Persistence remains forgiving: sanitize dimensions before the strict
        # constructor validation runs.
        for dim in DIMS:
            if dim in filtered:
                filtered[dim] = _clamped_float(filtered[dim], BASELINES[dim])

        ns = cls(**filtered)
        ns.clamp_all()

        cleaned_window: list[float] = []
        if isinstance(ns.bc_window, list):
            for value in ns.bc_window:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                try:
                    number = float(value)
                except (ValueError, OverflowError):
                    continue
                if math.isfinite(number):
                    cleaned_window.append(max(0.0, min(1.0, number)))
        ns.bc_window = cleaned_window

        # A corrupt non-string transcription must not break UI/serialization.
        if not isinstance(ns.emotion_transcription, str):
            ns.emotion_transcription = ""

        # Derive coherence from the sanitized samples, never a persisted score.
        # Fewer than five samples retain the initial, unmeasured score.
        ns.bc_score = 1.0
        if len(ns.bc_window) >= 5:
            mean = sum(ns.bc_window) / len(ns.bc_window)
            variance = sum((x - mean) ** 2 for x in ns.bc_window) / len(ns.bc_window)
            ns.bc_score = round(max(0.0, 1.0 - math.sqrt(variance) * 5), 4)

        now = time.time()
        for name in ("last_stimulus_time", "last_decay_check"):
            value = getattr(ns, name)
            try:
                number = float(value) if not isinstance(value, bool) else now
            except (TypeError, ValueError, OverflowError):
                number = now
            setattr(ns, name, number if math.isfinite(number) else now)

        return ns

    def snapshot(self) -> dict[str, float]:
        """Return a detached snapshot of the dimensions and coherence score."""
        return {dim: getattr(self, dim) for dim in DIMS} | {"bc_score": self.bc_score}

    def __repr__(self) -> str:
        return (
            f"NeuralState(stability={self.stability:.2f}, trust={self.trust:.2f}, "
            f"curiosity={self.curiosity:.2f}, bc={self.bc_score:.2f})"
        )
