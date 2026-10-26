"""The grant control plane (ADR 0011 §8, §9, §11, §12; W4 of #296).

Real in-memory authority service and lifecycle recorder; fake clocks and breaker."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from stiracore.authority import outbox as outbox_module
from stiracore.grants import state
from stiracore.grants.schema import GrantRefused
from stiracore.grants.service import AutonomySettings, GrantConfirmation, settings_from
from tests.grants_helpers import (
    confirm_and_issue,
    confirm_and_resume,
    confirm_and_verify,
    events,
    issue_body,
    make_service,
    settle,
)

MARKER = "goal-marker-q8z2k"


@pytest.fixture
def env(tmp_path):
    service, authority, recorder, clocks, breaker, ws = make_service(tmp_path)
    return type(
        "Env",
        (),
        {
            "service": service,
            "authority": authority,
            "recorder": recorder,
            "clocks": clocks,
            "breaker": breaker,
            "ws": ws,
            "tmp": tmp_path,
        },
    )


def issued(env, **overrides):
    body = issue_body(env.ws, goal=f"rewrite {MARKER} docs", **overrides)
    return confirm_and_issue(env.service, body)["grant"]


def refused(code, host=None):
    class Check:
        def __enter__(self):
            self.ctx = pytest.raises(GrantRefused)
            self.info = self.ctx.__enter__()
            return self.info

        def __exit__(self, *exc):
            out = self.ctx.__exit__(*exc)
            assert self.info.value.code == code, self.info.value.code
            if host is not None:
                assert self.info.value.extra.get("host_code") == host, self.info.value.extra
            return out

    return Check()


# --- settings -----------------------------------------------------------------------------


def test_the_settings_are_off_unless_exactly_true():
    assert settings_from(None).stage_c is False
    assert settings_from({}).stage_c is False
    assert settings_from({"stage_c": "true"}).stage_c is False
    assert settings_from({"stage_c": 1}).stage_c is False
    on = settings_from({"stage_c": True})
    assert on.stage_c and on.run_checks_unattended  # unattended defaults to true
    assert (
        settings_from({"stage_c": True, "run_checks_unattended": "no"}).run_checks_unattended
        is False
    )
    assert (
        settings_from({"stage_c": True, "run_checks_unattended": False}).run_checks_unattended
        is False
    )
    assert "autonomy_unknown_key" in settings_from({"stage_c": True, "x": 1}).warnings
    assert settings_from([1]).stage_c is False
    assert on.allow_browser_confirm is False  # host confirmation by default
    assert (
        settings_from({"stage_c": True, "allow_browser_confirm": 1}).allow_browser_confirm is False
    )
    assert (
        settings_from({"stage_c": True, "allow_browser_confirm": "true"}).allow_browser_confirm
        is False
    )
    assert settings_from({"stage_c": True, "allow_browser_confirm": True}).allow_browser_confirm


# --- issue: typed confirmation + host-confirmed operation ---------------------------------


def test_issue_needs_the_typed_word_and_a_confirmed_operation(env):
    body = issue_body(env.ws)
    with refused("grant_confirm_mismatch"):
        env.service.issue({**body, "confirm": "yes"})
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    assert view["kind"] == "grant_issue" and len(view["confirm_token"]) == 32
    with refused("grant_operation_refused", "host_operation_pending"):
        env.service.issue(body, op_id=view["id"])  # not confirmed yet
    with refused("grant_operation_refused", "host_not_acknowledged"):
        env.service.confirm_operation(
            view["id"], token=view["confirm_token"], acknowledged="true", instance=view["instance"]
        )
    with refused("grant_operation_refused", "host_operation_mismatch"):
        env.service.confirm_operation(
            view["id"], token="0" * 32, acknowledged=True, instance=view["instance"]
        )
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    result = env.service.issue(body, op_id=view["id"])
    assert result["grant"]["state"] == "active" and "replayed" not in result


def test_the_same_operation_and_body_return_the_original_result(env):
    body = issue_body(env.ws)
    first = confirm_and_issue(env.service, body)
    again = env.service.issue(body, op_id=first["op_id"])
    assert again["replayed"] is True
    assert again["historical"] is True and "historical" not in first  # #620 R2
    assert again["grant"]["grant_id"] == first["grant"]["grant_id"]
    assert len(env.service.list_view()["grants"]) == 1  # no second transition
    with refused("grant_operation_refused", "host_operation_mismatch"):
        env.service.issue({**body, "ttl_sec": 600}, op_id=first["op_id"])


def test_a_stale_operation_is_refused(env):
    body = issue_body(env.ws)
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    other = confirm_and_issue(env.service, issue_body(env.ws))  # the generation moved
    env.service.revoke(other["grant"]["grant_id"])
    with refused("grant_operation_refused"):
        env.service.issue(body, op_id=view["id"])  # replaced or stale: never applied
    assert len(env.service.list_view()["grants"]) == 1


def test_an_operation_of_another_kind_cannot_be_used(env):
    grant = issued(env)
    env.service.pause(grant["grant_id"])
    with pytest.raises(GrantConfirmation) as needed:
        env.service.resume(grant["grant_id"], {})
    view = needed.value.view
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    env.service.revoke(grant["grant_id"])
    with refused("grant_operation_refused", "host_operation_mismatch"):
        env.service.issue(issue_body(env.ws), op_id=view["id"])


def test_at_most_one_live_grant(env):
    issued(env)
    with refused("grant_already_active"):
        env.service.issue(issue_body(env.ws))


@pytest.mark.parametrize(
    ("unattended", "contained", "code"),
    [
        (False, True, "grant_run_checks_setting_off"),
        (True, False, "grant_run_checks_not_contained"),
        (False, False, "grant_run_checks_setting_off"),
    ],
)
def test_run_checks_needs_the_setting_and_a_verified_os_sandbox(
    tmp_path, unattended, contained, code
):
    service, *_rest = make_service(tmp_path, unattended=unattended, contained=contained)
    ws = _rest[-1]
    body = issue_body(ws)
    body["scope"]["tools"] = ["file_read", "run_checks"]
    with refused(code):
        service.issue(body)


def test_run_checks_is_issued_on_a_verified_os_sandbox(tmp_path):
    service, *_rest = make_service(tmp_path, contained=True)
    body = issue_body(_rest[-1])
    body["scope"]["tools"] = ["file_read", "run_checks"]
    assert "run_checks" in confirm_and_issue(service, body)["grant"]["tools"]


def test_issue_is_refused_while_killed_unhealthy_or_breaker_not_normal(env):
    env.breaker.level = "warning"
    with refused("grant_breaker_not_normal"):
        env.service.issue(issue_body(env.ws))
    env.breaker.level = "normal"
    env.authority.report_unhealthy("lifecycle_write_failed")
    with refused("grant_authority_unhealthy"):
        env.service.issue(issue_body(env.ws))


def test_issue_is_refused_after_the_kill(tmp_path):
    service, authority, *_rest = make_service(tmp_path)
    authority.kill_switch()
    with refused("grant_authority_killed"):
        service.issue(issue_body(_rest[-1]))


# --- revoke / pause / resume --------------------------------------------------------------


def test_revoke_is_one_press_and_always_available(env):
    grant = issued(env)
    env.authority.kill_switch()
    env.service._store._bad = "grant_store_write_failed"  # an unhealthy store
    out = env.service.revoke(grant["grant_id"])
    assert out["grant"]["state"] == "revoked"  # killed before: kill_switch wins the race
    again = env.service.revoke(grant["grant_id"])
    assert again["changed"] is False


def test_a_user_revoke_is_final(env):
    grant = issued(env)
    out = env.service.revoke(grant["grant_id"])
    assert (out["grant"]["state"], out["grant"]["reason"], out["grant"]["goal_state"]) == (
        "revoked",
        "user",
        "unmet",
    )
    with refused("grant_terminal"):
        env.service.resume(grant["grant_id"], {})
    with refused("grant_terminal"):
        env.service.pause(grant["grant_id"])


def test_pause_then_resume_with_the_operation(env):
    grant = issued(env)
    epoch = env.authority.epoch
    out = env.service.pause(grant["grant_id"])
    assert (out["grant"]["state"], out["grant"]["reason"]) == ("paused", "user")
    assert env.authority.epoch > epoch and out["grant"]["stop_epoch"] == env.authority.epoch
    with refused("grant_field_unknown"):
        env.service.resume(grant["grant_id"], {"confirm": "RESUME"})  # a pause needs no word
    resumed = confirm_and_resume(env.service, grant["grant_id"], {})
    assert resumed["grant"]["state"] == "active"
    assert env.service.pause(grant["grant_id"])["changed"] is True


def test_a_halt_resume_needs_the_typed_word_the_token_and_a_normal_breaker(env):
    grant = issued(env)
    env.breaker.level = "coherence"
    env.service.sync()
    view = env.service.view(grant["grant_id"])
    assert (view["state"], view["reason"]) == ("halted", "breaker_warning")
    with refused("grant_confirm_mismatch"):
        env.service.resume(grant["grant_id"], {})
    with refused("grant_still_dangerous"):
        env.service.resume(grant["grant_id"], {"confirm": "RESUME"})
    env.breaker.level = "normal"
    env.service.sync()
    assert env.service.view(grant["grant_id"])["state"] == "halted"  # NORMAL never releases
    out = confirm_and_resume(env.service, grant["grant_id"], {"confirm": "RESUME"})
    assert (out["grant"]["state"], out["grant"]["recheck"]) == ("active", True)


def test_a_resume_for_an_older_stop_is_stale_and_a_resent_one_returns_the_original(env):
    """ADR 0011 §9: halted (stop 41) → resume → halted again (stop 57); the re-sent first
    resume returns its original result and stop 57 stays."""
    grant = issued(env)
    env.breaker.level = "warning"
    env.service.sync()
    env.breaker.level = "normal"
    first = confirm_and_resume(env.service, grant["grant_id"], {"confirm": "RESUME"})
    assert first["grant"]["state"] == "active"
    env.breaker.level = "critical"
    env.service.sync()
    second_stop = env.service.view(grant["grant_id"])
    assert (
        second_stop["state"] == "halted"
        and second_stop["stop_epoch"] > first["grant"]["stop_epoch"]
    )
    env.breaker.level = "normal"
    replay = env.service.resume(grant["grant_id"], {"confirm": "RESUME"}, op_id=first["op_id"])
    assert replay["replayed"] is True
    assert env.service.view(grant["grant_id"])["state"] == "halted"  # the second stop stays
    # An operation asked for before the stop moved is stale when used after it.
    with pytest.raises(GrantConfirmation) as needed:
        env.service.resume(grant["grant_id"], {"confirm": "RESUME"})
    view = needed.value.view
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    env.service._apply(lambda g: state.charge(g, 1), [grant["grant_id"]])  # generation moves
    with refused("grant_operation_refused", "stale_operation"):
        env.service.resume(grant["grant_id"], {"confirm": "RESUME"}, op_id=view["id"])
    assert env.service.view(grant["grant_id"])["state"] == "halted"


def test_a_stop_between_request_and_consume_makes_the_resume_stale(env):
    grant = issued(env)
    env.service.pause(grant["grant_id"])
    with pytest.raises(GrantConfirmation) as needed:
        env.service.resume(grant["grant_id"], {})
    view = needed.value.view
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    env.service._apply(lambda g: state.charge(g, 1), [grant["grant_id"]])  # generation moves
    with refused("grant_operation_refused", "stale_operation"):
        env.service.resume(grant["grant_id"], {}, op_id=view["id"])
    assert env.service.view(grant["grant_id"])["state"] == "paused"
    # a stop in between: the resume body itself no longer fits (a halt needs the word)
    env.service.on_breaker_transition("normal", "warning")
    with refused("grant_confirm_mismatch"):
        env.service.resume(grant["grant_id"], {}, op_id=view["id"])


# --- automatic stops ------------------------------------------------------------------------


def test_the_kill_switch_revokes_every_grant(env):
    grant = issued(env)
    env.authority.kill_switch()
    env.service.sync()
    view = env.service.view(grant["grant_id"])
    assert (view["state"], view["reason"]) == ("revoked", "kill_switch")


def test_an_unhealthy_authority_revokes(env):
    grant = issued(env)
    env.authority.report_unhealthy("epoch_store_write_failed")
    env.service.sync()
    assert env.service.view(grant["grant_id"])["reason"] == "unhealthy"


def test_the_breaker_hook_halts_in_memory_and_advances_the_epoch(env):
    grant = issued(env)
    epoch = env.authority.epoch
    env.service.on_breaker_transition("normal", "critical")
    view = env.service.view(grant["grant_id"])
    assert (view["state"], view["reason"]) == ("halted", "breaker_critical")
    assert env.authority.epoch > epoch
    env.service.on_breaker_transition("critical", "normal")  # NORMAL releases nothing
    assert env.service.view(grant["grant_id"])["state"] == "halted"


def test_the_breaker_hook_does_no_io(env, monkeypatch):
    """Under the engine's lock: memory only; the write runs on the writer thread."""
    grant = issued(env)
    threads = []
    real_write, real_verify = env.service._store.write, env.service._store.verify
    monkeypatch.setattr(
        env.service._store,
        "write",
        lambda entries: threads.append(threading.current_thread()) or real_write(entries),
    )
    monkeypatch.setattr(
        env.service._store,
        "verify",
        lambda: threads.append(threading.current_thread()) or real_verify(),
    )
    narrowed = []
    real_narrow = env.authority.narrow
    monkeypatch.setattr(
        env.authority, "narrow", lambda reason: narrowed.append(reason) or real_narrow(reason)
    )
    epoch = env.authority.epoch
    env.service.on_breaker_transition("normal", "warning")
    assert env.authority.epoch > epoch and narrowed == []  # deferred: no epoch write here
    assert env.service.view(grant["grant_id"])["state"] == "halted"  # at once, in memory
    assert env.service.stop(5.0) == "stopped"
    assert threads and threading.current_thread() not in threads
    assert all(t.name == "grants-persist" for t in threads)


