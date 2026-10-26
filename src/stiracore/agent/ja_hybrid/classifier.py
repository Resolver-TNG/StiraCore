"""Japanese rule-based stimulus classifier: ML-Ask emotion words + discourse patterns (#164).

Opt-in only. Nothing in the default classifier selection uses this module; a
caller has to construct :class:`JaHybridClassifier` and pass it (for example
``NXPAgent(..., stimulus_classifier=JaHybridClassifier())``).

Two layers, then one explicit mapping table:

* **Emotion-word layer** — ``mlask-official`` (ML-Ask, BSD-3-Clause): 10 emotion
  classes from Nakamura's dictionary of emotive expressions, negation flipping
  (CVS), emoji / kaomoji / slang entries. Optional dependency: the
  ``ja-rules`` extra. No network access at run time; the dictionary ships in
  the wheels.
* **Discourse / intent layer** — the small hand-written tables in
  ``patterns.py`` (failure, frustration, criticism, pressure, resolved,
  escalated, plus scene cues for the 6 categories).
* **Mapping** — ``mapping.py`` turns both into one of the 6 existing
  ``EMOTION_MATRIX`` categories (or abstains). ``discourse_tags`` are attached to
  the result as a separate field; the engine does not consume them.

Safety properties (tested): no model calls, no network, bounded work per turn
(the analysed text is capped at ``max_chars`` and fed to ML-Ask in chunks of at
most ``chunk_chars``, because ML-Ask's per-call cost grows quadratically with
length), patterns without unbounded quantifiers, and no conversation text in
logs, ``transcription``, ``evidence`` or :meth:`status`.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Any

from stiracore.agent import stimulus_detector
from stiracore.agent.ja_hybrid import mapping, patterns
from stiracore.agent.stimulus_classifier import RuleBasedClassifier, StimulusResult

logger = logging.getLogger(__name__)

NAME = "ja-hybrid"
ON_MISSING = ("rule", "patterns", "error")
DEFAULT_MAX_CHARS = 4000
DEFAULT_CHUNK_CHARS = 400
INSTALL_HINT = (
    "JaHybridClassifier needs the optional 'ja-rules' extra "
    "(pip install 'stiracore[ja-rules]': mlask-official + ipadic)"
)

_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2}")
_SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")
# Kana, kanji or Latin letters: a match without any is an emoji / kaomoji / symbol.
_LETTER = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff21-\uff5a]")

# Valence / arousal of ML-Ask's classes (Russell circumplex, per the ML-Ask docs).
_POSITIVE = frozenset({"yorokobi", "suki", "yasu"})
_NEGATIVE = frozenset({"aware", "iya", "kowa", "ikari"})
_ACTIVE = frozenset({"yorokobi", "takaburi", "odoroki", "kowa", "ikari"})
_PASSIVE = frozenset({"yasu", "aware"})
# ML-Ask classes that, with a repetition marker, make "frustration".
_IRRITATED = frozenset({"ikari", "iya"})


def _compile(rows: Iterable[tuple[str, str]]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(rx)) for name, rx in rows)


_DISCOURSE = {tag: _compile(rows) for tag, rows in patterns.DISCOURSE.items()}
_CUES = {cat: _compile(rows) for cat, rows in patterns.CUES.items()}
_REPETITION = _compile(patterns.REPETITION)
_NOT_ACHIEVED = _compile(patterns.NOT_ACHIEVED)
_UNRESOLVED = _compile(patterns.UNRESOLVED)


def _blank(table, text: str) -> str:
    for _name, rx in table:
        text = rx.sub(" ", text)
    return text


_IGNORE = _compile(patterns.IGNORE)
_CLAUSE_SPLIT = re.compile(patterns.CLAUSE_SPLIT)


def _hits(table: tuple[tuple[str, re.Pattern[str]], ...], text: str) -> list[str]:
    return [name for name, rx in table if rx.search(text)]


@dataclass(frozen=True)
class JaHybridVerdict:
    """Everything the classifier decided for one message — fixed labels only, no text."""

    category: str | None
    intensity: float
    discourse_tags: tuple[str, ...] = ()
    scores: dict[str, float] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    mlask_classes: tuple[str, ...] = ()
    valence: str = "NEUTRAL"
    activation: str = "NEUTRAL"
    emotion_layer: str = "mlask"
    truncated: bool = False
    fallback: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "intensity": self.intensity,
            "discourse_tags": list(self.discourse_tags),
            "scores": dict(self.scores),
            "evidence": list(self.evidence),
            "mlask_classes": list(self.mlask_classes),
            "valence": self.valence,
            "activation": self.activation,
            "emotion_layer": self.emotion_layer,
            "truncated": self.truncated,
            "fallback": self.fallback,
        }


def cap_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Head + tail of an over-long message (the parts that usually carry the feeling)."""
    if len(text) <= max_chars:
        return text, False
    half = max_chars // 2
    return text[:half] + "\n" + text[-half:], True


