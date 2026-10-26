"""#171: hash-chained observability EventStore, migration, tampering, secrets (ADR 0009).

Every test is offline with real SQLite files. Each tampering test edits the files the
way an attacker with filesystem access would (plain sqlite3 / file writes) and checks
the store refuses to extend the chain afterwards.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from stiracore.observability.event_chain import (
    ChainState,
    EventChainError,
    genesis_hash,
    read_head,
    row_hash,
    write_head,
)
from stiracore.observability.event_store import (
    _HASHED_COLUMNS,
    RECOVERED_EVENT_TYPE,
    SQLiteEventStore,
)
from stiracore.observability.hooks import ObservabilityEvent

SECRET = "sk-" + "Zq9Xw8Vu7Ts6Rp5O"  # split so the literal never appears whole in source


def _event(n: int, **extra) -> ObservabilityEvent:
    return ObservabilityEvent(
        event_id=f"ev-{n}",
        event_type="tool_end",
        turn_id="turn-1",
        tool_name="memory_read",
        duration_ms=n,
        attributes={"n": n},
        **extra,
    )


def _store(tmp_path, count: int = 0, **kwargs) -> SQLiteEventStore:
    store = SQLiteEventStore(tmp_path / "events.db", **kwargs)
    for n in range(1, count + 1):
        store.emit(_event(n))
    return store


def _sql(tmp_path, statement: str, *params) -> None:
    conn = sqlite3.connect(tmp_path / "events.db")
    with conn:
        conn.execute(statement, params)
    conn.close()


def _reopen(tmp_path, **kwargs) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.db", **kwargs)


def _assert_refuses_writes(store: SQLiteEventStore) -> None:
    assert not store.healthy
    assert store.verify_chain()[0] is False
    with pytest.raises(EventChainError):
        store.emit(_event(99))
    with pytest.raises(EventChainError):
        store.clear()


def _legacy_db(path, rows) -> None:
    """The pre-#171 schema and INSERT, as main wrote them."""
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL, turn_id TEXT, duration_ms REAL,
                model_id TEXT, tool_name TEXT, tool_use_id TEXT, status TEXT,
                usage_json TEXT, message TEXT, attributes_json TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO events (event_id, timestamp, event_type, turn_id, duration_ms,"
            " model_id, tool_name, tool_use_id, status, usage_json, message, attributes_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.close()


def _legacy_row(n: int, attributes: dict | None = None, message: str | None = None):
    return (
        f"old-{n}",
        f"2026-10-01T00:00:0{n}+00:00",
        "turn_end",
        "turn-0",
        12,  # an INTEGER into a REAL column: hashed as stored (12.0)
        None,
        None,
        None,
        "completed",
        "null",
        message,
        json.dumps(attributes or {"n": n}),
    )


# --- basic chain ------------------------------------------------------------------------


def test_events_form_a_chain_anchored_by_an_owner_only_head(tmp_path):
    store = _store(tmp_path, 3)
    assert store.healthy and store.verify_chain() == (True, None)
    rows = sqlite3.connect(tmp_path / "events.db").execute(
        "SELECT seq, prev_hash, hash FROM events ORDER BY seq"
    )
    prev = genesis_hash("observability")
    for seq, prev_hash, digest in rows:
        assert prev_hash == prev
        prev = digest
    head_path = tmp_path / "events.db.head"
    assert stat.S_IMODE(os.stat(head_path).st_mode) == 0o600
    previous, latest = read_head(head_path, "observability")
    assert (latest.seq, latest.hash) == (3, prev)
    assert previous.seq == 2
    assert store.chain_status() == {
        "ok": True,
        "error": None,
        "namespace": "observability",
        "seq": 3,
        "anchor_seq": 0,
        "migrated_through_seq": "0",
        "recovered": False,
        "anchored": True,
        "purge_pending": False,
    }
    # Reads keep their pre-#171 shape.
    assert [event.event_id for event in store.get_events()] == ["ev-1", "ev-2", "ev-3"]
    assert store.get_events()[0].duration_ms == 1.0
    store.close()
    assert _reopen(tmp_path).verify_chain() == (True, None)


def test_hash_covers_the_exact_stored_values(tmp_path):
    store = _store(tmp_path, 1)
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["hash"] == row_hash(row["prev_hash"], [row[c] for c in _HASHED_COLUMNS])
    store.close()


def test_duplicate_event_id_is_idempotent_and_does_not_grow_the_chain(tmp_path):
    store = _store(tmp_path, 2)
    store.emit(_event(2))
    assert store.chain_status()["seq"] == 2
    assert store.verify_chain() == (True, None)


def test_in_memory_store_chains_without_a_head(tmp_path):
    store = SQLiteEventStore(":memory:")
    store.emit(_event(1))
    store.emit(_event(2))
    assert store.head_path is None
    assert store.verify_chain() == (True, None)
    assert store.chain_status()["anchored"] is False


def test_two_instances_on_one_file_extend_one_chain(tmp_path):
    first = _store(tmp_path, 1)
    second = _reopen(tmp_path)
    second.emit(_event(2))
    first.emit(_event(3))
    second.emit(_event(4))
    assert first.verify_chain() == (True, None)
    assert second.verify_chain() == (True, None)
    assert [e.event_id for e in first.get_events()] == ["ev-1", "ev-2", "ev-3", "ev-4"]


def test_namespace_is_bound_into_genesis_and_head(tmp_path):
    _store(tmp_path, 1).close()
    other = _reopen(tmp_path, namespace="approval")
    assert other.error == "chain metadata belongs to another namespace"
    _assert_refuses_writes(other)
    with pytest.raises(ValueError):
        SQLiteEventStore(tmp_path / "x.db", namespace="Bad Name")
    assert genesis_hash("observability") != genesis_hash("approval")


# --- migration --------------------------------------------------------------------------


def test_legacy_database_is_migrated_with_its_start_point_recorded(tmp_path):
    path = tmp_path / "events.db"
    _legacy_db(path, [_legacy_row(1), _legacy_row(2)])
    store = SQLiteEventStore(path)
    assert store.healthy, store.error
    meta = dict(sqlite3.connect(path).execute("SELECT key, value FROM event_chain"))
    assert meta["migrated_through_seq"] == "2"
    assert meta["migrated_rows"] == "2"
    assert meta["masked_rows"] == "0"
    assert meta["anchor_seq"] == "0"
    assert meta["anchor_hash"] == genesis_hash("observability")
    # Legacy rows stay readable and are covered from now on.
    assert [e.event_id for e in store.get_events()] == ["old-1", "old-2"]
    assert store.get_events()[0].duration_ms == 12.0
    store.emit(_event(3))
    assert store.verify_chain() == (True, None)
    store.close()
    reopened = SQLiteEventStore(path)
    assert reopened.verify_chain() == (True, None)
    assert reopened.chain_status()["migrated_through_seq"] == "2"
    # A legacy row changed after the migration is detected.
    reopened.close()
    _sql(tmp_path, "UPDATE events SET status = 'failed' WHERE seq = 1")
    assert _reopen(tmp_path).error == "event 1 was modified"


def test_legacy_secrets_are_redacted_before_they_are_hashed(tmp_path):
    path = tmp_path / "events.db"
    _legacy_db(
        path,
        [_legacy_row(1, attributes={"api_key": SECRET}), _legacy_row(2, message=f"x {SECRET}")],
    )
    store = SQLiteEventStore(path)
    assert store.healthy, store.error
    assert store.chain_status()["migrated_through_seq"] == "2"
    meta = dict(sqlite3.connect(path).execute("SELECT key, value FROM event_chain"))
    assert meta["masked_rows"] == "2"
    first, second = store.get_events()
    assert first.attributes == {"api_key": "***"}
    assert second.message == "x sk-***"
    store.close()
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_downgraded_writer_rows_without_hash_are_detected(tmp_path):
    _store(tmp_path, 1).close()
    _sql(
        tmp_path,
        "INSERT INTO events (event_id, timestamp, event_type) VALUES ('old-writer', 't', 'x')",
    )
    store = _reopen(tmp_path)
    assert store.error == "chain broken at seq 2 (a row was removed or moved)"
    _assert_refuses_writes(store)


def test_dropping_the_chain_does_not_restart_it(tmp_path):
    """Remove metadata and columns: the head (built after the migration) still binds."""
    _store(tmp_path, 2).close()
    _sql(tmp_path, "DROP TABLE event_chain")
    _sql(tmp_path, "ALTER TABLE events DROP COLUMN hash")
    _sql(tmp_path, "ALTER TABLE events DROP COLUMN prev_hash")
    _sql(tmp_path, "UPDATE events SET status = 'forged'")
    store = _reopen(tmp_path)
    assert store.error == "head anchor exists but the database has no chain"
    _assert_refuses_writes(store)


def test_metadata_dropped_but_columns_kept_is_tampering(tmp_path):
    _store(tmp_path, 1).close()
    _sql(tmp_path, "DROP TABLE event_chain")
    store = _reopen(tmp_path)
    assert store.error == "chain columns exist without chain metadata (tampering)"


def test_crash_inside_the_migration_is_redone_only_for_identical_rows(tmp_path):
    rows = [_legacy_row(1), _legacy_row(2)]
    done, crashed, changed = (tmp_path / name for name in ("done", "crashed", "changed"))
    for directory in (done, crashed, changed):
        directory.mkdir()
        _legacy_db(directory / "events.db", rows)
    SQLiteEventStore(done / "events.db").close()
    head = (done / "events.db.head").read_bytes()
    # Head written, COMMIT lost: the same rows produce exactly the head's state.
    (crashed / "events.db.head").write_bytes(head)
    assert SQLiteEventStore(crashed / "events.db").healthy
    # Different rows under that head: not a crash, tampering.
    _sql(changed, "UPDATE events SET status = 'forged' WHERE seq = 2")
    (changed / "events.db.head").write_bytes(head)
    store = SQLiteEventStore(changed / "events.db")
    assert store.error == "head anchor exists but the database has no chain"


# --- tampering --------------------------------------------------------------------------


def test_modified_row_is_detected(tmp_path):
    _store(tmp_path, 3).close()
    _sql(tmp_path, "UPDATE events SET attributes_json = '{\"n\":20}' WHERE seq = 2")
    store = _reopen(tmp_path)
    assert store.error == "event 2 was modified"
    _assert_refuses_writes(store)


def test_modified_row_with_a_recomputed_hash_breaks_the_next_link(tmp_path):
    _store(tmp_path, 3).close()
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM events WHERE seq = 2").fetchone())
    row["status"] = "forged"
    forged = row_hash(row["prev_hash"], [row[c] for c in _HASHED_COLUMNS])
    with conn:
        conn.execute("UPDATE events SET status = 'forged', hash = ? WHERE seq = 2", (forged,))
    conn.close()
    assert _reopen(tmp_path).error == "chain broken at seq 3 (a row was removed or moved)"


def test_deleted_middle_row_is_detected(tmp_path):
    _store(tmp_path, 3).close()
    _sql(tmp_path, "DELETE FROM events WHERE seq = 2")
    store = _reopen(tmp_path)
    assert store.error == "chain broken at seq 3 (a row was removed or moved)"
    _assert_refuses_writes(store)


def test_reordered_rows_are_detected(tmp_path):
    _store(tmp_path, 3).close()
    conn = sqlite3.connect(tmp_path / "events.db")
    with conn:
        conn.execute("UPDATE events SET seq = -2 WHERE seq = 2")
        conn.execute("UPDATE events SET seq = 2 WHERE seq = 3")
        conn.execute("UPDATE events SET seq = 3 WHERE seq = -2")
    conn.close()
    store = _reopen(tmp_path)
    assert store.error == "chain broken at seq 2 (a row was removed or moved)"
    _assert_refuses_writes(store)


def test_deleting_the_last_row_is_recorded_never_silently_accepted(tmp_path):
    _store(tmp_path, 3).close()
    _sql(tmp_path, "DELETE FROM events WHERE seq = 3")
    store = _reopen(tmp_path)
    # Indistinguishable from a crash between head and COMMIT: recorded on the chain.
    assert store.healthy, store.error
    assert store.recovery["head_seq"] == 3
    assert store.recovery["reason"] == "interrupted write or deleted tail"
    recovered = store.get_events(event_type=RECOVERED_EVENT_TYPE)
    assert len(recovered) == 1 and recovered[0].attributes["head_seq"] == 3
    assert store.verify_chain() == (True, None)
    assert store.chain_status()["recovered"] is True
    store.close()
    # Deleting the recovery record too is again "the last transaction is missing":
    # a new record appears, so the gap can never be made to look clean.
    _sql(tmp_path, f"DELETE FROM events WHERE event_type = '{RECOVERED_EVENT_TYPE}'")
    again = _reopen(tmp_path)
    assert again.healthy and again.recovery is not None
    assert len(again.get_events(event_type=RECOVERED_EVENT_TYPE)) == 1
    again.close()
    # Going further back than the last transaction is tampering.
    _sql(tmp_path, f"DELETE FROM events WHERE event_type = '{RECOVERED_EVENT_TYPE}' OR seq = 2")
    assert _reopen(tmp_path).error == "head anchor does not match the chain"


def test_deleting_more_than_the_last_transaction_is_tampering(tmp_path):
    _store(tmp_path, 3).close()
    _sql(tmp_path, "DELETE FROM events WHERE seq >= 2")
    store = _reopen(tmp_path)
    assert store.error == "head anchor does not match the chain"
    _assert_refuses_writes(store)


def test_deleting_every_row_without_clear_is_tampering(tmp_path):
    _store(tmp_path, 3).close()
    _sql(tmp_path, "DELETE FROM events")
    assert _reopen(tmp_path).error == "head anchor does not match the chain"


def test_forged_row_appended_with_a_valid_hash_is_detected(tmp_path):
    _store(tmp_path, 2).close()
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.row_factory = sqlite3.Row
    tail = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO events (seq, event_id, timestamp, event_type, prev_hash)"
            " VALUES (3, 'forged', 't', 'audit.approval.decided', ?)",
            (tail,),
        )
        row = conn.execute("SELECT * FROM events WHERE seq = 3").fetchone()
        digest = row_hash(tail, [row[c] for c in _HASHED_COLUMNS])
        conn.execute("UPDATE events SET hash = ? WHERE seq = 3", (digest,))
    conn.close()
    store = _reopen(tmp_path)
    assert store.error == "head anchor does not match the chain"
    _assert_refuses_writes(store)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p.unlink(), "head anchor is missing"),
        (lambda p: p.write_text("not json"), "head anchor is malformed"),
        (
            lambda p: p.write_text(p.read_text().replace('"v": 1', '"v": 2')),
            "head anchor is malformed",
        ),
        (
            lambda p: p.write_text(p.read_text().replace('"observability"', '"approval"')),
            "head anchor belongs to another namespace",
        ),
    ],
)
def test_missing_or_malformed_head_is_tampering(tmp_path, edit, message):
    _store(tmp_path, 2).close()
    edit(tmp_path / "events.db.head")
    store = _reopen(tmp_path)
    assert store.error == message
    _assert_refuses_writes(store)


