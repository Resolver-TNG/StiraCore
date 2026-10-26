"""Local, bounded checkpoint generations for explicit at-least-once replay.

Files contain private conversation data, not just metadata, unless the store is
created with ``store_messages=False`` (metadata-only mode). They are written
atomically with owner-only permissions. A checkpoint is a tool *batch* boundary;
replaying after a crash can repeat side effects and requires operator judgment.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .neural_state import SCHEMA_VERSION, NeuralState


@dataclass
class Checkpoint:
    neural_state: NeuralState
    conversation_history: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    timestamp: float
    thread_id: str
    # Preserve breaker hysteresis/cooldown as well as the emotional state.
    engine_state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.neural_state, NeuralState):
            raise TypeError("neural_state must be a NeuralState")
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise ValueError("thread_id must be nonempty")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, (int, float)):
            raise TypeError("timestamp must be finite")
        if not math.isfinite(self.timestamp):
            raise ValueError("timestamp must be finite")
        for value in (self.conversation_history, self.pending_tool_calls):
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise TypeError(
                    "conversation_history and pending_tool_calls must be lists of dicts"
                )
        # Snapshot construction must not retain references to live mutable state.
        self.neural_state = deepcopy(self.neural_state)
        self.conversation_history = deepcopy(self.conversation_history)
        self.pending_tool_calls = deepcopy(self.pending_tool_calls)
        self.engine_state = deepcopy(self.engine_state)


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> str: ...

    def load(self, checkpoint_id: str) -> Checkpoint: ...

    def list(self, thread_id: str | None = None) -> list[str]: ...

    def delete(self, checkpoint_id: str) -> None: ...


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        # Escape all mappings, including ones that happen to contain $bytes.
        return {"$mapping": {key: _encode(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        # A tuple is stored as a JSON array; its mappings must be escaped too (#601 B4).
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"], validate=True)
        if set(value) == {"$mapping"}:
            return {key: _decode(item) for key, item in value["$mapping"].items()}
        raise ValueError("Invalid checkpoint encoding")
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


_METADATA_ONLY_CALL_KEYS = frozenset({"name", "toolUseId", "tool_use_id", "tool"})


class FileCheckpointStore:
    """JSON generations, newest first; keep at most max_checkpoints per thread.

    One sequential writer per agent/thread is expected (as with NXPAgent).
    Missing/corrupt loads raise rather than silently restoring default state.
    """

    def __init__(
        self,
        base_dir: Path | str = ".local/stira/checkpoints",
        *,
        max_checkpoints: int = 20,
        store_messages: bool = True,
    ) -> None:
        if type(max_checkpoints) is not int or max_checkpoints < 1:
            raise ValueError("max_checkpoints must be a positive integer")
        if not isinstance(store_messages, bool):
            raise TypeError("store_messages must be a bool")
        self.base_dir = Path(base_dir)
        self.max_checkpoints = max_checkpoints
        # False: never persist conversation_history (message bodies) to disk.
        self.store_messages = store_messages

    def _path(self, checkpoint_id: str) -> Path:
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[0-9a-f]{32}", checkpoint_id):
            raise ValueError("Invalid checkpoint id")
        path = self.base_dir / f"{checkpoint_id}.json"
        if path.is_symlink():
            raise ValueError("Checkpoint symlinks are not allowed")
        return path

    def save(self, checkpoint: Checkpoint) -> str:
        data = asdict(checkpoint)
        if not self.store_messages:
            data["conversation_history"] = []
            # Tool arguments can carry message bodies; keep identifiers only.
            data["pending_tool_calls"] = [
                {k: v for k, v in call.items() if k in _METADATA_ONLY_CALL_KEYS}
                for call in data["pending_tool_calls"]
            ]
        payload = {
            "schema_version": 1,
            "neural_schema_version": SCHEMA_VERSION,
            "messages_stored": self.store_messages,
            "checkpoint": _encode(data),
        }
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_id = uuid4().hex
        path = self._path(checkpoint_id)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.base_dir, suffix=".tmp", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        self.cleanup(checkpoint.thread_id)
        return checkpoint_id

    def load(self, checkpoint_id: str) -> Checkpoint:
        payload = json.loads(self._path(checkpoint_id).read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") != 1
            or payload.get("neural_schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("Unsupported checkpoint schema")
        data = _decode(payload["checkpoint"])
        if not isinstance(data, dict) or set(data.get("neural_state", {})) != set(
            NeuralState.__dataclass_fields__
        ):
            raise ValueError("Incomplete checkpoint NeuralState")
        data["neural_state"] = NeuralState(**data["neural_state"])
        return Checkpoint(**data)

    def list(self, thread_id: str | None = None) -> list[str]:
        entries = []
        for path in self.base_dir.glob("*.json"):
            checkpoint = self.load(path.stem)
            if thread_id is None or checkpoint.thread_id == thread_id:
                entries.append((checkpoint.timestamp, path.stat().st_mtime_ns, path.stem))
        return [entry[2] for entry in sorted(entries, reverse=True)]

    def delete(self, checkpoint_id: str) -> None:
        self._path(checkpoint_id).unlink(missing_ok=True)

    def cleanup(self, thread_id: str) -> int:
        """Remove old generations only for the specified thread."""
        old = self.list(thread_id)[self.max_checkpoints :]
        for checkpoint_id in old:
            self.delete(checkpoint_id)
        return len(old)
