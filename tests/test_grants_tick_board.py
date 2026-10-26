"""W6d (#296): the heartbeat start's block on the Stage board (``/api/activity``) and the
code lists the pages keep — an allow-list of codes and counts, only for a flagged grant."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_grants_tick import _issue_flagged, fire, fire_and_wait, scripted, text_only, tick_env, w

from stiracore.observability import activity
from stiracore.scheduler import grant_tick

ROOT = Path(__file__).resolve().parents[1]
MARKER = "w6dMarker-5e1c9a7d3b204f68"


def _view(**over):
    view = {
        "short": "abababab",
        "state": "active",
        "goal_state": "active",
        "headline": {"subject": "grant", "state": "active"},
        "issued_wall": 10.0,
        "heartbeat_start": True,
    }
    view.update(over)
    return view


TICK = {
    "spacing_sec": 300,
    "started": 2,
    "last": "min_spacing",
    "skipped": {"min_spacing": 3, "turn_busy": 1},
}


def test_the_tick_block_is_exactly_the_allow_list():
    board = {
        "iteration": {"state": "idle", "number": 2, "end": "done"},
        "outcomes": [],
        "tick_costs": [
            {"number": 1, "model_calls": 1, "tokens_in": 5, "tokens_out": 1, "wall_ms": 10},
            {"number": 2, "model_calls": 2, "tokens_in": 9, "tokens_out": 4, "cache_read": 3,
             "wall_ms": 20, "extra": MARKER},
        ],
    }  # fmt: skip
    out = activity.grant_view(_view(), board, tick=TICK)
    assert out["tick"] == {
        "on": True,
        "started": 2,
        "spacing_sec": 300,
        "last": "min_spacing",
        "skipped": {"min_spacing": 3, "turn_busy": 1},
        "cost": {
            "number": 2,
            "model_calls": 2,
            "tokens_in": 9,
            "tokens_out": 4,
            "cache_read": 3,
            "wall_ms": 20,
        },
    }


def test_no_flag_or_no_ticker_no_tick_key():
    assert "tick" not in activity.grant_view(_view(heartbeat_start=False), None, tick=TICK)
    plain = _view()
    del plain["heartbeat_start"]
    assert "tick" not in activity.grant_view(plain, None, tick=TICK)
    assert "tick" not in activity.grant_view(_view(), None)
    assert activity.grant_view(plain, None) == activity.grant_view(plain, None, tick=None)


@pytest.mark.parametrize(
    "slot",
    ["last", "skipped_key", "skipped_value", "started", "cost"],
)
def test_a_marker_in_any_slot_is_never_in_the_block(slot):
    tick = json.loads(json.dumps(TICK))
    board = {"tick_costs": [{"tokens_in": 1, "tokens_out": 1}]}
    if slot == "last":
        tick["last"] = MARKER
    elif slot == "skipped_key":
        tick["skipped"][MARKER] = 1
    elif slot == "skipped_value":
        tick["skipped"]["turn_busy"] = MARKER
    elif slot == "started":
        tick["started"] = MARKER
    else:
        board["tick_costs"][0]["tokens_out"] = MARKER
        board["tick_costs"][0][MARKER] = 1
    out = activity.grant_view(_view(), board, tick=tick)
    assert MARKER not in json.dumps(out)


def test_the_pages_tick_codes_are_the_servers():
    src = (ROOT / "dashboard/stage/js/activity.js").read_text(encoding="utf-8")
    body = re.search(r"const TICK_SKIPS = new Set\(\[([^\]]*)\]\)", src).group(1)
    assert set(re.findall(r"'([a-z_]+)'", body)) == set(grant_tick.SKIP_REASONS)
    panel = (ROOT / "dashboard/static/js/settings_grants.js").read_text(encoding="utf-8")
    body = re.search(r"const TICK_SKIPS = \[([^\]]*)\]", panel).group(1)
    assert re.findall(r'"([a-z_]+)"', body) == list(grant_tick.SKIP_REASONS)
    for lang in ("en", "ja"):
        catalog = json.loads(
            (ROOT / f"dashboard/static/i18n/catalog.{lang}.json").read_text(encoding="utf-8")
        )
        for reason in grant_tick.SKIP_REASONS:
            assert f"grants.tick_skip_{reason}" in catalog, (lang, reason)


def test_the_board_read_of_a_real_ticker(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    assert fire_and_wait(env) == "started"
    env.healthy.value = False
    fire(env)
    record = env.service.board_record()
    out = activity.grant_view(
        env.service.board_view(record),
        env.executor.board(record.grant_id),
        tick=env.ticker.board(record.grant_id),
    )
    assert out["tick"]["started"] == 1
    assert out["tick"]["last"] == "heartbeat_unhealthy"
    assert out["tick"]["cost"]["tokens_in"] == 14
    text = json.dumps(out)
    assert "tidy the notes" not in text and str(env.sub) not in text


def test_an_unflagged_grant_on_a_ticking_server_shows_no_tick(tmp_path):
    env = tick_env(tmp_path)
    env.service.revoke(env.grant_id)
    _issue_flagged(env, flag=False)
    record = env.service.board_record()
    out = activity.grant_view(env.service.board_view(record), env.executor.board(record.grant_id))
    assert "tick" not in out


@pytest.mark.parametrize("code", ["heartbeat_off", "scheduler_off"])
def test_the_unavailable_code_is_allow_listed(code):
    out = activity.grant_view(_view(), None, tick={**TICK, "unavailable": code})
    assert out["tick"].get("unavailable") == code
    odd = activity.grant_view(_view(), None, tick={**TICK, "unavailable": MARKER})
    assert "unavailable" not in odd["tick"] and MARKER not in json.dumps(odd)