def test_an_older_head_is_tampering(tmp_path):
    store = _store(tmp_path, 1)
    older = (tmp_path / "events.db.head").read_bytes()
    store.emit(_event(2))
    store.emit(_event(3))
    store.close()
    (tmp_path / "events.db.head").write_bytes(older)
    assert _reopen(tmp_path).error == "head anchor does not match the chain"


# --- tampering while running ------------------------------------------------------------


def test_tail_changed_by_another_connection_stops_the_next_write(tmp_path):
    store = _store(tmp_path, 2)
    _sql(tmp_path, "UPDATE events SET status = 'forged' WHERE seq = 2")
    with pytest.raises(EventChainError, match="event 2 was modified"):
        store.emit(_event(3))
    _assert_refuses_writes(store)
    # Putting the row back does not re-enable this instance.
    _sql(tmp_path, "UPDATE events SET status = NULL WHERE seq = 2")
    with pytest.raises(EventChainError):
        store.emit(_event(4))


def test_tail_deleted_while_running_stops_the_next_write(tmp_path):
    store = _store(tmp_path, 2)
    _sql(tmp_path, "DELETE FROM events WHERE seq = 2")
    with pytest.raises(EventChainError, match="event 2 was deleted"):
        store.emit(_event(3))


def test_head_removed_while_running_stops_the_next_write(tmp_path):
    store = _store(tmp_path, 2)
    (tmp_path / "events.db.head").unlink()
    with pytest.raises(EventChainError, match="head anchor is missing"):
        store.emit(_event(3))


