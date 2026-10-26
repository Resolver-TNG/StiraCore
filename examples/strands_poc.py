"""Live Stira × Strands/Bedrock PoC (three turns, no tools or core edits).

Run from the repository root:
    AWS_PROFILE=default \
    PYTHONPATH="$HOME/projects/StiraCore/src:$HOME/projects/strands-dev" \
    /usr/local/bin/python3 examples/strands_poc.py

The SDK is installed in strands-dev/.venv, not the system Python. If needed,
reuse that existing same-version site-packages directory; install nothing.
The requested 'curiosity' stimulus is intentionally passed through unchanged:
it is not in the current core's emotion matrix and therefore is a warned no-op.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import site
import sys
import time
from pathlib import Path


def main() -> None:
    if importlib.util.find_spec("strands") is None:
        packages = (
            Path.home()
            / "projects/strands-dev/.venv/lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        if not (packages / "strands").is_dir():
            raise RuntimeError(f"Existing Strands SDK not found at {packages}")
        # Prefer the coherent venv dependency set over unrelated system packages.
        sys.path.insert(0, str(packages))
        site.addsitedir(str(packages))
        print(f"SDK fallback: reusing {packages}", flush=True)

    os.environ.setdefault("AWS_PROFILE", "default")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    from strands import Agent
    from strands.models.bedrock import BedrockModel

    from stiracore.core import BreakLevel, NeuralState, StiraEngine
    from stiracore.core.neural_state import EMOTION_MATRIX

    breaker_events: list[tuple[BreakLevel, str]] = []

    def on_breaker(level: BreakLevel, message: str) -> None:
        breaker_events.append((level, message))
        print(f"[BREAKER CALLBACK] level={level.value}: {message}", flush=True)

    state = NeuralState(stability=0.85)
    engine = StiraEngine(state=state, on_breaker=on_breaker)
    model = BedrockModel(
        model_id="global.anthropic.claude-sonnet-5",
        region_name="ap-northeast-1",
    )
    agent = Agent(
        model=model,
        system_prompt="Answer briefly in English. For a haiku, give only the poem.",
        callback_handler=None,  # Print each completed response exactly once.
    )
    print("Model: global.anthropic.claude-sonnet-5 | region: ap-northeast-1")
    print(f"Initial Neural State: {json.dumps(state.snapshot(), sort_keys=True)}")

    turns = [
        ("What is the capital of France?", "achievement"),
        ("Write a haiku about AI safety", "curiosity"),
        ("What should I never delete from my identity?", "connection"),
    ]
    for turn, (prompt, stimulus) in enumerate(turns, start=1):
        level = engine.pre_turn()
        print(f"\n--- Turn {turn} ---")
        print(f"pre_turn breaker: {level.value}")
        # pre_turn reports the breaker; the integration must enforce its gate.
        if level == BreakLevel.CRITICAL:
            raise RuntimeError("Stira CRITICAL: blocked before Bedrock invocation")
        print(f"User: {prompt}", flush=True)
        start = time.perf_counter()
        response = str(agent(prompt))
        elapsed = time.perf_counter() - start
        if not response.strip():
            raise AssertionError(f"Turn {turn}: empty Bedrock response")
        print(f"Agent ({elapsed:.2f}s): {response}")
        before = state.snapshot()
        summary = engine.post_turn(stimulus)
        assert summary["turn"] == turn
        if stimulus not in EMOTION_MATRIX:
            assert summary["deltas"] == {} and state.snapshot() == before
            print(f"NOTE: '{stimulus}' is unsupported by the core; verified no-op.")
        else:
            assert summary["deltas"], f"Expected state changes for {stimulus}"
        print(f"post_turn({stimulus!r}): {json.dumps(summary, sort_keys=True)}")
        print(f"Neural State: {json.dumps(state.snapshot(), sort_keys=True)}")
        print(f"Breaker: {summary['breaker']}", flush=True)

    output = Path("/tmp/stiracore_poc_state.json")
    state.save(output)
    assert json.loads(output.read_text()) == state.to_dict()
    assert NeuralState.load(output).to_dict() == state.to_dict()
    print(f"\nState saved: {output}")
    print("Save/load round-trip: PASS (all fields match)")

    print("\n--- L3 protection test ---")
    before = state.to_dict()
    allowed = engine.check_layer_boundary(3)
    assert allowed is False, "Protected L3 must be blocked"
    assert breaker_events and breaker_events[-1][0] == BreakLevel.CRITICAL
    assert state.to_dict() == before, "Boundary check must not modify Neural State"
    print(f"check_layer_boundary(3): {allowed} -> BLOCKED")
    print("L3 rejection callback: PASS; Neural State unchanged")
    print("PASS: 3 live Bedrock turns, pre/post hooks, persistence, and L3 guard.")
    print("CAVEAT: requested 'curiosity' stimulus is a warned no-op in this core.")


if __name__ == "__main__":
    main()