def test_a_clock_anomaly_revokes_and_the_ttl_expires(env):
    grant = issued(env)
    env.clocks.jump_wall(-120)
    env.service.sync()
    assert env.service.view(grant["grant_id"])["reason"] == "clock_anomaly"
    second = issued(env, ttl_sec=600)
    env.clocks.sleep(601)  # a sleep counts
    env.service.sync()
    view = env.service.view(second["grant_id"])
    assert (view["state"], view["reason"], view["ttl_left_sec"]) == ("expired", "ttl", 0)


def test_an_unreadable_clock_revokes(env):
    grant = issued(env)
    env.clocks.broken = True
    env.service.sync()
    env.clocks.broken = False
    assert env.service.view(grant["grant_id"])["reason"] == "clock_anomaly"


def test_a_session_reset_revokes(env):
    grant = issued(env)
    env.service.session_reset()
    view = env.service.view(grant["grant_id"])
    assert (view["state"], view["reason"]) == ("revoked", "session_reset")


def test_every_stop_advances_the_epoch(env):
    for stop in (
        lambda g: env.service.pause(g),
        lambda g: env.service.revoke(g),
    ):
        grant = issued(env)
        before = env.authority.epoch
        stop(grant["grant_id"])
        assert env.authority.epoch == before + 1
        if env.service.view(grant["grant_id"])["state"] != "revoked":
            env.service.revoke(grant["grant_id"])


