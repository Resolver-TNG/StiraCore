"""ADR 0015 Addendum rev 3 (#449): ``type``, ``wait_for`` and the five assertions.

Pure parts (the checks before launch, Stira's pass/fail decision, the fixed functions, the
summary, the logs) run everywhere; the real-browser tests need Chrome / Chromium / Edge and
are skipped without one unless ``STIRA_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import json
import os
import queue
import time

import pytest

from stiracore.tools import page_check as pc

BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)


def _policy(ws, **extra):
    from stiracore.identity_paths import WritePathPolicy

    return WritePathPolicy(base_dir=ws, write_roots=[ws], **extra)


@pytest.fixture
def ws(tmp_path):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    (root / "ok.html").write_text("<title>t</title>", encoding="utf-8")
    return root


# --- checked before any browser starts -------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        {"type": ""},
        {"type": "x" * (pc.TYPE_CHARS + 1)},
        {"type": "line\nbreak"},  # Enter is a key, not text
        {"type": "tab\there"},
        {"type": "bidi \u202e override"},
        {"type": "zero\u200bwidth"},
        {"type": 5},
        {"type": "a", "into": ""},
        {"type": "a", "into": 3},
        {"type": "a", "hold_ms": 3},  # each optional key belongs to one kind
        {"click": "#a", "into": "#b"},
        {"key": "a", "timeout_ms": 3},
        {"wait_for": "#a"},  # an object, not a string
        {"wait_for": {}},
        {"wait_for": {"selector": ""}},
        {"wait_for": {"selector": "#a", "visible": "yes"}},
        {"wait_for": {"selector": "#a", "text": "x"}},
        {"wait_for": {"text": ""}},
        {"wait_for": {"text": "x" * (pc.MATCH_CHARS + 1)}},
        {"wait_for": {"script": "1"}},
        {"wait_for": {"selector": "#a"}, "timeout_ms": -1},
        {"wait_for": {"selector": "#a"}, "timeout_ms": 1.5},
        {"expect": {"exists": True}},  # a selector is required
        {"expect": {"selector": "#a"}},  # and exactly one form
        {"expect": {"selector": "#a", "exists": True, "visible": True}},
        {"expect": {"selector": "#a", "exists": "true"}},
        {"expect": {"selector": "#a", "visible": 1}},
        {"expect": {"selector": "#a", "count": -1}},
        {"expect": {"selector": "#a", "count": pc.COUNT_MAX + 1}},
        {"expect": {"selector": "#a", "count": True}},
        {"expect": {"selector": "#a", "text_contains": ""}},
        {"expect": {"selector": "#a", "text_contains": "x" * (pc.MATCH_CHARS + 1)}},
        {"expect": {"selector": "#a", "text_equals": 1}},
        {"expect": {"selector": "#a", "matches": "x"}},  # only the five forms
        {"expect": {"selector": "#a", "js": "document.title === 'x'"}},
        {"expect": {"selector": "#a", "evaluate": "1"}},
        {"expect": "#a exists"},
    ],
)
def test_new_actions_outside_their_shape_are_refused_before_launch(ws, monkeypatch, action):
    monkeypatch.setattr(pc, "_Browser", None)  # a launch would fail loudly
    result = pc.run_page_check(
        "ok.html", _policy(ws), browser="/nonexistent", actions=[{"wait_ms": 1}, action]
    )
    assert (result["status"], result["reason"]) == ("refused", "invalid_action"), action
    assert result["message"].startswith("action 1:")


def test_the_new_actions_pass_the_check_in_their_shapes():
    checked, over = pc.check_actions(
        [
            {"type": "héllo wörld 日本語 ✓"},
            {"type": "x" * pc.TYPE_CHARS, "into": "#name"},
            {"wait_for": {"selector": "#done"}},
            {"wait_for": {"selector": "#done", "visible": True}, "timeout_ms": 99_999},
            {"wait_for": {"text": "Game over"}, "timeout_ms": 0},
            *({"expect": {"selector": "#s", form: v}} for form, v in _FORMS),
            {"expect": {"selector": "#s", "text_equals": ""}},
        ]
    )
    assert not over
    assert checked[0] == ("type", ("héllo wörld 日本語 ✓", None), 0)
    assert checked[1][1] == ("x" * pc.TYPE_CHARS, "#name")
    assert checked[2] == ("wait_for", ("selector", "#done", False), pc.WAIT_FOR_DEFAULT_MS)
    assert checked[3] == ("wait_for", ("selector", "#done", True), 99_999)  # clamped when run
    assert checked[4] == ("wait_for", ("text", "Game over", False), 0)
    assert [c[1][1] for c in checked[5:10]] == list(pc.EXPECT_FORMS)


_FORMS = [
    ("exists", True),
    ("visible", False),
    ("text_contains", "1"),
    ("text_equals", "Score 1"),
    ("count", 3),
]


def test_assertions_count_toward_the_action_limit():
    actions = [{"wait_ms": 1}] * (pc.ACTION_LIMIT - 1) + [
        {"expect": {"selector": "#a", "exists": True}}
    ] * 2
    checked, over = pc.check_actions(actions)
    assert len(checked) == pc.ACTION_LIMIT and over
    assert [k for k, _v, _h in checked].count("expect") == 1


# --- Stira decides pass / fail (no model-judged pass, no JavaScript from the model) -----


@pytest.mark.parametrize(
    ("form", "expected", "facts", "outcome"),
    [
        ("exists", True, [1, False, None], (True, None)),
        ("exists", True, [0, False, None], (False, "mismatch")),
        ("exists", False, [0, False, None], (True, None)),
        ("exists", False, [2, True, None], (False, "mismatch")),
        ("visible", True, [1, True, None], (True, None)),
        ("visible", True, [1, False, None], (False, "mismatch")),
        ("visible", True, [0, False, None], (False, "mismatch")),
        ("visible", False, [0, False, None], (True, None)),  # nothing there: not visible
        ("visible", False, [1, False, None], (True, None)),
        ("visible", False, [1, True, None], (False, "mismatch")),
        ("count", 2, [2, True, None], (True, None)),
        ("count", 0, [0, False, None], (True, None)),
        ("count", 2, [3, True, None], (False, "mismatch")),
        ("text_contains", "Score 1", [1, True, "  Score\n 1  of 3"], (True, None)),
        ("text_contains", "Score  1", [1, True, "Score 1"], (True, None)),  # both folded
        ("text_contains", "Score 2", [1, True, "Score 1"], (False, "mismatch")),
        ("text_contains", "x", [0, False, None], (False, "not_found")),
        ("text_contains", "s", [1, True, "S"], (False, "mismatch")),  # case matters
        ("text_equals", "Score 1", [1, True, "\n Score   1 "], (True, None)),
        ("text_equals", "Score", [1, True, "Score 1"], (False, "mismatch")),
        ("text_equals", "", [1, False, "   "], (True, None)),
        ("text_equals", "a", [0, False, None], (False, "not_found")),
        ("text_equals", "a", [1, True, "a" * pc.TEXT_SCAN_CHARS], (False, "mismatch")),
        ("exists", True, None, (False, "bad_selector")),  # the selector did not parse
        ("count", 1, [1, True], (False, "bad_selector")),  # malformed facts never pass
        ("count", 1, ["1", True, None], (False, "bad_selector")),
        ("visible", True, [1, "true", None], (False, "bad_selector")),
        ("exists", True, [-1, False, None], (False, "bad_selector")),
        ("text_contains", "a", [1, True, 5], (False, "bad_selector")),
    ],
)
def test_assertion_outcomes_are_decided_by_stira(form, expected, facts, outcome):
    assert pc.expect_outcome(form, expected, facts) == outcome


def test_an_unknown_form_never_passes():
    assert pc.expect_outcome("matches", "x", [1, True, "x"]) == (False, "bad_selector")


@pytest.mark.parametrize(
    ("how", "expected", "visible", "facts", "met"),
    [
        ("selector", "#a", False, [1, False, None], True),
        ("selector", "#a", True, [1, False, None], False),
        ("selector", "#a", True, [1, True, None], True),
        ("selector", "#a", False, [0, False, None], False),
        ("text", "Game  over", False, [1, True, "x\nGame over!"], True),
        ("text", "Game over", False, [1, True, "Game"], False),
        ("text", "Game", False, [0, False, None], False),
        ("selector", "#a", False, None, None),
    ],
)
def test_wait_for_conditions(how, expected, visible, facts, met):
    assert pc.wait_for_met(how, expected, visible, facts) is met


def test_the_summary_counts_passed_failed_and_not_run():
    steps = [
        {"i": 0, "action": "click", "ok": False},
        {"i": 1, "action": "expect", "form": "exists", "ok": True},
        {"i": 2, "action": "expect", "form": "count", "ok": False, "code": "mismatch"},
    ]
    assert pc.assertion_summary(steps, 4) == {
        "passed": 1,
        "failed": 1,
        "not_run": 2,
        "results": [
            {"i": 1, "form": "exists", "pass": True},
            {"i": 2, "form": "count", "pass": False},
        ],
    }


# --- the fixed functions: the agent's strings are arguments, never code ----------------


class _Page:
    """A fake CDP pipe: ``document`` has an object id, every fixed function answers
    ``answers[fn]`` (a value, or ``"throw"``); all sent commands are kept."""

    def __init__(self, answers=None):
        self.inbox = queue.Queue()
        self.sent = []
        self.answers = answers or {}

    def send(self, method, params=None, session=None):
        self.sent.append((method, params or {}))
        result: dict = {}
        if method == "Runtime.evaluate":
            result = {"result": {"objectId": "doc"}}
        elif method == "Runtime.callFunctionOn":
            answer = self.answers.get(params["functionDeclaration"])
            if answer == "throw":
                result = {"exceptionDetails": {"text": "SyntaxError"}}
            else:
                result = {"result": {"value": answer}}
        self.inbox.put({"id": len(self.sent), "result": result})
        return len(self.sent)


def _run(ws, answers=None, deadline=20.0):
    run = pc._Run(_Page(answers), _policy(ws), time.monotonic() + deadline)
    run.main_session = "S"
    return run


AGENT = "x'), document.title = 'pwned', ('"


def test_every_new_action_calls_only_stira_functions_with_the_agent_strings_as_arguments(ws):
    run = _run(
        ws,
        {
            pc._FN_FOCUS: [True, True],
            pc._FN_PROBE: [1, True, AGENT],
            pc._FN_SIGNATURE: [1, 1],
        },
    )
    checked, _ = pc.check_actions(
        [
            {"type": AGENT, "into": AGENT},
            {"type": AGENT},
            {"wait_for": {"selector": AGENT}},
            {"wait_for": {"text": AGENT}},
            *({"expect": {"selector": AGENT, f: v}} for f, v in _FORMS),
            {"expect": {"selector": AGENT, "text_contains": AGENT}},
        ]
    )
    for index, (kind, value, hold) in enumerate(checked):
        run.act(index, kind, value, hold)
    calls = [(m, p) for m, p in run.b.sent]
    for method, params in calls:
        assert method in {
            "Runtime.evaluate",
            "Runtime.callFunctionOn",
            "Input.insertText",
        }, method
        if method == "Runtime.evaluate":
            assert params["expression"] == "document"
        elif method == "Runtime.callFunctionOn":
            assert params["functionDeclaration"] in pc.FIXED_FUNCTIONS
            assert AGENT not in params["functionDeclaration"]
            assert params["silent"] is True and params["returnByValue"] is True
        else:
            assert params == {"text": AGENT}
    assert [m for m, _ in calls].count("Input.insertText") == 2
    assert all(fn.startswith("function (") for fn in pc.FIXED_FUNCTIONS)


def test_type_inserts_only_into_an_editable_focused_element(ws):
    for state, code in (
        (None, "not_found"),
        ([False, True], "not_editable"),
        ([True, False], "not_focused"),
        ("throw", "bad_selector"),
        ("junk", "not_editable"),
    ):
        run = _run(ws, {pc._FN_FOCUS: state, pc._FN_SIGNATURE: [0, 0]})
        step = run.act(0, "type", ("abc", "#x"), 0)
        assert (step["ok"], step.get("code")) == (False, code), state
        assert not [m for m, _ in run.b.sent if m == "Input.insertText"]
    run = _run(ws, {pc._FN_FOCUS: [True, True], pc._FN_SIGNATURE: [0, 0]})
    step = run.act(0, "type", ("abc", None), 0)
    assert step["ok"] and "code" not in step
    focus = [p for m, p in run.b.sent if p.get("functionDeclaration") == pc._FN_FOCUS]
    assert focus[0]["arguments"] == [{"value": None}]  # null: the focused element


def test_wait_for_times_out_and_is_clamped(ws, monkeypatch):
    run = _run(ws, {pc._FN_PROBE: [0, False, None], pc._FN_SIGNATURE: [0, 0]})
    started = time.monotonic()
    step = run.act(0, "wait_for", ("selector", "#late", False), 300)
    took = time.monotonic() - started
    assert (step["ok"], step["code"]) == (False, "timeout") and 0.3 <= took < 1.5
    assert 300 <= step["waited_ms"] < 1500 and "clamped" not in step
    monkeypatch.setattr(pc, "WAIT_MAX_MS", 200)
    started = time.monotonic()
    step = run.act(1, "wait_for", ("selector", "#late", False), 60_000)
    assert time.monotonic() - started < 1.5
    assert (step["ok"], step["code"], step["clamped"]) == (False, "timeout", True)


def test_wait_for_that_never_parses_is_a_bad_selector(ws):
    run = _run(ws, {pc._FN_PROBE: "throw", pc._FN_SIGNATURE: [0, 0]})
    step = run.act(0, "wait_for", ("selector", "a[[", False), 200)
    assert (step["ok"], step["code"]) == (False, "bad_selector")


def test_wait_for_ends_at_the_call_deadline(ws):
    run = _run(ws, {pc._FN_PROBE: [0, False, None], pc._FN_SIGNATURE: [0, 0]}, deadline=0.5)
    started = time.monotonic()
    with pytest.raises(pc._CallTimeout):
        run.act(0, "wait_for", ("selector", "#late", False), 10_000)
    assert time.monotonic() - started < 1.5


def test_a_met_wait_returns_at_once(ws):
    run = _run(ws, {pc._FN_PROBE: [1, True, None], pc._FN_SIGNATURE: [0, 0]})
    started = time.monotonic()
    step = run.act(0, "wait_for", ("selector", "#here", True), 10_000)
    assert step["ok"] and "code" not in step and time.monotonic() - started < 0.5


# --- what the model and the logs see ---------------------------------------------------


def _result(**extra):
    base = {
        "status": "ok",
        "reason": None,
        "elapsed_ms": 10,
        "loaded": True,
        "title": "T",
        "page_errors": {"count": 0, "first": []},
        "console_errors": {"count": 0, "first": []},
        "missing_files": {"count": 0, "first": []},
        "network": {"blocked": 0, "hosts": [], "hosts_total": 0, "proxy_refused": 0},
        "blocked": {},
        "text": "SECRET-BODY",
        "steps": [
            {"i": 0, "action": "type", "ok": True, "text_changed": True, "new_errors": 0},
            {"i": 1, "action": "wait_for", "ok": False, "code": "timeout", "waited_ms": 5},
            {"i": 2, "action": "expect", "form": "count", "ok": False, "code": "mismatch"},
            {"i": 3, "action": "expect", "form": "text_equals", "ok": True},
        ],
        "assertions": {
            "passed": 1,
            "failed": 1,
            "not_run": 0,
            "results": [
                {"i": 2, "form": "count", "pass": False},
                {"i": 3, "form": "text_equals", "pass": True},
            ],
        },
        "screenshots": [],
        "open_yourself": {"path": "a.html", "url": "file:///w/a.html"},
        "note": pc.UNTRUSTED_NOTE,
    }
    base.update(extra)
    return base


def test_assertions_are_in_stiras_block_and_page_text_only_in_the_framed_one():
    own_block, page_block = pc.content_for_model(_result())
    own = json.loads(own_block["text"])
    assert own["assertions"]["passed"] == 1 and own["assertions"]["failed"] == 1
    assert own["assertions"]["results"][0] == {"i": 2, "form": "count", "pass": False}
    assert "SECRET-BODY" not in own_block["text"] and "SECRET-BODY" in page_block["text"]
    assert "assertions" not in page_block["text"]


def test_the_logs_keep_kinds_and_counts_and_no_typed_or_expected_text():
    from stiracore.tools.file_tools import payload_for_log

    actions = [
        {"type": "SECRET-TYPED", "into": "#SECRET-INTO"},
        {"wait_for": {"text": "SECRET-WAIT"}, "timeout_ms": 1234},
        {"wait_for": {"selector": "#SECRET-SEL", "visible": True}},
        {"expect": {"selector": "#SECRET-EXP", "text_equals": "SECRET-EQ"}},
        {"expect": {"selector": ".n", "count": 4321}},
    ]
    logged = payload_for_log("page_check", "input", {"target": "a.html", "actions": actions})
    assert logged["actions"] == {
        "count": 5,
        "kinds": {"type": 1, "wait_for": 2, "expect": 2},
    }
    flat = json.dumps(logged)
    for secret in ("SECRET", "1234", "4321", "visible", "text_equals"):
        assert secret not in flat
    out = json.dumps(pc.for_log(_result()))
    assert "SECRET" not in out and "results" not in out and "form" not in out
    assert pc.for_log(_result())["assertions"] == {"passed": 1, "failed": 1, "not_run": 0}
    via_model = payload_for_log("page_check", "output", pc._tool_result("u", _result()))
    kept = json.loads(via_model["content"][0]["text"])
    assert kept["assertions"] == {"passed": 1, "failed": 1, "not_run": 0}
    assert kept["steps"] == {"count": 4, "failed": 2, "text_changed": 1}
    assert "SECRET" not in json.dumps(via_model)


def test_the_tool_definition_names_the_new_actions():
    description = pc.page_check.tool_spec["description"]
    for word in ('"type"', '"wait_for"', '"expect"', *pc.EXPECT_FORMS, "No other URLs"):
        assert word in description
    assert len(json.dumps(pc.page_check.tool_spec)) < 2000  # #368: one compact definition


# --- real browser -----------------------------------------------------------------------


PAGE = """<title>Form</title>
<input id=name><input id=ro readonly><input id=chk type=checkbox><textarea id=area></textarea>
<div id=edit contenteditable>e</div><p id=echo></p><p id=score>Score   0</p>
<ul><li class=n>1</li><li class=n>2</li><li class=n>3</li></ul>
<div id=none style="display:none">x</div><input id=hiddenin style="display:none">
<div style="display:none"><span id=inner>x</span></div>
<div id=hid style="visibility:hidden">x</div>
<div style="opacity:0"><span id=faded>x</span></div>
<div id=zero style="width:0;height:0;overflow:hidden">x</div>
<div id=left style="position:absolute;left:-5000px;top:10px;width:50px">x</div>
<div id=cv style="content-visibility:hidden"><span id=cvin>x</span></div>
<div style="height:3000px"></div><p id=below>far below</p>
<button id=later onclick="setTimeout(() => {
  const d = document.createElement('div'); d.id = 'done'; d.textContent = 'Game over';
  document.body.appendChild(d); }, 400)">later</button>
