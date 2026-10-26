import json
import logging
import os
from pathlib import Path

import pytest

from stiracore.core import EMOTION_MATRIX, NeuralState

DIMENSIONS = (
    "trust",
    "curiosity",
    "vigilance",
    "attachment",
    "stability",
    "satisfaction",
)

DEFAULTS = {
    "trust": 0.5,
    "curiosity": 0.5,
    "vigilance": 0.1,
    "attachment": 0.5,
    "stability": 0.7,
    "satisfaction": 0.5,
}


def _values(state):
    return {name: getattr(state, name) for name in DIMENSIONS}


def _prime_decay(state, clock):
    """Establish a decay timestamp without depending on private fields."""
    clock.advance(1.0)
    state.apply_decay(now_fn=clock, min_interval_sec=0.0)


def _schema_key(payload):
    """Locate the schema marker using the writer's serialization convention."""
    keys = [key for key in payload if key.replace("_", "").lower() == "schemaversion"]
    assert len(keys) == 1, "Saved state must include one schema-version marker"
    return keys[0]


def _saved_payload(state, path):
    state.save(path)
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_values(default_state):
    assert _values(default_state) == pytest.approx(DEFAULTS)


def test_apply_stimulus_known(default_state, fake_clock, known_stimulus):
    before = _values(default_state)

    result = default_state.apply_stimulus(
        known_stimulus,
        1.0,
        now_fn=fake_clock,
    )

    assert isinstance(result, dict)
    assert _values(default_state) != before
    assert all(0.0 <= value <= 1.0 for value in _values(default_state).values())


# Independent contract in DIMENSIONS order: values are per unit intensity.
EXPECTED_STIMULUS_DELTAS = {
    "joy": (0.02, 0.03, -0.02, 0.02, 0.02, 0.05),
    "threat": (-0.01, 0.00, 0.08, 0.01, -0.02, -0.03),
    "achievement": (0.01, 0.02, -0.03, 0.01, 0.03, 0.06),
    "loss": (-0.01, -0.01, 0.03, 0.04, -0.03, -0.04),
    "uncertainty": (0.00, 0.04, 0.04, 0.00, -0.01, -0.01),
    "connection": (0.03, 0.01, -0.05, 0.06, 0.02, 0.03),
}


def test_stimulus_contract_covers_all_stimuli():
    assert set(EXPECTED_STIMULUS_DELTAS) == set(EMOTION_MATRIX)
    assert all(len(row) == len(DIMENSIONS) for row in EMOTION_MATRIX.values())


@pytest.mark.parametrize("dimension_index,dimension", list(enumerate(DIMENSIONS)))
@pytest.mark.parametrize("stimulus,expected_deltas", EXPECTED_STIMULUS_DELTAS.items())
@pytest.mark.parametrize("intensity", [0.5, 1.0])
def test_stimulus_direction_and_magnitude(
    fake_clock, stimulus, expected_deltas, dimension_index, dimension, intensity
):
    state = NeuralState(**dict.fromkeys(DIMENSIONS, 0.5))
    expected = expected_deltas[dimension_index] * intensity

    deltas = state.apply_stimulus(stimulus, intensity, now_fn=fake_clock)
    actual = getattr(state, dimension) - 0.5

    assert actual == pytest.approx(expected)
    if expected > 0.0:
        assert actual > 0.0
    elif expected < 0.0:
        assert actual < 0.0
    else:
        assert actual == 0.0
        assert dimension not in deltas
    assert deltas.get(dimension, 0.0) == pytest.approx(expected)


@pytest.mark.parametrize("stimulus", ["joy", "achievement", "connection"])
@pytest.mark.parametrize("initial", [0.0, 0.01, DEFAULTS["vigilance"], 1.0])
def test_reassurance_clamps_vigilance_at_zero(fake_clock, stimulus, initial):
    state = NeuralState(vigilance=initial)

    for _ in range(100):
        before = state.vigilance
        state.apply_stimulus(stimulus, now_fn=fake_clock)
        assert 0.0 <= state.vigilance <= before

    assert state.vigilance == 0.0


