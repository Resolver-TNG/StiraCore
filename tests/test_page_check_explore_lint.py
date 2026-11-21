"""#747: ``explore`` (a bounded, seeded walk) and ``design_lint`` (model-free layout checks).

Pure parts (the checks before launch, Stira's decision from the page's numbers, the logs)
run everywhere; the real-browser tests need Chrome / Chromium / Edge and are skipped without
one unless ``STIRA_REQUIRE_BROWSER=1`` (as ``tests/test_page_check_assertions.py``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from stiracore.tools import page_check as pc

BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)
FIXTURES = Path(__file__).parent / "fixtures" / "page_check"
# Text of the fixture pages: none of it may appear in the numbers Stira reports.
PAGE_WORDS = ("Tea Shop", "Brewing", "The End", "kettle", "debug", "1280x800", "Small print")


def _policy(ws, **extra):
    from stiracore.identity_paths import WritePathPolicy

    return WritePathPolicy(base_dir=ws, write_roots=[ws], **extra)


@pytest.fixture
def ws(tmp_path):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    for page in FIXTURES.glob("*.html"):
        shutil.copy(page, root / page.name)
    return root


def _only_numbers(value) -> bool:
    """Counts, ratios, booleans, ``None`` and Stira's own ids — no free text."""
    ids = {*pc.LINT_FLAGS, *(name for name, _p in pc.LEAK_PATTERNS)}
    if isinstance(value, dict):
        return all(isinstance(k, str) and _only_numbers(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_only_numbers(v) for v in value)
    if isinstance(value, str):
        return value in ids
    return value is None or isinstance(value, (bool, int, float))


# --- checked before any browser starts -------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        {"explore": True},
        {"explore": {"max_steps": 0}},
        {"explore": {"max_steps": pc.EXPLORE_MAX_STEPS + 1}},
        {"explore": {"max_steps": 1.5}},
        {"explore": {"max_steps": True}},
        {"explore": {"seed": -1}},
        {"explore": {"seed": "7"}},
        {"explore": {"seed": pc.EXPLORE_SEED_MAX + 1}},
        {"explore": {"reach_markers": "The End"}},
        {"explore": {"reach_markers": [""]}},
        {"explore": {"reach_markers": ["  "]}},
        {"explore": {"reach_markers": [3]}},
        {"explore": {"reach_markers": ["x" * (pc.MATCH_CHARS + 1)]}},
        {"explore": {"reach_markers": ["m"] * (pc.EXPLORE_MARKERS + 1)}},
        {"explore": {"selector": "#a"}},
        {"explore": {"script": "1"}},
        {"explore": {}, "hold_ms": 3},
        {"design_lint": False},
        {"design_lint": {"deny": "x"}},  # the patterns are Stira's own, not the caller's
        {"design_lint": "debug"},
    ],
)
def test_explore_and_lint_outside_their_shape_are_refused_before_launch(ws, monkeypatch, action):
    monkeypatch.setattr(pc, "_Browser", None)  # a launch would fail loudly
    result = pc.run_page_check(
        "lint_clean.html", _policy(ws), browser="/nonexistent", actions=[{"wait_ms": 1}, action]
    )
    assert (result["status"], result["reason"]) == ("refused", "invalid_action"), action
    assert result["message"].startswith("action 1:")


@pytest.mark.parametrize("kind", pc.ONCE_PER_CALL)
def test_explore_and_lint_run_at_most_once_per_call(kind):
    value = {} if kind == "explore" else True
    with pytest.raises(pc.PageCheckRefused) as refused:
        pc.check_actions([{kind: value}, {kind: value}])
    assert refused.value.code == "invalid_action"


def test_explore_defaults_and_limits():
    checked, over = pc.check_actions([{"explore": {}}, {"design_lint": True}, {"screenshot": True}])
    assert not over
    assert checked[0][:2] == ("explore", (pc.EXPLORE_DEFAULT_STEPS, 0, ()))
    assert checked[1][:2] == ("design_lint", True)
    assert (pc.EXPLORE_DEFAULT_STEPS, pc.EXPLORE_MAX_STEPS) == (40, 200)
    checked, _over = pc.check_actions(
        [{"explore": {"max_steps": 200, "seed": 7, "reach_markers": ["The End"]}}]
    )
    assert checked[0][1] == (200, 7, ("The End",))


