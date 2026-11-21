"""Report files (#758, ADR 0028 §4): files the progress fingerprint ignores, chosen at issue.

Offline and scripted: a model that rewrites ``NOTES.md`` every round looks like progress to
W6a's fingerprint; with the file listed at issue the round is ``none`` and ``no_progress``
fires. The list is the person's (validated at issue, bound to the host-confirmed body,
never changed afterwards) and can only tighten.
"""

from __future__ import annotations

import inspect
import os

import pytest
from grants_exec_helpers import MARKER, grant, make_env, run_one, use
from grants_helpers import confirm_and_issue, confirm_and_resume, issue_body, request

from stiracore.grants import schema, service, state
from stiracore.grants.schema import GrantRefused, grant_from_dict, grant_to_dict, parse_issue
from stiracore.grants.service import GrantConfirmation
from stiracore.yolo.allowance import ClockReading


def _body(env, **overrides):
    return issue_body(
        env.sub,
        goal=f"tidy the notes {MARKER}",
        scope={"tools": ["file_read", "editor", "run_checks"], "workspace_subtree": str(env.sub)},
        budget={"total_tokens": 10_000, "per_tick_tokens": 2_000},
        **overrides,
    )


def _env(tmp_path, report_files=None):
    env = make_env(tmp_path)
    if report_files is not None:
        env.service.revoke(env.grant_id)
        body = _body(env, report_files=report_files)
        env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    return env


def _write(env, name, text, n=1):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": text}, n)


def _replace(env, name, old, new, n=1):
    arguments = {"command": "str_replace", "path": str(env.sub / name), "old_str": old}
    return use("editor", {**arguments, "new_str": new}, n)


def _iterate(env, *steps):
    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


def _notes_rounds(env, rounds=3):
    """The scripted model of the defect: it rewrites NOTES.md every round, nothing else."""
    (env.sub / "NOTES.md").write_text("round 0\n")
    for n in range(1, rounds + 1):
        _iterate(env, _replace(env, "NOTES.md", f"round {n - 1}", f"round {n}"))


def _tracker(env):
    return env.executor._trackers.get(env.grant_id)


# --- the defect and the fix ---------------------------------------------------------------------


def test_without_the_list_a_notes_rewrite_is_new_every_round(tmp_path):
    env = _env(tmp_path)
    _notes_rounds(env)
    g = grant(env)
    assert (g.iterations, g.no_progress, g.goal_status) == (3, 0, "active")


def test_with_the_report_file_listed_the_round_is_none_and_no_progress_fires(tmp_path):
    env = _env(tmp_path, ["NOTES.md"])
    _notes_rounds(env)
    assert (env.sub / "NOTES.md").read_text() == "round 3\n"  # the writes happened
    g = grant(env)
    assert (g.iterations, g.no_progress) == (3, 3)
    assert (g.goal_status, g.goal_reason) == ("blocked", "no_progress")
    tracker = _tracker(env)
    assert list(tracker.ring) == [None, None, None]  # ``none``: one history place each
    assert tracker.paths() == []


def test_a_report_file_written_first_is_never_tracked_and_real_work_still_counts(tmp_path):
    env = _env(tmp_path, ["NOTES.md", "docs/LOG.md"])
    (env.sub / "docs").mkdir()
    _iterate(env, _write(env, "NOTES.md", "first\n"), _write(env, "docs/LOG.md", "first\n", 2))
    assert grant(env).no_progress == 1 and _tracker(env).paths() == []
    _iterate(env, _replace(env, "NOTES.md", "first", "second"), _write(env, "a.py", "v1\n", 2))
    assert grant(env).no_progress == 0  # a tracked file changed: ``new``
    assert _tracker(env).paths() == [os.path.realpath(env.sub / "a.py")]
    # The same name elsewhere is not the report file.
    (env.sub / "pkg").mkdir()
    _iterate(env, _write(env, "pkg/NOTES.md", "work\n"))
    assert grant(env).no_progress == 0 and len(_tracker(env).paths()) == 2


