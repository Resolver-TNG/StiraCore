"""The grant store ``grants.json`` (ADR 0011 §7, §8; W4 of #296)."""

from __future__ import annotations

import itertools
import json
import os
import stat

import pytest

from stiracore.authority import private_file
from stiracore.grants import store as store_module
from stiracore.grants.store import FILE, GrantStore
from tests.grants_helpers import confirm_and_issue, events, issue_body, make_service

MARKER = "goal-marker-7f3a9c"


def _issue(tmp_path, **kw):
    service, authority, recorder, clocks, _breaker, ws = make_service(tmp_path, **kw)
    result = confirm_and_issue(service, issue_body(ws, goal=f"refactor {MARKER} module"))
    return service, authority, recorder, clocks, ws, result


def _file(tmp_path):
    return tmp_path / "data" / FILE


def test_the_store_is_0600_chained_and_holds_the_goal(tmp_path):
    _service, *_ = _issue(tmp_path)
    path = _file(tmp_path)
    info = os.lstat(path)
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    records = json.loads(path.read_text())
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))
    for before, after in itertools.pairwise(records):
        assert after["prev"] == before["hash"]
    assert MARKER in path.read_text()


def test_a_grant_is_never_persisted_as_active(tmp_path):
    _service, _a, _r, _c, _ws, result = _issue(tmp_path)
    assert result["grant"]["state"] == "active"
    records = json.loads(_file(tmp_path).read_text())
    for record in records:
        for item in record["grants"]:
            assert item["grant"]["status"] in ("revoked", "expired", "ended")
            assert item["grant"]["goal_status"] in ("unmet", "complete")
    last = records[-1]["grants"][0]
    assert (last["grant"]["status"], last["grant"]["reason"], last["restart_from"]) == (
        "revoked",
        "restart",
        "active",
    )


def test_every_start_loads_live_grants_as_revoked_restart_and_records_it(tmp_path):
    service, _a, _r, _c, _ws, result = _issue(tmp_path)
    grant_id = result["grant"]["grant_id"]
    service.pause(grant_id)
    again, _a2, recorder, *_ = make_service(tmp_path)
    view = again.view(grant_id)
    assert (view["state"], view["reason"], view["goal_state"]) == ("revoked", "restart", "unmet")
    restart = [e for e in events(recorder) if e["reason_code"] == "revoked_restart"]
    assert len(restart) == 1 and restart[0]["from"] == "paused"
    assert restart[0]["subject_id"] == grant_id
    # written back without the pending flag: a third start records nothing again
    _third, _a3, recorder3, *_ = make_service(tmp_path)
    assert not [e for e in events(recorder3) if e["reason_code"] == "revoked_restart"]


def _poisoned(tmp_path, damage):
    _service, *_ = _issue(tmp_path)
    path = _file(tmp_path)
    damage(path)
    before = path.read_bytes() if path.exists() and not path.is_symlink() else None
    again, _authority, recorder, *_ = make_service(tmp_path)
    return again, path, before, recorder


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        (lambda p: p.write_bytes(p.read_bytes()[:-5]), "grant_store_malformed"),
        (
            lambda p: p.write_text(p.read_text().replace("refactor", "rEfactor")),
            "grant_store_hash_mismatch",
        ),
        (lambda p: os.chmod(p, 0o644), "grant_store_unsafe"),
        (lambda p: os.link(p, p.with_name("hard")), "grant_store_unsafe"),
    ],
)
def test_a_damaged_or_unsafe_store_is_sticky_unhealthy_and_never_overwritten(
    tmp_path, damage, code
):
    again, path, before, recorder = _poisoned(tmp_path, damage)
    assert again.unhealthy_reason == code
    assert any(e["reason_code"] == code and e["subject_kind"] == "grants" for e in events(recorder))
    again.sync()
    assert again.stop(1.0) == "unhealthy"
    assert path.read_bytes() == before  # the evidence stays


def test_a_symlinked_or_fifo_store_is_unsafe(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text("[]")
    os.chmod(target, 0o600)
    (data / FILE).symlink_to(target)
    store = GrantStore(data)
    assert store.load().reason == "grant_store_unsafe"
    fifo_dir = tmp_path / "fifo"
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / FILE, 0o600)
    assert GrantStore(fifo_dir).load().reason == "grant_store_unsafe"


def test_a_foreign_owner_is_unsafe(tmp_path, monkeypatch):
    _issue(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(_file(tmp_path)).st_uid + 1)
    assert GrantStore(tmp_path / "data").load().reason == "grant_store_unsafe"


