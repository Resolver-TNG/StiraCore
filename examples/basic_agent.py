"""Basic usage example: StiraCore engine with Neural State."""

from stiracore.core import BreakLevel, NeuralState, StiraConfig, StiraEngine


def on_breaker(level: BreakLevel, message: str) -> None:
    print(f"  [BREAKER] {message}")


def main():
    # Fake clock for deterministic demo
    clock = [1000.0]

    def now():
        return clock[0]

    state = NeuralState(
        trust=0.9,
        curiosity=0.8,
        vigilance=0.1,
        attachment=0.9,
        stability=0.85,
        satisfaction=0.7,
        last_decay_check=now(),
        last_stimulus_time=now(),
    )
    print(f"Initial: {state}")

    config = StiraConfig(
        stability_warning=0.5,
        stability_critical=0.3,
        bc_threshold=0.6,
        bc_window_size=20,
        sycophancy_window_size=15,
    )

    engine = StiraEngine(state=state, config=config, on_breaker=on_breaker, now_fn=now)

    # Simulate 5 agent turns
    stimuli = ["connection", "achievement", "uncertainty", "achievement", "joy"]
    for i, stim in enumerate(stimuli):
        clock[0] += 120  # 2 minutes per turn
        level = engine.pre_turn()
        print(f"\nTurn {i + 1}: breaker={level.value}, stimulus={stim}")

        result = engine.post_turn(stim, intensity=0.8)
        print(f"  stability={result['stability']:.3f} bc={result['bc_score']:.3f}")

        # Track agreement (simulate: agrees 80% of the time)
        agreed = i != 2  # disagree on turn 3
        score = engine.record_agreement(agreed)
        if score is not None:
            print(f"  sycophancy={score:.1%}")

    # Layer boundary tests
    print("\n--- Layer Boundary ---")
    print(f"  L2 modify: {'allowed' if engine.check_layer_boundary(2) else 'BLOCKED'}")
    print(f"  L3 modify: {'allowed' if engine.check_layer_boundary(3) else 'BLOCKED'}")

    # is_blocked check
    print(f"\n  is_blocked: {engine.is_blocked()}")

    # Snapshot
    print(f"\n  Snapshot: {state.snapshot()}")

    # Save/load round-trip
    state.save("/tmp/stira_test_state.json")
    loaded = NeuralState.load("/tmp/stira_test_state.json")
    print(f"\n  Loaded:  {loaded}")
    print(f"  Match:   {state.stability == loaded.stability}")


if __name__ == "__main__":
    main()
