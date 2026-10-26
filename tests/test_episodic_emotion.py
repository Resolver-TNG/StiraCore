"""#364: an episode keeps the emotion of the moment it was saved, on the floor.

``episodic-summary/v2`` episodes have a sidecar record in ``<memory_dir>/emotion/`` with
the post-turn Neural State (six dimensions), the classifier category/intensity, the
breaker level, the engine turn and the UI turn id. v1 episodes stay as they are and read
as ``None``. Recording only reads the state. Emotion-proximity recall is an opt-in
function that nothing calls.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import math
import os
import random
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore import NeuralState, NXPAgent
from stiracore.agent.stimulus_classifier import StimulusResult
from stiracore.config.data_dir import build_policy, readable_names
from stiracore.config.models import AgentConfig
from stiracore.control.distillers import LLMDistiller
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import DIMS, EMOTION_MATRIX
from stiracore.identity_paths import READ_FLOOR_ERROR
from stiracore.integrations import runtime
from stiracore.observability.config_api import ConfigStore
from stiracore.tools import tool_registry
from stiracore.tools.episodic_emotion import (
    DISTANCE_TOLERANCE,
    FORMAT_V2,
    EmotionSnapshot,
    capture_emotion_snapshot,
    emotion_distance,
    emotion_record_path,
    normalize_snapshot,
    read_emotion_snapshot,
    recall_by_emotion,
)
from stiracore.tools.episodic_formatter import FORMAT_VERSION, record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.tools.memory_tools import create_memory_tools

ROOT = Path(__file__).resolve().parents[1]
TURN_ID = "0123456789abcdef0123456789abcdef"
STATE = {
    "trust": 0.6123456,
    "curiosity": 0.41,
    "vigilance": 0.2,
    "attachment": 0.55,
    "stability": 0.4321,
    "satisfaction": 0.5,
}
SUMMARY = {"category": "joy", "intensity": 0.75, "breaker": "warning", "turn": 7}


class _Agent:
    """The two things capture reads: ``engine`` (state + lock) and ``last_turn_summary``."""

    def __init__(self, state=None, summary=None):
        self.engine = StiraEngine(state=NeuralState(**(state or STATE)))
        self._summary = summary

    @property
    def last_turn_summary(self):
        return copy.deepcopy(self._summary)


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path.resolve() / "memory", "stira")


def _snapshot(state=None, summary=SUMMARY, turn_id=TURN_ID) -> EmotionSnapshot:
    return capture_emotion_snapshot(_Agent(state, summary), turn_id=turn_id)


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- new episodes are v2 with every field -----------------------------------------------


def test_a_new_episode_is_v2_with_the_post_turn_snapshot(tmp_path):
    store = _store(tmp_path)
    entry = record_episode(store, "hello", "hi there", key="chat-1", emotion=_snapshot())
    assert entry.metadata["format"] == FORMAT_V2 == "episodic-summary/v2"
    snapshot = read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, "chat-1"))
    assert snapshot.status == "stored"
    # Raw values, as captured (ADR 0017 Q11: no rounding of the six dimensions).
    assert dict(snapshot.state) == STATE
    assert snapshot.stability == 0.4321
    assert (snapshot.category, snapshot.intensity, snapshot.breaker) == ("joy", 0.75, "warning")
    assert (snapshot.turn, snapshot.turn_id) == (7, TURN_ID)
    record = _record(emotion_record_path(store, "chat-1"))
    assert set(record) == {
        "schema",
        "key",
        "content_sha256",
        "state",
        "category",
        "intensity",
        "breaker",
        "turn",
        "turn_id",
    }
    assert record["content_sha256"] == hashlib.sha256(entry.content.encode()).hexdigest()


def test_unknown_parts_of_a_turn_are_null_not_guessed(tmp_path):
    store = _store(tmp_path)
    # Abstained classifier, failed turn, or a host without a turn summary.
    for key, summary in (("a", {"category": None, "intensity": None}), ("b", None)):
        record_episode(store, "q", "a", key=key, emotion=_snapshot(summary=summary, turn_id=None))
        snapshot = read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, key))
        assert snapshot.status == "stored" and snapshot.complete
        assert snapshot.category is snapshot.intensity is snapshot.breaker is None
        assert snapshot.turn is snapshot.turn_id is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"category": "not-a-stimulus"}, {"category": None}),
        ({"category": 3}, {"category": None}),
        ({"breaker": "panic"}, {"breaker": None}),
        ({"intensity": float("nan")}, {"intensity": None}),
        ({"intensity": -0.1}, {"intensity": None}),
        ({"intensity": True}, {"intensity": None}),
        ({"turn": -1}, {"turn": None}),
        ({"turn": True}, {"turn": None}),
        ({"turn_id": "../../x"}, {"turn_id": None}),
        ({"turn_id": "x" * 65}, {"turn_id": None}),
        ({"state": {"trust": 1.5, "vigilance": "0.1"}}, {}),
    ],
)
def test_each_field_is_validated_on_its_own(raw, expected):
    snapshot = normalize_snapshot(raw)
    assert snapshot.state.get("trust") is None and snapshot.state.get("vigilance") is None
    for name, value in expected.items():
        assert getattr(snapshot, name) == value


def test_the_server_runner_writes_a_v2_episode_with_the_ui_turn_id(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "stira_server_emotion_under_test", ROOT / "dashboard" / "stira_server.py"
    )
    stira_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stira_server)
    captured = {}

    class _FakeAgent:
        def __init__(self, *, state, config, on_breaker=None, tools=(), **_):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tools
            self.state = state

        @property
        def last_turn_summary(self):
            return dict(SUMMARY, breaker="normal")

        def __call__(self, message):
            return "了解です。"

    real_blueprint = stira_server.create_chat_blueprint

    def _capture(agent, **kwargs):
        captured.update(kwargs)
        return real_blueprint(agent, **kwargs)

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "stira")
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _FakeAgent)
    monkeypatch.setattr(stira_server, "create_chat_blueprint", _capture)
    stira_server.create_app()
    store = MemoryStore(tmp_path / "stira" / "memory", "stira")

    captured["turn_recorder"].active.set(TURN_ID)
    assert captured["runner"]("こんにちは") == "了解です。"
    (entry,) = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    assert entry.metadata["format"] == FORMAT_V2
    snapshot = read_emotion_snapshot(store, entry)
    assert snapshot.status == "stored"
    assert snapshot.turn_id == TURN_ID and snapshot.breaker == "normal"
    assert snapshot.stability == pytest.approx(0.7)


# --- recording only reads ---------------------------------------------------------------


def _dims(state: NeuralState) -> dict:
    return {
        **{dim: getattr(state, dim) for dim in DIMS},
        "bc_window": list(state.bc_window),
        "bc_score": state.bc_score,
        "emotion_transcription": state.emotion_transcription,
        "last_stimulus_time": state.last_stimulus_time,
        "last_decay_check": state.last_decay_check,
    }


def test_capture_changes_neither_the_state_the_engine_nor_the_matrix():
    agent = _Agent(summary=SUMMARY)
    matrix = copy.deepcopy(EMOTION_MATRIX)
    before = _dims(agent.engine.state)
    turns = agent.engine._turn_count
    for _ in range(3):
        capture_emotion_snapshot(agent, turn_id=TURN_ID)
    assert _dims(agent.engine.state) == before
    assert agent.engine._turn_count == turns
    assert EMOTION_MATRIX == matrix


def _real_agent(monkeypatch, category="joy"):
    from stiracore.agent import nxp_agent

    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())
    monkeypatch.setattr(
        nxp_agent, "Agent", Mock(return_value=Mock(return_value="hello back", messages=[]))
    )
    classifier = Mock()
    classifier.classify.return_value = StimulusResult(category, 0.8, confidence=0.9)
    return NXPAgent(model_id="test", region_name="test", stimulus_classifier=classifier)


def test_the_chat_runtime_records_the_same_state_with_or_without_a_store(monkeypatch, tmp_path):
    """The classifier runs once per turn and the state moves exactly as before (#364)."""
    with_store, without = _real_agent(monkeypatch), _real_agent(monkeypatch)
    store = _store(tmp_path)
    runtime.ChatSession(with_store, store=store, source="discord").runner("hello")
    runtime.ChatSession(without).runner("hello")
    assert with_store._stimulus_classifier.classify.call_count == 1
    assert {d: getattr(with_store.state, d) for d in DIMS} == {
        d: getattr(without.state, d) for d in DIMS
    }
    (entry,) = store.list_entries(MemoryLayer.EPISODIC)
    snapshot = read_emotion_snapshot(store, entry)
    assert snapshot.category == "joy" and snapshot.intensity == 0.8
    assert snapshot.turn == 1 and snapshot.breaker == "normal"
    assert snapshot.state == {d: getattr(with_store.state, d) for d in DIMS}  # raw


def test_a_failing_snapshot_source_never_fails_the_turn(tmp_path, caplog):
    class _Broken:
        @property
        def engine(self):
            raise RuntimeError("SECRET-STATE")

    assert capture_emotion_snapshot(_Broken()) is None
    assert capture_emotion_snapshot(object()) is None
    entry = record_episode(_store(tmp_path), "q", "a", emotion=None)
    assert entry.metadata["format"] == FORMAT_VERSION  # no snapshot: v1, as before
    assert "SECRET-STATE" not in caplog.text


def test_a_record_write_failure_keeps_the_episode_and_logs_only_a_code(
    tmp_path, monkeypatch, caplog
):
    from stiracore.tools import episodic_emotion

    real_replace = os.replace

    def _boom(source, target, **kwargs):
        if Path(target).parent.name == "emotion":  # only the record, not the episode
            raise OSError("disk full 0.612 joy")
        return real_replace(source, target, **kwargs)

    store = _store(tmp_path)
    monkeypatch.setattr(episodic_emotion.os, "replace", _boom)
    with caplog.at_level(logging.WARNING):
        entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    assert entry is not None and entry.metadata["format"] == FORMAT_V2
    assert "E_EMOTION_WRITE_FAILED" in caplog.text
    assert "0.612" not in caplog.text and "joy" not in caplog.text
    assert read_emotion_snapshot(store, entry).status == "missing"
    assert not [p for p in (store.memory_dir / "emotion").iterdir()]  # temp file removed


def test_a_non_snapshot_emotion_argument_is_ignored(tmp_path, caplog):
    entry = record_episode(_store(tmp_path), "q", "a", emotion={"trust": 0.9})
    assert entry.metadata["format"] == FORMAT_VERSION
    assert "E_EMOTION_INVALID" in caplog.text


# --- v1 episodes: readable, untouched ---------------------------------------------------


V1_TEXT = (
    "---\nsource: web-chat\nformat: episodic-summary/v1\nmethod: deterministic\n"
    "user_chars: 5\nagent_chars: 2\nuser_truncated: false\nagent_truncated: false\n"
    "---\nUser: hello → Agent: hi"
)


def test_v1_and_unversioned_episodes_read_as_null_and_are_never_rewritten(tmp_path):
    store = _store(tmp_path)
    episodic = store.memory_dir / "episodic"
    (episodic / "old-v1.md").write_text(V1_TEXT, encoding="utf-8")
    (episodic / "agent-note.md").write_text("no frontmatter at all", encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in episodic.iterdir()}
    for key in ("old-v1", "agent-note"):
        snapshot = read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, key))
        assert snapshot.status == "v1"
        assert dict(snapshot.state) == dict.fromkeys(DIMS)
        assert snapshot.category is snapshot.breaker is snapshot.turn_id is None
    assert recall_by_emotion(store, NeuralState()) == []
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in episodic.iterdir()}
    assert after == before
    assert not (store.memory_dir / "emotion").exists()  # nothing is back-filled