def test_a_file_replaced_behind_the_store_is_never_overwritten(tmp_path):
    service, _a, _r, _c, _ws, result = _issue(tmp_path)
    path = _file(tmp_path)
    tampered = path.read_text()
    path.unlink()
    path.write_text(tampered)  # same bytes, another inode
    os.chmod(path, 0o600)
    service.sync()
    assert service.stop(5.0) == "unhealthy"  # the re-read runs on the writer
    assert service.unhealthy_reason == "grant_store_replaced"
    view = service.view(result["grant"]["grant_id"])
    assert (view["state"], view["reason"]) == ("revoked", "unhealthy")
    assert path.read_text() == tampered


def test_a_failed_write_is_sticky_and_revokes(tmp_path, monkeypatch):
    service, _a, _recorder, _c, _ws, result = _issue(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(private_file, "replace", boom)
    service.pause(result["grant"]["grant_id"])
    assert service.unhealthy_reason == "grant_store_write_failed"
    view = service.view(result["grant"]["grant_id"])
    assert (view["state"], view["reason"]) == ("revoked", "unhealthy")
    monkeypatch.undo()
    assert service.stop(1.0) == "unhealthy"  # a later chance to write does not clear it


def test_an_issue_the_store_cannot_hold_is_never_issued(tmp_path, monkeypatch):
    service, _a, recorder, _c, _b, ws = make_service(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(private_file, "replace", boom)
    from stiracore.grants.schema import GrantRefused

    with pytest.raises(GrantRefused, match="grant_store_unhealthy"):
        confirm_and_issue(service, issue_body(ws))
    assert service.list_view()["grants"] == []
    assert not [e for e in events(recorder) if e["reason_code"] == "grant_issued"]


def test_retention_keeps_at_most_max_grants(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_GRANTS", 3)
    import stiracore.grants.service as service_module

    monkeypatch.setattr(service_module, "MAX_GRANTS", 3)
    service, _a, _r, _c, _b, ws = make_service(tmp_path)
    ids = []
    for _ in range(5):
        result = confirm_and_issue(service, issue_body(ws))
        ids.append(result["grant"]["grant_id"])
        service.revoke(ids[-1])
    kept = [g["grant_id"] for g in service.list_view()["grants"]]
    assert kept == list(reversed(ids[-3:]))
    stored = json.loads(_file(tmp_path).read_text())[-1]["grants"]
    assert [item["grant"]["grant_id"] for item in stored] == ids[-3:]


def test_without_a_data_directory_nothing_is_written(tmp_path):
    service, *_rest = make_service(tmp_path, data_dir=None)
    ws = _rest[-1]
    confirm_and_issue(service, issue_body(ws))
    assert not list(tmp_path.rglob(FILE))


def test_a_store_that_holds_a_live_grant_is_refused(tmp_path):
    """A file that says ``active`` (hand-made, a valid chain) is malformed: the store only
    ever holds live grants in their revoked(restart) form."""
    from stiracore.grants import state
    from stiracore.grants.schema import grant_to_dict
    from stiracore.grants.store import _ZERO, _digest
    from tests.grants_helpers import FakeClocks, request

    data = tmp_path / "data"
    data.mkdir()
    live = state.new_grant(request(), grant_id="e" * 32, issuer="dashboard", now=FakeClocks()())
    record = {
        "seq": 1,
        "prev": _ZERO,
        "grants": [{"grant": grant_to_dict(live, "g"), "restart_from": ""}],
    }
    record["hash"] = _digest(record)
    path = data / FILE
    path.write_text(json.dumps([record]))
    os.chmod(path, 0o600)
    assert GrantStore(data).load().reason == "grant_store_malformed"
    stored = state.restart(live, wall=1.0)
    record["grants"] = [{"grant": grant_to_dict(stored, "g"), "restart_from": "active"}]
    record["hash"] = _digest(record)
    path.write_text(json.dumps([record]))
    loaded = GrantStore(data).load()
    assert loaded.reason is None and loaded.entries[0][2] == "active"


def test_the_store_re_reads_before_every_replace(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store = GrantStore(data)
    store.load()
    assert store.write([]) is True
    path = data / FILE
    text = path.read_text()
    path.unlink()
    path.write_text(text)  # same bytes, another file
    os.chmod(path, 0o600)
    assert store.write([]) is False  # no verify() in between: the write itself checks
    assert store.unhealthy_reason == "grant_store_replaced"
    assert path.read_text() == text


def test_a_failed_store_write_is_sticky(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    store = GrantStore(data)
    store.load()
    assert store.write([]) is True
    before = (data / FILE).read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(private_file, "replace", boom)
    assert store.write([]) is False
    monkeypatch.undo()
    assert store.unhealthy_reason == "grant_store_write_failed"
    assert store.write([]) is False  # never written again
    assert (data / FILE).read_bytes() == before