def test_older_row_rewritten_while_running_is_caught_by_verify_chain(tmp_path):
    store = _store(tmp_path, 3)
    _sql(tmp_path, "UPDATE events SET status = 'forged' WHERE seq = 1")
    assert store.verify_chain() == (False, "event 1 was modified")
    _assert_refuses_writes(store)


# --- interrupted writes -----------------------------------------------------------------


def test_crash_between_head_and_commit_is_recovered_on_the_next_open(tmp_path):
    store = _store(tmp_path, 2)
    original = store._write_head_locked

    def head_then_crash(prev, nxt):
        original(prev, nxt)
        raise OSError("simulated power loss before COMMIT")

    store._write_head_locked = head_then_crash
    with pytest.raises(EventChainError, match="after the head was anchored"):
        store.emit(_event(3))
    _assert_refuses_writes(store)  # this instance never builds on an unknown tail
    store.close()
    reopened = _reopen(tmp_path)
    assert reopened.healthy, reopened.error
    assert reopened.recovery["head_seq"] == 3
    assert [e.event_id for e in reopened.get_events()][:2] == ["ev-1", "ev-2"]
    assert reopened.get_events()[-1].event_type == RECOVERED_EVENT_TYPE
    reopened.emit(_event(4))
    assert reopened.verify_chain() == (True, None)