# --- verify -----------------------------------------------------------------------------------


def test_verify_only_while_awaiting_and_active(env):
    grant = issued(env)
    with refused("grant_not_awaiting"):
        env.service.verify(grant["grant_id"], {"decision": "confirm"})
    env.service.claim_complete(grant["grant_id"])
    with refused("grant_body_invalid"):
        env.service.verify(grant["grant_id"], {"decision": "confirm", "x": 1})
    env.service.pause(grant["grant_id"])
    with refused("grant_not_active"):
        env.service.verify(grant["grant_id"], {"decision": "confirm"})
    confirm_and_resume(env.service, grant["grant_id"], {})
    with pytest.raises(GrantConfirmation):  # a verification is a host operation too
        env.service.verify(grant["grant_id"], {"decision": "confirm"})
    out = confirm_and_verify(env.service, grant["grant_id"], "confirm")
    assert (out["grant"]["state"], out["grant"]["goal_state"], out["recorded_only"]) == (
        "ended",
        "complete",
        False,
    )


def test_a_late_verification_is_recorded_only_and_the_first_one_stays(env):
    grant = issued(env)
    env.service.claim_complete(grant["grant_id"])
    env.service.revoke(grant["grant_id"])
    out = confirm_and_verify(env.service, grant["grant_id"], "confirm")
    assert out["recorded_only"] is True
    assert (
        out["grant"]["state"],
        out["grant"]["goal_state"],
        out["grant"]["late_verification"],
    ) == ("revoked", "unmet", "pass")
    with refused("grant_terminal"):
        env.service.verify(grant["grant_id"], {"decision": "reject"})
    assert env.service.view(grant["grant_id"])["late_verification"] == "pass"


