"""#165: breaker log lines on transitions only, numbers-only summaries while a level persists.

Before #165 the engine wrote the full CRITICAL warning on every CRITICAL turn (a long
hostile conversation produced one warning per turn). The ``on_breaker`` callback still
fires every CRITICAL turn: hosts re-apply tool restrictions from it.
"""

from __future__ import annotations

import logging
import re

import pytest

from stiracore.core import BreakLevel, NeuralState, StiraEngine
from stiracore.core.breaker_sim import _DiscardCheckpointStore
from stiracore.core.engine import BREAKER_LOG_EVERY_TURNS

ENGINE_LOGGER = "stiracore.core.engine"
EPOCH = 1_700_000_000.0


def _engine(stability: float, **kwargs) -> StiraEngine:
    clock = {"now": EPOCH}
    engine = StiraEngine(
        state=NeuralState(stability=stability, last_decay_check=EPOCH, last_stimulus_time=EPOCH),
        now_fn=lambda: clock["now"],
        checkpoint_store=_DiscardCheckpointStore(),
        **kwargs,
    )
    engine._clock = clock  # test handle
    return engine


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ENGINE_LOGGER and record.levelno >= logging.WARNING
    ]


def test_critical_logs_once_on_entry_then_summary_every_n_turns(caplog):
    fired: list[BreakLevel] = []
    engine = _engine(0.2, on_breaker=lambda level, _msg: fired.append(level))
    caplog.set_level(logging.DEBUG, logger=ENGINE_LOGGER)

    turns = 2 * BREAKER_LOG_EVERY_TURNS + 5
    for _ in range(turns):
        assert engine.pre_turn().level is BreakLevel.CRITICAL
        engine.post_turn(stimulus="threat", intensity=1.0)

    lines = _warnings(caplog)
    assert len(lines) == 3
    assert lines[0].startswith("🚨 CRITICAL: stability=0.20.")
    for line, n in zip(lines[1:], (1, 2), strict=True):
        assert re.fullmatch(
            rf"breaker CRITICAL continues: turns={n * BREAKER_LOG_EVERY_TURNS} "
            r"stability=0\.00 bcScore=\d\.\d\d",
            line,
        ), line
    # The callback is not throttled: tool restrictions are re-applied every CRITICAL turn.
    assert fired == [BreakLevel.CRITICAL] * turns
    # The other turns are kept at DEBUG.
    debug = [r for r in caplog.records if r.name == ENGINE_LOGGER and r.levelno == logging.DEBUG]
    assert len(debug) == turns - 1 - 2


def test_transitions_are_logged_each_time(caplog):
    engine = _engine(0.7)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)

    # Fewer than 5 bc samples keep COHERENCE out of the way until the reset below.
    levels = []
    for stability in (0.7, 0.4, 0.2, 0.2, 0.2, 0.5):
        engine.state.stability = stability
        levels.append(engine.pre_turn().level)
    assert levels == [
        BreakLevel.NORMAL,
        BreakLevel.WARNING,
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
        BreakLevel.CRITICAL,
        BreakLevel.WARNING,
    ]
    engine.state.stability = 0.7
    engine.state.bc_window.clear()
    engine.state.bc_score = 1.0
    assert engine.pre_turn().level is BreakLevel.NORMAL
    lines = [r.getMessage() for r in caplog.records if r.name == ENGINE_LOGGER]
    assert [line.split(":")[0] for line in lines] == [
        "⚠️ WARNING",
        "🚨 CRITICAL",
        "⚠️ WARNING",
        "🟢 NORMAL",
    ]


def test_persisting_warning_writes_only_summaries(caplog):
    engine = _engine(0.4, breaker_log_every_turns=3)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    for _ in range(7):
        assert engine.pre_turn().level is BreakLevel.WARNING
    assert _warnings(caplog) == [
        (
            "⚠️ WARNING: stability=0.40. Write/exec tools withheld; reads keep running. "
            "Recovery requires stability > 0.6."
        ),
        "breaker WARNING continues: turns=3 stability=0.40 bcScore=1.00",
        "breaker WARNING continues: turns=6 stability=0.40 bcScore=1.00",
    ]


def test_breaker_log_never_contains_conversation_text(caplog):
    secret = "ユーザーの本文 SECRET-TOKEN-165"
    engine = _engine(0.2)
    caplog.set_level(logging.DEBUG, logger="stiracore")
    for _ in range(BREAKER_LOG_EVERY_TURNS + 1):
        engine.pre_turn(conversation_history=[{"role": "user", "content": secret}])
        engine.post_turn(stimulus="loss", intensity=1.0, transcription=secret)
    assert engine.state.emotion_transcription.startswith("ユーザーの本文")
    assert caplog.records
    for record in caplog.records:
        assert "SECRET-TOKEN-165" not in record.getMessage()
        assert "ユーザー" not in record.getMessage()


def test_resume_does_not_relog_the_restored_level(caplog, tmp_path):
    from stiracore.core.checkpoint import FileCheckpointStore

    store = FileCheckpointStore(tmp_path)
    engine = StiraEngine(state=NeuralState(stability=0.2), checkpoint_store=store)
    engine.pre_turn()
    checkpoint_id = engine.last_checkpoint_id
    engine.post_turn()

    resumed = StiraEngine(state=NeuralState(), checkpoint_store=store)
    resumed.resume_from_checkpoint(checkpoint_id)
    caplog.set_level(logging.INFO, logger=ENGINE_LOGGER)
    caplog.clear()
    assert resumed.pre_turn().level is BreakLevel.CRITICAL
    assert _warnings(caplog) == []


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "20"])
def test_breaker_log_every_turns_must_be_a_positive_integer(value):
    with pytest.raises(ValueError, match="breaker_log_every_turns"):
        StiraEngine(breaker_log_every_turns=value)
