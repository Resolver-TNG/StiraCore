"""#301: breaker texts on the settings page do not say the agent or its replies stop.

At every breaker level the model is still called and replies (CRITICAL ``halt`` asks a
human for each write/exec call; ``readonly`` removes write/exec tools). Whether reads
also stop at CRITICAL is still open (#270), so these texts only say what holds either
way: write/exec are restricted, replies continue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "dashboard" / "static" / "i18n"
SETTINGS = ROOT / "dashboard" / "templates" / "settings.html"
KEYS = ("hub.safety_desc", "hub.breaker_title", "hub.breaker_desc")
# Wording that said the agent itself is stopped (before #301 follow-up).
STOPS_THE_AGENT = {
    "ja": ("止める仕組み", "自動停止", "早めに止まり", "応答を停止", "すべてのツールを止め"),
    "en": ("the agent is stopped", "stop thresholds", "stop earlier", "stops responding"),
}


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_breaker_texts_do_not_say_the_agent_stops(lang):
    catalog = json.loads((I18N / f"catalog.{lang}.json").read_text(encoding="utf-8"))
    for key in KEYS:
        text = catalog[key]
        for phrase in STOPS_THE_AGENT[lang]:
            assert phrase not in text, (key, phrase)
    assert ("止まりません" if lang == "ja" else "does not stop replies") in catalog[
        "hub.safety_desc"
    ]


def test_settings_template_fallbacks_match_the_catalog():
    catalog = json.loads((I18N / "catalog.ja.json").read_text(encoding="utf-8"))
    html = SETTINGS.read_text(encoding="utf-8")
    for key in KEYS:
        assert catalog[key] in html, key
    for phrase in STOPS_THE_AGENT["ja"]:
        assert phrase not in html, phrase
