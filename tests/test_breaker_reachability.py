"""Measured reachability regressions for the current default breaker policy."""

from __future__ import annotations

from copy import deepcopy

import pytest

from stiracore.core.breaker_sim import FIXED_EPOCH, simulate
from stiracore.core.engine import BreakLevel, StiraConfig
from stiracore.core.neural_state import EMOTION_MATRIX

INTERVALS = (0.0, 0.5, 1.0)
NEGATIVE_STIMULI = ("threat", "loss", "uncertainty")
POSITIVE_STIMULI = ("joy", "achievement", "connection")


def test_clock_injection_is_deterministic_and_configuration_is_not_mutated():
    stimuli = [
        ("loss", 1.0, 0.0),
        ("threat", 2.0, 0.5),
        ("uncertainty", 1.0, 1.0),
        (None, 0.0, 24.0),
    ] * 8
    original_stimuli = list(stimuli)
    config = StiraConfig()
    original_config = deepcopy(config)
    calls = []

    def clock():
        calls.append(FIXED_EPOCH)
        return FIXED_EPOCH

    first = simulate(stimuli, config, now_fn=clock)
    second = simulate(stimuli, config, now_fn=clock)

    assert calls == [FIXED_EPOCH, FIXED_EPOCH]
    assert first == second
    assert first == simulate(stimuli, config)
    assert first == simulate(stimuli, config, now_fn=lambda: FIXED_EPOCH + 86400)
    assert config == original_config
    assert stimuli == original_stimuli


def test_no_filesystem_checkpoint_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = simulate([("loss", 1.0, 0.0)] * 20)

    assert result.first_reached[BreakLevel.CRITICAL] is not None
    assert list(tmp_path.iterdir()) == []


def test_empty_experiment_has_no_visits():
    result = simulate([], now_fn=lambda: FIXED_EPOCH)

    assert result.turns == ()
    assert result.first_reached == dict.fromkeys(BreakLevel)


def test_elapsed_time_is_applied_before_the_next_stimulus():
    immediate = simulate([("loss", 1.0, 0.0), ("loss", 1.0, 0.0)])
    delayed = simulate([("loss", 1.0, 0.0), ("loss", 1.0, 1.0)])

    assert immediate.turns[0].post.stability == 0.67
    assert immediate.turns[1].pre.stability == 0.67
    assert delayed.turns[1].pre.stability == 0.6725
    assert delayed.turns[1].post.stability == 0.6425
    assert delayed.turns[1].post.bc_score == delayed.turns[1].pre.bc_score


@pytest.mark.parametrize("stimulus", sorted(EMOTION_MATRIX))
@pytest.mark.parametrize("hours", INTERVALS)
def test_prefixes_are_reproducible_and_first_visits_are_monotone(stimulus, hours):
    schedule = [(stimulus, 1.0, hours)] * 60
    full = simulate(schedule)
    assert full == simulate(schedule)

    previous_hits = dict.fromkeys(BreakLevel)
    for length in range(1, 61):
        prefix = simulate(schedule[:length])
        assert prefix.turns == full.turns[:length]

        # Derive first visits from the actual boundary observations rather than
        # trusting the simulator's aggregate or assuming an ordering of levels.
        observed = dict.fromkeys(BreakLevel)
        for sample in prefix.turns:
            for boundary in (sample.pre, sample.post):
                if observed[boundary.level] is None:
                    observed[boundary.level] = sample.turn
        assert prefix.first_reached == observed

        for level in BreakLevel:
            full_hit = full.first_reached[level]
            expected = full_hit if full_hit is not None and full_hit <= length else None
            assert prefix.first_reached[level] == expected

            # Extending a sequence cannot undo or move an already observed hit.
            if previous_hits[level] is not None:
                assert prefix.first_reached[level] == previous_hits[level]
        previous_hits = prefix.first_reached


@pytest.mark.parametrize(
    ("stimulus", "warning_turn", "critical_turn"),
    [
        ("threat", 11, 21),
        ("loss", 7, 14),
        ("uncertainty", 21, 41),
    ],
)
def test_zero_interval_first_arrivals(stimulus, warning_turn, critical_turn):
    result = simulate([(stimulus, 1.0, 0.0)] * 60)

    assert result.first_reached[BreakLevel.WARNING] == warning_turn
    assert result.first_reached[BreakLevel.CRITICAL] == critical_turn
    assert warning_turn < critical_turn


