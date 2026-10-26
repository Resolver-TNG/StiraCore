"""#273: restoring a backup must not silently bring back older (looser) tool rules.

ADR 0010 addendum (#273). Without the opt-in anchor the behaviour is unchanged (a
consistent older copy loads; operations.md §8 requires ``stira tool-rules lock`` after a
restore). With ``STIRA_TOOL_RULES_ANCHOR_DIR`` — a directory *outside* the data
directory — an older generation, a reset data directory or other rules under the same
generation lock every tool Never until a human RECOVER save. Also measures what a
rolled-back operation ledger does on resume (ADR 0006 addendum).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from test_admin_api_permissions import stira_app  # noqa: F401 - fixture
from test_tool_policy import _client, _custom_store, _names, _some

from stiracore.tools import tool_config as tc
from stiracore.tools import tool_rules_cli
from stiracore.tools.tool_config import ToolConfig

FILES = ("t.json", "t.json.head", "settings-history.jsonl")


@pytest.fixture
def anchor(tmp_path_factory, monkeypatch):
    """An anchor directory outside the data directory; ``enable(path)`` sets the variable
    and runs the human initialisation (``stira tool-rules init-anchor``)."""
    directory = tmp_path_factory.mktemp("anchor-outside-data-dir")

    class Anchor:
        dir = directory

        @staticmethod
        def env():
            monkeypatch.setenv(tc.ANCHOR_ENV, str(directory))

        @staticmethod
        def enable(path):
            monkeypatch.setenv(tc.ANCHOR_ENV, str(directory))
            return tool_rules_cli.init_anchor(Path(path))

    return Anchor


def _restart():
    """A new process: no in-memory latch (an on-disk marker still latches)."""
    with tc._LATCH_LOCK:
        tc._LATCHED.clear()


def _backup(tmp_path, names=FILES):
    target = tmp_path / "backup"
    target.mkdir(exist_ok=True)
    for name in names:
        if (tmp_path / name).exists():
            shutil.copy2(tmp_path / name, target / name)
    return target


def _restore(tmp_path, backup):
    for name in (*FILES, "t.json.locked"):
        (tmp_path / name).unlink(missing_ok=True)
    for item in backup.iterdir():
        shutil.copy2(item, tmp_path / item.name)


def _loosened_then_tightened(tmp_path, anchor=None):
    """Gen 2 (backed up, with its settings history): a write tool Always. Gen 3: Never.

    With ``anchor``, it is initialised at generation 1. Returns (store, tool, backup)."""
    write, read = _some("write"), _some("read")
    store = _custom_store(tmp_path, enabled={write, read})  # generation 1
    if anchor is not None:
        assert anchor.enable(store.tool_path) == 1
    client = _client(store)
    assert (
        client.post("/api/config/tools", json={"name": read, "policy": "never"}).status_code == 200
    )
    assert (tmp_path / "settings-history.jsonl").exists()
    backup = _backup(tmp_path)  # generation 2: write tool Always
    assert (
        client.post("/api/config/tools", json={"name": write, "policy": "never"}).status_code == 200
    )
    return store, write, backup


def _all_never(config):
    return all(config.policy_of(name) == "never" for name in _names())


def _recover(store, enabled=()):
    client = _client(store)
    state = client.get("/api/config/hub").get_json()
    body = {
        "changes": {"tools": {"enabled_tools": sorted(enabled), "ask_tools": []}},
        "base_version": state["version"],
        "acknowledged_dangers": ["tool_policy_loosened"],
        "recover": "RECOVER",
    }
    return client.post("/api/config/hub", json=body)


def test_issue_reproduction_without_the_anchor_is_unchanged(tmp_path, monkeypatch):
    """Default behaviour is not changed (documented residual; operations.md §8)."""
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    store, write, backup = _loosened_then_tightened(tmp_path)
    _restore(tmp_path, backup)
    _restart()
    config = store.load_tools()
    assert (config.generation, config.integrity_error, config.policy_of(write)) == (
        2,
        None,
        "always",
    )


def test_issue_reproduction_with_the_anchor_locks(tmp_path, anchor, monkeypatch):
    """The exact steps of #273 (file + head restored together) now lock."""
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    p = tmp_path / "tool-config.json"
    ToolConfig.load(p)  # new install (generation 1), before the anchor is turned on
    anchor.enable(p)
    t = ToolConfig.load(p)
    t.ask_tools.discard("file_write")
    t.save()  # Always (generation 2)
    bk = tmp_path / "bk"
    bk.mkdir()
    for n in ["tool-config.json", "tool-config.json.head"]:
        shutil.copy(tmp_path / n, bk / n)
    t = ToolConfig.load(p)
    t.enabled_tools.discard("file_write")
    t.save()  # Never (generation 3)
    for n in ["tool-config.json", "tool-config.json.head"]:
        shutil.copy(bk / n, tmp_path / n)
    _restart()
    t = ToolConfig.load(p)
    assert (t.generation, t.integrity_error) == (2, "anchor_rollback")
    assert "file_write" not in t.enabled_tools and _all_never(t)


def test_a_full_restore_with_history_still_locks_and_stays_locked(tmp_path, anchor):
    store, _write, backup = _loosened_then_tightened(tmp_path, anchor)
    _restore(tmp_path, backup)  # rules, head and history: consistent with each other
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_rollback" and _all_never(config)
    assert (tmp_path / "t.json.locked").exists()
    _restart()
    (tmp_path / "t.json.locked").unlink()  # deleting the marker alone does not unlock
    assert store.load_tools().integrity_error == "anchor_rollback"
    rows = _client(store).get("/api/config/hub").get_json()
    assert rows["tools_locked"] == "anchor_rollback"
    assert all(row["effective"]["decision"] == "forbidden" for row in rows["tools"])


