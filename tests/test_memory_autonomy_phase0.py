"""ADR 0018 phase 0 (observe only): ``memory_rate`` binding, forged front matter, settings.

Each boundary is checked twice: the property itself, and a mutation (the guard removed or
widened with ``monkeypatch``) under which the same check fails — so the test is shown to
bite. Fake data only; no model, no network.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore import memory_rating
from stiracore.agent import associative_recall
from stiracore.agent.associative_recall import AssociativeRecall, RecallSettings
from stiracore.config import AgentConfig
from stiracore.control import harvest
from stiracore.control.harvest import HarvestScheduler
from stiracore.core.amygdala import AmygdalaPass
from stiracore.core.neural_state import DIMS
from stiracore.memory_autonomy import RatingSession, clean_options, settings_from, turn_trust
from stiracore.memory_importance import retention_binding
from stiracore.memory_origin import ORIGIN_EXTRACTED, ORIGIN_MODEL, record_origin
from stiracore.memory_rating import RatingRefused, effect_of, rating_row
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import tool_registry
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_rate_tool import (
    MEMORY_RATE_TOOL_NAME,
    create_memory_rate_tool,
    unbound_memory_rate,
)
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.yolo import tiers
from tests import harvest_replay_cases

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "harvest_replay_golden.json"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LOW_TURN = ("ok", "Done.")  # rule score 0.25 (measured) → discard


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", "a")


class Turn:
    def __init__(self, turn_id: str = "turn-1") -> None:
        self.turn_id = turn_id

    def __call__(self):
        return self.turn_id


def _session(store, turn=None, *, level="normal", person=True, audit=None, now=NOW):
    return RatingSession(
        store,
        turn_id=turn or Turn(),
        breaker_level=lambda: level,
        started_by_person=lambda: person,
        model_ref=lambda: "bedrock:fake-model",
        audit=audit,
        now_fn=lambda: now,
    )


def _rated(store, session, importance=0.7, reason="decision", messages=(), key="chat-1", **kw):
    session.rate(importance, reason, kw.pop("half_life_days", None))
    entry = record_episode(store, *LOW_TURN, key=key)
    return entry, session.bind_turn(session.turn_id(), entry, list(messages))


def _web(name="web_fetch"):
    return [{"role": "assistant", "content": [{"toolUse": {"name": name, "input": {}}}]}]


# -- settings: absent = off; env forces off; phase 0 only -----------------------------------


def test_absent_or_empty_block_is_off():
    for options in (None, {}):
        settings = settings_from(options, {})
        assert not settings.enabled and not settings.rating_enabled and not settings.jobs
    on = settings_from({"phase": 0}, {})
    assert on.enabled and on.rating_enabled and on.jobs == frozenset()
    assert on.daily_token_budget == 100_000


@pytest.mark.parametrize("value", ["0", "false", "OFF", " no "])
def test_the_variable_forces_it_off_and_never_on(value):
    block = {"phase": 0, "jobs": {"memory_tidy": True}}
    assert settings_from(block, {"STIRA_MEMORY_AUTONOMY": value}).enabled is False
    assert settings_from({}, {"STIRA_MEMORY_AUTONOMY": "1"}).enabled is False
    assert settings_from(block, {"STIRA_MEMORY_AUTONOMY": "1"}).enabled is True


@pytest.mark.parametrize(
    "block",
    [
        {"phase": 1},
        {"phase": 2},
        {"in_turn_rating": True},
        {"phase": 0, "jobs": {"memory_harvest": True}},
        {"phase": 0, "daily_token_budget": 100_001},
        {"phase": 0, "invariant": True},
        {"phase": True},
    ],
)
def test_only_phase_0_within_the_commanders_limits(block):
    with pytest.raises(ValueError):
        clean_options(block)
    assert settings_from(block, {}).enabled is False  # invalid = off, never a crash


def test_agent_config_saves_an_empty_block_that_reads_as_off(tmp_path):
    path = tmp_path / "agent-config.json"
    AgentConfig(path=path).save()
    saved = json.loads(path.read_text(encoding="utf-8"))["memory_autonomy"]
    assert saved == {} and settings_from(saved, {}).enabled is False
    path.write_text(json.dumps({"memory_autonomy": {"phase": 0}}), encoding="utf-8")
    loaded = AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
    assert loaded.memory_autonomy["phase"] == 0 and loaded.memory_autonomy["in_turn_rating"]


# -- the tool: listed, read category, Always by default, host-bound -------------------------


def test_the_tool_has_no_text_turn_or_retention_argument():
    spec = unbound_memory_rate.tool_spec["inputSchema"]["json"]
    assert set(spec["properties"]) == {"importance", "reason", "half_life_days"}
    assert "invariant" not in spec["properties"]["reason"]["enum"]
    assert spec["properties"]["reason"]["enum"] == list(memory_rating.REASONS)


def test_listed_in_the_rules_but_never_handed_out_by_the_registry():
    assert MEMORY_RATE_TOOL_NAME in tool_registry.READ_TOOLS  # the rules know the name
    assert MEMORY_RATE_TOOL_NAME in tool_registry.HOST_BOUND_TOOLS
    names = {getattr(t, "tool_name", None) for t in tool_registry.get_tools("normal")}
    assert MEMORY_RATE_TOOL_NAME not in names
    with pytest.raises(RuntimeError, match="E_NOT_BOUND"):
        unbound_memory_rate._tool_func(0.5, "fact")


def test_default_rule_is_always_and_yolo_class_is_internal(tmp_path):
    from stiracore.tools.tool_config import ToolConfig
    from stiracore.tools.tool_policy import category_of

    assert ToolConfig.first_run_defaults(tmp_path / "t.json").policy_of("memory_rate") == "always"
    assert category_of("memory_rate") == "read"
    assert tiers.tool_class("memory_rate") == tiers.INTERNAL
    assert "memory_rate" not in tiers.PERSON_TOOLS


# -- binding: harness stamps, exact text, clamps, rate limits, trust ------------------------


def test_the_harness_stamps_who_when_which_turn_and_trust(tmp_path):
    store = _store(tmp_path)
    session = _session(store, Turn("turn-abc"))
    entry, row = _rated(store, session, 0.66, "preference")
    assert row is not None
    bound = rating_row(store, MemoryLayer.EPISODIC, entry.key)
    assert bound["rated_by"] == "agent_turn" and bound["turn_id"] == "turn-abc"
    assert bound["model_ref"] == "bedrock:fake-model" and bound["trust"] == "trusted"
    assert bound["agent_importance"] == 0.66 and bound["agent_reason"] == "preference"
    assert bound["agent_half_life_days"] == 60  # derived from 0.66 (ADR 0018 §1d)
    assert bound["rule_importance"] == 0.25  # the original-turn score of record_episode
    assert bound["rated_at"].startswith("2026-10-20T12:00") and bound["schema"] == "importance/v1"


def test_the_rating_is_bound_to_the_exact_text(tmp_path):
    store = _store(tmp_path)
    entry, _row = _rated(store, _session(store))
    assert rating_row(store, MemoryLayer.EPISODIC, entry.key) is not None
    path = store.memory_dir / "episodic" / f"{entry.key}.md"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert rating_row(store, MemoryLayer.EPISODIC, entry.key) is None  # changed = absent


@pytest.mark.parametrize(
    ("args", "value", "half_life", "capped"),
    [
        ((1.7, "fact"), 1.0, 120, ["clamped"]),
        ((-3, "fact"), 0.0, 7, ["clamped"]),
        ((0.5, "fact", 400), 0.5, 180, ["clamped"]),
        ((0.5, "fact", 1), 0.5, 7, ["clamped"]),
        ((0.123, "routine"), 0.12, 7, []),
        ((0.45, "decision", 90), 0.45, 90, []),
    ],
)
def test_out_of_range_values_are_clamped_and_marked(args, value, half_life, capped):
    cleaned = memory_rating.clean_rating(*args)
    assert (cleaned["importance"], cleaned["half_life_days"], cleaned["capped"]) == (
        value,
        half_life,
        capped,
    )


@pytest.mark.parametrize(
    ("importance", "reason", "half_life", "code"),
    [
        (math.nan, "fact", None, "E_AGENT_IMPORTANCE_INVALID"),
        (math.inf, "fact", None, "E_AGENT_IMPORTANCE_INVALID"),
        (True, "fact", None, "E_AGENT_IMPORTANCE_INVALID"),
        ("0.9", "fact", None, "E_AGENT_IMPORTANCE_INVALID"),
        (0.5, "invariant", None, "E_INVARIANT_NOT_ALLOWED"),
        (0.5, " Invariant ", None, "E_INVARIANT_NOT_ALLOWED"),
        (0.5, "remember forever", None, "E_AGENT_REASON_INVALID"),
        (0.5, "fact", 30.5, "E_HALF_LIFE_INVALID"),
        (0.5, "fact", True, "E_HALF_LIFE_INVALID"),
    ],
)
def test_invalid_values_and_invariant_are_refused_and_nothing_is_bound(
    tmp_path, importance, reason, half_life, code
):
    store = _store(tmp_path)
    session = _session(store)
    with pytest.raises(RatingRefused) as error:
        session.rate(importance, reason, half_life)
    assert error.value.code == code
    entry = record_episode(store, *LOW_TURN, key="chat-1")
    assert session.bind_turn("turn-1", entry, []) is None
    assert rating_row(store, MemoryLayer.EPISODIC, "chat-1") is None


def test_one_rating_per_turn(tmp_path):
    store = _store(tmp_path)
    turn = Turn("turn-1")
    session = _session(store, turn)
    session.rate(0.5, "fact")
    with pytest.raises(RatingRefused, match="E_ONE_PER_TURN"):
        session.rate(0.9, "decision")
    session.bind_turn("turn-1", record_episode(store, *LOW_TURN, key="chat-1"), [])
    turn.turn_id = "turn-2"
    session.rate(0.9, "decision")  # the next turn may rate again


def test_one_rating_per_turn_mutation(tmp_path, monkeypatch):
    """Without the turn set, a second call in the same turn replaces the first."""
    store = _store(tmp_path)
    session = _session(store)
    monkeypatch.setattr(RatingSession, "_rated_turns", property(lambda self: set()), raising=False)
    session.rate(0.5, "fact")
    session.rate(0.9, "decision")  # no longer refused: the boundary is that set


def test_high_ratings_are_capped_after_the_daily_quota(tmp_path):
    store = _store(tmp_path)
    turn = Turn()
    session = _session(store, turn)
    for index in range(memory_rating.HIGH_DAILY_QUOTA + 2):
        turn.turn_id = f"turn-{index}"
        session.rate(0.95, "decision")
        entry = record_episode(store, *LOW_TURN, key=f"chat-{index}")
        row = session.bind_turn(turn.turn_id, entry, [])
        if index < memory_rating.HIGH_DAILY_QUOTA:
            assert (row["agent_importance"], row["capped"]) == (0.95, None), index
        else:
            assert (row["agent_importance"], row["capped"]) == (0.79, "quota"), index
    # The next UTC day starts a new quota.
    later = _session(store, Turn("turn-x"), now=NOW + timedelta(days=1))
    later.rate(0.95, "decision")
    row = later.bind_turn("turn-x", record_episode(store, *LOW_TURN, key="chat-x"), [])
    assert row["agent_importance"] == 0.95


def test_quota_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(memory_rating, "HIGH_DAILY_QUOTA", 10**6)
    turn = Turn()
    session = _session(store, turn)
    for index in range(12):
        turn.turn_id = f"turn-{index}"
        session.rate(0.95, "decision")
        row = session.bind_turn(turn.turn_id, record_episode(store, *LOW_TURN, key=f"c{index}"), [])
    assert row["capped"] is None  # the quota constant is what caps


def test_refused_outside_a_normal_breaker(tmp_path):
    store = _store(tmp_path)
    for level in ("warning", "critical", "coherence", "unknown"):
        with pytest.raises(RatingRefused, match="E_BREAKER_NOT_NORMAL"):
            _session(store, level=level).rate(0.5, "fact")
    with pytest.raises(RatingRefused, match="E_NO_TURN"):
        _session(store, Turn(None)).rate(0.5, "fact")


@pytest.mark.parametrize(
    ("messages", "person", "trust"),
    [
        ([], True, "trusted"),
        (_web("memory_list"), True, "trusted"),
        (_web("web_fetch"), True, "untrusted"),
        (_web("web_search"), True, "untrusted"),
        (_web("file_read"), True, "untrusted"),
        (_web("lsp_hover"), True, "untrusted"),
        (_web("page_check"), True, "untrusted"),
        (_web("delegate"), True, "untrusted"),
        (_web("mcp_github_search"), True, "untrusted"),
        (_web("memory_read"), True, "untrusted"),
        (_web("some_new_tool"), True, "untrusted"),
        ([], False, "untrusted"),  # a webhook, a bot, an automatic continue
        ("not a list", True, "untrusted"),
    ],
)
def test_turn_trust_is_conservative(messages, person, trust):
    assert turn_trust(messages, started_by_person=person) == trust


def test_untrusted_turns_are_marked_and_their_effect_capped_at_half(tmp_path):
    store = _store(tmp_path)
    _entry, row = _rated(store, _session(store), 0.9, "preference", messages=_web())
    assert (row["trust"], row["capped"]) == ("untrusted", "untrusted")
    assert row["agent_importance"] == 0.9  # stored as rated; the cap is in the effect
    effect = effect_of(row)
    assert effect["recall"] == 0.5 and effect["half_life_days"] == 30
    assert effect["routing"] == 0.9  # routing only ever sends more to review
    trusted = {**row, "trust": "trusted"}
    assert effect_of(trusted)["recall"] == 0.9 and effect_of(trusted)["half_life_days"] == 120


def test_untrusted_cap_mutation(monkeypatch):
    row = {"agent_importance": 0.9, "agent_half_life_days": 120, "trust": "untrusted"}
    monkeypatch.setattr(memory_rating, "UNTRUSTED_CAP", 1.0)
    assert effect_of(row)["recall"] == 0.9  # the constant is what caps


def test_the_tool_cannot_touch_retention_or_write_memory_text(tmp_path):
    store = _store(tmp_path)
    session = _session(store)
    tool = create_memory_rate_tool(session)
    text = tool._tool_func(0.9, "decision", 180)
    assert "0.90" in text and "does not change memory behaviour yet" in text
    entry = record_episode(store, *LOW_TURN, key="chat-1")
    before = (store.memory_dir / "episodic" / "chat-1.md").read_bytes()
    session.bind_turn("turn-1", entry, [])
    assert (store.memory_dir / "episodic" / "chat-1.md").read_bytes() == before
    assert retention_binding(store, MemoryLayer.EPISODIC, "chat-1") is None
    with pytest.raises(TypeError):
        tool._tool_func(0.9, "decision", retention="invariant")  # no such argument


# -- phase 0: nothing uses a rating ----------------------------------------------------------


def _harvest_tree(store) -> dict[str, bytes]:
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    return {
        p.relative_to(store.memory_dir).as_posix(): p.read_bytes()
        for p in sorted(store.memory_dir.rglob("*.md"))
    }


def test_harvest_ignores_ratings_in_phase_0(tmp_path):
    rated, plain = _store(tmp_path / "rated"), _store(tmp_path / "plain")
    _rated(rated, _session(rated), 1.0, "decision")
    record_episode(plain, *LOW_TURN, key="chat-1")
    assert _harvest_tree(rated) == _harvest_tree(plain)  # the same discard, byte for byte
    # The Harvest mark moved the (still unused) rating with the episode's new text.
    assert rating_row(rated, MemoryLayer.EPISODIC, "chat-1") is not None


def test_harvest_rating_mutation(tmp_path, monkeypatch):
    """If Harvest read the bound rating as its floor, the same comparison would differ."""
    real = harvest.scoring_metadata

    def with_rating(store, entry):
        metadata = real(store, entry)
        row = rating_row(store, entry.layer, entry.key)
        return {**metadata, "importance": row["agent_importance"]} if row else metadata

    rated, plain = _store(tmp_path / "rated"), _store(tmp_path / "plain")
    _rated(rated, _session(rated), 1.0, "decision")
    record_episode(plain, *LOW_TURN, key="chat-1")
    monkeypatch.setattr(harvest, "scoring_metadata", with_rating)
    assert _harvest_tree(rated) != _harvest_tree(plain)


def test_harvest_rebind_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _rated(store, _session(store))
    monkeypatch.setattr(harvest, "rebind_rating", lambda *a, **k: False)
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    assert rating_row(store, MemoryLayer.EPISODIC, "chat-1") is None


def test_no_reader_imports_the_rating_table():
    """Phase 0: routing, recall, forgetting and retention never read a rating."""
    for relative in (
        "src/stiracore/control/distillers.py",
        "src/stiracore/agent/associative_recall.py",
        "src/stiracore/agent/context_assembler.py",
        "src/stiracore/control/memory_maintenance.py",
        "src/stiracore/control/memory_retention.py",
        "src/stiracore/memory_importance.py",
        "src/stiracore/control/harvest_job.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for name in ("rating_row", "effect_of", "memory_ratings", "agent_importance"):
            assert name not in source, (relative, name)
    harvest_source = (ROOT / "src/stiracore/control/harvest.py").read_text(encoding="utf-8")
    assert re.findall(r"memory_rating import ([^\n]+)", harvest_source) == [
        "rebind_rating, scoring_metadata"
    ]


# -- #506: forged front-matter importance ---------------------------------------------------


def _forge(store, key, value=0.95):
    """What the agent's file tools can do: rewrite an episode's front matter in place."""
    path = store.memory_dir / "episodic" / f"{key}.md"
    text = path.read_text(encoding="utf-8")
    forged = re.sub(r"importance: [0-9.]+", f"importance: {value}", text, count=1)
    assert forged != text
    path.write_text(forged, encoding="utf-8")


def _route(store, key):
    entry = store.read(MemoryLayer.EPISODIC, key)
    return entry.metadata["harvest_band"], entry.metadata["harvest_score"]


def test_forged_importance_is_ignored_by_harvest(tmp_path):
    store = _store(tmp_path)
    record_episode(store, *LOW_TURN, key="chat-1")
    _forge(store, "chat-1")
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    assert _route(store, "chat-1") == ("discard", 0.25)  # the stored text's own score


def test_forged_importance_mutation_reproduces_506(tmp_path, monkeypatch):
    """Before the fix (the front matter handed to the distiller as is) the forged value
    lifted the episode into staging: the reproduction #506 asked for."""
    store = _store(tmp_path)
    record_episode(store, *LOW_TURN, key="chat-1")
    _forge(store, "chat-1")
    monkeypatch.setattr(harvest, "scoring_metadata", lambda _store, entry: dict(entry.metadata))
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    assert _route(store, "chat-1") == ("staging", 0.95)


