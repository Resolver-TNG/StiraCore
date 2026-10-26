"""Importance tags (``retention: invariant | derived`` + half-life) for forgetting (#173).

* Untagged entries are selected exactly as before (30/90/180 days, stale at 365).
* ``invariant`` is never compressed, never a duplicate/stale deletion candidate, and an
  Episodic one is held by a retention run — only when a human bound it.
* A ``derived`` half-life scales the stages (``age * 30 >= threshold * half_life``).
* The agent cannot tag its way out of forgetting: Episodic front matter is a claim
  (unbound), Harvest does not copy the field, ``memory_write`` cannot set it and the
  Semantic layer is not writable by its file tools. Tags are set by ``stira memory tag``
  (a human, owner lock, audited with digests only).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore.config.data_dir_lock import DataDirOwnerLock
from stiracore.control.distillation_cli import main as distill_main
from stiracore.control.distillers import RuleBasedDistiller
from stiracore.control.forgetting_archive import ARCHIVE_DIRNAME, AUDIT_LOG_NAME
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.memory_cli import main as memory_main
from stiracore.control.memory_forget import ForgetRequest, apply_forget, plan_forget
from stiracore.control.memory_layout import DataLayout
from stiracore.control.memory_maintenance import MemoryMaintenance
from stiracore.control.memory_retention import RetentionPolicy, retention_report
from stiracore.core.amygdala import AmygdalaPass
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

NOW = datetime(2026, 10, 18, tzinfo=UTC)
CANARY = "canary-importance-7d1f"


def _digest(layer_key: str) -> str:
    return hashlib.sha256(layer_key.encode()).hexdigest()


def _data(tmp_path: Path) -> tuple[Path, MemoryStore]:
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    return data, MemoryStore(data / "memory", "stira")


def _age(store: MemoryStore, key: str, days: int, *, now: datetime = NOW) -> None:
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE layer = 'semantic' AND key = ?",
            ((now - timedelta(days=days)).timestamp(), key),
        )


def _semantic(store, key, days, metadata=None, body=None, *, now=NOW):
    store.write(MemoryLayer.SEMANTIC, key, body or f"fact {key} " + "z" * 5_000, metadata or {})
    _age(store, key, days, now=now)


def _selected(store) -> dict[str, str]:
    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    return {item.key: item.stage for item in result.candidates}


# -- default: exactly the old selection ------------------------------------------------


AGES = (0, 29, 30, 89, 90, 179, 180, 364, 365, 400)
OLD_STAGE = {29: None, 30: "light", 89: "light", 90: "compact", 179: "compact"}


def _old_stage(age: int) -> str | None:
    if age >= 180:
        return "archive"
    return OLD_STAGE.get(age)


@pytest.mark.parametrize("metadata", [{}, {"retention": "derived"}])
def test_untagged_and_plain_derived_select_exactly_as_before(tmp_path, metadata):
    _, store = _data(tmp_path)
    for age in AGES:
        _semantic(store, f"facts/a{age}", age, dict(metadata))
    expected = {f"facts/a{age}": _old_stage(age) for age in AGES if _old_stage(age)}
    assert _selected(store) == expected
    stale = {c.key for c in MemoryMaintenance(store).find_deletion_candidates(now=NOW)}
    assert stale == {"facts/a365", "facts/a400"}


# -- invariant ---------------------------------------------------------------------------


def test_invariant_is_never_compressed_and_is_listed_as_protected(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/keep", 400, {"retention": "invariant"})
    _semantic(store, "facts/fade", 400)
    before = store.read_text(MemoryLayer.SEMANTIC, "facts/keep")

    preview = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    assert [c.key for c in preview.candidates] == ["facts/fade"]
    assert preview.protected == (
        {
            "area": "semantic",
            "key_sha256": _digest("semantic/facts/keep"),
            "code": "retention_invariant",
            "reason": "retention: invariant (never forgotten)",
        },
    )

    applied = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert applied.applied == 1
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/keep") == before


def test_an_entry_tagged_after_the_preview_is_not_compressed(tmp_path):
    """A tag written between the selection and the overwrite still wins (fail closed)."""
    _, store = _data(tmp_path)
    _semantic(store, "facts/x", 400)
    maintenance = MemoryMaintenance(store)
    (candidate,) = maintenance.run_forgetting(now=NOW, apply=False).candidates
    entry = store.read(MemoryLayer.SEMANTIC, "facts/x")
    store.write(MemoryLayer.SEMANTIC, "facts/x", entry.content, {"retention": "invariant"})
    tagged = store.read_text(MemoryLayer.SEMANTIC, "facts/x")
    try:
        result = maintenance._apply([candidate], NOW)
    finally:
        maintenance.archive.close()
    assert (result.applied, result.refused) == (0, 1)
    assert "invariant" in result.candidates[0].refused
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/x") == tagged


def test_a_tag_written_between_the_read_and_the_overwrite_wins(tmp_path):
    """The existing digest check right before the overwrite catches a later tag too."""
    _, store = _data(tmp_path)
    _semantic(store, "facts/x", 400)
    maintenance = MemoryMaintenance(store)
    audit = maintenance.archive.audit

    def tag_on_start(kind, **body):
        if kind == "forget.started":
            entry = store.read(MemoryLayer.SEMANTIC, "facts/x")
            store.write(MemoryLayer.SEMANTIC, "facts/x", entry.content, {"retention": "invariant"})
        return audit(kind, **body)

    maintenance.archive.audit = tag_on_start
    result = maintenance.run_forgetting(now=NOW, apply=True)
    tagged_before = store.read(MemoryLayer.SEMANTIC, "facts/x")
    assert (result.applied, result.refused) == (0, 1)
    assert tagged_before.metadata == {"retention": "invariant"}
    assert "forgetting_stage" not in tagged_before.metadata


def test_invariant_is_excluded_from_duplicate_and_stale_deletion(tmp_path):
    _, store = _data(tmp_path)
    body = "The same fact, twice."
    # The invariant copy is the older one: without the tag it would be the duplicate.
    _semantic(store, "dup/invariant", 10, {"retention": "invariant"}, body)
    _semantic(store, "dup/plain", 1, {}, body)
    _semantic(store, "dup2/a", 5, {"retention": "invariant"}, "Another fact.")
    _semantic(store, "dup2/b", 1, {"retention": "invariant"}, "Another fact.")
    _semantic(store, "stale/invariant", 900, {"retention": "invariant"})
    _semantic(store, "stale/plain", 900)

    found = {c.key: c for c in MemoryMaintenance(store).find_deletion_candidates(now=NOW)}
    assert set(found) == {"dup/plain", "stale/plain"}
    assert found["dup/plain"].duplicate_of == "dup/invariant"


# -- half-life -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("half_life", "age", "stage"),
    [
        (10, 9, None),
        (10, 10, "light"),
        (10, 30, "compact"),
        (10, 60, "archive"),
        (60, 59, None),
        (60, 60, "light"),
        (60, 180, "compact"),
        (60, 359, "compact"),
        (60, 360, "archive"),
        (30, 30, "light"),  # the reference half-life == untagged
    ],
)
def test_half_life_scales_the_stages(tmp_path, half_life, age, stage):
    _, store = _data(tmp_path)
    _semantic(
        store, "facts/x", age, {"retention": "derived", "retention_half_life_days": half_life}
    )
    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    assert {c.key: c.stage for c in result.candidates} == ({"facts/x": stage} if stage else {})
    if stage:
        (item,) = result.candidates
        assert item.half_life_days == half_life
        assert f"half-life {half_life} days" in item.reason
        assert item.strength == pytest.approx(0.5 ** (age / half_life), abs=1e-4)


def test_half_life_scales_the_stale_threshold(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "s/short", 122, {"retention_half_life_days": 10})  # 122*30 >= 365*10
    _semantic(store, "s/short-young", 121, {"retention_half_life_days": 10})
    _semantic(store, "s/long", 729, {"retention_half_life_days": 60})  # 729*30 < 365*60
    stale = {c.key for c in MemoryMaintenance(store).find_deletion_candidates(now=NOW)}
    assert stale == {"s/short"}


@pytest.mark.parametrize(
    "metadata",
    [
        {"retention": "forever"},
        {"retention": "INVARIANT"},
        {"retention": ["invariant"]},
        {"retention": "derived", "retention_half_life_days": 0},
        {"retention": "derived", "retention_half_life_days": -5},
        {"retention": "derived", "retention_half_life_days": "10"},
        {"retention": "derived", "retention_half_life_days": True},
        {"retention": "derived", "retention_half_life_days": 3651},
        {"retention": "derived", "retention_half_life_days": 7.5},
    ],
)
def test_an_invalid_tag_is_ignored_and_selects_as_before(tmp_path, metadata):
    """Fail-safe: an unreadable tag never protects an entry, nor makes it fade faster."""
    _, store = _data(tmp_path)
    for age in AGES:
        _semantic(store, f"facts/a{age}", age, dict(metadata))
    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    expected = {f"facts/a{age}": _old_stage(age) for age in AGES if _old_stage(age)}
    assert {c.key: c.stage for c in result.candidates} == expected
    assert all(c.retention_invalid for c in result.candidates)
    assert result.protected == ()


# -- dry run: the list and the reasons ------------------------------------------------------


def test_distill_dry_run_lists_candidates_reasons_and_protected_entries(tmp_path, capsys):
    data, store = _data(tmp_path)
    now = datetime.now(tz=UTC)
    _semantic(store, "facts/fade", 100, now=now)
    _semantic(store, "facts/keep", 400, {"retention": "invariant"}, now=now)
    _semantic(store, "facts/fast", 10, {"retention_half_life_days": 5}, now=now)
    before = {path: path.read_bytes() for path in (data / "memory").rglob("*") if path.is_file()}

    config = str(data / "agent-config.json")  # missing: the defaults
    assert distill_main(["--base-dir", str(data / "memory"), "--config", config, "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    forgetting = payload["forgetting"]
    items = {item["key_sha256"]: item for item in forgetting["items"]}
    fade, fast = _digest("semantic/facts/fade"), _digest("semantic/facts/fast")
    assert set(items) == {fade, fast}
    assert all("key" not in item and item["area"] == "semantic" for item in items.values())
    assert items[fade]["stage"] == "compact"
    assert "half-life 30 days" in items[fade]["reason"]
    assert items[fast]["stage"] == "light"  # 10 days = 2 half-lives of 5
    assert items[fast]["half_life_days"] == 5
    assert forgetting["protected"] == [
        {
            "area": "semantic",
            "key_sha256": _digest("semantic/facts/keep"),
            "code": "retention_invariant",
            "reason": "retention: invariant (never forgotten)",
        }
    ]
    assert payload["forgetting_candidates"] == 2
    after = {path: path.read_bytes() for path in (data / "memory").rglob("*") if path.is_file()}
    assert after == before  # the dry run wrote nothing


def test_new_outputs_never_show_a_key(tmp_path, capsys):
    """Keys can carry a memory's words: dry run, protected list, tag command (all paths)."""
    data, store = _data(tmp_path)
    now = datetime.now(tz=UTC)
    _semantic(store, f"facts/{CANARY}-fade", 100, now=now)
    _semantic(store, f"staging/{CANARY}-staged", 100, now=now)
    _semantic(store, f"facts/{CANARY}-keep", 400, {"retention": "invariant"}, now=now)
    _old_episode(store, f"chat-{CANARY}", {})
    config = str(data / "agent-config.json")
    base = ["--base-dir", str(data / "memory"), "--config", config]

    assert distill_main([*base, "--dry-run"]) == 0
    dry = capsys.readouterr()
    payload = json.loads(dry.out)
    assert len(payload["forgetting"]["items"]) == 2 and len(payload["forgetting"]["protected"]) == 1
    assert {i["area"] for i in payload["forgetting"]["items"]} == {"semantic", "staging"}
    assert CANARY not in dry.out + dry.err

    for args in (
        ("--id", f"semantic/facts/{CANARY}-keep", "--retention", "none"),
        ("--id", f"semantic/facts/{CANARY}-missing", "--retention", "invariant"),
        ("--id", f"semantic/facts/{CANARY}-missing", "--retention", "invariant", "--apply"),
        ("--id", f"episodic/chat-{CANARY}", "--retention", "invariant"),
        ("--id", f"episodic/chat-{CANARY}", "--retention", "invariant", "--apply"),
        ("--id", f"episodic/chat-{CANARY}", "--retention", "derived", "--half-life-days", "3"),
        ("--id", f"procedural/{CANARY}", "--retention", "invariant"),
        ("--id", f"semantic/../{CANARY}", "--retention", "invariant", "--apply"),
    ):
        _tag(data, *args)
        shown = capsys.readouterr()
        assert CANARY not in shown.out + shown.err, args

    # The run output's protected list follows the same rule (its items predate #173).
    _semantic(store, f"facts/{CANARY}-fade", 1, now=now)
    _semantic(store, f"staging/{CANARY}-staged", 1, now=now)
    assert distill_main(base) == 0
    run = capsys.readouterr()
    protected = json.loads(run.out)["forgetting"]["protected"]
    assert len(protected) == 1 and CANARY not in json.dumps(protected)
    assert CANARY not in run.err
    log = store.memory_dir / ARCHIVE_DIRNAME / AUDIT_LOG_NAME
    assert CANARY not in log.read_text(encoding="utf-8")


