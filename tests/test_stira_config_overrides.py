"""``stira_config_overrides`` and the running chat server (#272).

``stira_server`` used to build its ``StiraConfig`` from ``critical_action`` and the
identity anchors only, so every other saved override was shown by ``/api/config`` and
ignored by the engine. Measured with a real ``create_app`` and the engine it builds.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from stiracore.config import GatewayConfig
from stiracore.core import StiraConfig
from stiracore.observability.config_api import ConfigStore
from tests.provider_fakes import FakeModel, text_turn

# Distinct from the defaults and valid together.
DELIVERED_AT_START = {
    "bc_window_size": 8,
    "decay_min_interval_sec": 30.0,
    "critical_cooldown_turns": 5,
    "sycophancy_threshold": 0.8,
    "sycophancy_window_size": 10,
    "protected_layers": [2, 3, 4, 5],
    "hitl_timeout_sec": 60.0,
    "hitl_max_edit_rounds": 2,
}
# Copied onto the running engine before every turn (the entry thresholds and their exits).
DELIVERED_EACH_TURN = {
    "stability_warning": 0.55,
    "stability_critical": 0.35,
    "bc_threshold": 0.65,
    "stability_warning_exit": 0.65,
    "stability_critical_exit": 0.5,
    "bc_exit_threshold": 0.75,
}
# The server's own choices: a saved value never reaches the engine (separate decision, #272).
SERVER_FIXED = {"critical_action": "readonly", "blocked_tool_names": ["probe_tool"]}


class _Live:
    """The engine's config as it is now: ``apply_breaker_thresholds`` replaces the object."""

    def __init__(self, engine):
        self._engine = engine

    @property
    def current(self):
        return self._engine.config

    def __getattr__(self, name):
        return getattr(self._engine.config, name)


def _start(tmp_path, monkeypatch, overrides):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
    model = FakeModel([text_turn("one"), text_turn("two")])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    agents = []
    real_agent = stira_server.NXPAgent

    class Recording(real_agent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            agents.append(self)

    monkeypatch.setattr(stira_server, "NXPAgent", Recording)
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"stira_config_overrides": overrides}), encoding="utf-8"
    )
    app = stira_server.create_app(config_store=store)
    return app, _Live(agents[0].engine)


def _plain(value):
    return sorted(value) if isinstance(value, (set, frozenset)) else value


def test_every_delivered_override_reaches_the_engine(tmp_path, monkeypatch):
    _, config = _start(tmp_path, monkeypatch, {**DELIVERED_AT_START, **DELIVERED_EACH_TURN})
    for name, value in {**DELIVERED_AT_START, **DELIVERED_EACH_TURN}.items():
        assert _plain(getattr(config, name)) == value, name
    # What the server pins stays pinned.
    assert config.critical_action == "halt"
    assert config.identity_anchors == StiraConfig().identity_anchors


def test_critical_action_and_blocked_tool_names_stay_the_servers(tmp_path, monkeypatch):
    # Pinned (#272): a separate decision. The saved values are not given to the engine.
    _, config = _start(tmp_path, monkeypatch, SERVER_FIXED)
    assert config.critical_action == "halt"
    assert "probe_tool" not in config.blocked_tool_names
    assert "memory_write" in config.blocked_tool_names


def test_the_config_view_says_what_the_engine_applies(tmp_path, monkeypatch):
    app, _ = _start(
        tmp_path, monkeypatch, {**DELIVERED_AT_START, **DELIVERED_EACH_TURN, **SERVER_FIXED}
    )
    rows = app.test_client().get("/api/config").get_json()["stira_config_apply"]
    for name, value in DELIVERED_AT_START.items():
        assert (rows[name]["status"], rows[name]["applies"]) == ("applied", "restart"), name
        assert rows[name]["effective"] == value == rows[name]["saved"], name
    for name, value in DELIVERED_EACH_TURN.items():
        assert (rows[name]["status"], rows[name]["applies"]) == ("applied", "next_turn"), name
        assert rows[name]["effective"] == value == rows[name]["saved"], name
    for name in SERVER_FIXED:
        assert (rows[name]["status"], rows[name]["applies"]) == ("not_applied", "never"), name
    assert rows["critical_action"]["saved"] == "readonly"
    assert rows["critical_action"]["effective"] == "halt"
    assert "probe_tool" not in rows["blocked_tool_names"]["effective"]
    assert rows["identity_anchors"]["status"] == "applied"
    # Every StiraConfig field is accounted for.
    assert set(rows) == {f.name for f in dataclasses.fields(StiraConfig)}


