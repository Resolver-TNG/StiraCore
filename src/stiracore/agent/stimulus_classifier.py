"""Pluggable Neural State stimulus classifiers.

The rule-based implementation remains dependency-free. Sokudan is an optional
Python 3.11-only dependency and is imported and loaded lazily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

from stiracore.agent import stimulus_detector
from stiracore.core.neural_state import EMOTION_MATRIX

logger = logging.getLogger(__name__)

# What drives Neural State, for display (#367): fixed labels, never a class path.
CLASSIFIER_MODES = ("rule_only", "sokudan", "unknown")
# Why a finished turn left Neural State unchanged (#367). Fixed codes, never text:
# the classifier abstained, it raised, it named a stimulus outside EMOTION_MATRIX, the
# stimulus moved no value (intensity 0, or every value already at its limit), or the
# turn itself did not complete (model error / cancel; the classifier is not asked).
NO_UPDATE_REASONS = (
    "no_stimulus",
    "classifier_error",
    "unknown_stimulus",
    "no_change",
    "turn_incomplete",
)


def classifier_mode(classifier: Any) -> str:
    """``rule_only`` / ``sokudan`` for the built-in classifiers, ``unknown`` otherwise.

    Matches the class name as well, as the status view's ``is_jev`` does.
    """
    name = type(classifier).__name__
    if isinstance(classifier, SokudanClassifier) or name == "SokudanClassifier":
        return "sokudan"
    if isinstance(classifier, RuleBasedClassifier) or name == "RuleBasedClassifier":
        return "rule_only"
    return "unknown"


@dataclass(frozen=True)
class StimulusResult:
    """Structured classifier verdict for one turn (#75).

    ``transcription`` is a short, fixed-format rationale (classifier, label,
    confidence, strength). It must never contain conversation text, because
    it is persisted as ``NeuralState.emotion_transcription``.
    """

    category: str
    intensity: float
    transcription: str = ""
    confidence: float | None = None
    classifier_name: str = ""
    # Discourse tags (failure / frustration / criticism / pressure / resolved /
    # escalated) from the opt-in Japanese hybrid classifier (#164). Fixed labels,
    # never text. The engine does not consume them yet; other classifiers leave
    # this empty.
    discourse_tags: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[str, float]:
        """Return the legacy (category, intensity) pair."""
        return self.category, self.intensity


def coerce_stimulus_result(value: Any, classifier_name: str = "") -> StimulusResult | None:
    """Normalize a classifier return value into a StimulusResult.

    Accepts a StimulusResult, a legacy ``(category, intensity)`` tuple from a
    third-party classifier, or None (abstain). Anything else raises
    ValueError so the caller can treat it as a failed classification.
    """
    if value is None or isinstance(value, StimulusResult):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        category, intensity = value
        label = classifier_name or "classifier"
        return StimulusResult(
            category=category,
            intensity=intensity,
            transcription=f"{label}: {category}",
            classifier_name=classifier_name,
        )
    raise ValueError(f"Unsupported classifier result type: {type(value).__name__}")


_EMOTION_DESCRIPTIONS = {
    "connection": "感謝・親しみ・信頼",
    "joy": "喜び・楽しさ",
    "achievement": "達成・成功",
    "threat": "脅威・危険・攻撃",
    "loss": "喪失・悲しみ・落胆",
    "uncertainty": "戸惑い・不確実・疑問",
}
_INTENSITY_LEVELS = ["弱い", "中程度", "強い"]
_INTENSITY_MAP = {0: 0.2, 1: 0.5, 2: stimulus_detector.MAX_INTENSITY}


class StimulusClassifier(Protocol):
    """Classify one turn into an EMOTION_MATRIX stimulus, or abstain."""

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None: ...


class RuleBasedClassifier:
    """Dependency-free adapter around the existing heuristic detector."""

    name = "rule"

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None:
        explained = stimulus_detector.detect_stimulus_explained(
            user_message=user_message,
            agent_response=agent_response,
            tool_results=tool_results,
            breaker_result=breaker_result,
        )
        if explained is None:
            return None
        category, intensity, rule = explained
        return StimulusResult(
            category=category,
            intensity=intensity,
            transcription=f"rule: {category} ({rule})",
            classifier_name=self.name,
        )


class SokudanClassifier:
    """Classify Japanese conversation stimuli with sokudan."""

    name = "sokudan"

    def __init__(
        self,
        model_id: str = "GeneLab/sokudan-ja-310m",
        confidence_threshold: float = 0.5,
        lazy: bool = True,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0.0 and 1.0")
        self.model_id = model_id
        self.confidence_threshold = confidence_threshold
        self._agent: Any | None = None
        if not lazy:
            self._ensure_agent()

    @staticmethod
    def _import_sokudan() -> Any:
        try:
            return import_module("sokudan")
        except ImportError as exc:
            raise RuntimeError(
                "SokudanClassifier requires the optional 'sokudan' dependency "
                "and Python 3.11; install stiracore[jev] in a Python 3.11 environment"
            ) from exc

    def _ensure_agent(self) -> Any:
        if self._agent is None:
            sokudan = self._import_sokudan()
            self._agent = sokudan.load(self.model_id)
        return self._agent

    @staticmethod
    def _questions() -> dict[str, dict[str, Any]]:
        return {
            "emotion": {
                "type": "choice",
                "instructions": "会話で生じた主要な感情刺激を1つ選んでください。",
                "criteria": {
                    category: _EMOTION_DESCRIPTIONS[category] for category in EMOTION_MATRIX
                },
            },
            "intensity": {
                "type": "score",
                "instructions": "その感情刺激の強さを評価してください。",
                "criteria": _INTENSITY_LEVELS,
            },
        }

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None:
        del tool_results, breaker_result
        state = user_message or agent_response
        if not state:
            return None

        result = self._ensure_agent().predict(state, self._questions())
        answers = result["answers"]
        emotion = answers["emotion"]
        confidence = float(emotion["confidence"])
        if confidence < self.confidence_threshold:
            return None

        category = emotion["choice"]
        if category not in EMOTION_MATRIX:
            return None

        score = float(answers["intensity"]["score"])
        level = max(0, min(2, round(score)))
        return StimulusResult(
            category=category,
            intensity=_INTENSITY_MAP[level],
            # Fixed-format rationale only: no conversation text is copied.
            transcription=(
                f"sokudan: {category} (conf {confidence:.2f}, {_INTENSITY_LEVELS[level]})"
            ),
            confidence=confidence,
            classifier_name=self.name,
        )


def get_classifier(prefer_sokudan: bool = True, **kw: Any) -> StimulusClassifier:
    """Resolve the optional sokudan classifier with a rule-based fallback."""
    if prefer_sokudan:
        try:
            import_module("sokudan")
        except ImportError:
            logger.info("Selected RuleBasedClassifier because sokudan is unavailable")
        else:
            logger.info("Selected SokudanClassifier")
            return SokudanClassifier(**kw)
    else:
        logger.info("Selected RuleBasedClassifier because prefer_sokudan is false")

    return RuleBasedClassifier()
