"""W6a (#296): progress by outcome — the fingerprint, its normalizers and the rules.

Pure unit tests of ``stiracore.grants.progress`` (no executor, no model):

* the normalizer table: one false-positive test per normalizer (volatile fields stripped:
  durations, pids, temp paths, timestamps, ids, failure texts; a real change in a count
  differs);
* the fingerprint: keyed per grant, order-independent over files, sensitive to bytes,
  checks and the report;
* the rules: ring size, loop window (A-A, A-B-A-B, A-B-C-A; seven back is no loop),
  same blocker ×3 and its reset, no action ×2;
* fail closed: no digest → ``none``.
The executor-level behaviour is in ``tests/test_grants_progress_exec.py``.
"""

from __future__ import annotations

import os

import pytest

from stiracore.grants import progress as pg
from stiracore.grants.progress import (
    ABSENT,
    LOOP_WINDOW,
    NO_ACTION_LIMIT,
    RING_SIZE,
    SAME_BLOCKER_LIMIT,
    ProgressTracker,
    Trackers,
    file_digest,
    normalize_checks,
)


def _checks(failed=2, passed=10, duration=123, tmp="/tmp/pytest-of-x/123", pid=4242, **kw):
    check = {
        "kind": "pytest",
        "status": "failed" if failed else "passed",
        "reason": None,
        "exit_code": 1 if failed else 0,
        "duration_ms": duration,
        "counts": {"passed": passed, "failed": failed, "errors": 0, "skipped": 1},
        "consistent": True,
        "failures": [{"id": f"test_x[{tmp}]", "message": f"pid {pid}", "text": tmp}],
        "limit": None,
        "output_bytes": 999 + duration,
    }
    check.update(kw)
    return {
        "status": check["status"],
        "reason": None,
        "containment": "sandbox",
        "checks": [check],
        "tree_changes": {"added": [tmp], "changed": [], "removed": [], "more": 0},
        "real_tree_changed": False,
        "output_tail": f"ran in {duration} ms pid={pid} at 2026-10-25T23:{duration % 60:02d}",
        "limits": {"limits": {"RLIMIT_CPU": duration}},
        "_directory": tmp,
        "duration_ms": duration,
    }


# --- the normalizer table --------------------------------------------------------------------


def test_the_normalizer_table_covers_the_effect_tools_only():
    assert set(pg.NORMALIZERS) == {"editor", "run_checks"}


def test_run_checks_volatile_fields_are_stripped():
    a = normalize_checks(_checks(duration=5, tmp="/tmp/a1", pid=1))
    b = normalize_checks(_checks(duration=90_000, tmp="/private/var/b2", pid=99999))
    assert a == b


@pytest.mark.parametrize(
    "change",
    [
        {"failed": 3},
        {"passed": 11},
        {"failed": 0},
    ],
)
def test_run_checks_a_real_change_in_the_counts_differs(change):
    base = normalize_checks(_checks())
    assert normalize_checks(_checks(**change)) != base


def test_run_checks_status_reason_exit_and_kind_matter():
    base = normalize_checks(_checks())
    assert normalize_checks(_checks(kind="ruff")) != base
    assert normalize_checks(_checks(exit_code=2)) != base
    assert normalize_checks(_checks(reason="usage_error")) != base
    errored = _checks()
    errored["checks"][0]["status"] = "error"
    assert normalize_checks(errored) != base
    grouped = _checks()
    grouped["status"] = "error"  # the call's overall status: the worst of its group
    assert normalize_checks(grouped) == base  # not part of the outcome (per-check rows)


def test_a_result_without_rows_is_one_run_row():
    out = normalize_checks({"status": "error", "reason": "internal_error", "checks": []})
    assert out == (("run", "error", "internal_error"),)


def test_run_checks_without_an_outcome_is_none():
    assert normalize_checks({"status": "refused", "reason": "containment_unverified"}) is None
    assert normalize_checks(None) is None
    assert normalize_checks("passed") is None


