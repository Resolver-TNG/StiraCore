from __future__ import annotations

from pathlib import Path

from stiracore.core.checkpoint import Checkpoint, FileCheckpointStore
from stiracore.core.neural_state import NeuralState

SECRET = "TOP-SECRET-PROMPT-本文-42"


def _cp(ts: float, thread: str = "t1") -> Checkpoint:
    return Checkpoint(
        neural_state=NeuralState(),
        conversation_history=[{"role": "user", "content": [{"text": SECRET}]}],
        pending_tool_calls=[],
        timestamp=ts,
        thread_id=thread,
        engine_state={"turn": int(ts), "breaker": {"level": "NONE"}},
    )


def _all_text(base: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in base.glob("*.json"))


def test_old_generations_deleted_when_over_limit(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path, max_checkpoints=3)
    ids = [store.save(_cp(float(i))) for i in range(1, 6)]
    remaining = store.list("t1")
    assert remaining == list(reversed(ids[2:]))
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_limit_is_per_thread(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path, max_checkpoints=1)
    store.save(_cp(1.0, "a"))
    store.save(_cp(2.0, "b"))
    store.save(_cp(3.0, "a"))
    assert len(store.list("a")) == 1
    assert len(store.list("b")) == 1


def test_metadata_only_mode_excludes_message_bodies(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path, store_messages=False)
    cid = store.save(_cp(7.0))
    assert SECRET not in _all_text(tmp_path)
    loaded = store.load(cid)
    assert loaded.conversation_history == []
    assert loaded.engine_state == {"turn": 7, "breaker": {"level": "NONE"}}
    assert loaded.neural_state.stability == NeuralState().stability


def test_default_behavior_unchanged(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path)
    assert store.max_checkpoints == 20
    assert store.store_messages is True
    cid = store.save(_cp(1.0))
    assert SECRET in _all_text(tmp_path)
    assert store.load(cid).conversation_history[0]["content"][0]["text"] == SECRET


def test_metadata_only_mode_scrubs_pending_tool_call_arguments(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path, store_messages=False)
    checkpoint = _cp(8.0)
    checkpoint.pending_tool_calls = [
        {"name": "file_write", "toolUseId": "abc", "input": {"content": SECRET}}
    ]
    cid = store.save(checkpoint)
    assert SECRET not in _all_text(tmp_path)
    loaded = store.load(cid)
    assert loaded.pending_tool_calls == [{"name": "file_write", "toolUseId": "abc"}]