@pytest.mark.parametrize("stimulus", ["joy", "achievement", "connection"])
def test_reassurance_can_lower_vigilance_below_decay_baseline(fake_clock, stimulus):
    state = NeuralState()

    state.apply_stimulus(stimulus, now_fn=fake_clock)

    # The decay target is not a stimulus floor; only zero is a hard lower bound.
    assert 0.0 <= state.vigilance < DEFAULTS["vigilance"]


def test_threat_then_connection_raises_then_lowers_vigilance(fake_clock):
    state = NeuralState(last_decay_check=fake_clock())
    baseline = state.vigilance

    threat = state.apply_stimulus("threat", now_fn=fake_clock)
    raised = state.vigilance
    connection = state.apply_stimulus("connection", now_fn=fake_clock)

    assert raised == pytest.approx(baseline + 0.08)
    assert state.vigilance == pytest.approx(raised - 0.05)
    assert baseline < state.vigilance < raised
    assert threat["vigilance"] == pytest.approx(0.08)
    assert connection["vigilance"] == pytest.approx(-0.05)
    assert state.last_decay_check == fake_clock()


@pytest.mark.parametrize(
    "stimulus,drop", [("joy", 0.02), ("achievement", 0.03), ("connection", 0.05)]
)
def test_reassurance_and_decay_are_independent(fake_clock, stimulus, drop):
    start = fake_clock()
    reassured = NeuralState(vigilance=0.5, last_decay_check=start, last_stimulus_time=start)
    idle = NeuralState(vigilance=0.5, last_decay_check=start, last_stimulus_time=start)

    # Events work with no elapsed time; decay does not.
    reassured.apply_stimulus(stimulus, now_fn=fake_clock)
    assert reassured.vigilance == pytest.approx(0.5 - drop)
    assert reassured.apply_decay(now_fn=fake_clock) == {}
    assert idle.apply_decay(now_fn=fake_clock) == {}
    assert idle.vigilance == 0.5

    fake_clock.advance(3600.0)
    # Elapsed time alone does not get folded into apply_stimulus.
    reassured.apply_stimulus(stimulus, now_fn=fake_clock)
    assert reassured.vigilance == pytest.approx(0.5 - 2 * drop)
    assert reassured.last_decay_check == start

    for state in (reassured, idle):
        deltas = state.apply_decay(now_fn=fake_clock)
        assert deltas["vigilance"] == pytest.approx(-0.015)
        assert state.last_decay_check == fake_clock()
    assert reassured.vigilance == pytest.approx(0.5 - 2 * drop - 0.015)
    assert idle.vigilance == pytest.approx(0.485)
    assert idle.last_stimulus_time == start


@pytest.mark.parametrize("initial", [0.0, 0.05, 0.1, 0.5, 1.0])
def test_vigilance_decay_reaches_baseline_without_overshooting(fake_clock, initial):
    state = NeuralState(vigilance=initial, last_decay_check=fake_clock())
    baseline = DEFAULTS["vigilance"]

    fake_clock.advance(3600.0)
    state.apply_decay(now_fn=fake_clock)
    assert min(initial, baseline) <= state.vigilance <= max(initial, baseline)

    fake_clock.advance(3600.0 * 1000)
    state.apply_decay(now_fn=fake_clock)
    assert state.vigilance == baseline


def test_apply_stimulus_unknown_warns(
    default_state,
    fake_clock,
    caplog,
    recwarn,
):
    before = _values(default_state)
    unknown = "__not_a_registered_stimulus__"

    with caplog.at_level(logging.WARNING):
        default_state.apply_stimulus(unknown, 1.0, now_fn=fake_clock)

    messages = [record.getMessage() for record in caplog.records]
    messages.extend(str(warning.message) for warning in recwarn)
    assert any(unknown in message for message in messages)
    assert _values(default_state) == before