def test_run_checks_free_text_codes_are_folded_and_counts_bounded():
    weird = _checks(kind="My Kind /tmp/x", reason="Free text!")
    out = normalize_checks(weird)
    assert "My Kind /tmp/x" not in repr(out) and "Free text!" not in repr(out)
    huge = normalize_checks(_checks(failed=10**12))
    assert pg.COUNT_CAP in huge[0][4]


def test_editor_digest_ignores_mtime_and_mode_but_not_bytes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"one\n")
    first = file_digest(str(f))
    os.utime(f, (1, 1))
    os.chmod(f, 0o600)
    assert file_digest(str(f)) == first  # metadata only: same
    f.write_bytes(b"one\n")
    assert file_digest(str(f)) == first  # same bytes rewritten: same
    f.write_bytes(b"two\n")
    assert file_digest(str(f)) != first


def test_editor_digest_absent_and_unreadable(tmp_path):
    assert file_digest(str(tmp_path / "missing")) == ABSENT
    assert file_digest(str(tmp_path)) is None  # a directory: no digest (fail closed)
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    assert file_digest(str(link)) is None  # never through a symlink


def test_editor_digest_is_bounded(tmp_path, monkeypatch):
    f = tmp_path / "big"
    f.write_bytes(b"x" * 100)
    monkeypatch.setattr(pg, "DIGEST_MAX_BYTES", 99)
    assert file_digest(str(f)) is None


# --- the fingerprint ---------------------------------------------------------------------------


def _fp(tracker, files=(), checks=()):
    return tracker.fingerprint(tracker.identity(files), checks)


def test_the_fingerprint_is_keyed_per_grant():
    a, b = ProgressTracker(), ProgressTracker()
    files = [("x.md", b"d" * 32)]
    assert _fp(a, files) == _fp(a, files)
    assert _fp(a, files) != _fp(b, files)
    assert "key" not in repr(a) and a._key.hex() not in repr(a)


def test_the_fingerprint_reads_bytes_paths_and_checks():
    t = ProgressTracker()
    checks = [normalize_checks(_checks())]
    base = _fp(t, [("x.md", b"1" * 32)], checks)
    assert _fp(t, [("x.md", b"2" * 32)], checks) != base
    assert _fp(t, [("y.md", b"1" * 32)], checks) != base
    assert _fp(t, [("x.md", b"1" * 32)], [normalize_checks(_checks(failed=5))]) != base
    assert _fp(t, [("x.md", b"1" * 32)], [normalize_checks(_checks(duration=7))]) == base
    assert _fp(t, [("x.md", pg.ABSENT)], checks) != base  # a missing file is a state too


def test_the_checks_are_a_set():
    t = ProgressTracker()
    one, two = normalize_checks(_checks()), normalize_checks(_checks(failed=5))
    assert _fp(t, [], [one]) == _fp(t, [], [one, one, one])  # multiplicity
    assert _fp(t, [], [one, two]) == _fp(t, [], [two, one])  # order of the calls
    assert _fp(t, [], [one, two]) != _fp(t, [], [one])


def test_the_rows_of_one_result_are_sorted():
    a = _checks()
    b = _checks(kind="ruff")
    both = {**a, "checks": [a["checks"][0], b["checks"][0]]}
    flipped = {**a, "checks": [b["checks"][0], a["checks"][0]]}
    assert normalize_checks(both) == normalize_checks(flipped)


def test_a_check_is_bound_to_the_files_it_ran_on():
    t = ProgressTracker()
    check = [normalize_checks(_checks())]
    first = _fp(t, [("a.py", b"1" * 32)], check)
    assert _fp(t, [("a.py", b"2" * 32)], check) != first
    assert _fp(t, [("a.py", b"1" * 32)], check) == first


def test_the_tracked_files_are_the_written_ones_most_recent_last():
    t = ProgressTracker()
    t.adopt(["/w/a", "/w/b"], {})
    t.adopt(["/w/a"], {})
    assert t.paths() == ["/w/b", "/w/a"]
    assert t.retained(["/w/c"]) == ["/w/b", "/w/a", "/w/c"]
    assert t.paths() == ["/w/b", "/w/a"]  # retained() is pure