def test_only_the_typed_recovery_unlocks_and_numbers_above_the_anchor(tmp_path, anchor):
    store, _write, backup = _loosened_then_tightened(tmp_path, anchor)
    _restore(tmp_path, backup)
    _restart()
    assert store.load_tools().integrity_error == "anchor_rollback"
    client = _client(store)
    assert client.post("/api/tools/config", json={}).status_code == 409
    assert client.post("/api/tools/config", json={"recover": "recover"}).status_code == 409
    read = _some("read")
    state = client.get("/api/config/hub").get_json()
    body = {
        "changes": {"tools": {"enabled_tools": [read], "ask_tools": []}},
        "base_version": state["version"],
        "acknowledged_dangers": ["tool_policy_loosened"],
    }
    assert client.post("/api/config/hub", json=body).status_code == 409
    saved = client.post("/api/config/hub", json={**body, "recover": "RECOVER"})
    assert saved.status_code == 200, saved.get_json()
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "always"
    assert config.generation == 4  # above the anchor's 3, not head (2) + 1
    assert tc.read_anchor(store.tool_path)["generation"] == 4


def test_a_reset_data_directory_is_not_a_new_install(tmp_path, anchor):
    store, _write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)
    for name in FILES:
        (tmp_path / name).unlink(missing_ok=True)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_rollback" and _all_never(config)
    assert not (tmp_path / "t.json").exists()  # no first-run defaults were written


@pytest.mark.parametrize("same_identity", [False, True])
def test_same_generation_with_other_rules_locks(tmp_path, anchor, monkeypatch, same_identity):
    write = _some("write")
    store = _custom_store(tmp_path, enabled=set())  # generation 1: Never
    other = tmp_path / "other"
    other.mkdir()
    ToolConfig(mode="custom", enabled_tools={write}, path=other / "t.json").save()  # gen 1
    anchor.enable(store.tool_path)
    identity = tc.read_anchor(store.tool_path)["install_id"]
    for name in ("t.json", "t.json.head"):
        shutil.copy2(other / name, tmp_path / name)  # another install's copy, same number
    if same_identity:  # tampering: the head made to carry this install's identity
        head = json.loads((tmp_path / "t.json.head").read_text(encoding="utf-8"))
        head["install_id"] = identity
        (tmp_path / "t.json.head").write_text(json.dumps(head), encoding="utf-8")
    _restart()
    expected = "anchor_mismatch" if same_identity else "anchor_foreign_install"
    assert store.load_tools().integrity_error == expected


# --- review round 1: a missing anchor, symlinks, write failures, misplacement -------


def test_deleting_the_anchor_and_restoring_an_older_copy_locks(tmp_path, anchor):
    """The anchor is never re-created by a load: deleting it does not bypass the check."""
    store, _write, backup = _loosened_then_tightened(tmp_path, anchor)
    tc.anchor_path(store.tool_path).unlink()
    _restore(tmp_path, backup)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_missing" and _all_never(config)
    assert not tc.anchor_path(store.tool_path).exists()  # still not re-created
    with pytest.raises(ValueError, match="locked"):
        tool_rules_cli.init_anchor(store.tool_path)  # init cannot unlock either
    assert _recover(store, {_some("read")}).status_code == 200  # only RECOVER restores it
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None and config.generation == 3
    assert tc.read_anchor(store.tool_path)["generation"] == 3


def test_turning_the_variable_on_without_initialising_locks(tmp_path, anchor):
    """First use of the variable on an existing install: locked until the human step."""
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.env()
    _restart()
    assert store.load_tools().integrity_error == "anchor_missing"
    assert not tc.anchor_path(store.tool_path).exists()


def test_a_new_install_with_the_variable_set_locks_until_recover(tmp_path, anchor):
    anchor.env()
    path = tmp_path / "tool-config.json"
    config = ToolConfig.load(path)
    assert config.integrity_error == "anchor_missing" and not path.exists()
    with pytest.raises(ValueError):
        tool_rules_cli.init_anchor(path)  # nothing to anchor


def test_after_initialising_the_anchor_loads_are_normal(tmp_path, anchor):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    assert anchor.enable(store.tool_path) == 1
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "always"
    client = _client(store)
    assert (
        client.post("/api/config/tools", json={"name": read, "policy": "never"}).status_code == 200
    )
    assert tc.read_anchor(store.tool_path)["generation"] == 2
    with pytest.raises(ValueError, match="already exists"):
        tool_rules_cli.init_anchor(store.tool_path)  # never replaces an anchor


def test_a_direct_save_does_not_create_a_missing_anchor(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.env()
    _restart()
    with pytest.raises(tc.ToolRulesLocked):
        ToolConfig(mode="custom", enabled_tools=set(), path=store.tool_path).save()
    assert not tc.anchor_path(store.tool_path).exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW (Windows)")
def test_an_anchor_replaced_by_a_symlink_locks(tmp_path, anchor):
    store, _write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)
    target = tc.anchor_path(store.tool_path)
    decoy = anchor.dir / "decoy.json"
    decoy.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")  # valid content
    target.unlink()
    target.symlink_to(decoy)
    _restart()
    assert store.load_tools().integrity_error == "anchor_unsafe"
    with pytest.raises(tc.AnchorUnavailable):
        tc.read_anchor(store.tool_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard links")
def test_an_anchor_with_a_second_hard_link_locks(tmp_path, anchor):
    store, _write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)
    os.link(tc.anchor_path(store.tool_path), anchor.dir / "second-link.json")
    _restart()
    assert store.load_tools().integrity_error == "anchor_unsafe"