def sorted_if_set(value):
    return sorted(value) if isinstance(value, (set, frozenset)) else value


def test_a_threshold_saved_while_running_is_pending_until_the_next_turn(tmp_path, monkeypatch):
    app, config = _start(tmp_path, monkeypatch, DELIVERED_AT_START)
    client = app.test_client()
    token = client.get("/api/csrf").get_json()["token"]
    version = client.get("/api/config/hub").get_json()["version"]
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"breaker": {"stability_warning": 0.55}}, "base_version": version},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200, saved.get_json()
    row = client.get("/api/config").get_json()["stira_config_apply"]["stability_warning"]
    assert (row["status"], row["applies"], row["effective"]) == ("pending", "next_turn", 0.5)

    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    row = client.get("/api/config").get_json()["stira_config_apply"]["stability_warning"]
    assert (row["status"], row["effective"]) == ("applied", 0.55)
    # The turn's sync copies the thresholds only; the start-up values stay.
    for name, value in DELIVERED_AT_START.items():
        assert _plain(getattr(config, name)) == value, name


def test_the_start_up_warning_names_ignored_fields_and_no_values(tmp_path, monkeypatch, capsys):
    _start(tmp_path, monkeypatch, {**DELIVERED_AT_START, "blocked_tool_names": ["probe_tool"]})
    out = capsys.readouterr().out
    line = next(item for item in out.splitlines() if "does not apply" in item)
    assert "blocked_tool_names" in line
    assert "bc_window_size" not in line and "probe_tool" not in line


def test_the_delivery_table_covers_every_field_and_matches_the_hub():
    from stiracore.config import stira_delivery
    from stiracore.observability import settings_hub

    assert set(settings_hub.BREAKER_FIELDS) <= set(stira_delivery.NEXT_TURN_FIELDS)
    whens = {name: stira_delivery.applies_when(name) for name in stira_delivery.field_names()}
    assert {n for n, w in whens.items() if w == "next_turn"} == set(DELIVERED_EACH_TURN)
    assert {n for n, w in whens.items() if w == "restart"} == {
        *DELIVERED_AT_START,
        "identity_anchors",
    }
    assert {n for n, w in whens.items() if w == "never"} == set(SERVER_FIXED)


def test_a_server_without_a_running_engine_says_saved(tmp_path):
    from flask import Flask

    from stiracore.observability.config_api import create_config_blueprint

    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    rows = app.test_client().get("/api/config").get_json()["stira_config_apply"]
    assert {row["status"] for row in rows.values()} == {"saved"}
    assert all(row["effective"] is None for row in rows.values())


def test_a_threshold_pair_with_its_exits_applies_as_one(tmp_path, monkeypatch):
    # Entry above the default exit (0.6) is valid when the exit is saved with it. The
    # server applies the six thresholds together, so the pair no longer fails (#272).
    app, config = _start(
        tmp_path, monkeypatch, {"stability_warning": 0.65, "stability_warning_exit": 0.7}
    )
    client = app.test_client()
    assert (config.stability_warning, config.stability_warning_exit) == (0.65, 0.7)
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    assert client.get("/api/config/hub").get_json()["apply"]["breaker"]["status"] == "applied"
    # Edited while running: both move on the next turn, never one without the other.
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"stira_config_overrides": {"stability_warning": 0.58}}), encoding="utf-8"
    )
    rows = client.get("/api/config").get_json()["stira_config_apply"]
    assert rows["stability_warning_exit"]["status"] == "pending"
    assert "two" in client.post("/api/chat", json={"message": "b"}).text
    assert (config.stability_warning, config.stability_warning_exit) == (0.58, 0.6)