PUBLIC_ITEM_FIELDS = {
    "area",
    "key_sha256",
    "stage",
    "current_stage",
    "age_days",
    "last_accessed",
    "threshold_days",
    "chars",
    "limit",
    "half_life_days",
    "strength",
    "retention_invalid",
    "reason",
}


def test_the_dry_run_output_is_an_allow_list_no_metadata_string_reaches_it(tmp_path, capsys):
    """Every field is a fixed label, a digest or a number: an unknown ``forgetting_stage``
    (or any other front-matter string) never reaches stdout/stderr, also not via reason."""
    data, store = _data(tmp_path)
    now = datetime.now(tz=UTC)
    loud = {
        "forgetting_stage": f"{CANARY}-stage",
        "title": f"{CANARY}-title",
        "source_id": f"{CANARY}-source",
        "tags": [f"{CANARY}-tag"],
        "domain": f"{CANARY}-domain",
        "forgetting_archive_id": f"{CANARY}-archive",
    }
    _semantic(store, "facts/a", 100, dict(loud), body=f"{CANARY} body " + "z" * 5_000, now=now)
    _semantic(store, "facts/b", 100, {**loud, "retention": f"{CANARY}-ret"}, now=now)
    _semantic(store, "facts/c", 400, {**loud, "retention": "invariant"}, now=now)
    _semantic(store, "facts/d", 100, {**loud, "retention_half_life_days": f"{CANARY}-hl"}, now=now)
    _semantic(store, "facts/e", 100, {"forgetting_stage": "light"}, now=now)
    config = str(data / "agent-config.json")
    assert distill_main(["--base-dir", str(data / "memory"), "--config", config, "--dry-run"]) == 0
    shown = capsys.readouterr()
    assert CANARY not in shown.out + shown.err
    forgetting = json.loads(shown.out)["forgetting"]
    items = {item["key_sha256"]: item for item in forgetting["items"]}
    assert len(items) == 4
    for item in items.values():
        assert set(item) == PUBLIC_ITEM_FIELDS
        assert item["area"] in {"semantic", "staging"}
        assert item["stage"] in {"light", "compact", "archive"}
        assert item["current_stage"] in {"none", "light", "compact", "archive", "unknown"}
        for name in ("age_days", "threshold_days", "chars", "limit", "half_life_days"):
            assert type(item[name]) is int
        assert type(item["strength"]) is float and type(item["retention_invalid"]) is bool
    assert items[_digest("semantic/facts/a")]["current_stage"] == "unknown"
    assert "unknown -> compact" in items[_digest("semantic/facts/a")]["reason"]
    assert items[_digest("semantic/facts/e")]["current_stage"] == "light"
    (protected,) = forgetting["protected"]
    assert set(protected) == {"area", "key_sha256", "code", "reason"}