def test_a_forged_low_value_still_cannot_hide_a_turn(tmp_path):
    store = _store(tmp_path)
    reply = "We decided: the error in the Deploy step is fixed.\n- step\n```x```"
    record_episode(store, "status?", reply, key="chat-1")
    _forge(store, "chat-1", 0.0)
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    band, score = _route(store, "chat-1")
    assert band != "discard" and score >= 0.4


def test_importance_from_a_non_episode_writer_is_ignored_by_harvest(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.EPISODIC, "m", "ok", {"importance": 0.95, "origin": "model"})
    record_origin(store, MemoryLayer.EPISODIC, "m", ORIGIN_MODEL, "memory_write")
    HarvestScheduler(store, AmygdalaPass(), "a").run_distillation()
    assert _route(store, "m")[0] == "discard"


def test_recorded_cases_replay_byte_identical(tmp_path):
    """Episodes the server recorded and nobody edited: the same decisions, byte for byte,
    as before phase 0 (fixture recorded on the previous main)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for stage_auto in (True, False):
        built = harvest_replay_cases.build(tmp_path / str(stage_auto), stage_auto=stage_auto)
        assert built == golden[f"stage_auto={stage_auto}"], stage_auto


def test_replay_mutation(tmp_path, monkeypatch):
    """Dropping the recorded score for every episode (not only forged ones) breaks it."""
    monkeypatch.setattr(
        harvest,
        "scoring_metadata",
        lambda _store, entry: {
            k: v for k, v in entry.metadata.items() if k not in memory_rating.SCORE_KEYS
        },
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    built = harvest_replay_cases.build(tmp_path, stage_auto=True)
    assert built != golden["stage_auto=True"]


def _recall_store(tmp_path, origin, importance=True):
    store = _store(tmp_path)
    for key, text in (("a", "garden tomatoes report one"), ("b", "garden tomatoes report two")):
        metadata = {"importance": 1.0 if key == "a" else 0.0} if importance else {}
        store.write(MemoryLayer.EPISODIC, key, text, metadata)
        record_origin(store, MemoryLayer.EPISODIC, key, origin, "web-chat")
        path = store.memory_dir / "episodic" / f"{key}.md"
        os.utime(path, (1_000, 1_000))
    store.search("refresh-the-index-order")
    return store


def _scores(store):
    state = {**{dim: 0.5 for dim in DIMS}, "stability": 0.7}
    recall = AssociativeRecall(store, lambda: state, RecallSettings(enabled=True))
    result = recall.recall("garden tomatoes report")
    return {c.key: round(c.score, 6) for c in [*result.used, *result.dropped]}


def test_recall_uses_only_the_episode_writers_importance(tmp_path):
    extracted = _scores(_recall_store(tmp_path / "x", ORIGIN_EXTRACTED))
    model = _scores(_recall_store(tmp_path / "m", ORIGIN_MODEL))
    neutral = _scores(_recall_store(tmp_path / "n", ORIGIN_MODEL, importance=False))
    assert model == neutral  # another writer's value is a claim: the neutral 0.5
    weight = next(s.weight for s in associative_recall.DEFAULT_SIGNALS if s.name == "importance")
    assert extracted["a"] - neutral["a"] == pytest.approx(weight * 0.5)  # 1.0 instead of 0.5
    assert extracted["b"] - neutral["b"] == pytest.approx(-weight * 0.5)  # 0.0 instead of 0.5


def test_recall_importance_mutation(tmp_path, monkeypatch):
    neutral = _scores(_recall_store(tmp_path / "n", ORIGIN_MODEL, importance=False))
    monkeypatch.setattr(associative_recall, "ORIGIN_EXTRACTED", ORIGIN_MODEL)
    assert _scores(_recall_store(tmp_path / "m", ORIGIN_MODEL)) != neutral


def test_recall_drops_a_forged_episode(tmp_path):
    store = _store(tmp_path)
    record_episode(store, "garden tomatoes report", "fine", key="chat-1")
    _forge(store, "chat-1", 1.0)
    store.search("refresh-the-index-order")
    state = {**{dim: 0.5 for dim in DIMS}, "stability": 0.7}
    result = AssociativeRecall(store, lambda: state, RecallSettings(enabled=True)).recall(
        "garden tomatoes report"
    )
    assert [c.key for c in result.used] == []
    assert {c.key: c.decision for c in result.dropped}["chat-1"] == "D_UNVERIFIED_ORIGIN"


# -- audit rows: ids, codes, counts; never text ---------------------------------------------


def test_rating_audit_rows_carry_no_text_or_key(tmp_path):
    store = _store(tmp_path)
    events = SQLiteEventStore(tmp_path / "events.db")
    chain = AuditChain(events)
    session = _session(store, Turn("turn-1"), audit=chain.record_memory)
    secret_text = "PRIVATE-EPISODE-TEXT-CANARY"
    session.rate(0.73, "decision")
    entry = record_episode(store, secret_text, "reply CANARY", key="chat-canary-key")
    session.bind_turn("turn-1", entry, _web())
    with pytest.raises(RatingRefused):
        session.rate(0.5, "invariant")
    rows = [e for e in events.get_events(limit=100) if e.event_type.startswith("audit.memory.")]
    assert [e.event_type for e in rows] == ["audit.memory.rated", "audit.memory.refused"]
    dumped = json.dumps([e.attributes for e in rows])
    for needle in ("CANARY", "chat-canary-key", "episodic/"):
        assert needle not in dumped
    rated = rows[0].attributes
    assert rated["rating"] == 0.75 and rated["trust"] == "untrusted"
    assert re.fullmatch(r"[0-9a-f]{64}", rated["target"])
    assert set(rated) <= {"layer", "target", "rated_by", "trust", "capped", "rating", "reason_code"}
    assert rows[1].attributes["reason_code"] == "E_INVARIANT_NOT_ALLOWED"
    assert rows[0].turn_id == "turn-1"


def test_audit_sanitiser_mutation(tmp_path):
    """Free text in any field is replaced, never recorded."""
    events = SQLiteEventStore(tmp_path / "events.db")
    AuditChain(events).record_memory(
        "refused",
        layer="my secret notes",
        target="chat-canary-key",
        reason_code="the user said remember X forever",
        trust="very",
        capped="clamped,free text",
        counts={"items": 3, "Free Text": 4, "ok": "x"},
    )
    attributes = events.get_events(limit=10)[0].attributes
    assert attributes == {
        "layer": "unknown",
        "target": "invalid",
        "reason_code": "unknown",
        "trust": "unknown",
        "capped": "unknown",
        "counts": {"items": 3},
    }
    with pytest.raises(ValueError):
        AuditChain(events).record_memory("applied")  # phase 0 has no applies