def test_a_record_that_is_not_this_episode_reads_as_null(tmp_path):
    store = _store(tmp_path)
    entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    # The agent rewrites the episode with its memory tool: no frontmatter, v1.
    write = {t.tool_name: t for t in create_memory_tools(store)}["memory_write"]
    write(layer="episodic", key="chat-1", content="something else")
    assert read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, "chat-1")).status == "v1"
    # ...or keeps a v2 frontmatter with another body: the content hash does not match.
    store.write_text(MemoryLayer.EPISODIC, "chat-1", store.render("forged", entry.metadata))
    forged = store.read(MemoryLayer.EPISODIC, "chat-1")
    assert read_emotion_snapshot(store, forged).status == "mismatch"
    # ...or copies the episode to another key: no record for that key.
    store.write_text(MemoryLayer.EPISODIC, "chat-2", store.render(entry.content, entry.metadata))
    assert read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, "chat-2")).status == (
        "missing"
    )


def test_a_symlinked_oversized_or_broken_record_reads_as_missing(tmp_path):
    store = _store(tmp_path)
    entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    path = emotion_record_path(store, "chat-1")
    good = path.read_text()
    outside = tmp_path / "outside.json"
    outside.write_text(good)
    path.unlink()
    path.symlink_to(outside)
    assert read_emotion_snapshot(store, entry).status == "missing"
    path.unlink()
    path.write_text(" " * 5000 + good)
    assert read_emotion_snapshot(store, entry).status == "missing"
    path.write_text("{not json")
    assert read_emotion_snapshot(store, entry).status == "missing"
    path.write_text(json.dumps(dict(json.loads(good), schema="other/v9")))
    assert read_emotion_snapshot(store, entry).status == "missing"


