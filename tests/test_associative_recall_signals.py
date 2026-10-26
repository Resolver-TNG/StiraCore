"""Associative recall (#170) follow-up: pluggable signals, the memory.injected allow-list,
the STIRA_ASSOCIATIVE_RECALL switch, a few-hundred-entry latency check and the offline
ON/OFF fixture. Fixed store + fixed Neural State => the same choice every time.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from stiracore.agent import associative_recall as ar
from stiracore.agent.associative_recall import (
    AssociativeRecall,
    RecallSettings,
    Signal,
    SignalContext,
    recall_settings,
)
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import DIMS, EMOTION_MATRIX, NeuralState
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.observability import memory_injected as mi
from stiracore.observability.turn_events import SCHEMA_VERSION, validate_event
from stiracore.tools.memory_store import MemoryLayer
from tests.test_associative_recall import (
    ANCHORS,
    _assembler,
    _bond_pair,
    _config_store,
    _episode,
    _load_server,
    _recall,
    _state,
    _store,
    _used,
)

ROOT = Path(__file__).resolve().parents[1]


def _legacy_score(candidate, importance):
    """The score formula of PR #421, verbatim (the reference the signals must equal)."""
    bond_pull, guard = candidate.weights
    bond_term = ar.W_BOND * bond_pull * candidate.bond * (1.0 - guard)
    protective_term = ar.W_PROTECTIVE * guard * candidate.protective
    candidate.reasons = [r for r in candidate.reasons if r not in (ar.R_BOND, ar.R_PROTECTIVE)]
    if bond_term > 0:
        candidate.reasons.append(ar.R_BOND)
    if protective_term > 0:
        candidate.reasons.append(ar.R_PROTECTIVE)
    candidate.score = (
        ar.W_RELEVANCE * candidate.relevance
        + ar.W_IMPORTANCE * importance
        + ar.W_RECENCY * candidate.recency
        + bond_term
        + protective_term
    )


class _LegacyRecall(AssociativeRecall):
    def _score(self, candidate, context):
        _legacy_score(candidate, context.importance)


# --- the default signals are the #421 formula, exactly ---------------------------------


def test_default_signals_equal_the_previous_formula_bit_for_bit():
    rng = random.Random(170)
    recall = AssociativeRecall(None, dict)
    for _ in range(2000):
        values = [rng.random() for _ in range(7)]
        relevance, recency, bond, protective, pull, guard, importance = values
        guard = rng.choice([0.0, 1.0, guard])
        base = {"relevance": relevance, "recency": recency, "bond": bond, "protective": protective}
        new = ar.Candidate("k", 0.0, [ar.R_SEED], "", **base, weights=(pull, guard))
        old = copy.deepcopy(new)
        recall._score(new, SignalContext({}, pull, guard, importance))
        _legacy_score(old, importance)
        assert new.score == old.score  # exact, not approx: ties break the same way
        assert new.reasons == old.reasons


@pytest.mark.parametrize(
    "state",
    [
        _state(),
        _state(trust=1.0, attachment=1.0),
        _state(trust=0.0, attachment=0.0),
        _state(trust=1.0, attachment=1.0, stability=0.2),
        _state(stability=0.4),
    ],
)
def test_selection_and_event_are_unchanged_from_the_previous_formula(tmp_path, state):
    store = _store(tmp_path)
    _episode(store, "anchor", "Keep a consistent identity: garden tomatoes.", mtime=1_000)
    _episode(store, "thanks", "Thank you, garden tomatoes report.", mtime=2_000)
    _episode(store, "plain", "Status: garden tomatoes report.", mtime=3_000, importance=0.9)
    _episode(store, "assoc", "tomatoes and zucchini", mtime=4_000)
    _episode(store, "other", "unrelated weather note", mtime=5_000)
    query = "How are the garden tomatoes?"
    kwargs = {"identity_anchors": ANCHORS}
    new = _recall(store, state, **kwargs).recall(query, exclude_keys=["other"])
    old = _LegacyRecall(store, lambda: state, RecallSettings(enabled=True), **kwargs)
    reference = old.recall(query, exclude_keys=["other"])
    assert new.as_event_data() == reference.as_event_data()
    assert new.section == reference.section


# --- signals are pluggable --------------------------------------------------------------


@dataclass(frozen=True)
class _MetadataBoost(Signal):
    """A test stand-in for a later retention signal (#173): reads stored front matter."""

    name: str = "pinned"
    weight: float = 0.5
    reason: str | None = "R_PINNED"

    def term(self, candidate, context):
        return self.weight if context.loaded and context.metadata.get("pinned") else 0.0


