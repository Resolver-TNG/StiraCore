"""Fixed associative-recall cases whose output was recorded on ``main`` (9c3f0fd) before the
emotion-proximity signal (#364) was added: with the switch off, the prompt section and the
``memory.injected`` data must stay byte-identical (``tests/fixtures/recall_off_golden.json``).

Not a test module itself; ``tests/test_emotion_proximity_recall.py`` replays the cases and
``python -m tests.recall_golden_cases <out.json>`` (re)records them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from stiracore.agent.associative_recall import AssociativeRecall, RecallSettings
from stiracore.core.neural_state import DIMS
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ANCHORS = ["Keep one consistent identity across turns", "garden care stays honest"]
TOPICS = ["garden", "kaggle", "robot", "curry", "kyoto", "budget", "music", "train"]
QUERIES = [
    "How is the robot note7?",
    "garden tomatoes and identity?",
    "kyoto budget train",
    "Thank you for the curry music",
    "連想想起の話題3はどう？",
    "nothing-matches-here",
    "",
]
STATES = {
    "neutral": {},
    "bonded": {"trust": 0.9, "attachment": 0.85},
    "shaken": {"stability": 0.35, "vigilance": 0.6},
    "critical": {"stability": 0.2, "trust": 0.3},
}
SETTINGS = {
    "default": {},
    "two_items": {"max_items": 2, "max_chars": 200},
    "wide": {"max_items": 5, "max_chars": 2000, "window": 300},
}


def build_store(root: Path) -> MemoryStore:
    store = MemoryStore(root / "memory", "a")
    for index in range(120):
        topic = TOPICS[index % len(TOPICS)]
        text = f"Talked about {topic} item {index} and note{index % 13} with care."
        if index % 29 == 0:
            text = f"Thank you, I am glad: {text}"
        if index % 31 == 0:
            text = f"{text} Keep one consistent identity, garden care stays honest."
        if index % 17 == 0:
            text = f"マスター: 連想想起の話題{index % 5}について。\nリゾルバ: {text}"
        key = f"chat-{index:04d}"
        metadata = {"importance": round((index % 7) / 6, 3)} if index % 3 == 0 else {}
        store.write(MemoryLayer.EPISODIC, key, text, metadata)
        if index % 11 != 5:  # a few unverified ones
            record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
        path = store.memory_dir / "episodic" / f"{key}.md"
        os.utime(path, (1_000 + index, 1_000 + index))
    store.search("refresh-the-index-order")
    return store


def state_of(name: str) -> dict[str, float]:
    return {**{dim: 0.5 for dim in DIMS}, "stability": 0.7, **STATES[name]}


def run_cases(store: MemoryStore, make=AssociativeRecall) -> dict[str, dict]:
    out = {}
    for settings_name, options in SETTINGS.items():
        settings = RecallSettings(enabled=True, **options)
        for state_name in STATES:
            state = state_of(state_name)
            recall = make(
                store,
                lambda state=state: state,
                settings,
                identity_anchors=ANCHORS,
                ignore_terms=("マスター", "リゾルバ"),
            )
            for number, query in enumerate(QUERIES):
                for exclude in ((), ("chat-0119", "chat-0118")):
                    recall.set_query(query)
                    section = recall.section(exclude_keys=exclude)
                    result = recall.take_result()
                    data = None if result is None else result.as_event_data()
                    case = f"{settings_name}/{state_name}/q{number}/x{len(exclude)}"
                    out[case] = {"section": section, "event": data}
    return out


def record(path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cases = run_cases(build_store(Path(tmp)))
    path.write_text(json.dumps(cases, ensure_ascii=False, sort_keys=True, indent=0) + "\n")


if __name__ == "__main__":
    record(Path(sys.argv[1]))