def test_apply_stimulus_clamping(default_state, fake_clock, known_stimulus):
    # Repeated valid intensities exercise saturation without relying on
    # acceptance of intensities outside the documented unit interval.
    for _ in range(200):
        default_state.apply_stimulus(
            known_stimulus,
            1.0,
            now_fn=fake_clock,
        )
        assert all(0.0 <= value <= 1.0 for value in _values(default_state).values())

    assert any(value == 0.0 or value == 1.0 for value in _values(default_state).values())


def test_apply_stimulus_zero_intensity(
    default_state,
    fake_clock,
    known_stimulus,
):
    before = _values(default_state)

    default_state.apply_stimulus(known_stimulus, 0.0, now_fn=fake_clock)

    assert _values(default_state) == before


def test_decay_toward_baseline(default_state, fake_clock):
    _prime_decay(default_state, fake_clock)
    for name in DIMENSIONS:
        setattr(default_state, name, 0.95)
    before = _values(default_state)

    fake_clock.advance(3600.0)
    result = default_state.apply_decay(
        now_fn=fake_clock,
        min_interval_sec=0.0,
    )

    assert isinstance(result, dict)
    for name, baseline in DEFAULTS.items():
        after = getattr(default_state, name)
        assert baseline <= after < before[name]
        assert abs(after - baseline) < abs(before[name] - baseline)


def test_decay_recovery_slower(fake_clock):
    recovering = NeuralState()
    relaxing = NeuralState()
    # Prime both with same clock
    recovering.last_decay_check = fake_clock()
    relaxing.last_decay_check = fake_clock()
    recovering.trust = 0.3  # below baseline 0.5
    relaxing.trust = 0.7  # above baseline 0.5

    fake_clock.advance(3600.0)  # 1 hour for measurable decay
    recovering.apply_decay(now_fn=fake_clock, min_interval_sec=0.0)
    relaxing.apply_decay(now_fn=fake_clock, min_interval_sec=0.0)

    recovery = recovering.trust - 0.3
    relaxation = 0.7 - relaxing.trust
    assert 0.0 < recovery  # recovered toward baseline
    assert 0.0 < relaxation  # decayed toward baseline
    assert recovery < relaxation  # recovery is slower


def test_decay_min_interval(fake_clock):
    state = NeuralState()
    state.last_decay_check = fake_clock()
    state.trust = 0.95

    # 5 seconds: too short, no decay
    fake_clock.advance(5.0)
    state.apply_decay(now_fn=fake_clock, min_interval_sec=60.0)
    assert state.trust == pytest.approx(0.95)

    # 60 more seconds: now decay fires
    fake_clock.advance(7200.0)  # 2 hours for measurable decay
    state.apply_decay(now_fn=fake_clock, min_interval_sec=60.0)
    assert state.trust < 0.95


def test_decay_at_baseline_is_noop(default_state, fake_clock):
    _prime_decay(default_state, fake_clock)

    fake_clock.advance(3600.0)
    default_state.apply_decay(now_fn=fake_clock, min_interval_sec=0.0)

    assert _values(default_state) == pytest.approx(DEFAULTS)


def test_bc_window_update(default_state):
    for _ in range(12):
        score = default_state.update_bc_window(window_size=4)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


def test_bc_score_stable(default_state):
    scores = [default_state.update_bc_window(window_size=8) for _ in range(16)]

    assert scores[-1] == pytest.approx(1.0)
    assert scores[-1] == pytest.approx(scores[-2])


def test_bc_score_unstable(default_state):
    stable = NeuralState()
    for _ in range(20):
        stable_score = stable.update_bc_window(window_size=8)

    for index in range(20):
        value = float(index % 2)
        for name in DIMENSIONS:
            setattr(default_state, name, value)
        unstable_score = default_state.update_bc_window(window_size=8)

    assert 0.0 <= unstable_score < stable_score <= 1.0