def test_failed_head_write_leaves_nothing_committed(tmp_path):
    store = _store(tmp_path, 1)

    def fail(prev, nxt):
        raise OSError("disk full")

    store._write_head_locked = fail
    with pytest.raises(EventChainError, match="after the head was anchored"):
        store.emit(_event(2))
    store.close()
    reopened = _reopen(tmp_path)
    # The head was not replaced, the row was rolled back: consistent, no recovery.
    assert reopened.healthy and reopened.recovery is None
    assert [e.event_id for e in reopened.get_events()] == ["ev-1"]


# --- clear (retention) ------------------------------------------------------------------


def test_clear_moves_the_anchor_and_the_chain_continues(tmp_path):
    store = _store(tmp_path, 3)
    tail = store.chain_status()["seq"]
    store.clear()
    assert store.get_events() == []
    store.emit(_event(4))
    conn = sqlite3.connect(tmp_path / "events.db")
    seq, prev_hash = conn.execute("SELECT seq, prev_hash FROM events").fetchone()
    meta = dict(conn.execute("SELECT key, value FROM event_chain"))
    assert seq == tail + 1  # the internal sequence is not reset
    assert meta["anchor_seq"] == str(tail) and prev_hash == meta["anchor_hash"]
    assert store.verify_chain() == (True, None)
    store.close()
    assert _reopen(tmp_path).verify_chain() == (True, None)
    # Moving the anchor in the database alone (to hide a prefix) is tampering.
    _sql(tmp_path, "UPDATE event_chain SET value = '0' WHERE key = 'anchor_seq'")
    assert _reopen(tmp_path).error == "chain metadata does not start at this namespace's genesis"


