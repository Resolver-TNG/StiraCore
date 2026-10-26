"""ADR 0025 PR 2: what the page reads — the card's allow-again view, the allowance list,
revoke / "Revoke all", the log lines' source, and the #448 counter. Offline, no model.

PR 3 turns both buttons on (``session_allow.ENABLED``); with the constant off every view is
``not_enabled`` (PR 2's state, pinned by one test).
"""

from __future__ import annotations

import pytest
from flask import Flask
from test_session_allow import Env

from stiracore.observability import chat_api
from stiracore.yolo import allow_routes, allow_runtime, origin, tool_stats
from stiracore.yolo import session_allow as sa
from stiracore.yolo.allow_class import ORIGIN_CHAT
from stiracore.yolo.audit_schema import _tool_calls


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setattr(sa, "_TABLE", e.table)
    allow_runtime._reset_for_tests()
    allow_runtime.install(e.service, policy=e.policy)
    yield e
    allow_runtime._reset_for_tests()


@pytest.fixture
def on(monkeypatch):
    """The view as PR 3 computes it (the constant on, for this test only)."""
    monkeypatch.setattr(allow_runtime, "ENABLED", True)


def _app():
    app = Flask(__name__)
    allow_routes.install(app)
    return app.test_client()


def _item(env, request_id="r1", **extra):
    return {
        "id": request_id,
        "tool_name": "file_write",
        "tool_input": env.call(),
        "args_hash": "h" * 64,
        "reason_code": "rule_ask",
        "supersedes": None,
        "allow_origin": f"{ORIGIN_CHAT}:{env.table.session_epoch}",
        **extra,
    }


def test_every_card_view_is_not_enabled_without_the_constant(env, monkeypatch):
    monkeypatch.setattr(allow_runtime, "ENABLED", False)  # PR 2's state
    assert allow_runtime.card_view(_item(env)) == {"offered": False, "reason": "not_enabled"}
    assert allow_runtime.status()["enabled"] is False


def test_offered_view_names_the_class_relative_to_its_root(env, on):
    view = allow_runtime.card_view(_item(env))
    assert view["offered"] is True
    assert view["class_kind"] == "dir_subtree"
    assert view["label"] == "game/"
    assert view["whole_root"] is False
    assert view["max_hours"] == 2
    assert isinstance(view["token"], str) and view["token"]
    # A file at the root itself: the whole workspace (never hidden as a narrow class).
    top = _item(env, "r2", tool_input={"path": str(env.root / "top.py"), "content": "x"})
    wide = allow_runtime.card_view(top)
    assert wide["whole_root"] is True and wide["label"] == ""


def test_two_polls_of_one_card_keep_one_token_until_something_changes(env, on):
    first = allow_runtime.card_view(_item(env))
    again = allow_runtime.card_view(_item(env))
    assert again["token"] == first["token"]  # two tabs polling do not void each other
    sa.publish_end("stop")  # every view token ends with the fence
    fresh = allow_runtime.card_view(_item(env))
    assert fresh["token"] != first["token"]
    result = env.press(first["token"])
    assert result.status == "refused" and result.code in ("stale_view", "op_token_used")
    assert env.decided == 0


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"restored": True}, "not_this_session"),
        ({"supersedes": "r0"}, "edited"),
        ({"reason_code": "breaker_critical_halt"}, "breaker"),
        ({"allow_origin": None}, "not_this_session"),
        ({"allow_origin": f"{ORIGIN_CHAT}:99"}, "not_this_session"),
        ({"tool_name": "shell", "tool_input": {"command": "ls"}}, "exec"),
        ({"tool_name": "file_read", "tool_input": {"path": "a"}}, "read_unbound"),
        ({"tool_input": {"path": "game/a.py", "content": "AKIA" + "A" * 16}}, "secret_in_args"),
    ],
)
def test_not_offered_views_name_their_reason(env, on, extra, reason):
    view = allow_runtime.card_view(_item(env, **extra))
    assert view == {"offered": False, "reason": reason}


def test_a_request_of_an_earlier_session_is_not_offered(env, on):
    item = _item(env)
    env.table.new_session()  # /new after the card was raised
    assert allow_runtime.card_view(item) == {"offered": False, "reason": "not_this_session"}


def test_card_origin_only_inside_a_persons_turn(env):
    assert allow_runtime.card_origin("agent") is None
    with origin.human_turn("agent"):
        assert allow_runtime.card_origin("agent") == f"{ORIGIN_CHAT}:{env.table.session_epoch}"
        assert allow_runtime.card_origin("other-agent") is None
    with origin.human_turn("agent", automatic=True):
        assert allow_runtime.card_origin("agent") is None  # a window's continue: no


def test_the_page_never_sees_the_stored_origin(env):
    item = _item(env)
    shown = chat_api._with_allow_view(item)
    assert "allow_origin" not in shown
    assert shown["allow_again"]["offered"] is True


def test_list_shows_live_allowances_and_revoke_ends_one(env):
    client = _app()
    empty = client.get("/api/allowances").get_json()
    assert empty["allowances"] == [] and empty["max"] == 20 and empty["seq"] == 0
    env.allowed()
    body = client.get("/api/allowances").get_json()
    (row,) = body["allowances"]
    assert row["tool"] == "file_write" and row["class_kind"] == "dir_subtree"
    assert row["label"] == "game/" and row["uses"] == 0
    assert 7190 <= row["remaining_sec"] <= 7200
    revoked = client.post(f"/api/allowances/{row['allow_id']}/revoke")
    assert revoked.status_code == 200 and revoked.get_json()["allowances"] == []
    again = client.post(f"/api/allowances/{row['allow_id']}/revoke")
    assert again.status_code == 404
    env.service.flush_ended()
    assert [r["end_reason"] for r in env.rows("audit.allow.ended")] == ["revoked"]


def test_revoke_all_is_a_stop(env):
    client = _app()
    env.allowed()
    fence = env.table.snapshot().fence
    response = client.post("/api/allowances/revoke-all")
    assert response.status_code == 200 and response.get_json()["allowances"] == []
    assert env.table.snapshot().fence == fence + 1
    env.service.flush_ended()
    assert [r["end_reason"] for r in env.rows("audit.allow.ended")] == ["stop"]


def test_each_automatic_answer_is_one_log_line_source(env):
    env.allowed()
    binding = env.answer()
    run, _code, _intent = env.consume(binding)
    assert run is True
    allow_runtime.note_use(env.table.get(binding.allow_id))
    body = allow_runtime.status()
    assert body["seq"] == 1
    (use,) = body["uses"]
    assert use == {
        "seq": 1,
        "ref": binding.allow_id[:8],
        "tool": "file_write",
        "class_kind": "dir_subtree",
    }
    assert body["allowances"][0]["uses"] == 1


def test_card_end_voids_its_token_and_is_a_terminal_result(env, on):
    view = allow_runtime.card_view(_item(env))
    allow_runtime.card_ended("r1", "expired")
    assert env.press(view["token"]).status == "refused"


def test_allowed_again_is_a_counted_decision_448():
    stats = tool_stats.empty()
    tool_stats.add(stats, "file_write", "allowed_again")
    tool_stats.add(stats, "file_write", "manual")
    snap = tool_stats.snapshot(stats)
    assert snap["by_decision"]["allowed_again"] == 1 and snap["total"] == 2
    assert _tool_calls(snap)  # the audit row schema accepts it
