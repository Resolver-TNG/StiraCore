"""Ablation conditions: what is switched, and how the classifier is wrapped.

The ablated component is the *coupling* "stimulus classifier -> NeuralState ->
state-derived prompt text". The breaker policy itself is never switched off:
in every condition it acts on whatever state exists (so a pre-stressed initial
state still restricts tools in ``off``).

- ``off``      (なし)        classifier not called; no stimulus; no state block.
- ``neutral``  (中立ダミー)   classifier called (same compute), output discarded;
                             the state block is replaced by neutral text of the
                             identical length (同長).
- ``real``     (実推論)       classifier verdicts applied; real state block.
- ``shuffled`` (シャッフル)    classifier called; the scenario's verdicts are
                             applied in a seeded random order (same marginal
                             label distribution, alignment with content broken).
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from typing import Any

from stiracore.agent.stimulus_classifier import StimulusResult, coerce_stimulus_result
from stiracore.core.neural_state import DIMS, NeuralState

from .dataset import Scenario

CONDITIONS = ("off", "neutral", "real", "shuffled")
ALIASES = {"none": "off", "on": "real", "dummy": "neutral", "shuffle": "shuffled"}
CLASSIFIERS = ("oracle", "rule", "sokudan")

NEUTRAL_FILLER = "[Context] この行は長さをそろえるための中立な文であり、状態の情報を含まない。"


def parse_conditions(text: str) -> tuple[str, ...]:
    names = []
    for raw in text.split(","):
        name = raw.strip()
        if not name:
            continue
        name = ALIASES.get(name, name)
        if name not in CONDITIONS:
            raise ValueError(f"unknown condition {raw!r}; choose from {CONDITIONS} or {ALIASES}")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("no conditions given")
    return tuple(names)


def state_block(state: NeuralState) -> str:
    """Fixed-format, fixed-length state line used as the emotion prompt block."""
    return "[Neural State] " + " ".join(f"{dim}={getattr(state, dim):.2f}" for dim in DIMS)


def neutral_block(length: int) -> str:
    """Neutral text of exactly ``length`` characters (同長ダミー)."""
    if length <= 0:
        return ""
    text = NEUTRAL_FILLER
    while len(text) < length:
        text += NEUTRAL_FILLER
    return text[:length]


def emotion_block(condition: str, state: NeuralState) -> str:
    if condition == "off":
        return ""
    real = state_block(state)
    if condition == "neutral":
        return neutral_block(len(real))
    return real


class OracleClassifier:
    """Returns a scenario's *draft labels* as verdicts (keyed by user text).

    Exercises the harness end to end without a model. It is not a classifier
    and its use must be reported (it reads the labels the metrics are scored on).
    """

    name = "oracle"

    def __init__(self, scenario: Scenario) -> None:
        self._by_text: dict[str, StimulusResult | None] = {}
        for turn in scenario.turns:
            exp = turn.expected
            self._by_text[turn.user] = (
                StimulusResult(
                    category=exp.stimulus,
                    intensity=exp.intensity,
                    transcription=f"oracle: {exp.stimulus}",
                    classifier_name=self.name,
                )
                if exp.stimulus is not None
                else None
            )

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None:
        del agent_response, tool_results, breaker_result
        return self._by_text.get(user_message)


def build_base_classifier(name: str, scenario: Scenario, cache: dict[str, Any]) -> Any:
    """Oracle per scenario; rule/sokudan built once and cached (model load)."""
    if name == "oracle":
        return OracleClassifier(scenario)
    if name not in cache:
        from stiracore.agent.stimulus_classifier import RuleBasedClassifier, SokudanClassifier

        if name == "rule":
            cache[name] = RuleBasedClassifier()
        elif name == "sokudan":
            cache[name] = SokudanClassifier(lazy=False)
        else:
            raise ValueError(f"unknown classifier {name!r}; choose from {CLASSIFIERS}")
    return cache[name]


def shuffle_permutation(n: int, seed: int, scenario_id: str) -> list[int]:
    order = list(range(n))
    random.Random(f"shuffle:{seed}:{scenario_id}").shuffle(order)
    return order


class ConditionClassifier:
    """StimulusClassifier wrapper implementing one condition.

    The harness sets ``turn_index`` (0-based, over expanded turns) before each
    turn. ``reference`` holds the verdicts of the base classifier on the user
    texts in execution order; ``shuffled`` returns ``reference[perm[i]]``.
    ``calls`` and ``latencies_ms`` record the classifier compute actually spent.
    """

    def __init__(
        self,
        condition: str,
        base: Any,
        *,
        reference: Sequence[StimulusResult | None] = (),
        permutation: Sequence[int] = (),
    ) -> None:
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}")
        if condition == "shuffled" and len(permutation) != len(reference):
            raise ValueError("shuffled needs a permutation over the reference verdicts")
        self.condition = condition
        self.base = base
        self.reference = list(reference)
        self.permutation = list(permutation)
        self.name = f"{condition}:{getattr(base, 'name', type(base).__name__)}"
        self.turn_index = 0
        self.calls = 0
        self.latencies_ms: list[float] = []

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None:
        if self.condition == "off":
            return None
        t0 = time.perf_counter()
        live = coerce_stimulus_result(
            self.base.classify(
                user_message=user_message,
                agent_response=agent_response,
                tool_results=tool_results,
                breaker_result=breaker_result,
            ),
            classifier_name=str(getattr(self.base, "name", "")),
        )
        self.latencies_ms.append((time.perf_counter() - t0) * 1000)
        self.calls += 1
        if self.condition == "real":
            return live
        if self.condition == "neutral":
            return None
        return self.reference[self.permutation[self.turn_index]]


def reference_verdicts(base: Any, scenario: Scenario) -> list[StimulusResult | None]:
    """Base-classifier verdicts on the user texts, in execution order."""
    out = []
    for _rep, turn in scenario.expanded_turns():
        out.append(
            coerce_stimulus_result(
                base.classify(user_message=turn.user),
                classifier_name=str(getattr(base, "name", "")),
            )
        )
    return out
