"""Fixed dev/test split of the JP stimulus mini-set (#164, prereg §14).

The split is decided *before* any Japanese rule pattern is written or tuned, and
it never looks at labels: within each generation intent (``gen_target``, the
prompt the generator was given), utterance ids are sorted and alternate
dev, test, dev, test, ... starting with dev. With 14 intents x 10 utterances this
gives 70 / 70 and 5 / 5 per intent.

Why not plain id order (u001-u070 / u071-u140): the ids are opaque and the
intents are unevenly spread over them (e.g. criticism 1 dev / 9 test,
threat_resolved 8 / 2), so a pattern table tuned on the first half would be
tested on scenes it never saw. Stratifying on the generation intent (not on a
label) keeps every scene in both halves without peeking at labels.

Patterns are added and adjusted on ``dev`` only. ``test`` is scored once, for the
final report (docs/eval/jp-stimulus/ja-hybrid/).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SPLIT_SCHEMA = "stira-jp-stimulus-split/1"
SPLIT_RULE = "stratified by gen_target; ids sorted; alternate dev,test starting with dev"
SPLITS = ("dev", "test")


def split_ids(utterances: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Return ``{"dev": [...], "test": [...]}`` (sorted ids) for the given utterances."""
    by_target: dict[str, list[str]] = {}
    seen: set[str] = set()
    for u in utterances:
        uid = str(u["id"])
        if uid in seen:
            raise ValueError(f"duplicate utterance id {uid!r}")
        seen.add(uid)
        by_target.setdefault(str(u["gen_target"]), []).append(uid)
    out: dict[str, list[str]] = {"dev": [], "test": []}
    for target in sorted(by_target):
        for n, uid in enumerate(sorted(by_target[target])):
            out[SPLITS[n % 2]].append(uid)
    return {k: sorted(v) for k, v in out.items()}


def split_document(utterances: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The committed ``split.json`` payload."""
    rows = list(utterances)
    ids = split_ids(rows)
    return {
        "schema": SPLIT_SCHEMA,
        "rule": SPLIT_RULE,
        "n": {k: len(v) for k, v in ids.items()},
        "dev": ids["dev"],
        "test": ids["test"],
    }