def test_an_added_signal_changes_the_ranking_without_another_interface_change(tmp_path):
    store = _store(tmp_path)
    # A front-matter flag the default signals ignore, written by the server's path so the
    # provenance verifies. Both carry the key: the index text includes the front matter.
    for key, pinned, mtime in (("old", True, 1_000), ("new", False, 2_000)):
        store.write(MemoryLayer.EPISODIC, key, "garden tomatoes report", {"pinned": pinned})
        record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
        os.utime(store.memory_dir / "episodic" / f"{key}.md", (mtime, mtime))
    store.search("refresh-the-index-order")
    default = _recall(store, _state()).recall("garden tomatoes")
    boosted = _recall(store, _state(), signals=(*ar.DEFAULT_SIGNALS, _MetadataBoost()))
    result = boosted.recall("garden tomatoes")
    assert _used(default) == ["new", "old"]
    assert _used(result) == ["old", "new"]
    assert "R_PINNED" in result.used[0].reasons
    assert "R_PINNED" not in result.used[1].reasons
    data = result.as_event_data()
    assert data["signals"] == ["relevance", "importance", "recency", "bond", "protective", "pinned"]
    mi.validate_memory_injected(data)


def test_signals_see_a_read_only_copy_of_the_neural_state(tmp_path):
    store = _bond_pair(tmp_path)
    engine_state = NeuralState()
    before = {dim: getattr(engine_state, dim) for dim in DIMS}
    matrix = copy.deepcopy(EMOTION_MATRIX)
    seen = []

    @dataclass(frozen=True)
    class _Writer(Signal):
        name: str = "writer"
        weight: float = 0.0

        def term(self, candidate, context):
            with pytest.raises(TypeError):
                context.state["trust"] = 1.0  # type: ignore[index]
            seen.append(dict(context.state))
            return 0.0

    def provider():
        return {dim: getattr(engine_state, dim) for dim in DIMS}

    recall = AssociativeRecall(
        store, provider, RecallSettings(enabled=True), signals=(*ar.DEFAULT_SIGNALS, _Writer())
    )
    recall.recall("garden tomatoes")
    assert seen
    assert {dim: getattr(engine_state, dim) for dim in DIMS} == before
    assert EMOTION_MATRIX == matrix


@pytest.mark.parametrize(
    "signals",
    [
        (),
        (ar.RelevanceSignal(), ar.RelevanceSignal()),
        (ar.RelevanceSignal(name="Bad Name"),),
        (ar.RelevanceSignal(weight=float("nan")),),
        (ar.RelevanceSignal(weight=True),),
        (ar.BondSignal(reason="R_SEED"),),  # matching codes are not a signal's
        (ar.BondSignal(reason="free text"),),
        ("relevance",),
    ],
)
def test_signals_are_validated(tmp_path, signals):
    with pytest.raises(ValueError):
        AssociativeRecall(_store(tmp_path), dict, signals=signals)


def test_a_non_finite_signal_fails_the_recall_not_the_turn(tmp_path):
    @dataclass(frozen=True)
    class _Broken(Signal):
        name: str = "broken"
        weight: float = 1.0

        def term(self, candidate, context):
            return float("inf")

    store = _bond_pair(tmp_path)
    recall = _recall(store, _state(), signals=(*ar.DEFAULT_SIGNALS, _Broken()))
    recall.set_query("garden tomatoes")
    assert recall.section() == ""
    assert recall.last_result.status == ar.S_ERROR


# --- use / drop: the threshold and the discarded count ----------------------------------


def test_the_threshold_decides_and_the_discarded_count_is_logged(tmp_path):
    store = _store(tmp_path)
    _episode(store, "seed", "zucchini harvest with grandmother", mtime=1_000)
    # Shares only an association ("grandmother"): relevance 0.2 => below MIN_SCORE.
    _episode(store, "assoc", "grandmother visited", mtime=900)
    for index in range(4):
        _episode(store, f"fill-{index}", f"garden filler {index}", mtime=800 + index)
    result = _recall(store, _state()).recall("zucchini")
    dropped = {c.key: c for c in result.dropped}
    assert _used(result) == ["seed"]
    assert dropped["assoc"].decision == ar.D_LOW_SCORE
    assert dropped["assoc"].score < ar.MIN_SCORE <= result.used[0].score
    data = result.as_event_data()
    assert data["discarded"] == len(data["dropped"]) + data["unranked"] == 1


def test_unranked_candidates_count_as_discarded(tmp_path):
    store = _store(tmp_path)
    for index in range(ar.MAX_CANDIDATES + 5):
        _episode(store, f"e{index:02d}", f"garden note {index}", mtime=1_000 + index, refresh=False)
    store.search("refresh-the-index-order")
    data = _recall(store, _state()).recall("garden").as_event_data()
    assert data["unranked"] == 5
    assert data["discarded"] == len(data["dropped"]) + 5
    assert len(data["used"]) + len(data["dropped"]) == ar.MAX_CANDIDATES