# --- privacy: the data-directory floor, the memory tools, Harvest -----------------------


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "stira").resolve()
    for name in readable_names():
        (root / name).mkdir(parents=True)
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


def _run(tool, arguments: dict) -> dict:
    use = {"toolUseId": "u1", "name": tool.tool_name, "input": arguments}

    async def collect():
        events = [event async for event in tool.stream(use, {"agent": None})]
        last = events[-1]
        return last["tool_result"] if isinstance(last, dict) else last.tool_result

    return asyncio.run(collect())


def test_the_record_is_on_the_data_directory_read_and_write_floor(data_dir):
    store = MemoryStore(data_dir / "memory", "stira")
    entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    record = emotion_record_path(store, "chat-1")
    policy, _floor = build_policy(AgentConfig(), data_dir)
    tools = {tool.tool_name: tool for tool in tool_registry.get_tools(write_policy=policy)}

    refused = _run(tools["file_read"], {"path": str(record)})
    assert refused["status"] == "error"
    assert READ_FLOOR_ERROR in refused["content"][0]["text"]
    assert "0.612" not in json.dumps(refused, default=str)
    assert not policy.check(str(record)).allowed
    assert not policy.check(str(record.parent / "new.json")).allowed
    # The episode itself stays where it was: readable, and it holds no emotion values.
    episode = _run(tools["file_read"], {"path": str(entry.path)})
    assert episode["status"] == "success"
    text = episode["content"][0]["text"]
    assert "format: episodic-summary/v2" in text
    for value in ("0.612", "0.432", "joy", "warning", TURN_ID, "trust", "stability"):
        assert value not in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_record_and_its_directory_are_private_to_the_user(tmp_path):
    store = _store(tmp_path)
    record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    path = emotion_record_path(store, "chat-1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


def test_memory_tools_search_and_harvest_prompts_never_see_the_snapshot(tmp_path):
    store = _store(tmp_path)
    record_episode(store, "hello", "hi there", key="chat-1", emotion=_snapshot())
    tools = {t.tool_name: t for t in create_memory_tools(store)}
    shown = tools["memory_read"](layer="episodic", key="chat-1")
    found = tools["memory_search"](query="hello", layer="episodic")
    for output in (shown, found):
        for value in ("0.612", "joy", TURN_ID, "trust", "stability"):
            assert value not in output
    for query in ("0.612", "joy", TURN_ID, "warning"):
        assert store.search(query) == []  # the literal search index has no emotion text
    entry = store.read(MemoryLayer.EPISODIC, "chat-1")
    prompt = LLMDistiller.build_prompt(entry.content, dict(entry.metadata))
    assert "0.612" not in prompt and TURN_ID not in prompt
    # Only the format string differs from a v1 episode of the same turn.
    v1 = record_episode(store, "hello", "hi there", key="chat-v1")
    assert {k: v for k, v in entry.metadata.items() if k != "format"} == {
        k: v for k, v in v1.metadata.items() if k != "format"
    }


# --- opt-in emotion-proximity recall ----------------------------------------------------


def _uniform(value: float) -> dict:
    return dict.fromkeys(DIMS, value)


def test_distance_formula_examples():
    assert emotion_distance(_uniform(0.5), _uniform(0.5)) == 0.0
    assert emotion_distance(_uniform(0.0), _uniform(1.0)) == pytest.approx(1.0)
    one = dict(_uniform(0.5), trust=0.5 + 0.6)  # 1.1 is out of range
    with pytest.raises(ValueError):
        emotion_distance(one, _uniform(0.5))
    single = dict(_uniform(0.2), stability=0.8)
    assert emotion_distance(single, _uniform(0.2)) == pytest.approx(0.6 / math.sqrt(6))
    assert emotion_distance(NeuralState(), _uniform(0.5)) == pytest.approx(
        math.sqrt((0.4**2 + 0.2**2) / 6)
    )
    for bad in (
        {**_uniform(0.5), "trust": float("nan")},
        {"trust": 0.5},
        {**_uniform(0.5), "trust": True},
    ):
        with pytest.raises(ValueError):
            emotion_distance(bad, _uniform(0.5))


def test_rounding_to_three_decimals_moves_the_distance_by_at_most_the_tolerance():
    rng = random.Random(364)
    worst = 0.0
    for _ in range(5000):
        a = {dim: rng.random() for dim in DIMS}
        b = {dim: rng.random() for dim in DIMS}
        stored = {dim: round(value, 3) for dim, value in a.items()}
        worst = max(worst, abs(emotion_distance(stored, b) - emotion_distance(a, b)))
    assert DISTANCE_TOLERANCE == 0.0005
    assert worst <= DISTANCE_TOLERANCE + 1e-12


def test_recall_ranks_the_closest_state_first_and_leaves_out_v1(tmp_path):
    store = _store(tmp_path)
    for index, (key, value) in enumerate((("far", 0.9), ("near", 0.52), ("mid", 0.7))):
        record_episode(store, key, "a", key=key, emotion=_snapshot(state=_uniform(value)))
        stamp = 1_700_000_000 + index
        os.utime(store.memory_dir / "episodic" / f"{key}.md", (stamp, stamp))
    (store.memory_dir / "episodic" / "old-v1.md").write_text(V1_TEXT, encoding="utf-8")
    results = recall_by_emotion(store, _uniform(0.5), limit=5)
    assert [r.entry.key for r in results] == ["near", "mid", "far"]
    assert [r.distance for r in results] == pytest.approx([0.02, 0.2, 0.4])
    assert [r.entry.key for r in recall_by_emotion(store, _uniform(0.5), limit=1)] == ["near"]
    # Equal distance: newer first. ``window`` only looks at the newest episodes.
    record_episode(store, "twin", "a", key="twin", emotion=_snapshot(state=_uniform(0.52)))
    assert [r.entry.key for r in recall_by_emotion(store, _uniform(0.5), limit=2)] == [
        "twin",
        "near",
    ]
    assert [r.entry.key for r in recall_by_emotion(store, _uniform(0.5), window=2)] == ["twin"]
    assert not hasattr(results[0], "snapshot")  # the values are not handed out


def test_recall_is_read_only_and_validates_its_arguments(tmp_path):
    store = _store(tmp_path)
    record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    path = store.memory_dir / "episodic" / "chat-1.md"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    accessed = store.list_entries(MemoryLayer.EPISODIC)[0].last_accessed
    recall_by_emotion(store, NeuralState())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert store.list_entries(MemoryLayer.EPISODIC)[0].last_accessed == accessed
    for kwargs in ({"limit": 0}, {"window": 0}, {"window": 1001}, {"limit": True}):
        with pytest.raises(ValueError):
            recall_by_emotion(store, NeuralState(), **kwargs)


def test_nothing_in_stira_calls_the_recall_function():
    callers = [
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "dashboard")
        for path in base.rglob("*.py")
        if "recall_by_emotion" in path.read_text(encoding="utf-8")
    ]
    assert callers == ["src/stiracore/tools/episodic_emotion.py"]


