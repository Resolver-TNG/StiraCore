"""`distillation_cli --dry-run` / `--owner-lock` and one call per memory for the LLM distiller (#317)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from provider_fakes import FakeModel, text_turn

from stiracore.config import AgentConfig
from stiracore.config.data_dir_lock import DataDirOwnerLock
from stiracore.control.distillation_cli import main
from stiracore.control.distillers import LLMDistiller
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

STRUCTURED = (
    "## Design note\n- cache TTL 5 minutes\n- retry 3 times\n"
    "```python\ncache.set(k, v, ttl=300)\n```\nDecision: we go with this."
)


def _tree(root: Path) -> dict[str, tuple]:
    """Every file and directory (empty ones too): kind, mode, mtime and content hash."""
    tree: dict[str, tuple] = {".": ("dir", root.stat().st_mode, root.stat().st_mtime_ns)}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            tree[name] = ("link", os.readlink(path))
        elif path.is_dir():
            tree[name] = ("dir", info.st_mode, info.st_mtime_ns)
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            tree[name] = ("file", info.st_mode, info.st_mtime_ns, info.st_size, digest)
    return tree


def _setup(tmp_path: Path, mode: str = "rule") -> tuple[Path, Path, MemoryStore]:
    data = tmp_path / "data"
    base = data / "memory"
    config = data / "agent-config.json"
    AgentConfig(distiller_mode=mode, path=config).save()
    store = MemoryStore(base, "stira")
    record_episode(store, "thanks", "you are welcome", key="chat-1")
    record_episode(store, STRUCTURED, "Recorded the design note.", key="chat-2")
    return base, config, store


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    lines = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(lines[-1])


def test_rule_dry_run_predicts_the_run_and_writes_nothing(tmp_path, capsys):
    base, config, _store = _setup(tmp_path)
    before = _tree(tmp_path)

    code, plan = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")

    assert code == 0
    assert _tree(tmp_path) == before  # no Markdown, index or archive change
    assert plan["dry_run"] is True and plan["mode"] == "rule"
    assert plan["store"] == {"complete": True, "missing": []}
    assert plan["estimate"] == {
        "model_calls": 0,
        "prompt_chars": 0,
        "max_output_tokens": 0,
        "limit": None,
        "over_limit": False,
    }
    harvest = plan["harvest"]
    assert harvest["episodes"] == 2 and harvest["to_score"] == 2
    routes = harvest["routes_before_duplicate_checks"]
    assert routes["discard"] == 1 and routes["staging"] == 1 and routes["knowledge"] == 0

    code, result = _run(capsys, "--base-dir", str(base), "--config", str(config))
    assert code == 0
    assert result["harvest"]["discard"] == routes["discard"]
    assert result["staging_pending"] == routes["staging"]

    code, again = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")
    assert again["harvest"]["already_harvested"] == 2 and again["harvest"]["to_score"] == 0
    assert again["staging_pending"] == routes["staging"]


def test_llm_dry_run_sizes_the_calls_without_a_model(tmp_path, capsys, monkeypatch):
    base, config, store = _setup(tmp_path, mode="llm")

    def no_model(self):
        raise AssertionError("dry run must not build a model")

    monkeypatch.setattr(LLMDistiller, "_build_model", no_model)
    code, plan = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")

    assert code == 0
    episodes = store.list_entries(MemoryLayer.EPISODIC, limit=10)
    expected = sum(len(LLMDistiller.build_prompt(e.content, dict(e.metadata))) for e in episodes)
    assert plan["estimate"]["model_calls"] == 2
    assert plan["estimate"]["prompt_chars"] == expected
    assert plan["estimate"]["max_output_tokens"] == 2 * LLMDistiller.MAX_TOKENS


def test_jev_dry_run_reports_unavailable(tmp_path, capsys):
    base, config, _store = _setup(tmp_path, mode="jev")

    code, plan = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")

    assert code == 1  # a real run would fail every episode
    assert plan["distiller_available"] is False


def test_dry_run_needs_a_config(tmp_path):
    with pytest.raises(SystemExit):
        main(["--base-dir", str(tmp_path / "memory"), "--dry-run"])


def test_owner_lock_refuses_while_the_directory_is_owned(tmp_path, capsys):
    base, config, _store = _setup(tmp_path)
    data = base.parent
    owner = DataDirOwnerLock(data)
    owner.acquire()
    try:
        before = _tree(base)
        code, payload = _run(
            capsys, "--base-dir", str(base), "--config", str(config), "--owner-lock", str(data)
        )
    finally:
        owner.release()

    assert code == 3
    assert payload["refused"]["code"] == "E_DATA_DIR_OWNED"
    assert _tree(base) == before

    code, result = _run(
        capsys, "--base-dir", str(base), "--config", str(config), "--owner-lock", str(data)
    )
    assert code == 0 and result["staging_pending"] == 1
    probe = DataDirOwnerLock(data)
    probe.acquire()  # released after the run
    probe.release()


@pytest.mark.skipif(os.name == "nt", reason="O_NOFOLLOW is POSIX only")
def test_owner_lock_refuses_a_symlinked_lock_file(tmp_path, capsys):
    base, config, _store = _setup(tmp_path)
    data = base.parent
    target = tmp_path / "elsewhere.lock"
    target.write_text("", encoding="utf-8")
    (data / "server.lock").symlink_to(target)

    code, payload = _run(
        capsys, "--base-dir", str(base), "--config", str(config), "--owner-lock", str(data)
    )

    assert code == 3
    assert payload["refused"]["code"] == "E_LOCK_FILE_UNSAFE"
    assert target.read_text(encoding="utf-8") == ""


def test_owner_lock_must_contain_the_base_dir(tmp_path):
    base, config, _store = _setup(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "--base-dir",
                str(base),
                "--config",
                str(config),
                "--owner-lock",
                str(tmp_path / "elsewhere"),
            ]
        )


def test_dry_run_creates_nothing_for_a_store_that_does_not_exist(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "agent-config.json"  # outside: the data directory stays empty
    AgentConfig(path=config).save()
    before = _tree(data)

    code, plan = _run(
        capsys,
        "--base-dir",
        str(data / "memory"),
        "--config",
        str(config),
        "--owner-lock",
        str(data),
        "--dry-run",
    )

    assert code == 0
    assert _tree(data) == before  # no memory/, no index, no server.lock
    assert plan["store"] == {"complete": False, "missing": ["memory"]}
    assert plan["harvest"]["to_score"] == 0
    assert plan["estimate"]["model_calls"] == 0


def test_dry_run_does_not_rebuild_a_missing_index(tmp_path, capsys):
    base, config, store = _setup(tmp_path, mode="llm")
    store.index_path.unlink()
    before = _tree(tmp_path)

    code, plan = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")
    assert code == 0
    assert _tree(tmp_path) == before
    assert plan["store"] == {"complete": False, "missing": ["index.sqlite3"]}
    assert plan["harvest"]["to_score"] is None
    assert plan["estimate"]["model_calls"] is None  # unknown, not zero

    code, plan = _run(
        capsys,
        "--base-dir",
        str(base),
        "--config",
        str(config),
        "--max-model-calls",
        "5",
        "--dry-run",
    )
    assert code == 4 and plan["estimate"]["over_limit"] is True  # cannot tell = over
    assert _tree(tmp_path) == before


def test_owner_lock_dry_run_only_probes_the_lock(tmp_path, capsys):
    base, config, _store = _setup(tmp_path)
    data = base.parent
    previous = DataDirOwnerLock(data)  # a server ran here once: the file exists
    previous.acquire()
    previous.release()
    before = _tree(tmp_path)

    argv = ("--base-dir", str(base), "--config", str(config), "--owner-lock", str(data))
    code, plan = _run(capsys, *argv, "--dry-run")
    assert code == 0 and plan["dry_run"] is True
    assert _tree(tmp_path) == before  # the PID note is not rewritten

    owner = DataDirOwnerLock(data)
    owner.acquire()
    try:
        code, payload = _run(capsys, *argv, "--dry-run")
    finally:
        owner.release()
    assert code == 3 and payload["refused"]["code"] == "E_DATA_DIR_OWNED"

    after = DataDirOwnerLock(data)
    after.acquire()  # free again: the probe let go of its lock
    after.release()


def test_dry_run_takes_the_same_arguments_as_the_run(tmp_path, capsys):
    base, config, _store = _setup(tmp_path)
    argv = ("--base-dir", str(base), "--config", str(config))

    code, plan = _run(capsys, *argv, "--apply-forgetting", "--dry-run")
    assert code == 0 and plan["forgetting_apply"] is True

    for extra in (["--list-forgotten"], ["--undo-forgetting", "x"]):
        with pytest.raises(SystemExit):
            main([*argv, *extra, "--dry-run"])
    for bad in ("-1", "1/0", "a[$(touch x)0]", "1000000"):
        with pytest.raises(SystemExit):
            main([*argv, "--max-model-calls", bad])


def test_llm_run_stops_at_the_model_call_limit(tmp_path, capsys, monkeypatch):
    base, config, store = _setup(tmp_path, mode="llm")
    model = FakeModel([text_turn('{"score": 0.9, "reason": "r"}') for _ in range(2)])
    monkeypatch.setattr(LLMDistiller, "_build_model", lambda self: model)

    code, result = _run(
        capsys, "--base-dir", str(base), "--config", str(config), "--max-model-calls", "1"
    )

    assert code == 4
    assert result["model_call_limit"] == {"limit": 1, "calls": 1, "refused": 1}
    assert len(model.requests) == 1  # the second episode never reached the model
    marked = [
        entry.metadata.get("harvested")
        for entry in store.list_entries(MemoryLayer.EPISODIC, limit=10)
    ]
    assert sorted(bool(flag) for flag in marked) == [False, True]  # left for the next run

    code, plan = _run(capsys, "--base-dir", str(base), "--config", str(config), "--dry-run")
    assert plan["estimate"]["model_calls"] == 1


def test_llm_distiller_sends_one_memory_per_call():
    model = FakeModel([text_turn('{"score": 0.5, "reason": "r"}') for _ in range(3)])
    distiller = LLMDistiller(model)

    for index in range(3):
        distiller.distill(f"memory {index}", {})

    assert [len(request["messages"]) for request in model.requests] == [1, 1, 1]
    assert "memory 0" not in json.dumps(model.requests[2]["messages"], ensure_ascii=False)
