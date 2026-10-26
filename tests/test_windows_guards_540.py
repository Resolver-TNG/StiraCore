"""#540: POSIX-only calls guarded for Windows (Python 3.11), checked by simulation.

No Windows host here: the missing functions are removed with ``monkeypatch.delattr``
and ``sys.platform`` / ``os.name`` are faked. Each test failed before the fix.
"""

from __future__ import annotations

import os
import sys

import pytest

from stiracore import _platform
from stiracore.observability.event_chain import ChainState, write_head
from stiracore.scheduler.event_log import SchedulerEventLog


def _no_fchmod(monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)


def test_event_chain_head_without_fchmod(monkeypatch, tmp_path):
    _no_fchmod(monkeypatch)
    state = ChainState(seq=1, hash="a" * 64, anchor_seq=0, anchor_hash="0" * 64)
    write_head(tmp_path / "head.json", "ns", None, state)  # AttributeError before
    assert (tmp_path / "head.json").read_text(encoding="utf-8")


def test_scheduler_event_log_without_fchmod(monkeypatch, tmp_path):
    _no_fchmod(monkeypatch)
    log = SchedulerEventLog(tmp_path / "sched.jsonl")
    log.open()
    log.append("tick", {"n": 1}, ts=1.0)  # AttributeError before
    assert log.verify()[0]


def test_fchmod_private_still_applies_on_posix(tmp_path):
    if not hasattr(os, "fchmod"):
        pytest.skip("POSIX only")
    path = tmp_path / "f"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        _platform.fchmod_private(fd, 0o600)
    finally:
        os.close(fd)
    assert path.stat().st_mode & 0o777 == 0o600


def test_utime_nofollow_without_platform_support(monkeypatch, tmp_path):
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "supports_follow_symlinks", set())
    _platform.utime_nofollow(path, 1_000_000_000, 1_000_000_000)
    assert path.stat().st_mtime_ns == 1_000_000_000


def test_utime_nofollow_refuses_link_when_unsupported(monkeypatch, tmp_path):
    target = tmp_path / "t"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "l"
    link.symlink_to(target)
    monkeypatch.setattr(os, "supports_follow_symlinks", set())
    with pytest.raises(OSError):
        _platform.utime_nofollow(link, 1, 1)


def test_memory_removal_uses_utime_helper():
    """``os.utime(..., follow_symlinks=False)`` raises NotImplementedError on Windows."""
    from pathlib import Path

    src = Path(_platform.__file__).parent / "control" / "memory_removal.py"
    assert "follow_symlinks=False" not in src.read_text(encoding="utf-8")


def test_host_channel_refused_off_posix(monkeypatch, tmp_path):
    """No AF_UNIX / O_NOFOLLOW / peer credentials on Windows: fail closed, explicitly."""
    from stiracore.yolo import host_channel

    monkeypatch.setattr(host_channel, "_platform_supported", lambda: False)
    with pytest.raises(host_channel.HostChannelUnsupported):
        host_channel.HostChannelServer(object(), tmp_path, decisions=True)
    assert not list(tmp_path.iterdir())


def test_host_channel_unsupported_is_oserror():
    from stiracore.yolo import host_channel

    assert issubclass(host_channel.HostChannelUnsupported, OSError)


def test_platform_posix_flag(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    assert _platform.is_posix() is False


def test_geteuid_or_none(monkeypatch):
    monkeypatch.delattr(os, "geteuid", raising=False)
    assert _platform.geteuid_or_none() is None