# --- M1: deletion follows the episode; orphans can be found and removed -----------------


def _records(store: MemoryStore) -> list[str]:
    directory = store.memory_dir / "emotion"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def test_deleting_an_episode_deletes_its_emotion_record(tmp_path):
    store = _store(tmp_path)
    record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    record_episode(store, "q", "a", key="chat-2", emotion=_snapshot())
    record_episode(store, "q", "a", key="chat-v1")
    assert store.delete(MemoryLayer.EPISODIC, "chat-1") is True
    assert not emotion_record_path(store, "chat-1").exists()
    assert _records(store) == [emotion_record_path(store, "chat-2").name]
    assert store.delete(MemoryLayer.EPISODIC, "chat-v1") is True  # no record: fine
    assert store.delete(MemoryLayer.EPISODIC, "chat-1") is False  # already gone


def test_a_record_that_cannot_be_removed_keeps_the_episode_for_a_retry(tmp_path, monkeypatch):
    from stiracore.tools import episodic_emotion

    store = _store(tmp_path)
    entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    real_unlink = os.unlink

    def _refuse(path, *a, **k):
        if Path(path).parent.name == "emotion":
            raise PermissionError("no")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(episodic_emotion.os, "unlink", _refuse)
    with pytest.raises(PermissionError):
        store.delete(MemoryLayer.EPISODIC, "chat-1")
    assert entry.path.exists() and emotion_record_path(store, "chat-1").exists()
    monkeypatch.setattr(episodic_emotion.os, "unlink", real_unlink)
    assert store.delete(MemoryLayer.EPISODIC, "chat-1") is True
    assert _records(store) == []