def test_clear_by_another_instance_is_followed(tmp_path):
    first = _store(tmp_path, 2)
    second = _reopen(tmp_path)
    second.clear()
    first.emit(_event(3))
    assert first.verify_chain() == (True, None)
    assert [e.event_id for e in second.get_events()] == ["ev-3"]


# --- secrets ----------------------------------------------------------------------------


def test_secrets_never_reach_the_hashed_material_or_any_file(tmp_path):
    store = _store(tmp_path)
    store.emit(
        ObservabilityEvent(
            event_id="secret-1",
            event_type="tool_end",
            message=f"Authorization: Bearer {SECRET}",
            usage={"inputTokens": 3, "api_key": SECRET},
            attributes={"output": f"token={SECRET}", "nested": [{"password": SECRET}]},
        )
    )
    (event,) = store.get_events()
    assert SECRET not in json.dumps({"m": event.message, "u": event.usage, "a": event.attributes})
    assert event.usage == {"inputTokens": 3, "api_key": "***"}
    assert store.verify_chain() == (True, None)
    with pytest.raises(ValueError, match="secret"):
        store.emit(ObservabilityEvent(event_id=f"token={SECRET}", event_type="x"))
    store.close()
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_head_contains_only_hashes_and_counters(tmp_path):
    _store(tmp_path, 1).close()
    head = json.loads((tmp_path / "events.db.head").read_text())
    assert set(head) == {"v", "ns", "prev", "next"}
    assert set(head["next"]) == {"seq", "hash", "anchor_seq", "anchor_hash"}


