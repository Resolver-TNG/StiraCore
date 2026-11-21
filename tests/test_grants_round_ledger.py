"""The round ledger's allow-list, chain and file rules (ADR 0028 §5; #757). Offline."""

from __future__ import annotations

import copy
import json
import os
import stat

import pytest

from stiracore.grants import round_ledger as rl
from stiracore.grants.round_ledger import LedgerSchemaError, RoundLedger

GRANT = "ab" * 16
# Canaries: what must never reach the ledger (a goal, model text, a path, a URL, a secret,
# check output).
CANARIES = (
    "tidy the notes goalmarker-9f3c2a7e5b1d4c60aa",
    "I have finished the task, here is my summary.",
    "/home/user/project/secret_plan.md",
    "notes.md",
    "https://example.invalid/hook?token=abc",
    "sk-test-0123456789abcdef0123456789abcdef",
    "FAILED tests/test_x.py::test_y - AssertionError",
    "Done",
    "a b",
    "x" * 41,
    "",
)


def round_record(**changes) -> dict:
    record = {
        "k": "round",
        "grant": GRANT,
        "round": 1,
        "of_max": 10,
        "origin": "press",
        "start": 1_700_000_000,
        "end": 1_700_000_060,
        "wall_ms": 60_000,
        "end_code": "done",
        "detail": "none",
        "progress": "new",
        "model_calls": 2,
        "writes": 1,
        "approval_waits": 0,
        "reports": 1,
        "tools": {**dict.fromkeys(rl.TOOL_KEYS, 0), "editor": 1, "goal_report": 1},
        "files_changed": 1,
        "tokens_in": 14,
        "tokens_out": 6,
        "cache_read": None,
        "cache_write": None,
        "largest_input": 7,
        "summaries": None,
        "errors": [],
        "limits": {
            "iteration_wall_sec": 600,
            "ttl_left_s": 7000,
            "iterations_left": 9,
            "tokens_left": 9000,
            "ceiling_min": 360,
            "out_cap": None,
            "out_cap_source": "unknown",
            "ctx_mode": "unknown",
        },
        "stop": None,
        "axis": {"start": "not_run", "end": "not_run"},
    }
    record.update(changes)
    return record


def legacy_round(**changes) -> dict:
    """A ``round`` row as it was written before the ``axis`` key (#767): no ``axis``,
    the eight ``tools`` keys of that time."""
    record = round_record(**changes)
    del record["axis"]
    if "tools" not in changes:
        record["tools"] = {**dict.fromkeys(rl._LEGACY_TOOL_KEYS, 0), "editor": 1, "goal_report": 1}
    return record