def test_an_unreadable_anchor_locks(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    tc.anchor_path(store.tool_path).write_text("{broken", encoding="utf-8")
    _restart()
    assert store.load_tools().integrity_error == "anchor_unreadable"


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permission bits, non-root"
)
def test_recovery_with_an_unwritable_anchor_fails_and_stays_locked(tmp_path, anchor):
    """A recovery save is not a success until the anchor holds it: error, still locked."""
    store, _write, backup = _loosened_then_tightened(tmp_path, anchor)
    _restore(tmp_path, backup)
    _restart()
    assert store.load_tools().integrity_error == "anchor_rollback"
    before = (tmp_path / "t.json").read_bytes()
    anchor.dir.chmod(0o500)
    try:
        response = _recover(store, {_some("read")})
        assert response.status_code == 500
        assert response.get_json()["error_code"] == "persist_failed"
        assert (tmp_path / "t.json").read_bytes() == before  # nothing was replaced
        assert (tmp_path / "t.json.locked").exists()
        _restart()
        assert store.load_tools().integrity_error == "anchor_rollback"
    finally:
        anchor.dir.chmod(0o700)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permission bits, non-root"
)
def test_an_anchor_write_failing_after_the_rules_are_replaced_keeps_the_lock(
    tmp_path, anchor, monkeypatch
):
    store, _write, backup = _loosened_then_tightened(tmp_path, anchor)
    _restore(tmp_path, backup)
    _restart()
    assert store.load_tools().integrity_error == "anchor_rollback"

    def failing(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tc, "_write_anchor", failing)  # probe passes, the write fails
    response = _recover(store, {_some("read")})
    assert response.status_code == 500
    assert (tmp_path / "t.json.locked").exists()
    _restart()
    assert store.load_tools().integrity_error is not None  # still locked after restart


@pytest.mark.parametrize("where", ["rules_dir", "registered_root", "symlink_into"])
def test_an_anchor_inside_the_data_directory_is_a_configuration_error(tmp_path, monkeypatch, where):
    store = _custom_store(tmp_path, enabled={_some("read")})
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    if where == "rules_dir":
        directory = tmp_path / "anchor"
    elif where == "registered_root":
        monkeypatch.setattr(tc, "_DATA_ROOTS", set())
        tc.register_data_root(data_root)
        directory = data_root / "sub" / "anchor"
    else:
        link = tmp_path.parent / f"{tmp_path.name}-anchor-link"
        link.symlink_to(tmp_path, target_is_directory=True)
        directory = link / "anchor"
    directory.mkdir(parents=True)
    monkeypatch.setenv(tc.ANCHOR_ENV, str(directory))
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_misconfigured" and _all_never(config)
    with pytest.raises(ValueError, match="inside the data directory"):
        tool_rules_cli.init_anchor(store.tool_path)
    response = _recover(store, {_some("read")})  # a RECOVER cannot place it there either
    assert response.status_code == 500 and (tmp_path / "t.json.locked").exists()


def test_the_cli_refuses_an_anchor_inside_a_given_data_dir(tmp_path, anchor, capsys):
    from stiracore.diagnostics.__main__ import main

    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.env()
    args = ["tool-rules", "init-anchor", "--file", str(store.tool_path)]
    assert main([*args, "--data-dir", str(anchor.dir.parent)]) == 1
    assert "inside the data directory" in capsys.readouterr().err
    assert main(args) == 0 and tc.read_anchor(store.tool_path)["generation"] == 1


@pytest.mark.usefixtures("stira_app")
def test_the_stira_server_registers_its_data_directory(tmp_path):
    assert os.path.realpath(tmp_path / "data") in tc._DATA_ROOTS


# --- the restore command: stira tool-rules lock / status / --file checks ------------