# -- the agent cannot tag its way out --------------------------------------------------------


def _old_episode(store, key, metadata, *, days=400, body="an old turn"):
    store.write(MemoryLayer.EPISODIC, key, body, {"harvested": True, **metadata})
    when = (NOW - timedelta(days=days)).timestamp()
    os.utime(store._path(MemoryLayer.EPISODIC, key), (when, when))


def _episodic_plan(data):
    report, plan = retention_report(
        DataLayout.at(data),
        RetentionPolicy.from_options({"episodic": {"max_age_days": 365}}),
        now=NOW,
    )
    removed = {key for layer, key in plan.memory if layer is MemoryLayer.EPISODIC}
    return report["stores"]["episodic"], removed


def test_a_self_assigned_invariant_in_episodic_front_matter_is_not_trusted(tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig

    data, store = _data(tmp_path)
    (data / "workspace").mkdir()
    policy, _ = build_policy(AgentConfig(), data)
    _old_episode(store, "chat-claim", {})
    path = store._path(MemoryLayer.EPISODIC, "chat-claim")
    assert policy.check(str(path)).allowed  # the agent's file tools may write episodic/
    assert not policy.check(str(store.index_path)).allowed  # the binding is on the floor
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("harvested: true", "harvested: true\nretention: invariant"))
    when = (NOW - timedelta(days=400)).timestamp()
    os.utime(path, (when, when))

    episodic, removed = _episodic_plan(data)
    assert removed == {"chat-claim"}  # not held
    assert episodic["retention"] == {
        "held_invariant": 0,
        "invariant": 0,
        "unverified": 1,
        "unverified_held": 0,
    }