def chunks(text: str, size: int) -> list[str]:
    """Split at sentence ends into pieces of at most ``size`` characters."""
    out: list[str] = []
    buf = ""
    for part in _SENTENCE_END.split(text):
        while len(part) > size:  # one very long "sentence": hard split
            if buf:
                out.append(buf)
                buf = ""
            out.append(part[:size])
            part = part[size:]
        if len(buf) + len(part) > size:
            out.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        out.append(buf)
    return [c for c in out if c.strip()]


class JaHybridClassifier:
    """Opt-in Japanese rule classifier (ML-Ask + discourse patterns), no model calls.

    ``on_missing`` decides what happens when ``mlask-official`` (or its MeCab
    dictionary) is not available:

    * ``"rule"`` (default): every turn is classified by the existing
      :class:`RuleBasedClassifier`; a warning is logged once, :meth:`status`
      reports ``fallback="rule"`` and the result's ``classifier_name`` is
      ``"ja-hybrid:rule-fallback"``.
    * ``"patterns"``: run the discourse/cue layer without the emotion-word layer
      (``status()["emotion_layer"] == "unavailable"``).
    * ``"error"``: raise ``RuntimeError`` with the install hint.

    Any other exception while building the analyzer is treated like a missing
    dependency (``reason="analyzer_init_error"``, one warning, no retry per turn),
    except with ``on_missing="error"``, which raises.

    ``last_verdict`` is per-instance state (the verdict of the most recent
    :meth:`classify` call). Do not call :meth:`classify` concurrently on one
    instance if you read ``last_verdict``; use one instance per thread instead.
    The ML-Ask calls themselves are serialised with a lock.
    """

    name = NAME

    def __init__(
        self,
        *,
        on_missing: str = "rule",
        backend: str = "mecab",
        mecab_arg: str | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        lazy: bool = True,
        analyzer: Any | None = None,
        emotion_layer: bool = True,
    ) -> None:
        if on_missing not in ON_MISSING:
            raise ValueError(f"on_missing must be one of {ON_MISSING}")
        if backend not in ("mecab", "fugashi"):
            raise ValueError("backend must be 'mecab' or 'fugashi'")
        if max_chars < 16 or chunk_chars < 16:
            raise ValueError("max_chars and chunk_chars must be at least 16")
        self.on_missing = on_missing
        self.backend = backend
        self.mecab_arg = mecab_arg
        self.max_chars = max_chars
        self.chunk_chars = chunk_chars
        self._analyzer = analyzer if emotion_layer else None
        # emotion_layer=False: patterns only, never imports mlask (evaluation ablation).
        self._resolved = analyzer is not None or not emotion_layer
        self._disabled = not emotion_layer
        self._unavailable: str | None = None
        self._lock = threading.Lock()
        self._rule = RuleBasedClassifier()
        self.last_verdict: JaHybridVerdict | None = None
        if not lazy:
            self._ensure_analyzer()

    # -- emotion-word layer ------------------------------------------------

    def _build_analyzer(self) -> Any:
        mlask = import_module("mlask_official")
        arg = self.mecab_arg
        if arg is None:
            arg = ""
            if self.backend == "mecab":
                try:  # pip-installed IPADIC (the ja-rules extra); else MeCab's own config
                    arg = import_module("ipadic").MECAB_ARGS
                except ImportError:
                    arg = ""
        # use_cache=False: no lemma cache written under the user's home directory.
        return mlask.MLAskOfficial(arg, backend=self.backend, use_cache=False)

    def _ensure_analyzer(self) -> Any | None:
        if self._resolved:
            return self._analyzer
        with self._lock:
            if self._resolved:
                return self._analyzer
            try:
                self._analyzer = self._build_analyzer()
            except ImportError as exc:
                self._mark_unavailable("mlask_not_installed", exc)
            except (RuntimeError, OSError, ValueError) as exc:  # MeCab / dictionary init
                self._mark_unavailable("mecab_init_failed", exc)
            except Exception as exc:  # noqa: BLE001 - unavailable once, not a retry per turn
                self._mark_unavailable("analyzer_init_error", exc)
            self._resolved = True
        return self._analyzer

    def _mark_unavailable(self, reason: str, exc: BaseException) -> None:
        if self.on_missing == "error":
            raise RuntimeError(f"{INSTALL_HINT} [{reason}]") from exc
        self._unavailable = reason
        logger.warning(
            "JaHybridClassifier: emotion-word layer unavailable (%s, %s); using %s",
            reason,
            type(exc).__name__,
            "RuleBasedClassifier" if self.on_missing == "rule" else "patterns only",
        )

    def status(self) -> dict[str, Any]:
        """What is actually in use (fixed labels only)."""
        if self._disabled:
            layer = "off"
        elif not self._resolved:
            layer = "not_loaded"
        elif self._analyzer is not None:
            layer = "mlask"
        else:
            layer = "unavailable"
        fallback = None
        if layer == "unavailable":
            fallback = "rule" if self.on_missing == "rule" else "patterns"
        return {
            "name": NAME,
            "emotion_layer": layer,
            "fallback": fallback,
            "reason": self._unavailable,
            "backend": self.backend,
            "on_missing": self.on_missing,
            "max_chars": self.max_chars,
            "chunk_chars": self.chunk_chars,
        }

    def _mlask(self, text: str) -> tuple[dict[str, tuple[bool, str]], int]:
        """ML-Ask classes found and the emphasis count.

        Each class maps to ``(any exact match, kind)`` where kind is ``"word"``,
        or ``"slang"`` / ``"symbol"`` when every match is internet laughter or
        an emoji/kaomoji. The matched words themselves are not kept.
        """
        analyzer = self._analyzer
        words_by_class: dict[str, list[str]] = {}
        emphasis = 0
        if analyzer is None:
            return {}, emphasis
        with self._lock:  # MeCab taggers are not documented as thread-safe
            results = [analyzer.analyze(c) for c in chunks(text, self.chunk_chars)]
        for r in results:
            for cls, words in (r.get("emotion") or {}).items():
                words_by_class.setdefault(cls, []).extend(str(w) for w in words)
            for items in (r.get("intensifier") or {}).values():
                emphasis += len(items)
        classes: dict[str, tuple[bool, str]] = {}
        for cls, words in words_by_class.items():
            exact = any("≈" not in w for w in words)
            bare = [w.replace("≈", "").replace("*CVS", "").replace("*depCVS", "") for w in words]
            if all(b in mapping.SLANG_LAUGH for b in bare):
                kind = "slang"
            elif not any(_LETTER.search(b) for b in bare):
                kind = "symbol"
            else:
                kind = "word"
            classes[cls] = (exact, kind)
        return classes, emphasis

    # -- the hybrid verdict ------------------------------------------------

    def analyze(self, text: str) -> JaHybridVerdict:
        """Classify one message with both layers (also used by the evaluation script)."""
        self._ensure_analyzer()
        capped, truncated = cap_text(text, self.max_chars)
        for _name, rx in _IGNORE:
            capped = rx.sub(" ", capped)
        norm = unicodedata.normalize("NFKC", capped).lower()

        classes, emphasis = self._mlask(capped)
        evidence: list[str] = [f"mlask:{c}" for c in sorted(classes)]

        cue_hits = {cat: _hits(table, norm) for cat, table in _CUES.items()}
        if _hits(_NOT_ACHIEVED, norm):
            cue_hits["achievement"] = []
            evidence.append("not-achieved")
        for cat, names in cue_hits.items():
            evidence.extend(f"cue:{cat}/{n}" for n in names)

        # #601: "復旧していない" is not "復旧": blanked for the resolved table only.
        unresolved = _blank(_UNRESOLVED, norm)
        tag_hits = {
            tag: _hits(table, unresolved if tag == "resolved" else norm)
            for tag, table in _DISCOURSE.items()
        }
        repetition = _hits(_REPETITION, norm)
        tags: set[str] = {t for t, names in tag_hits.items() if names}
        if repetition and classes.keys() & _IRRITATED and "frustration" not in tags:
            tags.add("frustration")
            tag_hits["frustration"] = ["repetition+irritation"]
        threatish = bool(cue_hits["threat"])
        if "resolved" in tags and not (threatish or set(tag_hits["resolved"]) - {"stopped"}):
            # "止まった" alone may be a failure ("デプロイが止まった"); it needs a threat cue.
            # "復旧" / "ひと安心" are resolutions on their own.
            tags.discard("resolved")
        # Per clause, latest state wins: a threat cue or an escalation marker in a
        # clause *after* the last resolved clause is still active
        # ("障害は復旧したが別の侵入が進行中", "止まっていたがまた始まった").
        # "障害が発生しましたが、復旧しました" and "被害拡大は収束した" are not.
        clauses = [c for c in _CLAUSE_SPLIT.split(norm) if c.strip()]
        last_res = max(
            (
                i
                for i, c in enumerate(clauses)
                if _hits(_DISCOURSE["resolved"], _blank(_UNRESOLVED, c))
            ),
            default=-1,
        )
        later = clauses[last_res + 1 :]
        open_escalation = any(_hits(_DISCOURSE["escalated"], c) for c in later)
        active_threat = open_escalation or any(_hits(_CUES["threat"], c) for c in later)
        if "escalated" in tags and not (open_escalation and (threatish or "failure" in tags)):
            # Escalation needs a threat or a failure, outside a resolved clause.
            tags.discard("escalated")
        if "resolved" in tags and active_threat and threatish:
            evidence.append("active-threat")
        for tag in sorted(tags):
            evidence.extend(f"{tag}/{n}" for n in tag_hits[tag])

        valence = self._valence(classes)
        scores: dict[str, float] = dict.fromkeys(mapping.TIE_ORDER, 0.0)
        sources: dict[str, set[str]] = {c: set() for c in mapping.TIE_ORDER}

        def vote(cat: str, weight: float, source: str) -> None:
            if weight:
                scores[cat] += weight
                sources[cat].add(source)

        for cls, (exact, kind) in classes.items():
            gate = mapping.VALENCE_GATED.get(cls)
            if gate and valence != gate:
                continue
            factor = 1.0 if exact else mapping.APPROX_WEIGHT
            if kind == "slang":
                factor = min(factor, mapping.SLANG_WEIGHT)
            elif kind == "symbol":
                factor = min(factor, mapping.SYMBOL_WEIGHT)
            for cat, w in mapping.MLASK_VOTES.get(cls, {}).items():
                vote(cat, w * factor, f"mlask:{cls}")
        for cat, names in cue_hits.items():
            if names:
                weight = max(
                    mapping.CUE_RULE_WEIGHT.get(f"{cat}/{n}", mapping.CUE_WEIGHT[cat])
                    for n in names
                )
                vote(cat, weight, f"cue:{cat}")
        for tag in tags:
            for cat, w in mapping.TAG_VOTES[tag].items():
                vote(cat, w, f"tag:{tag}")

        ev = set(evidence)
        for winner, loser in mapping.DOMINATES:
            if scores[winner] and scores[loser]:
                scores[loser] = 0.0
                evidence.append(f"dominates:{winner}>{loser}")
        for if_tags, zero, rule, unless in mapping.SUPPRESS:
            if tags & if_tags and not (ev & unless):
                for cat in zero:
                    if scores[cat]:
                        scores[cat] = 0.0
                        evidence.append(f"suppress:{rule}")

        category = None
        best = max(scores.values())
        if best >= mapping.MIN_SCORE:
            category = next(c for c in mapping.TIE_ORDER if scores[c] == best)
        intensity = 0.0
        if category is not None:
            intensity = self._intensity(best, emphasis, sources[category])
        return JaHybridVerdict(
            category=category,
            intensity=intensity,
            discourse_tags=tuple(t for t in patterns.DISCOURSE_TAGS if t in tags),
            scores={c: round(s, 3) for c, s in scores.items() if s},
            evidence=tuple(evidence),
            mlask_classes=tuple(sorted(classes)),
            valence=valence,
            activation=self._activation(classes),
            emotion_layer=self.status()["emotion_layer"],
            truncated=truncated,
        )

    @staticmethod
    def _valence(classes: Iterable[str]) -> str:
        cs = set(classes)
        pos, neg = len(cs & _POSITIVE), len(cs & _NEGATIVE)
        return "POSITIVE" if pos > neg else "NEGATIVE" if neg > pos else "NEUTRAL"

    @staticmethod
    def _activation(classes: Iterable[str]) -> str:
        cs = set(classes)
        act, pas = len(cs & _ACTIVE), len(cs & _PASSIVE)
        return "ACTIVE" if act > pas else "PASSIVE" if pas > act else "NEUTRAL"

    @staticmethod
    def _intensity(score: float, emphasis: int, sources: set[str]) -> float:
        steps = [v for _t, v in mapping.INTENSITY_STEPS]  # high -> low
        idx = next(i for i, (t, _v) in enumerate(mapping.INTENSITY_STEPS) if score >= t)
        if emphasis >= mapping.EMPHASIS_STEP_UP:
            idx = max(0, idx - 1)
        if sources and sources <= {f"tag:{t}" for t in mapping.WEAK_ONLY_TAGS}:
            idx = len(steps) - 1
        return min(steps[idx], stimulus_detector.MAX_INTENSITY)

    # -- StimulusClassifier protocol ---------------------------------------

    def classify(
        self,
        user_message: str,
        agent_response: str = "",
        tool_results: list[Any] | None = None,
        breaker_result: Any | None = None,
    ) -> StimulusResult | None:
        # Breaker / tool signals keep the existing rule semantics (they are not text).
        structural = stimulus_detector.detect_stimulus_explained(
            tool_results=tool_results, breaker_result=breaker_result
        )
        if structural is not None:
            category, intensity, rule = structural
            self.last_verdict = JaHybridVerdict(category, intensity, evidence=(f"rule:{rule}",))
            return StimulusResult(
                category=category,
                intensity=intensity,
                transcription=f"{NAME}: {category} ({rule})",
                classifier_name=NAME,
            )
        text = user_message or agent_response
        if not text:
            self.last_verdict = None
            return None

        self._ensure_analyzer()
        if self._analyzer is None and self.on_missing == "rule" and not self._disabled:
            return self._delegate(user_message, agent_response, "rule", "rule-fallback")

        verdict = self.analyze(text)
        if self._analyzer is None and not self._disabled:
            verdict = replace(verdict, fallback="patterns")
        if verdict.category is None and _LATIN_WORD.search(text) and not _JAPANESE.search(text):
            # Non-Japanese text: the existing (English) rules decide.
            return self._delegate(user_message, agent_response, "rule-english", "rule-english")
        self.last_verdict = verdict
        if verdict.category is None:
            return None
        layer = "" if self._analyzer is not None else " [no-mlask]"
        return StimulusResult(
            category=verdict.category,
            intensity=verdict.intensity,
            # Fixed labels only, never conversation text. The tag names are left
            # out on purpose: the transcription is persisted as
            # NeuralState.emotion_transcription and appears in the system prompt's
            # Neural State section, and the engine must not consume the tags yet.
            transcription=f"{NAME}{layer}: {verdict.category}",
            classifier_name=NAME,
            discourse_tags=verdict.discourse_tags,
        )

    def _delegate(
        self, user_message: str, agent_response: str, why: str, name_suffix: str | None
    ) -> StimulusResult | None:
        res = self._rule.classify(user_message=user_message, agent_response=agent_response)
        layer = self.status()["emotion_layer"]
        self.last_verdict = JaHybridVerdict(
            category=res.category if res else None,
            intensity=res.intensity if res else 0.0,
            emotion_layer=layer,
            fallback=why,
        )
        if res is None:
            return None
        label = f"{NAME}:{name_suffix}" if name_suffix else NAME
        return StimulusResult(
            category=res.category,
            intensity=res.intensity,
            transcription=f"{label} -> {res.transcription}",
            classifier_name=label,
        )


def mlask_available() -> bool:
    """True when ``mlask_official`` can be imported (does not start MeCab)."""
    try:
        import_module("mlask_official")
    except ImportError:
        return False
    return True