def test_the_tracked_files_are_capped_without_a_counter():
    t = ProgressTracker()
    t.adopt([f"/w/{i}" for i in range(pg.FILES_KEPT + 3)], {})
    assert len(t.files) == pg.FILES_KEPT == 256
    assert t.paths()[0] == "/w/3"  # the oldest left silently
    assert not hasattr(t, "overflow") and not hasattr(t, "lost")


def test_the_tracker_repr_holds_no_path():
    t = ProgressTracker()
    t.adopt(["/w/pathmarker-a1b2c3d4e5f6"], {})
    assert "pathmarker" not in repr(t)


# --- the fresh read --------------------------------------------------------------------------


def test_read_files_reads_the_current_bytes_and_marks_a_missing_file(tmp_path):
    a, gone = tmp_path / "a", tmp_path / "gone"
    a.write_bytes(b"one")
    os.utime(a, ns=(10**18, 10**18))  # settled: its digest is cached
    digests, cache = pg.read_files([str(a), str(gone)], {})
    assert str(a) in cache
    assert digests[str(gone)] == pg.ABSENT and str(gone) not in cache
    assert digests[str(a)] == pg.file_digest(str(a))
    a.write_bytes(b"two")  # a change: the cache never answers for it
    again, _ = pg.read_files([str(a)], cache)
    assert again[str(a)] != digests[str(a)]


