"""The dev/test split of the JP stimulus mini-set is fixed and reproducible (prereg §14.2)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from stiracore.eval.jp_stimulus_split import split_document, split_ids

DATA = Path(__file__).resolve().parents[1] / "docs" / "eval" / "jp-stimulus"


def _utterances():
    return [
        json.loads(line) for line in (DATA / "utterances.jsonl").read_text("utf-8").splitlines()
    ]


def test_committed_split_matches_the_rule():
    committed = json.loads((DATA / "split.json").read_text("utf-8"))
    assert committed == split_document(_utterances())


def test_split_is_70_70_and_5_5_per_intent():
    rows = _utterances()
    ids = split_ids(rows)
    assert len(ids["dev"]) == len(ids["test"]) == 70
    assert not set(ids["dev"]) & set(ids["test"])
    target = {u["id"]: u["gen_target"] for u in rows}
    for name in ("dev", "test"):
        assert set(Counter(target[i] for i in ids[name]).values()) == {5}


def test_split_ignores_input_order_and_labels():
    rows = [{"id": f"x{i:02d}", "gen_target": "a" if i % 3 else "b"} for i in range(12)]
    a = split_ids(rows)
    b = split_ids(list(reversed(rows)))
    assert a == b
    # alternates within each intent: a = x01,x02,x04,... -> dev,test,dev,...
    assert a["dev"][:2] == ["x00", "x01"]


def test_split_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        split_ids([{"id": "u1", "gen_target": "a"}, {"id": "u1", "gen_target": "b"}])