# --- Stira's decision from the page's numbers --------------------------------------------


def _numbers(**changed):
    row = {
        "content": 4,
        "bottom": 0.05,
        "left": 0.0,
        "right": 0.0,
        "min_font": 16,
        "small": 0,
        "contrast": 0,
        "clipped": 0,
        "overlaps": 0,
        "leak_lines": 0,
        "leaks": [0] * len(pc.LEAK_PATTERNS),
        "capped": False,
    }
    row.update(changed)
    return list(row.values())


def test_lint_report_passes_clean_numbers():
    report = pc.lint_report(_numbers())
    assert report["ok"] is True and report["flags"] == []
    assert report["dev_strings"] == {"lines": 0, "patterns": {}}
    assert _only_numbers(report)


@pytest.mark.parametrize(
    ("changed", "flag"),
    [
        ({"bottom": 0.25}, "empty_bottom"),
        ({"left": 0.3, "right": 0.2}, "empty_sides"),
        ({"small": 1, "min_font": 8}, "small_text"),
        ({"contrast": 2}, "low_contrast"),
        ({"clipped": 1}, "overflow_clipped"),
        ({"overlaps": 3}, "overlap"),
        ({"leak_lines": 1, "leaks": [1, 1, 0, 0, 0, 0, 0]}, "dev_strings"),
    ],
)
def test_lint_report_flags_each_measure(changed, flag):
    report = pc.lint_report(_numbers(**changed))
    assert report["ok"] is False and report["flags"] == [flag]
    assert _only_numbers(report)


def test_lint_report_names_pattern_ids_not_text():
    report = pc.lint_report(_numbers(leak_lines=1, leaks=[1, 1, 0, 0, 0, 0, 0]))
    assert report["dev_strings"] == {"lines": 1, "patterns": {"dev_label": 1, "dimensions": 1}}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "debug: window 1280x800",
        _numbers()[:-1],
        _numbers(bottom=1.5),
        _numbers(bottom="0.3"),
        _numbers(small=-1),
        _numbers(small="debug"),
        _numbers(min_font="8px"),
        _numbers(leaks=[0]),
        _numbers(leaks=["debug"] * len(pc.LEAK_PATTERNS)),
        _numbers(capped="no"),
    ],
)
def test_lint_report_refuses_anything_but_numbers(value):
    assert pc.lint_report(value) is None


def test_explore_view_keeps_numbers_and_viewport_points_only():
    view = pc.explore_view([7, [1, 2], [True, False], [[10, 20], [5000, 5], ["a", 1]]], 2)
    assert view == (7, [1, 2], [True, False], [(10, 20)])
    for bad in (
        None,
        [7, [1], [True]],
        ["Tea Shop", [], [], []],
        [7, ["Tea Shop"], [], []],
        [7, [], ["yes"], []],
        [7, [], [True], []],  # one answer per marker
        [7, [], [], "points"],
    ):
        assert pc.explore_view(bad, 0) is None, bad


def test_leak_patterns_match_the_issues_examples():
    import re

    def ids(line):
        return [name for name, pattern in pc.LEAK_PATTERNS if re.search(pattern, line)]

    assert ids("debug: window 1280x800") == ["dev_label", "dimensions"]
    assert ids("  scene: counter") == ["dev_label"]
    assert ids("@900w:off") == ["breakpoint"]
    assert ids("TODO fix") == ["todo"]
    assert ids("score: undefined") == ["undefined"]
    assert ids("NaN points") == ["nan"]
    assert ids("item [object Object]") == ["object"]
    for clean in ("Tea Shop", "Pick a tea and serve the guest.", "A window seat", "Banana"):
        assert ids(clean) == [], clean


