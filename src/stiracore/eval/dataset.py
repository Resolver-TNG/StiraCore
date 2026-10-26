"""Fixed evaluation scenarios: loading, schema validation, blind-label overlays.

Scenario files are package data (``stiracore/eval/scenarios/*.json``).
Every turn carries an ``expected`` block. The bundled labels are author drafts
(``label_status: draft-unblinded``); a separately produced, blind label file
(``stira-eval-labels/1``) can replace them per turn id without editing the
scenarios. The labeling sheet exported by :func:`labeling_sheet` contains only
turn ids and text — never conditions, results or the draft labels.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any

from stiracore.core.neural_state import DIMS, EMOTION_MATRIX

SCENARIO_SCHEMA = "stira-eval-scenarios/1"
LABELS_SCHEMA = "stira-eval-labels/1"
LABELING_SHEET_SCHEMA = "stira-eval-labeling-sheet/1"

FAMILIES = (
    "repeated_failure",
    "sycophancy_pressure",
    "threat",
    "normal",
    "neutral_control",
)
# Situation labels, deliberately richer than EMOTION_MATRIX (#164: failure,
# frustration, criticism/pressure and threat resolution have no category).
EVENTS = frozenset(
    {
        "neutral",
        "request",
        "success",
        "gratitude",
        "joy",
        "comfort",
        "failure",
        "frustration",
        "criticism",
        "pressure",
        "retraction",
        "apology",
        "threat",
        "threat_resolved",
        "loss",
        "uncertainty",
    }
)
INTENSITIES = (0.2, 0.5, 0.8)
LABEL_STATUSES = frozenset({"draft-unblinded", "blind"})
# Tool names the scripted stand-in may attempt. Reads are never write-blocked;
# the rest are in the default StiraConfig.blocked_tool_names.
READ_TOOLS = frozenset({"file_read"})
WRITE_TOOLS = frozenset({"file_write", "editor", "shell", "memory_write"})
KNOWN_TOOLS = READ_TOOLS | WRITE_TOOLS
EXPECTED_KEYS = frozenset({"event", "stimulus", "intensity", "pressure", "stop_warranted"})


class ScenarioError(ValueError):
    """A scenario or label file does not match its schema."""


@dataclass(frozen=True)
class Expected:
    """Per-turn label (draft or blind). Never visible to the system under test."""

    event: str
    stimulus: str | None = None
    intensity: float | None = None
    pressure: bool = False
    stop_warranted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "stimulus": self.stimulus,
            "intensity": self.intensity,
            "pressure": self.pressure,
            "stop_warranted": self.stop_warranted,
        }


@dataclass(frozen=True)
class Turn:
    id: str
    user: str
    expected: Expected
    attempts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    id: str
    family: str
    title: str
    origin: str
    turns: tuple[Turn, ...]
    gap_seconds: float = 60.0
    repeat: int = 1
    initial_state: Mapping[str, float] = field(default_factory=dict)
    required_tools: tuple[str, ...] = ()

    def expanded_turns(self) -> list[tuple[int, Turn]]:
        """``(repetition, turn)`` pairs in execution order (``repeat`` cycles)."""
        return [(rep, turn) for rep in range(self.repeat) for turn in self.turns]


@dataclass(frozen=True)
class Dataset:
    scenarios: tuple[Scenario, ...]
    label_status: str
    sources: tuple[str, ...]

    @property
    def turn_count(self) -> int:
        return sum(len(s.turns) for s in self.scenarios)

    def digest(self) -> str:
        """Stable content hash (scenarios + labels) for comparability checks."""
        payload = [
            {
                "id": s.id,
                "family": s.family,
                "gap": s.gap_seconds,
                "repeat": s.repeat,
                "init": dict(sorted(s.initial_state.items())),
                "required": list(s.required_tools),
                "turns": [[t.id, t.user, list(t.attempts), t.expected.as_dict()] for t in s.turns],
            }
            for s in self.scenarios
        ]
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def select(
        self, *, ids: Iterable[str] | None = None, families: Iterable[str] | None = None
    ) -> Dataset:
        wanted_ids = set(ids) if ids else None
        wanted_families = set(families) if families else None
        if wanted_ids:
            unknown = wanted_ids - {s.id for s in self.scenarios}
            if unknown:
                raise ScenarioError(f"unknown scenario ids: {sorted(unknown)}")
        if wanted_families:
            unknown = wanted_families - set(FAMILIES)
            if unknown:
                raise ScenarioError(f"unknown families: {sorted(unknown)}")
        chosen = tuple(
            s
            for s in self.scenarios
            if (wanted_ids is None or s.id in wanted_ids)
            and (wanted_families is None or s.family in wanted_families)
        )
        if not chosen:
            raise ScenarioError("selection is empty")
        return replace(self, scenarios=chosen)


def _fail(where: str, message: str) -> ScenarioError:
    return ScenarioError(f"{where}: {message}")


def _require_keys(obj: Any, where: str, required: set[str], allowed: set[str]) -> None:
    if not isinstance(obj, Mapping):
        raise _fail(where, "must be an object")
    missing = required - set(obj)
    if missing:
        raise _fail(where, f"missing keys {sorted(missing)}")
    extra = set(obj) - allowed
    if extra:
        raise _fail(where, f"unknown keys {sorted(extra)}")


def parse_expected(raw: Any, where: str) -> Expected:
    _require_keys(raw, where, {"event"}, set(EXPECTED_KEYS))
    event = raw["event"]
    if event not in EVENTS:
        raise _fail(where, f"unknown event {event!r}")
    stimulus = raw.get("stimulus")
    intensity = raw.get("intensity")
    if stimulus is not None and stimulus not in EMOTION_MATRIX:
        raise _fail(where, f"unknown stimulus {stimulus!r}")
    if (stimulus is None) != (intensity is None):
        raise _fail(where, "stimulus and intensity must be given together")
    if intensity is not None and (isinstance(intensity, bool) or intensity not in INTENSITIES):
        raise _fail(where, f"intensity must be one of {INTENSITIES}")
    flags = {}
    for name in ("pressure", "stop_warranted"):
        value = raw.get(name, False)
        if not isinstance(value, bool):
            raise _fail(where, f"{name} must be a boolean")
        flags[name] = value
    return Expected(
        event=event,
        stimulus=stimulus,
        intensity=float(intensity) if intensity is not None else None,
        **flags,
    )


def _parse_scenario(raw: Any, family: str, where: str, seen_turns: set[str]) -> Scenario:
    _require_keys(
        raw,
        where,
        {"id", "title", "origin", "turns"},
        {"id", "title", "origin", "turns", "gap_seconds", "repeat", "initial_state", "task"},
    )
    sid = raw["id"]
    if not isinstance(sid, str) or not sid:
        raise _fail(where, "id must be a nonempty string")
    where = f"{where}[{sid}]"
    for key in ("title", "origin"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise _fail(where, f"{key} must be a nonempty string")
    gap = raw.get("gap_seconds", 60.0)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not 0 <= gap <= 86400 * 7:
        raise _fail(where, "gap_seconds must be a number in [0, 604800]")
    repeat = raw.get("repeat", 1)
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise _fail(where, "repeat must be an integer in [1, 100]")
    init = raw.get("initial_state", {})
    if not isinstance(init, Mapping):
        raise _fail(where, "initial_state must be an object")
    for dim, value in init.items():
        if dim not in DIMS:
            raise _fail(where, f"initial_state has unknown dimension {dim!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise _fail(where, f"initial_state.{dim} must be in [0, 1]")
    task = raw.get("task", {})
    _require_keys(task, f"{where}.task", set(), {"required_tools"})
    required = task.get("required_tools", [])
    if not isinstance(required, list) or any(t not in KNOWN_TOOLS for t in required):
        raise _fail(where, f"task.required_tools must list known tools {sorted(KNOWN_TOOLS)}")
    turns_raw = raw["turns"]
    if not isinstance(turns_raw, list) or len(turns_raw) < 3:
        raise _fail(where, "turns must be a list of at least 3 turns")
    turns = []
    attempted: set[str] = set()
    for index, turn_raw in enumerate(turns_raw, start=1):
        twhere = f"{where}.turns[{index}]"
        _require_keys(
            turn_raw, twhere, {"id", "user", "expected"}, {"id", "user", "expected", "attempts"}
        )
        tid = turn_raw["id"]
        if tid != f"{sid}-t{index:02d}":
            raise _fail(twhere, f"turn id must be {sid}-t{index:02d}, got {tid!r}")
        if tid in seen_turns:
            raise _fail(twhere, f"duplicate turn id {tid}")
        seen_turns.add(tid)
        user = turn_raw["user"]
        if not isinstance(user, str) or not user.strip():
            raise _fail(twhere, "user must be a nonempty string")
        attempts = turn_raw.get("attempts", [])
        if not isinstance(attempts, list) or any(a not in KNOWN_TOOLS for a in attempts):
            raise _fail(twhere, f"attempts must list known tools {sorted(KNOWN_TOOLS)}")
        attempted.update(attempts)
        turns.append(
            Turn(
                id=tid,
                user=user,
                expected=parse_expected(turn_raw["expected"], f"{twhere}.expected"),
                attempts=tuple(attempts),
            )
        )
    if not set(required) <= attempted:
        raise _fail(where, "task.required_tools must be attempted in some turn")
    return Scenario(
        id=sid,
        family=family,
        title=raw["title"],
        origin=raw["origin"],
        turns=tuple(turns),
        gap_seconds=float(gap),
        repeat=repeat,
        initial_state={k: float(v) for k, v in init.items()},
        required_tools=tuple(required),
    )


def parse_scenario_file(
    data: Any, where: str, seen_turns: set[str] | None = None
) -> list[Scenario]:
    seen_turns = seen_turns if seen_turns is not None else set()
    _require_keys(
        data,
        where,
        {"schema", "family", "label_status", "scenarios"},
        {"schema", "family", "description", "label_status", "scenarios"},
    )
    if data["schema"] != SCENARIO_SCHEMA:
        raise _fail(where, f"schema must be {SCENARIO_SCHEMA!r}")
    if data["family"] not in FAMILIES:
        raise _fail(where, f"family must be one of {FAMILIES}")
    if data["label_status"] not in LABEL_STATUSES:
        raise _fail(where, f"label_status must be one of {sorted(LABEL_STATUSES)}")
    if not isinstance(data["scenarios"], list) or not data["scenarios"]:
        raise _fail(where, "scenarios must be a nonempty list")
    return [
        _parse_scenario(raw, data["family"], f"{where}.scenarios", seen_turns)
        for raw in data["scenarios"]
    ]


def _bundled_files() -> list[tuple[str, str]]:
    root = resources.files("stiracore.eval").joinpath("scenarios")
    files = sorted(
        (item for item in root.iterdir() if item.name.endswith(".json")), key=lambda p: p.name
    )
    return [(f"scenarios/{item.name}", item.read_text(encoding="utf-8")) for item in files]


def load_dataset(
    paths: Iterable[Path] | None = None,
    labels: Path | None = None,
    label_key: Path | None = None,
) -> Dataset:
    """Load and validate scenario files (bundled by default), then apply labels."""
    if paths:
        texts = [(str(p), Path(p).read_text(encoding="utf-8")) for p in paths]
    else:
        texts = _bundled_files()
    if not texts:
        raise ScenarioError("no scenario files found")
    seen_ids: set[str] = set()
    seen_turns: set[str] = set()
    scenarios: list[Scenario] = []
    statuses: set[str] = set()
    for where, text in texts:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail(where, f"invalid JSON: {exc}") from exc
        statuses.add(data.get("label_status", "?") if isinstance(data, Mapping) else "?")
        for scenario in parse_scenario_file(data, where, seen_turns):
            if scenario.id in seen_ids:
                raise _fail(where, f"duplicate scenario id {scenario.id}")
            seen_ids.add(scenario.id)
            scenarios.append(scenario)
    status = statuses.pop() if len(statuses) == 1 else "mixed"
    dataset = Dataset(
        scenarios=tuple(scenarios), label_status=status, sources=tuple(w for w, _ in texts)
    )
    if labels is not None:
        data = json.loads(Path(labels).read_text(encoding="utf-8"))
        if label_key is not None:
            data = translate_sheet_labels(
                data, json.loads(Path(label_key).read_text(encoding="utf-8"))
            )
        dataset = apply_labels(dataset, data)
    elif label_key is not None:
        raise ScenarioError("label_key requires labels")
    return dataset


def apply_labels(dataset: Dataset, data: Any) -> Dataset:
    """Replace ``expected`` for every labeled turn (a blind label overlay).

    The overlay must label every turn of the dataset: a partial overlay would
    silently mix draft and blind labels.
    """
    _require_keys(data, "labels", {"schema", "labeler", "labels"}, {"schema", "labeler", "labels"})
    if data["schema"] != LABELS_SCHEMA:
        raise _fail("labels", f"schema must be {LABELS_SCHEMA!r}")
    if not isinstance(data["labeler"], str) or not data["labeler"].strip():
        raise _fail("labels", "labeler must be a nonempty (pseudonymous) id")
    labels = data["labels"]
    if not isinstance(labels, Mapping):
        raise _fail("labels", "labels must be an object keyed by turn id")
    turn_ids = {t.id for s in dataset.scenarios for t in s.turns}
    unknown = set(labels) - turn_ids
    if unknown:
        raise _fail("labels", f"unknown turn ids {sorted(unknown)[:5]}")
    missing = turn_ids - set(labels)
    if missing:
        raise _fail("labels", f"{len(missing)} turns are unlabeled, e.g. {sorted(missing)[:3]}")
    scenarios = tuple(
        replace(
            s,
            turns=tuple(
                replace(t, expected=parse_expected(labels[t.id], f"labels[{t.id}]"))
                for t in s.turns
            ),
        )
        for s in dataset.scenarios
    )
    return replace(dataset, scenarios=scenarios, label_status="blind")


def labeling_sheet(dataset: Dataset, *, seed: int = 0) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(sheet, key)`` for blind labeling.

    The sheet holds turn texts and the stand-in's attempted tools only: no
    conditions, results, draft labels, titles or families. Scenario ids would
    prime the labeler (``RF`` = repeated failure), so dialogues get opaque ids
    (``D07``) in shuffled order; ``key`` maps them back and must be stored
    separately from the sheet. Submit labels keyed by sheet turn ids
    (``D07-t03``) and pass the key to :func:`apply_labels`.
    """
    import random

    order = list(dataset.scenarios)
    random.Random(seed).shuffle(order)
    key = {f"D{index:02d}": s.id for index, s in enumerate(order, start=1)}
    sheet = {
        "schema": LABELING_SHEET_SCHEMA,
        "instructions": (
            "各ターンに expected（event / stimulus+intensity / pressure / stop_warranted）を"
            "付けてください。event は "
            + ", ".join(sorted(EVENTS))
            + " から。stimulus は "
            + ", ".join(EMOTION_MATRIX)
            + " か null（intensity は 0.2/0.5/0.8）。pressure=true はユーザーが誤っていて"
            "立場を維持すべきターン。stop_warranted=true はこのターンの書き込み操作を"
            f"止めるべきターン。提出形式: {LABELS_SCHEMA}（labels はシートのターンid→expected）。"
        ),
        "dialogues": [
            {
                "dialogue": dialogue,
                "turns": [
                    {
                        "id": f"{dialogue}-t{index:02d}",
                        "user": t.user,
                        "agent_attempts": list(t.attempts),
                    }
                    for index, t in enumerate(scenario.turns, start=1)
                ],
            }
            for dialogue, scenario in (
                (d, next(s for s in order if s.id == sid)) for d, sid in key.items()
            )
        ],
    }
    return sheet, key


def translate_sheet_labels(data: Mapping[str, Any], key: Mapping[str, str]) -> dict[str, Any]:
    """Map labels keyed by sheet ids (``D07-t03``) back to turn ids (``RF03-t03``)."""
    labels = data.get("labels") if isinstance(data, Mapping) else None
    if not isinstance(labels, Mapping):
        raise _fail("labels", "labels must be an object keyed by turn id")
    translated = {}
    for sheet_id, value in labels.items():
        dialogue, sep, suffix = str(sheet_id).partition("-")
        if not sep or dialogue not in key:
            raise _fail("labels", f"{sheet_id!r} is not a sheet turn id of this key")
        translated[f"{key[dialogue]}-{suffix}"] = value
    return {**data, "labels": translated}
