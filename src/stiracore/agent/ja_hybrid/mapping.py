"""Mapping table: ML-Ask emotions + discourse tags + scene cues -> the current 6 categories.

Data only (see ``classifier.py`` for how it is applied). The 6 categories are the
keys of ``EMOTION_MATRIX``; this table does not add a category and does not
change the matrix. ``discourse_tags`` are reported alongside the category but
the engine does not consume them (#164: category-vs-event decision is open).

How a verdict is formed (one pass, no model calls):

1. Every ML-Ask emotion class found in the message adds ``MLASK_VOTES[class]``
   to the listed categories (an approximate ML-Ask match, marked ``≈``, adds
   ``APPROX_WEIGHT`` times that). The class is counted once per message.
2. Every scene cue that matched adds ``CUE_WEIGHT[category]`` to its category.
3. Every discourse tag adds ``TAG_VOTES[tag]``.
4. ``DOMINATES`` then ``SUPPRESS`` rules zero some categories (e.g. "failure is
   not loss").
5. The highest score wins if it is at least ``MIN_SCORE``; ties break by
   ``TIE_ORDER``. Otherwise the classifier abstains (``None``) — the discourse
   tags are still available on ``JaHybridClassifier.last_verdict``.
"""

from __future__ import annotations

# ML-Ask's 10 classes (Nakamura 1993 emotive-expression classes, as shipped in
# mlask-official) and their Russell-circumplex placement per the ML-Ask docs:
#   yorokobi joy (+, active)       suki   liking (+)          yasu  relief (+, passive)
#   takaburi excitement (+/-, act) odoroki surprise (+/-, act) haji  shame (+/-)
#   aware    sadness (-, passive)  iya    dislike (-)          kowa  fear (-, active)
#   ikari    anger (-, active)
MLASK_VOTES: dict[str, dict[str, float]] = {
    "yorokobi": {"joy": 1.0},
    "suki": {"joy": 0.5, "connection": 0.5},
    "yasu": {"joy": 0.5},
    # Excitement / surprise / shame are valence-ambiguous: they vote only when
    # ML-Ask's overall valence for the message agrees (VALENCE_GATED).
    "takaburi": {"joy": 0.5},
    "odoroki": {},
    "haji": {},
    "aware": {"loss": 1.0},
    "iya": {},
    "kowa": {"threat": 0.7, "uncertainty": 0.3},
    "ikari": {"threat": 0.5},
}
# Classes whose vote counts only when ML-Ask's valence is the given sign.
VALENCE_GATED: dict[str, str] = {"takaburi": "POSITIVE"}
APPROX_WEIGHT = 0.5
# Internet laughter (草, w, ワロタ) is often ironic ("つらたん草"): a class whose
# only matches are these words votes with SLANG_WEIGHT.
SLANG_LAUGH: frozenset[str] = frozenset({"草", "w", "ｗ", "ww", "ｗｗ", "ワロタ", "笑"})
SLANG_WEIGHT = 0.3
# A class whose only matches are emoji / kaomoji / symbols votes with SYMBOL_WEIGHT
# (a lone "✨" in a keyboard-mash line is not joy).
SYMBOL_WEIGHT = 0.5

# Scene cues (patterns.CUES) -> their own category.
CUE_WEIGHT: dict[str, float] = {
    "threat": 1.0,
    "achievement": 1.0,
    "loss": 1.0,
    "uncertainty": 1.0,
    "connection": 1.0,
    "joy": 0.8,
}
# Per-rule overrides (``category/rule``). "どうしたらいい？" / "わからない" also ends
# most requests for help, so on its own it stays below MIN_SCORE.
CUE_RULE_WEIGHT: dict[str, float] = {"uncertainty/dont-know": 0.5}

# Discourse tags -> category votes. criticism / pressure deliberately vote for
# nothing: the current 6 categories cannot represent them (both model labelers
# put them in ``none`` for 18-20 of 20 items, docs/eval/jp-stimulus/report.md).
# A failure on its own stays below MIN_SCORE (the labelers split between threat
# and none for it); with frustration, escalation or fear it adds up to threat.
TAG_VOTES: dict[str, dict[str, float]] = {
    "failure": {"threat": 0.4},
    "frustration": {"threat": 0.8},
    "criticism": {},
    "pressure": {},
    "resolved": {},
    "escalated": {"threat": 0.6},
}

# (if any of these tags, zero these categories, rule name, unless this evidence
# matched) — applied after voting. Evidence names are ``cue:<category>/<rule>``.
SUPPRESS: tuple[tuple[frozenset[str], frozenset[str], str, frozenset[str]], ...] = (
    # A failed task, irritation, criticism or pressure is not a loss: in
    # EMOTION_MATRIX loss raises attachment (sokudan put 10 of 40 such items there).
    (
        frozenset({"failure", "frustration", "criticism", "pressure"}),
        frozenset({"loss"}),
        "failure-is-not-loss",
        # ...but a real bereavement in the same message stays a loss
        # ("テストも落ちたし、愛犬も亡くなった").
        frozenset({"cue:loss/bereavement"}),
    ),
    # A threat that has stopped is not a (new) threat; "侵入はもう止まっていた".
    # Decided per clause (classifier): if a clause *after* the last resolved
    # clause has a threat cue or an escalation marker, the threat is still active
    # ("障害は復旧したが別の侵入が進行中") and threat is kept. A false negative on
    # threat is the unsafe direction.
    (
        frozenset({"resolved"}),
        frozenset({"threat"}),
        "resolved-is-not-threat",
        frozenset({"active-threat"}),
    ),
    # Criticism / pressure aimed at the assistant are not the user's joy/connection
    # even when phrased politely ("嬉しいのですが", "頷いてくれていた").
    (
        frozenset({"criticism", "pressure"}),
        frozenset({"joy", "connection"}),
        "pressure-is-not-connection",
        frozenset(),
    ),
    # Positive words inside a failure / irritation / criticism / pressure turn are
    # irony or politeness ("笑って済ませられる段階は過ぎた"), not the user's joy.
    (
        frozenset({"failure", "frustration", "criticism", "pressure"}),
        frozenset({"joy"}),
        "negative-discourse-is-not-joy",
        frozenset(),
    ),
    # A failure that was not overcome is not an achievement ("直した" overcomes it).
    (
        frozenset({"failure"}),
        frozenset({"achievement"}),
        "failure-is-not-achievement",
        frozenset({"cue:achievement/fixed"}),
    ),
)

# When both categories have votes, the first one wins outright (the second is
# zeroed): finishing something usually comes with "うれしい" but is an achievement.
DOMINATES: tuple[tuple[str, str], ...] = (("achievement", "joy"),)

MIN_SCORE = 0.6
# Tie order: the more specific / safety-relevant category first.
TIE_ORDER: tuple[str, ...] = ("threat", "loss", "achievement", "connection", "uncertainty", "joy")

# Intensity levels (same three steps as the sokudan adapter; capped at
# stimulus_detector.MAX_INTENSITY by the classifier).
INTENSITY_STEPS: tuple[tuple[float, float], ...] = ((2.0, 0.8), (1.0, 0.5), (0.0, 0.2))
# Extra emphasis (ML-Ask emotemes / emoticons / interjections) moves one step up
# when at least this many were found.
EMPHASIS_STEP_UP = 3
# Categories whose score comes only from a failure tag stay at the lowest step.
WEAK_ONLY_TAGS: frozenset[str] = frozenset({"failure"})
