"""Deterministic A/B run guards (#729): fingerprint, plan check, leak check."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from stiracore.eval import ab_guards as g

HARNESS = {"agent/prompt.md": b"be brief\n", "agent/tools.py": b"TOOLS = []\n"}
INPUTS = {"task-001.json": b'{"q": 1}', "task-002.json": b'{"q": 2}'}


# --- fingerprint ---------------------------------------------------------------


def test_same_input_same_fingerprint_and_key_order_does_not_matter():
    fp = g.run_fingerprint(HARNESS, INPUTS)
    assert re.fullmatch(r"[0-9a-f]{64}", fp)
    assert g.run_fingerprint(dict(HARNESS), dict(INPUTS)) == fp
    assert g.run_fingerprint(dict(reversed(HARNESS.items())), dict(reversed(INPUTS.items()))) == fp


def test_fingerprint_is_a_fixed_value():
    # Pins the encoding: a change here invalidates recorded fingerprints.
    assert g.run_fingerprint({"a": b"1"}, {"b": b"2"}) == (
        "8e7820aaa8f02133509e3bc1b8e229cfdee967a7ce4ba96395a7215229db6abb"
    )


@pytest.mark.parametrize(
    ("harness", "inputs"),
    [
        ({**HARNESS, "agent/prompt.md": b"be brief\r"}, INPUTS),  # one byte in a harness file
        (HARNESS, {**INPUTS, "task-002.json": b'{"q": 3}'}),  # one byte in an input
        ({**HARNESS, "extra": b""}, INPUTS),  # an added (empty) file
        ({"agent/prompt.md": HARNESS["agent/prompt.md"]}, INPUTS),  # a removed file
        (
            {
                "agent/Prompt.md": HARNESS["agent/prompt.md"],
                "agent/tools.py": HARNESS["agent/tools.py"],
            },
            INPUTS,
        ),  # a renamed file
        (HARNESS, {}),
    ],
)
def test_any_difference_changes_the_fingerprint(harness, inputs):
    assert g.run_fingerprint(harness, inputs) != g.run_fingerprint(HARNESS, INPUTS)


def test_moving_a_file_between_harness_and_inputs_changes_the_fingerprint():
    assert g.run_fingerprint({"x": b"1"}, {}) != g.run_fingerprint({}, {"x": b"1"})


def test_names_cannot_imitate_another_file_set():
    # Names are length-prefixed, so a crafted name is not read as two records.
    one = g.run_fingerprint({"a": b"", "b": b""}, {})
    crafted = "a:" + g.fingerprint_detail({"a": b""}, {})["harness"]["a"] + "\n1:b"
    assert g.run_fingerprint({crafted: b""}, {}) != one


def test_names_are_opaque_no_path_normalisation():
    assert g.run_fingerprint({"a/b.py": b"x"}, {}) != g.run_fingerprint({"./a/b.py": b"x"}, {})


@pytest.mark.parametrize(
    ("harness", "inputs"),
    [
        ({"a": "text"}, {}),
        ({"a": bytearray(b"x")}, {}),
        ({1: b"x"}, {}),
        ({}, {"a": None}),
        ([("a", b"x")], {}),
        ({}, None),
    ],
)
def test_fingerprint_rejects_wrong_types(harness, inputs):
    with pytest.raises(TypeError):
        g.run_fingerprint(harness, inputs)


def test_detail_holds_digests_only():
    detail = g.fingerprint_detail(HARNESS, INPUTS)
    assert set(detail) == {"harness", "inputs"}
    assert set(detail["harness"]) == set(HARNESS)
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for s in detail.values() for d in s.values())


# --- declared change -----------------------------------------------------------


def _arms(change: dict[str, bytes], inputs_b=INPUTS):
    a = g.fingerprint_detail(HARNESS, INPUTS)
    b = g.fingerprint_detail({**HARNESS, **change}, inputs_b)
    return a, b


def test_declared_change_matches():
    a, b = _arms({"agent/prompt.md": b"be very brief\n"})
    assert g.diff_matches_declared_change(a, b, {"agent/prompt.md"}) == g.Result(True, ())
    assert g.diff_matches_declared_change(b, a, frozenset({"agent/prompt.md"})).ok


def test_added_file_counts_as_a_change():
    a, b = _arms({"agent/new.md": b"x"})
    assert g.diff_matches_declared_change(a, b, {"agent/new.md"}).ok


def test_undeclared_change_is_refused():
    a, b = _arms({"agent/prompt.md": b"p2", "agent/tools.py": b"t2"})
    r = g.diff_matches_declared_change(a, b, {"agent/prompt.md"})
    assert (r.ok, r.reasons) == (False, (g.E_CHANGE_NOT_DECLARED,))


def test_declared_but_unchanged_is_refused():
    a, b = _arms({})
    r = g.diff_matches_declared_change(a, b, {"agent/prompt.md"})
    assert (r.ok, r.reasons) == (False, (g.E_DECLARED_NOT_CHANGED,))


def test_wrong_file_declared_gives_both_codes():
    a, b = _arms({"agent/tools.py": b"t2"})
    r = g.diff_matches_declared_change(a, b, {"agent/prompt.md"})
    assert r.reasons == (g.E_CHANGE_NOT_DECLARED, g.E_DECLARED_NOT_CHANGED)


def test_different_inputs_are_refused():
    a, b = _arms({"agent/prompt.md": b"p2"}, inputs_b={**INPUTS, "task-001.json": b"{}"})
    r = g.diff_matches_declared_change(a, b, {"agent/prompt.md"})
    assert (r.ok, r.reasons) == (False, (g.E_INPUTS_DIFFER,))


def test_empty_declaration_is_refused_even_for_identical_arms():
    a, b = _arms({})
    r = g.diff_matches_declared_change(a, b, set())
    assert (r.ok, r.reasons) == (False, (g.E_DECLARED_EMPTY,))


@pytest.mark.parametrize("declared", [None, "agent/prompt.md", ["agent/prompt.md"], {1}])
def test_declared_wrong_type_is_refused(declared):
    a, b = _arms({"agent/prompt.md": b"p2"})
    r = g.diff_matches_declared_change(a, b, declared)
    assert (r.ok, r.reasons) == (False, (g.E_DECLARED_INVALID,))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "abc",
        {},
        {"harness": {}},
        {"harness": {}, "inputs": {}, "extra": {}},
        {"harness": {"a": "not-a-digest"}, "inputs": {}},
        {"harness": {"a": b"x"}, "inputs": {}},
        {"harness": [], "inputs": {}},
    ],
)
def test_malformed_detail_is_refused(bad):
    good = g.fingerprint_detail(HARNESS, INPUTS)
    for a, b in ((bad, good), (good, bad)):
        r = g.diff_matches_declared_change(a, b, {"agent/prompt.md"})
        assert (r.ok, r.reasons) == (False, (g.E_FINGERPRINT_INVALID,))


# --- plan ----------------------------------------------------------------------

PLAN = {"A": 2, "B": 2}


def test_plan_met():
    assert g.check_plan(PLAN, ["A/0", "B/0", "A/1", "B/1"]) == g.Result(True, ())
    assert g.check_plan(PLAN, (x for x in ["A/0", "B/0", "A/1", "B/1"])).ok


@pytest.mark.parametrize(
    "actual",
    [
        ["A/0", "A/1", "B/0"],  # one missing
        ["A/0", "A/1", "B/0", "B/1", "B/2"],  # one extra
        ["A/0", "A/1"],  # an arm with no run
        [],
    ],
)
def test_count_mismatch_is_refused(actual):
    r = g.check_plan(PLAN, actual)
    assert (r.ok, r.reasons) == (False, (g.E_COUNT_MISMATCH,))


def test_duplicate_label_is_refused_and_not_counted_twice():
    r = g.check_plan(PLAN, ["A/0", "A/0", "B/0", "B/1"])
    assert (r.ok, r.reasons) == (False, (g.E_LABEL_DUPLICATE, g.E_COUNT_MISMATCH))
    r = g.check_plan(PLAN, ["A/0", "A/1", "A/1", "B/0", "B/1"])
    assert (r.ok, r.reasons) == (False, (g.E_LABEL_DUPLICATE,))


def test_unplanned_arm_is_refused():
    r = g.check_plan(PLAN, ["A/0", "A/1", "B/0", "B/1", "C/0"])
    assert (r.ok, r.reasons) == (False, (g.E_ARM_UNPLANNED,))


@pytest.mark.parametrize("label", ["A", "/0", "A/", ""])
def test_malformed_label_is_refused(label):
    r = g.check_plan(PLAN, ["A/0", "A/1", "B/0", "B/1", label])
    assert (r.ok, r.reasons) == (False, (g.E_LABEL_INVALID,))


@pytest.mark.parametrize(
    "planned",
    [
        None,
        [("A", 1)],
        {"A": 0},
        {"A": -1},
        {"A": True},
        {"A": 1.0},
        {"A": "1"},
        {"": 1},
        {"A/x": 1},
        {1: 1},
    ],
)
def test_invalid_plan_is_refused(planned):
    r = g.check_plan(planned, ["A/0"])
    assert (r.ok, r.reasons) == (False, (g.E_PLAN_INVALID,))


def test_empty_plan_is_refused():
    r = g.check_plan({}, [])
    assert (r.ok, r.reasons) == (False, (g.E_PLAN_EMPTY,))


@pytest.mark.parametrize("actual", [None, "A/0", b"A/0", 3, ["A/0", 1], {"A/0": 1}])
def test_invalid_actual_is_refused(actual):
    r = g.check_plan({"A": 1}, actual)
    assert (r.ok, r.reasons) == (False, (g.E_ACTUAL_INVALID,))


# --- leak ----------------------------------------------------------------------

GLOBS = ["eval/heldout/*", "*.answers.json"]
TERMS = ["rubric", "expected answer"]


def test_clean_context_passes():
    parts = ["Fix the failing test in src/app.py.", "Tools: read_file, grep"]
    assert g.leak_check(parts, GLOBS, TERMS) == g.Result(True, ())
    assert g.leak_check(iter(parts), iter(GLOBS), iter(TERMS)).ok


@pytest.mark.parametrize(
    "part",
    [
        "Follow the RUBRIC closely.",
        "the expected\n   answer is 42",
        "the ｒｕｂｒｉｃ says",  # full-width, folded by NFKC
    ],
)
def test_banned_term_is_refused(part):
    r = g.leak_check(["hello", part], GLOBS, TERMS)
    assert (r.ok, r.reasons) == (False, (g.E_LEAK_BANNED_TERM,))


@pytest.mark.parametrize(
    "part",
    [
        "see eval/heldout/case-01.json",
        "open `eval/heldout/case-01.json`.",
        "path=/srv/repo/eval/heldout/deep/case.json",
        'files: ["src/a.py", "data/t1.answers.json"]',
        "C:\\repo\\eval\\heldout\\case.json",
        "(eval/heldout/x)",
    ],
)
def test_heldout_path_is_refused(part):
    r = g.leak_check([part], GLOBS, TERMS)
    assert (r.ok, r.reasons) == (False, (g.E_LEAK_HELDOUT_PATH,))


def test_both_leaks_give_both_codes_once():
    r = g.leak_check(["rubric at eval/heldout/a", "rubric again eval/heldout/b"], GLOBS, TERMS)
    assert r.reasons == (g.E_LEAK_BANNED_TERM, g.E_LEAK_HELDOUT_PATH)


def test_similar_path_outside_the_glob_passes():
    assert g.leak_check(["see eval/public/case.json and heldout.md"], GLOBS, TERMS).ok


@pytest.mark.parametrize(
    ("parts", "globs", "terms", "code"),
    [
        ([], GLOBS, TERMS, g.E_CONTEXT_EMPTY),
        (["x"], [], TERMS, g.E_HELDOUT_GLOBS_EMPTY),
        (["x"], GLOBS, [], g.E_BANNED_TERMS_EMPTY),
        (None, GLOBS, TERMS, g.E_CONTEXT_INVALID),
        ("one string", GLOBS, TERMS, g.E_CONTEXT_INVALID),
        ([b"bytes"], GLOBS, TERMS, g.E_CONTEXT_INVALID),
        (["x", None], GLOBS, TERMS, g.E_CONTEXT_INVALID),
        (["x"], "eval/heldout/*", TERMS, g.E_HELDOUT_GLOBS_INVALID),
        (["x"], None, TERMS, g.E_HELDOUT_GLOBS_INVALID),
        (["x"], ["eval/heldout/*", ""], TERMS, g.E_HELDOUT_GLOBS_INVALID),
        (["x"], [1], TERMS, g.E_HELDOUT_GLOBS_INVALID),
        (["x"], GLOBS, "rubric", g.E_BANNED_TERMS_INVALID),
        (["x"], GLOBS, None, g.E_BANNED_TERMS_INVALID),
        (["x"], GLOBS, ["rubric", "  "], g.E_BANNED_TERMS_INVALID),
        (["x"], GLOBS, {"rubric": 1}, g.E_BANNED_TERMS_INVALID),
    ],
)
def test_undecidable_configuration_is_refused(parts, globs, terms, code):
    r = g.leak_check(parts, globs, terms)
    assert (r.ok, r.reasons) == (False, (code,))


def test_undecidable_wins_over_a_clean_looking_context():
    r = g.leak_check([], [], [])
    assert r.reasons == (g.E_CONTEXT_EMPTY, g.E_HELDOUT_GLOBS_EMPTY, g.E_BANNED_TERMS_EMPTY)


# --- contract ------------------------------------------------------------------


def test_reasons_are_fixed_codes_without_free_text():
    secret_term = "zebra-rubric-term"
    results = [
        g.leak_check([f"{secret_term} in eval/heldout/secret-name.json"], GLOBS, [secret_term]),
        g.check_plan(PLAN, ["A/secret-run", "A/secret-run", "Z/1", "bad"]),
        g.diff_matches_declared_change(*_arms({"agent/tools.py": b"t2"}), {"agent/prompt.md"}),
        g.diff_matches_declared_change(None, None, None),
    ]
    for r in results:
        assert not r.ok and r.reasons
        assert set(r.reasons) <= g.REASON_CODES
        assert len(set(r.reasons)) == len(r.reasons)
    assert all(re.fullmatch(r"E_[A-Z_]+", code) for code in g.REASON_CODES)


def test_result_is_immutable():
    r = g.check_plan(PLAN, [])
    with pytest.raises(AttributeError):
        r.ok = True  # type: ignore[misc]


def test_module_is_pure_by_imports():
    # No clock, randomness, network, model, process or file I/O module is imported.
    tree = ast.parse(Path(g.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported == {
        "__future__",
        "hashlib",
        "re",
        "unicodedata",
        "collections",
        "dataclasses",
        "fnmatch",
    }
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "print", "input", "eval", "exec", "__import__"}