def test_the_agent_cannot_write_a_semantic_tag_with_its_file_tools(tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig

    data, store = _data(tmp_path)
    (data / "workspace").mkdir()
    policy, _ = build_policy(AgentConfig(), data)
    _semantic(store, "facts/x", 1)
    assert not policy.check(str(store._path(MemoryLayer.SEMANTIC, "facts/x"))).allowed
    assert not policy.check(str(store._path(MemoryLayer.SEMANTIC, "staging/new"))).allowed


def test_memory_write_cannot_set_a_tag(tmp_path):
    from stiracore.tools.memory_tools import create_memory_tools

    _, store = _data(tmp_path)
    tools = {t.tool_name: t for t in create_memory_tools(store)}
    forged = "---\nretention: invariant\n---\nkeep me forever"
    tools["memory_write"](layer="episodic", key="chat-w", content=forged)
    assert "retention" not in store.read(MemoryLayer.EPISODIC, "chat-w").metadata
    denied = tools["memory_write"](layer="semantic", key="facts/w", content=forged)
    assert "denied" in str(denied)
    assert store.read(MemoryLayer.SEMANTIC, "facts/w") is None


def test_harvest_does_not_copy_a_tag_from_an_episode(tmp_path):
    _, store = _data(tmp_path)
    store.write(
        MemoryLayer.EPISODIC,
        "chat-h",
        "Important decision: always keep the release checklist. " * 3,
        {"retention": "invariant", "retention_half_life_days": 3650, "tags": ["x"]},
    )
    HarvestScheduler(
        store, None, "stira", distiller=RuleBasedDistiller(AmygdalaPass()), stage_auto=True
    ).run_distillation()
    derived = [
        e
        for e in store.list_entries(MemoryLayer.SEMANTIC, limit=100)
        if e.metadata.get("source_id") == "chat-h"
    ]
    assert derived
    for entry in derived:
        assert "retention" not in entry.metadata
        assert "retention_half_life_days" not in entry.metadata


# -- the human command -------------------------------------------------------------------------


def _tag(data, *args):
    return memory_main(["tag", "--data-dir", str(data), *args])


def test_tag_previews_by_default_and_applies_under_the_owner_lock(tmp_path, capsys):
    data, store = _data(tmp_path)
    _semantic(store, "facts/x", 400, {"title": "x"}, f"body {CANARY}")
    before = store.read_text(MemoryLayer.SEMANTIC, "facts/x")

    assert _tag(data, "--id", "semantic/facts/x", "--retention", "invariant") == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["current"] == {"kind": "derived", "half_life_days": 30, "tagged": False}
    assert preview["new"] == {"kind": "invariant", "tagged": True}
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/x") == before

    lock = DataDirOwnerLock(data)
    lock.acquire()
    try:
        assert _tag(data, "--id", "semantic/facts/x", "--retention", "invariant", "--apply") == 3
    finally:
        lock.release()
    capsys.readouterr()
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/x") == before

    assert _tag(data, "--id", "semantic/facts/x", "--retention", "invariant", "--apply") == 0
    out = capsys.readouterr().out
    assert CANARY not in out and "facts/x" not in out  # digests, not the key
    assert store.read(MemoryLayer.SEMANTIC, "facts/x").metadata["retention"] == "invariant"
    assert store.read(MemoryLayer.SEMANTIC, "facts/x").content == f"body {CANARY}"
    assert _selected(store) == {}

    log = store.memory_dir / ARCHIVE_DIRNAME / AUDIT_LOG_NAME
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    tagged = [line for line in lines if line["kind"] == "retention.tagged"]
    assert len(tagged) == 1
    assert set(tagged[0]["body"]) == {"layer", "key_sha256", "previous", "retention"}
    assert CANARY not in log.read_text(encoding="utf-8")
    assert "facts/x" not in log.read_text(encoding="utf-8")

    # none clears the tag: the entry is forgotten again as before.
    assert _tag(data, "--id", "semantic/facts/x", "--retention", "none", "--apply") == 0
    capsys.readouterr()
    assert "retention" not in store.read(MemoryLayer.SEMANTIC, "facts/x").metadata
    _age(store, "facts/x", 400)
    assert _selected(store) == {"facts/x": "archive"}


def test_tag_sets_a_half_life_and_refuses_bad_input(tmp_path, capsys):
    data, store = _data(tmp_path)
    _semantic(store, "facts/x", 20)
    args = ("--id", "semantic/facts/x", "--retention", "derived")
    assert _tag(data, *args, "--half-life-days", "10", "--apply") == 0
    capsys.readouterr()
    assert store.read(MemoryLayer.SEMANTIC, "facts/x").metadata["retention_half_life_days"] == 10
    _age(store, "facts/x", 20)
    assert _selected(store) == {"facts/x": "light"}
    for bad in (
        ("--half-life-days", "0"),
        ("--half-life-days", "4000"),
    ):
        assert _tag(data, *args, *bad, "--apply") == 2
    assert _tag(data, "--id", "semantic/facts/x", "--retention", "invariant",
                "--half-life-days", "5") == 2  # fmt: skip
    assert _tag(data, "--id", "semantic/missing", "--retention", "invariant") == 1
    assert _tag(data, "--id", "procedural/rules", "--retention", "invariant") == 2
    assert _tag(data, "--id", "episodic/chat-1", "--retention", "derived",
                "--half-life-days", "5") == 2  # fmt: skip


def test_tag_refuses_an_entry_whose_forgetting_can_still_be_undone(tmp_path, capsys):
    """Tagging would change the compressed bytes and block --undo-forgetting."""
    data, store = _data(tmp_path)
    _semantic(store, "facts/x", 100)
    assert MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).applied == 1
    compressed = store.read_text(MemoryLayer.SEMANTIC, "facts/x")
    assert _tag(data, "--id", "semantic/facts/x", "--retention", "invariant", "--apply") == 1
    assert json.loads(capsys.readouterr().out)["refused"]["code"] == "E_FORGETTING_UNDOABLE"
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/x") == compressed


