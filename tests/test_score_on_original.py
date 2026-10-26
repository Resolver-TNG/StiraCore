"""#293: score each turn on its original text and keep the human gate working.

The Episodic summary flattens headings, bullets and code blocks and cuts long
text, which used to push important turns out of staging. These cases are the
ones measured in the audit (original vs summary scores).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stiracore.config import AgentConfig
from stiracore.control import distillation_cli
from stiracore.control.distillers import DistillationDecision, RuleBasedDistiller
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.staging_review import load_rejections
from stiracore.core.amygdala import SCORING_VERSION, AmygdalaPass
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.tools.episodic_formatter import EpisodicFormatter, record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ROOT = Path(__file__).resolve().parents[1]

# Heading + bullets + code block in the reply (audit: original 0.80 -> summary 0.60).
STRUCTURED = (
    "キャッシュ層の設計はどうする？",
    (
        "## 結論\n- 読み取りは Redis を先に見る\n- 書き込み時に無効化する\n\n"
        "```python\ncache.invalidate(key)\n```\n\n前回の不具合はこれで防げる。"
    ),
)
_FILLER = "".join(
    f"{i}番目の項目を順番に見直して、細かい点を一つずつ整理していきました。" for i in range(1, 8)
)
# Long message whose conclusion is at the end (audit: original 0.65 -> summary 0.40).
LONG_TAIL = (
    "昨日のデプロイで不具合が出ました。"
    + _FILLER
    + _FILLER
    + "結局、キャッシュは使わない方針にします。",
    "了解です。次は何から始めますか？",
)


def _server_episode(store: MemoryStore, key: str, text: str, metadata: dict) -> None:
    """An episode as the server's episode writer leaves it: front matter + verified origin
    (#506: a recorded ``importance`` counts only for such an episode)."""
    store.write(MemoryLayer.EPISODIC, key, text, metadata)
    record_origin(store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat")


def _formatter() -> EpisodicFormatter:
    return EpisodicFormatter(user_label="マスター", agent_label="リゾルバ")


def _staged(store: MemoryStore) -> list:
    return [
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=1000)
        if entry.key.startswith("staging/")
    ]


def _promoted(store: MemoryStore) -> list:
    return [
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=1000)
        if not entry.key.startswith("staging/") and entry.key != "index"
    ]


# ------------------------------------------------------------ (a) original text


def test_summary_loses_the_signals_that_the_original_has():
    """Pins the measured gap the fix closes (scoring the summary is not enough)."""
    scorer = AmygdalaPass()
    for (user, agent), original, summary in ((STRUCTURED, 0.8, 0.6), (LONG_TAIL, 0.65, 0.4)):
        assert scorer.score(f"{user}\n{agent}") == pytest.approx(original)
        assert scorer.score(_formatter().format(user, agent).content) == pytest.approx(summary)


@pytest.mark.parametrize(
    ("turn", "expected"), [(STRUCTURED, 0.8), (LONG_TAIL, 0.65)], ids=["structured", "long-tail"]
)
def test_record_episode_keeps_the_original_turn_score(tmp_path, turn, expected):
    store = MemoryStore(tmp_path, "stira")
    entry = record_episode(store, *turn, formatter=_formatter(), key="turn")
    assert entry is not None
    stored = store.read(MemoryLayer.EPISODIC, "turn")
    assert stored.metadata["importance"] == pytest.approx(expected)
    assert isinstance(stored.metadata["importance"], float)
    assert stored.metadata["importance_basis"] == "original-turn"
    # Only the number is kept; the raw text is still not duplicated into metadata.
    assert "Redis" not in json.dumps(stored.metadata, ensure_ascii=False)


