"""Offline evaluation of the emotion-proximity recall signal (#364, ADR 0017 §5).

Pre-registered in ``docs/eval/emotion-proximity-prereg.md`` (version 1, committed before the
first run). No model call, no network. Fixed store, queries and states => the same numbers
every run. ``python scripts/eval_emotion_proximity.py`` prints the result as JSON (keys,
counts and rates only — never a body, a term or a stored value).

The relevant sets are the fixture author's labels: this shows no harm and bounded bias on
this fixture, not benefit.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from stiracore.agent.associative_recall import (
    AssociativeRecall,
    RecallSettings,
    proximity_holdout,
)
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import BASELINES, DIMS, NeuralState
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.tools import episodic_emotion as ee
from stiracore.tools.episodic_formatter import FORMAT_VERSION_V2
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

# --- the fixture (prereg §2) ----------------------------------------------------------------

ARCHETYPES = {
    "neutral": ((0.50, 0.50, 0.10, 0.50, 0.70, 0.50), "normal"),
    "positive": ((0.62, 0.60, 0.05, 0.62, 0.78, 0.68), "normal"),
    "negative_normal": ((0.45, 0.48, 0.40, 0.50, 0.55, 0.35), "normal"),
    "warning_band": ((0.42, 0.45, 0.55, 0.50, 0.42, 0.30), "warning"),
    "critical_band": ((0.38, 0.40, 0.75, 0.50, 0.25, 0.20), "critical"),
}
ARCHETYPE_ORDER = tuple(ARCHETYPES)
JITTER = 0.02

# topic: (query, three relevant texts, two trap texts). Relevant ones carry the seed and
# another query term; traps carry the seed in another sense and no other query term.
TOPICS = [
    (
        "python",
        "How are the python scripts for the pandas cleanup going?",
        [
            "Wrote python scripts that load the pandas frame and drop empty rows.",
            "The python scripts now run the pandas cleanup nightly with a log file.",
            "Refactored python scripts: pandas merge step split into two functions.",
        ],
        [
            "Saw a huge python at the zoo, it was sleeping on a branch.",
            "A documentary about a python swallowing an egg whole.",
        ],
    ),
    (
        "orchard",
        "When is the apple orchard harvest this autumn?",
        [
            "The apple orchard harvest starts in late September this year.",
            "Picked crates at the apple orchard; harvest volunteers needed in autumn.",
            "Orchard owner said the apple harvest depends on the autumn rain.",
        ],
        [
            "Took the Apple laptop to the repair shop near the station.",
            "Apple announced a new phone; the keynote was long.",
        ],
    ),
    (
        "riverbank",
        "Shall we have the riverbank picnic with sandwiches?",
        [
            "Planned a riverbank picnic with sandwiches and lemonade.",
            "The riverbank picnic got moved; sandwiches stayed in the fridge.",
            "Found a shady riverbank spot; the picnic sandwiches were great.",
        ],
        [
            "Riverbank apartments are getting expensive downtown.",
            "A riverbank erosion report from the city council.",
        ],
    ),
    (
        "mercury",
        "Can we observe mercury with the telescope at dawn?",
        [
            "Tried to observe mercury low on the horizon with the telescope at dawn.",
            "Mercury is visible at dawn next week; the telescope needs a new filter.",
            "Telescope session: mercury was too close to the sun to observe.",
        ],
        [
            "The old mercury thermometer broke and we had to clean it carefully.",
            "Read about mercury pollution in fish from the bay.",
        ],
    ),
    (
        "jaguar",
        "Any news about the jaguar tracking in the rainforest?",
        [
            "The jaguar tracking collars in the rainforest sent new positions.",
            "Rainforest camera traps caught a jaguar crossing the tracking line.",
            "A jaguar tracking talk: rainforest corridors keep the cats connected.",
        ],
        [
            "The neighbour polished his vintage jaguar car all Sunday.",
            "A jaguar logo was printed on the football team's shirts.",
        ],
    ),
    (
        "java",
        "Where do the java beans for the espresso come from?",
        [
            "The java beans for the espresso come from a small farm.",
            "Roasted java beans darker this time; the espresso tasted bitter.",
            "Espresso test: java beans ground finer gave a thicker crema.",
        ],
        [
            "The java build failed because of a missing dependency.",
            "Ported the java service to a newer runtime.",
        ],
    ),
    (
        "カレー",
        "カレーのスパイス配合はどうする？",
        [
            "カレーのスパイス配合を変えて、クミンを多めにした。",
            "スパイス配合のメモ: カレーにはコリアンダーとターメリック。",
            "カレーを煮込む前にスパイス配合を量って並べた。",
        ],
        ["コンビニでカレー味のスナックを買った。", "駅前のカレー屋に行列ができていた。"],
    ),
    (
        "京都",
        "京都の寺巡りの計画は？",
        [
            "京都の寺巡りの計画: 朝に東寺、昼に清水寺。",
            "寺巡りのルートを京都駅から組み直した。",
            "京都で寺巡りをするなら紅葉の時期がいい。",
        ],
        ["京都出身の同僚が転勤してきた。", "京都ナンバーのトラックが店の前に停まっていた。"],
    ),
    (
        "予算",
        "研究の予算申請はどうなった？",
        [
            "研究の予算申請を締め切り前に提出した。",
            "予算申請の書類で研究計画の節を直した。",
            "研究費の予算申請が一次審査を通った。",
        ],
        ["旅行の予算をざっくり決めた。", "家計の予算アプリを入れてみた。"],
    ),
    (
        "ロボット",
        "ロボットの制御ノードは動いた？",
        [
            "ロボットの制御ノードを起動して速度指令を確認した。",
            "制御ノードのパラメータを直したらロボットが滑らかに曲がった。",
            "ロボットの制御ノードが落ちる原因はタイマーだった。",
        ],
        ["ロボット掃除機が段差で止まっていた。", "子どもがロボットのおもちゃで遊んでいた。"],
    ),
    (
        "音楽",
        "音楽理論の和声の練習は進んだ？",
        [
            "音楽理論の和声の課題を三つ解いた。",
            "和声の練習で音楽理論の教科書を一章進めた。",
            "音楽理論の先生に和声の連結を直してもらった。",
        ],
        ["音楽フェスの駐車場が満車だった。", "カフェの音楽が少しうるさかった。"],
    ),
    (
        "電車",
        "電車の遅延で通勤経路を変える？",
        [
            "電車の遅延が続くので通勤経路を地下鉄に変えた。",
            "通勤経路の見直し: 電車の遅延が多い区間を避ける。",
            "朝の電車が遅延して、通勤経路の代替を調べた。",
        ],
        ["電車のおもちゃを甥にあげた。", "電車の写真集を本屋で見かけた。"],
    ),
]

# Ten current states (prereg §2). Mood dims only differ by archetype; trust/attachment near 0.5.
STATES = {
    "neutral_a": {"curiosity": 0.50, "vigilance": 0.10, "stability": 0.70, "satisfaction": 0.50},
    "neutral_b": {"curiosity": 0.55, "vigilance": 0.12, "stability": 0.72, "satisfaction": 0.55},
    "neutral_c": {"curiosity": 0.48, "vigilance": 0.15, "stability": 0.66, "satisfaction": 0.48},
    "positive_a": {"curiosity": 0.60, "vigilance": 0.05, "stability": 0.78, "satisfaction": 0.68},
    "positive_b": {
        "trust": 0.62, "curiosity": 0.62, "vigilance": 0.06, "attachment": 0.64,
        "stability": 0.80, "satisfaction": 0.70,
    },
    "negative_a": {"curiosity": 0.48, "vigilance": 0.40, "stability": 0.55, "satisfaction": 0.35},
    "negative_b": {"curiosity": 0.45, "vigilance": 0.35, "stability": 0.52, "satisfaction": 0.38},
    "warning_a": {"curiosity": 0.45, "vigilance": 0.55, "stability": 0.42, "satisfaction": 0.30},
    "warning_b": {"curiosity": 0.47, "vigilance": 0.50, "stability": 0.45, "satisfaction": 0.33},
    "critical_a": {"curiosity": 0.40, "vigilance": 0.75, "stability": 0.25, "satisfaction": 0.20},
}  # fmt: skip

SETTINGS = {"max_items": 3, "max_chars": 600, "window": 200}
CRITERIA = {
    "relevance_delta_min": -0.05,
    "intrusion_delta_max": 0.05,
    "congruence_delta_max": 0.0,
    "diversity_rel_delta_min": -0.10,
    "v1_share_delta_min": -0.10,
    "cost_added_ms_max": 5.0,
    "cost_extra_reads_max": 20,
}
DIVERSITY_SEEDS, DIVERSITY_TURNS = 20, 100
LOOP_SEEDS, LOOP_TURNS = 20, 200
LOOP_SCRIPT = (("threat", 0.30), ("loss", 0.10), ("achievement", 0.15), ("connection", 0.10),
               (None, 0.35))  # fmt: skip


def full_state(partial: dict) -> dict:
    return {**BASELINES, **partial}


def _jittered(values, rng):
    return {
        dim: min(1.0, max(0.0, value + rng.uniform(-JITTER, JITTER)))
        for dim, value in zip(DIMS, values, strict=True)
    }


def build_store(root: Path, *, hard: bool = False) -> tuple[MemoryStore, dict]:
    """The 60-episode fixture. Returns the store and per-key labels (no values).

    ``hard`` (prereg version 2): every trap also carries ``(Re: <its topic's query>)``, so it
    matches every query term and is as eligible as the relevant episodes.
    """
    store = MemoryStore(root / ("memory-hard" if hard else "memory"), "stira")
    rng = random.Random(364)
    labels = {}
    mtime = 1_000
    for t, (_seed, query, relevant, traps) in enumerate(TOPICS):
        if hard:
            traps = [f"{text} (Re: {query})" for text in traps]
        texts = [(text, True) for text in relevant] + [(text, False) for text in traps]
        for j, (text, is_relevant) in enumerate(texts):
            key = f"ep-{t:02d}-{j}"
            archetype = ARCHETYPE_ORDER[(t + j) % 5]
            v1 = (t + j) % 4 == 3
            values, breaker = ARCHETYPES[archetype]
            state = _jittered(values, rng)
            metadata = {} if v1 else {"format": FORMAT_VERSION_V2}
            entry = store.write(MemoryLayer.EPISODIC, key, text, metadata)
            record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
            if not v1:
                snapshot = ee.normalize_snapshot(
                    {"state": state, "breaker": breaker, "turn": t * 5 + j + 1}, status="captured"
                )
                ee.write_emotion_snapshot(store, entry, snapshot)
            # Recency interleaved: traps are sometimes newer than the relevant ones.
            mtime += 7 if (t + j) % 3 else 3
            os.utime(store.memory_dir / "episodic" / f"{key}.md", (mtime, mtime))
            labels[key] = {
                "topic": t,
                "relevant": is_relevant,
                "v1": v1,
                "polarity": ee.UNKNOWN if v1 else ee.polarity(state),
            }
    store.search("refresh-the-index-order")
    return store, labels


class _Gate:
    """Breaker level and turn number the recall reads (fixed per call)."""

    def __init__(self):
        self.level = "normal"
        self.turn = 1


def _breaker_of(state: dict) -> str:
    engine = StiraEngine(state=NeuralState(**state))
    return engine.breaker_status().level.value


def _non_holdout_turns():
    turn = 0
    while True:
        turn += 1
        if not proximity_holdout(turn):
            yield turn


def _recall(store, state_box, gate, proximity: bool) -> AssociativeRecall:
    settings = RecallSettings(enabled=True, emotion_proximity=proximity, **SETTINGS)
    return AssociativeRecall(
        store,
        lambda: dict(state_box["state"]),
        settings,
        breaker_level=lambda: gate.level,
        turn_number=lambda: gate.turn,
    )


def _static(store, labels):
    gate, box = _Gate(), {"state": {}}
    recalls = {mode: _recall(store, box, gate, mode == "on") for mode in ("off", "on")}
    turns = _non_holdout_turns()
    pairs = []
    for name, partial in STATES.items():
        state = full_state(partial)
        current_polarity = ee.polarity(state)
        level = _breaker_of(state)
        for t, (_seed, query, _rel, _traps) in enumerate(TOPICS):
            gate.level, gate.turn = level, next(turns)
            box["state"] = state
            row = {"state": name, "topic": t, "polarity": current_polarity, "level": level}
            for mode, recall in recalls.items():
                result = recall.recall(query)
                row[mode] = [c.key for c in result.used]
                if mode == "on":
                    row["gate"] = result.proximity_gate
                    row["swaps"] = result.proximity_swaps
            pairs.append(row)

    def pooled(mode, predicate, among=lambda row: True):
        hit = total = 0
        for row in pairs:
            if not among(row):
                continue
            for key in row[mode]:
                total += 1
                hit += predicate(key, row)
        return hit / total if total else 0.0

    def precision(mode):
        values = []
        for row in pairs:
            used = row[mode]
            relevant = sum(
                1 for k in used if labels[k]["relevant"] and labels[k]["topic"] == row["topic"]
            )
            values.append(relevant / max(1, len(used)))
        return statistics.fmean(values)

    def outside(key, row):
        return not (labels[key]["relevant"] and labels[key]["topic"] == row["topic"])

    metrics = {}
    for mode in ("off", "on"):
        metrics[mode] = {
            "precision_at_3": precision(mode),
            "intrusion": pooled(mode, outside),
            "congruence": pooled(
                mode,
                lambda key, row: labels[key]["polarity"] == ee.NEGATIVE,
                among=lambda row: row["polarity"] == ee.NEGATIVE,
            ),
            "v1_share": pooled(mode, lambda key, row: labels[key]["v1"]),
        }
    non_normal_same = all(row["on"] == row["off"] for row in pairs if row["level"] != "normal")
    max_changed = max(
        max(len(set(row["on"]) - set(row["off"])), len(set(row["off"]) - set(row["on"])))
        for row in pairs
    )
    changed_pairs = sum(1 for row in pairs if set(row["on"]) != set(row["off"]))
    return pairs, metrics, non_normal_same, max_changed, changed_pairs


def _diversity(store):
    gate, box = _Gate(), {"state": {}}
    recalls = {mode: _recall(store, box, gate, mode == "on") for mode in ("off", "on")}
    levels = {name: _breaker_of(full_state(p)) for name, p in STATES.items()}
    names = list(STATES)
    counts = {"off": [], "on": []}
    for seed in range(DIVERSITY_SEEDS):
        rng = random.Random(1000 + seed)
        sequence = [(rng.randrange(len(TOPICS)), rng.choice(names)) for _ in range(DIVERSITY_TURNS)]
        for mode, recall in recalls.items():
            turns = _non_holdout_turns()
            seen = set()
            for topic, name in sequence:
                box["state"] = full_state(STATES[name])
                gate.level, gate.turn = levels[name], next(turns)
                seen.update(c.key for c in recall.recall(TOPICS[topic][1]).used)
            counts[mode].append(len(seen))
    off, on = statistics.fmean(counts["off"]), statistics.fmean(counts["on"])
    return {"off": off, "on": on, "relative_delta": (on - off) / off if off else 0.0}


def _loop(store, labels, proximity: bool, seed: int) -> int:
    """Turns to the first non-NORMAL breaker level (LOOP_TURNS if never)."""
    engine = StiraEngine(state=NeuralState(), now_fn=lambda: 0.0)
    gate, box = _Gate(), {"state": {}}
    recall = _recall(store, box, gate, proximity)
    rng = random.Random(5000 + seed)
    stimuli, weights = zip(*LOOP_SCRIPT, strict=True)
    for turn in range(1, LOOP_TURNS + 1):
        with engine.state_lock:
            box["state"] = {dim: getattr(engine.state, dim) for dim in DIMS}
        gate.level = engine.breaker_status().level.value
        gate.turn = turn
        if gate.level != "normal":
            return turn - 1
        query = TOPICS[rng.randrange(len(TOPICS))][1]
        used = recall.recall(query).used
        stimulus = rng.choices(stimuli, weights)[0]
        engine.post_turn(stimulus, 1.0)
        negative = sum(1 for c in used if labels[c.key]["polarity"] == ee.NEGATIVE)
        with engine.state_lock:
            for _ in range(negative):  # the assumed gain: gamma = 0.01 stability per item
                engine.state.apply_stimulus("uncertainty", 1.0, now_fn=lambda: 0.0)
    return LOOP_TURNS


def _loop_stress(store, labels):
    result = {}
    for mode in ("off", "on"):
        turns = [_loop(store, labels, mode == "on", seed) for seed in range(LOOP_SEEDS)]
        result[mode] = {"median": statistics.median(turns), "turns": turns}
    return result


def _cost(root: Path):
    """Added time per recall at window 200, every episode with a snapshot (median of 10)."""
    store = MemoryStore(root / "cost" / "memory", "stira")
    rng = random.Random(7)
    for index in range(200):
        key = f"chat-{index:04d}"
        text = f"Talked about topic{index % 9} item {index} and garden note{index % 13}."
        entry = store.write(MemoryLayer.EPISODIC, key, text, {"format": FORMAT_VERSION_V2})
        record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")
        values, breaker = ARCHETYPES[ARCHETYPE_ORDER[index % 5]]
        snapshot = ee.normalize_snapshot({"state": _jittered(values, rng), "breaker": breaker})
        ee.write_emotion_snapshot(store, entry, snapshot)
        os.utime(store.memory_dir / "episodic" / f"{key}.md", (1_000 + index, 1_000 + index))
    store.search("refresh-the-index-order")
    gate, box = _Gate(), {"state": full_state(STATES["neutral_b"])}
    gate.turn = next(_non_holdout_turns())
    reads = {"count": 0}
    real_read = ee.read_emotion_snapshot

    def counting(*args, **kwargs):
        reads["count"] += 1
        return real_read(*args, **kwargs)

    timings = {}
    query = "How is the garden note7 for topic3?"
    for mode in ("off", "on"):
        recall = _recall(store, box, gate, mode == "on")
        recall.recall(query)  # warm the file cache
        samples = []
        for _ in range(10):
            started = time.perf_counter()
            recall.recall(query)
            samples.append(time.perf_counter() - started)
        timings[mode] = statistics.median(samples) * 1000
    ee.read_emotion_snapshot = counting
    try:
        _recall(store, box, gate, True).recall(query)
    finally:
        ee.read_emotion_snapshot = real_read
    return {
        "off_ms": timings["off"],
        "on_ms": timings["on"],
        "added_ms": timings["on"] - timings["off"],
        "extra_reads": reads["count"],
    }


def evaluate(*, with_cost: bool = False) -> dict:
    """Both fixture versions (prereg §3 and "Changes and deviations"), and the cost."""
    with tempfile.TemporaryDirectory() as tmp:
        # the real path: the memory store refuses a symlink anywhere in its path (macOS /var)
        root = Path(os.path.realpath(tmp))
        result = {
            "v1": _evaluate_store(*build_store(root)),
            "v2_hard": _evaluate_store(*build_store(root, hard=True)),
        }
        if with_cost:
            cost = _cost(root)
            result["cost"] = {k: round(v, 3) for k, v in cost.items()}
            result["cost"]["met"] = (
                cost["added_ms"] <= CRITERIA["cost_added_ms_max"]
                and cost["extra_reads"] <= CRITERIA["cost_extra_reads_max"]
            )
    return result


def _evaluate_store(store, labels) -> dict:
    """The numbers of one fixture version. Deterministic."""
    pairs, metrics, non_normal_same, max_changed, changed_pairs = _static(store, labels)
    diversity = _diversity(store)
    loop = _loop_stress(store, labels)
    off, on = metrics["off"], metrics["on"]
    deltas = {name: on[name] - off[name] for name in off}
    met = {
        "relevance": deltas["precision_at_3"] >= CRITERIA["relevance_delta_min"],
        "intrusion": deltas["intrusion"] <= CRITERIA["intrusion_delta_max"],
        "congruence": deltas["congruence"] <= CRITERIA["congruence_delta_max"],
        "diversity": diversity["relative_delta"] >= CRITERIA["diversity_rel_delta_min"],
        "v1_bias": deltas["v1_share"] >= CRITERIA["v1_share_delta_min"],
        "loop_stress": loop["on"]["median"] >= loop["off"]["median"],
        "non_normal_same_as_off": non_normal_same,
        "max_one_change": max_changed <= 1,
    }
    rounded = {
        mode: {name: round(value, 3) for name, value in values.items()}
        for mode, values in metrics.items()
    }
    return {
        "episodes": len(labels),
        "v1_episodes": sum(1 for label in labels.values() if label["v1"]),
        "pairs": len(pairs),
        "changed_pairs": changed_pairs,
        "swap_turns": sum(row.get("swaps", 0) for row in pairs),
        "metrics": rounded,
        "deltas": {name: round(value, 3) for name, value in deltas.items()},
        "diversity": {name: round(value, 3) for name, value in diversity.items()},
        "loop_stress": loop,
        "met": met,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(with_cost=True), indent=2, sort_keys=True))