def test_the_new_functions_are_fixed_and_take_arguments_only():
    assert pc._FN_EXPLORE in pc.FIXED_FUNCTIONS and pc._FN_LINT in pc.FIXED_FUNCTIONS
    for fn in (pc._FN_EXPLORE, pc._FN_LINT):
        assert fn.startswith("function (")
        assert "eval(" not in fn and "Function(" not in fn and "fetch(" not in fn


# --- logs ------------------------------------------------------------------------------


def test_logs_keep_counts_and_ids_only():
    kept = pc.input_for_log(
        {
            "target": "game.html",
            "actions": [
                {"explore": {"seed": 3, "reach_markers": ["The secret End"]}},
                {"design_lint": True},
            ],
        }
    )
    assert kept["actions"] == {"count": 2, "kinds": {"explore": 1, "design_lint": 1}}
    assert "secret" not in json.dumps(kept)
    result = {
        "status": "ok",
        "explore": {
            "steps": 5,
            "clicks": 4,
            "keys": 1,
            "distinct_screens": 3,
            "unique_lines": 9,
            "errors": 0,
            "reached": [True],
            "reached_any": True,
            "note": "page text",
        },
        "design_lint": {
            "ok": False,
            "flags": ["small_text", "page text", "dev_strings"],
            "small_text": 1,
            "contrast_failures": "page text",
            "dev_strings": {"lines": 1, "patterns": {"dev_label": 1, "page text": 4}},
        },
    }
    logged = pc.for_log(result)
    assert logged["explore"] == {
        "steps": 5,
        "clicks": 4,
        "keys": 1,
        "distinct_screens": 3,
        "unique_lines": 9,
        "errors": 0,
        "reached_any": True,
    }
    assert logged["design_lint"] == {
        "flags": ["small_text", "dev_strings"],
        "small_text": 1,
        "dev_strings": {"dev_label": 1},
    }
    assert "page text" not in json.dumps(logged)


def test_reports_are_stiras_part_not_page_content():
    result = {
        "status": "ok",
        "title": "t",
        "text": "Tea Shop",
        "explore": {"steps": 1, "reached": [True], "reached_any": True},
        "design_lint": pc.lint_report(_numbers()),
    }
    own, page = pc.split_page_content(result)
    assert own["explore"] == result["explore"] and own["design_lint"] == result["design_lint"]
    assert set(page) == {"title", "text"}


# --- with a real browser -----------------------------------------------------------------


def _run(ws, page, actions, **kw):
    return pc.run_page_check(
        page, _policy(ws), wait_ms=100, screenshot=False, actions=actions, **kw
    )


@needs_browser
def test_default_report_has_no_explore_or_lint(ws):
    result = _run(ws, "lint_flagged.html", None)
    assert result["status"] == "ok"
    assert "explore" not in result and "design_lint" not in result


@needs_browser
def test_explore_finds_three_screens_and_the_marker(ws):
    explore = {"max_steps": 12, "seed": 1, "reach_markers": ["The End", "Never shown"]}
    result = _run(ws, "explore_three_screens.html", [{"explore": explore}])
    assert result["status"] == "ok", result
    report = result["explore"]
    assert report["distinct_screens"] == 3
    assert report["reached"] == [True, False] and report["reached_any"] is True
    assert report["steps"] == 12 == report["clicks"] + report["keys"]
    assert report["clicks"] >= 2 and report["errors"] == 0
    assert report["unique_lines"] == 9  # three screens of a title, a line and a button
    assert _only_numbers(report)
    step = result["steps"][0]
    assert step["action"] == "explore" and step["ok"] is True and "text" not in step
    own, _page = pc.split_page_content(result)
    told = json.dumps([own["explore"], own["steps"], pc.for_log(result)])
    assert not any(word in told for word in PAGE_WORDS)
    assert result["network"]["blocked"] == 0


@needs_browser
def test_explore_same_seed_same_report(ws):
    actions = [{"explore": {"max_steps": 10, "seed": 5, "reach_markers": ["The End"]}}]
    first = _run(ws, "explore_three_screens.html", actions)
    second = _run(ws, "explore_three_screens.html", actions)
    assert first["explore"] == second["explore"]
    assert first["explore"]["steps"] == 10