def test_a_failed_threshold_apply_is_failed_not_pending():
    # The row logic (#412 review): the hub's last apply failed and these values cannot apply.
    from stiracore.config import stira_delivery

    saved = StiraConfig(stability_warning=0.55)
    running = stira_delivery.plain_values(StiraConfig())
    rows = stira_delivery.apply_rows(saved, running, {"stability_warning": 0.55}, "ValueError")
    assert rows["stability_warning"]["status"] == "failed"
    assert rows["stability_warning"]["reason"] == "ValueError"
    assert stira_delivery.apply_rows(saved, running)["stability_warning"]["status"] == "pending"


def test_a_field_the_server_never_applies_is_not_applied_even_when_values_are_equal(
    tmp_path, monkeypatch
):
    app, _ = _start(tmp_path, monkeypatch, {"critical_action": "halt"})
    rows = app.test_client().get("/api/config").get_json()["stira_config_apply"]
    # Saved == running, but the chat server never reads the field from the file.
    assert rows["critical_action"]["saved"] == rows["critical_action"]["effective"] == "halt"
    assert rows["critical_action"]["status"] == "not_applied"
    # Not set in the overrides: the server's own choice.
    assert rows["blocked_tool_names"]["status"] == "server_fixed"


def test_a_start_up_field_saved_while_running_is_pending_until_a_restart(tmp_path, monkeypatch):
    app, _ = _start(tmp_path, monkeypatch, {"bc_window_size": 8})
    client = app.test_client()
    row = client.get("/api/config").get_json()["stira_config_apply"]["bc_window_size"]
    assert (row["status"], row["effective"]) == ("applied", 8)
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"stira_config_overrides": {"bc_window_size": 9}}), encoding="utf-8"
    )
    row = client.get("/api/config").get_json()["stira_config_apply"]["bc_window_size"]
    assert (row["status"], row["applies"], row["saved"], row["effective"]) == (
        "pending",
        "restart",
        9,
        8,
    )
    # A turn does not apply it: only a restart does.
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    row = client.get("/api/config").get_json()["stira_config_apply"]["bc_window_size"]
    assert row["status"] == "pending"


@pytest.mark.parametrize("bad", [3, "20", None, 5.0])
def test_a_clamped_bc_window_reaches_the_engine_and_the_view_agrees(tmp_path, monkeypatch, bad):
    # #409: a bad hand-edited value is rounded up to 5 at load and flagged. The engine gets
    # the rounded 5 (#412), /api/config shows it as applied, and the flag stays on.
    app, config = _start(tmp_path, monkeypatch, {"bc_window_size": bad})
    assert config.bc_window_size == 5
    data = app.test_client().get("/api/config").get_json()
    row = data["stira_config_apply"]["bc_window_size"]
    assert (row["saved"], row["effective"], row["status"]) == (5, 5, "applied")
    assert data["bc_window_clamped"] is True
    assert data["stira_config"]["bc_window_size"] == 5


def test_a_valid_bc_window_is_not_flagged_as_clamped(tmp_path, monkeypatch):
    app, config = _start(tmp_path, monkeypatch, {"bc_window_size": 8})
    data = app.test_client().get("/api/config").get_json()
    assert config.bc_window_size == 8
    assert data["bc_window_clamped"] is False
    assert data["stira_config_apply"]["bc_window_size"]["status"] == "applied"


