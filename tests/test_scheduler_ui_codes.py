"""#279: /schedules words server codes from the catalog (code + allowlisted variables).

The scheduler sends codes (alert keys, run reasons, notification ``code``) and, for
notifications, ``params`` (``count`` / ``tool`` / ``kind`` / ``limit``, from an allowlist,
no free text). The ``message`` sentence stays as masked audit text. These tests keep the
Python code sets, the JS tables and both catalogs in step, so a new code cannot reach the
page unworded.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from test_scheduler import HUMAN, T0, Clock, _job, _make, _tick_at

from stiracore.scheduler import scheduler as sched_module
from stiracore.scheduler.scheduler import (
    ALERT_CODES,
    BUDGET_KINDS,
    NOTIFICATION_CODES,
    RUN_REASON_CODES,
    _budget_params,
    _note_params,
)

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "dashboard" / "static" / "js" / "schedules_page.js").read_text(encoding="utf-8")
CATALOGS = {
    lang: json.loads(
        (ROOT / "dashboard" / "static" / "i18n" / f"catalog.{lang}.json").read_text("utf-8")
    )
    for lang in ("ja", "en")
}


def _js_table(name: str) -> dict[str, str]:
    body = re.search(rf"const {name} = \{{(.*?)\n  \}};", JS, re.DOTALL).group(1)
    return dict(re.findall(r'"?(\w+)"?: "([\w.]+)"', body))


def test_allowlist_keeps_only_shaped_values():
    assert _note_params(
        {"count": 3, "limit": 20, "kind": "max_runs_per_day", "tool": "file_write"}
    ) == {
        "count": 3,
        "limit": 20,
        "kind": "max_runs_per_day",
        "tool": "file_write",
    }
    secret = "sk-" + "a" * 40
    dropped = _note_params(
        {
            "count": -1,
            "limit": True,
            "kind": "free text",
            "tool": secret,
            "message": "free sentence",
            "job_id": "x",
        }
    )
    assert dropped == {}
    assert _note_params({"tool": "rm -rf /"}) == {}  # not an identifier
    assert _note_params("text") == {} and _note_params(None) == {}


def test_budget_reason_sentences_become_kind_and_limit():
    assert _budget_params("max_runs_per_day (2) reached") == {
        "kind": "max_runs_per_day",
        "limit": 2,
    }
    assert _budget_params("max_tokens_per_day (2000) would be exceeded") == {
        "kind": "max_tokens_per_day",
        "limit": 2000,
    }
    assert _budget_params("daily_token_budget (9) would be exceeded")["kind"] == (
        "daily_token_budget"
    )
    assert _budget_params("max_tokens_per_run reached during a run") == {
        "kind": "max_tokens_per_run"
    }
    assert _budget_params("something else") == {}


def test_job_failing_budget_and_approval_notifications_carry_params(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(budget={"max_runs_per_day": 1}), by=HUMAN, confirm=True)
    probe.fail = True
    for minute in (5, 10, 15):
        _tick_at(scheduler, clock, T0 + minute * 60)
    by_code = {}
    for note in scheduler.notifications():
        by_code.setdefault((note["code"], note["state"]), note)
    budget = by_code[("budget_exhausted", "event")]
    assert budget["params"] == {"kind": "max_runs_per_day", "limit": 1}
    assert budget["job_id"] == "probe"
    # Every notification has a params object (older log entries give {}).
    assert all(isinstance(n["params"], dict) for n in scheduler.notifications())
    scheduler.stop()


def test_failure_count_is_a_variable_and_the_sentence_stays(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock, failure_alert_after=2)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    probe.fail = True
    for minute in (5, 10):
        _tick_at(scheduler, clock, T0 + minute * 60)
    raised = [n for n in scheduler.notifications() if n["code"] == "job_failing"]
    assert [n["params"] for n in raised] == [{"count": 2}]
    assert raised[0]["message"] == "job probe failed 2 times in a row"  # audit text kept
    scheduler.stop()


def test_notification_params_survive_a_restart_and_are_sanitized_on_replay(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock, failure_alert_after=1)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    probe.fail = True
    _tick_at(scheduler, clock, T0 + 300)
    scheduler.stop()
    restarted, _ = _make(tmp_path, clock, failure_alert_after=1)
    notes = [n for n in restarted.notifications() if n["code"] == "job_failing"]
    assert notes and notes[0]["params"] == {"count": 1}
    restarted.stop()


def test_every_code_the_scheduler_raises_is_in_the_code_sets():
    tree = ast.parse(Path(sched_module.__file__).read_text(encoding="utf-8"))
    notified: set[str] = set()
    alerted: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        args = node.args
        if node.func.attr == "_notify" and args and isinstance(args[0], ast.Constant):
            notified.add(str(args[0].value))
        if node.func.attr == "_alert" and len(args) > 1 and isinstance(args[1], ast.Constant):
            alerted.add(args[1].value)
    assert alerted and alerted <= ALERT_CODES
    assert notified and notified <= NOTIFICATION_CODES
    assert ALERT_CODES <= NOTIFICATION_CODES


def test_every_code_is_worded_in_the_page_and_both_catalogs():
    notes = _js_table("NOTE_KEYS")
    reasons = _js_table("REASON_KEYS")
    assert set(notes) == NOTIFICATION_CODES
    assert set(reasons) == RUN_REASON_CODES
    for lang, catalog in CATALOGS.items():
        for key in [*notes.values(), *reasons.values()]:
            assert catalog.get(key), (lang, key)
        for kind in BUDGET_KINDS:
            assert catalog.get(f"schedules.budget_kind_{kind}"), (lang, kind)
        assert catalog.get("schedules.other_code"), lang
        assert catalog.get("schedules.other"), lang


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_unknown_code_wording_shows_the_code_inside_a_sentence(lang):
    text = CATALOGS[lang]["schedules.other_code"]
    assert "{code}" in text
    assert text.strip() != "{code}"  # never the bare code