def test_structured_turn_reaches_staging_through_harvest(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    record_episode(store, *STRUCTURED, formatter=_formatter(), key="turn")

    result = HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()

    assert (result.semantic, result.knowledge) == (1, 0)
    (staged,) = _staged(store)
    assert staged.metadata["score"] == pytest.approx(0.8)
    assert staged.metadata["harvest_band"] == "staging"
    assert _promoted(store) == []


class _Leaky:
    """A scorer whose exception message carries the original text."""

    def score(self, content, *_args, **_kwargs):
        raise RuntimeError(f"cannot score: {content}")


def test_scorer_failure_records_a_reason_code_without_logging_the_text(tmp_path, caplog):
    store = MemoryStore(tmp_path, "stira")
    with caplog.at_level(logging.DEBUG):
        entry = record_episode(store, "SECRET-USER-TEXT", "SECRET-REPLY", key="t", scorer=_Leaky())
    assert entry is not None
    assert "importance" not in entry.metadata
    assert entry.metadata["importance_error"] == "E_ORIGINAL_SCORE_FAILED"
    (record,) = [r for r in caplog.records if "scoring failed" in r.getMessage()]
    assert "E_ORIGINAL_SCORE_FAILED" in record.getMessage()
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is None and record.exc_text is None
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("value", ["high", float("nan"), 1.5, True])
def test_invalid_scores_are_reason_coded(tmp_path, value):
    class _Odd:
        def score(self, *_args, **_kwargs):
            return value

    entry = record_episode(MemoryStore(tmp_path, "stira"), "q", "a", scorer=_Odd())
    assert entry.metadata["importance_error"] == "E_ORIGINAL_SCORE_INVALID"
    assert "importance" not in entry.metadata


@pytest.mark.parametrize("stage_auto", [True, False])
def test_failed_original_score_is_held_for_review(tmp_path, stage_auto):
    """Not re-scored from the summary: a trivial turn would otherwise be discarded."""
    store = MemoryStore(tmp_path, "stira")
    record_episode(store, "ok", "fine", key="turn", scorer=_Leaky())

    result = HarvestScheduler(
        store, AmygdalaPass(), "stira", stage_auto=stage_auto
    ).run_distillation()

    assert (result.semantic, result.knowledge, result.discard) == (1, 0, 0)
    (staged,) = _staged(store)
    assert staged.metadata["harvest_reason"].startswith("E_ORIGINAL_SCORE_FAILED")
    episode = store.read(MemoryLayer.EPISODIC, "turn")
    assert episode.metadata["harvest_route"] == "staging"


def test_extra_metadata_cannot_set_the_score_fields(tmp_path, caplog):
    store = MemoryStore(tmp_path, "stira")
    forged = {
        "importance": 0.1,
        "importance_basis": "hand",
        "importance_version": "v0",
        "importance_error": "E_FAKE",
        "turn": 7,
    }
    with caplog.at_level(logging.WARNING):
        entry = record_episode(store, *STRUCTURED, formatter=_formatter(), extra_metadata=forged)
    assert entry.metadata["importance"] == pytest.approx(0.8)
    assert entry.metadata["importance_basis"] == "original-turn"
    assert entry.metadata["importance_version"] == SCORING_VERSION
    assert "importance_error" not in entry.metadata
    assert entry.metadata["turn"] == 7
    assert "ignored reserved metadata keys" in caplog.text


def _agent_written_episode(store: MemoryStore, key: str, importance: float, text: str) -> None:
    """What file_write can put in the episodic layer (a write root, #271)."""
    path = store.memory_dir / "episodic" / f"{key}.md"
    path.write_text(f"---\nimportance: {importance}\n---\n{text}", encoding="utf-8")


@pytest.mark.parametrize(
    ("forged", "text", "stage_auto", "expected"),
    [
        # A forged low value cannot hide a turn: the stored text is re-scored (floor).
        (0.0, "## 決定\n- API の不具合を修正する方針？", True, "staging"),
        (0.0, "## 決定\n- API の不具合を修正する方針？", False, "staging"),
        # A forged high value is ignored (#506, ADR 0018 phase 0): the episode is not what the
        # server wrote, so the stored text's own score decides ("hello" = 0.2, discard).
        (0.95, "hello", True, "discard"),
        (0.5, "hello", True, "discard"),
        (0.5, "hello", False, "discard"),
    ],
)
def test_forged_importance_in_an_agent_written_episode(
    tmp_path, forged, text, stage_auto, expected
):
    store = MemoryStore(tmp_path, "stira")
    _agent_written_episode(store, "forged", forged, text)

    result = HarvestScheduler(
        store, AmygdalaPass(), "stira", stage_auto=stage_auto
    ).run_distillation()

    counts = {"staging": result.semantic, "knowledge": result.knowledge, "discard": result.discard}
    assert counts[expected] == 1
    assert sum(counts.values()) == 1


# ------------------------------------------------- (d) names next to Japanese


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今日はAPI設計の話", 0.4),  # 0.2 base + decision + name (was 0.35: no name bonus)
        ("今日は API 設計の話", 0.4),
        ("今日は設計の話", 0.35),
        ("このbugを直す", 0.35),  # English keyword next to Japanese
        ("debugging only", 0.2),  # still a whole-word match
    ],
)
def test_ascii_words_next_to_japanese_are_recognized(text, expected):
    assert AmygdalaPass().score(text) == pytest.approx(expected)


