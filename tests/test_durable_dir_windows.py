"""#543: directory fsync is POSIX-only; Windows must not reject every durable write.

Windows is simulated (no Windows host here): ``sys.platform = "win32"`` plus an
``os.open`` that raises ``PermissionError`` for directories, as Windows does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from stiracore import durable_dir
from stiracore.durable_dir import fsync_dir

_real_open = os.open


def _windows_open(path, flags, *args, **kwargs):
    if Path(path).is_dir():
        raise PermissionError(13, "Permission denied", str(path))
    return _real_open(path, flags, *args, **kwargs)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "open", _windows_open)


@pytest.fixture
def windows_open_only(monkeypatch):
    """Directory open fails like Windows but the platform is not marked win32."""
    monkeypatch.setattr(os, "open", _windows_open)


def test_posix_fsyncs_directory(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX only")
    flushed = []
    fsync_dir(tmp_path, flushed.append)
    assert len(flushed) == 1


def test_posix_failure_stays_fail_closed(tmp_path, windows_open_only):
    if os.name != "posix":
        pytest.skip("POSIX only")
    with pytest.raises(PermissionError):
        fsync_dir(tmp_path)


def test_windows_skips_directory_open(tmp_path, windows):
    fsync_dir(tmp_path)  # must not raise


def test_flush_error_propagates_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX only")

    def broken(_fd):
        raise OSError(5, "EIO")

    with pytest.raises(OSError):
        fsync_dir(tmp_path, broken)


def test_supported_reads_platform_per_call(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert durable_dir._supported() is False


def test_approval_store_submit_works_on_windows(tmp_path, windows):
    from stiracore.core.approval_store import ApprovalStore

    store = ApprovalStore(tmp_path / "a.db")
    record = store.submit(
        request_id="r1",
        agent_id="a",
        tool_name="file_write",
        tool_input={"path": "x"},
        expires_at=9e12,
    )
    assert record.request_id == "r1"
    assert [r.request_id for r in store.pending()] == ["r1"]


def test_approval_store_submit_fails_closed_when_posix_dir_fsync_fails(tmp_path, windows_open_only):
    if os.name != "posix":
        pytest.skip("POSIX only")
    from stiracore.core.approval_store import ApprovalStore, ApprovalStoreError

    store = ApprovalStore(tmp_path / "a.db")
    with pytest.raises((ApprovalStoreError, PermissionError)):
        store.submit(
            request_id="r1",
            agent_id="a",
            tool_name="file_write",
            tool_input={"path": "x"},
            expires_at=9e12,
        )


def test_event_chain_head_on_windows(tmp_path, windows):
    from stiracore.observability import event_chain

    genesis = event_chain.genesis_hash("ns")
    nxt = event_chain.ChainState(1, "a" * 64, 0, genesis)
    head = tmp_path / "head.json"
    event_chain.write_head(head, "ns", None, nxt)
    assert event_chain.read_head(head, "ns")[1] == nxt


def test_yolo_durable_write_on_windows(tmp_path, windows):
    from stiracore.yolo.window import durable_write

    target = tmp_path / "m.json"
    durable_write(target, b"{}")
    assert target.read_bytes() == b"{}"