def test_clamp_all(default_state):
    for index, name in enumerate(DIMENSIONS):
        setattr(default_state, name, -0.25 if index % 2 == 0 else 1.25)

    default_state.clamp_all()

    for index, name in enumerate(DIMENSIONS):
        assert getattr(default_state, name) == (0.0 if index % 2 == 0 else 1.0)


def test_snapshot(default_state):
    snapshot = default_state.snapshot()

    assert isinstance(snapshot, dict)
    for name, expected in DEFAULTS.items():
        assert snapshot[name] == pytest.approx(expected)

    default_state.trust = 0.9
    assert snapshot["trust"] == pytest.approx(DEFAULTS["trust"])

    snapshot["stability"] = 0.0
    assert default_state.stability == pytest.approx(DEFAULTS["stability"])


def test_save_load_roundtrip(tmp_path):
    state = NeuralState(
        trust=0.82,
        curiosity=0.31,
        vigilance=0.23,
        attachment=0.64,
        stability=0.76,
        satisfaction=0.91,
    )
    for _ in range(4):
        state.update_bc_window(window_size=4)
    path = tmp_path / "state.json"

    state.save(path)
    restored = NeuralState.load(path)

    assert restored is not state
    assert _values(restored) == pytest.approx(_values(state))
    assert restored.update_bc_window(window_size=4) == pytest.approx(
        state.update_bc_window(window_size=4)
    )


def test_save_atomic(default_state, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    default_state.save(path)
    previous_contents = path.read_bytes()
    real_replace = os.replace
    replacements = []

    def checked_replace(source, destination, **kwargs):
        source = Path(source)
        destination = Path(destination)
        assert source.resolve() != destination.resolve()
        assert source.parent.resolve() == destination.parent.resolve()
        assert destination.resolve() == path.resolve()
        assert destination.read_bytes() == previous_contents

        payload = json.loads(source.read_text(encoding="utf-8"))
        assert payload["trust"] == pytest.approx(0.91)
        replacements.append(source)
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", checked_replace)
    default_state.trust = 0.91
    default_state.save(path)

    assert len(replacements) == 1
    assert not replacements[0].exists()
    assert NeuralState.load(path).trust == pytest.approx(0.91)


def test_load_missing_file(tmp_path):
    path = tmp_path / "missing.json"
    # load() returns defaults for missing files (graceful degradation)
    result = NeuralState.load(path)
    assert result.stability == pytest.approx(0.7)
    assert not path.exists()


def test_load_v1_migration(default_state, tmp_path):
    path = tmp_path / "v1.json"
    current_payload = _saved_payload(default_state, path)
    version_key = _schema_key(current_payload)

    # A minimal v1 document has dimensions but no v2 decay/window metadata.
    payload = {name: current_payload[name] for name in DIMENSIONS}
    payload[version_key] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = NeuralState.load(path)

    assert _values(restored) == pytest.approx(DEFAULTS)
    assert 0.0 <= restored.update_bc_window() <= 1.0

    migrated_path = tmp_path / "migrated.json"
    migrated = _saved_payload(restored, migrated_path)
    assert migrated[_schema_key(migrated)] == 2


def test_load_invalid_schema_raises(default_state, tmp_path):
    path = tmp_path / "future.json"
    payload = _saved_payload(default_state, path)
    payload[_schema_key(payload)] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        NeuralState.load(path)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("invalid_value", [-0.1, 1.1])
def test_load_range_validation(
    default_state,
    tmp_path,
    dimension,
    invalid_value,
):
    path = tmp_path / "invalid-range.json"
    payload = _saved_payload(default_state, path)
    payload[dimension] = invalid_value
    path.write_text(json.dumps(payload), encoding="utf-8")

    # load() clamps out-of-range values instead of raising
    restored = NeuralState.load(path)
    value = getattr(restored, dimension)
    assert 0.0 <= value <= 1.0
