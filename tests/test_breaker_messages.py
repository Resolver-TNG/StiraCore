"""#301: the breaker's log / on_breaker text matches what the runtime really does.

CRITICAL ``halt`` keeps the model and read tools running and asks a human for every
write/exec call (ADR 0002/0010, ``test_halt_runs_model_with_write_tools_for_operation_
level_hitl``); it must not say that execution or replies are halted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from stiracore.core import BreakLevel, NeuralState, StiraConfig, StiraEngine

CATALOGS = Path(__file__).resolve().parent.parent / "dashboard" / "static" / "i18n"


def _critical_message(action: str) -> tuple[BreakLevel, str]:
    fired: list[tuple[BreakLevel, str]] = []
    StiraEngine(
        state=NeuralState(stability=0.2),
        config=StiraConfig(critical_action=action),
        on_breaker=lambda level, message: fired.append((level, message)),
    ).pre_turn()
    assert len(fired) == 1
    return fired[0]


def test_halt_message_says_model_and_reads_continue_and_writes_need_approval():
    level, message = _critical_message("halt")
    assert level is BreakLevel.CRITICAL
    assert message == (
        "🚨 CRITICAL: stability=0.20. Model and read tools keep running; every write/exec "
        "call needs a human approval. Recovery requires stability > 0.45 and 3 protected turns."
    )
    assert "Execution HALTED" not in message


def test_readonly_message_differs_from_halt():
    level, message = _critical_message("readonly")
    assert level is BreakLevel.CRITICAL
    assert "Read-only mode active; mutations HALTED." in message
    assert message != _critical_message("halt")[1]


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_ui_texts_do_not_claim_critical_stops_replies_or_all_tools(lang):
    catalog = json.loads((CATALOGS / f"catalog.{lang}.json").read_text(encoding="utf-8"))
    for key in ("hub.crit_desc", "observer.critical_detail", "hub.tools_desc"):
        text = catalog[key]
        for wrong in (
            "応答を停止",
            "すべてのツールを止め",
            "自律ターンを停止",
            "replies halt",
            "all tools stop",
            "Autonomous turns must stop",
        ):
            assert wrong not in text, (key, wrong)
    # #301: the unused keys that said "CRITICAL stops all tools" are removed.
    assert "settings.tools_hint" not in catalog
    assert "tools.breaker_hint" not in catalog


# --- #301: the texts are checked against what the runtime enforces ----------------------


def _real_behaviour():
    """What each breaker level does to a read and a write tool, from the policy layer."""
    from stiracore.tools import tool_policy

    def decide(level, action, category):
        return tool_policy.breaker_layer(level, category, action)["decision"]

    return {
        ("warning", "read"): decide("warning", "readonly", "read"),
        ("warning", "write"): decide("warning", "readonly", "write"),
        ("critical-readonly", "read"): decide("critical", "readonly", "read"),
        ("critical-readonly", "write"): decide("critical", "readonly", "write"),
        ("critical-halt", "read"): decide("critical", "halt", "read"),
        ("critical-halt", "write"): decide("critical", "halt", "write"),
    }


def test_the_runtime_facts_the_texts_must_match():
    from stiracore.tools import tool_policy

    real = _real_behaviour()
    allow, prompt, forbidden = tool_policy.ALLOW, tool_policy.PROMPT, tool_policy.FORBIDDEN
    assert real == {
        ("warning", "read"): allow,
        ("warning", "write"): forbidden,
        ("critical-readonly", "read"): allow,
        ("critical-readonly", "write"): forbidden,
        ("critical-halt", "read"): allow,
        ("critical-halt", "write"): prompt,
    }


_READS = {"ja": "読み取り", "en": "Reads"}
_CONTINUE = {"ja": "続", "en": "continue"}
_APPROVAL = {"ja": "承認", "en": "approval"}
_WRITES = {"ja": "書き込み・実行系", "en": "write/exec"}


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_critical_texts_state_reads_continue_and_writes_stop_or_need_approval(lang):
    """Each CRITICAL text states the three facts: reads continue, writes/exec are stopped
    (read-only) or need a human approval per call (halt)."""
    catalog = json.loads((CATALOGS / f"catalog.{lang}.json").read_text(encoding="utf-8"))
    for key in ("hub.crit_desc", "observer.critical_detail"):
        text = catalog[key]
        assert _READS[lang].lower() in text.lower() and _CONTINUE[lang] in text, key
        assert _WRITES[lang] in text, key
        assert _APPROVAL[lang] in text, key
    for key in ("hub.warn_desc", "observer.warning_detail"):
        text = catalog[key]
        assert _WRITES[lang] in text, key
        assert "承認" not in text and "approval" not in text, key  # WARNING has no approval route
    # WARNING has no confirmation route (ADR 0010 §2): the titles do not promise one.
    assert "confirmation" not in catalog["observer.warning_title"].lower()
    assert "確認" not in catalog["observer.warning_title"]
    assert "optimization" not in catalog["observer.critical_title"].lower()
    assert "最適化" not in catalog["observer.critical_title"]
    # The per-tool lines of the tool stack say the same, level by level.
    assert _APPROVAL[lang] in catalog["policy.reason_breaker_critical_halt"]
    assert _READS[lang].lower() in catalog["policy.reason_breaker_critical_read"].lower()
    assert _READS[lang].lower() in catalog["policy.reason_breaker_warning_read"].lower()


_WRONG_CLAIMS = (
    r"応答を停止",
    r"応答は止まります",
    r"replies halt",
    r"Autonomous turns must stop",
    r"自律ターンを停止",
    r"Execution HALTED",
    r"CRITICAL.*all tools",
    r"CRITICAL 以上で全ツール",
)


def test_no_catalog_text_or_source_claims_that_critical_halts_replies_or_reads():
    """Every catalog value and every UI/engine source file, not just the keys fixed in #301."""
    root = Path(__file__).resolve().parent.parent
    for lang in ("ja", "en"):
        catalog = json.loads((CATALOGS / f"catalog.{lang}.json").read_text(encoding="utf-8"))
        for key, text in catalog.items():
            for wrong in _WRONG_CLAIMS:
                assert not re.search(wrong, text), (lang, key, wrong)
        # "Every tool stopped" is true only as the fail-closed line for an unknown action.
        for key, text in catalog.items():
            if "すべてのツールを止め" in text or "every tool is stopped" in text:
                assert key == "policy.reason_breaker_critical", (lang, key)
    sources = [
        *(root / "src" / "stiracore").rglob("*.py"),
        *(root / "dashboard").rglob("*.js"),
        *(root / "dashboard" / "templates").glob("*.html"),
    ]
    for path in sources:
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for wrong in ("Execution HALTED", "Autonomous turns must stop"):
            assert wrong not in text, (path, wrong)