def test_harvest_marks_keep_the_record_matching(tmp_path):
    from stiracore.control.harvest import HarvestScheduler
    from stiracore.core.amygdala import AmygdalaPass

    store = _store(tmp_path)
    record_episode(
        store, "## 結論\n- 決定: A案", "了解。A案で進めます。", key="chat-1", emotion=_snapshot()
    )
    HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()
    entry = store.read(MemoryLayer.EPISODIC, "chat-1")
    assert entry.metadata.get("harvested")  # rewritten with marks, body unchanged
    assert read_emotion_snapshot(store, entry).status == "stored"


def _orphan_fixture(tmp_path):
    store = _store(tmp_path)
    for key in ("keep", "gone", "rewritten"):
        record_episode(store, key, "a", key=key, emotion=_snapshot())
    directory = store.memory_dir / "emotion"
    (store.memory_dir / "episodic" / "gone.md").unlink()  # removed outside MemoryStore
    store.write(MemoryLayer.EPISODIC, "rewritten", "the agent rewrote it")
    keep = emotion_record_path(store, "keep")
    (directory / ("0" * 32 + ".json")).write_text(keep.read_text())  # misnamed copy
    (directory / ("1" * 32 + ".json")).write_text("{broken")
    (directory / ("2" * 32 + ".json")).symlink_to(keep)
    old_tmp = directory / ".emotion-old.tmp"
    old_tmp.write_text("x")
    os.utime(old_tmp, (1, 1))
    (directory / ".emotion-new.tmp").write_text("x")  # a write in progress: left alone
    (directory / "subdir").mkdir()
    return store, directory


