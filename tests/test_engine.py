import pytest

from stiracore.core import BreakAction, BreakLevel, NeuralState, StiraConfig

DIMENSIONS = (
    "trust",
    "curiosity",
    "vigilance",
    "attachment",
    "stability",
    "satisfaction",
)


def _values(state):
    return {name: getattr(state, name) for name in DIMENSIONS}


def test_normal_level(default_state, default_config, make_engine):
    engine = make_engine(state=default_state, config=default_config)

    result = engine.pre_turn()

    assert result.level == BreakLevel.NORMAL
    assert result.actions == BreakAction.NONE
    assert not engine.is_write_blocked()


def test_warning_level(make_engine):
    engine = make_engine(state=NeuralState(stability=0.4))

    result = engine.pre_turn()

    assert result.level == BreakLevel.WARNING


def test_critical_level(make_engine):
    engine = make_engine(state=NeuralState(stability=0.2))

    result = engine.pre_turn()

    assert result.level == BreakLevel.CRITICAL


def test_coherence_level(make_engine):
    state = NeuralState()
    for index in range(20):
        value = float(index % 2)
        for name in DIMENSIONS:
            setattr(state, name, value)
        score = state.update_bc_window(window_size=8)

    assert score < 0.99
    # The final sample has high stability, isolating coherence from the
    # stability breakers. A high threshold makes this test independent of
    # the exact volatility-to-coherence scaling.
    config = StiraConfig(bc_threshold=0.99, bc_exit_threshold=1.0)
    engine = make_engine(state=state, config=config)

    result = engine.pre_turn()

    assert result.level == BreakLevel.COHERENCE


def test_critical_blocks_writes(make_engine):
    blocked = frozenset({"write_file", "delete_file"})
    config = StiraConfig(
        critical_action="readonly",
        blocked_tool_names=blocked,
    )
    engine = make_engine(
        state=NeuralState(stability=0.2),
        config=config,
    )

    result = engine.pre_turn()

    assert result.actions & BreakAction.BLOCK_WRITES
    assert not result.actions & BreakAction.HALT
    assert engine.is_write_blocked()
    assert engine.blocked_tools() == set(blocked)


def test_critical_identity_injection(make_engine):
    anchors = [
        "Respect the user's stated intent.",
        "Preserve protected identity layers.",
    ]
    engine = make_engine(
        state=NeuralState(stability=0.2),
        config=StiraConfig(identity_anchors=anchors),
    )

    result = engine.pre_turn()
    injection = engine.identity_injection()

    assert result.actions & BreakAction.INJECT_IDENTITY
    assert isinstance(injection, str)
    for anchor in anchors:
        assert anchor in injection


def test_warning_requires_confirmation(make_engine):
    engine = make_engine(state=NeuralState(stability=0.4))

    result = engine.pre_turn()

    assert result.actions & BreakAction.REQUIRE_CONFIRMATION
    assert not result.actions & BreakAction.HALT


def test_layer3_blocked(make_engine):
    engine = make_engine(state=NeuralState(stability=0.4))
    engine.pre_turn()

    assert not engine.check_layer_boundary(target_layer=3, confirmed=False)


@pytest.mark.parametrize("layer", [3, 4, 5])
def test_layer_blocked_at_critical(make_engine, layer):
    engine = make_engine(state=NeuralState(stability=0.2))
    engine.pre_turn()

    assert not engine.check_layer_boundary(target_layer=layer, confirmed=False)
    assert not engine.check_layer_boundary(target_layer=layer, confirmed=True)


@pytest.mark.parametrize("layer", [1, 2])
def test_warning_layer12_needs_confirmation(make_engine, layer):
    engine = make_engine(state=NeuralState(stability=0.4))
    engine.pre_turn()
    # L1-L2 at WARNING: blocked without confirmation, allowed with
    assert not engine.check_layer_boundary(target_layer=layer, confirmed=False)
    assert engine.check_layer_boundary(target_layer=layer, confirmed=True)


@pytest.mark.parametrize("layer", [3, 4, 5])
def test_warning_layer3plus_always_blocked(make_engine, layer):
    engine = make_engine(state=NeuralState(stability=0.4))
    engine.pre_turn()
    # L3+ always blocked regardless of confirmation
    assert not engine.check_layer_boundary(target_layer=layer, confirmed=False)
    assert not engine.check_layer_boundary(target_layer=layer, confirmed=True)


