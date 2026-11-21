"""Follow-ups of the report files review (#761; ADR 0028 §4). Offline and scripted.

Two rules the first tests did not pin (they held, but only breaking the code showed it),
and the stricter shape of a report file name at issue.
"""

from __future__ import annotations

import os
import unicodedata

import pytest
from grants_exec_helpers import grant
from grants_helpers import request
from test_grants_report_files import (
    _env,
    _iterate,
    _parse,
    _replace,
    _tracker,
    _write,
    sub,  # noqa: F401 - the fixture
)

from stiracore.grants import state
from stiracore.grants.schema import GrantRefused, grant_from_dict, grant_to_dict
from stiracore.yolo.allowance import ClockReading

# --- rules that were only proven by breaking the code -------------------------------------------


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs a file its owner cannot read",
)
def test_a_report_file_unreadable_before_its_first_write_makes_the_round_unknown(tmp_path):
    env = _env(tmp_path, ["NOTES.md"])
    (env.sub / "NOTES.md").write_text("x\n")
    os.chmod(env.sub / "NOTES.md", 0)  # its bytes cannot be read before the write
    try:
        _iterate(env, _write(env, "NOTES.md", "y\n"))
    finally:
        os.chmod(env.sub / "NOTES.md", 0o644)
    tracker = _tracker(env)
    assert tracker is None or list(tracker.ring) == []  # unknown: no place in the history
    assert tracker is None or tracker.paths() == []
    assert grant(env).no_progress == 1  # ... and never progress


def test_a_round_cut_off_after_a_report_file_write_keeps_the_file_untracked(tmp_path):
    env = _env(tmp_path, ["NOTES.md"])
    (env.sub / "NOTES.md").write_text("round 0\n")

    def a_persons_turn_arrives(call):
        if call == 2:  # after the write of the first call
            env.busy = True

    env.provider.on_call = a_persons_turn_arrives
    view = _iterate(env, _replace(env, "NOTES.md", "round 0", "round 1"))
    env.busy, env.provider.on_call = False, None
    assert view["end"] == "yielded_user_turn"
    assert (env.sub / "NOTES.md").read_text() == "round 1\n"  # the write happened
    tracker = _tracker(env)
    assert tracker is None or tracker.paths() == []
    before = grant(env).no_progress
    # The next round writes only the report file: still no progress, still untracked.
    _iterate(env, _replace(env, "NOTES.md", "round 1", "round 2"))
    assert _tracker(env).paths() == []
    assert grant(env).no_progress == before + 1


# --- the shape of a name at issue ---------------------------------------------------------------

NFD_NAME = unicodedata.normalize("NFD", "caf\u00e9.md")
ODD_NAMES = [
    "\u202eNOTES.md",  # a bidi override
    "NO\u200bTES.md",  # a zero-width space (format character)
    "NOTES\u2066.md",  # a bidi isolate
    "NOTES.md\u0085",  # a C1 control (NEL)
    "NO\u009fTES.md",  # a C1 control
    "NOTES\u2028.md",  # a line separator
    "NOTES\u2029.md",  # a paragraph separator
    "doc\u2215x.md",  # a division slash
    "doc\u2044x.md",  # a fraction slash
    "doc\uff0fx.md",  # a fullwidth solidus
    "doc\uff3cx.md",  # a fullwidth backslash
    "doc\u2216x.md",  # a set minus
    "NOTES.md ",  # a trailing space
    "NOTES.md\u00a0",  # a trailing no-break space
    "NOTES.md.",  # a trailing dot
    "docs /x.md",  # ... in any component
    "docs./x.md",
    "...",
    NFD_NAME,  # not NFC
]


@pytest.mark.parametrize("name", ODD_NAMES, ids=[ascii(name) for name in ODD_NAMES])
def test_an_odd_name_is_refused_at_issue(sub, name):  # noqa: F811
    with pytest.raises(GrantRefused) as refused:
        _parse(sub, [name])
    assert refused.value.code == "grant_field_invalid"
    assert name not in str(refused.value) and name not in repr(refused.value.extra)


@pytest.mark.parametrize(
    "names",
    [
        ["NOTES.md", "notes.md"],
        ["docs/LOG.md", "Docs/log.MD"],
        ["stra\u00dfe.md", "STRASSE.md"],  # the case fold, not only lower()
    ],
)
def test_names_that_differ_only_by_case_are_duplicates_at_issue(sub, names):  # noqa: F811
    with pytest.raises(GrantRefused, match="^grant_field_invalid$"):
        _parse(sub, names)


def test_plain_names_are_accepted_as_before(sub):  # noqa: F811
    nfc = unicodedata.normalize("NFC", NFD_NAME)
    names = ["NOTES.md", "docs/new report.md", ".notes", "a.b/c d.txt", nfc, "\u30e1\u30e2.md"]
    assert _parse(sub, names).report_files == tuple(names)


def test_a_stored_list_from_before_the_stricter_check_still_loads():
    """The stricter shape is checked at issue only: a grant store written before it must
    not turn unreadable (``grant_store_malformed``) after an upgrade."""
    now = ClockReading(wall=1e9, boot=10.0, mono=10.0)
    plain = state.new_grant(request(), grant_id="b" * 32, issuer="t", now=now)
    stored = grant_to_dict(plain, "g")
    for old in (["NOTES.md "], ["NOTES.md", "notes.md"], [NFD_NAME], ["doc\u2215x.md"]):
        assert grant_from_dict({**stored, "report_files": old})[0].report_files == tuple(old)