# --- Safety valves stay where they were (#272) ------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"protected_layers": [4, 5]}, "protected_layers"),
        ({"protected_layers": [1, 2]}, "protected_layers"),
        ({"protected_layers": []}, "protected_layers"),
        ({"critical_cooldown_turns": 2}, "critical_cooldown_turns"),
        ({"bc_window_size": 3}, "bc_window_size"),
        ({"hitl_timeout_sec": 0}, "hitl_timeout_sec"),
        ({"hitl_max_edit_rounds": -1}, "hitl_max_edit_rounds"),
        ({"sycophancy_window_size": 0}, "sycophancy_window_size"),
        ({"stability_warning_exit": 0.4}, "stability_warning_exit"),
    ],
)
def test_a_value_that_weakens_a_valve_is_still_refused(overrides, message, tmp_path):
    from stiracore.config import AgentConfig

    with pytest.raises(ValueError, match=message):
        AgentConfig(stira_config_overrides=overrides, path=tmp_path / "agent-config.json")


def _write_layers(tmp_path, layers):
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"stira_config_overrides": {"protected_layers": layers}}))
    return path


def test_adding_only_l1_l2_is_accepted_and_a_direct_value_without_l3_is_refused(tmp_path):
    from stiracore.config import AgentConfig

    ok = AgentConfig(
        stira_config_overrides={"protected_layers": [1, 2, 3, 4, 5, 6]},
        path=tmp_path / "agent-config.json",
    )
    assert sorted(ok.stira_config_overrides["protected_layers"]) == [1, 2, 3, 4, 5, 6]
    # Construction (and so every settings save) still refuses it.
    with pytest.raises(ValueError, match="protected_layers"):
        AgentConfig(stira_config_overrides={"protected_layers": [4, 5]}, path=tmp_path / "x.json")


@pytest.mark.parametrize(
    ("layers", "completed"),
    [([3, 4], [3, 4, 5]), ([], [3, 4, 5]), ([1, 2], [1, 2, 3, 4, 5]), ([5, 9], [3, 4, 5, 9])],
)
def test_a_hand_edited_file_without_l3_to_l5_is_completed_and_flagged(
    tmp_path, monkeypatch, caplog, layers, completed
):
    # The start-up path completes the layers, logs once without the value, and starts (as
    # the bc_window_size rounding of #409 does); a settings save is still refused.
    from stiracore.config import AgentConfig, models, resolve_config

    monkeypatch.setattr(models, "_PL_CLAMP_WARNED", False)
    path = _write_layers(tmp_path, layers)
    with caplog.at_level("WARNING"):
        agent = AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
        AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
    assert sorted(agent.stira_config_overrides["protected_layers"]) == completed
    assert agent.protected_layers_clamped is True
    resolved = resolve_config(agent=agent)
    assert resolved.protected_layers_clamped is True
    assert sorted(resolved.stira_config.protected_layers) == completed
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "protected_layers" in record.getMessage()
    assert record.args == () and "%" not in record.msg  # no value in the log, once per process
    # The next save writes the completed list and the flag goes away.
    agent.save()
    assert (
        AgentConfig.load(path, tool_config_path=tmp_path / "tools.json").protected_layers_clamped
        is False
    )


def test_a_complete_or_absent_layer_list_is_not_flagged(tmp_path, monkeypatch, caplog):
    from stiracore.config import AgentConfig, models

    monkeypatch.setattr(models, "_PL_CLAMP_WARNED", False)
    for layers in ([3, 4, 5], [1, 2, 3, 4, 5]):
        with caplog.at_level("WARNING"):
            agent = AgentConfig.load(
                _write_layers(tmp_path, layers), tool_config_path=tmp_path / "tools.json"
            )
        assert agent.protected_layers_clamped is False
    (tmp_path / "agent-config.json").write_text("{}")
    assert not AgentConfig.load(
        tmp_path / "agent-config.json", tool_config_path=tmp_path / "tools.json"
    ).protected_layers_clamped
    assert not [r for r in caplog.records if "protected_layers" in r.getMessage()]


