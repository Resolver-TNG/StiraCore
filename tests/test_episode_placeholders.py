"""Fixed words inside an episode line follow ``episode_labels.placeholders`` (#653)."""

from __future__ import annotations

from stiracore.tools.episodic_formatter import (
    EpisodicFormatter,
    clean_episode_labels,
    fallback_format,
)

CODE = "see\n```python\nx = 1\ny = 2\n```\nok"


def test_default_keeps_japanese_words():
    content = EpisodicFormatter("U", "A").format("", CODE).content
    assert "(空)" in content
    assert "[コード: python 2行 省略]" in content


def test_en_words():
    content = EpisodicFormatter("U", "A", placeholders="en").format("", CODE).content
    assert "(empty)" in content
    assert "[code: python 2 lines omitted]" in content
    assert "空" not in content and "コード" not in content


def test_fallback_en_and_default():
    assert "[code omitted]" in fallback_format("q", CODE, placeholders="en").content
    assert "[コード 省略]" in fallback_format("q", CODE).content
    assert "(空)" in fallback_format("", "a").content


def test_clean_accepts_only_known_languages():
    assert clean_episode_labels({"placeholders": "en"}) == {"placeholders": "en"}
    assert clean_episode_labels({"placeholders": "fr"}) == {}
    assert clean_episode_labels({"placeholders": ["en"]}) == {}
    assert clean_episode_labels({"user": "U", "placeholders": "en"}) == {
        "user": "U",
        "placeholders": "en",
    }