# --------------------------------------------- (b) auto band held for review


def test_auto_band_is_staged_by_default(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    record_episode(store, *LONG_TAIL, formatter=_formatter(), key="turn")

    result = HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()

    assert (result.semantic, result.knowledge) == (1, 0)
    (staged,) = _staged(store)
    assert staged.key.startswith("staging/knowledge/general/harvest-")
    assert staged.metadata["harvest_band"] == "auto"
    assert staged.metadata["harvest_route"] == "staging"
    episode = store.read(MemoryLayer.EPISODIC, "turn")
    assert episode.metadata["harvest_route"] == "staging"
    assert episode.metadata["harvest_band"] == "auto"
    assert _promoted(store) == []


def test_auto_band_promotes_directly_only_when_disabled(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    record_episode(store, *LONG_TAIL, formatter=_formatter(), key="turn")

    result = HarvestScheduler(store, AmygdalaPass(), "stira", stage_auto=False).run_distillation()

    assert (result.semantic, result.knowledge) == (0, 1)
    assert _staged(store) == []
    (promoted,) = _promoted(store)
    assert promoted.key.startswith("knowledge/general/harvest-")


def test_agent_config_stages_auto_band_by_default(tmp_path):
    assert AgentConfig().harvest_stage_auto is True
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"harvest_stage_auto": False}), encoding="utf-8")
    loaded = AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
    assert loaded.harvest_stage_auto is False
    assert loaded.as_dict()["harvest_stage_auto"] is False
    with pytest.raises(ValueError, match="harvest_stage_auto"):
        AgentConfig(harvest_stage_auto="yes")