def test_the_lock_command_locks_a_restored_install_until_recover(tmp_path, monkeypatch, capsys):
    """The default path (no anchor): the runbook step that re-asks a human."""
    from stiracore.diagnostics.__main__ import main

    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    store, write, backup = _loosened_then_tightened(tmp_path)
    _restore(tmp_path, backup)
    _restart()
    assert main(["tool-rules", "status", "--file", str(store.tool_path)]) == 0
    assert "not locked" in capsys.readouterr().out
    assert main(["tool-rules", "lock", "--file", str(store.tool_path)]) == 0
    assert "RECOVER" in capsys.readouterr().out
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "restored_backup" and _all_never(config)
    assert main(["tool-rules", "status", "--file", str(store.tool_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["locked"] and report["lock_reason"] == "restored_backup"
    assert report["generation"] == report["head_generation"] == 2
    # Idempotent: an existing lock keeps its reason.
    assert tool_rules_cli.lock(store.tool_path) == (False, "restored_backup")
    client = _client(store)
    assert client.post("/api/tools/config", json={}).status_code == 409
    state = client.get("/api/config/hub").get_json()
    body = {
        "changes": {"tools": {"enabled_tools": [write], "ask_tools": [write]}},
        "base_version": state["version"],
        "acknowledged_dangers": ["tool_policy_loosened"],
        "recover": "RECOVER",
    }
    assert client.post("/api/config/hub", json=body).status_code == 200
    assert not (tmp_path / "t.json.locked").exists()
    assert store.load_tools().policy_of(write) == "ask"


def test_the_lock_command_also_locks_a_running_server_at_its_next_read(tmp_path, monkeypatch):
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    store = _custom_store(tmp_path, enabled={_some("read")})
    assert store.load_tools().integrity_error is None  # the server's per-call read
    assert tool_rules_cli.lock(store.tool_path) == (True, "restored_backup")
    assert store.load_tools().integrity_error == "restored_backup"  # same process


def test_status_changes_nothing(tmp_path, anchor):
    anchor.env()
    path = tmp_path / "t.json"
    report = tool_rules_cli.status(path)
    assert report["locked"] is True and report["lock_reason"] == "anchor_missing"
    assert report["anchor"] == {"generation": None, "inside_data_dir": False}
    assert list(tmp_path.iterdir()) == [] and list(anchor.dir.iterdir()) == []


def test_lock_command_refuses_a_missing_directory(tmp_path):
    from stiracore.diagnostics.__main__ import main

    assert main(["tool-rules", "lock", "--file", str(tmp_path / "nope" / "t.json")]) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("bad", ["symlink_file", "symlink_marker", "directory", "parent_file"])
def test_the_file_argument_is_checked(tmp_path, monkeypatch, capsys, bad):
    from stiracore.diagnostics.__main__ import main

    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    store = _custom_store(tmp_path, enabled={_some("read")})
    target = Path(store.tool_path)
    if bad == "symlink_file":
        link = tmp_path / "link.json"
        link.symlink_to(target)
        target = link
    elif bad == "symlink_marker":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_text("{}", encoding="utf-8")
        tc.latch_path(target).symlink_to(elsewhere)
    elif bad == "directory":
        target = tmp_path / "dir.json"
        target.mkdir()
    else:
        target = target / "child.json"  # parent is a file
    for command in ("lock", "status", "init-anchor"):
        assert main(["tool-rules", command, "--file", str(target)]) == 2, command
    assert "stira tool-rules:" in capsys.readouterr().err
    if bad == "symlink_marker":
        assert (tmp_path / "elsewhere").read_text(encoding="utf-8") == "{}"


def test_the_commands_are_not_agent_tools_and_their_files_are_on_the_floor(tmp_path, anchor):
    """Host commands only: no registered tool runs them, and every file they write is on
    the agent's write floor; the exec class that could run them is hidden by default."""
    from stiracore.tools import tool_registry

    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    names = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    assert not [n for n in names if "tool_rules" in n or "tool-rules" in n]
    assert not tool_registry.exec_tools_enabled()
    floor = store.protected_files()
    for path in (
        store.tool_path,
        tc.head_path(store.tool_path),
        tc.latch_path(store.tool_path),
        tc.anchor_path(store.tool_path),
    ):
        assert str(Path(path).absolute()) in floor


def test_anchor_files_are_outside_the_rules_directory_by_default(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    target = tc.anchor_path(store.tool_path)
    assert target.parent == Path(anchor.dir).absolute() and target.exists()
    assert not tc.anchor_inside_data_dir(store.tool_path)
    assert str(tmp_path) not in target.read_text(encoding="utf-8")  # no path stored


# --- A newer generation than the anchor (#273, the remaining gap) --------------------
# A backup from another install (or another path) can carry a file + head *newer* than
# this anchor. Only one step above it, and loosening nothing compared with the rules the
# anchor recorded, is a save whose anchor update failed; anything else locks.


def _foreign_rules(tmp_path, monkeypatch, saves, enabled):
    """file + head of another install: ``saves`` generations, the last one ``enabled``
    (custom mode, no Ask). Written without the anchor (it is another install)."""
    value = os.environ.get(tc.ANCHOR_ENV)
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    other = tmp_path / "other-install"
    other.mkdir()
    for _ in range(saves):
        ToolConfig(mode="custom", enabled_tools=set(enabled), path=other / "t.json").save()
    monkeypatch.setenv(tc.ANCHOR_ENV, value)
    return other


def _put_foreign(tmp_path, other):
    for name in ("t.json", "t.json.head"):
        shutil.copy2(other / name, tmp_path / name)


def test_a_foreign_backup_with_a_higher_generation_locks(tmp_path, anchor, monkeypatch):
    store, write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)  # anchor: 3
    other = _foreign_rules(tmp_path, monkeypatch, 7, {write})  # gen 7, write tool Always
    _put_foreign(tmp_path, other)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_foreign_install" and _all_never(config)
    assert tc.read_anchor(store.tool_path)["generation"] == 3  # the anchor did not move
    _restart()
    assert store.load_tools().integrity_error == "anchor_foreign_install"  # latched


def test_a_foreign_backup_one_generation_ahead_and_looser_locks(tmp_path, anchor, monkeypatch):
    store, write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)  # anchor: 3
    other = _foreign_rules(tmp_path, monkeypatch, 4, {write, _some("read")})  # gen 4
    _put_foreign(tmp_path, other)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_foreign_install" and _all_never(config)
    assert tc.read_anchor(store.tool_path)["generation"] == 3
    identity = tc.read_anchor(store.tool_path)["install_id"]
    response = _recover(store, {_some("read")})  # the existing recovery still unlocks
    assert response.status_code == 200, response.get_json()
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None and config.generation == 5
    assert tc.read_anchor(store.tool_path)["generation"] == 5
    assert tc._read_head(store.tool_path)["install_id"] == identity  # never the foreign one


def test_more_than_one_generation_ahead_of_the_anchor_locks(tmp_path, anchor, monkeypatch):
    """Two saves whose anchor update failed: not a single interrupted save, locked."""
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    config = store.load_tools()
    config.ask_tools.add(read)

    def failing(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tc, "_write_anchor", failing)
    for _ in range(2):  # the same process saves twice, with no load in between
        with pytest.raises(tc.AnchorWriteError):
            config.save()
    monkeypatch.undo()
    anchor.env()
    _restart()
    config = store.load_tools()
    assert (config.generation, config.integrity_error) == (3, "anchor_generation_jump")
    assert tc.read_anchor(store.tool_path)["generation"] == 1


def _save_with_failing_anchor(store, monkeypatch, change):
    config = store.load_tools()
    change(config)
    original = tc._write_anchor

    def failing(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tc, "_write_anchor", failing)
    with pytest.raises(tc.AnchorWriteError):
        config.save()  # rules and head replaced, the anchor not: reported as failed
    monkeypatch.setattr(tc, "_write_anchor", original)


def test_a_tightening_save_whose_anchor_update_failed_is_completed(tmp_path, anchor, monkeypatch):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None and config.policy_of(read) == "ask"
    assert tc.read_anchor(store.tool_path)["generation"] == 2  # moved up on the load


def test_a_loosening_save_whose_anchor_update_failed_locks(tmp_path, anchor, monkeypatch):
    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.enabled_tools.add(write))
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_unconfirmed_generation" and _all_never(config)
    assert tc.read_anchor(store.tool_path)["generation"] == 1


def test_an_anchor_without_a_rule_snapshot_does_not_confirm_a_newer_generation(
    tmp_path, anchor, monkeypatch
):
    """An anchor written before the snapshot existed: a newer generation cannot be
    compared, so it locks (fail closed) instead of moving the anchor up."""
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    target = tc.anchor_path(store.tool_path)
    old = json.loads(target.read_text(encoding="utf-8"))
    old.pop("rules", None)
    target.write_text(json.dumps(old), encoding="utf-8")
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))
    _restart()
    assert store.load_tools().integrity_error == "anchor_unconfirmed_generation"


