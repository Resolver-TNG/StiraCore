"""Opt-in Japanese hybrid classifier (#164): mapping rules, safety bounds, fallback, no text leaks.

Most tests inject a fake ML-Ask analyzer so they run without the optional
``ja-rules`` extra. Tests marked with ``mlask`` in their name need the real
``mlask_official`` (skipped when it is not installed).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stiracore.agent import stimulus_classifier as sc
from stiracore.agent.ja_hybrid import JaHybridClassifier, mapping, patterns
from stiracore.agent.ja_hybrid import classifier as jc
from stiracore.core.neural_state import EMOTION_MATRIX

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "eval" / "jp-stimulus"
OUT = DATA / "ja-hybrid"

try:  # Python 3.11+
    import re._parser as sre_parse  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import sre_parse  # type: ignore[no-redef]

MLASK_CLASSES = {
    "yorokobi",
    "suki",
    "yasu",
    "takaburi",
    "odoroki",
    "haji",
    "aware",
    "iya",
    "kowa",
    "ikari",
}


class FakeMLAsk:
    """Minimal stand-in for MLAskOfficial: keyword -> emotion class."""

    def __init__(self, table: dict[str, str] | None = None):
        self.table = table or {}
        self.calls: list[int] = []

    def analyze(self, text: str) -> dict:
        self.calls.append(len(text))
        found: dict[str, list[str]] = {}
        for word, cls in self.table.items():
            if word in text:
                found.setdefault(cls, []).append(word)
        return {"emotion": found or None, "intensifier": {}, "text": text}


def _clf(table: dict[str, str] | None = None, **kw) -> JaHybridClassifier:
    return JaHybridClassifier(analyzer=FakeMLAsk(table), **kw)


# --- data tables -----------------------------------------------------------------


def test_tables_only_use_existing_categories_and_known_classes():
    cats = set(EMOTION_MATRIX)
    assert set(mapping.TIE_ORDER) == cats
    assert set(mapping.MLASK_VOTES) == MLASK_CLASSES
    for votes in (*mapping.MLASK_VOTES.values(), *mapping.TAG_VOTES.values()):
        assert set(votes) <= cats
    assert set(mapping.CUE_WEIGHT) == cats == set(patterns.CUES)
    assert set(mapping.TAG_VOTES) == set(patterns.DISCOURSE_TAGS)
    assert patterns.DISCOURSE_TAGS == (
        "failure",
        "frustration",
        "criticism",
        "pressure",
        "resolved",
        "escalated",
    )
    for if_tags, zero, _name, _unless in mapping.SUPPRESS:
        assert if_tags <= set(patterns.DISCOURSE_TAGS) and zero <= cats
    for key in mapping.CUE_RULE_WEIGHT:
        cat, rule = key.split("/")
        assert rule in {n for n, _ in patterns.CUES[cat]}


def test_criticism_and_pressure_vote_for_no_category():
    # The 6 categories cannot represent them; they are reported as tags only.
    assert mapping.TAG_VOTES["criticism"] == {} and mapping.TAG_VOTES["pressure"] == {}


@pytest.mark.parametrize(("table", "name", "rx"), patterns.all_patterns())
def test_every_pattern_has_a_bounded_width(table, name, rx):
    """ReDoS guard: no unbounded quantifier, so a search is linear in the input."""
    _lo, hi = sre_parse.parse(rx).getwidth()
    assert hi <= 200, f"{table}/{name} can match unboundedly ({hi})"
    assert "(?=" not in rx and "(?!" not in rx.replace("(?![a-z])", "")
    assert "\\1" not in rx


def test_rule_names_are_fixed_labels():
    for _table, name, _rx in patterns.all_patterns():
        assert name.isascii() and " " not in name


# --- mapping rules (fake ML-Ask) ---------------------------------------------------


def test_failure_is_not_loss():
    clf = _clf({"がっかり": "aware"})
    res = clf.classify("ビルドがまた落ちた、がっかり")
    assert res is None or res.category != "loss"
    assert "failure" in clf.last_verdict.discourse_tags
    assert "suppress:failure-is-not-loss" in clf.last_verdict.evidence


def test_sadness_without_failure_is_loss():
    clf = _clf({"悲しい": "aware"})
    res = clf.classify("祖母が亡くなって悲しい")
    assert res.category == "loss" and res.discourse_tags == ()


def test_resolved_threat_is_not_threat():
    clf = _clf()
    res = clf.classify("侵入はもう止まっていた")
    assert res is None
    assert clf.last_verdict.discourse_tags == ("resolved",)
    assert "suppress:resolved-is-not-threat" in clf.last_verdict.evidence


@pytest.mark.parametrize(
    "text",
    [
        "障害は復旧したが別の侵入が進行中",
        "侵入はもう止まっていたがまた始まった",
        "障害は復旧した。しかし不正アクセスが続いている",
        "停電は復旧したけど、火事が広がっている",
    ],
)
def test_resolution_does_not_hide_a_still_active_threat(text):
    """Per clause: a threat or escalation outside the resolved clause keeps threat."""
    clf = _clf()
    res = clf.classify(text)
    assert res is not None and res.category == "threat"
    v = clf.last_verdict
    assert "active-threat" in v.evidence
    assert "suppress:resolved-is-not-threat" not in v.evidence
    assert "resolved" in v.discourse_tags


def test_escalation_is_not_dropped_by_an_earlier_resolution():
    clf = _clf()
    res = clf.classify("障害は復旧したが別の侵入が進行中")
    assert "escalated" in res.discourse_tags


@pytest.mark.parametrize(
    "text",
    [
        "障害が発生しましたが、復旧しました",
        "例の障害の件ですが、無事に復旧しました",
        "火事が広がっていたが鎮火した",
    ],
)
def test_threat_followed_by_its_resolution_is_resolved(text):
    clf = _clf()
    assert clf.classify(text) is None
    assert "suppress:resolved-is-not-threat" in clf.last_verdict.evidence


def test_escalation_inside_the_resolved_clause_is_past():
    clf = _clf()
    assert clf.classify("被害拡大は収束しました") is None
    assert clf.last_verdict.discourse_tags == ("resolved",)


def test_restart_without_a_threat_word_is_not_resolved():
    clf = _clf()
    clf.classify("もう止まっていたがまた始まった")
    assert "resolved" not in clf.last_verdict.discourse_tags


def test_bereavement_stays_loss_next_to_a_failure():
    clf = _clf()
    res = clf.classify("テストも落ちたし、愛犬も亡くなった")
    assert res.category == "loss"
    assert "failure" in res.discourse_tags
    assert "suppress:failure-is-not-loss" not in clf.last_verdict.evidence


def test_stopped_without_a_threat_cue_is_not_resolved():
    clf = _clf()
    clf.classify("デプロイが途中で止まった")
    assert "resolved" not in clf.last_verdict.discourse_tags
    assert "failure" in clf.last_verdict.discourse_tags


def test_escalating_threat_is_threat_with_tag():
    clf = _clf()
    res = clf.classify("不正アクセスの被害がどんどん拡大している")
    assert res.category == "threat"
    assert res.discourse_tags == ("escalated",)


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("使えないな", "criticism"),
        ("君は間違っている", "criticism"),
        ("謝れ", "pressure"),
        ("前の方が素直で良かった", "pressure"),
        ("同意してよ、バグじゃないって言ってよ", "pressure"),
    ],
)
def test_criticism_and_pressure_abstain_but_keep_tags(text, tag):
    clf = _clf({"良い": "yorokobi"})
    assert clf.classify(text) is None
    assert tag in clf.last_verdict.discourse_tags


def test_failure_alone_abstains_and_frustration_makes_threat():
    clf = _clf({"嫌": "iya"})
    assert clf.classify("テストがまた落ちてる") is None
    assert clf.last_verdict.discourse_tags == ("failure",)
    res = clf.classify("テストがまた落ちてる、もう嫌だ")
    assert res.category == "threat"
    assert set(res.discourse_tags) == {"failure", "frustration"}
    assert res.intensity <= 0.8


def test_repetition_plus_irritation_is_frustration():
    clf = _clf({"疲れる": "iya"})
    clf.classify("毎回これが出て疲れる")
    assert "frustration" in clf.last_verdict.discourse_tags


def test_achievement_dominates_joy_and_redo_is_not_a_fix():
    clf = _clf({"嬉しい": "yorokobi"})
    assert clf.classify("試験に合格して嬉しい").category == "achievement"
    res = clf.classify("全部やり直したのにまた落ちた")
    assert res is None or res.category != "achievement"


def test_slang_laughter_alone_is_weak():
    clf = _clf({"草": "yorokobi"})
    assert clf.classify("それは草") is None
    assert clf.last_verdict.scores.get("joy") == pytest.approx(mapping.SLANG_WEIGHT)


def test_request_formula_is_not_joy():
    clf = _clf({"嬉しい": "yorokobi"})
    assert clf.classify("例もあったら嬉しいです") is None


def test_full_width_ascii_is_normalised():
    clf = _clf()
    clf.classify("ＣＩが落ちた")
    assert "failure" in clf.last_verdict.discourse_tags


def test_intensity_steps_are_capped():
    clf = _clf({"幸せ": "yorokobi", "嬉しい": "yorokobi", "楽しい": "suki"})
    res = clf.classify("幸せ！嬉しい！楽しい！最高！")
    assert res.category == "joy" and 0 < res.intensity <= 0.8


# --- protocol, structural signals, English ----------------------------------------


def test_breaker_signal_keeps_rule_semantics():
    clf = _clf()
    res = clf.classify("ありがとう", breaker_result="BreakLevel.WARNING")
    assert (res.category, res.intensity) == ("threat", 0.6)
    assert res.classifier_name == "ja-hybrid"


def test_english_text_uses_the_existing_rules():
    clf = _clf()
    rule = sc.RuleBasedClassifier().classify("why is this happening?")
    res = clf.classify("why is this happening?")
    assert (res.category, res.intensity) == (rule.category, rule.intensity)
    assert clf.last_verdict.fallback == "rule-english"
    assert res.classifier_name == "ja-hybrid:rule-english"


def test_empty_message_abstains():
    clf = _clf()
    assert clf.classify("") is None


def test_discourse_tags_do_not_reach_the_persisted_transcription_or_prompt():
    """The engine must not consume tags yet: they stay out of emotion_transcription."""
    from stiracore.agent.context_assembler import _snapshot_text
    from stiracore.core import NeuralState, StiraConfig, StiraEngine

    clf = _clf({"嫌": "iya"})
    res = clf.classify("テストがまた落ちてる、もう嫌だ")
    assert res.discourse_tags and res.transcription == "ja-hybrid: threat"
    engine = StiraEngine(state=NeuralState(), config=StiraConfig())
    engine.post_turn(
        stimulus=res.category, intensity=res.intensity, transcription=res.transcription
    )
    dumped = json.dumps(engine.state.to_dict(), ensure_ascii=False) + _snapshot_text(engine.state)
    for tag in res.discourse_tags:
        assert tag not in dumped


def test_stimulus_result_default_tags_are_empty():
    assert sc.StimulusResult("joy", 0.4).discourse_tags == ()
    assert sc.RuleBasedClassifier().classify("thanks!").discourse_tags == ()


def test_default_selection_is_unchanged_and_does_not_import_mlask():
    code = (
        "import sys\n"
        "from stiracore.agent.stimulus_classifier import get_classifier\n"
        "c = get_classifier(prefer_sokudan=False)\n"
        "print(type(c).__name__, 'mlask_official' in sys.modules, "
        "'stiracore.agent.ja_hybrid' in sys.modules)\n"
    )
    env_path = f"{ROOT / 'src'}:{ROOT}"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": env_path, "PATH": "/usr/bin:/bin"},
        check=True,
    ).stdout.split()
    assert out[-3:] == ["RuleBasedClassifier", "False", "False"]


def test_patterns_only_mode_never_imports_mlask(monkeypatch):
    def boom(name, *a, **k):
        raise AssertionError("must not import " + name)

    monkeypatch.setattr(jc, "import_module", boom)
    clf = JaHybridClassifier(emotion_layer=False)
    clf.classify("テストがまた落ちた")
    assert clf.status()["emotion_layer"] == "off"
    assert clf.last_verdict.fallback is None


# --- missing mlask: explicit fallback / error -----------------------------------------


def _no_mlask(monkeypatch):
    real = jc.import_module

    def fake(name, *a, **k):
        if name in ("mlask_official", "ipadic"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **k)

    monkeypatch.setattr(jc, "import_module", fake)


def test_missing_mlask_falls_back_to_rule_visibly(monkeypatch, caplog):
    _no_mlask(monkeypatch)
    clf = JaHybridClassifier()
    with caplog.at_level(logging.WARNING, logger=jc.__name__):
        clf.classify("thank you so much")
        clf.classify("ありがとう")
    st = clf.status()
    assert st["fallback"] == "rule" and st["reason"] == "mlask_not_installed"
    assert st["emotion_layer"] == "unavailable"
    assert clf.last_verdict.fallback == "rule"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # once, not every turn
    res = clf.classify("thank you so much")
    assert res.classifier_name == "ja-hybrid:rule-fallback"
    assert res.transcription.startswith("ja-hybrid:rule-fallback -> rule:")


def test_missing_mlask_patterns_mode_keeps_tags(monkeypatch):
    _no_mlask(monkeypatch)
    clf = JaHybridClassifier(on_missing="patterns")
    res = clf.classify("サーバー障害が拡大している")
    assert res.category == "threat" and "[no-mlask]" in res.transcription
    assert clf.last_verdict.fallback == "patterns"
    assert clf.status()["fallback"] == "patterns"


class _Boom(Exception):
    pass


def _broken_analyzer(monkeypatch):
    calls = []

    def build(self):
        calls.append(1)
        raise _Boom("unexpected")

    monkeypatch.setattr(JaHybridClassifier, "_build_analyzer", build)
    return calls


def test_unexpected_init_error_is_unavailable_once(monkeypatch, caplog):
    calls = _broken_analyzer(monkeypatch)
    clf = JaHybridClassifier()
    with caplog.at_level(logging.WARNING, logger=jc.__name__):
        for _ in range(3):
            res = clf.classify("thank you")
    assert len(calls) == 1  # not retried every turn
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1
    st = clf.status()
    assert (st["emotion_layer"], st["fallback"], st["reason"]) == (
        "unavailable",
        "rule",
        "analyzer_init_error",
    )
    assert res.classifier_name == "ja-hybrid:rule-fallback"


def test_unexpected_init_error_raises_in_error_mode(monkeypatch):
    _broken_analyzer(monkeypatch)
    with pytest.raises(RuntimeError, match="analyzer_init_error"):
        JaHybridClassifier(on_missing="error", lazy=False)


def test_missing_mlask_error_mode_raises(monkeypatch):
    _no_mlask(monkeypatch)
    with pytest.raises(RuntimeError, match="ja-rules"):
        JaHybridClassifier(on_missing="error", lazy=False)


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError):
        JaHybridClassifier(on_missing="maybe")
    with pytest.raises(ValueError):
        JaHybridClassifier(backend="sudachi")
    with pytest.raises(ValueError):
        JaHybridClassifier(max_chars=1)


# --- no conversation text anywhere --------------------------------------------------


def test_no_conversation_text_in_outputs_or_logs(caplog):
    secret = "ZQX秘密の合言葉ZQX"
    clf = _clf({"悲しい": "aware"})
    with caplog.at_level(logging.DEBUG):
        res = clf.classify(f"{secret} テストがまた落ちて悲しい、謝れ {secret}")
    blobs = [
        caplog.text,
        res.transcription if res else "",
        json.dumps(clf.last_verdict.as_dict(), ensure_ascii=False),
        json.dumps(clf.status(), ensure_ascii=False),
    ]
    assert all("ZQX" not in b and "合言葉" not in b for b in blobs)


# --- bounded work on long input -------------------------------------------------------


def test_long_input_is_capped_and_chunked():
    fake = FakeMLAsk({"悲しい": "aware"})
    clf = JaHybridClassifier(analyzer=fake, max_chars=4000, chunk_chars=400)
    text = "テストが落ちて悲しい。" * 10_000  # ~110k chars
    t = time.perf_counter()
    clf.classify(text)
    elapsed = time.perf_counter() - t
    assert clf.last_verdict.truncated
    assert max(fake.calls) <= 400
    assert sum(fake.calls) <= 4001
    assert elapsed < 2.0  # generous: measured ~10 ms (patterns on 4k chars)


def test_chunks_split_at_sentence_ends_and_hard_split_long_runs():
    parts = jc.chunks("あ" * 1000, 400)
    assert [len(p) for p in parts] == [400, 400, 200]
    parts = jc.chunks("一文目。二文目！三文目？", 6)
    assert all(len(p) <= 6 for p in parts) and "".join(parts) == "一文目。二文目！三文目？"


@pytest.mark.parametrize(
    "text",
    [
        "また" * 50_000,
        "あ" * 100_000,
        "前の" * 50_000,
        "!" * 100_000,
        "さっきまで" + "あ" * 100_000,
        "(" * 100_000,
    ],
)
def test_adversarial_inputs_stay_fast(text):
    clf = _clf()
    t = time.perf_counter()
    clf.classify(text)
    assert time.perf_counter() - t < 2.0


# --- real ML-Ask (optional extra) ------------------------------------------------------


def test_mlask_real_analyzer_examples():
    pytest.importorskip("mlask_official")
    pytest.importorskip("ipadic")
    clf = JaHybridClassifier(on_missing="error", lazy=False)
    assert clf.status()["emotion_layer"] == "mlask"
    assert clf.classify("今日はめっちゃ幸せ！").category == "joy"
    assert clf.classify("祖父が亡くなって泣けてきた").category == "loss"
    res = clf.classify("ビルドがまた落ちた、もう嫌だ")
    assert res.category == "threat" and "frustration" in res.discourse_tags


def test_mlask_long_input_is_bounded():
    pytest.importorskip("mlask_official")
    pytest.importorskip("ipadic")
    clf = JaHybridClassifier(on_missing="error", lazy=False)
    text = "テストがまた落ちてる。本当に腹が立つし、悲しいし、怖い。" * 3300  # ~100k chars
    t = time.perf_counter()
    clf.classify(text)
    assert time.perf_counter() - t < 2.0  # measured ~30 ms on an M1 Max


def test_mlask_init_and_classify_open_no_socket(monkeypatch):
    pytest.importorskip("mlask_official")
    pytest.importorskip("ipadic")
    import socket

    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    clf = JaHybridClassifier(on_missing="error", lazy=False)
    assert clf.classify("サーバー障害が拡大している").category == "threat"


# --- committed evaluation artefacts ----------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "jp_hybrid_eval", ROOT / "scripts" / "jp_hybrid_eval.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_results_recompute_from_committed_files():
    mod = _load_script()
    committed = json.loads((OUT / "results.json").read_text("utf-8"))
    again = mod.build_results(DATA, OUT, committed["splits"])
    assert again == committed
    assert (OUT / "report.md").read_text("utf-8") == mod.render_report(committed)


def test_committed_patterns_only_outputs_match_the_current_tables():
    """Editing patterns/mapping after the test split was scored shows up here."""
    committed = json.loads((OUT / "outputs.json").read_text("utf-8"))["predictions"]
    utts = [json.loads(x) for x in (DATA / "utterances.jsonl").read_text("utf-8").splitlines()]
    clf = JaHybridClassifier(emotion_layer=False)
    for u in utts:
        r = clf.classify(user_message=u["text"])
        got = {
            "category": r.category if r else "none",
            "intensity": r.intensity if r else None,
            "discourse_tags": list(clf.last_verdict.discourse_tags) if clf.last_verdict else [],
        }
        assert got == committed["ja-hybrid-patterns-only"][u["id"]], u["id"]


def test_mlask_committed_hybrid_outputs_reproduce():
    pytest.importorskip("mlask_official")
    pytest.importorskip("ipadic")
    committed = json.loads((OUT / "outputs.json").read_text("utf-8"))
    from importlib import metadata

    if metadata.version("mlask-official") != committed["versions"]["mlask-official"]:
        pytest.skip("different mlask-official version than the committed run")
    utts = [json.loads(x) for x in (DATA / "utterances.jsonl").read_text("utf-8").splitlines()]
    clf = JaHybridClassifier(on_missing="error", lazy=False)
    for u in utts:
        r = clf.classify(user_message=u["text"])
        cat = r.category if r else "none"
        assert cat == committed["predictions"]["ja-hybrid"][u["id"]]["category"], u["id"]


def test_report_never_copies_utterance_text():
    report = (OUT / "report.md").read_text("utf-8")
    utts = [json.loads(x) for x in (DATA / "utterances.jsonl").read_text("utf-8").splitlines()]
    for u in utts:
        assert u["text"][:20] not in report


def test_test_split_needs_final_flag():
    mod = _load_script()
    assert mod.main(["report", "--splits", "dev,test", "--print-only"]) == 2