def test_the_check_rows_stay_in_the_fingerprint(tmp_path):
    env = _env(tmp_path, ["NOTES.md"])
    (env.sub / "NOTES.md").write_text("round 0\n")
    _iterate(
        env,
        _replace(env, "NOTES.md", "round 0", "round 1"),
        use("run_checks", {"path": str(env.sub)}, 2),
    )
    assert grant(env).no_progress == 0 and len(env.checks) == 1


def test_unreadable_stays_unknown_with_a_report_file_written(tmp_path):
    env = _env(tmp_path, ["NOTES.md"])
    _iterate(env, _write(env, "a.py", "v1\n"))
    tracker = _tracker(env)
    assert len(tracker.ring) == 1 and grant(env).no_progress == 0
    (env.sub / "a.py").unlink()
    (env.sub / "a.py").mkdir()  # a tracked path that is no regular file: cannot be read
    _iterate(env, _write(env, "NOTES.md", "note\n"))
    assert len(tracker.ring) == 1  # unknown: not in the history (``none`` would add a place)
    assert grant(env).no_progress == 1  # ... and never progress
    assert tracker.paths() == [os.path.realpath(env.sub / "a.py")]


# --- validation at issue ------------------------------------------------------------------------


@pytest.fixture
def sub(tmp_path):
    path = tmp_path / "ws" / "sub"
    (path / "docs").mkdir(parents=True)
    (path / "NOTES.md").write_text("n\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.md").write_text("o\n")
    (path / "out.md").symlink_to(outside / "o.md")
    (path / "outdir").symlink_to(outside)
    (path / "alias.md").symlink_to(path / "NOTES.md")
    return path


def _parse(sub, report_files):
    return parse_issue(issue_body(sub, report_files=report_files), roots=[sub.parent])


def test_a_valid_list_parses_and_the_default_is_none(sub):
    assert parse_issue(issue_body(sub), roots=[sub.parent]).report_files == ()
    assert _parse(sub, []).report_files == ()
    req = _parse(sub, ["NOTES.md", "docs/new report.md"])  # a file need not exist yet
    assert req.report_files == ("NOTES.md", "docs/new report.md")
    assert len(_parse(sub, [f"r{n}.md" for n in range(8)]).report_files) == 8
    assert "NOTES" not in repr(req)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ([f"r{n}.md" for n in range(9)], "grant_cap_exceeded"),
        ("NOTES.md", "grant_field_invalid"),
        (None, "grant_field_invalid"),
        ([1], "grant_field_invalid"),
        ([""], "grant_field_invalid"),
        (["*.md"], "grant_field_invalid"),
        (["docs/*"], "grant_field_invalid"),
        (["note?.md"], "grant_field_invalid"),
        (["note[12].md"], "grant_field_invalid"),
        (["{a,b}.md"], "grant_field_invalid"),
        (["docs"], "grant_field_invalid"),  # a directory
        (["docs/"], "grant_field_invalid"),
        (["."], "grant_field_invalid"),
        (["/etc/NOTES.md"], "grant_field_invalid"),
        (["../NOTES.md"], "grant_field_invalid"),
        (["docs/../../x.md"], "grant_field_invalid"),
        (["./NOTES.md"], "grant_field_invalid"),
        (["docs//x.md"], "grant_field_invalid"),
        (["docs\\x.md"], "grant_field_invalid"),
        (["~/x.md"], "grant_field_invalid"),
        (["a\x00b"], "grant_field_invalid"),
        (["a\nb"], "grant_field_invalid"),
        (["x" * 256], "grant_field_invalid"),
        (["NOTES.md", "NOTES.md"], "grant_field_invalid"),
        (["out.md"], "grant_field_invalid"),  # a symlink out of the subtree
        (["outdir/o.md"], "grant_field_invalid"),  # through a symlinked directory
        (["alias.md"], "grant_field_invalid"),  # any symlink: the name must be the file
        (["NOTES.md/x"], "grant_field_invalid"),  # below a file
    ],
)
def test_the_list_is_strict(sub, value, code):
    with pytest.raises(GrantRefused, match=f"^{code}$"):
        _parse(sub, value)