def test_the_anchor_snapshot_has_no_secret_looking_commands(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    secret = "echo AKIAIOSFODNN7EXAMPLE"
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert tc.secret_commands([secret]) == [secret]
    data["shell_allowlist"] = [secret]
    (tmp_path / "t.json").write_text(json.dumps(data), encoding="utf-8")
    config = ToolConfig(
        mode="custom", enabled_tools={_some("read")}, shell_allowlist=[secret], path=store.tool_path
    )
    config.generation = 1
    config._write_head()  # as if saved before secrets were refused
    _restart()
    anchor.enable(store.tool_path)
    text = tc.anchor_path(store.tool_path).read_text(encoding="utf-8")
    assert "AKIA" not in text
    assert "rules" not in json.loads(text)  # a snapshot missing a command never confirms


# --- Install identity (#410 review): a foreign file one step ahead, same rules --------


def _same_rules_elsewhere(tmp_path, monkeypatch, rules, saves, *, own_anchor=False):
    """file + head of another install holding ``rules`` at generation ``saves``.
    ``own_anchor``: that install has an anchor of its own (so its head carries its ID)."""
    value = os.environ.get(tc.ANCHOR_ENV)
    monkeypatch.delenv(tc.ANCHOR_ENV, raising=False)
    other = tmp_path / "other-install"
    other.mkdir()
    target = other / "t.json"
    for _ in range(saves - 1 if own_anchor else saves):
        ToolConfig(**rules, path=target).save()
    if own_anchor:
        monkeypatch.setenv(tc.ANCHOR_ENV, str(tmp_path / "other-anchor"))
        tool_rules_cli.init_anchor(target)
        _restart()
        ToolConfig.load(target).save()
    monkeypatch.setenv(tc.ANCHOR_ENV, value)
    return other


@pytest.mark.parametrize("own_anchor", [False, True])
def test_a_foreign_file_one_step_ahead_with_the_same_rules_locks(
    tmp_path, anchor, monkeypatch, own_anchor
):
    store, _write, _backup_dir = _loosened_then_tightened(tmp_path, anchor)  # anchor: 3
    rules = store.load_tools().as_dict()
    other = _same_rules_elsewhere(tmp_path, monkeypatch, rules, 4, own_anchor=own_anchor)
    assert json.loads((other / "t.json").read_text(encoding="utf-8"))["generation"] == 4
    _put_foreign(tmp_path, other)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_foreign_install" and _all_never(config)
    assert tc.read_anchor(store.tool_path)["generation"] == 3


def test_init_anchor_binds_the_head_to_the_anchor_identity(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    identity = tc.read_anchor(store.tool_path)["install_id"]
    assert tc.valid_install_id(identity)
    assert tc._read_head(store.tool_path)["install_id"] == identity
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None
    config.save()
    assert tc._read_head(store.tool_path)["install_id"] == identity
    assert tc.read_anchor(store.tool_path)["install_id"] == identity


def test_an_old_anchor_and_head_without_an_identity_still_load_and_get_one(tmp_path, anchor):
    """Upgrade: an anchor written before the identity existed, same generation: loads; the
    next save gives anchor and head one ID. One step ahead of it: locked (fail closed)."""
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    for target in (tc.anchor_path(store.tool_path), tc.head_path(store.tool_path)):
        data = json.loads(target.read_text(encoding="utf-8"))
        data.pop("install_id")
        target.write_text(json.dumps(data), encoding="utf-8")
    _restart()
    config = store.load_tools()
    assert config.integrity_error is None
    config.save()
    identity = tc.read_anchor(store.tool_path)["install_id"]
    assert tc.valid_install_id(identity)
    assert tc._read_head(store.tool_path)["install_id"] == identity


def test_an_anchor_with_an_invalid_identity_is_unreadable(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    target = tc.anchor_path(store.tool_path)
    data = json.loads(target.read_text(encoding="utf-8"))
    data["install_id"] = "not an id"
    target.write_text(json.dumps(data), encoding="utf-8")
    _restart()
    assert store.load_tools().integrity_error == "anchor_unreadable"


# --- #410 review: the remaining paths, measured -------------------------------------


@pytest.mark.parametrize("route", ["legacy", "hub"])
def test_an_api_loosening_save_whose_anchor_write_fails_is_500_and_locks(
    tmp_path, anchor, monkeypatch, route
):
    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _restart()
    client = _client(store)
    if route == "hub":
        state = client.get("/api/config/hub").get_json()
        url, body = (
            "/api/config/hub",
            {
                "changes": {"tools": {"enabled_tools": sorted({read, write}), "ask_tools": []}},
                "base_version": state["version"],
                "acknowledged_dangers": ["tool_policy_loosened"],
                "confirm_always": [write],
            },
        )
    else:
        url = "/api/config/tools"
        body = {"name": write, "enabled": True, "confirm": True, "confirm_always": [write]}
    original = tc._write_anchor

    def failing(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tc, "_write_anchor", failing)
    response = client.post(url, json=body)
    assert response.status_code == 500, response.get_json()
    if route == "hub":
        assert response.get_json()["error_code"] == "persist_failed"
    monkeypatch.setattr(tc, "_write_anchor", original)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_unconfirmed_generation" and _all_never(config)


def test_an_api_loosening_save_whose_audit_fails_changes_nothing(tmp_path, anchor, monkeypatch):
    from stiracore.observability import settings_hub

    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    rules, head = (tmp_path / "t.json").read_bytes(), tc.head_path(store.tool_path).read_bytes()
    before = tc.anchor_path(store.tool_path).read_bytes()

    def failing(*_args, **_kwargs):
        raise OSError("history disk full")

    monkeypatch.setattr(settings_hub, "append_history", failing)
    body = {"name": write, "enabled": True, "confirm": True, "confirm_always": [write]}
    response = _client(store).post("/api/config/tools", json=body)
    assert response.status_code == 500, response.get_json()
    assert response.get_json()["error_code"] == "audit_failed"
    assert (tmp_path / "t.json").read_bytes() == rules
    assert tc.head_path(store.tool_path).read_bytes() == head
    assert tc.anchor_path(store.tool_path).read_bytes() == before
    _restart()
    assert store.load_tools().policy_of(write) == "never"


def test_a_snapshot_over_the_size_limit_is_left_out_and_one_step_ahead_locks(
    tmp_path, anchor, monkeypatch
):
    read = _some("read")
    store = _store_with_allowlist(tmp_path, read, [f"echo a{i:05d}" for i in range(6000)])
    anchor.enable(store.tool_path)
    target = tc.anchor_path(store.tool_path)
    assert len(target.read_bytes()) <= tc._ANCHOR_LIMIT
    assert "rules" not in json.loads(target.read_text(encoding="utf-8"))
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))  # tighter
    _restart()
    assert store.load_tools().integrity_error == "anchor_unconfirmed_generation"


def _store_with_allowlist(tmp_path, read, commands):
    from stiracore.observability.config_api import ConfigStore

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(
        mode="custom", enabled_tools={read}, shell_allowlist=commands, path=store.tool_path
    ).save()
    return store


def test_an_unconfirmed_generation_stays_locked_without_recover(tmp_path, anchor, monkeypatch):
    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.enabled_tools.add(write))
    _restart()
    assert store.load_tools().integrity_error == "anchor_unconfirmed_generation"
    with pytest.raises(tc.ToolRulesLocked):
        ToolConfig(mode="custom", enabled_tools={read}, path=store.tool_path).save()
    client = _client(store)
    tighter = client.post("/api/config/tools", json={"name": read, "enabled": False})
    assert tighter.status_code == 409  # even a tightening save is not a recovery
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_unconfirmed_generation" and _all_never(config)
    _restart()
    (tmp_path / "t.json.locked").unlink()  # the marker alone: the anchor still disagrees
    assert store.load_tools().integrity_error == "anchor_unconfirmed_generation"
    assert _recover(store, {read}).status_code == 200
    _restart()
    assert store.load_tools().integrity_error is None