@pytest.mark.parametrize(("stage_auto", "bucket"), [(True, "semantic"), (False, "knowledge")])
def test_distillation_cli_follows_the_setting(tmp_path, capsys, stage_auto, bucket):
    base_dir = tmp_path / "memory"
    config_path = tmp_path / "agent.json"
    AgentConfig(path=config_path, harvest_stage_auto=stage_auto).save()
    store = MemoryStore(base_dir, "stira")
    _server_episode(store, "note", "Reusable note", {"importance": 0.5})

    code = distillation_cli.main(["--base-dir", str(base_dir), "--config", str(config_path)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["harvest"][bucket] == 1
    assert payload["staging_pending"] == (1 if stage_auto else 0)


# ------------------------------------------------------- (c) staging CLI


def _staging_main(argv: list[str]) -> int:
    from stiracore.control import staging_cli  # new in #293

    return staging_cli.main(argv)


def _cli(capsys, base_dir: Path, *args: str) -> tuple[int, object]:
    code = _staging_main(["--base-dir", str(base_dir), "--agent-id", "stira", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def _two_staged(tmp_path) -> tuple[Path, MemoryStore]:
    base_dir = tmp_path / "memory"
    store = MemoryStore(base_dir, "stira")
    _server_episode(store, "high", "High importance decision", {"importance": 0.9})
    _server_episode(store, "mid", "Mid importance note", {"importance": 0.5, "tags": ["ops"]})
    HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()
    return base_dir, store


def test_staging_cli_lists_without_content(tmp_path, capsys):
    base_dir, _ = _two_staged(tmp_path)
    code, payload = _cli(capsys, base_dir, "list")
    assert code == 0
    assert payload["count"] == 2
    by_band = {item["band"]: item for item in payload["entries"]}
    assert set(by_band) == {"auto", "staging"}
    assert by_band["auto"]["key"].startswith("staging/knowledge/ops/")
    assert "content" not in by_band["auto"]

    code, shown = _cli(capsys, base_dir, "show", by_band["auto"]["key"])
    assert code == 0 and shown["content"] == "Mid importance note"


def test_staging_cli_approve_moves_to_final_home_and_indexes(tmp_path, capsys):
    base_dir, store = _two_staged(tmp_path)
    _, listed = _cli(capsys, base_dir, "list")
    auto_key = next(item["key"] for item in listed["entries"] if item["band"] == "auto")

    code, payload = _cli(capsys, base_dir, "approve", auto_key)

    assert code == 0
    target = auto_key.removeprefix("staging/")
    assert payload == {"approved": auto_key, "key": target, "index": "updated"}
    promoted = store.read(MemoryLayer.SEMANTIC, target)
    assert promoted.metadata["review"] == "approved"
    assert store.read(MemoryLayer.SEMANTIC, auto_key) is None
    index = store.read(MemoryLayer.SEMANTIC, "index")
    assert f"semantic/{target}.md" in index.content
    assert _cli(capsys, base_dir, "list")[1]["count"] == 1


def test_staging_cli_reject_records_and_is_not_reproposed(tmp_path, capsys):
    base_dir, store = _two_staged(tmp_path)
    _, listed = _cli(capsys, base_dir, "list")
    high_key = next(item["key"] for item in listed["entries"] if item["band"] == "staging")

    code, payload = _cli(capsys, base_dir, "reject", high_key, "--reason", "not durable")

    assert code == 0 and payload["rejected"] == high_key
    assert [record["reason"] for record in load_rejections(store)] == ["not durable"]
    _server_episode(store, "high", "High importance decision", {"importance": 0.9})
    HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()
    assert [item["band"] for item in _cli(capsys, base_dir, "list")[1]["entries"]] == ["auto"]


def test_staging_cli_unknown_key_fails_without_writing(tmp_path, capsys):
    base_dir, _ = _two_staged(tmp_path)
    code = _staging_main(["--base-dir", str(base_dir), "approve", "staging/nope"])
    captured = capsys.readouterr()
    assert code == 1
    assert "staging/nope" in captured.err
    assert _cli(capsys, base_dir, "list")[1]["count"] == 2


@pytest.mark.skipif(sys.platform == "win32", reason="bash script is POSIX only")
def test_stira_ctl_lists_the_staging_action():
    script = ROOT / "scripts" / "stira_ctl.sh"
    result = subprocess.run(
        ["bash", str(script), "--help"], capture_output=True, text=True, timeout=30, check=False
    )
    assert "staging" in result.stderr


# --------------------------------------------------- review follow-ups (#319)


def _tree(path: Path) -> dict[str, bytes]:
    """Every memory file; the SQLite index is left out (a read records last_accessed)."""
    return {
        p.relative_to(path).as_posix(): (b"<link>" if p.is_symlink() else p.read_bytes())
        for p in sorted(path.rglob("*"))
        if (p.is_symlink() or p.is_file()) and "index.sqlite3" not in p.name
    }


@pytest.mark.parametrize(
    "key",
    [
        "staging/../index",
        "../knowledge/x",
        "staging/./x",
        "/etc/passwd",
        "staging//x",
        "staging/a\\b",
        "staging/x\0y",
    ],
)
@pytest.mark.parametrize("command", ["show", "approve", "reject"])
def test_staging_cli_refuses_escaping_keys(tmp_path, capsys, key, command):
    base_dir, _ = _two_staged(tmp_path)
    before = _tree(base_dir)
    code = _staging_main(["--base-dir", str(base_dir), command, key])
    assert code == 1
    assert "path" in capsys.readouterr().err
    assert _tree(base_dir) == before


@pytest.mark.parametrize("agent_id", ["..", "../other", "a/b", "", "x\\y"])
def test_staging_cli_refuses_an_escaping_agent_id(tmp_path, capsys, agent_id):
    base_dir, _ = _two_staged(tmp_path)
    before = _tree(base_dir)
    code = _staging_main(["--base-dir", str(base_dir), "--agent-id", agent_id, "list"])
    assert code == 1
    assert "agent_id" in capsys.readouterr().err
    assert _tree(base_dir) == before


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_staging_cli_ignores_or_refuses_symlinks(tmp_path, capsys):
    base_dir, store = _two_staged(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nscore: 0.9\n---\nnot staged", encoding="utf-8")
    staging_dir = store.memory_dir / "semantic" / "staging"
    (staging_dir / "linked.md").symlink_to(outside)
    # A symlinked file is not listed, and cannot be shown or approved.
    _, listed = _cli(capsys, base_dir, "list")
    assert "staging/linked" not in [item["key"] for item in listed["entries"]]
    for command in ("show", "approve", "reject"):
        assert _staging_main(["--base-dir", str(base_dir), command, "staging/linked"]) == 1
        assert "symbolic" in capsys.readouterr().err
    assert outside.read_text(encoding="utf-8").endswith("not staged")
    assert not (store.memory_dir / "semantic" / "linked.md").exists()
    # A symlinked staging directory is refused as a whole.
    (store.memory_dir / "semantic" / "elsewhere").mkdir()
    linked_dir = store.memory_dir / "semantic" / "staging" / "sub"
    linked_dir.symlink_to(store.memory_dir / "semantic" / "elsewhere", target_is_directory=True)
    assert _staging_main(["--base-dir", str(base_dir), "approve", "staging/sub/x"]) == 1
    assert "symbolic" in capsys.readouterr().err


@pytest.mark.parametrize("same_source", [True, False])
def test_staging_cli_never_overwrites_an_existing_target(tmp_path, capsys, same_source):
    base_dir, store = _two_staged(tmp_path)
    _, listed = _cli(capsys, base_dir, "list")
    item = next(item for item in listed["entries"] if item["band"] == "staging")
    target = item["key"].removeprefix("staging/")
    source = item["source_id"] if same_source else "someone-else"
    store.write(MemoryLayer.SEMANTIC, target, "kept as is", {"source_id": source})
    before = _tree(base_dir)

    code = _staging_main(["--base-dir", str(base_dir), "approve", item["key"]])

    assert code == 1
    assert "E_TARGET_EXISTS" in capsys.readouterr().err
    assert _tree(base_dir) == before
    assert store.read(MemoryLayer.SEMANTIC, target).content == "kept as is"


def test_staging_cli_reports_a_stale_index_and_repairs_on_rerun(tmp_path, capsys, monkeypatch):
    from stiracore.control import staging_cli

    base_dir, store = _two_staged(tmp_path)
    _, listed = _cli(capsys, base_dir, "list")
    key = next(item["key"] for item in listed["entries"] if item["band"] == "staging")
    target = key.removeprefix("staging/")
    real = staging_cli.write_semantic_index

    def _disk_full(_store):
        raise OSError(28, "No space left on device", "/private/secret/path")

    monkeypatch.setattr(staging_cli, "write_semantic_index", _disk_full)
    code = staging_cli.main(["--base-dir", str(base_dir), "approve", key])
    captured = capsys.readouterr()
    assert code == 3
    payload = json.loads(captured.out)
    assert payload == {"approved": key, "key": target, "index": "stale", "error": "E_INDEX_WRITE"}
    assert "E_INDEX_WRITE" in captured.err and "/private/secret" not in captured.err
    assert store.read(MemoryLayer.SEMANTIC, target).metadata["review"] == "approved"
    index = store.read(MemoryLayer.SEMANTIC, "index")
    assert index is None or target not in index.content

    monkeypatch.setattr(staging_cli, "write_semantic_index", real)
    code, payload = _cli(capsys, base_dir, "approve", key)  # the same command, repeated
    assert code == 0
    assert payload["already_approved"] is True and payload["index"] == "updated"
    assert f"semantic/{target}.md" in store.read(MemoryLayer.SEMANTIC, "index").content
    code, payload = _cli(capsys, base_dir, "reindex")
    assert (code, payload) == (0, {"index": "updated"})


def test_no_agent_tool_can_run_the_staging_cli(monkeypatch):
    from stiracore.agent.nxp_agent import _tool_name
    from stiracore.tools import tool_registry

    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    names = {_tool_name(tool) for tool in tool_registry.get_tools()}
    assert not names & tool_registry.EXEC_TOOL_NAMES  # no shell / batch / cron / use_agent
    assert not any(name and "staging" in name for name in names)


def test_scoring_version_is_recorded_and_used_by_harvest(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    entry = record_episode(store, *STRUCTURED, formatter=_formatter())
    assert entry.metadata["importance_version"] == SCORING_VERSION == RuleBasedDistiller.version


class _OldRules:
    """Stands in for the v2 sweep: importance overrode the text; '今日はAPI設計' scored 0.35."""

    version = "amygdala/v2-multilingual"

    def distill(self, content, metadata):
        score = metadata.get("importance", 0.35)
        return DistillationDecision(score, AmygdalaPass().route(score), "v2")


def test_v3_sweep_over_v2_state_keeps_reviews_and_rejections(tmp_path, capsys):
    """Re-harvesting after the version bump must not resurrect or duplicate anything."""
    base_dir = tmp_path / "memory"
    store = MemoryStore(base_dir, "stira")
    _server_episode(store, "pending", "Pending decision", {"importance": 0.9})
    _server_episode(store, "approved", "Approved decision", {"importance": 0.9})
    _server_episode(store, "rejected", "Rejected decision", {"importance": 0.9})
    _server_episode(store, "auto", "Auto note", {"importance": 0.5})
    store.write(MemoryLayer.EPISODIC, "discarded", "今日はAPI設計の話", {})  # v2: 0.35
    old = HarvestScheduler(store, AmygdalaPass(), "stira", distiller=_OldRules(), stage_auto=False)
    assert old.run_distillation().discard == 1
    _, listed = _cli(capsys, base_dir, "list")
    keys = {item["source_id"]: item["key"] for item in listed["entries"]}
    assert _cli(capsys, base_dir, "approve", keys["approved"])[0] == 0
    assert _cli(capsys, base_dir, "reject", keys["rejected"])[0] == 0
    semantic_before = {e.key for e in store.list_entries(MemoryLayer.SEMANTIC, limit=1000)}
    ledger_before = load_rejections(store)

    result = HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()

    semantic_after = {e.key for e in store.list_entries(MemoryLayer.SEMANTIC, limit=1000)}
    new = semantic_after - semantic_before
    # Only the episode that v2 discarded is re-evaluated; it now scores 0.40 and is staged.
    assert len(new) == 1 and next(iter(new)).startswith("staging/knowledge/general/")
    assert (result.semantic, result.skipped) == (1, 4)
    assert load_rejections(store) == ledger_before
    _, listed = _cli(capsys, base_dir, "list")
    assert sorted(item["source_id"] for item in listed["entries"]) == ["discarded", "pending"]
    routes = {
        key: store.read(MemoryLayer.EPISODIC, key).metadata
        for key in ("pending", "approved", "rejected", "auto", "discarded")
    }
    assert routes["rejected"]["harvest_route"] == "rejected"
    for key in ("pending", "approved", "auto"):
        assert routes[key]["harvest_route"] == "duplicate", key
    assert all(meta["harvest_version"] == SCORING_VERSION for meta in routes.values())
    # The saved importance (no basis/version: written before #293) is still honoured.
    assert routes["pending"]["harvest_score"] == 0.9


@pytest.mark.parametrize(
    "text",
    ["\n" * 100_000, " \n" * 50_000, " " * 100_000, "\t\t\t\n" * 25_000],
    ids=["newlines", "space-newlines", "spaces", "tab-lines"],
)
def test_scoring_100kb_of_blank_lines_is_linear(text):
    started = time.perf_counter()
    AmygdalaPass().score(text)
    # Measured ~10 ms on an M1 laptop; the quadratic pattern took ~18 s for 40,000 newlines.
    assert time.perf_counter() - started < float(os.environ.get("STIRA_SCORE_BUDGET_S", "1.0"))


# --------------------------------------------------- review round 2 (#319)


class _EchoingScorer(AmygdalaPass):
    """Re-scoring the stored text fails with the text as the exception message."""

    def score(self, content, metadata=None):
        raise RuntimeError(content)


def test_harvest_failure_log_carries_no_episode_text(tmp_path, caplog):
    store = MemoryStore(tmp_path, "stira")
    store.write(MemoryLayer.EPISODIC, "ep", "SECRET-EPISODE-BODY", {})
    with caplog.at_level(logging.DEBUG):
        result = HarvestScheduler(store, _EchoingScorer(), "stira").run_distillation()
    assert result.errors == 1
    (record,) = [r for r in caplog.records if "Harvest episode failed" in r.getMessage()]
    assert "E_HARVEST_EPISODE" in record.getMessage() and "RuntimeError" in record.getMessage()
    assert record.exc_info is None and record.exc_text is None
    assert "SECRET" not in caplog.text


def test_formatter_failure_logs_carry_no_turn_text(tmp_path, caplog, monkeypatch):
    from stiracore.tools import episodic_formatter

    class _Echo:
        def format(self, user, agent):
            raise RuntimeError(f"{user} {agent}")

    def _fallback_echo(user, agent, **_):
        raise RuntimeError(f"{user} {agent}")

    store = MemoryStore(tmp_path, "stira")
    with caplog.at_level(logging.DEBUG):
        assert record_episode(store, "SECRET-U", "SECRET-A", formatter=_Echo()) is not None
        monkeypatch.setattr(episodic_formatter, "fallback_format", _fallback_echo)
        assert record_episode(store, "SECRET-U", "SECRET-A", formatter=_Echo()) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("E_FORMATTER_FAILED, RuntimeError" in m for m in messages)
    assert any("E_FALLBACK_FAILED, RuntimeError" in m for m in messages)
    assert all(r.exc_info is None and r.exc_text is None for r in caplog.records)
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("exc", [ValueError("SECRET"), KeyError("SECRET"), OSError("SECRET")])
def test_any_index_failure_after_approval_is_exit_3_without_the_message(
    tmp_path, capsys, monkeypatch, exc
):
    from stiracore.control import staging_cli

    base_dir, store = _two_staged(tmp_path)
    _, listed = _cli(capsys, base_dir, "list")
    key = next(item["key"] for item in listed["entries"] if item["band"] == "staging")

    def _broken(_store):
        raise exc

    monkeypatch.setattr(staging_cli, "write_semantic_index", _broken)
    code = staging_cli.main(["--base-dir", str(base_dir), "approve", key])
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.out)["error"] == "E_INDEX_WRITE"
    assert f"E_INDEX_WRITE ({type(exc).__name__})" in captured.err
    assert "SECRET" not in captured.out + captured.err
    target = key.removeprefix("staging/")
    assert store.read(MemoryLayer.SEMANTIC, target).metadata["review"] == "approved"


def test_failed_original_score_is_staged_even_if_rescoring_raises(tmp_path, caplog):
    store = MemoryStore(tmp_path, "stira")
    record_episode(store, "ok", "fine", key="turn", scorer=_Leaky())

    with caplog.at_level(logging.WARNING):
        result = HarvestScheduler(store, _EchoingScorer(), "stira").run_distillation()

    assert (result.semantic, result.errors) == (1, 0)
    (staged,) = _staged(store)
    assert staged.metadata["harvest_reason"].startswith("E_ORIGINAL_SCORE_FAILED")
    assert staged.metadata["score"] == AmygdalaPass().review_threshold
    episode = store.read(MemoryLayer.EPISODIC, "turn")
    assert episode.metadata["harvested"] is True  # not left in the retry queue
    assert "Harvest episode failed" not in caplog.text