def test_a_list_subclass_or_a_secret_like_name_is_refused(sub):
    class Sneaky(list):
        pass

    with pytest.raises(GrantRefused, match="^grant_field_invalid$"):
        _parse(sub, Sneaky(["NOTES.md"]))
    secret = "AKIA" + "ABCDEFGHIJKLMNOP.md"
    with pytest.raises(GrantRefused) as caught:
        _parse(sub, [secret])
    assert caught.value.code == "grant_field_invalid"
    assert "AKIA" not in str(caught.value) and "AKIA" not in repr(caught.value.extra)


# --- the record, the host confirmation, the views -----------------------------------------------


def test_the_stored_form_of_the_list():
    now = ClockReading(wall=1e9, boot=10.0, mono=10.0)
    listed = state.new_grant(
        request(report_files=("NOTES.md", "docs/LOG.md")), grant_id="a" * 32, issuer="t", now=now
    )
    plain = state.new_grant(request(), grant_id="b" * 32, issuer="t", now=now)
    stored = grant_to_dict(listed, "g")
    assert stored["report_files"] == ["NOTES.md", "docs/LOG.md"]
    assert grant_from_dict(stored)[0] == listed
    stored = grant_to_dict(plain, "g")
    assert "report_files" not in stored  # a record without a list is as before
    assert grant_from_dict(stored)[0].report_files == ()
    many = [f"r{n}.md" for n in range(9)]
    for bad in ([], "NOTES.md", ["../x"], ["/x"], ["*.md"], ["a", "a"], [1], many):
        with pytest.raises(GrantRefused, match="^grant_body_invalid$"):
            grant_from_dict({**stored, "report_files": bad})


def test_the_list_is_in_the_host_confirmed_view_and_bound_to_it(tmp_path):
    env = make_env(tmp_path)
    env.service.revoke(env.grant_id)
    body = _body(env, report_files=["NOTES.md", "docs/LOG.md"])
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    assert view["summary"].get("changes") == ["report_files:2"]
    assert "NOTES" not in repr(view)  # a count, never a path
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    # The confirmation was for this list: the same operation with another list is refused.
    for other in (["NOTES.md"], ["NOTES.md", "src/main.py"], []):
        with pytest.raises(GrantRefused) as refused:
            env.service.issue({**body, "report_files": other}, op_id=view["id"])
        assert refused.value.code == "grant_operation_refused"
    without = {k: v for k, v in body.items() if k != "report_files"}
    with pytest.raises(GrantRefused) as refused:
        env.service.issue(without, op_id=view["id"])
    assert refused.value.code == "grant_operation_refused"
    issued = env.service.issue(body, op_id=view["id"])["grant"]
    assert issued["report_files"] == 2
    assert env.service._get(issued["grant_id"]).report_files == ("NOTES.md", "docs/LOG.md")
    # Without a list the summary and the view are as before.
    env.service.revoke(issued["grant_id"])
    with pytest.raises(GrantConfirmation) as plain:
        env.service.issue(_body(env))
    assert "changes" not in plain.value.view["summary"]


def test_a_grant_without_a_list_carries_no_key_in_its_view(tmp_path):
    env = make_env(tmp_path)
    assert "report_files" not in env.service.view(env.grant_id)


def test_the_list_cannot_change_after_issue(tmp_path):
    """No route or service call sets it; a pause, a resume and iterations keep it."""
    env = _env(tmp_path, ["NOTES.md"])
    _notes_rounds(env)  # blocked(no_progress)
    confirm_and_resume(env.service, env.grant_id, {})
    env.service.pause(env.grant_id)
    assert grant(env).report_files == ("NOTES.md",)
    # The record's list is set in one place only: a new grant, from its issue request.
    assert inspect.getsource(state).count("report_files=") == 1
    assert "report_files=tuple(request.report_files)" in inspect.getsource(state.new_grant)
    assert "report_files=" not in inspect.getsource(service)
    assert schema.REPORT_FILES_MAX == 8