@pytest.mark.parametrize("bad", [["x"], [True, 3], [0, 3, 4, 5], "345", 5])
def test_other_invalid_layer_values_still_stop_the_load(tmp_path, bad):
    from stiracore.config import AgentConfig

    with pytest.raises(ValueError):
        AgentConfig.load(_write_layers(tmp_path, bad), tool_config_path=tmp_path / "tools.json")


def test_the_server_starts_with_the_completed_layers_and_every_view_says_so(tmp_path, monkeypatch):
    from stiracore.config import models

    monkeypatch.setattr(models, "_PL_CLAMP_WARNED", False)
    app, config = _start(tmp_path, monkeypatch, {"protected_layers": [4]})
    assert sorted(config.protected_layers) == [3, 4, 5]
    client = app.test_client()
    status = client.get("/api/gateway/status").get_json()
    api = client.get("/api/config").get_json()
    hub = client.get("/api/config/hub").get_json()
    assert status["protected_layers_clamped"] is api["protected_layers_clamped"] is True
    assert hub["protected_layers_clamped"] is True
    assert api["stira_config"]["protected_layers"] == [3, 4, 5]
    row = api["stira_config_apply"]["protected_layers"]
    assert (row["saved"], row["effective"], row["status"]) == ([3, 4, 5], [3, 4, 5], "applied")
    # Fixing the file clears the flag in all three without a restart.
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"stira_config_overrides": {"protected_layers": [3, 4, 5]}})
    )
    assert client.get("/api/gateway/status").get_json()["protected_layers_clamped"] is False
    assert client.get("/api/config").get_json()["protected_layers_clamped"] is False
    assert client.get("/api/config/hub").get_json()["protected_layers_clamped"] is False


# --- Applying the six thresholds: readers never see half a pair (#427 review) ----------


def test_the_six_thresholds_are_published_as_one_whole_config():
    import threading
    from types import SimpleNamespace

    from stiracore.observability import settings_hub

    slow = {
        "stability_warning": 0.55,
        "stability_warning_exit": 0.65,
        "stability_critical": 0.35,
        "stability_critical_exit": 0.5,
        "bc_threshold": 0.6,
        "bc_exit_threshold": 0.7,
    }
    fast = {
        "stability_warning": 0.7,
        "stability_warning_exit": 0.8,
        "stability_critical": 0.45,
        "stability_critical_exit": 0.6,
        "bc_threshold": 0.75,
        "bc_exit_threshold": 0.85,
    }
    engine = SimpleNamespace(config=StiraConfig(**slow))
    stop = threading.Event()
    seen_sets: set[tuple] = set()
    torn: list[tuple] = []

    def reader():
        while not stop.is_set():
            config = engine.config  # what the engine does: one read per evaluation
            values = tuple(getattr(config, name) for name in slow)
            seen_sets.add(values)
            if not (
                config.stability_warning < config.stability_warning_exit
                and config.stability_critical < config.stability_critical_exit
                and config.bc_threshold < config.bc_exit_threshold
                and values in (tuple(slow.values()), tuple(fast.values()))
            ):
                torn.append(values)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    # A tiny switch interval makes a field-by-field edit show half a pair within a few
    # thousand writes (measured: the old in-place apply gave >200000 torn reads).
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    for thread in readers:
        thread.start()
    try:
        for _ in range(3000):
            settings_hub.apply_breaker_thresholds(engine, fast)
            settings_hub.apply_breaker_thresholds(engine, slow)
    finally:
        stop.set()
        for thread in readers:
            thread.join()
        sys.setswitchinterval(interval)
    assert not torn, torn[:3]
    assert seen_sets  # the readers ran


def test_the_breaker_check_reads_the_config_once_per_evaluation():
    from stiracore.core.engine import StiraEngine

    reads = []

    class Counting(StiraEngine):
        @property
        def config(self):
            reads.append(1)
            return self._cfg

        @config.setter
        def config(self, value):
            self._cfg = value

    engine = Counting(config=StiraConfig())
    reads.clear()
    engine._check_breaker()
    assert len(reads) == 1, len(reads)