def test_chain_state_validation():
    good = {"seq": 1, "hash": "a" * 64, "anchor_seq": 0, "anchor_hash": "b" * 64}
    assert ChainState.from_dict(good).seq == 1
    for bad in (
        {**good, "seq": True},
        {**good, "hash": "A" * 64},
        {**good, "seq": -1},
        {**good, "anchor_seq": 2},
        {**good, "extra": 1},
    ):
        with pytest.raises(ValueError):
            ChainState.from_dict(bad)


def test_write_head_round_trip(tmp_path):
    state = ChainState(2, "c" * 64, 0, "d" * 64)
    write_head(tmp_path / "h", "observability", None, state)
    assert read_head(tmp_path / "h", "observability") == (None, state)
    assert read_head(tmp_path / "missing", "observability") is None


# --- review round 1 (#268): sticky failures, schema, secrets, purge, busy, processes ----


def test_malformed_head_while_running_is_sticky_for_emit(tmp_path):
    store = _store(tmp_path, 2)
    head = tmp_path / "events.db.head"
    good = head.read_bytes()
    head.write_text("not json")
    with pytest.raises(EventChainError, match="head anchor is malformed"):
        store.emit(_event(3))
    assert not store.healthy and store.error == "head anchor is malformed"
    head.write_bytes(good)  # putting it back does not re-enable this instance
    _assert_refuses_writes(store)


def test_malformed_head_while_running_is_sticky_for_verify(tmp_path):
    store = _store(tmp_path, 2)
    head = tmp_path / "events.db.head"
    good = head.read_bytes()
    head.write_text("not json")
    assert store.verify_chain() == (False, "head anchor is malformed")
    head.write_bytes(good)
    assert store.verify_chain() == (False, "head anchor is malformed")
    _assert_refuses_writes(store)


def test_dropped_hash_column_is_unhealthy_not_an_exception(tmp_path):
    store = _store(tmp_path, 2)
    _sql(tmp_path, "ALTER TABLE events DROP COLUMN hash")
    with pytest.raises(EventChainError, match="schema"):
        store.emit(_event(3))
    _assert_refuses_writes(store)
    store.close()
    reopened = _reopen(tmp_path)  # no exception from the constructor
    assert reopened.error == "events table schema is not the chained schema"
    _assert_refuses_writes(reopened)


def test_dropped_hash_column_is_caught_by_verify_chain(tmp_path):
    store = _store(tmp_path, 2)
    _sql(tmp_path, "ALTER TABLE events DROP COLUMN prev_hash")
    assert store.verify_chain() == (False, "events table schema is not the chained schema")
    _assert_refuses_writes(store)


@pytest.mark.parametrize(
    "field", ["event_type", "status", "turn_id", "model_id", "tool_name", "tool_use_id"]
)
def test_secret_in_an_identifier_column_is_refused(tmp_path, field):
    store = _store(tmp_path)
    values = {"event_type": "tool_end", field: f"token={SECRET}"}
    with pytest.raises(ValueError, match="secret"):
        store.emit(ObservabilityEvent(event_id="e1", **values))
    assert store.get_events() == [] and store.healthy
    store.close()
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_legacy_secret_identifiers_are_replaced_before_hashing(tmp_path):
    path = tmp_path / "events.db"
    row = list(_legacy_row(1))
    row[0] = f"id-token={SECRET}"  # event_id
    row[2] = f"type password={SECRET}"  # event_type
    row[8] = f"Bearer {SECRET}"  # status
    _legacy_db(path, [tuple(row), _legacy_row(2)])
    store = SQLiteEventStore(path)
    assert store.healthy, store.error
    first, second = store.get_events()
    assert first.event_id == "redacted-1"
    assert SECRET not in first.event_type and SECRET not in first.status
    assert second.event_id == "old-2"
    assert store.chain_status()["purge_pending"] is False
    assert store.verify_chain() == (True, None)
    store.close()
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def _purge_marker(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM event_chain WHERE key = 'purge_pending'").fetchone()[
            0
        ]
    finally:
        conn.close()