# --- lifecycle events, outbox, goal text --------------------------------------------------------


def test_every_transition_is_one_lifecycle_event_with_codes_only(env):
    grant = issued(env)
    env.service.pause(grant["grant_id"])
    confirm_and_resume(env.service, grant["grant_id"], {})
    env.service.claim_complete(grant["grant_id"])
    confirm_and_verify(env.service, grant["grant_id"], "reject")
    env.service.revoke(grant["grant_id"])
    mine = [e for e in events(env.recorder) if e["subject_id"] == grant["grant_id"]]
    assert [(e["subject_kind"], e["reason_code"]) for e in mine] == [
        ("grant", "grant_issued"),
        ("goal", "goal_issued"),
        ("grant", "paused_user"),
        ("grant", "grant_resumed"),
        ("goal", "goal_claimed"),
        ("goal", "verification_rejected"),
        ("grant", "revoked_user"),
        ("goal", "goal_unmet"),
    ]
    paused = mine[2]
    assert (
        paused["epoch"] == env.service.view(grant["grant_id"])["stop_epoch"] - 1
        or paused["epoch"] > 0
    )


def test_stop_class_transitions_use_the_existing_stop_notice_keys(env):
    grant = issued(env)
    env.service.revoke(grant["grant_id"])
    assert env.recorder.drain() == "clean"
    keys = {item["key"] for item in env.recorder._items}
    assert "stop|grant|*|revoked_user" in keys
    assert "stop|goal|*|goal_unmet" in keys
    issued_key = next(k for k in keys if k.startswith("grant|"))  # not a stop: informational
    assert grant["grant_id"] in issued_key
    second = issued(env)
    env.service.revoke(second["grant_id"])
    env.recorder.drain()
    revoked = [i for i in env.recorder._items if i["key"] == "stop|grant|*|revoked_user"]
    assert len(revoked) == 1 and revoked[0]["count"] == 2  # one notice per stop code
    assert outbox_module.critical(
        {"subject_kind": "goal", "to": "awaiting_verification", "reason_code": "goal_claimed"}
    )


def test_the_goal_text_appears_only_in_the_store(env, caplog):
    caplog.set_level(logging.DEBUG)
    grant = issued(env)
    env.service.pause(grant["grant_id"])
    env.service.on_breaker_transition("normal", "warning")
    env.service.revoke(grant["grant_id"])
    try:
        env.service.verify(grant["grant_id"], {"decision": "nope"})
    except GrantRefused as error:
        assert MARKER not in repr(error) and MARKER not in str(error.extra)
    env.recorder.drain()
    surfaces = {
        "events": json.dumps(events(env.recorder)),
        "outbox": json.dumps(env.recorder.outbox()),
        "list": json.dumps(env.service.list_view()),
        "view": json.dumps(env.service.view(grant["grant_id"])),
        "status": json.dumps(env.service.status()),
        "logs": caplog.text,
        "repr": repr(env.service._grants),
    }
    for name, text in surfaces.items():
        assert MARKER not in text, name
        assert str(env.ws) not in text or name == "repr", name  # no workspace path in views
    store = (env.tmp / "data" / "grants.json").read_text()
    assert MARKER in store