def _retention_apply(data: Path, capsys) -> dict:
    config = data / "agent-config.json"
    config.write_text(json.dumps({"memory_retention": {"episodic": {"max_age_days": 365}}}))
    try:
        code = memory_main(["retention", "--data-dir", str(data), "--apply"])
    finally:
        config.unlink()
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 1), payload
    return payload


def _counts(**values):
    return {"held_invariant": 0, "invariant": 0, "unverified": 0, "unverified_held": 0, **values}


def test_a_human_invariant_survives_any_edit_by_the_agent(tmp_path, capsys):
    """Approval and integrity are separate: an edit (even dropping the front-matter field)
    never lifts a human's tag; the entry stays held and is reported for a human look."""
    data, store = _data(tmp_path)
    _old_episode(store, "chat-keep", {})
    _old_episode(store, "chat-fade", {})
    assert _tag(data, "--id", "episodic/chat-keep", "--retention", "invariant", "--apply") == 0
    capsys.readouterr()
    path = store._path(MemoryLayer.EPISODIC, "chat-keep")
    when = (NOW - timedelta(days=400)).timestamp()
    assert path.stat().st_mtime == pytest.approx(when)

    episodic, removed = _episodic_plan(data)
    assert removed == {"chat-fade"}
    assert episodic["retention"] == _counts(held_invariant=1, invariant=1)

    # What the agent's file tools can do: append a character, drop the tag field.
    path.write_text(path.read_text(encoding="utf-8") + "x", encoding="utf-8")
    os.utime(path, (when, when))
    episodic, removed = _episodic_plan(data)
    assert "chat-keep" not in removed
    assert episodic["retention"] == _counts(held_invariant=1, invariant=1, unverified_held=1)
    text = path.read_text(encoding="utf-8").replace("retention: invariant\n", "")
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when, when))
    episodic, removed = _episodic_plan(data)
    assert "chat-keep" not in removed
    assert episodic["retention"]["unverified_held"] == 1

    # The real run removes the other one only.
    result = _retention_apply(data, capsys)
    assert result["result"]["applied"]
    assert path.exists()
    assert store.read(MemoryLayer.EPISODIC, "chat-fade") is None

    # The preview tells the human the text changed since the tag.
    assert _tag(data, "--id", "episodic/chat-keep", "--retention", "none") == 0
    assert json.loads(capsys.readouterr().out)["recorded"] == {
        "kind": "invariant",
        "text": "changed",
    }
    # Only the human command lifts it.
    assert _tag(data, "--id", "episodic/chat-keep", "--retention", "none", "--apply") == 0
    capsys.readouterr()
    os.utime(path, (when, when))
    _, removed = _episodic_plan(data)
    assert removed == {"chat-keep"}


