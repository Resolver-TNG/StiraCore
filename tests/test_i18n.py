"""UI i18n catalogs (#184): consistency, coverage and server-side embedding."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask

from stiracore.observability.i18n import LANGS, install_i18n, load_catalogs

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
CATALOG_DIR = DASH / "static" / "i18n"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture(scope="module")
def catalogs():
    return load_catalogs(CATALOG_DIR)


def test_catalogs_have_identical_keys(catalogs):
    assert set(catalogs["ja"]) == set(catalogs["en"])
    assert len(catalogs["ja"]) > 100


def test_placeholders_match_between_languages(catalogs):
    for key in catalogs["ja"]:
        ja = set(PLACEHOLDER.findall(catalogs["ja"][key]))
        en = set(PLACEHOLDER.findall(catalogs["en"][key]))
        assert ja == en, f"{key}: placeholders differ ja={ja} en={en}"


def test_english_is_not_untranslated_japanese(catalogs):
    cjk = re.compile("[぀-ヿ㐀-鿿]")
    allowed = {"base.language"}  # selector option labels live outside the catalog
    # Stage (#341): the language switch names the other language in its own script
    allowed |= {"stage.lang_toggle", "stage.hint", "stage.hint_live"}
    for key, text in catalogs["en"].items():
        if key in allowed:
            continue
        assert not cjk.search(text), f"{key}: Japanese text in the English catalog: {text!r}"


def test_extractor_check_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "i18n_extract.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_bilingual_english_spans_left_in_stira_templates():
    for name in ("base", "chat", "settings", "observability"):
        html = (DASH / "templates" / f"{name}.html").read_text(encoding="utf-8")
        assert '<span lang="en">' not in html, name


def test_install_i18n_embeds_catalog_and_language_selector():
    app = Flask(
        __name__, template_folder=str(DASH / "templates"), static_folder=str(DASH / "static")
    )
    catalogs = install_i18n(app)

    @app.get("/x")
    def page():
        from flask import render_template

        return render_template("settings.html")

    # settings.html needs the config blueprint for url_for; fall back to base-only render.
    with app.test_request_context("/x"):
        from flask import render_template_string

        html = render_template_string('{% extends "base.html" %}{% block content %}x{% endblock %}')
    assert 'id="i18n-catalog"' in html
    assert 'id="app-lang"' in html
    assert '<html lang="ja">' in html
    for lang in LANGS:
        assert catalogs[lang]["base.chat"] in html or lang == "en"
    assert "チャット" in html  # server-rendered nav keeps the Japanese default


def test_mismatched_catalogs_are_rejected(tmp_path):
    (tmp_path / "catalog.ja.json").write_text('{"a": "あ", "b": "い"}', encoding="utf-8")
    (tmp_path / "catalog.en.json").write_text('{"a": "A"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalogs(tmp_path)