def test_purge_blocked_by_a_reader_is_recorded_and_retried(tmp_path, caplog):
    path = tmp_path / "events.db"
    _legacy_db(path, [_legacy_row(1, attributes={"api_key": SECRET})])
    reader = sqlite3.connect(path)
    reader.execute("PRAGMA journal_mode = WAL")
    store = None
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()  # holds a snapshot
        store = SQLiteEventStore(path, timeout=0.2)
        assert store.healthy, store.error
        assert store.chain_status()["purge_pending"] is True
        assert _purge_marker(path) == "1"
        assert "checkpoint" in caplog.text
    finally:
        reader.rollback()
        reader.close()
    assert store.purge_secrets() is True
    assert store.chain_status()["purge_pending"] is False
    store.close()
    assert _purge_marker(path) == "0"
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_crash_after_migration_commit_redoes_the_purge(tmp_path, monkeypatch):
    path = tmp_path / "events.db"
    _legacy_db(path, [_legacy_row(1, attributes={"api_key": SECRET})])
    monkeypatch.setattr(SQLiteEventStore, "purge_secrets", lambda self: False)
    crashed = SQLiteEventStore(path)  # "crashes" right after COMMIT: no checkpoint
    assert _purge_marker(path) == "1"
    monkeypatch.undo()
    crashed._connection.close()  # no close(): that would checkpoint
    reopened = SQLiteEventStore(path)
    assert reopened.chain_status()["purge_pending"] is False
    reopened.close()
    assert _purge_marker(path) == "0"
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_busy_database_at_open_is_transient(tmp_path):
    _store(tmp_path, 1).close()
    locker = sqlite3.connect(tmp_path / "events.db", isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        store = _reopen(tmp_path, timeout=0.1)
        assert not store.healthy and store.error is None
        assert store.chain_status()["error"] is None
        with pytest.raises(EventChainError, match="busy"):
            store.emit(_event(2))
        assert store.error is None
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    store.emit(_event(2))  # retried on the next write
    assert store.healthy and store.verify_chain() == (True, None)


def test_busy_database_at_write_is_transient(tmp_path):
    store = _store(tmp_path, 1, timeout=0.1)
    locker = sqlite3.connect(tmp_path / "events.db", isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.emit(_event(2))
        assert store.healthy
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    store.emit(_event(2))
    assert store.verify_chain() == (True, None)


# --- real processes -------------------------------------------------------------------------

_WRITER = """
import os, sys
from pathlib import Path
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.observability.hooks import ObservabilityEvent
path, tag, count, mode = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
store = SQLiteEventStore(path)
if mode == "kill-before-commit":
    original = store._write_head_locked
    def head_then_die(prev, nxt):
        original(prev, nxt)
        os.kill(os.getpid(), 9)
    store._write_head_locked = head_then_die
for n in range(count):
    store.emit(ObservabilityEvent(event_id=f"{tag}-{n}", event_type="tool_end"))
    if mode == "loop" and n == 5:
        print("ready", flush=True)
store.close()
"""


def _writer(path, tag, count, mode="plain"):
    import subprocess
    import sys

    import stiracore

    env = {
        **os.environ,
        "PYTHONPATH": str(os.path.dirname(os.path.dirname(stiracore.__file__))),
    }
    return subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(path), tag, str(count), mode],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_writer_processes_build_one_valid_chain(tmp_path):
    path = tmp_path / "events.db"
    _store(tmp_path).close()
    procs = [_writer(path, f"p{i}", 40) for i in range(4)]
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    store = _reopen(tmp_path)
    assert store.healthy and store.recovery is None, store.error
    assert store.chain_status()["seq"] == 160
    assert store.verify_chain() == (True, None)


def test_kill_9_between_head_and_commit_is_recovered(tmp_path):
    path = tmp_path / "events.db"
    _store(tmp_path, 2).close()
    proc = _writer(path, "k", 1, mode="kill-before-commit")
    proc.communicate(timeout=60)
    assert proc.returncode == -9
    store = _reopen(tmp_path)
    assert store.healthy, store.error
    assert store.recovery is not None and store.recovery["head_seq"] == 3
    assert [e.event_id for e in store.get_events()][:2] == ["ev-1", "ev-2"]
    assert store.verify_chain() == (True, None)


def test_kill_9_at_an_arbitrary_point_never_leaves_an_unverifiable_chain(tmp_path):
    import signal

    path = tmp_path / "events.db"
    _store(tmp_path).close()
    proc = _writer(path, "r", 100000, mode="loop")
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGKILL)
    proc.communicate(timeout=60)
    store = _reopen(tmp_path)
    assert store.healthy, store.error  # consistent, or recovered (head one ahead)
    assert store.verify_chain() == (True, None)