def test_harvest_keeps_a_verified_tag_on_its_own_rewrite(tmp_path, capsys):
    data, store = _data(tmp_path)
    store.write(
        MemoryLayer.EPISODIC,
        "chat-keep",
        "Important decision: always keep the release checklist. " * 3,
        {},
    )
    assert _tag(data, "--id", "episodic/chat-keep", "--retention", "invariant", "--apply") == 0
    capsys.readouterr()
    HarvestScheduler(
        store, None, "stira", distiller=RuleBasedDistiller(AmygdalaPass()), stage_auto=True
    ).run_distillation()
    assert store.read(MemoryLayer.EPISODIC, "chat-keep").metadata.get("harvested")
    path = store._path(MemoryLayer.EPISODIC, "chat-keep")
    when = (NOW - timedelta(days=400)).timestamp()
    os.utime(path, (when, when))
    episodic, removed = _episodic_plan(data)
    assert "chat-keep" not in removed
    assert episodic["retention"] == _counts(held_invariant=1, invariant=1)  # verified


def _harvest_with(store, hook):
    """Run Harvest; ``hook(phase)`` runs while the episode is scored (after the sweep read
    it, before the rewrite) and right after the rewrite of ``chat-keep``."""
    original = store.write
    distiller = RuleBasedDistiller(AmygdalaPass())
    score = distiller.distill

    def distill(content, metadata):
        hook("scoring")
        return score(content, metadata)

    distiller.distill = distill

    def write(layer, key, *args, **kwargs):
        entry = original(layer, key, *args, **kwargs)
        if layer is MemoryLayer.EPISODIC and key == "chat-keep":
            hook("after")
        return entry

    store.write = write
    try:
        HarvestScheduler(
            store, None, "stira", distiller=distiller, stage_auto=True
        ).run_distillation()
    finally:
        del store.write