def test_the_goal_view_is_for_the_issuer_only(env):
    result = confirm_and_issue(env.service, issue_body(env.ws, goal=f"x {MARKER}"))
    grant_id, token = result["grant"]["grant_id"], result["goal_view_token"]
    with refused("grant_view_refused"):
        env.service.goal_view(grant_id, confirm="show", token=token)
    with refused("grant_view_refused"):
        env.service.goal_view(grant_id, confirm="SHOW", token="0" * 32)
    with refused("grant_view_refused"):
        env.service.goal_view(grant_id, confirm="SHOW", token=None)
    with refused("grant_unknown"):
        env.service.goal_view("f" * 32, confirm="SHOW", token=token)
    shown = env.service.goal_view(grant_id, confirm="SHOW", token=token)
    assert MARKER in shown["goal"]
    restarted, *_ = make_service(env.tmp)
    with refused("grant_view_refused"):
        restarted.goal_view(grant_id, confirm="SHOW", token=token)  # a restart ends it


# --- shutdown, threads ----------------------------------------------------------------------------


def test_the_stop_step_reports_durable_only_when_written(env, monkeypatch):
    grant = issued(env)
    assert env.service.stop(1.0) == "stopped"
    gate = threading.Event()
    entered = threading.Event()
    real = env.service._store.write

    def slow(entries):
        entered.set()
        gate.wait(10)
        return real(entries)

    monkeypatch.setattr(env.service._store, "write", slow)
    env.service.on_breaker_transition("normal", "warning")  # a change: a write is wanted
    assert entered.wait(5)
    assert env.service.status()["dirty"] is True
    assert env.service.stop(0.05) == "pending"  # a write already entered is not waited out
    gate.set()
    assert env.service.stop(5.0) == "stopped"
    assert env.service.status()["dirty"] is False
    env.service.revoke(grant["grant_id"])
    env.service._store._bad = "grant_store_write_failed"
    assert env.service.stop(1.0) == "unhealthy"


def test_the_service_starts_no_thread(tmp_path):
    before = {t.ident for t in threading.enumerate()}
    service, authority, _recorder, _clocks, _breaker, ws = make_service(tmp_path)
    grant = confirm_and_issue(service, issue_body(ws))["grant"]
    service.pause(grant["grant_id"])
    service.sync()
    service.stop(1.0)
    authority.flush(5.0)
    new = [
        t.name
        for t in threading.enumerate()
        if t.ident not in before and t.is_alive() and not t.name.startswith("authority-settle")
    ]
    assert new == []


