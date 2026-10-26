"""Atomic JSON persistence for a mutable NeuralState.

Supported state representations are dataclasses, Pydantic models, and objects
providing ``to_dict``/``from_dict``. Loading preserves the supplied object's
identity so engines already referencing it see restored values.

The per-instance lock serializes persistence operations. Applications must
also synchronize external state mutations if they require an atomic snapshot
of a state being changed concurrently.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

from stiracore.core import NeuralState

# Windows raises PermissionError ([WinError 5] Access is denied) from os.replace
# when the destination has a concurrent open handle (e.g. another thread's
# read_text()). POSIX rename is atomic regardless, so retries there are a
# harmless no-op path that never triggers. Bounded retries with a short
# backoff let the other handle close without masking a persistent failure.
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.01


def _replace_with_retry(source: Path, destination: Path) -> None:
    """os.replace with bounded retry/backoff for transient Windows sharing violations."""
    for attempt in range(1, _REPLACE_MAX_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_MAX_ATTEMPTS:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * attempt)


def _state_dict(state: Any) -> dict[str, Any]:
    if callable(getattr(state, "model_dump", None)):
        data = state.model_dump(mode="json")
    elif callable(getattr(state, "to_dict", None)):
        data = state.to_dict()
    elif is_dataclass(state) and not isinstance(state, type):
        data = asdict(state)
    else:
        raise TypeError(
            "NeuralState must be a dataclass, a Pydantic model, or implement to_dict/from_dict"
        )
    if not isinstance(data, Mapping):
        raise TypeError("Serialized NeuralState must be a mapping")
    return dict(data)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot persist value of type {type(value).__name__}")


def _restore(state: Any, data: dict[str, Any]) -> None:
    """Construct first, then copy restored fields into the existing object."""
    state_type = type(state)

    if callable(getattr(state_type, "model_validate", None)):
        restored = state_type.model_validate(data)
        names = list(state_type.model_fields)
    elif callable(getattr(state_type, "from_dict", None)):
        restored = state_type.from_dict(data)
        names = list(_state_dict(restored))
    elif is_dataclass(state) and not isinstance(state, type):
        state_fields = fields(state)
        names = [field.name for field in state_fields]
        unknown = set(data).difference(names)
        # Allow metadata fields (schema_version, etc.) that aren't dataclass fields
        _METADATA_FIELDS = {"schema_version", "version", "timestamp", "agent_id"}
        unexpected = unknown - _METADATA_FIELDS
        if unexpected:
            raise ValueError(f"Unknown NeuralState fields: {sorted(unexpected)}")
        restored = state_type(
            **{
                field.name: data[field.name]
                for field in state_fields
                if field.init and field.name in data
            }
        )
        for field in state_fields:
            if not field.init and field.name in data:
                setattr(restored, field.name, data[field.name])
    else:
        raise TypeError("NeuralState does not support restoration")

    if not isinstance(restored, state_type):
        raise TypeError("Restoration must produce the original NeuralState type")

    values = {name: getattr(restored, name) for name in names}
    for name, value in values.items():
        setattr(state, name, value)


class StatePersistence:
    """Save and restore NeuralState without replacing its shared reference."""

    def __init__(self, state: NeuralState, path: Path, *, state_lock: Any = None) -> None:
        """``state_lock`` (e.g. ``StiraEngine.state_lock``) makes saved snapshots, restores
        (``load``) and the SDK copy (``inject_agent_state``) consistent with concurrent
        turns/heartbeats instead of mixing dimensions (#601).

        Lock order: ``self._lock`` -> ``state_lock``, each ``state_lock`` hold covers one
        copy only and is released before the next (``save_session`` takes it twice in
        sequence, never nested), so a non-reentrant lock works. Callers must not call
        ``load`` / ``save`` / ``inject_agent_state`` while holding ``state_lock`` (the
        engine never does: it only locks around its own state updates)."""
        self.state = state
        self.path = Path(path)
        self._lock = RLock()
        self.state_lock = state_lock

    def load(self) -> NeuralState:
        """Load JSON, or retain the supplied in-memory defaults if missing.

        Corrupt files and invalid state data raise rather than silently
        replacing potentially recoverable data with defaults.
        """
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return self.state

            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("NeuralState file must contain a JSON object")  # noqa: TRY004 - preserve the public ValueError validation contract
            with self.state_lock or nullcontext():  # #601: same lock as save()
                _restore(self.state, data)
            return self.state

    def save(self) -> None:
        """Atomically replace the destination with a complete JSON snapshot."""
        with self._lock:
            with self.state_lock or nullcontext():
                data = _state_dict(self.state)
            payload = (
                json.dumps(
                    data,
                    default=_json_default,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )

            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                # Same directory guarantees a same-filesystem replacement.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())

                _replace_with_retry(temporary_path, self.path)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass

    def inject_agent_state(self, agent: Any, *, reload: bool = False) -> None:
        """Copy live state into the SDK JSON namespace, never share mutable state.

        Snapshot restoration must not make old snapshot state authoritative.
        With reload=True the JSON file is read first, preserving engine references.
        """
        with self._lock:
            if reload:
                self.load()
            with self.state_lock or nullcontext():  # #601: a consistent copy
                data = _state_dict(self.state)
            agent.state.set("nxp", data)

    def save_session(self, agent: Any, session_manager: Any) -> None:
        """Persist live NXP state after post_turn, then sync the official snapshot.

        These are two ordered writes, not a cross-backend transaction. Disk NXP
        state is authoritative if a process fails between the writes.
        """
        with self._lock:
            self.save()
            self.inject_agent_state(agent)
            session_manager.sync_agent(agent)

    def auto_save_hook(self) -> Callable[..., None]:
        """Return a callback accepting arbitrary post-turn arguments.

        Register or invoke this callback after ``engine.post_turn`` has
        applied its updates. It does not replace or monkey-patch that method.
        """

        def hook(*args: Any, **kwargs: Any) -> None:
            self.save()

        return hook
