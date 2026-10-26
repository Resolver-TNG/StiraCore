"""JP stimulus mini-set (#164): schema, agreement maths, classifier scoring, committed data.

The committed labels are model-pair drafts (not human blind labels). These tests
check the arithmetic and that the committed artefacts are consistent and
reproducible; they do not judge whether any label is right.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from stiracore.eval import stimulus_labels as sl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "eval" / "jp-stimulus"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "jp_stimulus_labels", ROOT / "scripts" / "jp_stimulus_labels.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(i, label, core6=None, polarity="none", reason="r"):
    return {
        "id": i,
        "label": label,
        "core6": core6 or label,
        "polarity": polarity,
        "reason": reason,
    }


# --- kappa ---------------------------------------------------------------------


def test_kappa_textbook_2x2():
    # 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no: po=0.7, pe=0.5 -> kappa=0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert sl.cohen_kappa(a, b) == pytest.approx(0.4)


def test_kappa_perfect_chance_and_undefined():
    assert sl.cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == pytest.approx(1.0)
    assert sl.cohen_kappa(["a", "b"], ["b", "a"]) == pytest.approx(-1.0)
    assert sl.cohen_kappa(["a", "a"], ["a", "a"]) is None  # pe == 1
    assert sl.cohen_kappa([], []) is None
    with pytest.raises(ValueError):
        sl.cohen_kappa(["a"], [])


def test_kappa_interval_contains_point_and_is_seeded():
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    ci = sl.kappa_interval(a, b, reps=300, seed=0)
    assert ci["low"] < 0.4 < ci["high"]
    assert ci == sl.kappa_interval(a, b, reps=300, seed=0)


def test_bands_are_the_preregistered_thresholds():
    assert sl.kappa_band(0.6) == "adopt-as-draft-reference"
    assert sl.kappa_band(0.5999) == "descriptive-only-revise-definitions"
    assert sl.kappa_band(0.4) == "descriptive-only-revise-definitions"
    assert sl.kappa_band(0.3999) == "do-not-use"
    assert sl.kappa_band(None) == "undefined"


def test_specific_agreement_and_confusion():
    a = ["x", "x", "y", "y"]
    b = ["x", "y", "y", "y"]
    psa = sl.specific_agreement(a, b, ["x", "y"])
    assert psa["x"] == {"both": 1, "n_a": 2, "n_b": 1, "psa": pytest.approx(2 / 3)}
    assert psa["y"]["psa"] == pytest.approx(4 / 5)
    m = sl.confusion(a, b, ["x", "y"])
    assert m == {"x": {"x": 1, "y": 1}, "y": {"x": 0, "y": 2}}


def test_reference_keeps_only_agreement():
    la = {"u1": _rec("u1", "failure", "loss"), "u2": _rec("u2", "joy")}
    lb = {
        "u1": _rec("u1", "frustration", "loss"),
        "u2": _rec("u2", "joy"),
        "u3": _rec("u3", "none"),
    }
    ref = sl.reference_labels(la, lb, ["u1", "u2", "u3", "u4"])
    assert ref["u1"] == {"label": None, "core6": "loss", "polarity": "none"}
    assert ref["u2"]["label"] == "joy"
    assert ref["u3"] == {"label": None, "core6": None, "polarity": None}  # one labeler only
    assert ref["u4"] == {"label": None, "core6": None, "polarity": None}  # neither: needs human
    assert sorted(ref) == ["u1", "u2", "u3", "u4"]  # the population, not the labeled ids


def test_reference_population_rejects_extra_and_duplicates():
    la = {"u1": _rec("u1", "joy"), "u9": _rec("u9", "joy")}
    with pytest.raises(sl.LabelError, match="outside the population"):
        sl.reference_labels(la, {}, ["u1"])
    with pytest.raises(sl.LabelError, match="duplicate utterance ids"):
        sl.reference_labels({}, {}, ["u1", "u1"])


def test_index_labels_rejects_duplicate_and_extra_ids():
    recs = [_rec("u1", "joy"), _rec("u2", "loss")]
    assert set(sl.index_labels(recs, ["u1", "u2", "u3"])) == {"u1", "u2"}
    with pytest.raises(sl.LabelError, match="duplicate id"):
        sl.index_labels([*recs, _rec("u1", "none")], ["u1", "u2"])
    with pytest.raises(sl.LabelError, match="not in the utterance population"):
        sl.index_labels(recs, ["u1"])


def test_kappa_interval_rejects_length_mismatch():
    with pytest.raises(ValueError):
        sl.kappa_interval(["a", "b"], ["a"], reps=10)


# --- schema --------------------------------------------------------------------


def test_parse_batch_accepts_fenced_json_and_checks_ids():
    reply = "```json\n" + json.dumps({"items": [_rec("u1", "pressure", "none")]}) + "\n```"
    assert sl.parse_batch(reply, ["u1"])[0]["label"] == "pressure"
    with pytest.raises(sl.LabelError, match="missing"):
        sl.parse_batch(reply, ["u1", "u2"])
    with pytest.raises(sl.LabelError, match="no JSON"):
        sl.parse_batch("sorry", ["u1"])


def test_extract_json_ignores_backticks_inside_values():
    # A reply quoting code-mixed text (```) must not be cut at the inner fence.
    reply = json.dumps({"items": [{"style": 7, "text": "```py\nx = 1\n``` が落ちる"}]})
    assert sl.extract_json(reply)["items"][0]["style"] == 7


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "u1", "label": "anger", "core6": "none", "polarity": "none", "reason": ""},
        {"id": "u1", "label": "none", "core6": "failure", "polarity": "none", "reason": ""},
        {"id": "u1", "label": "none", "core6": "none", "polarity": "up", "reason": ""},
        {"id": "u1", "label": "joy", "core6": "loss", "polarity": "none", "reason": ""},
        {"id": "u1", "label": "joy", "core6": "joy", "polarity": "none"},
    ],
)
def test_validate_record_rejects_off_schema(bad):
    with pytest.raises(sl.LabelError):
        sl.validate_record(bad)


def test_label_space():
    assert set(sl.CORE6) < set(sl.LABELS)
    assert {"failure", "frustration", "criticism", "pressure", "threat_resolved"} <= set(sl.LABELS)
    assert {v[0] for v in sl.GEN_TARGETS.values()} <= set(sl.LABELS)


def test_find_pii_returns_kinds_and_counts_only():
    hits = sl.find_pii("連絡は a.b@corp.jp か c@corp.jp まで")
    assert hits == {"email": 2}
    assert "corp" not in json.dumps(hits)  # the matched string is never copied
    assert sl.find_pii("テストがまた落ちてる") == {}


@pytest.mark.parametrize(
    "url,flagged",
    [
        ("https://example.com/x", False),
        ("http://localhost:8000/a", False),
        ("https://docs.example.org/p", False),
        ("https://example.com.evil.test/x", True),  # host boundary, not a prefix match
        ("https://example.com@evil.test/x", True),  # userinfo trick: host is evil.test
        ("https://notexample.com/x", True),
        ("https://evil.test/?u=https://example.com", True),
    ],
)
def test_find_pii_url_host_boundary(url, flagged):
    assert ("url" in sl.find_pii(f"see {url} now")) is flagged


def test_redact_pii():
    text = "key AKIA" + "ABCDEFGHIJKLMNOP" + " mail a@corp.jp ok https://example.com/x"
    red = sl.redact_pii(text)
    assert "ABCDEFGHIJKLMNOP" not in red and "a@corp.jp" not in red
    assert "[REDACTED:aws_key_id]" in red and "[REDACTED:email]" in red
    assert red.endswith("ok https://example.com/x")


# --- classifier scoring --------------------------------------------------------


def test_precision_recall_and_detection():
    ref = ["joy", "joy", "none", "loss"]
    pred = ["joy", "none", "joy", "loss"]
    rows = sl.precision_recall(ref, pred, ["joy", "loss", "none", "threat"])
    assert rows["joy"]["precision"] == pytest.approx(0.5)
    assert rows["joy"]["recall"] == pytest.approx(0.5)
    assert rows["loss"]["f1"] == pytest.approx(1.0)
    assert rows["threat"] == {
        "tp": 0,
        "n_pred": 0,
        "n_ref": 0,
        "precision": None,
        "recall": None,
        "f1": None,
    }
    assert sl.macro(rows, "recall", ["joy", "loss", "threat"]) == pytest.approx(0.75)
    det = sl.detection_scores(ref, pred)
    assert det["precision"] == pytest.approx(2 / 3)
    assert det["recall"] == pytest.approx(2 / 3)


def test_human_sample_is_stratified_and_seeded():
    ref = {
        f"u{i:02d}": {"label": ["joy", "loss", None][i % 3], "core6": None, "polarity": None}
        for i in range(30)
    }
    s = sl.human_sample(ref)
    assert len(s["agreed"]) == 14 and len(s["disagreed"]) == 6
    assert all(ref[i]["label"] is None for i in s["disagreed"])
    labels = [ref[i]["label"] for i in s["agreed"]]
    assert abs(labels.count("joy") - labels.count("loss")) <= 1
    assert s == sl.human_sample(ref)


# --- committed artefacts -------------------------------------------------------


def _jsonl(path):
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


# --- script: Bedrock gate, call budget, PII handling (fake client, no network) -----


class FakeBedrock:
    """Counts requests; ``reply(prompt)`` builds the text returned for each one."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = 0

    def converse(self, **kwargs):
        self.requests += 1
        prompt = kwargs["messages"][0]["content"][0]["text"]
        return {
            "output": {"message": {"content": [{"text": self.reply(prompt)}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "stopReason": "end_turn",
        }


def _good_labels(prompt):
    items = [json.loads(x) for x in prompt.splitlines()[1:-1]]
    return json.dumps({"items": [_rec(it["id"], "none") for it in items]})


def _write_utts(path, texts):
    path.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": f"u{n:03d}", "gen_target": "neutral", "style": "s", "text": t}
        for n, t in enumerate(texts, 1)
    ]
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (path / "utterances.jsonl").write_text(body, "utf-8")


def _args(data, **kw):
    import argparse

    base = {"data": data, "allow_bedrock": True, "max_calls": 40, "labeler": "astra"}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def script(monkeypatch):
    mod = _load_script()

    def no_client():
        raise AssertionError("Bedrock client must not be constructed")

    monkeypatch.setattr(mod, "_client", no_client)
    return mod


def test_bedrock_is_refused_by_default(script, tmp_path):
    _write_utts(tmp_path, ["次のファイルを開いて"])
    for cmd in (["generate"], ["label", "--labeler", "opus"]):
        assert script.main(["--data", str(tmp_path), *cmd]) == 2  # no --allow-bedrock


def test_sdk_retries_are_off():
    cfg = _load_script()._client_config()
    assert cfg.retries["total_max_attempts"] == 1


def test_requests_never_exceed_max_calls(script, monkeypatch, tmp_path):
    _write_utts(tmp_path, [f"発話{n}" for n in range(60)])  # 3 batches of 20
    fake = FakeBedrock(lambda _p: "not json")  # every batch fails -> parse retry
    monkeypatch.setattr(script, "_client", lambda: fake)
    with pytest.raises(SystemExit, match="budget"):
        script.cmd_label(_args(tmp_path, max_calls=3))
    assert fake.requests == 3  # 2 for batch 1 + 1 for batch 2, then stop


def test_label_run_uses_one_request_per_batch(script, monkeypatch, tmp_path):
    _write_utts(tmp_path, [f"発話{n}" for n in range(45)])
    fake = FakeBedrock(_good_labels)
    monkeypatch.setattr(script, "_client", lambda: fake)
    assert script.cmd_label(_args(tmp_path, max_calls=3)) == 0
    assert fake.requests == 3
    assert len(_jsonl(tmp_path / "labels-astra.jsonl")) == 45


def test_label_refuses_to_send_pii(script, tmp_path):
    _write_utts(tmp_path, ["普通の発話", "連絡は a.b@corp.jp まで"])
    assert script.cmd_label(_args(tmp_path)) == 2  # _client would raise if reached


def test_generate_excludes_pii_and_logs_only_reason_codes(script, monkeypatch, tmp_path):
    secret = "x.y@corp.jp"
    items = [{"style": 1, "text": "ビルド落ちた"}, {"style": 2, "text": f"連絡は {secret} へ"}]
    fake = FakeBedrock(lambda _p: json.dumps({"items": items}, ensure_ascii=False))
    monkeypatch.setattr(script, "_client", lambda: fake)
    assert script.cmd_generate(_args(tmp_path, max_calls=20)) == 0
    assert fake.requests == len(sl.GEN_TARGETS)
    utts = _jsonl(tmp_path / "utterances.jsonl")
    assert [u["text"] for u in utts] == ["ビルド落ちた"]  # deduplicated, PII item dropped
    log = (tmp_path / "generation-log.jsonl").read_text("utf-8")
    assert secret not in log and "[REDACTED:email]" in log
    first = _jsonl(tmp_path / "generation-log.jsonl")[0]
    assert first["excluded"] == [{"index": 1, "reason": "pii:email"}]


# --- committed artefacts (always run: the data is part of the repository) -----


def test_committed_artefacts_are_present():
    for name in ("utterances.jsonl", "labels-astra.jsonl", "labels-opus.jsonl", "results.json"):
        assert (DATA / name).is_file(), name


class TestCommittedData:
    def test_size_and_coverage(self):
        utts = _jsonl(DATA / "utterances.jsonl")
        assert len(utts) >= 120
        assert {u["gen_target"] for u in utts} == set(sl.GEN_TARGETS)
        assert len({u["id"] for u in utts}) == len(utts)
        assert len({u["text"] for u in utts}) == len(utts)
        assert all(not sl.find_pii(u["text"]) for u in utts)
        assert all(not u["pii_screen"] for u in utts)

    def test_labels_validate_and_labelers_never_saw_intent(self):
        utts = {u["id"]: u for u in _jsonl(DATA / "utterances.jsonl")}
        for name in ("astra", "opus"):
            for rec in _jsonl(DATA / f"labels-{name}.jsonl"):
                sl.validate_record(rec)
                assert rec["id"] in utts
            seen = []
            for call in _jsonl(DATA / f"labeling-log-{name}.jsonl"):
                assert "gen_target" not in call["system"]
                lines = call["prompt"].splitlines()
                assert lines[0] == "<utterances_untrusted_data>"
                assert lines[-1] == "</utterances_untrusted_data>"
                for line in lines[1:-1]:
                    item = json.loads(line)
                    assert set(item) == {"id", "text"}  # no intent, style or other labels
                    assert item["text"] == utts[item["id"]]["text"]
                    if call.get("attempt", 1) == 1:
                        seen.append(item["id"])
            assert sorted(seen) == sorted(utts)
        orders = []
        for name in ("astra", "opus"):
            first = _jsonl(DATA / f"labeling-log-{name}.jsonl")[0]["prompt"]
            orders.append(first)
        assert orders[0] != orders[1]  # separate shuffles

    def test_report_reproduces_from_committed_files(self):
        script = _load_script()
        res = script.build_results(DATA)
        committed = json.loads((DATA / "results.json").read_text("utf-8"))
        fresh = json.loads(json.dumps(res, ensure_ascii=False, sort_keys=True))
        assert fresh == committed
        assert committed["label_status"].startswith("model-pair-draft")
