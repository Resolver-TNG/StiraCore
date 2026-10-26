"""W2 (#296, ADR 0011 §5 / §6 / §9): the latch store and its human clear.

Durable, chained, fail closed: an unusable file is "every latch set" and is never
touched; a latch is cleared only by an operation bound to incarnation + subject + stop id
+ expected generation + op_id. No model is called; no server is started.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from stiracore.authority import latches as latch_mod
from stiracore.authority.latches import LATCHES, LatchRefused, LatchStore, Stop

INC = "1" * 32
OTHER = "2" * 32
OP = "3" * 32
OP2 = "4" * 32
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def _path(tmp_path):
    return tmp_path / latch_mod.LATCH_FILE


def _raw(tmp_path):
    return _path(tmp_path).read_bytes()


def _clear(store, subject, *, by=INC, op_id=OP, stops=None, generation=None):
    latched = store.latched()
    if stops is None:
        stops = (
            {name: latched[name] for name in latched}
            if subject == "all"
            else {subject: latched[subject]}
        )
    return store.clear(
        subject,
        stops=stops,
        expected_generation=store.generation if generation is None else generation,
        op_id=op_id,
        by=by,
    )


def test_a_missing_file_is_a_first_start_written_0600_at_the_first_change(tmp_path):
    store = LatchStore(tmp_path)
    assert store.latched() == {} and store.unhealthy_reason is None
    assert store.generation == 0
    assert not _path(tmp_path).exists()  # a read never creates it
    store.set("kill", incarnation=INC, epoch=1)
    assert store.generation == 1
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(_path(tmp_path)).st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_set_latch_survives_a_new_store(tmp_path):
    store = LatchStore(tmp_path)
    assert store.set("kill", incarnation=INC, epoch=7) is True
    assert store.set("kill", incarnation=OTHER, epoch=9) is False  # keeps the first stop
    again = LatchStore(tmp_path)
    assert again.latched() == {"kill": Stop(INC, 7)}
    assert again.generation == 1


def test_unknown_names_and_free_text_are_refused(tmp_path):
    store = LatchStore(tmp_path)
    for name in ("halted:because the goal said so", "halted:", "KILL", "kill ", 3):
        with pytest.raises(LatchRefused, match="latch_unknown"):
            store.set(name, incarnation=INC, epoch=1)
    assert store.latched() == {}
    store.set("halted:breaker", incarnation=INC, epoch=1)
    assert "halted:breaker" in store.latched()


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json",
        '{"v": 1, "records": []}',
        "flip-hash",
        "drop-first",
        "extra-key",
    ],
)
def test_a_corrupt_file_sets_every_latch_and_is_never_touched(tmp_path, corrupt):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    store.set("unhealthy", incarnation=INC, epoch=2)
    path = _path(tmp_path)
    data = json.loads(path.read_text())
    if corrupt == "flip-hash":
        data["records"][-1]["latches"] = {}
        text = json.dumps(data)
    elif corrupt == "drop-first":
        data["records"] = data["records"][1:]
        text = json.dumps(data)
    elif corrupt == "extra-key":
        # A consistent writer adding a field (its hash recomputed): still refused.
        last = data["records"][-1]
        last["note"] = "the goal text"
        last["hash"] = latch_mod._digest(last)
        text = json.dumps(data)
    else:
        text = corrupt
    path.write_text(text)
    os.chmod(path, 0o600)
    before = path.read_bytes()
    broken = LatchStore(tmp_path)
    assert broken.unhealthy_reason is not None
    assert broken.unhealthy_reason.startswith("latch_store_")
    assert set(broken.latched()) == set(LATCHES)
    with pytest.raises(OSError):
        broken.set("halted:budget", incarnation=INC, epoch=3)
    with pytest.raises(LatchRefused, match="latch_store_unhealthy"):
        broken.clear("kill", stops={}, expected_generation=1, op_id=OP, by=INC)
    assert path.read_bytes() == before  # the evidence stays


@POSIX
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "fifo", "owner"])
def test_an_unsafe_file_sets_every_latch_and_is_never_touched(tmp_path, kind, monkeypatch):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    path = _path(tmp_path)
    before = path.read_bytes()
    if kind == "symlink":
        real = tmp_path / "real"
        os.replace(path, real)
        os.symlink(real, path)
    elif kind == "hardlink":
        os.link(path, tmp_path / "backup")
    elif kind == "mode":
        os.chmod(path, 0o644)
    elif kind == "fifo":
        os.unlink(path)
        os.mkfifo(path, 0o600)
    else:
        from stiracore.authority import epoch_store

        uid = os.getuid()
        monkeypatch.setattr(epoch_store.os, "getuid", lambda: uid + 1)
    broken = LatchStore(tmp_path)
    assert broken.unhealthy_reason == "latch_store_unsafe"
    assert set(broken.latched()) == set(LATCHES)
    if kind != "fifo":
        target = tmp_path / "real" if kind == "symlink" else path
        assert target.read_bytes() == before


def test_a_file_replaced_behind_the_store_is_not_overwritten(tmp_path):
    store = LatchStore(tmp_path)
    other = LatchStore(tmp_path)
    other.set("kill", incarnation=OTHER, epoch=5)  # a second writer (or a person) replaced it
    before = _raw(tmp_path)
    with pytest.raises(OSError):
        store.set("unhealthy", incarnation=INC, epoch=1)
    assert store.unhealthy_reason == "latch_store_replaced"
    assert set(store.latched()) == set(LATCHES)
    assert _raw(tmp_path) == before


def test_a_failed_write_changes_nothing_in_memory_and_latches_the_store(tmp_path, monkeypatch):
    store = LatchStore(tmp_path)

    def failing(*_a, **_k):
        raise OSError(28, "no space")

    monkeypatch.setattr(latch_mod.private_file, "replace", failing)
    with pytest.raises(OSError):
        store.set("kill", incarnation=INC, epoch=1)
    assert store.unhealthy_reason == "latch_store_write_failed"
    assert set(store.latched()) == set(LATCHES)  # fail closed, not "nothing set"


# --- the clear (operation token) -------------------------------------------------------


def test_a_clear_with_the_current_stop_and_generation_clears_once(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    result = _clear(store, "kill", by=OTHER)
    assert result == {"cleared": ["kill"], "generation": 2, "replayed": False}
    assert LatchStore(tmp_path).latched() == {}


def test_the_same_op_id_and_payload_returns_the_original_result(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    stops = {"kill": Stop(INC, 4)}
    first = store.clear("kill", stops=stops, expected_generation=1, op_id=OP, by=OTHER)
    store.set("kill", incarnation=OTHER, epoch=9)  # killed again afterwards
    again = store.clear("kill", stops=stops, expected_generation=1, op_id=OP, by=OTHER)
    assert again == {**first, "replayed": True}
    assert store.latched() == {"kill": Stop(OTHER, 9)}  # the new kill stays
    # Also after a restart (the store is read back).
    reread = LatchStore(tmp_path)
    assert reread.clear("kill", stops=stops, expected_generation=1, op_id=OP, by=OTHER)["replayed"]
    assert reread.latched() == {"kill": Stop(OTHER, 9)}


def test_the_same_op_id_with_another_payload_is_refused(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    store.set("unhealthy", incarnation=INC, epoch=5)
    _clear(store, "kill")
    with pytest.raises(LatchRefused, match="op_id_reused"):
        _clear(store, "unhealthy")
    assert "unhealthy" in store.latched()


def test_an_op_id_of_another_incarnation_is_unknown_never_reapplied(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    stops = {"kill": Stop(INC, 4)}
    store.clear("kill", stops=stops, expected_generation=1, op_id=OP, by=INC)
    store.set("kill", incarnation=OTHER, epoch=4)
    with pytest.raises(LatchRefused, match="op_unknown"):
        store.clear("kill", stops=stops, expected_generation=1, op_id=OP, by=OTHER)
    assert "kill" in store.latched()


def test_a_token_for_an_older_stop_is_resume_stale(tmp_path):
    """ADR 0011 §9 example: halted (stop 41) → resume → halted again (stop 57); a late
    resume for stop 41 is refused and stop 57 stays."""
    store = LatchStore(tmp_path)
    store.set("halted:breaker", incarnation=INC, epoch=41)
    old = {"halted:breaker": Stop(INC, 41)}
    store.clear("halted:breaker", stops=old, expected_generation=1, op_id=OP, by=INC)
    store.set("halted:breaker", incarnation=INC, epoch=57)
    with pytest.raises(LatchRefused, match="resume_stale"):
        store.clear(
            "halted:breaker", stops=old, expected_generation=store.generation, op_id=OP2, by=INC
        )
    assert store.latched() == {"halted:breaker": Stop(INC, 57)}


def test_the_same_epoch_of_another_incarnation_is_resume_stale(tmp_path):
    """A rollback can reproduce an epoch value; the stop id carries the incarnation."""
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=OTHER, epoch=4)
    with pytest.raises(LatchRefused, match="resume_stale"):
        _clear(store, "kill", stops={"kill": Stop(INC, 4)})
    assert "kill" in store.latched()


def test_a_changed_generation_is_resume_stale(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    seen = store.generation
    store.set("unhealthy", incarnation=INC, epoch=5)  # changed while the person confirmed
    with pytest.raises(LatchRefused, match="resume_stale"):
        _clear(store, "kill", generation=seen)
    assert "kill" in store.latched()


def test_clearing_a_latch_that_is_not_set_is_resume_stale(tmp_path):
    store = LatchStore(tmp_path)
    with pytest.raises(LatchRefused, match="resume_stale"):
        store.clear("kill", stops={"kill": Stop(INC, 1)}, expected_generation=0, op_id=OP, by=INC)


def test_all_clears_exactly_the_latches_the_person_saw(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    store.set("unhealthy", incarnation=INC, epoch=5)
    with pytest.raises(LatchRefused, match="resume_stale"):
        _clear(store, "all", stops={"kill": Stop(INC, 4)})  # one is missing from the view
    result = _clear(store, "all")
    assert result["cleared"] == ["kill", "unhealthy"]
    assert store.latched() == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_the_file_holds_codes_ids_and_hashes_only(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=4)
    _clear(store, "kill")
    data = json.loads(_raw(tmp_path))
    text = json.dumps(data)
    assert set(data) == {"v", "records"}
    for record in data["records"]:
        assert set(record) == {"seq", "prev", "hash", "store", "latches", "change"}
    assert "goal" not in text and "args" not in text


@POSIX
def test_a_file_swapped_between_the_check_and_the_open_is_refused(tmp_path, monkeypatch):
    """lstat sees the real file, the descriptor is another one (a swap in between)."""
    from stiracore.authority import private_file

    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    path = _path(tmp_path)
    other = tmp_path / "other"
    other.write_bytes(path.read_bytes())
    os.chmod(other, 0o600)
    real_open = private_file.os.open

    def swapping_open(target, flags, *args):
        if str(target) == str(path):
            os.replace(other, path)  # same content, another inode
        return real_open(target, flags, *args)

    monkeypatch.setattr(private_file.os, "open", swapping_open)
    broken = LatchStore(tmp_path)
    assert broken.unhealthy_reason == "latch_store_unsafe"
    assert set(broken.latched()) == set(LATCHES)


# --- B2 (fix round 1): the commitment in the epoch store and the lifecycle cross-check --


def _service(tmp_path):
    from stiracore.authority import AuthorityService

    return AuthorityService(tmp_path)


def _killed_then(tmp_path):
    from stiracore.authority.outbox import LifecycleRecorder

    service = _service(tmp_path)
    recorder = LifecycleRecorder(tmp_path)
    service.recorder = recorder
    service.kill_switch()
    recorder.drain()
    return service


def _assert_refused(tmp_path, reason="latch_store_rolled_back"):
    from stiracore.authority import AuthorityError, GrantRecord, Receipt

    restarted = _service(tmp_path)
    assert restarted.killed is True
    assert restarted.unhealthy_reason == reason
    assert set(restarted.latches.latched()) == set(LATCHES)
    record = GrantRecord("yolo", "a" * 32, restarted.incarnation, 1, "active")
    with pytest.raises(AuthorityError):
        restarted.issue_permit(
            record,
            kind="yolo",
            receipt=Receipt("op:" + "b" * 32 + ":decision", 1, "c" * 64),
        )
    return restarted


def test_a_deleted_latch_file_never_clears_a_kill(tmp_path):
    _killed_then(tmp_path)
    _path(tmp_path).unlink()
    _assert_refused(tmp_path)


def test_a_rollback_to_a_pre_kill_latch_file_never_clears_a_kill(tmp_path):
    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    old = _raw(tmp_path)
    service.kill_switch()
    _path(tmp_path).write_bytes(old)
    os.chmod(_path(tmp_path), 0o600)
    _assert_refused(tmp_path)


def test_a_truncated_latch_file_sets_every_latch(tmp_path):
    _killed_then(tmp_path)
    raw = _raw(tmp_path)
    _path(tmp_path).write_bytes(raw[: len(raw) // 2])
    os.chmod(_path(tmp_path), 0o600)
    _assert_refused(tmp_path, "latch_store_malformed")


def test_a_kill_in_the_log_without_its_latch_is_refused_even_without_an_epoch_file(tmp_path):
    from stiracore.authority import epoch_store
    from stiracore.authority.service import KILL_FIRED

    _killed_then(tmp_path)
    _path(tmp_path).unlink()
    (tmp_path / epoch_store.EPOCH_FILE).unlink()  # no commitment left: the log decides
    (tmp_path / KILL_FIRED).unlink()  # nor the kill marker (only the log is left)
    _assert_refused(tmp_path)


def test_a_kill_cleared_by_a_person_in_the_log_starts_normally(tmp_path):
    from stiracore.authority import cli as authority_cli

    _killed_then(tmp_path)
    args = type("A", (), {"authority_command": "unlatch", "data_dir": tmp_path, "latch": "kill"})
    assert authority_cli.run(args, is_tty=lambda: True, read_line=lambda _p: "UNLATCH") == 0
    restarted = _service(tmp_path)
    assert restarted.killed is False and restarted.unhealthy_reason is None


def test_a_w1_directory_upgrades_once_and_a_later_deletion_is_refused(tmp_path):
    from stiracore.authority import epoch_store

    w1 = epoch_store.next_record(None, 5)
    epoch_store.write(tmp_path, (w1,))
    assert not _path(tmp_path).exists()
    upgraded = _service(tmp_path)
    assert upgraded.killed is False and upgraded.unhealthy_reason is None
    assert epoch_store.load(tmp_path).last.commitment == (0, "0" * 64, "0" * 32)  # committed
    assert _service(tmp_path).unhealthy_reason is None  # an empty store stays fine
    upgraded = _service(tmp_path)
    upgraded.kill_switch()
    _path(tmp_path).unlink()  # the second deletion: refused
    _assert_refused(tmp_path)


def test_a_newer_latch_store_than_its_commitment_is_accepted(tmp_path):
    """The crash window between a latch write and its commitment (and the offline
    unlatch, which commits nothing) — newer is never less safe."""
    service = _service(tmp_path)
    service.latches.set("kill", incarnation=INC, epoch=3)  # not committed
    restarted = _service(tmp_path)
    assert restarted.killed is True and restarted.unhealthy_reason is None


def test_commitment_only_epoch_records(tmp_path):
    from stiracore.authority import epoch_store

    first = epoch_store.next_record(None, 4, (0, "0" * 64, "0" * 32))
    moved = epoch_store.next_record(first, 4, (1, "a" * 64, "b" * 32))
    epoch_store.write(tmp_path, (first, moved))
    assert epoch_store.load(tmp_path).status == "ok"
    same = epoch_store.next_record(moved, 4, (1, "a" * 64, "b" * 32))
    epoch_store.write(tmp_path, (first, moved, same))
    assert epoch_store.load(tmp_path).reason == "epoch_store_not_monotonic"
    back = epoch_store.next_record(moved, 5)  # v2 then v1: no way back
    epoch_store.write(tmp_path, (first, moved, back))
    assert epoch_store.load(tmp_path).reason == "epoch_store_malformed"
    lower = epoch_store.next_record(moved, 5, (0, "c" * 64, "d" * 32))  # never restarts
    epoch_store.write(tmp_path, (first, moved, lower))
    assert epoch_store.load(tmp_path).reason == "epoch_store_not_monotonic"


# --- reset-latches -------------------------------------------------------------------------


def _reset_args(tmp_path):
    return type("A", (), {"authority_command": "reset-latches", "data_dir": tmp_path})


def test_reset_latches_sets_every_latch_keeps_the_old_file_and_commits(tmp_path):
    from stiracore.authority import cli as authority_cli
    from stiracore.authority import epoch_store
    from stiracore.authority.lifecycle_events import LifecycleLog

    _killed_then(tmp_path)
    _path(tmp_path).write_text("garbage")
    os.chmod(_path(tmp_path), 0o600)
    assert (
        authority_cli.run(_reset_args(tmp_path), is_tty=lambda: True, read_line=lambda _p: "RESET")
        == 0
    )
    kept = [p for p in tmp_path.iterdir() if p.name.startswith("authority-latches.replaced-")]
    assert len(kept) == 1 and kept[0].read_text() == "garbage"
    store = LatchStore(tmp_path)
    assert set(store.latched()) == set(LATCHES)
    assert epoch_store.load(tmp_path).last.commitment == store.commitment()
    reasons = [e["reason_code"] for e in LifecycleLog(tmp_path).events()]
    assert reasons.count("latch_store_reset") == len(LATCHES)
    restarted = _service(tmp_path)  # all set, but the store itself is accepted
    assert restarted.killed and restarted.unhealthy_reason == "latched_unhealthy"
    args = type("A", (), {"authority_command": "unlatch", "data_dir": tmp_path, "latch": "all"})
    assert authority_cli.run(args, is_tty=lambda: True, read_line=lambda _p: "UNLATCH") == 0
    clean = _service(tmp_path)
    assert clean.killed is False and clean.unhealthy_reason is None


def test_reset_latches_needs_the_word_a_tty_and_the_server_stopped(tmp_path):
    from stiracore.authority import cli as authority_cli
    from stiracore.config.data_dir_lock import DataDirOwnerLock

    _killed_then(tmp_path)
    _path(tmp_path).unlink()
    run = authority_cli.run
    assert run(_reset_args(tmp_path), is_tty=lambda: True, read_line=lambda _p: "yes") == 1
    assert run(_reset_args(tmp_path), is_tty=lambda: False, read_line=lambda _p: "RESET") == 1
    if os.name == "posix":
        lock = DataDirOwnerLock(tmp_path)
        lock.acquire()
        try:
            assert (
                run(_reset_args(tmp_path), is_tty=lambda: True, read_line=lambda _p: "RESET") == 2
            )
        finally:
            lock.release()
    assert not _path(tmp_path).exists()
    _assert_refused(tmp_path)


def test_a_store_rewritten_with_the_same_bytes_on_another_inode_is_not_overwritten(tmp_path):
    """Opus lesser survivor: the identity compare before a replace."""
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    raw = _raw(tmp_path)
    _path(tmp_path).unlink()
    _path(tmp_path).write_bytes(raw)  # the same records, another inode
    os.chmod(_path(tmp_path), 0o600)
    with pytest.raises(OSError):
        store.set("unhealthy", incarnation=INC, epoch=2)
    assert store.unhealthy_reason == "latch_store_replaced"
    assert _raw(tmp_path) == raw


# --- fix round 2: lineage (O-M1) --------------------------------------------------------


def _unlatch_all(tmp_path):
    from stiracore.authority import cli as authority_cli

    args = type("A", (), {"authority_command": "unlatch", "data_dir": tmp_path, "latch": "all"})
    return authority_cli.run(args, is_tty=lambda: True, read_line=lambda _p: "UNLATCH")


def _reset(tmp_path):
    from stiracore.authority import cli as authority_cli

    return authority_cli.run(
        _reset_args(tmp_path), is_tty=lambda: True, read_line=lambda _p: "RESET"
    )


def _drop_log(tmp_path):
    from stiracore.authority.lifecycle_events import LOG_FILE, OLD_LOG_FILE

    for name in (LOG_FILE, OLD_LOG_FILE):
        if (tmp_path / name).exists():
            (tmp_path / name).unlink()


def test_a_backup_from_before_a_reset_is_refused_also_without_the_log(tmp_path):
    """Opus r2 M1: backup (no kill, higher generation) → kill → corrupt → reset →
    unlatch all → kill again → restore the backup with the log gone: refused."""
    service = _service(tmp_path)
    for epoch in range(1, 9):
        service.latches.set("halted:budget", incarnation=INC, epoch=epoch)
        service.latches.clear(
            "halted:budget",
            stops={"halted:budget": Stop(INC, epoch)},
            expected_generation=service.latches.generation,
            op_id=f"{epoch:032x}",
            by=INC,
        )
    service._commit_latches()
    backup = _raw(tmp_path)  # generation 16, nothing set
    _killed_then(tmp_path)
    _path(tmp_path).write_text("garbage")
    os.chmod(_path(tmp_path), 0o600)
    assert _reset(tmp_path) == 0
    assert _unlatch_all(tmp_path) == 0
    _killed_then(tmp_path)
    assert LatchStore(tmp_path).generation > 16  # never restarted
    _path(tmp_path).write_bytes(backup)
    os.chmod(_path(tmp_path), 0o600)
    _drop_log(tmp_path)
    _assert_refused(tmp_path)


def test_reset_then_restoring_any_older_copy_is_refused(tmp_path):
    _killed_then(tmp_path)
    older = _raw(tmp_path)
    _path(tmp_path).write_text("garbage")
    os.chmod(_path(tmp_path), 0o600)
    assert _reset(tmp_path) == 0
    _path(tmp_path).write_bytes(older)  # the pre-reset store (it even holds the kill)
    os.chmod(_path(tmp_path), 0o600)
    _drop_log(tmp_path)
    _assert_refused(tmp_path)


def test_a_same_generation_store_with_other_contents_is_refused(tmp_path):
    """O-M2 (b): the same generation, another record (same store instance)."""
    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    other = _raw(tmp_path)
    data = json.loads(other)
    service.latches.set("kill", incarnation=INC, epoch=2)
    service._commit_latches()
    # A different second record on the same chain: same generation, other hash.
    forged = json.loads(_raw(tmp_path))
    forged["records"][-1]["latches"] = data["records"][-1]["latches"]
    forged["records"][-1]["hash"] = latch_mod._digest(forged["records"][-1])
    _path(tmp_path).write_text(json.dumps(forged))
    os.chmod(_path(tmp_path), 0o600)
    _drop_log(tmp_path)
    _assert_refused(tmp_path)


def test_a_normal_restart_and_offline_unlatches_keep_the_lineage(tmp_path):
    _killed_then(tmp_path)
    assert _unlatch_all(tmp_path) == 0
    restarted = _service(tmp_path)
    assert restarted.killed is False and restarted.unhealthy_reason is None
    restarted.kill_switch()
    again = _service(tmp_path)
    assert again.killed and again.unhealthy_reason is None


# --- fix round 2: a kill on a full disk (Opus minor) -------------------------------------


def test_a_kill_on_a_full_disk_survives_a_restart_through_the_marker(tmp_path, monkeypatch):
    from stiracore.authority import epoch_store, private_file
    from stiracore.authority.service import KILL_ARMED, KILL_FIRED

    service = _service(tmp_path)
    assert (tmp_path / KILL_ARMED).exists()

    def full(*_a, **_k):
        raise OSError(28, "no space left on device")

    monkeypatch.setattr(private_file, "replace", full)
    monkeypatch.setattr(private_file, "append_line", full)
    monkeypatch.setattr(epoch_store, "write", full)
    assert service.kill_switch()["latch"] == "failed"
    assert (tmp_path / KILL_FIRED).exists() and not (tmp_path / KILL_ARMED).exists()
    monkeypatch.undo()
    assert not LatchStore(tmp_path).is_set("kill")  # the latch itself was lost
    _assert_refused(tmp_path)


def test_unlatching_the_kill_disarms_the_marker(tmp_path):
    from stiracore.authority.service import KILL_ARMED, KILL_FIRED

    _killed_then(tmp_path)
    assert (tmp_path / KILL_FIRED).exists()
    assert _unlatch_all(tmp_path) == 0
    assert not (tmp_path / KILL_FIRED).exists()
    restarted = _service(tmp_path)
    assert restarted.killed is False and restarted.unhealthy_reason is None
    assert (tmp_path / KILL_ARMED).exists()  # armed again


def test_a_rollback_of_the_unhealthy_latch_is_refused(tmp_path):
    """Opus r2 minor: the unhealthy latch is committed as well."""
    service = _service(tmp_path)
    before = _raw(tmp_path) if _path(tmp_path).exists() else None
    service.report_unhealthy("lifecycle_write_failed")
    assert service.persist_unhealthy() == "lifecycle_write_failed"
    if before is None:
        _path(tmp_path).unlink()
    else:
        _path(tmp_path).write_bytes(before)
    _drop_log(tmp_path)
    restarted = _service(tmp_path)
    assert restarted.unhealthy_reason == "latch_store_rolled_back"


# --- fix round 3: the armed marker must be a safe file (A-M2) ----------------------------


@POSIX
@pytest.mark.parametrize(
    "kind", ["symlink", "dangling", "hardlink", "fifo", "mode", "directory", "owner", "eacces"]
)
def test_an_unusable_armed_marker_starts_the_service_unhealthy(tmp_path, kind, monkeypatch):
    from stiracore.authority import AuthorityError, GrantRecord, Receipt
    from stiracore.authority import service as service_mod
    from stiracore.authority.service import KILL_ARMED

    armed = tmp_path / KILL_ARMED
    if kind == "symlink":
        (tmp_path / "target").write_text("")
        os.chmod(tmp_path / "target", 0o600)
        os.symlink(tmp_path / "target", armed)
    elif kind == "dangling":
        os.symlink(tmp_path / "nowhere", armed)
    elif kind == "hardlink":
        armed.write_text("")
        os.chmod(armed, 0o600)
        os.link(armed, tmp_path / "second")
    elif kind == "fifo":
        os.mkfifo(armed, 0o600)
    elif kind == "mode":
        armed.write_text("")
        os.chmod(armed, 0o666)
    elif kind == "directory":
        armed.mkdir()
    elif kind == "owner":
        armed.write_text("")
        os.chmod(armed, 0o600)
        from stiracore.authority import epoch_store

        uid = os.getuid()
        monkeypatch.setattr(epoch_store.os, "getuid", lambda: uid + 1)
    else:
        real_open = service_mod.os.open

        def refusing(path, flags, *args):
            if str(path).endswith(KILL_ARMED):
                raise PermissionError(13, "denied")
            return real_open(path, flags, *args)

        monkeypatch.setattr(service_mod.os, "open", refusing)
    service = _service(tmp_path)
    expected = "kill_marker_unavailable" if kind == "eacces" else "kill_marker_unsafe"
    if kind == "owner":
        # every file of the directory then counts as another user's: unhealthy anyway
        assert service.unhealthy_reason is not None
    else:
        assert service.unhealthy_reason == expected
    record = GrantRecord("yolo", "a" * 32, service.incarnation, 1, "active")
    with pytest.raises(AuthorityError):
        service.issue_permit(
            record, kind="yolo", receipt=Receipt("op:" + "b" * 32 + ":decision", 1, "c" * 64)
        )


def test_a_safe_armed_marker_starts_healthy(tmp_path):
    from stiracore.authority.service import KILL_ARMED

    assert _service(tmp_path).unhealthy_reason is None
    assert (tmp_path / KILL_ARMED).is_file()
    assert _service(tmp_path).unhealthy_reason is None  # the existing one is accepted


# --- fix round 3: unlatch validates like the server's start (O-M1) ---------------------


def _unlatch(tmp_path, latch):
    from stiracore.authority import cli as authority_cli

    args = type("A", (), {"authority_command": "unlatch", "data_dir": tmp_path, "latch": latch})
    return authority_cli.run(args, is_tty=lambda: True, read_line=lambda _p: "UNLATCH")


def test_unlatch_refuses_an_older_restored_store_and_leaves_the_epoch_file_valid(tmp_path):
    """Opus r3 (1): a lower generation restored; unlatch would have written a lower
    commitment (epoch file not monotonic, reset-latches then impossible)."""
    from stiracore.authority import epoch_store

    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    backup = _raw(tmp_path)
    service.report_unhealthy("lifecycle_write_failed")
    service.persist_unhealthy()
    service.kill_switch()
    epoch_before = (tmp_path / epoch_store.EPOCH_FILE).read_bytes()
    _path(tmp_path).write_bytes(backup)
    os.chmod(_path(tmp_path), 0o600)
    assert _unlatch(tmp_path, "halted:budget") == 1
    assert (tmp_path / epoch_store.EPOCH_FILE).read_bytes() == epoch_before
    assert epoch_store.load(tmp_path).status == "ok"
    assert "halted:budget" in LatchStore(tmp_path).latched()  # nothing cleared
    assert _reset(tmp_path) == 0  # the documented repair still works
    assert _unlatch_all(tmp_path) == 0
    clean = _service(tmp_path)
    assert clean.unhealthy_reason is None and clean.killed is False


def test_unlatch_refuses_a_same_generation_store_without_the_log(tmp_path):
    """Opus r3 (2): the same generation, other contents, the log gone: no laundering."""
    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    backup = _raw(tmp_path)  # generation 1: halted:budget
    service.latches.clear(
        "halted:budget",
        stops={"halted:budget": Stop(INC, 1)},
        expected_generation=1,
        op_id="9" * 32,
        by=INC,
    )
    service.report_unhealthy("lifecycle_write_failed")
    service.persist_unhealthy()  # generation 3 committed: unhealthy only
    _path(tmp_path).write_bytes(backup)
    os.chmod(_path(tmp_path), 0o600)
    _drop_log(tmp_path)
    assert _unlatch(tmp_path, "halted:budget") == 1
    restarted = _service(tmp_path)
    assert restarted.unhealthy_reason == "latch_store_rolled_back"  # still refused


def test_status_names_a_store_that_is_not_the_committed_one(tmp_path, capsys):
    from stiracore.authority import cli as authority_cli

    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    backup = _raw(tmp_path)
    service.kill_switch()
    _path(tmp_path).write_bytes(backup)
    os.chmod(_path(tmp_path), 0o600)
    args = type("A", (), {"authority_command": "status", "data_dir": tmp_path})
    assert authority_cli.run(args) == 0
    assert "NOT THE COMMITTED ONE (latch_store_rolled_back)" in capsys.readouterr().out


# --- fix round 3: test strength (Opus r3 minor) ------------------------------------------


def _forge(tmp_path, change):
    data = json.loads(_raw(tmp_path))
    change(data["records"])
    prev = None
    for record in data["records"]:
        if prev is not None:
            record["prev"] = prev
        record["hash"] = latch_mod._digest(record)
        prev = record["hash"]
    _path(tmp_path).write_text(json.dumps(data))
    os.chmod(_path(tmp_path), 0o600)


def test_a_file_mixing_two_store_ids_is_malformed(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    store.set("unhealthy", incarnation=INC, epoch=2)
    _forge(tmp_path, lambda records: records[1].update(store="e" * 32))
    assert LatchStore(tmp_path).unhealthy_reason == "latch_store_malformed"


def test_a_reset_record_after_the_first_is_malformed(tmp_path):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    store.set("unhealthy", incarnation=INC, epoch=2)

    def make_reset(records):
        records[1]["change"]["op"] = "reset"

    _forge(tmp_path, make_reset)
    assert LatchStore(tmp_path).unhealthy_reason == "latch_store_malformed"


def test_a_reset_store_must_chain_to_the_committed_head(tmp_path):
    """A reset record with the right generation but another ``prev`` does not continue
    the committed store."""
    _killed_then(tmp_path)
    from stiracore.authority import epoch_store

    committed = epoch_store.load(tmp_path).last.commitment
    _path(tmp_path).unlink()
    LatchStore(tmp_path).reset_all(
        incarnation=INC, epoch=1, after=(committed[0], "f" * 64, committed[2])
    )
    _drop_log(tmp_path)
    _assert_refused(tmp_path)


@POSIX
def test_unlatch_refuses_when_the_fired_marker_cannot_be_removed(tmp_path):
    from stiracore.authority.service import KILL_FIRED

    _killed_then(tmp_path)
    (tmp_path / KILL_FIRED).unlink()
    (tmp_path / KILL_FIRED).mkdir()  # not removable by unlink
    assert _unlatch(tmp_path, "kill") == 1
    assert "kill" in LatchStore(tmp_path).latched()  # nothing cleared


# --- fix round 4: unlatch refuses a kill marker the server would refuse (item 5) --------


def test_unlatch_refuses_while_a_fired_marker_has_no_kill_latch(tmp_path, capsys):
    from stiracore.authority.service import KILL_FIRED

    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    (tmp_path / KILL_FIRED).write_text("")
    os.chmod(tmp_path / KILL_FIRED, 0o600)
    assert _unlatch(tmp_path, "halted:budget") == 1
    assert "latch_store_rolled_back" in capsys.readouterr().err
    assert "halted:budget" in LatchStore(tmp_path).latched()


@POSIX
def test_unlatch_refuses_an_unsafe_kill_marker(tmp_path, capsys):
    from stiracore.authority.service import KILL_ARMED

    service = _service(tmp_path)
    service.latches.set("halted:budget", incarnation=INC, epoch=1)
    service._commit_latches()
    os.unlink(tmp_path / KILL_ARMED)
    os.symlink(tmp_path / "elsewhere", tmp_path / KILL_ARMED)
    assert _unlatch(tmp_path, "halted:budget") == 1
    assert "kill_marker_unsafe" in capsys.readouterr().err
    assert "halted:budget" in LatchStore(tmp_path).latched()