def test_hysteresis_no_chatter(make_engine):
    state = NeuralState(stability=0.49)
    engine = make_engine(state=state)

    assert engine.pre_turn().level == BreakLevel.WARNING

    for stability in (0.51, 0.49, 0.55, 0.59):
        state.stability = stability
        assert engine.pre_turn().level == BreakLevel.WARNING

    state.stability = 0.61
    assert engine.pre_turn().level == BreakLevel.NORMAL

    state.stability = 0.55
    assert engine.pre_turn().level == BreakLevel.NORMAL


def test_critical_cooldown_3_turns(make_engine):
    state = NeuralState(stability=0.2)
    config = StiraConfig(critical_cooldown_turns=3)
    engine = make_engine(state=state, config=config)

    # Enter CRITICAL
    assert engine.pre_turn().level == BreakLevel.CRITICAL
    # Recover stability above exit threshold
    state.stability = 0.9

    # Cooldown: must stay CRITICAL for configured turns
    for i in range(config.critical_cooldown_turns - 1):
        result = engine.pre_turn()
        assert result.level == BreakLevel.CRITICAL, f"Turn {i + 1} should still be CRITICAL"

    # After cooldown, should recover
    # (exact turn count depends on implementation; verify recovery eventually)
    found_normal = False
    for _ in range(5):
        if engine.pre_turn().level == BreakLevel.NORMAL:
            found_normal = True
            break
    assert found_normal, "Should recover to NORMAL after cooldown"


def test_sycophancy_rolling_window(make_engine):
    engine = make_engine()

    # Long runs avoid coupling this test to an undocumented window size.
    for _ in range(256):
        high_agreement = engine.record_agreement(True)

    assert high_agreement == pytest.approx(1.0)

    for _ in range(256):
        low_agreement = engine.record_agreement(False)

    # Implementations may return only alerts, or also non-alert ratios.
    # Either way, old agreements must leave the rolling window.
    assert low_agreement is None or low_agreement == pytest.approx(0.0)

    for _ in range(256):
        high_agreement = engine.record_agreement(True)

    assert high_agreement == pytest.approx(1.0)


def test_sycophancy_threshold_alert(make_engine):
    engine = make_engine()
    alerts = []

    for _ in range(256):
        ratio = engine.record_agreement(True)
        if ratio is not None:
            assert 0.0 <= ratio <= 1.0
            alerts.append(ratio)

    assert alerts, "Sustained unanimous agreement must produce a ratio alert"
    assert alerts[-1] == pytest.approx(1.0)


def test_post_turn_stimulus(
    default_state,
    make_engine,
    fake_clock,
    known_stimulus,
):
    reference = NeuralState(**_values(default_state))
    engine = make_engine(state=default_state)
    before = _values(default_state)

    reference.apply_stimulus(known_stimulus, 0.5, now_fn=fake_clock)
    result = engine.post_turn(stimulus=known_stimulus, intensity=0.5)

    assert isinstance(result, dict)
    assert _values(default_state) != before
    assert _values(default_state) == pytest.approx(_values(reference))


def test_post_turn_without_stimulus(default_state, make_engine):
    engine = make_engine(state=default_state)
    before = _values(default_state)

    result = engine.post_turn(stimulus=None, intensity=0.0)

    assert isinstance(result, dict)
    assert _values(default_state) == before


def test_breaker_callback_fires_on_transition(make_engine):
    events = []

    def on_breaker(*args, **kwargs):
        events.append((args, kwargs))

    state = NeuralState(stability=0.7)
    engine = make_engine(state=state, on_breaker=on_breaker)
    assert engine.pre_turn().level == BreakLevel.NORMAL
    events.clear()

    state.stability = 0.4
    assert engine.pre_turn().level == BreakLevel.WARNING
    assert len(events) == 1

    assert engine.pre_turn().level == BreakLevel.WARNING
    assert len(events) == 1

    state.stability = 0.2
    assert engine.pre_turn().level == BreakLevel.CRITICAL
    assert len(events) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"stability_warning": 1.1},
        {"stability_critical": -0.1},
        {"stability_warning": 0.2, "stability_critical": 0.3},
        {"stability_warning_exit": 0.4},
        {"stability_critical_exit": 0.2},
        {"bc_threshold": -0.1},
        {"bc_threshold": 1.1},
        {"bc_exit_threshold": 0.5},
        {"critical_action": "not-a-supported-action"},
        {"critical_cooldown_turns": -1},
    ],
)
def test_config_validation_rejects_bad_values(overrides):
    with pytest.raises(ValueError):
        StiraConfig(**overrides)