@pytest.mark.parametrize("phase", ["scoring", "after"])
def test_harvest_never_moves_a_tag_onto_text_it_did_not_write(tmp_path, capsys, phase):
    """Another writer's edit after the sweep read the episode (before Harvest checks and
    rewrites it), or between the rewrite and the re-binding, must stay a mismatch: the
    human's tag is never moved onto text nobody approved."""
    data, store = _data(tmp_path)
    store.write(
        MemoryLayer.EPISODIC,
        "chat-keep",
        "Important decision: always keep the release checklist. " * 3,
        {},
    )
    assert _tag(data, "--id", "episodic/chat-keep", "--retention", "invariant", "--apply") == 0
    capsys.readouterr()
    path = store._path(MemoryLayer.EPISODIC, "chat-keep")
    forged = {}

    def hook(now):
        if now == phase:
            text = path.read_text(encoding="utf-8") + "\ninjected by another writer"
            path.write_text(text, encoding="utf-8")
            forged["text"] = text

    _harvest_with(store, hook)
    current = path.read_text(encoding="utf-8")
    if phase == "after":
        assert current == forged["text"]
    with sqlite3.connect(store.index_path) as connection:
        kind, digest = connection.execute(
            "SELECT retention, retention_sha256 FROM memory_entries"
            " WHERE layer = 'episodic' AND key = 'chat-keep'"
        ).fetchone()
    assert kind == "invariant"  # still the human's tag ...
    assert digest != hashlib.sha256(current.encode()).hexdigest()  # ... not approving this
    when = (NOW - timedelta(days=400)).timestamp()
    os.utime(path, (when, when))
    episodic, removed = _episodic_plan(data)
    assert "chat-keep" not in removed
    assert episodic["retention"]["unverified_held"] == 1


def test_a_delete_request_still_removes_an_invariant_entry(tmp_path):
    """invariant stops maintenance, not the human delete command."""
    data, store = _data(tmp_path)
    _semantic(store, "facts/x", 1, {"retention": "invariant"}, f"body {CANARY}")
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse(["semantic/facts/x"], []))
    result = apply_forget(layout, plan, at=NOW)
    assert result["job"]["state"] == "completed"
    assert store.read(MemoryLayer.SEMANTIC, "facts/x") is None


def test_the_retention_report_of_untagged_data_has_zero_tag_counts(tmp_path):
    data, store = _data(tmp_path)
    _old_episode(store, "chat-1", {})
    episodic, removed = _episodic_plan(data)
    assert removed == {"chat-1"}
    assert episodic["retention"] == _counts()