# --- memory.injected allow-list ---------------------------------------------------------


def test_recall_codes_match_the_event_allow_list():
    assert set(ar.STATUSES) == mi.STATUSES
    assert set(ar.DROP_REASONS) == mi.DROP_REASONS
    assert all(mi.USE_REASON.fullmatch(code) for code in ar.USE_REASONS)
    assert ar.SOURCE == mi.SOURCE and ar.EVENT_SCHEMA == mi.SCHEMA == 2


def _event(data):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "b" * 32,
        "turn_id": "t1",
        "seq": 1,
        "ts": time.time(),
        "type": "memory.injected",
        "data": data,
    }


def _valid(tmp_path):
    store = _bond_pair(tmp_path)
    return _recall(store, _state()).recall("How are the garden tomatoes?").as_event_data()


def _mutations():
    def put(path, value):
        def apply(data):
            target = data
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value

        return apply

    def drop(name):
        return lambda data: data.pop(name)

    return {
        "extra key": put(("query",), "garden tomatoes"),
        "missing key": drop("signals"),
        "old schema": put(("schema",), 1),
        "bad source": put(("source",), "other"),
        "bad status": put(("status",), "maybe"),
        "body in used row": put(("used", 0, "content"), "Thank you, garden tomatoes"),
        "free-text reason": put(("used", 0, "reasons"), ["because it fits"]),
        "empty reasons": put(("used", 0, "reasons"), []),
        "multi-line key": put(("used", 0, "key"), "a\nb"),
        "long key": put(("used", 0, "key"), "k" * 129),
        "nan score": put(("used", 0, "score"), float("nan")),
        "bool score": put(("used", 0, "score"), True),
        "bad layer": put(("used", 0, "layer"), "soul"),
        "negative count": put(("chars",), -1),
        "bool count": put(("associations",), True),
        "discarded below dropped": put(("discarded",), -1),
        "free-text signal": put(("signals",), ["Relevance Score"]),
        "duplicate signal": put(("signals",), ["bond", "bond"]),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_memory_injected_outside_the_allow_list_is_rejected(tmp_path, name):
    data = _valid(tmp_path)
    validate_event(_event(copy.deepcopy(data)))
    _mutations()[name](data)
    with pytest.raises(ValueError):
        validate_event(_event(data))


def test_a_dropped_row_with_an_unknown_code_is_rejected(tmp_path):
    store = _bond_pair(tmp_path)
    result = _recall(store, _state()).recall("garden tomatoes", exclude_keys=["plain"])
    data = result.as_event_data()
    assert data["dropped"] and data["dropped"][0]["reason"] == ar.D_IN_CONTEXT
    validate_event(_event(copy.deepcopy(data)))
    data["dropped"][0]["reason"] = "D_SOMETHING_ELSE"
    with pytest.raises(ValueError):
        validate_event(_event(data))


def test_the_event_carries_no_body_term_or_state_value(tmp_path):
    store = _bond_pair(tmp_path)
    state = _state(trust=0.8123, attachment=0.7456)
    data = _recall(store, state).recall("How are the garden tomatoes?").as_event_data()
    text = json.dumps(data, ensure_ascii=False)
    for leak in ("garden", "tomatoes", "Thank you", "0.8123", "0.7456"):
        assert leak not in text


# --- STIRA_ASSOCIATIVE_RECALL ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "configured", "expected"),
    [
        (None, {}, False),
        ("", {}, False),
        ("  ", {"enabled": True}, True),
        ("1", {}, True),
        ("ON", {}, True),
        ("true", {"enabled": False}, True),
        ("0", {"enabled": True}, False),
        ("off", {"enabled": True}, False),
        ("no-thanks", {"enabled": True}, False),
    ],
)
def test_the_variable_overrides_enabled_and_keeps_the_limits(raw, configured, expected):
    env = {} if raw is None else {ar.ENV_SWITCH: raw}
    settings = recall_settings({**configured, "max_items": 2}, env)
    assert settings.enabled is expected
    assert settings.max_items == 2


def test_the_variable_does_not_rescue_invalid_options():
    with pytest.raises(ValueError):
        recall_settings({"max_items": 99}, {ar.ENV_SWITCH: "1"})


def test_server_follows_the_variable(tmp_path, monkeypatch):
    server = _load_server("stira_server_recall_env")
    engine = StiraEngine(state=NeuralState())
    store = _store(tmp_path)
    monkeypatch.setenv(ar.ENV_SWITCH, "1")
    recall = server._associative_recall(_config_store(tmp_path, {}), store, engine)
    assert recall is not None and recall.settings.enabled
    monkeypatch.setenv(ar.ENV_SWITCH, "0")
    on = _config_store(tmp_path, {"associative_recall": {"enabled": True}})
    assert server._associative_recall(on, store, engine) is None
    monkeypatch.delenv(ar.ENV_SWITCH)
    assert server._associative_recall(on, store, engine) is not None