def test_orphans_are_classified_and_the_dry_run_writes_nothing(tmp_path, capsys):
    from stiracore.control import emotion_cli

    store, directory = _orphan_fixture(tmp_path)
    before = sorted(p.name for p in directory.iterdir())
    assert emotion_cli.main(["--base-dir", str(store.base_dir), "orphans"]) == 0
    out = json.loads(capsys.readouterr().out)
    reasons = {o["file"]: o["reason"] for o in out["orphans"]}
    assert reasons == {
        emotion_record_path(store, "gone").name: "no-episode",
        emotion_record_path(store, "rewritten").name: "stale",
        "0" * 32 + ".json": "misnamed",
        "1" * 32 + ".json": "unreadable",
        "2" * 32 + ".json": "unreadable",
        ".emotion-old.tmp": "temporary",
        "subdir": "not-a-file",
    }
    assert out["applied"] is False and out["deleted"] == 0
    assert sorted(p.name for p in directory.iterdir()) == before
    assert not (store.memory_dir / "emotion-log.jsonl").exists()
    for value in ("0.612", "joy", TURN_ID, "keep", "gone"):
        assert value not in json.dumps(out)


def test_apply_deletes_orphans_with_an_audit_log_and_keeps_live_records(tmp_path, capsys):
    from stiracore.control import emotion_cli

    store, directory = _orphan_fixture(tmp_path)
    keep = emotion_record_path(store, "keep")
    assert emotion_cli.main(["--base-dir", str(store.base_dir), "orphans", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"] == 6
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        [keep.name, ".emotion-new.tmp", "subdir"]
    )
    assert keep.is_file() and not keep.is_symlink()  # the symlink went, not its target
    entry = store.read(MemoryLayer.EPISODIC, "keep")
    assert read_emotion_snapshot(store, entry).status == "stored"
    log = store.memory_dir / "emotion-log.jsonl"
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["action"] for line in lines] == ["delete_orphan"] * 6
    assert set(lines[0]) == {"ts", "action", "file", "reason"}
    assert "0.612" not in log.read_text() and "joy" not in log.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
    # Nothing left to do on a second run.
    assert emotion_cli.main(["--base-dir", str(store.base_dir), "orphans", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 0


def test_a_log_failure_stops_the_clean_up_before_anything_is_deleted(tmp_path, capsys):
    from stiracore.control import emotion_cli

    store, directory = _orphan_fixture(tmp_path)
    (store.memory_dir / "emotion-log.jsonl").mkdir()  # cannot be appended to
    before = sorted(p.name for p in directory.iterdir())
    assert emotion_cli.main(["--base-dir", str(store.base_dir), "orphans", "--apply"]) == 1
    assert "E_EMOTION_ORPHANS" in capsys.readouterr().err
    assert sorted(p.name for p in directory.iterdir()) == before


# --- M2: the opt-out switch -------------------------------------------------------------


def test_the_switch_defaults_on_and_is_validated(tmp_path):
    assert AgentConfig().episodic_emotion_snapshot is True
    with pytest.raises(ValueError):
        AgentConfig(episodic_emotion_snapshot="false")
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"episodic_emotion_snapshot": False}))
    loaded = AgentConfig.load(path, tool_config_path=tmp_path / "tool-config.json")
    assert loaded.episodic_emotion_snapshot is False
    assert loaded.as_dict()["episodic_emotion_snapshot"] is False