def test_the_goal_view_route_is_never_cached_even_without_the_server_headers(env):
    from flask import Flask

    from stiracore.grants.routes import GOAL_VIEW_HEADER, create_grants_blueprint

    result = confirm_and_issue(env.service, issue_body(env.ws))
    app = Flask(__name__)
    app.register_blueprint(create_grants_blueprint(lambda: env.service))
    client = app.test_client()
    url = f"/api/grants/{result['grant']['grant_id']}/goal?confirm=SHOW"
    cross = client.get(
        url,
        headers={GOAL_VIEW_HEADER: result["goal_view_token"], "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403  # refused before the one-time token is spent
    shown = client.get(url, headers={GOAL_VIEW_HEADER: result["goal_view_token"]})
    assert shown.status_code == 200 and shown.headers["Cache-Control"] == "no-store"
    refused = client.get(url)
    assert refused.status_code == 403 and refused.headers["Cache-Control"] == "no-store"


# --- fix round 1: the issue fence, durability first, revoke before I/O --------------------


def _receipt():
    from stiracore.authority import Receipt

    return Receipt(event_id="op:" + "a" * 32 + ":decision", seq=1, hash="b" * 64)


def _between_consume_and_publish(env, monkeypatch, interleave):
    """Wrap the real operation consume: right after it succeeded, ``interleave()`` runs."""
    real = env.service._ops.consume

    def consume(*args, **kwargs):
        real(*args, **kwargs)
        interleave()

    monkeypatch.setattr(env.service._ops, "consume", consume)


STOPS = [
    lambda env: (
        env.authority.breaker_transition("normal", "warning"),
        env.service.on_breaker_transition("normal", "warning"),
    ),
    lambda env: env.service.session_reset(),
    lambda env: env.authority.kill_switch(),
    lambda env: env.authority.report_unhealthy("lifecycle_write_failed"),
]
STOP_IDS = ["breaker", "session_reset", "kill", "unhealthy"]


@pytest.mark.parametrize("stop", STOPS[:2], ids=STOP_IDS[:2])
def test_a_stop_after_the_preliminary_checks_aborts_the_issuance(env, monkeypatch, stop):
    """Astra B1: a breaker departure, /new, the kill or an unhealthy authority between the
    preliminary checks and the publication never leaves an active grant."""
    _between_consume_and_publish(env, monkeypatch, lambda: stop(env))
    with refused("grant_issue_aborted_epoch"):
        confirm_and_issue(env.service, issue_body(env.ws))
    assert env.service.status()["live"] == 0
    assert not [g for g in env.service.list_view()["grants"] if g["state"] == "active"]


@pytest.mark.parametrize("stop", STOPS, ids=STOP_IDS)
def test_a_stop_after_the_durable_write_aborts_the_issuance(env, monkeypatch, stop):
    """The final fence (Astra N2): a stop after the durable write, right before the
    publication, aborts it."""
    real = env.service._store.write
    fired = []

    def write(entries):
        ok = real(entries)
        if not fired and any(item["restart_from"] == "active" for item in entries):
            fired.append(1)
            stop(env)  # after the write, before the publication
        return ok

    monkeypatch.setattr(env.service._store, "write", write)
    with refused("grant_issue_aborted_epoch"):
        confirm_and_issue(env.service, issue_body(env.ws))
    assert env.service.status()["live"] == 0
    env.service.stop(5.0)
    stored = json.loads((env.tmp / "data" / "grants.json").read_text())[-1]["grants"]
    assert not [item for item in stored if item["restart_from"] == "active"]


def test_nothing_is_usable_before_the_durable_write_returned(env, monkeypatch):
    """Astra B2: while the issue's write is held, no record is published; a failed write
    leaves no live grant and reports the failure."""
    gate, entered = threading.Event(), threading.Event()
    real = env.service._store.write

    def write(entries):
        if any(item["restart_from"] == "active" for item in entries):
            entered.set()
            gate.wait(10)
            raise_os = True
        else:
            raise_os = False
        if raise_os:
            from stiracore.authority import private_file

            original = private_file.replace
            private_file.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
            try:
                return real(entries)
            finally:
                private_file.replace = original
        return real(entries)

    monkeypatch.setattr(env.service._store, "write", write)
    outcome = {}

    def run():
        try:
            confirm_and_issue(env.service, issue_body(env.ws))
        except GrantRefused as error:
            outcome["code"] = error.code

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    assert env.service.status()["live"] == 0  # not published while it is written
    assert env.service.list_view()["grants"] == []
    gate.set()
    worker.join(10)
    assert outcome["code"] == "grant_store_unhealthy"
    assert env.service.status()["live"] == 0


def test_an_issue_whose_write_does_not_return_in_time_is_refused(env, monkeypatch):
    import stiracore.grants.service as service_module

    monkeypatch.setattr(service_module, "ISSUE_WRITE_WAIT_SEC", 0.2)
    gate = threading.Event()
    real = env.service._store.write

    def write(entries):
        if any(item["restart_from"] == "active" for item in entries):
            gate.wait(10)
        return real(entries)

    monkeypatch.setattr(env.service._store, "write", write)
    with refused("grant_store_timeout"):
        confirm_and_issue(env.service, issue_body(env.ws))
    gate.set()
    assert env.service.stop(5.0) == "stopped"
    assert env.service.status()["live"] == 0


def test_revoke_stops_in_memory_and_moves_the_epoch_before_a_hung_write(env, monkeypatch):
    """Astra B3: the store write blocked → revoke returns, the grant is stopped, a permit
    issued before is refused."""
    grant = issued(env)
    record = env.service.authority_record(grant["grant_id"])
    permit = env.authority.issue_permit(record, kind="goal", receipt=_receipt())
    gate, entered = threading.Event(), threading.Event()
    real = env.service._store.write

    def write(entries):
        entered.set()
        gate.wait(10)
        return real(entries)

    monkeypatch.setattr(env.service._store, "write", write)
    monkeypatch.setattr(env.service._store, "verify", lambda: None)
    env.service.pause(grant["grant_id"])  # a write is now hung
    assert entered.wait(5)
    started = time.monotonic()
    out = env.service.revoke(grant["grant_id"])
    assert time.monotonic() - started < 3.0
    assert out["grant"]["state"] == "revoked" and out["persisted"] is False
    from stiracore.authority import AuthorityError

    with pytest.raises(AuthorityError):
        env.authority.consume(permit, record, kind="goal")
    gate.set()
    assert env.service.stop(5.0) == "stopped"


def test_the_kill_reaches_the_grant_at_once(env):
    """Opus N2: the authority's kill listener — no look needed."""
    grant = issued(env)
    env.authority.kill_switch()
    current = env.service._grants[grant["grant_id"]]
    assert (current.status, current.reason) == ("revoked", "kill_switch")


def test_check_now_applies_the_ttl_before_a_record_or_a_claim(env):
    """Opus N3 / Astra N1: authority_record and claim_complete go through check_now."""
    grant = issued(env, ttl_sec=60)
    env.clocks.tick(61)
    record = env.service.authority_record(grant["grant_id"])
    assert record.state == "expired"
    second = issued(env, ttl_sec=60)
    env.clocks.tick(61)
    with refused("grant_not_active"):
        env.service.claim_complete(second["grant_id"])


def test_an_unreadable_breaker_halts(env):
    grant = issued(env)

    def broken():
        raise RuntimeError("engine")

    env.service._breaker = broken
    env.service.sync()
    assert env.service.view(grant["grant_id"])["state"] == "halted"


def test_a_store_problem_found_by_the_look_is_a_notice(env):
    grant = issued(env)
    path = env.tmp / "data" / "grants.json"
    text = path.read_text()
    path.unlink()
    path.write_text(text)
    path.chmod(0o600)
    env.service.sync()
    assert env.service.stop(5.0) == "unhealthy"
    codes = [(e["subject_kind"], e["reason_code"]) for e in events(env.recorder)]
    assert ("grants", "grant_store_replaced") in codes
    assert env.service.view(grant["grant_id"])["reason"] == "unhealthy"


def test_a_look_writes_nothing_without_a_state_change(env, monkeypatch):
    """Opus N7: clock readings stay in memory; a look writes the store only on a change."""
    issued(env)
    assert env.service.stop(5.0) == "stopped"
    writes = []
    real = env.service._store.write
    monkeypatch.setattr(env.service._store, "write", lambda e: writes.append(1) or real(e))
    for _ in range(3):
        env.clocks.tick(5)
        env.service.list_view()
    assert env.service.stop(5.0) == "stopped"
    assert writes == []


def test_the_goal_view_token_is_one_time_and_never_replayed(env):
    result = confirm_and_issue(env.service, issue_body(env.ws))
    grant_id, token = result["grant"]["grant_id"], result["goal_view_token"]
    assert env.service.goal_view(grant_id, confirm="SHOW", token=token)["goal"]
    with refused("grant_view_refused"):
        env.service.goal_view(grant_id, confirm="SHOW", token=token)
    replay = env.service.issue(issue_body(env.ws), op_id=result["op_id"])
    assert replay["replayed"] is True and "goal_view_token" not in replay


def test_without_a_tool_call_tracker_every_operation_is_refused(tmp_path):
    from stiracore.grants.service import GrantService
    from tests.grants_helpers import FakeClocks

    service, authority, recorder, _clocks, _breaker, ws = make_service(tmp_path)
    bare = GrantService(
        settings=AutonomySettings(stage_c=True, allow_browser_confirm=True),
        data_dir=None,
        authority=authority,
        recorder=recorder,
        clock=FakeClocks(),
        write_roots=lambda: [ws],
    )
    bare.start()
    with refused("grant_operation_refused", "host_channel_unavailable"):
        bare.issue(issue_body(ws))
    service._tool_call_running = lambda: True
    with refused("grant_operation_refused", "tool_call_running"):
        service.issue(issue_body(ws))


# --- fix round 2: host channel required, verification fence ------------------------------


def test_without_a_host_channel_issue_resume_and_verify_are_refused(tmp_path, caplog):
    """Astra r2 B1: the browser channel without the user's explicit choice refuses."""
    from dataclasses import replace as dc_replace

    service, *_rest = make_service(tmp_path, browser=False)
    ws = _rest[-1]
    assert service.confirm_layer == "unavailable"
    with refused("grant_operation_refused", "host_channel_required") as caught:
        service.issue(issue_body(ws))
    assert "allow_browser_confirm" in caught.value.extra["hint"]
    service.settings = dc_replace(service.settings, allow_browser_confirm=True)
    grant = confirm_and_issue(service, issue_body(ws))["grant"]
    service.settings = dc_replace(service.settings, allow_browser_confirm=False)
    assert service.pause(grant["grant_id"])["changed"] is True  # narrowings stay one press
    with refused("grant_operation_refused", "host_channel_required"):
        service.resume(grant["grant_id"], {})
    service.settings = dc_replace(service.settings, allow_browser_confirm=True)
    confirm_and_resume(service, grant["grant_id"], {})
    service.claim_complete(grant["grant_id"])
    service.settings = dc_replace(service.settings, allow_browser_confirm=False)
    with refused("grant_operation_refused", "host_channel_required"):
        service.verify(grant["grant_id"], {"decision": "confirm"})
    with refused("grant_operation_refused", "host_channel_required"):
        service.confirm_operation("a" * 32, token="b" * 32, acknowledged=True, instance="c" * 16)
    assert service.revoke(grant["grant_id"])["grant"]["state"] == "revoked"
    caplog.set_level(logging.WARNING)
    service.settings = dc_replace(service.settings, allow_browser_confirm=True)
    assert service.confirm_layer == "browser_weak"
    confirm_and_issue(service, issue_body(ws))
    assert "browser confirmation (the weaker layer" in caplog.text


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_a_verification_crossing_the_ttl_is_recorded_only(env, monkeypatch, decision):
    """Astra r2 B2: the clocks pass the TTL after the operation was consumed and before the
    verification applies → recorded only on the expired grant, never complete / active."""
    grant = issued(env, ttl_sec=60)
    env.service.claim_complete(grant["grant_id"])
    _between_consume_and_publish(env, monkeypatch, lambda: env.clocks.tick(61))
    out = confirm_and_verify(env.service, grant["grant_id"], decision)
    assert out["recorded_only"] is True
    assert (out["grant"]["state"], out["grant"]["goal_state"]) == ("expired", "unmet")
    assert out["grant"]["late_verification"] == ("pass" if decision == "confirm" else "fail")


def test_a_verification_after_a_stop_since_its_step_began_does_not_apply(env, monkeypatch):
    """The final fence: an epoch move between the fresh look and the commit (here inside
    the breaker read) → the verification is not applied (stale), the goal stays waiting."""
    grant = issued(env)
    env.service.claim_complete(grant["grant_id"])
    real = env.service._breaker
    armed = []

    def breaker():
        if armed == [1]:  # the verification's own fresh read, after the consume
            armed.append(2)
            env.authority.narrow("rules_saved")
        return real()

    env.service._breaker = breaker
    _between_consume_and_publish(env, monkeypatch, lambda: armed.append(1))
    with refused("grant_operation_refused", "stale_operation"):
        confirm_and_verify(env.service, grant["grant_id"], "confirm")
    view = env.service.view(grant["grant_id"])
    assert (view["state"], view["goal_state"]) == ("active", "awaiting_verification")


def test_a_kill_after_the_consume_records_the_verification_late(env, monkeypatch):
    grant = issued(env)
    env.service.claim_complete(grant["grant_id"])
    _between_consume_and_publish(env, monkeypatch, lambda: env.authority.kill_switch())
    out = confirm_and_verify(env.service, grant["grant_id"], "confirm")
    assert out["recorded_only"] is True and out["grant"]["goal_state"] == "unmet"
    assert out["grant"]["reason"] == "kill_switch"


def test_an_unexpected_writer_failure_releases_the_writer_and_latches(env, monkeypatch):
    """Astra r2 N3: a raise inside the writer clears its ownership and latches health."""
    issued(env)
    assert env.service.stop(5.0) == "stopped"

    def boom(entries):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(env.service._store, "write", boom)
    env.service.on_breaker_transition("normal", "warning")  # a change: a write is wanted
    assert env.service._wait_idle(5.0) is True
    assert env.service.unhealthy_reason == "grant_persist_failed"


# --- W4.5 (#620 W5-2, W5-3, R1) -----------------------------------------------------------


def _until(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_a_ttl_that_runs_out_between_the_last_look_and_the_commit_is_recorded_only(
    env, monkeypatch, decision
):
    """#620 W5-2: the clocks are read again inside the fenced commit — a TTL that runs
    out after the verification's own last look (here: right before ``commit_at_epoch``
    takes the leaf lock) is recorded only, never ``complete`` / an active goal."""
    grant = issued(env, ttl_sec=60)
    env.service.claim_complete(grant["grant_id"])
    real = env.authority.commit_at_epoch

    def commit_at_epoch(commit, *, expected=None):
        env.clocks.tick(61)  # the barrier: after the last look, before the callback
        return real(commit, expected=expected)

    monkeypatch.setattr(env.authority, "commit_at_epoch", commit_at_epoch)
    out = confirm_and_verify(env.service, grant["grant_id"], decision)
    assert out["recorded_only"] is True
    raw = env.service._get(grant["grant_id"])  # the raw record, no further look
    assert (raw.status, raw.reason, raw.goal_status) == ("expired", "ttl", "unmet")
    assert raw.late_verification == ("pass" if decision == "confirm" else "fail")


def test_a_blocking_recorder_delays_neither_the_kill_persistence_nor_its_cancellation(env):
    """#620 W5-3: the grants' kill listener revokes in memory only; its lifecycle posts
    and its write request (and the authority's own records) come after the kill's
    persistence attempt and cancel kickoff."""
    from stiracore.authority import GrantRecord, Receipt

    grant = issued(env)
    authority = env.authority
    record = GrantRecord(
        kind="yolo",
        grant_id="d" * 32,
        incarnation=authority.incarnation,
        generation=1,
        state="active",
    )
    receipt = Receipt(event_id=f"op:{'e' * 32}:decision", seq=1, hash="f" * 64)
    ran = threading.Event()
    authority.consume(
        authority.issue_permit(record, kind="yolo", receipt=receipt),
        record,
        kind="yolo",
        on_cancel=lambda _r: ran.set(),
    )
    entered, gate = threading.Event(), threading.Event()
    real_post = env.recorder.post

    def blocked(event):
        entered.set()
        gate.wait(60)
        return real_post(event)

    env.recorder.post = blocked
    killer = threading.Thread(target=authority.kill_switch)
    killer.start()
    try:
        assert ran.wait(10)  # cancelled while the recorder blocks
        assert authority.latches.is_set("kill")  # the kill latch was persisted first
        assert entered.wait(10)  # the posts came after both
        raw = env.service._get(grant["grant_id"])
        assert (raw.status, raw.reason) == ("revoked", "kill_switch")  # in memory at once
    finally:
        gate.set()
        killer.join(30)
    assert not killer.is_alive()
    codes = [e["reason_code"] for e in events(env.recorder)]
    assert "kill_switch" in codes and "revoked_kill_switch" in codes
    assert settle(env.service) == "stopped"


def test_an_unexpected_writer_failure_revokes_the_live_grants_at_once(env, monkeypatch):
    """#620 R1: not at the next checked read — no look runs in this test."""
    grant = issued(env)
    assert env.service.stop(5.0) == "stopped"

    def boom(entries):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(env.service._store, "write", boom)
    env.service.on_breaker_transition("normal", "warning")  # a change: a write is wanted
    assert _until(lambda: env.service._get(grant["grant_id"]).status == "revoked")
    raw = env.service._get(grant["grant_id"])
    assert raw.reason == "unhealthy"
    assert _until(lambda: env.service._wait_idle(0.1))
