"""Strict grant schema (ADR 0011 §2, §7, §8; W4 of #296)."""

from __future__ import annotations

import copy
import os

import pytest

from stiracore.grants import schema, state
from stiracore.grants.schema import GrantRefused, grant_from_dict, grant_to_dict, parse_issue
from tests.grants_helpers import FakeClocks, issue_body, request


def refused(code):
    return pytest.raises(GrantRefused, match=f"^{code}$")


@pytest.fixture
def roots(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    return ws


def test_a_valid_body_parses_with_the_defaults(roots):
    req = parse_issue(issue_body(roots / "sub"), roots=[roots])
    assert req.tools == ("editor", "file_read")
    assert (req.ttl_sec, req.max_iterations, req.no_progress_ticks, req.verifier) == (
        7200,
        10,
        3,
        "user",
    )
    assert req.workspace_subtree == os.path.realpath(roots / "sub")
    assert "make the tests" not in repr(req)  # never the goal in a repr


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda b: b.update(extra=1), "grant_field_unknown"),
        (lambda b: b.pop("verifier"), "grant_field_missing"),
        (lambda b: b.update(confirm="issue"), "grant_confirm_mismatch"),
        (lambda b: b.pop("confirm"), "grant_field_missing"),
        (lambda b: b.update(verifier="run_checks:pytest"), "grant_verifier_invalid"),
        (lambda b: b.update(verifier=None), "grant_verifier_invalid"),
        (lambda b: b["scope"].update(tools=["shell"]), "grant_tool_invalid"),
        (lambda b: b["scope"].update(tools=[]), "grant_tool_invalid"),
        (lambda b: b["scope"].update(tools=["editor", "editor"]), "grant_tool_invalid"),
        (lambda b: b["scope"].update(tools="editor"), "grant_tool_invalid"),
        (lambda b: b["scope"].update(more=1), "grant_field_unknown"),
        (lambda b: b["budget"].update(total_tokens=True), "grant_field_invalid"),
        (lambda b: b["budget"].update(total_tokens=1.5), "grant_field_invalid"),
        (lambda b: b["budget"].update(total_tokens=0), "grant_field_invalid"),
        (lambda b: b["budget"].update(total_tokens=10**7), "grant_cap_exceeded"),
        (lambda b: b["budget"].update(per_tick_tokens=2000), "grant_cap_exceeded"),
        (lambda b: b.update(ttl_sec=21601), "grant_cap_exceeded"),
        (lambda b: b.update(ttl_sec=10), "grant_field_invalid"),
        (lambda b: b.update(max_iterations=21), "grant_cap_exceeded"),
        (lambda b: b.update(no_progress_ticks=0), "grant_field_invalid"),
        (lambda b: b.update(goal=""), "grant_goal_invalid"),
        (lambda b: b.update(goal="  "), "grant_goal_invalid"),
        (lambda b: b.update(goal="x" * 2001), "grant_goal_invalid"),
        (lambda b: b.update(goal="bell\x07"), "grant_goal_invalid"),
        (lambda b: b.update(goal=["list"]), "grant_goal_invalid"),
    ],
)
def test_the_issue_body_is_strict(roots, change, code):
    body = issue_body(roots)
    change(body)
    with refused(code):
        parse_issue(body, roots=[roots])


def test_caps_are_accepted_at_their_limit(roots):
    body = issue_body(roots, ttl_sec=21600, max_iterations=20)
    req = parse_issue(body, roots=[roots])
    assert (req.ttl_sec, req.max_iterations) == (21600, 20)


def test_a_body_that_is_not_built_in_json_is_refused(roots):
    class Sneaky(dict):
        pass

    with refused("grant_body_invalid"):
        parse_issue(Sneaky(issue_body(roots)), roots=[roots])
    with refused("grant_body_invalid"):
        parse_issue([issue_body(roots)], roots=[roots])


def test_a_goal_that_mask_secrets_would_change_is_refused(roots):
    secret = "deploy with AKIA" + "ABCDEFGHIJKLMNOP and push"
    body = issue_body(roots, goal=secret)
    with pytest.raises(GrantRefused) as caught:
        parse_issue(body, roots=[roots])
    assert caught.value.code == "grant_goal_secret"
    assert "AKIA" not in str(caught.value) and "AKIA" not in repr(caught.value.extra)
    assert schema.check_goal("改善: テストを緑にする\n次に README") is not None