@pytest.mark.parametrize(
    ("env", "configured", "expected"),
    [
        (None, True, True),
        (None, False, False),
        (None, "yes", False),  # only a real True counts
        ("", False, False),  # empty = not set
        ("0", True, False),
        ("off", True, False),
        ("anything", True, False),
        ("1", False, True),
        (" On ", False, True),
    ],
)
def test_the_environment_variable_overrides_the_config(monkeypatch, env, configured, expected):
    from stiracore.tools.episodic_emotion import emotion_snapshot_enabled

    if env is None:
        monkeypatch.delenv("STIRA_EPISODIC_EMOTION", raising=False)
    else:
        monkeypatch.setenv("STIRA_EPISODIC_EMOTION", env)
    assert emotion_snapshot_enabled(configured) is expected


def _server(tmp_path, monkeypatch, *, agent_config=None):
    spec = importlib.util.spec_from_file_location(
        "stira_server_emotion_switch", ROOT / "dashboard" / "stira_server.py"
    )
    stira_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stira_server)
    captured = {}

    class _FakeAgent:
        def __init__(self, *, state, config, on_breaker=None, tools=(), **_):
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tools

        def __call__(self, message):
            return "了解です。"

    real_blueprint = stira_server.create_chat_blueprint

    def _capture(agent, **kwargs):
        captured.update(kwargs)
        return real_blueprint(agent, **kwargs)

    data = tmp_path / ".local" / "stira"
    data.mkdir(parents=True)
    if agent_config is not None:
        (data / "agent-config.json").write_text(agent_config)
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "NXPAgent", _FakeAgent)
    monkeypatch.setattr(stira_server, "create_chat_blueprint", _capture)
    stira_server.create_app(
        ConfigStore(
            data / "gateway-config.json", data / "agent-config.json", data / "tool-config.json"
        )
    )
    return captured, MemoryStore(data / "memory", "stira")