# --- hitl_timeout_sec: bounded before it reaches an approval (#427 review) --------------


def _write_timeout(tmp_path, value):
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"stira_config_overrides": {"hitl_timeout_sec": value}}))
    return path


@pytest.mark.parametrize(
    ("value", "expected", "clamped"),
    [
        (90, 90.0, False),
        (86400, 86400.0, False),
        (86400.5, 86400.0, True),
        (1e300, 86400.0, True),
        (float("inf"), 86400.0, True),
        (0, 120.0, True),
        (-5, 120.0, True),
        (float("nan"), 120.0, True),
        ("30", 120.0, True),
        (None, 120.0, True),
        (True, 120.0, True),
    ],
)
def test_the_load_bounds_the_approval_timeout(
    tmp_path, monkeypatch, caplog, value, expected, clamped
):
    # Too long: capped at 24 h (an approval cannot wait for ever). Not a positive finite
    # number: the engine's default 120 s. Either way the start goes on, once-logged, no value.
    from stiracore.config import AgentConfig, models, resolve_config

    monkeypatch.setattr(models, "_HT_CLAMP_WARNED", False)
    with caplog.at_level("WARNING"):
        agent = AgentConfig.load(
            _write_timeout(tmp_path, value), tool_config_path=tmp_path / "tools.json"
        )
    assert agent.stira_config_overrides["hitl_timeout_sec"] == expected
    assert agent.hitl_timeout_clamped is clamped
    assert resolve_config(agent=agent).hitl_timeout_clamped is clamped
    assert resolve_config(agent=agent).stira_config.hitl_timeout_sec == expected
    warnings = [r for r in caplog.records if "hitl_timeout_sec" in r.getMessage()]
    assert len(warnings) == (1 if clamped else 0)
    assert all(r.args == () for r in warnings)


@pytest.mark.parametrize("value", [1e300, 86400.5, 0, -1, float("nan"), "30", None, True])
def test_a_direct_value_that_cannot_be_used_is_still_refused(tmp_path, value):
    from stiracore.config import AgentConfig

    with pytest.raises(ValueError, match="hitl_timeout_sec"):
        AgentConfig(
            stira_config_overrides={"hitl_timeout_sec": value}, path=tmp_path / "agent-config.json"
        )


def test_a_huge_timeout_reaches_the_engine_capped_and_expires_a_request(tmp_path, monkeypatch):
    from stiracore.config import models
    from stiracore.core.engine import (
        BreakLevel,
        HITLAction,
        HITLDecision,
        HITLRequest,
        StiraEngine,
    )

    monkeypatch.setattr(models, "_HT_CLAMP_WARNED", False)
    app, config = _start(tmp_path, monkeypatch, {"hitl_timeout_sec": 1e300})
    assert config.hitl_timeout_sec == 86400.0
    client = app.test_client()
    data = client.get("/api/config").get_json()
    assert data["hitl_timeout_clamped"] is True
    assert client.get("/api/gateway/status").get_json()["hitl_timeout_clamped"] is True
    assert client.get("/api/config/hub").get_json()["hitl_timeout_clamped"] is True
    row = data["stira_config_apply"]["hitl_timeout_sec"]
    assert (row["saved"], row["effective"], row["status"]) == (86400.0, 86400.0, "applied")
    seen = []

    def approver(request):
        seen.append(request)
        return HITLDecision(HITLAction.REJECT, reason="no")

    clock = [1_700_000_000.0]
    engine = StiraEngine(config=config.current, on_hitl_request=approver, now_fn=lambda: clock[0])
    engine.request_hitl(
        HITLRequest(
            tool_name="file_write",
            tool_input={"path": "a"},
            break_level=BreakLevel.NORMAL,
            reason="test",
        )
    )
    assert seen[0].expires_at == clock[0] + 86400.0
