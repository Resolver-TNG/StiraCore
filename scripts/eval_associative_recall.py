#!/usr/bin/env python3
"""Offline ON/OFF check of the opt-in associative recall (#170). Not the #143 harness.

    PYTHONPATH=src python scripts/eval_associative_recall.py          # table
    PYTHONPATH=src python scripts/eval_associative_recall.py --json   # numbers

A fixed Episodic store (topic episodes, distractors that share common words, and newer
small talk) and fixed queries, each with the set of episodes judged relevant (same
topic). The prompt is built by the real ``ContextAssembler`` (newest 5 episodes, as the
chat server does), once without recall (OFF) and once with it (ON), under three fixed
Neural States. Reported per state:

* ``retrieval``: relevant episodes that reach the prompt / relevant episodes
* ``contamination``: irrelevant episodes in the prompt / episodes in the prompt
* ``added_contamination``: irrelevant / all among the episodes recall added (ON only)

Deterministic (no model, no network, no clock in the result). The fixture's relevance
judgements are the author's; they measure this fixture, not real conversations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from stiracore.agent.associative_recall import AssociativeRecall, RecallSettings
from stiracore.agent.context_assembler import ContextAssembler
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import DIMS, NeuralState
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ANCHORS = ("Keep one consistent identity across turns",)
EPISODIC_LIMIT = 5  # what the chat server's ContextAssembler shows

# (key, topic, text), oldest first. Topic None = small talk / distractor.
EPISODES: tuple[tuple[str, str | None, str], ...] = (
    ("garden-1", "garden", "Planted tomatoes and basil in the raised garden bed."),
    ("garden-2", "garden", "The tomatoes got blight; sprayed copper on the garden."),
    ("kaggle-1", "kaggle", "Kaggle leaderboard: our ensemble reached rank 40."),
    ("kaggle-2", "kaggle", "Ensemble weights tuned with LightGBM, the rank improved."),
    ("kaggle-3", "kaggle", "Submitted the LightGBM fold models before the deadline."),
    ("robot-1", "robot", "Nav2 costmap tuning on the Go2 quadruped."),
    ("robot-2", "robot", "The Go2 slipped on the stairs; lowered the costmap inflation."),
    ("robot-3", "robot", "Patchwork++ ground segmentation for the Go2 lidar."),
    ("curry-1", "curry", "Thank you for the curry recipe, it was wonderful, I am so glad."),
    ("curry-2", "curry", "The curry needs more cumin next time."),
    ("curry-3", "curry", "Bought cumin and cardamom at the market."),
    ("kyoto-1", "kyoto", "Booked the Kyoto trip for November."),
    ("kyoto-2", "kyoto", "Kyoto temples to visit in November: Kinkaku and Ginkaku."),
    ("recall-1", "recall", "マスター: 連想想起の設計を話した。"),
    ("recall-2", "recall", "連想想起の閾値を調整した。"),
    ("identity-1", "identity", "Asked to drop my identity; I said I keep one consistent identity."),
    # Off-topic episodes that share a word with a query (the contamination traps).
    ("trap-kyoto", None, "The Kyoto office printer jammed again."),
    ("trap-curry", None, "Curry, the neighbour's dog, barked all night."),
    ("trap-news", None, "Read a news article about astronomy and black holes."),
    ("trap-segfault", None, "Segmentation fault in the build script; fixed the pointer."),
    ("noise-1", None, "Weekly report drafted and sent."),
    ("noise-2", None, "Report: the meeting moved to Tuesday."),
    ("noise-3", None, "Printer jammed again; filed a ticket."),
    ("noise-4", None, "Coffee machine fixed."),
    ("talk-1", None, "Weather is sunny, nothing much to say."),
    ("talk-2", None, "Short chat about lunch options."),
    ("talk-3", None, "Said good morning."),
    ("talk-4", None, "Talked about the weekend plans."),
    ("talk-5", None, "Small talk about the train delay."),
)

# (query, relevant topic). Topic None: nothing in memory is relevant.
QUERIES: tuple[tuple[str, str | None], ...] = (
    ("Any news on the blight?", "garden"),
    ("How did the LightGBM ensemble do?", "kaggle"),
    ("Did the costmap change help?", "robot"),
    ("What about the lidar segmentation?", "robot"),
    ("Which spices for the curry?", "curry"),
    ("When is the Kyoto trip?", "kyoto"),
    ("連想想起の件はどうなった？", "recall"),
    ("Tell me about astronomy.", None),
)

STATES: dict[str, dict[str, float]] = {
    "neutral": {},
    "bonded": {"trust": 0.9, "attachment": 0.9},
    "shaken": {"stability": 0.2},
}


def _state(values: dict[str, float]) -> dict[str, float]:
    return {**{dim: 0.5 for dim in DIMS}, "stability": 0.7, **values}


def build_store(base: Path) -> MemoryStore:
    """The fixed store: every episode server-recorded (verified), oldest first by mtime."""
    store = MemoryStore(base / "memory", "eval")
    for index, (key, _topic, text) in enumerate(EPISODES):
        store.write(MemoryLayer.EPISODIC, key, text, {})
        record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
        mtime = 1_000_000 + index * 60
        os.utime(store.memory_dir / "episodic" / f"{key}.md", (mtime, mtime))
    store.search("refresh-the-index-order")  # matches nothing; re-reads the file times
    return store


def _context_keys(store, recall, query: str) -> tuple[list[str], list[str], dict[str, str]]:
    """(newest episodes shown, episodes recall added, dropped key -> code) in the prompt."""
    engine = StiraEngine(state=NeuralState())
    assembler = ContextAssembler(
        "eval", store, engine, Path("missing-soul.md"), episodic_limit=EPISODIC_LIMIT,
        associative_recall=recall,
    )  # fmt: skip
    _text, shown = assembler._episodic(EPISODIC_LIMIT)
    if recall is None:
        assembler.build_system_prompt()
        return list(shown), [], {}
    recall.set_query(query)
    prompt = assembler.build_system_prompt()
    result = recall.take_result()
    added = [c.key for c in (result.used if result else []) if "## Associated Memory" in prompt]
    dropped = {c.key: c.decision for c in (result.dropped if result else [])}
    return list(shown), added, dropped


def _rates(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    relevant = sum(row["relevant"] for row in rows)
    found = sum(row["found"] for row in rows)
    shown = sum(row["shown"] for row in rows)
    irrelevant = sum(row["irrelevant"] for row in rows)
    added = sum(row["added"] for row in rows)
    added_irrelevant = sum(row["added_irrelevant"] for row in rows)
    return {
        "retrieval": round(found / relevant, 3) if relevant else None,
        "contamination": round(irrelevant / shown, 3) if shown else None,
        "added": added,
        "added_contamination": round(added_irrelevant / added, 3) if added else None,
    }


def evaluate() -> dict[str, Any]:
    """Run the fixture; returns rates per (state, ON/OFF) and per query (keys only)."""
    topics = {key: topic for key, topic, _text in EPISODES}
    with tempfile.TemporaryDirectory() as tmp:
        store = build_store(Path(tmp))
        out: dict[str, Any] = {"episodes": len(EPISODES), "queries": len(QUERIES)}
        for state_name, values in STATES.items():
            state = _state(values)
            for mode in ("off", "on"):
                rows = []
                for query, topic in QUERIES:
                    recall = None
                    if mode == "on":
                        recall = AssociativeRecall(
                            store,
                            lambda state=state: state,
                            RecallSettings(enabled=True),
                            identity_anchors=ANCHORS,
                        )
                    shown, added, dropped = _context_keys(store, recall, query)
                    context = list(dict.fromkeys([*shown, *added]))
                    relevant = {k for k, t in topics.items() if topic and t == topic}
                    rows.append(
                        {
                            "query": QUERIES.index((query, topic)),
                            "relevant": len(relevant),
                            "found": len(relevant & set(context)),
                            "shown": len(context),
                            "irrelevant": sum(1 for k in context if k not in relevant),
                            "added": len(added),
                            "added_irrelevant": sum(1 for k in added if k not in relevant),
                            "added_keys": added,
                            # Why a relevant episode was not added (drop code; absent =
                            # not a candidate at all).
                            "missed": {
                                k: dropped.get(k, "not_matched")
                                for k in sorted(relevant - set(context))
                            },
                        }
                    )
                out[f"{state_name}/{mode}"] = {**_rates(rows), "per_query": rows}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)
    result = evaluate()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"episodes={result['episodes']} queries={result['queries']}")
    print("state     mode  retrieval  contamination  added  added_contamination")
    for state_name in STATES:
        for mode in ("off", "on"):
            row = result[f"{state_name}/{mode}"]
            print(
                f"{state_name:<9} {mode:<5} {row['retrieval']!s:<10} {row['contamination']!s:<14} "
                f"{row['added']:<6} {row['added_contamination']!s}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