def test_the_two_critical_log_lines_differ_and_say_what_happens():
    _, halt = _critical_message("halt")
    _, readonly = _critical_message("readonly")
    assert "read tools keep running" in halt and "human approval" in halt
    assert "Read-only" in readonly and "approval" not in readonly
    for message in (halt, readonly):
        assert "stability=0.20" in message and "Recovery requires" in message


# --- #301 (rest): the WARNING log line and model notice match what WARNING enforces --------


def _warning_engine() -> tuple[StiraEngine, list[tuple[BreakLevel, str]]]:
    fired: list[tuple[BreakLevel, str]] = []
    engine = StiraEngine(
        state=NeuralState(stability=0.4),
        on_breaker=lambda level, message: fired.append((level, message)),
    )
    assert engine.pre_turn().level is BreakLevel.WARNING
    return engine, fired


def test_warning_log_line_says_writes_are_withheld_and_reads_continue():
    _, fired = _warning_engine()
    assert fired == [
        (
            BreakLevel.WARNING,
            (
                "⚠️ WARNING: stability=0.40. Write/exec tools withheld; reads keep running. "
                "Recovery requires stability > 0.6."
            ),
        )
    ]
    # WARNING has no confirmation route (ADR 0010 §2): the line does not promise one.
    assert "onfirmation" not in fired[0][1]


def test_warning_notice_to_the_model_matches_the_withheld_tools():
    engine, _ = _warning_engine()
    notice = engine.identity_injection()
    assert notice.endswith(
        "WARNING: Write and exec tools are unavailable until stability recovers; "
        "read tools keep working."
    )
    assert "onfirmation" not in notice
    assert _real_behaviour()[("warning", "write")] != _real_behaviour()[("warning", "read")]