def test_the_subtree_must_be_inside_a_write_root_without_a_symlink_escape(tmp_path, roots):
    outside = tmp_path / "outside"
    outside.mkdir()
    with refused("grant_subtree_outside"):
        schema.check_subtree(str(outside), [roots])
    link = roots / "link"
    link.symlink_to(outside)
    with refused("grant_subtree_invalid"):
        schema.check_subtree(str(link), [roots])  # the final component is a symlink
    (roots / "sub" / "deeper").mkdir()
    through = roots / "through"
    through.symlink_to(outside)
    (outside / "inner").mkdir()
    with refused("grant_subtree_outside"):
        schema.check_subtree(str(through / "inner"), [roots])  # resolves outside
    with refused("grant_subtree_invalid"):
        schema.check_subtree("relative/path", [roots])
    with refused("grant_subtree_invalid"):
        schema.check_subtree(str(roots / "missing"), [roots])
    (roots / "file.txt").write_text("x")
    with refused("grant_subtree_invalid"):
        schema.check_subtree(str(roots / "file.txt"), [roots])
    with refused("grant_subtree_invalid"):
        schema.check_subtree(str(roots) + "\0", [roots])
    sibling = tmp_path / "ws-evil"  # a prefix of the root's name is not inside it
    sibling.mkdir()
    with refused("grant_subtree_outside"):
        schema.check_subtree(str(sibling), [roots])
    assert schema.check_subtree(str(roots / "sub" / "deeper"), [roots]).endswith("deeper")


def test_the_write_floor_policy_can_refuse_a_subtree(roots):
    class Floor:
        def check(self, path):
            return type("D", (), {"allowed": False})()

    with refused("grant_subtree_outside"):
        schema.check_subtree(str(roots), [roots], Floor())


def test_the_record_round_trips_and_unknown_fields_are_refused():
    g = state.new_grant(request(), grant_id="c" * 32, issuer="dashboard", now=FakeClocks()())
    data = grant_to_dict(g, "the goal")
    back, goal = grant_from_dict(copy.deepcopy(data))
    assert back == g and goal == "the goal"
    for change in (
        lambda d: d.update(extra=1),
        lambda d: d.pop("goal"),
        lambda d: d.update(kind="yolo"),
        lambda d: d.update(session_ref="grant:" + "d" * 32),
        lambda d: d.update(status="running"),
        lambda d: d.update(verifier="model"),
        lambda d: d.update(tools=["shell"]),
        lambda d: d.update(ttl_sec=99999),
        lambda d: d.update(generation=True),
        lambda d: d.update(issued_wall=float("inf")),
        lambda d: d.update(issued_wall=10**400),
        lambda d: d.update(recheck=1),
        lambda d: d.update(late_verification="maybe"),
        lambda d: d.update(reason="Free Text!"),
    ):
        bad = copy.deepcopy(data)
        change(bad)
        with refused("grant_body_invalid"):
            grant_from_dict(bad)


def test_refusal_codes_are_fixed():
    error = GrantRefused("not a code at all")
    assert error.code == "grant_body_invalid"
    assert all(code.startswith("grant_") for code in schema.CODES)


def test_the_resolved_subtree_obeys_the_readers_length_cap(tmp_path, roots, monkeypatch):
    """Opus N5: a short given path through a symlinked parent whose resolved form is
    longer than the cap is refused at issue (the reader would refuse it at the next start)."""
    deep = roots / ("d" * 40) / ("e" * 40)
    deep.mkdir(parents=True)
    short = roots / "s"
    short.symlink_to(deep.parent)
    given = str(short / ("e" * 40))
    limit = len(given) + 5
    assert len(os.path.realpath(given)) > limit
    monkeypatch.setattr(schema, "SUBTREE_MAX_CHARS", limit)
    with refused("grant_subtree_invalid"):
        schema.check_subtree(given, [roots])


def test_the_store_never_writes_a_record_its_reader_refuses(tmp_path):
    from stiracore.grants.store import GrantStore

    g = state.new_grant(request(), grant_id="c" * 32, issuer="dashboard", now=FakeClocks()())
    bad = grant_to_dict(g, "goal")  # an active grant: the reader refuses it
    store = GrantStore(tmp_path)
    store.load()
    assert store.write([{"grant": bad, "restart_from": ""}]) is False
    assert store.unhealthy_reason == "grant_store_write_failed"
    assert not (tmp_path / "grants.json").exists()
