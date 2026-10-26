"""Emotion-proximity recall (#364, ADR 0017): opt-in, default off, bounded, read-only.

Every guard has a test and, next to it, a mutation check: the same observation with the guard
disabled (monkeypatched) must differ, so the test would catch its removal.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
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
    EmotionProximitySignal,
    RecallSettings,
    SignalContext,
    proximity_holdout,
    recall_settings,
)
from stiracore.config.models import AgentConfig
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import BASELINES, DIMS, EMOTION_MATRIX, NeuralState
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.observability.turn_events import SCHEMA_VERSION, validate_event
from stiracore.tools import episodic_emotion as ee
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from tests import recall_golden_cases as golden
from tests.test_associative_recall import _assembler, _config_store, _load_server

ROOT = Path(__file__).resolve().parents[1]

# Distinct stored values, easy to grep for (never expected outside the protected record).
CLOSE = {"trust": 0.51734, "curiosity": 0.50291, "vigilance": 0.10463,
         "attachment": 0.49817, "stability": 0.70382, "satisfaction": 0.50627}  # fmt: skip
FAR = {"trust": 0.48211, "curiosity": 0.91357, "vigilance": 0.60793,
       "attachment": 0.52469, "stability": 0.90531, "satisfaction": 0.90173}  # fmt: skip
NEGATIVE = {"trust": 0.45127, "curiosity": 0.48359, "vigilance": 0.40281,
            "attachment": 0.50443, "stability": 0.55617, "satisfaction": 0.35089}  # fmt: skip
LEAKS = [f"{v:.5f}"[:7] for s in (CLOSE, FAR, NEGATIVE) for v in s.values()]
CURRENT = dict(BASELINES)  # neutral mood, NORMAL
LIVE = {"trust": 0.50113, "curiosity": 0.50519, "vigilance": 0.10227,
        "attachment": 0.50331, "stability": 0.70119, "satisfaction": 0.50447}  # fmt: skip


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path.resolve() / "memory", "stira")


def _episode(store, key, text, state=None, *, mtime, importance=None, v1=False):
    """A server-recorded episode; with ``state`` a v2 episode with a stored snapshot."""
    metadata = {} if v1 or state is None else {"format": ee.FORMAT_V2}
    if importance is not None:
        metadata["importance"] = importance
    entry = store.write(MemoryLayer.EPISODIC, key, text, metadata)
    record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
    if state is not None and not v1:
        snapshot = ee.normalize_snapshot({"state": state, "breaker": "normal", "turn": 1})
        ee.write_emotion_snapshot(store, entry, snapshot)
    os.utime(store.memory_dir / "episodic" / f"{key}.md", (mtime, mtime))
    return entry


def _swap_store(tmp_path, *, d_state=CLOSE, e_state=CLOSE, long_d=False):
    """U0 (off) = a, b, c (seed + another term, far mood); d, e are eligible but lower
    (another term only, high importance, newer) with a close mood."""
    store = _store(tmp_path)
    for index, key in enumerate("abc"):
        _episode(store, key, f"Zucchini harvest notes {key}.", FAR, mtime=1_000 + index,
                 importance=0.0)  # fmt: skip
    d_text = "Zucchini notes d. " + ("word " * 110 if long_d else "")
    _episode(store, "d", d_text, d_state, mtime=2_000, importance=1.0)
    _episode(store, "e", "Zucchini notes e.", e_state, mtime=2_001, importance=1.0)
    for index in range(4):
        _episode(store, f"z{index}", f"unrelated filler {index}", mtime=500 + index)
    store.search("refresh-the-index-order")
    return store


QUERY = "zucchini harvest notes"


def _recall(store, *, proximity=True, state=CURRENT, level="normal", turn=1, **kwargs):
    settings = kwargs.pop("settings", None) or RecallSettings(
        enabled=True, emotion_proximity=proximity
    )
    return AssociativeRecall(
        store,
        lambda: dict(state),
        settings,
        breaker_level=lambda: level,
        turn_number=lambda: turn,
        **kwargs,
    )


def _used(result):
    return [c.key for c in result.used]


def _decisions(result):
    return {c.key: c.decision for c in result.used + result.dropped}


# --- off: byte-identical to main --------------------------------------------------------


def test_off_replays_the_cases_recorded_on_main_byte_for_byte(tmp_path):
    expected = json.loads((ROOT / "tests" / "fixtures" / "recall_off_golden.json").read_text())
    actual = golden.run_cases(golden.build_store(tmp_path))
    differing = sorted(case for case in expected if actual.get(case) != expected[case])
    assert set(actual) == set(expected) and not differing, differing[:5]  # no giant diff
    # Mutation check: a different bond weight (one term of the formula) is caught.
    heavier = (*ar.DEFAULT_SIGNALS[:3], ar.BondSignal(weight=0.26), ar.DEFAULT_SIGNALS[4])

    class _Mutated(AssociativeRecall):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, signals=heavier, **kwargs)

    mutated = golden.run_cases(golden.build_store(tmp_path / "m"), make=_Mutated)
    assert json.dumps(mutated, sort_keys=True) != json.dumps(expected, sort_keys=True)


def test_off_adds_no_signal_and_reads_no_snapshot(tmp_path, monkeypatch):
    store = _swap_store(tmp_path)
    calls = []
    monkeypatch.setattr(ee, "read_emotion_snapshot", lambda *a: calls.append(a))
    recall = _recall(store, proximity=False)
    result = recall.recall(QUERY)
    assert [s.name for s in recall.signals] == [s.name for s in ar.DEFAULT_SIGNALS]
    assert calls == []
    data = result.as_event_data()
    assert data["schema"] == 2 and "proximity_gate" not in data
    assert all("proximity" not in row for row in data["used"] + data["dropped"])


# --- settings, variable, AgentConfig ----------------------------------------------------


def test_the_setting_is_off_by_default_and_must_be_a_boolean():
    assert RecallSettings().emotion_proximity is False
    assert RecallSettings.from_options({}).emotion_proximity is False
    assert RecallSettings.from_options({"emotion_proximity": True}).emotion_proximity is True
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError):
            RecallSettings.from_options({"emotion_proximity": bad})
    config = AgentConfig(associative_recall={"enabled": True, "emotion_proximity": True})
    assert config.associative_recall == {"enabled": True, "emotion_proximity": True}
    with pytest.raises(ValueError):
        AgentConfig(associative_recall={"emotion_proximity": "on"})


@pytest.mark.parametrize(
    ("raw", "configured", "expected"),
    [
        (None, {}, False),
        ("", {"emotion_proximity": True}, True),
        ("1", {}, True),
        ("on", {"emotion_proximity": False}, True),
        ("0", {"emotion_proximity": True}, False),
        ("nope", {"emotion_proximity": True}, False),
    ],
)
def test_the_variable_overrides_emotion_proximity_only(raw, configured, expected):
    env = {} if raw is None else {ar.PROXIMITY_ENV_SWITCH: raw}
    settings = recall_settings({**configured, "enabled": True, "max_items": 2}, env)
    assert settings.emotion_proximity is expected
    assert settings.enabled is True and settings.max_items == 2


def test_the_server_wires_the_gates_only_when_switched_on(tmp_path, monkeypatch):
    monkeypatch.delenv(ar.PROXIMITY_ENV_SWITCH, raising=False)
    monkeypatch.delenv(ar.ENV_SWITCH, raising=False)
    server = _load_server("stira_server_proximity_wiring")
    engine = StiraEngine(state=NeuralState())
    store = _store(tmp_path)
    off = server._associative_recall(
        _config_store(tmp_path, {"associative_recall": {"enabled": True}}), store, engine
    )
    assert off.emotion_proximity is False and off.breaker_level is None
    assert [s.name for s in off.signals] == [s.name for s in ar.DEFAULT_SIGNALS]
    on_config = {"associative_recall": {"enabled": True, "emotion_proximity": True}}
    on = server._associative_recall(_config_store(tmp_path, on_config), store, engine)
    assert on.emotion_proximity is True
    assert [s.name for s in on.signals][-1] == "emotion_proximity"
    assert on.breaker_level().value == "normal"
    assert on.turn_number() == 1  # the number post_turn will give the coming turn
    assert on.affect_of is None  # Q6: the bond term keeps its inputs
    monkeypatch.setenv(ar.PROXIMITY_ENV_SWITCH, "0")
    assert server._associative_recall(_config_store(tmp_path, on_config), store, engine)
    assert not server._associative_recall(
        _config_store(tmp_path, on_config), store, engine
    ).emotion_proximity


# --- formula -----------------------------------------------------------------------------


def test_mood_distance_uses_the_four_mood_dimensions_with_equal_weights():
    a = dict(BASELINES)
    b = {**a, "curiosity": a["curiosity"] + 0.25}
    assert ee.mood_distance(a, b) == pytest.approx(0.125)
    assert ee.mood_similarity(a, b) == pytest.approx(0.375)
    assert ee.mood_similarity(a, {**a, "vigilance": 0.5}) == 0.0  # 0.4 apart: beyond R
    assert ee.mood_similarity(a, {**a, "trust": 0.0, "attachment": 1.0}) == 1.0  # not mood
    assert ee.mood_similarity(a, {**a, "stability": None}) is None  # no partial distance
    assert ee.polarity(BASELINES) == ee.NEUTRAL
    assert ee.polarity({**BASELINES, "stability": 0.52, "vigilance": 0.40,
                        "satisfaction": 0.38}) == ee.NEGATIVE  # fmt: skip
    assert ee.valence_index({"stability": 0.5}) is None and ee.polarity({}) == ee.UNKNOWN


def test_proximity_shares_the_bond_budget_and_never_exceeds_it():
    """M2: bond + proximity <= W_BOND * (1 - guard), for any pull, bond, s and guard."""
    rng = random.Random(364)
    signal, bond_signal = EmotionProximitySignal(), ar.BondSignal()
    snapshot = ee.normalize_snapshot({"state": CLOSE}, status="stored")
    for _ in range(3000):
        state = {d: rng.random() for d in DIMS}
        state.update({k: CLOSE[k] + rng.uniform(-0.1, 0.1) for k in ee.MOOD_DIMS})
        guard = rng.choice([0.0, 0.0, rng.random()])
        context = SignalContext(state, rng.random(), guard, snapshot=snapshot, loaded=True,
                                proximity_gate=ar.G_ON)  # fmt: skip
        candidate = ar.Candidate("k", 0.0, [], "")
        candidate.bond = rng.random()
        total = bond_signal.term(candidate, context) + signal.term(candidate, context)
        assert signal.term(candidate, context) >= 0.0
        assert total <= ar.W_BOND * (1.0 - guard) + 1e-12
        assert signal.term(candidate, context) <= ar.W_PROXIMITY + 1e-12

    # Mutation check: the same term without the shared cap goes over the budget.
    @dataclass(frozen=True)
    class _Uncapped(EmotionProximitySignal):
        def term(self, candidate, context):
            return self.weight * (ar.proximity_value(candidate, context) or 0.0)

    candidate = ar.Candidate("k", 0.0, [], "")
    candidate.bond = 1.0
    context = SignalContext(dict(CLOSE), 1.0, 0.0, snapshot=snapshot, loaded=True,
                            proximity_gate=ar.G_ON)  # fmt: skip
    assert bond_signal.term(candidate, context) + _Uncapped().term(candidate, context) > 0.25


# --- the swap budget (M3) and "never admits" (M1) ----------------------------------------


def test_at_most_one_used_item_changes_because_of_proximity(tmp_path, monkeypatch):
    store = _swap_store(tmp_path)
    off = _recall(store, proximity=False).recall(QUERY)
    on = _recall(store).recall(QUERY)
    assert _used(off) == ["a", "b", "c"] or set(_used(off)) == {"a", "b", "c"}
    assert len(set(_used(on)) - set(_used(off))) == 1
    assert len(set(_used(off)) - set(_used(on))) == 1
    swapped = next(c for c in on.used if c.key not in _used(off))
    assert ar.R_EMOTION_SWAP in swapped.reasons and ar.R_EMOTION_PROXIMITY in swapped.reasons
    assert ar.D_EMOTION_BUDGET in _decisions(on).values()  # the second lift was refused
    assert on.proximity_swaps == 1 and on.as_event_data()["proximity_swaps"] == 1
    # Mutation check: a larger budget lets both close episodes in.
    monkeypatch.setattr(ar, "MAX_EMOTION_SWAPS", 3)
    wider = _recall(store).recall(QUERY)
    assert len(set(_used(wider)) - set(_used(off))) == 2


def test_a_swap_that_would_push_out_two_items_keeps_the_off_choice(tmp_path, monkeypatch):
    store = _swap_store(tmp_path, long_d=True, e_state=FAR)
    settings = RecallSettings(enabled=True, emotion_proximity=True, max_chars=600)
    off = _recall(store, proximity=False, settings=RecallSettings(enabled=True)).recall(QUERY)
    on = _recall(store, settings=settings).recall(QUERY)
    assert _used(on) == _used(off) and on.items == off.items
    assert _decisions(on)["d"] == ar.D_EMOTION_BUDGET and on.proximity_swaps == 0
    # Mutation check: without the fall-back the long swap pushes out two items.
    original = ar.MAX_EMOTION_SWAPS
    real = AssociativeRecall._recall_with_proximity

    def no_fallback(self, *args):
        monkeypatch.setattr(ar, "MAX_EMOTION_SWAPS", 99)
        try:
            return real(self, *args)
        finally:
            monkeypatch.setattr(ar, "MAX_EMOTION_SWAPS", original)

    monkeypatch.setattr(AssociativeRecall, "_recall_with_proximity", no_fallback)
    pushed = _recall(store, settings=settings).recall(QUERY)
    assert len(set(_used(off)) - set(_used(pushed))) >= 2


def test_proximity_never_admits_a_candidate_below_the_threshold(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _episode(store, "seed", "Zucchini harvest with grandmother.", FAR, mtime=1_000)
    # Shares only an association: relevance 0.2, below MIN_SCORE without proximity.
    _episode(store, "assoc", "grandmother visited", CLOSE, mtime=900)
    for index in range(4):  # v2 as well, so the front-matter words are widespread
        _episode(store, f"f{index}", f"garden filler {index}", FAR, mtime=800 + index)
    store.search("refresh-the-index-order")
    on = _recall(store).recall("zucchini")
    assoc = next(c for c in on.dropped if c.key == "assoc")
    assert assoc.decision == ar.D_LOW_SCORE
    assert assoc.proximity_term > 0 and assoc.score >= ar.MIN_SCORE > assoc.score_off
    # Mutation check: deciding the threshold on the score with proximity admits it.
    real = AssociativeRecall._select

    def by_full_score(self, order, eligible_score, **kwargs):
        return real(self, order, lambda c: c.score, **kwargs)

    monkeypatch.setattr(AssociativeRecall, "_select", by_full_score)
    assert "assoc" in _used(_recall(store).recall("zucchini"))


# --- gates: NORMAL only (M5), no negative-to-negative lift (M4), holdout (M6) ------------


@pytest.mark.parametrize("level", ["warning", "critical", "coherence"])
def test_proximity_is_off_outside_normal(tmp_path, monkeypatch, level):
    store = _swap_store(tmp_path)
    off = _recall(store, proximity=False).recall(QUERY)
    on = _recall(store, level=level).recall(QUERY)
    assert on.proximity_gate == ar.G_BREAKER
    assert _used(on) == _used(off) and on.items == off.items
    assert all(c.proximity in (None, 0.0) for c in on.used + on.dropped)
    # Mutation check: ignoring the breaker level lets the swap through.
    monkeypatch.setattr(AssociativeRecall, "_proximity_gate", lambda self: ar.G_ON)
    assert _used(_recall(store, level=level).recall(QUERY)) != _used(off)


def test_the_breaker_level_comes_from_the_engine_enum(tmp_path):
    store = _swap_store(tmp_path)
    engine = StiraEngine(state=NeuralState(stability=0.2))
    recall = _recall(store)
    recall.breaker_level = lambda: engine.breaker_status().level
    assert recall.recall(QUERY).proximity_gate == ar.G_BREAKER
    healthy = StiraEngine(state=NeuralState())
    recall.breaker_level = lambda: healthy.breaker_status().level
    assert recall.recall(QUERY).proximity_gate == ar.G_ON


def test_no_negative_to_negative_lift(tmp_path, monkeypatch):
    low_mood = {**BASELINES, **{k: NEGATIVE[k] for k in ee.MOOD_DIMS}}
    assert ee.polarity(low_mood) == ee.NEGATIVE == ee.polarity(NEGATIVE)
    store = _swap_store(tmp_path, d_state=NEGATIVE, e_state=NEGATIVE)
    off = _recall(store, proximity=False, state=low_mood).recall(QUERY)
    on = _recall(store, state=low_mood).recall(QUERY)
    assert on.proximity_gate == ar.G_ON
    assert _used(on) == _used(off)
    assert {c.key: c.proximity for c in on.used + on.dropped}["d"] == 0.0
    # The same negative episodes are lifted when the current mood is not negative...
    neutral_close = {**BASELINES, **{k: NEGATIVE[k] + 0.0 for k in ee.MOOD_DIMS}}
    assert ee.mood_similarity(neutral_close, NEGATIVE) > 0.9
    # ...and the guard is what stops it: mutation check with polarity disabled.
    monkeypatch.setattr(ee, "polarity", lambda state: ee.NEUTRAL)
    assert _used(_recall(store, state=low_mood).recall(QUERY)) != _used(off)


def test_the_holdout_is_deterministic_and_about_one_turn_in_ten(tmp_path, monkeypatch):
    turns = range(1, 10_001)
    first = [proximity_holdout(n) for n in turns]
    assert first == [proximity_holdout(n) for n in turns]
    assert 0.09 <= sum(first) / len(first) <= 0.11
    assert [n for n in range(1, 60) if proximity_holdout(n)] == [11, 46]
    store = _swap_store(tmp_path)
    off = _recall(store, proximity=False).recall(QUERY)
    held = _recall(store, turn=11).recall(QUERY)
    assert held.proximity_gate == ar.G_HOLDOUT and _used(held) == _used(off)
    assert _recall(store, turn=12).recall(QUERY).proximity_gate == ar.G_ON
    # Mutation check: without the holdout turn 11 is a proximity turn.
    monkeypatch.setattr(ar, "proximity_holdout", lambda turn: False)
    assert _recall(store, turn=11).recall(QUERY).proximity_gate == ar.G_ON


@pytest.mark.parametrize("broken", ["no_providers", "raises", "bad_turn"])
def test_a_failing_gate_turns_proximity_off_not_the_recall(tmp_path, caplog, broken):
    store = _swap_store(tmp_path)
    recall = _recall(store)
    if broken == "no_providers":
        recall.breaker_level = None
    elif broken == "raises":

        def boom():
            raise RuntimeError(f"secret {CLOSE['trust']}")

        recall.breaker_level = boom
    else:
        recall.turn_number = lambda: "eleven"
    with caplog.at_level(logging.WARNING):
        result = recall.recall(QUERY)
    assert result.proximity_gate == ar.G_ERROR and result.status == ar.S_RECALLED
    assert _used(result) == _used(_recall(store, proximity=False).recall(QUERY))
    assert "0.517" not in caplog.text


# --- the stored side: protected reader only; v1 / missing / incomplete count 0 ----------


def test_v1_missing_mismatched_and_incomplete_snapshots_contribute_nothing(tmp_path):
    store = _store(tmp_path)
    _episode(store, "v1", "Zucchini harvest v1.", CLOSE, mtime=1_000, v1=True)
    _episode(store, "missing", "Zucchini harvest missing.", CLOSE, mtime=1_001)
    os.unlink(ee.emotion_record_path(store, "missing"))
    _episode(store, "mismatch", "Zucchini harvest mismatch.", CLOSE, mtime=1_002)
    path = ee.emotion_record_path(store, "mismatch")
    record = json.loads(path.read_text())
    record["content_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    partial = {**CLOSE, "vigilance": None}
    _episode(store, "partial", "Zucchini harvest partial.", partial, mtime=1_003)
    # All four mood values known, trust missing: still incomplete (all six are required).
    no_trust = {**CLOSE, "trust": None}
    _episode(store, "no-trust", "Zucchini harvest no trust.", no_trust, mtime=1_004)
    store.search("refresh-the-index-order")
    settings = RecallSettings(enabled=True, emotion_proximity=True, max_items=5, max_chars=2000)
    on = _recall(store, settings=settings).recall("zucchini harvest")
    off = _recall(store, settings=RecallSettings(enabled=True, max_items=5, max_chars=2000))
    assert _used(on) == _used(off.recall("zucchini harvest"))  # never excluded
    assert set(_used(on)) == {"v1", "missing", "mismatch", "partial", "no-trust"}
    for candidate in on.used:
        assert candidate.proximity is None and candidate.proximity_term == 0.0
    assert all(row["proximity"] is None for row in on.as_event_data()["used"])


def test_snapshots_are_read_only_through_the_protected_reader(tmp_path, monkeypatch):
    store = _swap_store(tmp_path)
    calls = []
    real = ee.read_emotion_snapshot

    def spy(store_, entry):
        calls.append(entry.key)
        return real(store_, entry)

    monkeypatch.setattr(ee, "read_emotion_snapshot", spy)
    result = _recall(store).recall(QUERY)
    assert set(calls) == {"a", "b", "c", "d", "e"}  # stage 2 only, the protected reader
    assert len(calls) <= ar.MAX_CANDIDATES
    assert result.proximity_swaps == 1
    # A forged record (body rewritten after capture) reads as mismatch: no lift.
    store.write(MemoryLayer.EPISODIC, "d", "Zucchini notes d!", {"format": ee.FORMAT_V2})
    record_origin(store, MemoryLayer.EPISODIC, "d", ORIGIN_EXTRACTED, "web-chat")
    forged = _recall(store).recall(QUERY)
    assert next(c for c in forged.used + forged.dropped if c.key == "d").proximity is None


def test_the_bond_term_is_not_replaced_in_proximity_mode(tmp_path):
    store = _store(tmp_path)
    _episode(store, "warm", "Thank you, zucchini harvest.", FAR, mtime=1_000)
    store.search("refresh-the-index-order")
    state = {**CURRENT, "trust": 0.9, "attachment": 0.9}
    off = _recall(store, proximity=False, state=state).recall("zucchini harvest").used[0]
    on = _recall(store, state=state).recall("zucchini harvest").used[0]
    assert on.bond == off.bond and on.score_off == off.score
    assert ar.R_STORED_EMOTION not in on.reasons


# --- privacy: no stored value in the event, the prompt, events.db or logs ----------------


def _event(data):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "c" * 32,
        "turn_id": "t1",
        "seq": 1,
        "ts": time.time(),
        "type": "memory.injected",
        "data": data,
    }


def test_the_event_carries_scores_and_codes_but_no_stored_value(tmp_path):
    store = _swap_store(tmp_path)
    data = _recall(store).recall(QUERY).as_event_data()
    validate_event(_event(copy.deepcopy(data)))
    assert data["schema"] == 3 and data["proximity_gate"] == "on"
    text = json.dumps(data)
    for leak in LEAKS:
        assert leak not in text
    scores = [row["proximity"] for row in data["used"] + data["dropped"]]
    assert all(s is None or round(s, 3) == s for s in scores) and any(s for s in scores)


def _prox_mutations():
    def put(path, value):
        def apply(data):
            target = data
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value

        return apply

    return {
        "stored value field": put(("used", 0, "stored_state"), CLOSE),
        "distance field": put(("used", 0, "distance"), 0.012),
        "polarity field": put(("used", 0, "polarity"), "negative"),
        "raw proximity": put(("used", 0, "proximity"), 0.86123),
        "proximity above 1": put(("used", 0, "proximity"), 1.5),
        "text proximity": put(("used", 0, "proximity"), "near"),
        "unknown gate": put(("proximity_gate",), "mood"),
        "two swaps": put(("proximity_swaps",), 2),
        "current state": put(("current",), CURRENT),
        "missing gate": lambda data: data.pop("proximity_gate"),
        "proximity in schema 2": put(("schema",), 2),
    }


@pytest.mark.parametrize("name", sorted(_prox_mutations()))
def test_the_allow_list_rejects_values_in_the_proximity_event(tmp_path, name):
    data = _recall(_swap_store(tmp_path)).recall(QUERY).as_event_data()
    validate_event(_event(copy.deepcopy(data)))
    _prox_mutations()[name](data)
    with pytest.raises(ValueError):
        validate_event(_event(data))


def test_the_prompt_frame_is_unchanged_and_carries_no_value(tmp_path):
    store = _swap_store(tmp_path)
    off_recall, on_recall = _recall(store, proximity=False), _recall(store)
    texts = {}
    for name, recall in (("off", off_recall), ("on", on_recall)):
        recall.set_query(QUERY)
        texts[name] = _assembler(store, recall, episodic_limit=0).build_system_prompt()
    on_text = texts["on"]
    for leak in [*LEAKS, "proximity", "polarity", "R_EMOTION", "0.861"]:
        assert leak not in on_text
    header = on_text[on_text.index("## Associated Memory") :].splitlines()[:3]
    assert header == texts["off"][texts["off"].index("## Associated Memory") :].splitlines()[:3]


def _scan(root: Path, needles, skip_dir: Path) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if not path.is_file() or skip_dir in path.parents:
            continue
        data = path.read_bytes()
        hits += [f"{path.name}:{n}" for n in needles if n.encode() in data]
    return hits


def test_a_server_turn_keeps_values_out_of_events_db_the_prompt_and_logs(
    tmp_path, monkeypatch, caplog
):
    """End to end: proximity on, snapshot capture on, two chat turns. The stored values stay
    in memory/emotion/; turns.db has the schema-3 recall event; events.db (the audit chain)
    has no value or proximity score; the prompt has none."""
    monkeypatch.delenv(ar.PROXIMITY_ENV_SWITCH, raising=False)
    monkeypatch.delenv(ar.ENV_SWITCH, raising=False)
    server = _load_server("stira_server_proximity_e2e")
    captured, prompts = {}, []

    class _FakeAgent:
        def __init__(self, *, state, config, on_breaker=None, tools=(), **_):
            for dim, value in LIVE.items():  # the current state (persisted as usual)
                setattr(state, dim, value)
            self.engine = StiraEngine(state=state, config=config)
            self.tools = tools
            self.state = state

        @property
        def last_turn_summary(self):
            return None

        def __call__(self, message):
            parts = self.prompt_parts_provider()
            prompts.append(json.dumps([str(p) for p in vars(parts).values()], ensure_ascii=False))
            return "了解です。"

    real_blueprint = server.create_chat_blueprint

    def _capture(agent, **kwargs):
        captured.update(kwargs)
        return real_blueprint(agent, **kwargs)

    data_dir = tmp_path / "stira"
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(server, "NXPAgent", _FakeAgent)
    monkeypatch.setattr(server, "create_chat_blueprint", _capture)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = {"associative_recall": {"enabled": True, "emotion_proximity": True}}
    app = server.create_app(_config_store(config_dir, config))
    try:
        store = MemoryStore(data_dir / "memory", "stira")
        _episode(store, "chat-old", "マスター: ズッキーニの収穫を話した。\nリゾルバ: メモ。", CLOSE,
                 mtime=1_000)  # fmt: skip
        for index in range(6):
            _episode(store, f"chat-small-{index}", f"マスター: 雑談{index}。\nリゾルバ: はい。",
                     FAR, mtime=1_001 + index)  # fmt: skip
        store.search("refresh-the-index-order")
        recorder = captured["turn_recorder"]
        with caplog.at_level(logging.DEBUG):
            for turn in ("turn-prox-1", "turn-prox-2"):
                recorder.active.set(turn)
                assert captured["runner"]("ズッキーニの収穫はどうなった？") == "了解です。"
        injected = [
            e["data"]
            for e in recorder.store.events("turn-prox-1")
            if e["type"] == "memory.injected"
        ]
        assert len(injected) == 1 and injected[0]["schema"] == 3
        assert injected[0]["used"][0]["key"] == "chat-old"
        assert injected[0]["used"][0]["proximity"] == pytest.approx(1.0, abs=0.06)
        # The values are in the protected records only (the episode written by the turn too).
        protected = Path(store.memory_dir) / ee.EMOTION_DIRNAME
        assert len(list(protected.glob("*.json"))) >= 8
        needles = [f"{v}" for v in CLOSE.values()] + [f"{v}" for v in FAR.values()]
        assert _scan(data_dir, needles, protected) == []
        events_db = data_dir / "events.db"
        assert events_db.exists()  # the audit chain of this server
        raw = b"".join(p.read_bytes() for p in data_dir.glob("events.db*"))
        assert b"proximity" not in raw and b"memory.injected" not in raw
        for leak in needles:
            assert all(leak not in prompt for prompt in prompts)
            assert leak not in caplog.text
        assert "ズッキーニの収穫" in prompts[0]  # the body itself is offered
        # Sanity of the scan itself (it finds a planted value).
        (data_dir / "planted.txt").write_text(needles[0])
        assert _scan(data_dir, needles, protected)
    finally:
        app.extensions["lifecycle"].shutdown()


# --- read-only: Neural State, matrix, anchors, files ------------------------------------


def _fingerprint(store, engine, anchors):
    files = sorted(
        (str(p), p.stat().st_mtime_ns, p.read_bytes())
        for p in Path(store.memory_dir).rglob("*")
        if p.is_file() and not p.name.endswith((".sqlite3-wal", ".sqlite3-shm"))
        and p.name != "index.sqlite3"
    )  # fmt: skip
    return (
        engine.state.to_dict(),
        copy.deepcopy(EMOTION_MATRIX),
        list(anchors),
        engine.config.stability_warning,
        files,
    )


def test_proximity_reads_neural_state_and_writes_nothing(tmp_path):
    store = _swap_store(tmp_path)
    engine = StiraEngine(state=NeuralState(**{d: CURRENT[d] for d in DIMS}))
    anchors = list(engine.config.identity_anchors) or ["Keep one identity"]

    def state_view():
        with engine.state_lock:
            return {d: getattr(engine.state, d) for d in DIMS}

    recall = AssociativeRecall(
        store,
        state_view,
        RecallSettings(enabled=True, emotion_proximity=True),
        identity_anchors=anchors,
        breaker_level=lambda: engine.breaker_status().level,
        turn_number=lambda: 1,
    )
    before = _fingerprint(store, engine, anchors)
    recall.set_query(QUERY)
    assert recall.section()
    assert recall.take_result().proximity_swaps == 1
    assert _fingerprint(store, engine, anchors) == before

    # Mutation check: a signal that nudges the state is caught by the same comparison.
    @dataclass(frozen=True)
    class _Writer(EmotionProximitySignal):
        def term(self, candidate, context):
            engine.state.stability = 0.69
            return 0.0

    bad = AssociativeRecall(
        store, state_view, RecallSettings(enabled=True, emotion_proximity=True),
        signals=(*ar.DEFAULT_SIGNALS, _Writer()), breaker_level=lambda: "normal",
        turn_number=lambda: 1,
    )  # fmt: skip
    bad.recall(QUERY)
    assert _fingerprint(store, engine, anchors) != before


# --- raw stored values (Q11) ----------------------------------------------------------------


def test_snapshot_values_are_stored_raw(tmp_path):
    store = _store(tmp_path)
    _episode(store, "raw", "Zucchini raw.", CLOSE, mtime=1_000)
    record = json.loads(ee.emotion_record_path(store, "raw").read_text())
    assert record["state"] == CLOSE  # 5 decimals kept: no rounding, no bucketing
    snapshot = ee.read_emotion_snapshot(store, store.read(MemoryLayer.EPISODIC, "raw"))
    assert dict(snapshot.state) == CLOSE


# --- latency on the existing fixture ----------------------------------------------------


def test_latency_with_proximity_on_the_300_entry_fixture_stays_under_50_ms(tmp_path):
    from tests.test_associative_recall_signals import _hundreds

    store = _hundreds(tmp_path, 300)
    rng = random.Random(3)
    for entry in store.list_entries(MemoryLayer.EPISODIC, 300):
        store.write(MemoryLayer.EPISODIC, entry.key, entry.content,
                    {**entry.metadata, "format": ee.FORMAT_V2})  # fmt: skip
        state = {d: rng.random() for d in DIMS}
        ee.write_emotion_snapshot(
            store,
            store.read(MemoryLayer.EPISODIC, entry.key),
            ee.normalize_snapshot({"state": state}),
        )
    store.search("refresh-the-index-order")
    settings = RecallSettings(enabled=True, window=300, emotion_proximity=True)
    recall = _recall(store, settings=settings, turn=12)
    query = "How is the robot note7?"
    recall.recall(query)
    timings = []
    for _ in range(9):
        started = time.perf_counter()
        result = recall.recall(query)
        timings.append(time.perf_counter() - started)
    assert result.proximity_gate == ar.G_ON
    median = statistics.median(timings)
    print(f"associative recall + emotion proximity, 300 entries: median {median * 1000:.1f} ms")
    assert median < 0.05, timings


# --- the pre-registered offline evaluation (docs/eval/emotion-proximity-prereg.md) -------


def _eval_module():
    path = ROOT / "scripts" / "eval_emotion_proximity.py"
    spec = importlib.util.spec_from_file_location("eval_emotion_proximity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_preregistered_criteria_are_met_on_both_fixture_versions():
    module = _eval_module()
    result = module.evaluate()  # deterministic: the exact numbers below are asserted
    for version in ("v1", "v2_hard"):
        assert all(result[version]["met"].values()), (version, result[version]["met"])
    v1, hard = result["v1"], result["v2_hard"]
    assert v1["episodes"] == 60 and v1["v1_episodes"] == 15 and v1["pairs"] == 120
    # Recorded numbers (PR body, docs): version 1 changes nothing (see the prereg deviation).
    assert v1["changed_pairs"] == 0 and v1["deltas"]["precision_at_3"] == 0.0
    assert hard["changed_pairs"] == hard["swap_turns"] == 10
    assert hard["deltas"] == {
        "precision_at_3": -0.008, "intrusion": 0.009, "congruence": 0.0, "v1_share": -0.024,
    }  # fmt: skip
    assert hard["diversity"]["relative_delta"] == 0.077
    assert hard["loop_stress"]["off"]["median"] == hard["loop_stress"]["on"]["median"] == 16
    text = json.dumps(result, ensure_ascii=False)
    assert "zoo" not in text and "カレー" not in text  # keys and counts only