def test_a_load_and_a_save_racing_never_move_the_anchor_back(tmp_path, anchor, monkeypatch):
    """A load completing an interrupted save (anchor 1 -> 2) while another thread saves
    generation 3: the anchor must end at 3, never back at 2 (#410 review)."""
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))  # file 2
    _restart()
    original = tc._write_anchor
    at_write, results = threading.Event(), {}

    def slow(path, generation, *args, **kwargs):
        if threading.current_thread().name == "loader":
            at_write.set()
            time.sleep(0.3)  # the saver runs now unless the load holds the rules lock
        return original(path, generation, *args, **kwargs)

    monkeypatch.setattr(tc, "_write_anchor", slow)
    newer = ToolConfig(mode="custom", enabled_tools={read}, ask_tools={read}, path=store.tool_path)

    def load():
        results["load"] = ToolConfig.load(store.tool_path).integrity_error

    def save():
        at_write.wait(5)
        newer.save()
        results["saved"] = newer.generation

    threads = [threading.Thread(target=load, name="loader"), threading.Thread(target=save)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == {"load": None, "saved": 3}
    assert tc.read_anchor(store.tool_path)["generation"] == 3
    _restart()
    assert store.load_tools().integrity_error is None


def test_the_anchor_never_takes_a_lower_generation(tmp_path, anchor):
    """Another process (no shared in-process lock) writing an older generation: refused."""
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    ToolConfig.load(store.tool_path).save()  # anchor 2
    with pytest.raises(OSError):
        tc._write_anchor(store.tool_path, 1, "0" * 64)
    assert tc.read_anchor(store.tool_path)["generation"] == 2


# --- Round 2: anchor updates are exclusive across processes ------------------------
# Process A completes an interrupted save (anchor 1 -> 2) and stops right before it
# replaces the anchor; process B meanwhile completes the same generation and saves a
# tighter generation 3. Without a lock shared by processes, A then writes 2 over 3 and
# restoring the generation-2 files later loads them unlocked.

_RACER = r"""
import json, os, sys, time
from pathlib import Path
from stiracore import owner_lock
from stiracore.tools import tool_config as tc
from stiracore.tools.tool_config import ToolConfig

role, rules_path, sync = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
anchor = tc.anchor_path(rules_path)
if role == "A":
    original = tc._atomic_write

    def paused(target, data, precommit=None):
        if Path(target) == anchor and data.get("generation") == 2:
            (sync / "a-at-write").write_text("1")
            # Wait for B's reached point, not a time: B waiting on the anchor lock (the
            # fix) or B finished (no lock shared by processes). Either way exactly once.
            deadline = time.monotonic() + 30
            while not ((sync / "b-waiting").exists() or (sync / "b-done").exists()):
                if time.monotonic() > deadline:
                    raise SystemExit("B never reached the anchor lock")
                time.sleep(0.01)
        return original(target, data, precommit)

    tc._atomic_write = paused
    result = ToolConfig.load(rules_path).integrity_error
else:
    original_try = owner_lock._try_lock

    def reporting(fd):
        try:
            original_try(fd)
        except OSError:
            (sync / "b-waiting").write_text("1")  # contended: A holds the anchor lock
            raise

    owner_lock._try_lock = reporting
    loaded = ToolConfig.load(rules_path)
    tighter = ToolConfig(mode="custom", enabled_tools=set(), path=rules_path)
    try:
        tighter.save()
        result = [loaded.integrity_error, tighter.generation]
    except OSError as error:
        result = [loaded.integrity_error, type(error).__name__]
    (sync / "b-done").write_text("1")
(sync / f"{role}.json").write_text(json.dumps(result))
"""


def _racer(tmp_path, role, sync):
    import subprocess

    script = tmp_path / "racer.py"
    script.write_text(_RACER, encoding="utf-8")
    source = str(Path(tc.__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([source, os.environ.get("PYTHONPATH", "")])}
    return subprocess.Popen(
        [sys.executable, str(script), role, str(tmp_path / "t.json"), str(sync)], env=env
    )


@pytest.mark.skipif(sys.platform == "win32", reason="flock: POSIX (Windows unverified)")
def test_two_processes_racing_never_move_the_anchor_back(tmp_path, anchor, monkeypatch):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))  # file 2
    sync = tmp_path / "sync"
    sync.mkdir()
    generation_2 = _backup(tmp_path, ("t.json", "t.json.head"))
    first = _racer(tmp_path, "A", sync)
    deadline = time.monotonic() + 30
    while not (sync / "a-at-write").exists() and time.monotonic() < deadline:
        assert first.poll() is None, "process A ended before it reached the anchor write"
        time.sleep(0.02)
    second = _racer(tmp_path, "B", sync)
    assert first.wait(30) == 0 and second.wait(30) == 0
    a_result = json.loads((sync / "A.json").read_text(encoding="utf-8"))
    b_result = json.loads((sync / "B.json").read_text(encoding="utf-8"))
    assert a_result is None and b_result == [None, 3]
    assert tc.read_anchor(store.tool_path)["generation"] == 3  # never back at 2
    _restart()
    assert store.load_tools().integrity_error is None
    _restore(tmp_path, generation_2)  # restoring generation 2 still locks
    _restart()
    assert store.load_tools().integrity_error == "anchor_rollback"