<script>
document.getElementById('name').addEventListener('input', e => {
  document.getElementById('echo').textContent = 'typed:' + e.target.value;
});
</script>"""


@pytest.fixture
def form(ws):
    (ws / "form.html").write_text(PAGE, encoding="utf-8")
    return ws


def _check(ws, actions, **kw):
    kw.setdefault("wait_ms", 0)
    kw.setdefault("screenshot", False)
    return pc.run_page_check("form.html", _policy(ws), actions=actions, **kw)


@needs_browser
def test_type_into_a_selector_and_into_the_focused_element(form):
    result = _check(
        form,
        [
            {"type": "héllo 日本", "into": "#name"},
            {"text": "#echo"},
            {"click": "#area"},
            {"type": "in area"},
            {"type": "x", "into": "#edit"},
            {"expect": {"selector": "#edit", "text_contains": "x"}},
            {"type": "no", "into": "#ro"},
            {"type": "no", "into": "#chk"},
            {"type": "no", "into": "#missing"},
            {"type": "no", "into": "#none"},
            {"type": "no", "into": "#hiddenin"},
            {"type": "no", "into": "a[[["},
        ],
    )
    assert result["status"] == "ok", result
    steps = result["steps"]
    assert steps[0]["ok"] and steps[0]["text_changed"] is True
    assert steps[1]["text"] == "typed:héllo 日本"
    assert steps[3]["ok"] and steps[4]["ok"] and steps[5]["ok"]
    assert [s.get("code") for s in steps[6:]] == [
        "not_editable",
        "not_editable",
        "not_found",
        "not_editable",  # a div is not a field
        "not_focused",  # a hidden field takes no focus
        "bad_selector",
    ]
    assert result["page_errors"]["count"] == 0


@needs_browser
def test_type_with_nothing_focused_inserts_nothing(form):
    result = _check(form, [{"type": "lost"}, {"text": "body"}])
    assert (result["steps"][0]["ok"], result["steps"][0]["code"]) == (False, "not_editable")
    assert "lost" not in result["steps"][1]["text"]


@needs_browser
def test_the_visibility_rules(form):
    cases = [
        ("#name", True),
        ("#below", True),  # below the fold is visible (the page can scroll there)
        ("#none", False),
        ("#inner", False),  # an ancestor with display: none
        ("#hid", False),
        ("#faded", False),  # an ancestor with opacity 0
        ("#zero", False),
        ("#left", False),  # wholly left of the page
        ("#cvin", False),  # content-visibility: hidden
        ("#missing", False),
    ]
    actions = [{"expect": {"selector": s, "visible": True}} for s, _ in cases]
    result = _check(form, actions)
    got = [s["ok"] for s in result["steps"]]
    assert got == [v for _s, v in cases], list(zip([c[0] for c in cases], got, strict=True))
    negated = _check(form, [{"expect": {"selector": s, "visible": False}} for s, _ in cases])
    assert [s["ok"] for s in negated["steps"]] == [not v for _s, v in cases]


@needs_browser
def test_the_five_forms_against_a_real_page(form):
    result = _check(
        form,
        [
            {"expect": {"selector": "#score", "exists": True}},
            {"expect": {"selector": "#nope", "exists": False}},
            {"expect": {"selector": ".n", "count": 3}},
            {"expect": {"selector": ".n", "count": 2}},
            {"expect": {"selector": "#score", "text_contains": "Score 0"}},
            {"expect": {"selector": "#score", "text_equals": "Score 0"}},
            {"expect": {"selector": "#score", "text_equals": "Score"}},
            {"expect": {"selector": "#nope", "text_contains": "x"}},
            {"expect": {"selector": "a[[[", "exists": True}},
            {"expect": {"selector": AGENT, "exists": False}},  # not CSS: never passes
        ],
    )
    steps = result["steps"]
    expected = [True, True, True, False, True, True, False, False, False, False]
    assert [s["ok"] for s in steps] == expected
    assert steps[3]["count"] == 3 and steps[2]["count"] == 3
    assert [s.get("code") for s in steps if not s["ok"]] == [
        "mismatch",
        "mismatch",
        "not_found",
        "bad_selector",
        "bad_selector",
    ]
    assert result["assertions"]["passed"] == 5 and result["assertions"]["failed"] == 5
    assert result["title"] == "Form"  # the breakout selector ran as a selector only
    assert result["page_errors"]["count"] == 0


@needs_browser
def test_wait_for_a_selector_and_a_text_then_a_timeout(form):
    started = time.monotonic()
    result = _check(
        form,
        [
            {"click": "#later"},
            {"wait_for": {"selector": "#done", "visible": True}, "timeout_ms": 5000},
            {"wait_for": {"text": "Game  over"}, "timeout_ms": 1000},
            {"wait_for": {"selector": "#never"}, "timeout_ms": 400},
            {"wait_for": {"text": "never"}, "timeout_ms": 300},
            {"expect": {"selector": "#done", "text_equals": "Game over"}},
        ],
    )
    steps = result["steps"]
    assert steps[1]["ok"] and 250 <= steps[1]["waited_ms"] < 2500, steps[1]
    assert steps[2]["ok"] and steps[2]["waited_ms"] < 500
    assert (steps[3]["ok"], steps[3]["code"]) == (False, "timeout")
    assert 400 <= steps[3]["waited_ms"] < 1500
    assert (steps[4]["ok"], steps[4]["code"]) == (False, "timeout")
    assert steps[5]["ok"]
    assert result["assertions"] == {
        "passed": 1,
        "failed": 0,
        "not_run": 0,
        "results": [{"i": 5, "form": "text_equals", "pass": True}],
    }
    assert time.monotonic() - started < 10


@needs_browser
def test_a_long_wait_for_is_cut_at_the_time_limit_and_unrun_assertions_are_counted(form):
    started = time.monotonic()
    result = _check(
        form,
        [
            {"wait_for": {"selector": "#never"}, "timeout_ms": 60_000},
            {"expect": {"selector": "#name", "exists": True}},
        ],
        time_limit=3,
    )
    assert time.monotonic() - started < 10
    assert (result["status"], result["reason"]) == ("truncated", "time_limit")
    assert result["assertions"]["not_run"] == 1 and result["assertions"]["passed"] == 0


@needs_browser
def test_the_new_actions_run_through_the_registry_wrapper(form):
    import asyncio

    from stiracore.tools import tool_registry

    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=_policy(form))}
    tool = tools["page_check"]

    async def run():
        use = {
            "toolUseId": "u1",
            "name": "page_check",
            "input": {
                "target": "form.html",
                "wait_ms": 0,
                "actions": [
                    {"type": "zz-typed", "into": "#name"},
                    {"expect": {"selector": "#echo", "text_contains": "zz-typed"}},
                ],
            },
        }
        return [event async for event in tool.stream(use, {})]

    (event,) = asyncio.run(run())
    own_block, page_block = event.tool_result["content"]
    own = json.loads(own_block["text"])
    assert own["assertions"]["passed"] == 1 and own["steps"][1]["ok"]
    assert "zz-typed" not in own_block["text"] and "zz-typed" in page_block["text"]


@needs_browser
def test_typed_text_and_a_valid_breakout_selector_never_run_as_code(form):
    breakout = "[data-x=\"'), document.title = 'pwned', ('\"]"  # valid CSS
    typed = "'); document.title = 'pwned2'; ('"
    result = _check(
        form,
        [
            {"expect": {"selector": breakout, "exists": False}},
            {"wait_for": {"selector": breakout}, "timeout_ms": 100},
            {"type": typed, "into": "#name"},
            {"expect": {"selector": "#echo", "text_equals": "typed:" + typed}},
            {"wait_for": {"text": typed}, "timeout_ms": 100},
        ],
    )
    assert result["title"] == "Form"
    assert [s["ok"] for s in result["steps"]] == [True, False, True, True, True]
    assert result["steps"][1]["code"] == "timeout"


@needs_browser
def test_the_visibility_rules_without_check_visibility(form):
    """Stira's own rules hold where ``checkVisibility`` does not exist (older Chromium)."""
    page = PAGE.replace("<script>", "<script>delete Element.prototype.checkVisibility;", 1)
    (form / "form.html").write_text(page, encoding="utf-8")
    selectors = ["#name", "#none", "#inner", "#hid", "#faded", "#zero", "#left"]
    result = _check(form, [{"expect": {"selector": s, "visible": True}} for s in selectors])
    assert [s["ok"] for s in result["steps"]] == [True] + [False] * 6