def test_default_prompt_stays_byte_identical_with_the_variable_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ar.ENV_SWITCH, raising=False)
    server = _load_server("stira_server_recall_default")
    engine = StiraEngine(state=NeuralState())
    store = _bond_pair(tmp_path)
    recall = server._associative_recall(_config_store(tmp_path, {}), store, engine)
    assert recall is None
    plain = _assembler(store, engine=engine)
    wired = _assembler(store, recall, engine=engine)
    assert wired.build_system_prompt() == plain.build_system_prompt()
    assert wired.build_prompt_parts() == plain.build_prompt_parts()


# --- determinism and latency over a few hundred entries ---------------------------------


def _hundreds(tmp_path, count=300):
    store = _store(tmp_path)
    topics = ["garden", "kaggle", "robot", "curry", "kyoto", "budget", "music", "train"]
    for index in range(count):
        topic = topics[index % len(topics)]
        text = f"Talked about {topic} item {index} and note{index % 13} with care."
        if index % 29 == 0:
            text = f"Thank you, I am glad: {text}"
        _episode(store, f"chat-{index:04d}", text, mtime=1_000 + index, refresh=False)
    store.search("refresh-the-index-order")
    return store


def test_a_fixed_store_and_state_give_the_same_choice_across_instances(tmp_path):
    store = _hundreds(tmp_path)
    state = _state(trust=0.9, attachment=0.8, stability=0.45)
    first = _recall(store, state, identity_anchors=ANCHORS).recall("How is the robot note7?")
    second = _recall(store, state, identity_anchors=ANCHORS).recall("How is the robot note7?")
    assert first.status == ar.S_RECALLED
    assert first.as_event_data() == second.as_event_data()
    assert first.section == second.section


def test_recall_over_a_few_hundred_entries_stays_under_the_target(tmp_path):
    store = _hundreds(tmp_path, 300)
    recall = _recall(store, _state(), settings=RecallSettings(enabled=True, window=300))
    query = "How is the robot note7?"
    recall.recall(query)  # warm the file cache
    timings = []
    for _ in range(9):
        started = time.perf_counter()
        result = recall.recall(query)
        timings.append(time.perf_counter() - started)
    assert result.used
    median = statistics.median(timings)
    print(f"associative recall, 300 entries: median {median * 1000:.1f} ms")
    # Target 50 ms (#170); measured ~12 ms on an M1 Max. The bound leaves room for slow CI.
    assert median < 0.15, timings


# --- the offline ON/OFF fixture (not #143) ----------------------------------------------


def _eval_module():
    path = ROOT / "scripts" / "eval_associative_recall.py"
    spec = importlib.util.spec_from_file_location("eval_associative_recall", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_fixture_on_retrieves_more_and_contaminates_less_than_off():
    module = _eval_module()
    result = module.evaluate()
    assert result == module.evaluate()  # deterministic
    for state in module.STATES:
        off, on = result[f"{state}/off"], result[f"{state}/on"]
        assert off["retrieval"] == 0.0 and off["contamination"] == 1.0  # newest 5 = small talk
        assert on["retrieval"] == 0.611
        assert on["contamination"] == 0.792
        assert on["added"] == 13 and on["added_contamination"] == 0.154
    # The misses are association-only matches below the threshold (D_LOW_SCORE).
    missed = {code for row in result["neutral/on"]["per_query"] for code in row["missed"].values()}
    assert missed == {ar.D_LOW_SCORE}
    text = json.dumps(result, ensure_ascii=False)
    assert "tomatoes" not in text and "連想想起" not in text  # keys and counts only


# --- no model call, no network ----------------------------------------------------------


def test_a_recall_turn_opens_no_connection_and_builds_no_model(tmp_path, monkeypatch):
    import socket

    from stiracore.agent import model_factory

    def refuse(*_args, **_kwargs):
        raise AssertionError("associative recall must not reach the network or a model")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for name in dir(model_factory):
        if name.startswith("create_"):
            monkeypatch.setattr(model_factory, name, refuse)
    store = _bond_pair(tmp_path)
    recall = _recall(store, _state(trust=0.9), identity_anchors=ANCHORS)
    recall.set_query("How are the garden tomatoes?")
    prompt = _assembler(store, recall, episodic_limit=1).build_system_prompt()
    assert "## Associated Memory" in prompt
    assert recall.take_result().status == ar.S_RECALLED