@pytest.mark.parametrize(
    ("config", "env", "expected"),
    [
        ('{"episodic_emotion_snapshot": false}', None, FORMAT_VERSION),
        (None, "0", FORMAT_VERSION),
        ('{"episodic_emotion_snapshot": false}', "1", FORMAT_V2),
        (None, None, FORMAT_V2),
    ],
)
def test_switched_off_the_server_saves_v1_and_no_record(
    tmp_path, monkeypatch, config, env, expected
):
    if env is None:
        monkeypatch.delenv("STIRA_EPISODIC_EMOTION", raising=False)
    else:
        monkeypatch.setenv("STIRA_EPISODIC_EMOTION", env)
    captured, store = _server(tmp_path, monkeypatch, agent_config=config)
    assert captured["runner"]("こんにちは") == "了解です。"
    (entry,) = store.list_entries(MemoryLayer.EPISODIC)
    assert entry.metadata["format"] == expected
    assert bool(_records(store)) is (expected == FORMAT_V2)


def test_a_config_that_breaks_after_start_up_turns_collection_off(tmp_path, monkeypatch):
    monkeypatch.delenv("STIRA_EPISODIC_EMOTION", raising=False)
    captured, store = _server(tmp_path, monkeypatch)
    (tmp_path / ".local" / "stira" / "agent-config.json").write_text("{not json")
    assert captured["runner"]("こんにちは") == "了解です。"
    (entry,) = store.list_entries(MemoryLayer.EPISODIC)
    assert entry.metadata["format"] == FORMAT_VERSION and _records(store) == []


def test_a_failing_ui_turn_id_does_not_drop_the_reply(tmp_path, monkeypatch):
    monkeypatch.delenv("STIRA_EPISODIC_EMOTION", raising=False)
    captured, store = _server(tmp_path, monkeypatch)

    def _boom():
        raise RuntimeError("recorder down")

    monkeypatch.setattr(captured["turn_recorder"].active, "current", _boom)
    assert captured["runner"]("こんにちは") == "了解です。"
    (entry,) = store.list_entries(MemoryLayer.EPISODIC)
    snapshot = read_emotion_snapshot(store, entry)
    assert snapshot.status == "stored" and snapshot.turn_id is None and snapshot.complete


def test_the_chat_runtime_switched_off_saves_v1(monkeypatch, tmp_path):
    monkeypatch.delenv("STIRA_EPISODIC_EMOTION", raising=False)
    store = _store(tmp_path)
    runtime.ChatSession(_real_agent(monkeypatch), store=store, emotion=False).runner("hello")
    (entry,) = store.list_entries(MemoryLayer.EPISODIC)
    assert entry.metadata["format"] == FORMAT_VERSION and _records(store) == []


# --- minor: permissions, never raises ---------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_an_existing_loose_emotion_directory_is_tightened(tmp_path):
    store = _store(tmp_path)
    directory = store.memory_dir / "emotion"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_reading_never_raises_for_what_validation_and_the_os_can_throw(tmp_path, caplog):
    from dataclasses import replace

    store = _store(tmp_path)
    entry = record_episode(store, "q", "a", key="chat-1", emotion=_snapshot())
    record = emotion_record_path(store, "chat-1")
    good = record.read_text()
    cases = [
        replace(entry, key="../escape"),  # ValueError from key validation
        replace(entry, key=""),
        replace(entry, content=None),  # a malformed entry (TypeError/AttributeError)
        replace(entry, metadata=None),
    ]
    for case in cases:
        assert read_emotion_snapshot(store, case).status in {"missing", "v1"}
    record.write_text(json.dumps(["not", "a", "dict"]))
    assert read_emotion_snapshot(store, entry).status == "missing"
    record.write_text(json.dumps(dict(json.loads(good), state=[1, 2, 3])))
    snapshot = read_emotion_snapshot(store, entry)
    assert snapshot.status == "stored" and not snapshot.complete
    record.unlink()
    record.mkdir()  # a directory where the record should be
    assert read_emotion_snapshot(store, entry).status == "missing"
    record.rmdir()
    record.write_text(good)
    if os.name == "posix" and os.geteuid() != 0:
        os.chmod(record, 0)  # PermissionError
        assert read_emotion_snapshot(store, entry).status == "missing"
        os.chmod(record, 0o600)
    # The whole emotion/ directory swapped for a symlink: MemorySymlinkError.
    directory = store.memory_dir / "emotion"
    moved = tmp_path / "moved"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    assert read_emotion_snapshot(store, entry).status == "missing"
    assert "E_EMOTION_READ" in caplog.text
