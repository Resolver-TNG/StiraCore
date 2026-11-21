"""#476 / #195: source checks of the settings page that run without a browser.

The behaviour is driven in jsdom by ``tests/test_settings_model_usage_ui.py`` (skipped
where jsdom is not installed); these checks keep the contract visible everywhere: every
refusal code of the model save has a sentence in both catalogs, the page shows no server
sentence, and the usage card exists and says "unknown" for a count nobody reported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from stiracore.observability import config_api

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
JS = (DASH / "static" / "js" / "settings.js").read_text(encoding="utf-8")
HTML = (DASH / "templates" / "settings.html").read_text(encoding="utf-8")


def _catalogs() -> dict[str, dict[str, str]]:
    return {
        lang: json.loads((DASH / "static" / "i18n" / f"catalog.{lang}.json").read_text("utf-8"))
        for lang in ("ja", "en")
    }


def test_every_refusal_code_has_a_sentence_in_both_catalogs():
    catalogs = _catalogs()
    for code in config_api._SELECTION_MESSAGES:
        match = re.search(
            rf'^\s+{code}: \([a-z]*\) => (?:t\("(mset\.err_\w+)"|reasonText\()', JS, re.MULTILINE
        )
        assert match, f"settings.js has no text for {code}"
        if match.group(1):
            for lang, catalog in catalogs.items():
                assert match.group(1) in catalog, (lang, code)


def test_the_page_shows_no_server_sentence():
    # The server's ``error`` is English free text: the page maps codes instead (#330).
    assert re.search(r"data\.error(?!_code)", JS) is None
    assert "error.message" not in JS
    assert 't("mset.err_unknown"' in JS


def test_the_save_carries_the_loaded_version_and_the_new_fields():
    assert "base_version: providerData.version" in JS
    for element in ("custom-region", "custom-organization-env", "region-pinned", "model-error"):
        assert f'id="{element}"' in HTML, element
        assert f'$("{element}")' in JS, element
    # Names only: the two inputs are plain text fields and never a password box.
    assert 'type="password"' not in HTML


def test_the_usage_card_is_on_the_page():
    catalogs = _catalogs()
    assert '"/api/observability/usage"' in JS
    for element in ("sec-usage", "usage-table", "usage-rows", "usage-status", "usage-refresh"):
        assert f'id="{element}"' in HTML, element
    assert 'href="#sec-usage"' in HTML
    keys = set(re.findall(r't\("(usage\.\w+)"', JS)) | set(
        re.findall(r'data-i18n="(usage\.\w+)"', HTML)
    )
    assert {"usage.unknown", "usage.total", "usage.unavailable"} <= keys
    for lang, catalog in catalogs.items():
        assert keys <= set(catalog), (lang, sorted(keys - set(catalog)))
    # A count nobody reported is never turned into a number.
    assert "?? 0" not in JS and "|| 0" not in JS