def write_stored(tmp_path, records, name=None) -> None:
    """Put ``records`` on disk as an older writer left them: a valid chain from seq 1,
    without the current writer's allow-list (the rows are what the test says they are)."""
    prev, body = "0" * 64, b""
    for seq, fields in enumerate(records, 1):
        line = {"v": 1, "seq": seq, "prev": prev, **fields}
        prev = line["hash"] = rl._digest(line)
        body += (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    path = tmp_path / (name or rl.LEDGER_FILE)
    path.write_bytes(body)
    os.chmod(path, 0o600)


def approval_record(**changes) -> dict:
    record = {
        "k": "approval",
        "grant": GRANT,
        "round": 1,
        "origin": "press",
        "ts": 1_700_000_030,
        "tool": "editor",
        "args_digest": "0123456789ab",
        "target_class": "inside_subtree",
        "consulted": "none",
        "decision": "approved",
        "approved_by": "person",
        "approver_id": None,
        "reason": "person_card",
        "latency_ms": 1200,
        "approver_tokens_in": None,
        "approver_tokens_out": None,
        "budget_left": 9000,
        "iteration_approvals": 1,
    }
    record.update(changes)
    return record


def _raw(tmp_path) -> bytes:
    path = tmp_path / rl.LEDGER_FILE
    return path.read_bytes() if path.exists() else b""


def _slots(record: dict):
    """Every place a value sits: top-level keys, nested dict keys, list items."""
    for key, value in record.items():
        yield (key,)
        if type(value) is dict:
            for inner in value:
                yield (key, inner)
        if type(value) is list:
            yield (key, 0)


def _put(record: dict, slot: tuple, value) -> dict:
    out = copy.deepcopy(record)
    if len(slot) == 1:
        out[slot[0]] = value
    elif type(out[slot[0]]) is list:
        out[slot[0]] = [value]
    else:
        out[slot[0]][slot[1]] = value
    return out


# --- the allow-list ---------------------------------------------------------------------


def test_a_valid_round_and_approval_are_written_in_one_chain(tmp_path):
    ledger = RoundLedger(tmp_path)
    first = ledger.append(round_record())
    second = ledger.append(approval_record())
    third = ledger.append(round_record(round=2, origin="tick", progress="repeat"))
    assert [r["seq"] for r in (first, second, third)] == [1, 2, 3]
    assert "prev" not in first and "hash" not in first
    lines = [json.loads(text) for text in _raw(tmp_path).decode().splitlines()]
    assert [line["k"] for line in lines] == ["round", "approval", "round"]
    assert lines[0]["prev"] == "0" * 64
    assert lines[1]["prev"] == lines[0]["hash"] and lines[2]["prev"] == lines[1]["hash"]
    assert ledger.unhealthy_reason is None
    assert [r["k"] for r in ledger.records(GRANT)] == ["round", "approval", "round"]
    assert len(ledger.records(GRANT, kind="round")) == 2
    assert ledger.records("cd" * 16) == []


@pytest.mark.parametrize("make", [round_record, approval_record])
def test_an_unknown_or_missing_key_is_refused_and_nothing_is_written(tmp_path, make):
    ledger = RoundLedger(tmp_path)
    for bad in (
        {**make(), "note": "ok"},
        {**make(), "goal": "ok"},
        {k: v for k, v in make().items() if k != "grant"},
        {**make(), "seq": 1},  # the writer's own keys cannot be supplied
        {**make(), "hash": "0" * 64},
        {**make(), "k": "other"},
        [],
        None,
    ):
        with pytest.raises(LedgerSchemaError):
            ledger.append(bad)
    assert _raw(tmp_path) == b"" and ledger.records() == []
    assert ledger.refused == 8
    assert ledger.unhealthy_reason == "round_ledger_record_refused"  # the error is visible


@pytest.mark.parametrize("make", [round_record, approval_record])
def test_no_canary_text_can_enter_any_field(tmp_path, make):
    """Property: whatever slot a free text is put into (a value, a nested value, a list
    item, a new key), the record is refused and the file never holds the text."""
    ledger = RoundLedger(tmp_path)
    ledger.append(make())
    before = _raw(tmp_path)
    for canary in CANARIES:
        for slot in _slots(make()):
            with pytest.raises(LedgerSchemaError):
                ledger.append(_put(make(), slot, canary))
            with pytest.raises(LedgerSchemaError):
                ledger.append(_put(make(), slot, {"text": canary}))
            with pytest.raises(LedgerSchemaError):
                ledger.append(_put(make(), slot, [canary, canary]))
        with pytest.raises(LedgerSchemaError):
            ledger.append({**make(), canary: 1})
    assert _raw(tmp_path) == before
    for canary in CANARIES:
        if canary:
            assert canary.encode() not in _raw(tmp_path)
            assert canary not in json.dumps(ledger.records())


def test_out_of_enum_strings_and_wrong_types_are_refused(tmp_path):
    ledger = RoundLedger(tmp_path)
    bad = [
        round_record(end_code="finished"),
        round_record(detail=""),
        round_record(progress="some"),
        round_record(origin="cron"),
        round_record(stop="because"),
        round_record(errors=["oops"]),
        round_record(errors=["model_error"] * 9),
        round_record(grant="AB" * 16),
        round_record(grant="grant-1"),
        round_record(round=0),
        round_record(round=True),
        round_record(model_calls=1.0),
        round_record(model_calls=-1),
        round_record(files_changed=257),
        round_record(tokens_in="14"),
        round_record(start=1_700_000_000.5),
        round_record(tools={**dict.fromkeys(rl.TOOL_KEYS, 0), "shell": 1}),
        round_record(tools={"editor": 1}),
        round_record(limits={**round_record()["limits"], "ctx_mode": "full"}),
        round_record(limits={**round_record()["limits"], "extra": 1}),
        approval_record(approved_by="main"),  # a model approver is not built
        approval_record(approved_by="reviewer"),
        approval_record(consulted="main"),
        approval_record(approver_id="rev_1"),
        approval_record(decision="approved", approved_by="none"),
        approval_record(decision="refused", approved_by="person"),
        approval_record(reason="because I said so"),
        approval_record(tool="shell"),
        approval_record(tool="goal_report"),
        approval_record(args_digest="0123456789abcdef"),
        approval_record(target_class="/etc"),
    ]
    for record in bad:
        with pytest.raises(LedgerSchemaError):
            ledger.append(record)
    assert _raw(tmp_path) == b""
    ledger.append(approval_record(decision="refused", approved_by="none", reason="timeout"))
    ledger.append(round_record(stop="no_progress", errors=["model_error"], end_code="other"))


def test_the_codes_are_the_executors():
    from stiracore.grants import executor, schema, state

    assert rl.END_CODES == executor.END_CODES | {"other"}
    axis_details = rl.AXIS_CODES - {"pass", "not_run"}
    assert rl.DETAIL_CODES == (executor.DETAIL_CODES - {""}) | {"none", "other"}
    assert axis_details <= executor.DETAIL_CODES  # S5: the detail of page_tools_unavailable
    assert set(rl._LEGACY_TOOL_KEYS) == (schema.TOOLS - schema.PAGE_TOOLS) | {executor.REPORT_TOOL}
    assert rl.TOOL_KEYS == (*rl._LEGACY_TOOL_KEYS, "page_look", "page_review", "view_image")
    # S5 (#784): the executor produces the end code and a grant scope can name page_look
    # and the narrowed view_image; page_review is not nameable (S7), and a page tool is
    # not an approval tool (in a grant no card answers one)
    assert "page_tools_unavailable" in executor.END_CODES
    assert schema.PAGE_TOOLS == {"page_look", "view_image"} and schema.PAGE_TOOLS < schema.TOOLS
    assert "page_review" not in schema.TOOLS
    assert not {"page_look", "page_review", "view_image"} & rl.APPROVAL_TOOLS
    assert state.GOAL_REASONS["blocked"] <= rl.STOP_CODES
    assert {"person", "none"} == rl.APPROVED_BY


# --- the file ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_file_is_0600_and_an_unsafe_file_is_not_used(tmp_path):
    ledger = RoundLedger(tmp_path)
    ledger.append(round_record())
    assert stat.S_IMODE(os.stat(tmp_path / rl.LEDGER_FILE).st_mode) == 0o600
    os.chmod(tmp_path / rl.LEDGER_FILE, 0o644)
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_unsafe"
    with pytest.raises(OSError):
        ledger.append(round_record(round=2))  # the writer refuses the changed file too
    assert ledger.unhealthy_reason == "round_ledger_replaced"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_symlink_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    os.chmod(target, 0o600)
    os.symlink(target, tmp_path / rl.LEDGER_FILE)
    ledger = RoundLedger(tmp_path)
    assert ledger.unhealthy_reason == "round_ledger_unsafe"
    with pytest.raises(OSError):
        ledger.append(round_record())
    assert target.read_bytes() == b""


def _three(tmp_path) -> list[bytes]:
    ledger = RoundLedger(tmp_path)
    for n in (1, 2, 3):
        ledger.append(round_record(round=n))
    return _raw(tmp_path).splitlines(keepends=True)


def _rewrite(tmp_path, body: bytes) -> None:
    (tmp_path / rl.LEDGER_FILE).write_bytes(body)


def test_a_reopened_ledger_continues_the_chain(tmp_path):
    _three(tmp_path)
    ledger = RoundLedger(tmp_path)
    assert ledger.unhealthy_reason is None
    assert ledger.append(round_record(round=4))["seq"] == 4
    assert [r["round"] for r in RoundLedger(tmp_path).records()] == [1, 2, 3, 4]


def test_tampering_is_detected_on_read(tmp_path):
    lines = _three(tmp_path)
    edited = lines[1].replace(b'"model_calls":2', b'"model_calls":9')
    assert edited != lines[1]
    _rewrite(tmp_path, lines[0] + edited + lines[2])
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_hash_mismatch"
    _rewrite(tmp_path, lines[0] + lines[2])  # a removed line
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_sequence_gap"
    _rewrite(tmp_path, lines[1] + lines[0] + lines[2])  # reordered
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_sequence_gap"
    _rewrite(tmp_path, lines[1] + lines[2])  # the head cut off
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_sequence_gap"
    _rewrite(tmp_path, lines[0] + b"not json\n")
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_malformed"
    bad = RoundLedger(tmp_path)
    assert bad.records() == []  # an unusable ledger shows nothing
    with pytest.raises(OSError):
        bad.append(round_record())


def test_a_line_with_a_valid_hash_but_a_field_outside_the_allow_list_is_malformed(tmp_path):
    lines = _three(tmp_path)
    item = json.loads(lines[2])
    item["note"] = "free text"
    item["hash"] = rl._digest(item)
    forged = (json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _rewrite(tmp_path, lines[0] + lines[1] + forged)
    ledger = RoundLedger(tmp_path)
    assert ledger.unhealthy_reason == "round_ledger_malformed"
    assert "free text" not in json.dumps(ledger.records())


def test_truncation_is_detected(tmp_path):
    lines = _three(tmp_path)
    _rewrite(tmp_path, lines[0] + lines[1] + lines[2][:-20])  # a torn last line
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_torn"


def test_the_running_writer_detects_whole_lines_cut_from_the_end(tmp_path):
    ledger = RoundLedger(tmp_path)
    for n in (1, 2, 3):
        ledger.append(round_record(round=n))
    lines = _raw(tmp_path).splitlines(keepends=True)
    with open(tmp_path / rl.LEDGER_FILE, "r+b") as handle:
        handle.truncate(len(lines[0]) + len(lines[1]))
    assert ledger.verify() == "round_ledger_replaced"
    with pytest.raises(OSError):
        ledger.append(round_record(round=4))
    other = RoundLedger(tmp_path)  # a fresh writer: its next append sees the size too
    assert other.unhealthy_reason is None


def test_segments_are_bounded_and_the_chain_continues_across_the_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 2000)
    ledger = RoundLedger(tmp_path)
    for n in range(1, 13):
        ledger.append(round_record(round=n))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [rl.OLD_LEDGER_FILE, rl.LEDGER_FILE]  # one rotated segment, no more
    for name in names:
        assert (tmp_path / name).stat().st_size <= 2000
    assert ledger.verify() is None
    kept = [r["seq"] for r in ledger.records()]
    assert kept == list(range(kept[0], 13)) and kept[0] > 1  # the oldest segment is gone
    reopened = RoundLedger(tmp_path)
    assert reopened.unhealthy_reason is None
    assert [r["seq"] for r in reopened.records()] == kept
    assert reopened.append(round_record(round=13))["seq"] == 13
    # the two segments are one chain: a current segment that does not continue the older
    old = (tmp_path / rl.OLD_LEDGER_FILE).read_bytes().splitlines(keepends=True)
    (tmp_path / rl.OLD_LEDGER_FILE).write_bytes(b"".join(old[:-1]))
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_sequence_gap"


def test_an_offline_reader_does_not_rotate_or_write(tmp_path, monkeypatch):
    _three(tmp_path)
    before = _raw(tmp_path)
    monkeypatch.setattr(rl, "ROTATE_BYTES", 10)
    reader = RoundLedger(tmp_path, rotate=False)
    assert len(reader.records()) == 3
    assert _raw(tmp_path) == before and not (tmp_path / rl.OLD_LEDGER_FILE).exists()


def test_a_failed_write_is_sticky_and_raised(tmp_path, monkeypatch):
    ledger = RoundLedger(tmp_path)
    ledger.append(round_record())

    def fail(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr(rl.private_file, "append_line", fail)
    with pytest.raises(OSError):
        ledger.append(round_record(round=2))
    monkeypatch.undo()
    assert ledger.unhealthy_reason == "round_ledger_write_failed"
    with pytest.raises(OSError):
        ledger.append(round_record(round=2))  # sticky: nothing more is written
    assert len(_raw(tmp_path).splitlines()) == 1
    assert ledger.unhealthy_reason in rl.UNHEALTHY_REASONS


def test_a_memory_only_ledger_writes_no_file(tmp_path):
    ledger = RoundLedger(None)
    ledger.append(round_record())
    assert len(ledger.records()) == 1 and list(tmp_path.iterdir()) == []


def test_the_summary_is_numbers_only():
    records = [
        rl.public({"v": 1, "seq": n, **round_record(round=n, tokens_in=tokens)})
        for n, tokens in ((1, 10), (2, None), (3, 5))
    ]
    records[1]["progress"] = "none"
    out = rl.summary(records)
    assert out == {"rounds": 3, "hours": 0.05, "new": 2, "tokens_in": 15, "tokens_out": 18}
    assert rl.summary([])["tokens_in"] is None


# --- the axis key and the page tool counts (ADR 0015 rev 6, slice S2; #767) --------------

AXIS_CODES = {
    "pass",
    "not_run",
    "axis_stale",
    "axis_unreadable",
    "guard_pending",
    "browser_guard_unverified",
    "browser_guard_unmeasured",
    "platform_unmeasured",
    "rules_locked",
    "rules_changed",
    "record_unhealthy",
    "ledger_unhealthy",
    "write_floor_changed",
    "reviewer_changed",
    "reviewer_remote",
    "file_read_not_always",
    "not_in_scope",
    "breaker_not_normal",
    "other",
}


def test_axis_key_allow_listed(tmp_path):
    assert rl.AXIS_CODES == AXIS_CODES
    assert "axis" in rl._ROUND_KEYS and "axis" not in rl._APPROVAL_KEYS
    ledger = RoundLedger(tmp_path)
    stored = ledger.append(round_record())
    assert stored["axis"] == {"start": "not_run", "end": "not_run"}
    for n, code in enumerate(sorted(AXIS_CODES), 2):
        axis = {"start": code, "end": "pass"}
        assert ledger.append(round_record(round=n, axis=axis))["axis"] == axis
    lines = [json.loads(text) for text in _raw(tmp_path).decode().splitlines()]
    assert all(set(line["axis"]) == {"start", "end"} for line in lines)
    assert ledger.unhealthy_reason is None and RoundLedger(tmp_path).unhealthy_reason is None
    # the new end code and its details are members of the lists; nothing produces them
    ledger.append(round_record(round=40, end_code="page_tools_unavailable", detail="axis_stale"))


def test_every_cap_code_can_be_written_as_an_axis_value(tmp_path):
    """#778: the enum has every axis code; ``page_look``'s four per-turn cap codes are
    written ``other`` through the explicit table, anything else ``axis_unreadable``."""
    from stiracore.tools import page_check_guard as pg
    from stiracore.tools import page_look as pl

    assert set(pg.AXIS_CODES) <= rl.AXIS_CODES
    assert rl.AXIS_CODES_AS_OTHER == set(pl.CAP_CODES) - rl.AXIS_CODES
    assert rl.AXIS_CODES_AS_OTHER == {
        "origin_not_covered",
        "repeated_failure",
        "call_limit",
        "cap_unreadable",
    }
    ledger = RoundLedger(tmp_path)
    for n, code in enumerate(pl.CAP_CODES, 1):
        value = rl.axis_code(code)
        assert value == (code if code in rl.AXIS_CODES else "other")
        axis = {"start": value, "end": value}
        assert ledger.append(round_record(round=n, axis=axis))["axis"] == axis
    assert ledger.unhealthy_reason is None and RoundLedger(tmp_path).unhealthy_reason is None
    assert rl.axis_code("pass") == "pass"
    for odd in (None, 7, True, "", "Pass", "/etc/hosts", ["pass"], "passed"):
        assert rl.axis_code(odd) == "axis_unreadable"


def test_axis_value_outside_enum_refused(tmp_path):
    ledger = RoundLedger(tmp_path)
    bad = [
        {"start": "passed", "end": "not_run"},
        {"start": "not_run", "end": "PASS"},
        {"start": "not_run", "end": ""},
        {"start": "none", "end": "not_run"},  # a detail code, not an axis code
        {"start": None, "end": "not_run"},
        {"start": True, "end": "not_run"},
        {"start": 0, "end": "not_run"},
        {"start": ["pass"], "end": "not_run"},
        {"start": "https://example.invalid/x", "end": "not_run"},
        "not_run",
        None,
        ["not_run", "not_run"],
    ]
    for axis in bad:
        with pytest.raises(LedgerSchemaError):
            ledger.append(round_record(axis=axis))
    assert _raw(tmp_path) == b"" and ledger.records() == []
    assert ledger.refused == len(bad)
    assert ledger.unhealthy_reason == "round_ledger_record_refused"


def test_axis_extra_key_refused(tmp_path):
    ledger = RoundLedger(tmp_path)
    ok = {"start": "not_run", "end": "not_run"}
    without_axis = {k: v for k, v in round_record().items() if k != "axis"}
    bad = [
        round_record(axis={**ok, "items": 1}),
        round_record(axis={**ok, "measured_at": 12}),
        round_record(axis={**ok, "note": "pass"}),
        round_record(axis={"start": "not_run"}),  # a missing key
        round_record(axis={"end": "not_run"}),
        round_record(axis={}),
        without_axis,  # the writer requires the key
        legacy_round(),  # the old shape is for reading only
        approval_record(axis=ok),  # not a key of the approval kind
    ]
    for record in bad:
        with pytest.raises(LedgerSchemaError):
            ledger.append(record)
    assert _raw(tmp_path) == b"" and ledger.refused == len(bad)


def test_tool_keys_include_page_tools(tmp_path):
    assert rl.TOOL_KEYS[-3:] == ("page_look", "page_review", "view_image")
    ledger = RoundLedger(tmp_path)
    tools = {**dict.fromkeys(rl.TOOL_KEYS, 0), "page_look": 2, "page_review": 1, "view_image": 3}
    stored = ledger.append(round_record(tools=tools))
    assert stored["tools"] == tools
    before = _raw(tmp_path)
    for bad in (
        {k: v for k, v in tools.items() if k != "page_look"},  # a missing count
        {**tools, "page_check": 1},  # not a key
        {**tools, "page_look": True},
        {**tools, "view_image": "3"},
        {**tools, "page_review": -1},
        dict.fromkeys(rl._LEGACY_TOOL_KEYS, 0),  # the old keys with an axis: refused
    ):
        with pytest.raises(LedgerSchemaError):
            ledger.append(round_record(tools=bad))
    assert _raw(tmp_path) == before


def test_old_rows_without_axis_still_verify(tmp_path):
    old = [legacy_round(round=1), approval_record(), legacy_round(round=2, origin="tick")]
    write_stored(tmp_path, old)
    before = _raw(tmp_path)
    ledger = RoundLedger(tmp_path)
    assert ledger.unhealthy_reason is None and ledger.verify() is None
    shown = ledger.records(GRANT, kind="round")
    assert [r["round"] for r in shown] == [1, 2]
    assert all("axis" not in r and set(r["tools"]) == set(rl._LEGACY_TOOL_KEYS) for r in shown)
    assert rl.summary(ledger.records())["rounds"] == 2
    # the next round continues the same chain; the old lines are not rewritten
    assert ledger.append(round_record(round=3))["seq"] == 4
    raw = _raw(tmp_path)
    assert raw.startswith(before) and ledger.verify() is None
    lines = [json.loads(text) for text in raw.decode().splitlines()]
    assert lines[3]["prev"] == lines[2]["hash"] and "axis" in lines[3]
    reopened = RoundLedger(tmp_path)
    assert reopened.unhealthy_reason is None
    assert ["axis" in r for r in reopened.records(kind="round")] == [False, False, True]
    assert RoundLedger(tmp_path, rotate=False).unhealthy_reason is None  # the CLI's reader
    from stiracore.control import grant_rounds_cli

    assert grant_rounds_cli.main(["--data-dir", str(tmp_path)]) == 0  # old and new rows


def test_old_shape_is_only_the_old_shape(tmp_path):
    """The reader accepts exactly the row of before ``axis`` — not a row that merely
    lacks the key, and not an old-shaped row after a row with ``axis``."""
    eleven = dict.fromkeys(rl.TOOL_KEYS, 0)
    eight = dict.fromkeys(rl._LEGACY_TOOL_KEYS, 0)
    for stored in (
        [legacy_round(tools=eleven)],  # no axis, the new tool keys
        [round_record(tools=eight)],  # an axis, the old tool keys
        [{**legacy_round(), "note": 1}],
        [legacy_round(end_code="finished")],
        [round_record(axis={"start": "passed", "end": "not_run"})],
        [round_record(axis={"start": "pass", "end": "pass", "items": 1})],
        [round_record(round=1), legacy_round(round=2)],  # old after new
    ):
        write_stored(tmp_path, stored)
        ledger = RoundLedger(tmp_path)
        assert ledger.unhealthy_reason == "round_ledger_malformed"
        assert ledger.records() == []
    # the same order across the rotation: the older segment already has an axis row
    write_stored(tmp_path, [round_record(round=1)], rl.OLD_LEDGER_FILE)
    last = json.loads((tmp_path / rl.OLD_LEDGER_FILE).read_bytes())
    line = {"v": 1, "seq": 2, "prev": last["hash"], **legacy_round(round=2)}
    line["hash"] = rl._digest(line)
    _rewrite(tmp_path, (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.chmod(tmp_path / rl.LEDGER_FILE, 0o600)
    assert RoundLedger(tmp_path).unhealthy_reason == "round_ledger_malformed"


def test_rotation_keeps_chain_with_axis_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "ROTATE_BYTES", 2400)
    write_stored(tmp_path, [legacy_round(round=n) for n in (1, 2, 3)])
    old_bytes = _raw(tmp_path)
    ledger = RoundLedger(tmp_path)
    assert ledger.unhealthy_reason is None
    ledger.append(round_record(round=4))  # would pass the bound: the old rows rotate first
    assert (tmp_path / rl.OLD_LEDGER_FILE).read_bytes() == old_bytes  # moved, not rewritten
    for n in range(5, 12):
        ledger.append(round_record(round=n, axis={"start": "pass", "end": "axis_stale"}))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [rl.OLD_LEDGER_FILE, rl.LEDGER_FILE]
    assert ledger.verify() is None
    kept = [r["seq"] for r in ledger.records()]
    assert kept == list(range(kept[0], 12))
    reopened = RoundLedger(tmp_path)
    assert reopened.unhealthy_reason is None and reopened.verify() is None
    assert [r["seq"] for r in reopened.records()] == kept
    assert all("axis" in r for r in reopened.records())  # the old rows left with their segment
    assert reopened.append(round_record(round=12))["seq"] == 12


def test_rotation_keeps_chain_from_old_rows_to_axis_rows(tmp_path, monkeypatch):
    """One segment of old rows, the next of rows with ``axis``: one chain, both read."""
    write_stored(tmp_path, [legacy_round(round=n) for n in (1, 2, 3)])
    monkeypatch.setattr(rl, "ROTATE_BYTES", len(_raw(tmp_path)) + 10)
    ledger = RoundLedger(tmp_path)
    ledger.append(round_record(round=4))
    ledger.append(round_record(round=5))
    assert (tmp_path / rl.OLD_LEDGER_FILE).exists() and ledger.verify() is None
    reopened = RoundLedger(tmp_path)
    assert reopened.unhealthy_reason is None
    assert [r["seq"] for r in reopened.records()] == [1, 2, 3, 4, 5]
    assert ["axis" in r for r in reopened.records()] == [False, False, False, True, True]
    first_new = json.loads(_raw(tmp_path).splitlines()[0])
    last_old = json.loads((tmp_path / rl.OLD_LEDGER_FILE).read_bytes().splitlines()[-1])
    assert first_new["prev"] == last_old["hash"]
