"""#454 (ADR 0020): workspace artifacts — registration, versions, limits, visibility."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.config.data_dir import build_policy
from stiracore.config.models import AgentConfig
from stiracore.tools.workspace_artifacts import (
    ArtifactHook,
    WorkspaceArtifacts,
    artifact_id,
    artifact_kind,
    eligible_path,
)


@pytest.fixture
def setup(tmp_path):
    data_dir = (tmp_path / "data").resolve()
    (data_dir / "workspace").mkdir(parents=True)
    policy, _floor = build_policy(AgentConfig(), data_dir)
    registry = WorkspaceArtifacts(data_dir / "artifacts")
    yield SimpleNamespace(
        data_dir=data_dir, workspace=data_dir / "workspace", policy=policy, registry=registry
    )
    registry.close()


def _write(setup, name: str, text: str) -> Path:
    path = setup.workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_create_update_unchanged_are_versions_with_turn_ids(setup):
    reg, policy = setup.registry, setup.policy
    _write(setup, "hockey_game.html", "<p>one</p>\n")
    first = reg.register(policy, "hockey_game.html", turn_id="turn-1")
    assert first == {
        "status": "registered",
        "id": artifact_id("hockey_game.html"),
        "version": 1,
        "op": "create",
    }
    _write(setup, "hockey_game.html", "<p>two</p>\n<p>three</p>\n")
    second = reg.register(policy, str(setup.workspace / "hockey_game.html"), turn_id="turn-2")
    assert second["version"] == 2 and second["op"] == "update"
    assert reg.register(policy, "hockey_game.html", turn_id="turn-3")["status"] == "unchanged"

    detail = reg.get(policy, first["id"])
    assert detail["path"] == "hockey_game.html" and detail["kind"] == "html"
    assert [(v["version"], v["turn_id"], v["op"]) for v in detail["versions"]] == [
        (2, "turn-2", "update"),
        (1, "turn-1", "create"),
    ]
    assert detail["versions"][0]["size"] == len(b"<p>two</p>\n<p>three</p>\n")
    assert len(detail["versions"][0]["sha256"]) == 64
    assert reg.content(policy, first["id"], 1)["data"] == b"<p>one</p>\n"

    diff = reg.diff(policy, first["id"], 1, 2)
    assert diff["available"] and diff["added"] == 2 and diff["removed"] == 1
    assert "-<p>one</p>" in diff["lines"] and "+<p>three</p>" in diff["lines"]


def test_list_is_newest_first_and_filters_kind_turn_and_query(setup):
    reg, policy = setup.registry, setup.policy
    clock = iter(range(100, 200))
    reg._clock = lambda: float(next(clock))
    for name, turn in (("a.md", "t1"), ("b.py", "t1"), ("pics/c.png", "t2"), ("d.html", "t2")):
        _write(setup, name, f"{name}\n")
        assert reg.register(policy, name, turn_id=turn)["status"] == "registered"
    assert [a["path"] for a in reg.list(policy)] == ["d.html", "pics/c.png", "b.py", "a.md"]
    assert [a["path"] for a in reg.list(policy, kind="code")] == ["b.py"]
    assert [a["path"] for a in reg.list(policy, turn_id="t1")] == ["b.py", "a.md"]
    assert [a["path"] for a in reg.list(policy, query="PICS")] == ["pics/c.png"]
    assert [a["path"] for a in reg.list(policy, limit=1)] == ["d.html"]
    assert {artifact_kind(n) for n in ("x.htm", "x.markdown", "x.svg", "x.json", "x.csv", "x")} == {
        "html",
        "markdown",
        "image",
        "code",
        "text",
        "other",
    }


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("../outside.txt", "outside_workspace"),
        ("/etc/hosts", "outside_workspace"),
        (".env", "hidden"),
        (".stira-verify/shot.png", "hidden"),
        ("SOUL.md", "floor"),
        ("AKIAIOSFODNN7EXAMPLE.txt", "secret_name"),
        ("", "invalid_path"),
    ],
)
def test_paths_that_are_never_registered(setup, path, reason):
    if path and not path.startswith(("/", "..")):
        _write(setup, path, "x\n")
    assert eligible_path(setup.policy, path) == (None, reason)
    assert setup.registry.register(setup.policy, path)["reason"] == reason
    assert setup.registry.list(setup.policy) == []


def test_the_data_directory_and_its_artifact_store_are_not_registered(setup):
    for target in (setup.data_dir / "artifacts" / "index.db", setup.data_dir / "notes.txt"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        assert setup.registry.register(setup.policy, str(target))["status"] == "skipped"


def test_symlinks_are_neither_registered_nor_shown(setup, tmp_path):
    reg, policy = setup.registry, setup.policy
    outside = tmp_path / "secret.txt"
    outside.write_text("outside\n", encoding="utf-8")
    os.symlink(outside, setup.workspace / "out.txt")
    real = _write(setup, "real.txt", "inside\n")
    os.symlink(real, setup.workspace / "alias.txt")
    os.symlink(tmp_path, setup.workspace / "dirlink")
    for name in ("out.txt", "alias.txt", "dirlink/secret.txt"):
        assert reg.register(policy, name) == {"status": "skipped", "reason": "symlink"}, name
    # Registered while real; replaced by a symlink later: no longer listed or shown.
    ident = reg.register(policy, "real.txt")["id"]
    assert [a["path"] for a in reg.list(policy)] == ["real.txt"]
    real.unlink()
    os.symlink(outside, real)
    assert reg.list(policy) == []
    assert reg.get(policy, ident) is None and reg.content(policy, ident, 1) is None


def test_a_deleted_file_keeps_its_history(setup):
    path = _write(setup, "gone.txt", "bye\n")
    ident = setup.registry.register(setup.policy, "gone.txt")["id"]
    path.unlink()
    assert setup.registry.get(setup.policy, ident)["versions"][0]["version"] == 1
    assert setup.registry.content(setup.policy, ident, 1)["data"] == b"bye\n"


def test_versions_are_capped_and_old_snapshots_removed(setup, tmp_path):
    reg = WorkspaceArtifacts(tmp_path / "capped", max_versions=3)
    try:
        for n in range(5):
            _write(setup, "v.txt", f"version {n}\n")
            reg.register(setup.policy, "v.txt", turn_id=f"t{n}")
        detail = reg.get(setup.policy, artifact_id("v.txt"))
        assert [v["version"] for v in detail["versions"]] == [5, 4, 3]
        assert len([p for p in (tmp_path / "capped" / "blobs").iterdir()]) == 3
        assert reg.content(setup.policy, artifact_id("v.txt"), 1) is None
    finally:
        reg.close()


def test_artifact_count_limit_refuses_new_paths_and_warns(setup, tmp_path):
    reg = WorkspaceArtifacts(tmp_path / "few", max_artifacts=2)
    try:
        for name in ("1.txt", "2.txt", "3.txt"):
            _write(setup, name, name)
        assert reg.register(setup.policy, "1.txt")["status"] == "registered"
        assert reg.register(setup.policy, "2.txt")["status"] == "registered"
        assert reg.register(setup.policy, "3.txt") == {
            "status": "skipped",
            "reason": "limit_artifacts",
        }
        _write(setup, "1.txt", "changed")  # an existing path still gets versions
        assert reg.register(setup.policy, "1.txt")["version"] == 2
        assert reg.status()["warnings"]["limit_artifacts"] == 1 and reg.status()["count"] == 2
    finally:
        reg.close()


def test_big_files_are_hash_only_or_skipped(setup, tmp_path):
    reg = WorkspaceArtifacts(tmp_path / "small", max_snapshot_bytes=16, max_file_bytes=64)
    try:
        _write(setup, "big.txt", "x" * 40)
        first = reg.register(setup.policy, "big.txt")
        _write(setup, "big.txt", "y" * 40)
        reg.register(setup.policy, "big.txt")
        detail = reg.get(setup.policy, first["id"])
        assert [v["stored"] for v in detail["versions"]] == [False, False]
        assert reg.diff(setup.policy, first["id"], 1, 2) == {
            "available": False,
            "reason": "not_stored",
        }
        assert reg.status()["warnings"]["hash_only"] == 2
        _write(setup, "huge.txt", "z" * 65)
        assert reg.register(setup.policy, "huge.txt")["reason"] == "too_large"
    finally:
        reg.close()


def test_store_size_limit_turns_new_versions_hash_only(setup, tmp_path):
    reg = WorkspaceArtifacts(tmp_path / "full", max_store_bytes=10)
    try:
        _write(setup, "a.txt", "12345678\n")
        assert reg.register(setup.policy, "a.txt")["status"] == "registered"
        _write(setup, "a.txt", "abcdefgh\n")
        reg.register(setup.policy, "a.txt")
        versions = reg.get(setup.policy, artifact_id("a.txt"))["versions"]
        assert [v["stored"] for v in versions] == [False, True]
    finally:
        reg.close()


def test_binary_versions_have_no_diff(setup):
    (setup.workspace / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n1")
    ident = setup.registry.register(setup.policy, "p.png")["id"]
    (setup.workspace / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n2")
    setup.registry.register(setup.policy, "p.png")
    assert setup.registry.diff(setup.policy, ident, 1, 2) == {
        "available": False,
        "reason": "binary",
    }


def test_the_index_and_snapshots_hold_no_body_beyond_the_snapshot(setup):
    marker = "BODY-MARKER-7f3a"
    _write(setup, "note.md", f"# {marker}\n")
    setup.registry.register(setup.policy, "note.md", turn_id="t1")
    index = (setup.data_dir / "artifacts" / "index.db").read_bytes()
    assert marker.encode() not in index
    blobs = list((setup.data_dir / "artifacts" / "blobs").iterdir())
    assert len(blobs) == 1 and marker.encode() in blobs[0].read_bytes()


def test_the_store_is_on_the_agents_read_and_write_floor(setup):
    target = str(setup.data_dir / "artifacts" / "index.db")
    assert not setup.policy.check(target).allowed
    assert not setup.policy.check_read(target).allowed


def _event(name, arguments, status="success"):
    return SimpleNamespace(
        tool_use={"name": name, "toolUseId": "u1", "input": arguments},
        result={"toolUseId": "u1", "status": status, "content": [{"text": "ok"}]},
    )


def test_the_hook_registers_successful_mutations_only(setup):
    hook = ArtifactHook(
        setup.registry, policy_provider=lambda: setup.policy, turn_id_provider=lambda: "turn-9"
    )
    _write(setup, "w.txt", "w\n")
    hook.after_tool(_event("file_write", {"path": "w.txt", "content": "w\n"}))
    hook.after_tool(_event("editor", {"command": "view", "path": "w.txt"}))
    hook.after_tool(_event("file_write", {"path": "w.txt", "content": "x"}, status="error"))
    hook.after_tool(_event("file_read", {"path": "w.txt"}))
    detail = setup.registry.get(setup.policy, artifact_id("w.txt"))
    assert [(v["version"], v["turn_id"], v["tool"]) for v in detail["versions"]] == [
        (1, "turn-9", "file_write")
    ]
    _write(setup, "w.txt", "edited\n")
    hook.after_tool(_event("editor", {"command": "str_replace", "path": "w.txt"}))
    assert setup.registry.get(setup.policy, artifact_id("w.txt"))["versions"][0]["tool"] == (
        "editor"
    )


def test_a_failing_registry_never_breaks_the_tool_result(setup, caplog):
    def boom():
        raise RuntimeError("secret /path/value")

    hook = ArtifactHook(setup.registry, policy_provider=boom)
    event = _event("file_write", {"path": "w.txt", "content": "w"})
    before = dict(event.result)
    hook.after_tool(event)
    assert event.result == before
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_labels_take_a_fixed_set_and_keep_a_history(setup):
    reg, policy = setup.registry, setup.policy
    _write(setup, "g.html", "<p>g</p>")
    ident = reg.register(policy, "g.html")["id"]
    for bad in (["gold"], "stable", [1], ["stable"] * 4):
        with pytest.raises(ValueError):
            reg.set_labels(policy, ident, bad, principal="local:me")
    seen = []
    changed = reg.set_labels(
        policy, ident, ["experimental"], principal="local:me", before_write=seen.append
    )
    assert changed["changed"] and seen == [
        {"id": ident, "labels": ["experimental"], "previous": [], "version": 1}
    ]
    reg.set_labels(policy, ident, ["stable", "latest"], principal="local:me")
    history = reg.get(policy, ident)["label_history"]
    assert [(h["labels"], h["previous"], h["by"]) for h in history] == [
        (["stable", "latest"], ["experimental"], "local:me"),
        (["experimental"], [], "local:me"),
    ]
    assert reg.set_labels(policy, "a" + "0" * 20, ["stable"], principal="local:me") is None


def test_a_failing_before_write_changes_nothing(setup):
    _write(setup, "g.html", "<p>g</p>")
    ident = setup.registry.register(setup.policy, "g.html")["id"]

    def refuse(_change):
        raise RuntimeError("audit down")

    with pytest.raises(RuntimeError):
        setup.registry.set_labels(
            setup.policy, ident, ["stable"], principal="local:me", before_write=refuse
        )
    detail = setup.registry.get(setup.policy, ident)
    assert detail["labels"] == [] and detail["label_history"] == []


def test_a_page_check_result_is_kept_as_codes_and_counts(setup):
    import json as _json

    hook = ArtifactHook(
        setup.registry, policy_provider=lambda: setup.policy, turn_id_provider=lambda: "turn-4"
    )
    _write(setup, "hockey.html", "<p>h</p>")
    hook.after_tool(_event("file_write", {"path": "hockey.html", "content": "<p>h</p>"}))
    own = {
        "status": "ok",
        "loaded": True,
        "page_errors": {"count": 1},
        "console_errors": {"count": 2},
        "network": {"blocked": 3, "hosts_total": 1},
        "assertions": {"passed": 2, "failed": 1, "not_run": 0},
        "open_yourself": {"path": str(setup.workspace / "hockey.html"), "url": "file:///x"},
    }
    result = {
        "toolUseId": "u2",
        "status": "success",
        "content": [{"text": _json.dumps(own)}, {"text": "<page-content>SECRET TITLE"}],
    }
    hook.after_tool(
        SimpleNamespace(
            tool_use={"name": "page_check", "toolUseId": "u2", "input": {}}, result=result
        )
    )
    checks = setup.registry.get(setup.policy, artifact_id("hockey.html"))["checks"]
    assert len(checks) == 1 and checks[0]["version"] == 1 and checks[0]["turn_id"] == "turn-4"
    summary = checks[0]["summary"]
    assert summary["status"] == "ok" and summary["page_errors"] == 1
    assert summary["console_errors"] == 2 and summary["network"]["blocked"] == 3
    assert "target" not in summary and "SECRET" not in _json.dumps(checks)