@pytest.mark.skipif(sys.platform == "win32", reason="flock: POSIX (Windows unverified)")
def test_a_held_anchor_lock_fails_closed(tmp_path, anchor, monkeypatch):
    """Lock not obtained in time: the load locks, a save fails; nothing loosens."""
    import fcntl

    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))  # file 2
    monkeypatch.setattr(tc, "_ANCHOR_LOCK_TIMEOUT", 0.2)
    holder = os.open(tc.anchor_lock_path(store.tool_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)  # another process holding it (or hanging)
        _restart()
        config = ToolConfig.load(store.tool_path)
        assert config.integrity_error == "anchor_unwritable" and _all_never(config)
        _restart()
        before = (tmp_path / "t.json").read_bytes(), tc.head_path(store.tool_path).read_bytes()
        looser = ToolConfig(mode="custom", enabled_tools={read, write}, path=store.tool_path)
        with pytest.raises(tc.AnchorWriteError):
            looser.save(recover=True)  # even a recovery: refused before anything is replaced
        after = (tmp_path / "t.json").read_bytes(), tc.head_path(store.tool_path).read_bytes()
        assert after == before and tc.read_anchor(store.tool_path)["generation"] == 1
        assert (tmp_path / "t.json.locked").exists()  # the lock stays
    finally:
        os.close(holder)
    _restart()
    config = store.load_tools()
    assert config.integrity_error == "anchor_unwritable" and config.policy_of(write) == "never"


def _breaking_anchor_under_the_lock(monkeypatch, path, how):
    """Right after the anchor lock is taken, the anchor is deleted or broken (another
    process, a person): what the code reads again under the lock is that state."""
    original = tc._anchor_file_lock

    @contextlib.contextmanager
    def breaking(target):
        with original(target):
            anchor = tc.anchor_path(path)
            if how == "missing":
                anchor.unlink(missing_ok=True)
            else:
                anchor.write_text("{broken", encoding="utf-8")
            yield

    monkeypatch.setattr(tc, "_anchor_file_lock", breaking)


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_load_never_re_creates_an_anchor_that_vanished_under_the_lock(
    tmp_path, anchor, monkeypatch, how
):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _save_with_failing_anchor(store, monkeypatch, lambda c: c.ask_tools.add(read))  # file 2
    _restart()
    _breaking_anchor_under_the_lock(monkeypatch, store.tool_path, how)
    config = ToolConfig.load(store.tool_path)
    assert config.integrity_error == f"anchor_{how}" and _all_never(config)
    target = tc.anchor_path(store.tool_path)
    if how == "missing":
        assert not target.exists()  # not written again by a load
    else:
        assert target.read_text(encoding="utf-8") == "{broken"
    monkeypatch.setattr(tc, "_anchor_file_lock", tc._anchor_file_lock.__wrapped__)
    _restart()
    assert store.load_tools().integrity_error == f"anchor_{how}"  # latched until RECOVER


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_normal_save_never_re_creates_an_anchor_that_vanished_under_the_lock(
    tmp_path, anchor, monkeypatch, how
):
    read, write = _some("read"), _some("write")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    _restart()
    config = store.load_tools()
    config.enabled_tools.add(write)
    before = (tmp_path / "t.json").read_bytes(), tc.head_path(store.tool_path).read_bytes()
    _breaking_anchor_under_the_lock(monkeypatch, store.tool_path, how)
    with pytest.raises(tc.ToolRulesLocked):
        config.save()
    after = (tmp_path / "t.json").read_bytes(), tc.head_path(store.tool_path).read_bytes()
    assert after == before  # nothing replaced
    target = tc.anchor_path(store.tool_path)
    assert not target.exists() if how == "missing" else target.read_text() == "{broken"
    assert tc.latched_reason(store.tool_path) == f"anchor_{how}"


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_a_recover_save_re_creates_an_anchor_that_vanished(tmp_path, anchor, how):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    target = tc.anchor_path(store.tool_path)
    if how == "missing":
        target.unlink()
    else:
        target.write_text("{broken", encoding="utf-8")
    _restart()
    assert store.load_tools().integrity_error == f"anchor_{how}"
    assert _recover(store, {read}).status_code == 200
    _restart()
    assert store.load_tools().integrity_error is None
    assert tc.read_anchor(store.tool_path)["generation"] == 2


def test_an_anchor_lock_that_is_a_symlink_refuses_the_write(tmp_path, anchor, monkeypatch):
    read = _some("read")
    store = _custom_store(tmp_path, enabled={read})
    anchor.enable(store.tool_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("", encoding="utf-8")
    tc.anchor_lock_path(store.tool_path).unlink(missing_ok=True)
    tc.anchor_lock_path(store.tool_path).symlink_to(elsewhere)
    with pytest.raises(OSError):
        ToolConfig.load(store.tool_path).save()
    assert tc.read_anchor(store.tool_path)["generation"] == 1


def test_the_anchor_lock_is_on_the_write_floor(tmp_path, anchor):
    store = _custom_store(tmp_path, enabled={_some("read")})
    anchor.enable(store.tool_path)
    lock = str(tc.anchor_lock_path(store.tool_path).absolute())
    assert lock in store.protected_files()


# --- ADR 0006: an operation ledger restored from a backup (measured) -----------------


def _rolled_back_ledger_replay(tmp_path, second_answer):
    from test_hitl_binding import CALL, ExternalService
    from test_hitl_persistence import Host, _pause_then_shut_down, _results

    from stiracore.core import HITLAction, HITLDecision
    from stiracore.core.approval import OperationLedger

    service = ExternalService()
    backup = OperationLedger(tmp_path / "backup-operations.db")  # before any operation
    backup._conn.close()
    second = {"toolUseId": "ext-2", "name": "external_update", "input": {"record": "r2"}}
    second["input"]["value"] = "v2"
    _pause_then_shut_down(tmp_path, service, calls=[CALL, second], approve_first=True)
    assert [r for r, _v, _k in service.applied] == ["r1"]  # r1 ran, r2 paused
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"operations.db{suffix}").unlink(missing_ok=True)
    shutil.copy2(tmp_path / "backup-operations.db", tmp_path / "operations.db")
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    worker = threading.Thread(
        target=host.broker.decide,
        args=(restored["id"], HITLDecision(HITLAction.APPROVE)),
        kwargs={"args_hash": restored["args_hash"]},
    )
    worker.start()
    deadline, asked = time.monotonic() + 5, []
    while time.monotonic() < deadline and not asked:
        asked = [p for p in host.broker.pending() if p["id"] != restored["id"]]
        time.sleep(0.02)
    for item in asked:
        action = HITLAction.APPROVE if second_answer == "approve" else HITLAction.REJECT
        host.broker.decide(item["id"], HITLDecision(action), args_hash=item["args_hash"])
    worker.join(10)
    assert not worker.is_alive()
    return service, asked, _results(host.agent)


def test_a_rolled_back_ledger_asks_again_for_a_call_that_already_ran(tmp_path):
    """Measured: the replay asks a fresh human approval for r1 (it no longer knows r1
    ran). Approved again, r1 runs a second time — with the same idempotency key."""
    service, asked, results = _rolled_back_ledger_replay(tmp_path, "approve")
    assert [item["tool_use_id"] for item in asked] == ["ext-1"]
    assert [r for r, _v, _k in service.applied] == ["r1", "r1", "r2"]
    keys = [k for r, _v, k in service.applied if r == "r1"]
    assert keys[0] == keys[1] and keys[0].startswith("op-")
    assert results["ext-1"]["status"] == "success"


def test_a_rolled_back_ledger_runs_nothing_twice_if_the_new_request_is_rejected(tmp_path):
    service, asked, results = _rolled_back_ledger_replay(tmp_path, "reject")
    assert [item["tool_use_id"] for item in asked] == ["ext-1"]
    assert [r for r, _v, _k in service.applied] == ["r1", "r2"]
    assert results["ext-1"]["status"] == "error"


def test_moving_the_approval_store_aside_resumes_nothing(tmp_path):
    """operations.md §8 step 3: without the restored store nothing is resumable."""
    from test_hitl_binding import CALL, ExternalService
    from test_hitl_persistence import Host, _pause_then_shut_down

    service = ExternalService()
    _pause_then_shut_down(tmp_path, service, calls=[CALL])
    aside = tmp_path / "aside"
    aside.mkdir()
    for item in tmp_path.glob("approvals.db*"):
        item.rename(aside / item.name)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    assert host.broker.pending() == [] and service.applied == []