@needs_browser
def test_explore_stops_at_max_steps(ws):
    result = _run(ws, "explore_three_screens.html", [{"explore": {"max_steps": 1, "seed": 0}}])
    report = result["explore"]
    assert report["steps"] == 1 and report["distinct_screens"] <= 2
    assert report["reached"] == [] and report["reached_any"] is False


@needs_browser
def test_explore_never_clicks_a_hidden_or_covered_element(ws):
    (ws / "hidden.html").write_text(
        "<!doctype html><title>h</title><body style='margin:0'><p id='out'>start</p>"
        "<button style='display:none' onclick=\"out.textContent='hidden clicked'\">a</button>"
        "<button style='visibility:hidden' onclick=\"out.textContent='hidden clicked'\">b"
        "</button><button style='position:absolute;top:3000px' "
        "onclick=\"out.textContent='hidden clicked'\">c</button>"
        "<button id='under' style='position:absolute;left:0;top:200px;width:100px;height:40px'"
        " onclick=\"out.textContent='hidden clicked'\">d</button>"
        "<div style='position:absolute;left:0;top:200px;width:100px;height:40px;"
        "background:#fff'></div></body>",
        encoding="utf-8",
    )
    result = _run(
        ws, "hidden.html", [{"explore": {"max_steps": 6, "reach_markers": ["hidden clicked"]}}]
    )
    report = result["explore"]
    assert report["clicks"] == 0 and report["keys"] == 6
    assert report["reached_any"] is False and report["distinct_screens"] == 1


@needs_browser
def test_design_lint_flags_the_fixture(ws):
    result = _run(ws, "lint_flagged.html", [{"design_lint": True}])
    assert result["status"] == "ok", result
    lint = result["design_lint"]
    assert lint["ok"] is False
    assert lint["flags"] == ["empty_bottom", "small_text", "dev_strings"]
    assert lint["empty_area_ratio"]["bottom"] == 0.25
    assert lint["min_font_px"] == 8.0 and lint["small_text"] == 1
    assert lint["dev_strings"] == {"lines": 1, "patterns": {"dev_label": 1, "dimensions": 1}}
    assert _only_numbers(lint)
    own, _page = pc.split_page_content(result)
    told = json.dumps([own["design_lint"], own["steps"], pc.for_log(result)])
    assert not any(word in told for word in PAGE_WORDS)


@needs_browser
def test_design_lint_passes_the_clean_fixture(ws):
    result = _run(ws, "lint_clean.html", [{"design_lint": True}])
    assert result["status"] == "ok", result
    lint = result["design_lint"]
    assert lint["ok"] is True and lint["flags"] == []
    assert lint["min_font_px"] == 16.0
    assert lint["contrast_failures"] == lint["overflow_clipped"] == lint["overlap_pairs"] == 0
    assert lint["dev_strings"] == {"lines": 0, "patterns": {}}


@needs_browser
def test_design_lint_counts_contrast_clipping_and_overlap(ws):
    (ws / "bad.html").write_text(
        "<!doctype html><title>b</title><style>html,body{margin:0}"
        "body{font:16px sans-serif;color:#111;background:#fff}"
        "div{position:absolute;left:0;width:1280px}</style><body>"
        "<div style='top:0;height:200px;color:#ccc'>pale grey on white</div>"
        "<div style='top:200px;height:20px;overflow:hidden'><p style='margin:0;height:200px'>"
        "cut off</p></div>"
        "<div style='top:400px;height:400px'>one box</div>"
        "<div style='top:420px;height:380px'>another box over it</div></body>",
        encoding="utf-8",
    )
    lint = _run(ws, "bad.html", [{"design_lint": True}])["design_lint"]
    assert lint["contrast_failures"] == 1
    assert lint["overflow_clipped"] == 1
    assert lint["overlap_pairs"] >= 1
    assert {"low_contrast", "overflow_clipped", "overlap"} <= set(lint["flags"])
    assert _only_numbers(lint)