def test_read_files_reuses_the_digest_only_for_an_unchanged_stat(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.write_bytes(b"one")
    os.utime(a, ns=(10**18, 10**18))  # settled (older than CACHE_SETTLE_SEC)
    monkeypatch.setattr(pg, "_now", lambda: 10**9 + 100.0)
    _, cache = pg.read_files([str(a)], {})
    assert cache
    calls = []
    real = pg.file_digest
    monkeypatch.setattr(pg, "file_digest", lambda p: calls.append(p) or real(p))
    pg.read_files([str(a)], cache)
    assert calls == []  # unchanged stat: the cached digest
    os.utime(a, ns=(10**18 - 5, 10**18 - 5))
    pg.read_files([str(a)], cache)
    assert calls == [str(a)]  # moved: read again


def test_read_files_is_unknown_when_anything_is_unreadable(tmp_path):
    a, d = tmp_path / "a", tmp_path / "d"
    a.write_bytes(b"x")
    d.mkdir()
    assert pg.read_files([str(a), str(d)], {}) is None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert pg.read_files([str(fifo)], {}) is None


def test_the_fingerprint_is_independent_of_the_write_order():
    t = ProgressTracker()
    one = [("a", b"1" * 32), ("b", b"2" * 32)]
    assert _fp(t, one) == _fp(t, list(reversed(one)))


def test_the_fingerprint_holds_no_path_or_content():
    t = ProgressTracker()
    marker = "pathmarker-7d1e9b33c0a54f21"
    fp = _fp(t, [(marker, marker.encode())], [])
    assert len(fp) == 32 and marker.encode() not in fp


# --- the rules ---------------------------------------------------------------------------------


def _record(t, fp, end="done", report=("", ""), streak=1):
    return t.record(end, report, fp, streak)


def test_new_then_repeat_then_none():
    t = ProgressTracker()
    a = _fp(t, [("a", b"1" * 32)])
    assert _record(t, a) == ("new", "")
    assert t.classify(a) == "repeat"
    assert _record(t, None) == ("none", "")


@pytest.mark.parametrize(
    "pattern",
    ["AA", "ABAB", "ABCA", "ABCDEFA"],
)
def test_a_recurrence_within_the_window_is_a_loop(pattern):
    t = ProgressTracker()
    fps = {c: _fp(t, [(c, b"0" * 32)]) for c in set(pattern)}
    rules = [_record(t, fps[c])[1] for c in pattern]
    assert rules[-1] == "loop_detected"
    assert set(rules[:-1]) <= {""} or pattern == "ABAB"


def test_a_recurrence_seven_back_is_a_repeat_but_no_loop():
    t = ProgressTracker()
    pattern = "ABCDEFGA"  # A again after 6 others: 7 back
    assert len(pattern) - 1 == LOOP_WINDOW + 1
    fps = {c: _fp(t, [(c, b"0" * 32)]) for c in set(pattern)}
    out = [_record(t, fps[c]) for c in pattern]
    assert out[-1] == ("repeat", "")


def test_none_iterations_take_window_places():
    t = ProgressTracker()
    a = _fp(t, [("a", b"0" * 32)])
    _record(t, a)
    for _ in range(LOOP_WINDOW - 1):
        _record(t, None)
    assert _record(t, a) == ("repeat", "loop_detected")  # 6 back: inside
    t2 = ProgressTracker()
    a2 = _fp(t2, [("a", b"0" * 32)])
    _record(t2, a2)
    for _ in range(LOOP_WINDOW):
        _record(t2, None)
    assert _record(t2, a2) == ("repeat", "")  # 7 back: outside


def test_the_ring_keeps_the_last_sixteen():
    t = ProgressTracker()
    fps = [_fp(t, [(str(i), b"0" * 32)]) for i in range(17)]
    for fp in fps:
        _record(t, fp)
    assert len(t.ring) == RING_SIZE == 16
    assert t.classify(fps[0]) == "new"  # fell out
    assert t.classify(fps[1]) == "repeat"


def test_same_blocker_on_the_third_in_a_row():
    t = ProgressTracker()
    rep = ("blocked", "needs_decision")
    for n in range(1, SAME_BLOCKER_LIMIT):
        assert t.blocker_code("needs_decision") == "needs_decision"
        _record(t, None, "reported_blocked", rep)
        assert t.blocker_streak == n
    assert t.blocker_code("needs_decision") == "same_blocker"
    assert t.blocker_code("missing_input") == "missing_input"


def test_the_same_blocker_streak_resets_on_another_end_or_code():
    t = ProgressTracker()
    rep = ("blocked", "needs_decision")
    _record(t, None, "reported_blocked", rep)
    _record(t, None, "reported_blocked", rep)
    _record(t, None, "done")
    assert t.blocker_streak == 0 and t.blocker_code("needs_decision") == "needs_decision"
    _record(t, None, "reported_blocked", rep)
    _record(t, None, "reported_blocked", ("blocked", "missing_input"))
    assert t.blocker_code("needs_decision") == "needs_decision"
    assert (t.blocker, t.blocker_streak) == ("missing_input", 1)


def test_no_action_twice_in_a_row_blocks_no_progress():
    t = ProgressTracker()
    assert _record(t, None, "no_action", streak=1) == ("none", "")
    assert _record(t, None, "no_action", streak=2) == ("none", "no_progress")


def test_no_action_needs_two_since_the_last_resume():
    t = ProgressTracker()
    _record(t, None, "no_action", streak=1)
    _record(t, None, "no_action", streak=2)
    # a person resumed (the grant's streak is 0 again): one no-action is not enough
    assert _record(t, None, "no_action", streak=1) == ("none", "")
    assert _record(t, None, "no_action", streak=2)[1] == "no_progress"


def test_no_action_streak_is_broken_by_another_end():
    t = ProgressTracker()
    _record(t, None, "no_action", streak=1)
    _record(t, None, "done", streak=2)
    assert _record(t, None, "no_action", streak=3) == ("none", "")
    assert NO_ACTION_LIMIT == 2


def test_trackers_are_bounded():
    trackers = Trackers()
    first = trackers.get("g0")
    for i in range(1, pg.TRACKERS_KEPT + 3):
        trackers.get(f"g{i}")
    assert len(trackers) == pg.TRACKERS_KEPT
    assert trackers.get("g0") is not first


def test_trackers_evict_the_least_recently_used():
    trackers = Trackers()
    first = trackers.get("g0")
    for i in range(1, pg.TRACKERS_KEPT):
        trackers.get(f"g{i}")
    assert trackers.get("g0") is first  # used again: now the most recent
    trackers.get("new")
    assert trackers.find("g0") is first and trackers.find("g1") is None


def test_read_files_is_unknown_when_a_digest_fails(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.write_bytes(b"x")
    monkeypatch.setattr(pg, "file_digest", lambda p: None)  # a torn read, a permission error
    assert pg.read_files([str(a)], {}) is None