@pytest.mark.parametrize("hours", INTERVALS)
def test_default_reachability_matrix_is_measured(hours):
    # This also fails if EMOTION_MATRIX gains a key without a reviewed baseline.
    assert set(EMOTION_MATRIX) == set(NEGATIVE_STIMULI + POSITIVE_STIMULI)

    for stimulus in NEGATIVE_STIMULI:
        result = simulate([(stimulus, 1.0, hours)] * 60)
        warning = result.first_reached[BreakLevel.WARNING]
        critical = result.first_reached[BreakLevel.CRITICAL]

        assert warning is not None, (stimulus, hours, result)
        assert critical is not None, (stimulus, hours, result)
        assert warning < critical
        assert result.first_reached[BreakLevel.COHERENCE] is None
        assert all(
            observation.level != BreakLevel.COHERENCE
            for sample in result.turns
            for observation in (sample.pre, sample.post)
        )

    for stimulus in POSITIVE_STIMULI:
        result = simulate([(stimulus, 1.0, hours)] * 60)

        assert result.first_reached[BreakLevel.WARNING] is None
        assert result.first_reached[BreakLevel.CRITICAL] is None
        # Increasing stability can also produce enough variance for COHERENCE.
        assert result.first_reached[BreakLevel.COHERENCE] is not None
        assert min(sample.post.stability for sample in result.turns) >= 0.7
        assert min(sample.pre.bc_score for sample in result.turns) < 0.6


@pytest.mark.parametrize("hours", INTERVALS)
def test_unreached_coherence_is_not_synonymous_with_high_bc(hours):
    threat = simulate([("threat", 1.0, hours)] * 60)
    uncertainty = simulate([("uncertainty", 1.0, hours)] * 60)

    # Threat crosses the BC threshold, but higher-priority levels mask it.
    assert min(sample.pre.bc_score for sample in threat.turns) < 0.6
    assert threat.first_reached[BreakLevel.COHERENCE] is None

    # Uncertainty does not cross the BC entry threshold in this experiment.
    assert min(sample.pre.bc_score for sample in uncertainty.turns) >= 0.6
    assert uncertainty.first_reached[BreakLevel.COHERENCE] is None


def test_warning_hysteresis_uses_strict_exit_threshold():
    result = simulate([("threat", 1.0, 0.0)] * 11 + [("joy", 1.0, 0.0)] * 7)

    assert result.turns[10].post.stability == 0.48
    assert result.turns[10].post.level == BreakLevel.WARNING

    assert result.turns[11].post.stability == 0.5
    assert result.turns[11].post.level == BreakLevel.WARNING

    assert result.turns[16].post.stability == 0.6
    assert result.turns[16].post.level == BreakLevel.WARNING

    assert result.turns[17].post.stability == 0.62
    assert result.turns[17].post.level != BreakLevel.WARNING


def test_coherence_hysteresis_survives_recovery_above_entry_threshold():
    result = simulate([("achievement", 1.0, 0.0)] * 10 + [(None, 0.0, 0.0)] * 40)
    assert result.first_reached[BreakLevel.COHERENCE] is not None

    recovering_band = [
        current
        for previous, current in zip(result.turns, result.turns[1:])
        if current.pre.bc_score > previous.pre.bc_score and 0.6 < current.pre.bc_score <= 0.7
    ]
    assert recovering_band
    assert all(sample.pre.level == BreakLevel.COHERENCE for sample in recovering_band)
    assert result.turns[-1].pre.bc_score == 1.0
    assert result.turns[-1].pre.level == BreakLevel.NORMAL


def test_critical_cooldown_counts_pre_turn_attempts_not_post_turn_entry():
    # Large, API-supported intensities isolate cooldown from slow recovery.
    # The representative reachability matrix above always uses intensity 1.
    result = simulate(
        [
            ("loss", 14.0, 0.0),
            ("joy", 20.0, 0.0),
            (None, 0.0, 0.0),
            (None, 0.0, 0.0),
        ]
    )

    assert result.first_reached[BreakLevel.CRITICAL] == 1
    assert result.turns[0].post.stability == 0.28
    assert result.turns[1].post.stability == 0.68

    assert [sample.pre.level for sample in result.turns] == [
        BreakLevel.NORMAL,
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
    ]
    assert [sample.post.level for sample in result.turns] == [
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
        BreakLevel.NORMAL,
    ]

    longer = simulate(
        [
            ("loss", 14.0, 0.0),
            ("joy", 20.0, 0.0),
            (None, 0.0, 0.0),
            (None, 0.0, 0.0),
        ],
        config=StiraConfig(critical_cooldown_turns=4),
    )
    assert longer.turns[-1].post.level == BreakLevel.CRITICAL


@pytest.mark.parametrize("hours", [-1.0, float("nan"), float("inf"), True])
def test_invalid_elapsed_time_is_rejected(hours):
    with pytest.raises(ValueError):
        simulate([("loss", 1.0, hours)])


@pytest.mark.parametrize("intensity", [float("nan"), float("inf"), True])
def test_invalid_intensity_is_rejected(intensity):
    with pytest.raises(ValueError):
        simulate([("loss", intensity, 0.0)])


def test_unknown_stimulus_is_not_silently_ignored():
    with pytest.raises(ValueError, match="Unknown stimulus"):
        simulate([("typo", 1.0, 0.0)])
