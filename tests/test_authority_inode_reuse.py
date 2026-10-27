"""#661: a delete + recreate of an authority / grant file is detected even when the
filesystem hands the freed inode number to the new file (ext4 and tmpfs do so at once).

The identity of these files is ``(st_dev, st_ino, st_size, st_ctime_ns)``, recorded by the
writer after its own last change. macOS (APFS) does not reuse an inode number at once, so
the reuse is made deterministic here: after the recreate, ``os.lstat`` / ``os.fstat``
report the old device / inode for the new file (what Linux shows anyway).
"""

from __future__ import annotations

import os
import time

import pytest

from stiracore.authority import epoch_store, private_file
from stiracore.authority.latches import LATCH_FILE, LatchStore
from stiracore.authority.lifecycle_events import LOG_FILE, OLD_LOG_FILE, LifecycleLog
from stiracore.grants.store import FILE as GRANT_FILE
from stiracore.grants.store import GrantStore

INC = "1" * 32

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes (0600)")


class _Reused:
    """A stat result that shows another device / inode (the reused number)."""

    def __init__(self, real, dev, ino):
        self._real, self.st_dev, self.st_ino = real, dev, ino

    def __getattr__(self, name):
        return getattr(self._real, name)


def _recreate_on_the_same_inode(path, monkeypatch):
    """Delete ``path`` and write the same bytes into a new ``0600`` file; from now on the
    new file reports the old file's device / inode number (inode reuse)."""
    old = os.lstat(path)
    raw = path.read_bytes()
    time.sleep(0.01)  # a coarse ctime clock still ticks (Linux: one jiffy)
    path.unlink()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    new = os.lstat(path)
    if (new.st_dev, new.st_ino) == (old.st_dev, old.st_ino):
        return raw  # the filesystem reused the number itself (Linux)
    real_lstat, real_fstat = os.lstat, os.fstat

    def reused(info):
        if (info.st_dev, info.st_ino) == (new.st_dev, new.st_ino):
            return _Reused(info, old.st_dev, old.st_ino)
        return info

    monkeypatch.setattr(os, "lstat", lambda *a, **k: reused(real_lstat(*a, **k)))
    monkeypatch.setattr(os, "fstat", lambda *a, **k: reused(real_fstat(*a, **k)))
    return raw


def test_the_identity_has_the_size_and_the_change_time(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    info = os.stat(path)
    assert epoch_store.identity(info) == (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_ctime_ns,
    )


@posix_only
def test_the_writer_records_the_identity_after_its_own_rename(tmp_path):
    """The identity ``replace`` returns is what the next read sees (no false alarm from
    the writer's own write, flush or rename)."""
    for body in (b"one", b"two", b"three"):
        written = private_file.replace(tmp_path, "x", body)
        loaded = private_file.read(tmp_path / "x", max_bytes=100, prefix="x")
        assert loaded.status == "ok" and loaded.identity == written


@posix_only
def test_a_latch_store_recreated_on_the_same_inode_is_not_overwritten(tmp_path, monkeypatch):
    store = LatchStore(tmp_path)
    store.set("kill", incarnation=INC, epoch=1)
    raw = _recreate_on_the_same_inode(tmp_path / LATCH_FILE, monkeypatch)
    with pytest.raises(OSError):
        store.set("unhealthy", incarnation=INC, epoch=2)
    assert store.unhealthy_reason == "latch_store_replaced"
    assert (tmp_path / LATCH_FILE).read_bytes() == raw


@posix_only
def test_a_grant_store_recreated_on_the_same_inode_is_not_overwritten(tmp_path, monkeypatch):
    store = GrantStore(tmp_path)
    store.load()
    assert store.write([]) is True
    raw = _recreate_on_the_same_inode(tmp_path / GRANT_FILE, monkeypatch)
    assert store.verify() == "grant_store_replaced"
    assert store.write([]) is False
    assert (tmp_path / GRANT_FILE).read_bytes() == raw


@posix_only
def test_a_lifecycle_log_recreated_on_the_same_inode_is_not_appended_to(tmp_path, monkeypatch):
    log = LifecycleLog(tmp_path)
    log.append(_event())
    raw = _recreate_on_the_same_inode(tmp_path / LOG_FILE, monkeypatch)
    with pytest.raises(OSError):
        log.append(_event(epoch=4))
    assert log.unhealthy_reason == "lifecycle_log_replaced"
    assert (tmp_path / LOG_FILE).read_bytes() == raw


@posix_only
def test_an_epoch_store_recreated_on_the_same_inode_differs_from_what_was_written(
    tmp_path, monkeypatch
):
    record = epoch_store.next_record(None, 1)
    written = epoch_store.write(tmp_path, (record,))
    assert epoch_store.load(tmp_path).identity == written
    _recreate_on_the_same_inode(tmp_path / epoch_store.EPOCH_FILE, monkeypatch)
    loaded = epoch_store.load(tmp_path)
    assert loaded.status == "ok" and loaded.identity != written


@posix_only
def test_a_rotated_log_still_verifies(tmp_path, monkeypatch):
    """The rotation renames the segment (a rename sets st_ctime on Linux): the sealed
    segment's identity is taken again, so the next pass does not see a replacement."""
    from stiracore.authority import lifecycle_events

    monkeypatch.setattr(lifecycle_events, "ROTATE_BYTES", 1500)
    log = LifecycleLog(tmp_path)
    for epoch in range(1, 12):
        log.append(_event(epoch=epoch))
    assert (tmp_path / OLD_LOG_FILE).exists()
    assert log.verify() is None


def _event(epoch=3):
    from stiracore.authority.lifecycle_events import make_event

    return make_event(
        "latch",
        "kill",
        "cleared",
        "set",
        "kill_switch",
        incarnation=INC,
        epoch=epoch,
        ts_wall=1000.0,
    )
