"""Recorded Harvest cases for the #506 / ADR 0018 phase-0 change (forged ``importance``).

``build(root)`` records a fixed set of turns through the server's own episode writer
(``record_episode``: original-turn score + verified origin), runs one rule Harvest sweep and
returns every file of the memory tree as text (the search index excluded). The fixture
``tests/fixtures/harvest_replay_golden.json`` was recorded with the code *before* phase 0;
``tests/test_memory_autonomy_phase0.py`` replays it after: an episode a server writer
recorded and nobody edited must reach the same decision, byte for byte.

Run ``python tests/harvest_replay_cases.py tests/fixtures/harvest_replay_golden.json`` to
record it again (only when the decision rules change on purpose).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from stiracore.control.harvest import HarvestScheduler
from stiracore.core.amygdala import AmygdalaPass
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryStore

TOPICS = ["garden", "deploy", "budget", "設計", "テスト", "release", "API", "notes"]
REPLIES = [
    "Done.",
    "We decided to keep the old schema; the error was in the migration step.",
    "## Plan\n- step one\n- step two\n```python\nprint('x')\n```",
    "結論: 方針を確認しました。バグは未解決です。",
    "Is that what you meant? I can check the Release notes.",
    "x" * 620,
    "## Decision\n- We decided to fix the error in the Deploy step?\n```sql\nSELECT 1;\n```\n"
    + "Details follow. " * 40,
]


class _FailingScorer:
    version = "replay/failing"

    def score(self, _text):
        raise RuntimeError("scorer down")


def turns() -> list[tuple[str, str, str]]:
    """(key, person's message, reply) — deterministic."""
    out = []
    for index in range(36):
        topic = TOPICS[index % len(TOPICS)]
        user = f"Question {index} about {topic}?" if index % 4 else f"Note {index}: {topic}."
        if index % 9 == 0:
            user = f"Remember the {topic} decision we confirmed yesterday, please."
        reply = REPLIES[index % len(REPLIES)]
        out.append((f"chat-{index:04d}", user, reply))
    return out


def tree(store: MemoryStore) -> dict[str, str]:
    files = {}
    for path in sorted(store.memory_dir.rglob("*")):
        if path.is_file() and path.name != "index.sqlite3":
            files[path.relative_to(store.memory_dir).as_posix()] = path.read_text(encoding="utf-8")
    return files


def build(root: Path, *, stage_auto: bool = True) -> dict[str, str]:
    store = MemoryStore(root / "memory", "a")
    for number, (key, user, reply) in enumerate(turns()):
        scorer = _FailingScorer() if number % 17 == 5 else None
        record_episode(store, user, reply, key=key, scorer=scorer)
        path = store.memory_dir / "episodic" / f"{key}.md"
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    HarvestScheduler(store, AmygdalaPass(), "a", stage_auto=stage_auto).run_distillation()
    return tree(store)


def record(path: Path) -> None:
    cases = {}
    for stage_auto in (True, False):
        with tempfile.TemporaryDirectory() as tmp:
            cases[f"stage_auto={stage_auto}"] = build(Path(tmp), stage_auto=stage_auto)
    path.write_text(json.dumps(cases, ensure_ascii=False, sort_keys=True, indent=0) + "\n")


if __name__ == "__main__":
    record(Path(sys.argv[1]))
