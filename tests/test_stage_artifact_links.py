"""#535: the board's rows and the progress report link to ``/artifacts?turn=<turn>`` when
that turn wrote artifacts (#454), with a count only; a turn that wrote none has no link.

Offline: the scripted model and fake clock of ``tests/test_progress_report.py``.
"""

from __future__ import annotations

import json

from tests.provider_fakes import text_turn
from tests.test_progress_report import (  # noqa: F401 - the fixtures
    ClockModel,
    _chat,
    _open_window,
    _report_turn,
    _turn_frame,
    _workspace,
    _write,
    clock,
    make_app,
)

BODY = "SECRET-CONTENT-123"


def _row(items, turn, kind, what):
    return next(i for i in items if i["turn"] == turn and i["kind"] == kind and i["what"] == what)


def test_board_rows_count_and_link_only_the_turns_that_wrote(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model)
    client = app.test_client()
    _open_window(client)  # file_write is Ask: the window approves it
    model.script = [
        _write(app, "a.md", BODY + "\n", "w1"),
        _write(app, "b.md", "two\n", "w2"),
        text_turn("written"),
    ]
    first = _turn_frame(_chat(app, client, "k1"))["turn_id"]
    model.script = [text_turn("nothing written")]
    second = _turn_frame(_chat(app, client, "k2"))["turn_id"]
    items = client.get("/api/activity").get_json()["items"]
    assert _row(items, first, "turn", "completed")["artifacts"] == 2
    writes = [i for i in items if i["turn"] == first and i["kind"] == "tool"]
    assert [i.get("artifacts") for i in writes] == [2, 2]
    assert all("artifacts" not in i for i in items if i["turn"] == second)
    others = [i for i in items if i["kind"] == "model" or i["what"] == "started"]
    assert others and all("artifacts" not in i for i in others)
    text = json.dumps(items)
    assert BODY not in text and "a.md" not in text and str(_workspace(app)) not in text
    # The link's target lists that turn's artifacts only.
    listed = client.get(f"/api/artifacts?turn={first}").get_json()["artifacts"]
    assert sorted(a["path"] for a in listed) == ["a.md", "b.md"]
    assert client.get(f"/api/artifacts?turn={second}").get_json()["artifacts"] == []
    assert client.get("/artifacts").status_code == 200


def test_a_report_on_a_turn_that_wrote_carries_the_count(make_app, clock):  # noqa: F811
    from tests.test_progress_report import _on

    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, _report = _report_turn(app, client, model)
    body = client.get("/api/progress/current").get_json()
    report = body["report"]
    assert (
        report["turn_id"] == frame["turn_id"] and report["artifacts"] == 1
    )  # a.md (b.md cancelled)
    assert BODY not in json.dumps(body) and 'a.md"' not in json.dumps(body)
    # The turn events and the audit hold no body or path (counts only on the read).
    events = client.get(f"/api/turns/{frame['turn_id']}/events").get_json()["events"]
    assert all("artifacts" not in e["data"] for e in events)
    assert BODY not in json.dumps(events)


def test_a_report_on_a_turn_without_artifacts_has_no_count(make_app, clock):  # noqa: F811
    from tests.test_progress_report import MIN, _on

    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [
        [{"advance": 31 * MIN}, *_write(app, "b.md", "never\n", "w2")],
        text_turn("Nothing written yet."),
    ]
    _chat(app, client, "k1")
    report = client.get("/api/progress/current").get_json()["report"]
    assert report is not None and "artifacts" not in report


def test_turn_counts_are_bounded_and_skip_hidden_paths(tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig
    from stiracore.tools import workspace_artifacts as wa

    data_dir = (tmp_path / "data").resolve()
    workspace = data_dir / "workspace"
    workspace.mkdir(parents=True)
    policy, _floor = build_policy(AgentConfig(), data_dir)
    store = wa.WorkspaceArtifacts(data_dir / "artifacts")
    try:
        for name, turn in (("a.md", "t1"), ("b.md", "t1"), ("c.md", "t2")):
            (workspace / name).write_text(name, encoding="utf-8")
            assert store.register(policy, name, turn_id=turn)["status"] == "registered"
        (workspace / "a.md").write_text("again", encoding="utf-8")
        store.register(policy, "a.md", turn_id="t2")
        assert store.turn_counts(policy, ["t1", "t2", "t3", "t1"]) == {"t1": 2, "t2": 2}
        assert store.turn_counts(None, ["t1"]) == {}  # no policy: nothing is visible
        assert store.turn_counts(policy, []) == {}
        assert store.turn_counts(policy, ["../x", 5, None]) == {}
        (workspace / "c.md").unlink()
        (workspace / "c.md").symlink_to(workspace / "b.md")  # now a symlink: hidden
        assert store.turn_counts(policy, ["t2"]) == {"t2": 1}
        many = [f"x{i}" for i in range(100)] + ["t1"]
        assert store.turn_counts(policy, many) == {}  # only the first TURN_COUNTS_MAX ids
        assert wa.TURN_COUNTS_MAX == 32
    finally:
        store.close()