# --- review round 2 (#268): initialization errors, deterministic redaction ----------------


def test_non_sqlite_file_opens_unhealthy_without_raising(tmp_path):
    path = tmp_path / "events.db"
    path.write_bytes(b"this is not a database" * 100)
    store = SQLiteEventStore(path)
    assert store.error == "database could not be initialized (DatabaseError)"
    assert store.chain_status()["ok"] is False
    with pytest.raises(EventChainError):
        store.emit(_event(1))
    assert store.verify_chain()[0] is False


def test_wal_switch_busy_at_construction_is_pending_and_retried(tmp_path):
    path = tmp_path / "events.db"
    _legacy_db(path, [_legacy_row(1)])  # DELETE journal mode
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        store = SQLiteEventStore(path, timeout=0.1)
        assert not store.healthy and store.error is None
        with pytest.raises(EventChainError, match="busy"):
            store.emit(_event(2))
        assert store.error is None
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    store.emit(_event(2))
    assert store.healthy and store.verify_chain() == (True, None)
    assert [e.event_id for e in store.get_events()] == ["old-1", "ev-2"]


def test_legacy_event_id_redaction_is_deterministic_and_secret_free(tmp_path):
    rows = []
    for n in (1, 2):
        row = list(_legacy_row(n))
        row[0] = f"id-token={SECRET}{n}"
        rows.append(tuple(row))
    runs = []
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        _legacy_db(directory / "events.db", rows)
        store = SQLiteEventStore(directory / "events.db")
        runs.append([e.event_id for e in store.get_events()])
        store.close()
    assert runs[0] == runs[1] == ["redacted-1", "redacted-2"]


def test_redacted_event_id_collision_gets_a_deterministic_suffix(tmp_path):
    path = tmp_path / "events.db"
    row = list(_legacy_row(1))
    row[0] = f"id-token={SECRET}"
    other = list(_legacy_row(2))
    other[0] = "redacted-1"  # an existing id that would collide
    _legacy_db(path, [tuple(row), tuple(other)])
    store = SQLiteEventStore(path)
    assert [e.event_id for e in store.get_events()] == ["redacted-1-1", "redacted-1"]
    assert store.verify_chain() == (True, None)


_MIGRATION_KILLER = """
import os, sys
from pathlib import Path
from stiracore.observability.event_store import SQLiteEventStore
original = SQLiteEventStore._write_head_locked
def head_then_die(self, prev, nxt):
    original(self, prev, nxt)
    os.kill(os.getpid(), 9)
SQLiteEventStore._write_head_locked = head_then_die
SQLiteEventStore(Path(sys.argv[1]))
"""


def test_kill_9_after_migration_head_before_commit_migrates_on_reopen(tmp_path):
    import subprocess
    import sys

    import stiracore

    path = tmp_path / "events.db"
    row = list(_legacy_row(1, attributes={"api_key": SECRET}))
    row[0] = f"id-token={SECRET}"
    _legacy_db(path, [tuple(row), _legacy_row(2)])
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(stiracore.__file__))}
    proc = subprocess.run(
        [sys.executable, "-c", _MIGRATION_KILLER, str(path)], env=env, timeout=60, check=False
    )
    assert proc.returncode == -9
    head = read_head(tmp_path / "events.db.head", "observability")
    assert head is not None and head[0] is None  # migration head, never committed
    store = SQLiteEventStore(path)
    assert store.healthy, store.error
    assert [e.event_id for e in store.get_events()] == ["redacted-1", "old-2"]
    assert store.chain_status()["purge_pending"] is False
    assert store.verify_chain() == (True, None)
    store.close()
    for file in tmp_path.iterdir():
        assert SECRET.encode() not in file.read_bytes(), file.name
