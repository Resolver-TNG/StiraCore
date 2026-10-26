"""#512 requirement 4: an inverted breaker entry/exit pair in agent-config.json.

``StiraConfig`` refuses an exit at or under its entry, a critical line at or above the warning
line, and a critical exit at or above the warning exit. A hand-edited (or other-version) file
with such a set used to make every settings route answer 503 through the shared loader, and
The chat server could not start. Now the load runs on the default thresholds (never looser
than the defaults), flags it (``breaker_pairs_reset``) and the breaker card says so. A save
still refuses an inverted set; a save of anything else keeps the file's values as written.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.config import AgentConfig
from stiracore.core.engine import StiraConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint

DEFAULTS = {name: float(getattr(StiraConfig(), name)) for name in hub.BREAKER_SET}

# File overrides breaking each pair rule, also with one side omitted (its default makes the
# set inverted).
INVERTED = [
    {"stability_warning": 0.5, "stability_warning_exit": 0.4},
    {"stability_warning_exit": 0.45},  # warning 0.5 (default) >= exit; also crit exit
    {"stability_critical": 0.55},  # critical >= warning (default 0.5)
    {"stability_critical_exit": 0.7},  # critical exit >= warning exit (default 0.6)
    {"bc_exit_threshold": 0.5},  # bc exit <= bc entry (default 0.6)
    {"bc_threshold": 0.8, "bc_exit_threshold": 0.8},
]


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


def _client(store, **kwargs):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal", **kwargs))
    return app.test_client()


@pytest.fixture
def client(store):
    return _client(store)


def _state(client):
    return client.get("/api/config/hub").get_json()


def _save(client, changes, **extra):
    return client.post(
        "/api/config/hub",
        json={"changes": changes, "base_version": _state(client)["version"], **extra},
    )


def _write(store, overrides, **extra):
    store.agent_path.write_text(
        json.dumps({"stira_config_overrides": overrides, **extra}), encoding="utf-8"
    )


def _file_overrides(store) -> dict:
    return json.loads(store.agent_path.read_text(encoding="utf-8"))["stira_config_overrides"]


def _shown(overrides):
    return {**DEFAULTS, **{k: float(v) for k, v in overrides.items() if k in DEFAULTS}}


@pytest.mark.parametrize("overrides", INVERTED)
def test_an_inverted_pair_does_not_take_the_settings_page_down(client, store, overrides):
    _write(store, overrides)
    raw = store.agent_path.read_text(encoding="utf-8")
    for route in (
        "/api/config",
        "/api/config/hub",
        "/api/config/models",
        "/api/config/providers",
        "/api/config/history",
    ):
        response = client.get(route)
        assert response.status_code == 200, (route, response.get_json())
    state = _state(client)
    assert state["breaker_pairs_reset"] is True
    # The page shows the file's values (an omitted one at its default), so entering any
    # valid set (the defaults too) is a change it can save.
    assert state["values"]["breaker"] == _shown(overrides)
    assert client.get("/api/config").get_json()["breaker_pairs_reset"] is True
    # What runs: the default thresholds, all six (never looser than the defaults).
    _gateway, agent = store.load()
    assert agent.breaker_pairs_reset is True
    resolved = store.resolved()
    assert {name: getattr(resolved.stira_config, name) for name in DEFAULTS} == DEFAULTS
    assert client.get("/api/config").get_json()["stira_config"]["stability_warning"] == 0.5
    # Opening the page writes nothing.
    assert store.agent_path.read_text(encoding="utf-8") == raw
    # A direct construction (any save) still refuses the set.
    with pytest.raises(ValueError):
        AgentConfig(stira_config_overrides=overrides)


def test_a_valid_file_is_not_flagged(client, store):
    _write(store, {"stability_warning": 0.55, "stability_warning_exit": 0.65})
    state = _state(client)
    assert state["breaker_pairs_reset"] is False
    assert state["values"]["breaker"]["stability_warning"] == 0.55
    assert store.load()[1].breaker_pairs_reset is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"stability_warning_exit": None},
        {"stability_warning_exit": "0.4"},
        {"stability_warning_exit": True},
        {"stability_warning_exit": 1.5},
        {"stability_warning": 0.5, "stability_warning_exit": 0.4, "bc_threshold": None},
    ],
)
def test_a_null_a_string_a_boolean_or_out_of_range_is_not_replaced(client, store, overrides):
    """Only an omitted value takes the default; a written wrong type or out-of-range value is
    an invalid value, refused as before (503), never silently replaced."""
    _write(store, overrides)
    response = client.get("/api/config/hub")
    assert response.status_code == 503
    with pytest.raises(ValueError):
        store.load()


@pytest.mark.parametrize(
    "fixed",
    [
        {"stability_warning_exit": 0.6},  # the default exit
        # Move the entries instead (and the critical exit under the file's warning exit).
        {"stability_warning": 0.35, "stability_critical": 0.2, "stability_critical_exit": 0.3},
    ],
)
def test_saving_a_valid_set_from_that_state_fixes_the_file_and_clears_the_note(
    client, store, fixed
):
    overrides = {"stability_warning": 0.5, "stability_warning_exit": 0.4, "bc_threshold": 0.65}
    _write(store, overrides)
    response = _save(client, {"breaker": fixed})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["state"]["breaker_pairs_reset"] is False
    written = _file_overrides(store)
    # The file's other values stay as written; only the moved ones change.
    assert written == {**overrides, **fixed}
    assert store.load()[1].breaker_pairs_reset is False
    assert _state(client)["values"]["breaker"] == _shown({**overrides, **fixed})


def test_taking_the_defaults_writes_them(client, store):
    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    response = _save(client, {"breaker": dict(DEFAULTS)})
    assert response.status_code == 200, response.get_json()
    assert _file_overrides(store)["stability_warning_exit"] == 0.6
    assert store.load()[1].breaker_pairs_reset is False


def test_saving_a_set_that_is_still_inverted_is_refused(client, store):
    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    raw = store.agent_path.read_text(encoding="utf-8")
    # Moving another pair while the inverted one stays: the whole set is checked.
    response = _save(client, {"breaker": {"bc_threshold": 0.65}})
    assert response.status_code == 400
    assert {e["field"] for e in response.get_json()["errors"]} >= {"breaker.stability_warning"}
    assert store.agent_path.read_text(encoding="utf-8") == raw


def test_saving_an_inverted_set_is_still_refused_from_a_valid_file(client, store):
    """Save-time validation is unchanged (400), with no file to rescue."""
    for change in (
        {"stability_warning_exit": 0.4},
        {"stability_critical": 0.55},
        {"stability_critical_exit": 0.7},
        {"bc_exit_threshold": 0.5},
    ):
        response = _save(client, {"breaker": change})
        assert response.status_code == 400, change
    assert not store.agent_path.exists() or "stira_config_overrides" not in json.loads(
        store.agent_path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("overrides", INVERTED)
def test_an_unrelated_save_keeps_the_file_values_and_the_flag(client, store, overrides):
    """Like review r568 M-2: a save of anything else (the details fold, the write roots, the
    gateway file, the model page) leaves the file's thresholds as written (an omitted one
    stays omitted): no silent rewrite to the defaults."""
    _write(store, {**overrides, "critical_cooldown_turns": 4})
    kept = {k: v for k, v in _file_overrides(store).items() if k in DEFAULTS}
    for changes in (
        {"breaker_details": {"critical_cooldown_turns": 5}},
        {"cache": {"enabled": True, "ttl": "5m"}},
        {"heartbeat": {"enabled": True}},
        {"write": {"roots": list(hub.WRITE_SCOPES["workspace_tmp"])}},
    ):
        extra = {"acknowledged_dangers": ["write_roots_widened"]} if "write" in changes else {}
        response = _save(client, changes, **extra)
        assert response.status_code == 200, (changes, response.get_json())
        assert response.get_json()["state"]["breaker_pairs_reset"] is True, changes
        written = _file_overrides(store)
        assert {k: v for k, v in written.items() if k in DEFAULTS} == kept, changes
    assert _file_overrides(store)["critical_cooldown_turns"] == 5
    assert store.load()[1].breaker_pairs_reset is True
    model = client.post(
        "/api/config/models",
        json={"provider": "bedrock", "model_id": "global.anthropic.claude-sonnet-5"},
    )
    assert model.status_code == 200, model.get_json()
    assert {k: v for k, v in _file_overrides(store).items() if k in DEFAULTS} == kept
    assert store.load()[1].breaker_pairs_reset is True


def test_the_apply_status_says_failed_with_the_reason(store):
    _write(store, {"bc_exit_threshold": 0.5})
    runtime = {
        "tools": None,
        "breaker": dict(DEFAULTS),
        "classifier_choice": "auto",
        "error": None,
    }
    client = _client(store, runtime_settings_provider=lambda: runtime)
    status = _state(client)["apply"]["breaker"]
    assert status == {"status": "failed", "applies": "next_turn", "reason": "breaker_pairs_reset"}


def test_the_reset_warning_is_logged_once_without_the_values(store, caplog, monkeypatch):
    from stiracore.config import models

    monkeypatch.setattr(models, "_BP_RESET_WARNED", False)
    _write(store, {"stability_warning": 0.4321, "stability_warning_exit": 0.4123})
    with caplog.at_level("WARNING", logger=models.logger.name):
        store.load()
        store.load()
    lines = [r.getMessage() for r in caplog.records if "breaker_pairs_reset" in r.getMessage()]
    assert len(lines) == 1
    assert "0.4321" not in lines[0] and "0.4123" not in lines[0]
    caplog.clear()
    monkeypatch.setattr(models, "_BP_RESET_WARNED", False)
    _write(store, {"stability_warning": 0.55})
    with caplog.at_level("WARNING", logger=models.logger.name):
        assert store.load()[1].breaker_pairs_reset is False
    assert not [r for r in caplog.records if "breaker_pairs_reset" in r.getMessage()]


# --- the running engine (fail-closed) ---------------------------------------------------------


@pytest.mark.parametrize(
    "running",
    [
        dict(DEFAULTS),
        # Stricter than the defaults (saved before the hand edit): kept, never loosened.
        {**DEFAULTS, "stability_warning": 0.7, "stability_warning_exit": 0.8},
        # Looser than the defaults: raised to the defaults, never kept looser.
        {
            **DEFAULTS,
            "stability_warning": 0.35,
            "stability_critical": 0.2,
            "stability_warning_exit": 0.5,
            "stability_critical_exit": 0.4,
            "bc_threshold": 0.4,
            "bc_exit_threshold": 0.5,
        },
    ],
)
def test_the_engine_thresholds_while_reset_are_never_looser(running):
    """Per field the stricter of the running value and the default (every threshold: higher
    restricts earlier and recovers later). The result is always a valid set."""
    thresholds = hub.reset_breaker_thresholds(StiraConfig(**running))
    for name in hub.BREAKER_SET:
        assert thresholds[name] >= DEFAULTS[name], name
        assert thresholds[name] >= running[name], name
    holder = SimpleNamespace(config=StiraConfig(**running))
    hub.apply_breaker_thresholds(holder, thresholds)  # valid: does not raise


def test_stira_starts_on_the_defaults_and_a_hand_edit_while_running_never_loosens(
    tmp_path, monkeypatch
):
    """The chat server could not start with such a file (the load raised). Now it starts on
    the default thresholds; a hand edit to an inverted set while it runs keeps the stricter
    of what runs and the defaults (per field), never the file's values."""
    from tests.test_stira_config_overrides import _start

    app, config = _start(
        tmp_path, monkeypatch, {"stability_warning": 0.5, "stability_warning_exit": 0.4}
    )
    assert {name: getattr(config, name) for name in DEFAULTS} == DEFAULTS
    client = app.test_client()
    assert client.get("/api/config/hub").status_code == 200
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    assert {name: getattr(config, name) for name in DEFAULTS} == DEFAULTS
    hub_state = client.get("/api/config/hub").get_json()
    assert hub_state["breaker_pairs_reset"] is True
    assert hub_state["apply"]["breaker"]["reason"] == "breaker_pairs_reset"

    # A valid stricter set, applied on a turn, then a hand edit makes it inverted.
    store = tmp_path / "agent-config.json"
    stricter = {"stability_warning": 0.7, "stability_warning_exit": 0.8}
    store.write_text(json.dumps({"stira_config_overrides": stricter}), encoding="utf-8")
    assert "two" in client.post("/api/chat", json={"message": "b"}).text
    assert (config.stability_warning, config.stability_warning_exit) == (0.7, 0.8)
    inverted = {"stability_warning": 0.3, "stability_warning_exit": 0.2}
    store.write_text(json.dumps({"stira_config_overrides": inverted}), encoding="utf-8")
    # The fake model has two answers only: the thresholds are synced before the model call.
    client.post("/api/chat", json={"message": "c"})
    assert (config.stability_warning, config.stability_warning_exit) == (0.7, 0.8)
    assert config.stability_critical == DEFAULTS["stability_critical"]


def test_the_breaker_card_shows_the_note_the_reason_and_the_file_values(tmp_path, store):
    """The real template and ``settings_hub.js`` in jsdom (``tests/a11y/
    settings_breaker_reset_dom.cjs``): the note, the apply status in words, the file's
    values on the sliders, and a moved exit going above its entry."""
    import os
    import shutil
    import subprocess

    from tests.ui_pages import render_pages

    def skip_or_fail(reason):
        if os.environ.get("STIRA_REQUIRE_AXE") == "1":
            pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
        pytest.skip(reason)

    node = shutil.which("node")
    if node is None:
        skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "pages")
    except RuntimeError as exc:
        skip_or_fail(str(exc))
    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    runtime = {"tools": None, "breaker": dict(DEFAULTS), "classifier_choice": "auto", "error": None}
    hub_state = _state(_client(store, runtime_settings_provider=lambda: runtime))
    assert hub_state["breaker_pairs_reset"] is True
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    state = tmp_path / "hub.json"
    state.write_text(json.dumps(hub_state), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            node,
            str(root / "tests" / "a11y" / "settings_breaker_reset_dom.cjs"),
            str(page),
            str(state),
        ],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        skip_or_fail(output.strip())
    assert proc.returncode == 0, f"breaker reset on the page (exit {proc.returncode}):\n{output}"


# --- review r591 ------------------------------------------------------------------------------


def test_env_overrides_keep_the_flag(store):
    """M-1: ``apply_env_overrides`` copies the gateway config only; the flag is read from the
    agent object the loader returned, so no environment variable drops it."""
    from stiracore.config.resolver import resolve_config

    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    gateway, agent = store.load()
    environ = {
        "STIRA_MODEL": "global.anthropic.claude-sonnet-5",
        "AWS_REGION": "us-east-1",
        "STIRA_PORT": "5999",
        "STIRA_PROVIDER": "bedrock",
    }
    resolved = resolve_config(gateway, agent, environ=environ)
    assert resolved.breaker_pairs_reset is True
    assert resolve_config(gateway, None, environ=environ).breaker_pairs_reset is False


@pytest.mark.parametrize("name", list(hub.BREAKER_SET))
def test_a_higher_threshold_is_stricter_for_every_one_of_the_six(name):
    """Why ``reset_breaker_thresholds`` takes the maximum (review r591): measured on the
    engine's own decision, a higher entry trips at a value the lower one lets pass, and a
    higher exit keeps the level at a value the lower one releases (bc included)."""
    from stiracore.core.engine import BreakLevel, StiraEngine

    low = {**DEFAULTS}
    high = {**DEFAULTS}
    if name in hub.EXIT_FIELDS:  # an entry: probe between the two candidate lines
        low[name], high[name] = DEFAULTS[name] - 0.05, DEFAULTS[name] + 0.05
        if name == "stability_warning":
            high["stability_warning_exit"] = 0.7
        probe = DEFAULTS[name]
    else:  # an exit
        low[name], high[name] = DEFAULTS[name] - 0.04, DEFAULTS[name] + 0.04
        if name == "stability_critical_exit":
            high["stability_warning_exit"] = 0.6
            low[name], high[name] = 0.4, 0.55
        probe = DEFAULTS[name]

    is_bc = name.startswith("bc_")
    target = (
        BreakLevel.COHERENCE
        if is_bc
        else BreakLevel.CRITICAL
        if "critical" in name
        else BreakLevel.WARNING
    )

    def tripped(values, _unused=True):
        engine = StiraEngine(config=StiraConfig(**values))
        config = engine.config
        calm_stability, calm_bc = 0.9, 0.95
        if name in hub.EXIT_FIELDS:  # an entry: one evaluation at the probe
            stability, bc = (calm_stability, probe) if is_bc else (probe, calm_bc)
            return engine._evaluate_breaker(config, stability, bc) == target
        # An exit: trip first (well under the entry), then probe the recovery.
        if is_bc:
            engine._evaluate_breaker(config, calm_stability, 0.1)
            return engine._evaluate_breaker(config, calm_stability, probe) == target
        if "critical" in name:
            engine._evaluate_breaker(config, 0.05, calm_bc)
            engine._critical_turns = 99  # cooldown served: only the exit decides
            return engine._evaluate_breaker(config, probe, calm_bc) == target
        engine._evaluate_breaker(config, 0.4, calm_bc)  # WARNING, not CRITICAL
        return engine._evaluate_breaker(config, probe, calm_bc) == target

    assert tripped(high, True) is True, name
    assert tripped(low, True) is False, name


def test_a_preset_from_that_state_writes_a_valid_set_and_clears_the_flag(client, store):
    """M-2: presets fill the draft on the page and are saved by the hub (``persist``)."""
    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    state = _state(client)
    safe = next(p for p in state["presets"] if p["id"] == "safe")["values"]["breaker"]
    response = _save(client, {"breaker": safe})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["state"]["breaker_pairs_reset"] is False
    written = _file_overrides(store)
    assert {name: written.get(name, DEFAULTS[name]) for name in DEFAULTS} == safe


def test_a_rollback_in_that_state_keeps_the_file_values_or_writes_a_valid_set(client, store):
    """M-2: a rollback goes through ``persist`` too. Rolling back an unrelated entry keeps the
    file's six values as written; rolling back a breaker entry from before the hand edit
    moves a threshold, so the whole shown set is checked and written (here valid)."""
    assert _save(client, {"breaker": {"stability_warning_exit": 0.7}}).status_code == 200
    assert _save(client, {"cache": {"enabled": True, "ttl": "5m"}}).status_code == 200
    entries = client.get("/api/config/history").get_json()["entries"]

    def entry_for(group):
        return next(
            e for e in entries if all(c["field"].startswith(f"{group}.") for c in e["changes"])
        )

    cache_entry, breaker_entry = entry_for("cache"), entry_for("breaker")
    # The hand edit.
    _write(store, {"stability_warning": 0.5, "stability_warning_exit": 0.4})
    kept = {k: v for k, v in _file_overrides(store).items() if k in DEFAULTS}
    undo_cache = client.post(f"/api/config/history/{cache_entry['id']}/rollback", json={})
    assert undo_cache.status_code == 200, undo_cache.get_json()
    assert {k: v for k, v in _file_overrides(store).items() if k in DEFAULTS} == kept
    assert store.load()[1].breaker_pairs_reset is True
    # Rolling back the exit (0.7 -> 0.6) moves a threshold: the shown set with it is valid.
    undo_breaker = client.post(f"/api/config/history/{breaker_entry['id']}/rollback", json={})
    assert undo_breaker.status_code == 200, undo_breaker.get_json()
    assert store.load()[1].breaker_pairs_reset is False
    assert _file_overrides(store) == {"stability_warning": 0.5, "stability_warning_exit": 0.6}


def test_stira_a_looser_running_set_is_raised_to_the_defaults_and_a_stricter_one_kept(
    tmp_path, monkeypatch
):
    """M-3: tells the per-field maximum apart from "keep what runs" (main) and from "run
    the defaults": the warning pair runs stricter than the defaults (kept), the coherence
    pair looser (raised), with an environment override on (M-1)."""
    from tests.test_stira_config_overrides import _start

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mixed = {
        "stability_warning": 0.7,
        "stability_warning_exit": 0.8,
        "bc_threshold": 0.4,
        "bc_exit_threshold": 0.5,
    }
    app, config = _start(tmp_path, monkeypatch, mixed)
    client = app.test_client()
    assert "one" in client.post("/api/chat", json={"message": "a"}).text
    assert (config.stability_warning, config.bc_threshold) == (0.7, 0.4)
    store = tmp_path / "agent-config.json"
    inverted = {**mixed, "stability_critical": 0.75}  # critical above warning: inverted
    store.write_text(json.dumps({"stira_config_overrides": inverted}), encoding="utf-8")
    assert client.get("/api/config/hub").get_json()["breaker_pairs_reset"] is True
    assert "two" in client.post("/api/chat", json={"message": "b"}).text
    applied = {name: getattr(config, name) for name in DEFAULTS}
    assert applied == {
        **DEFAULTS,
        "stability_warning": 0.7,  # stricter than the default: kept
        "stability_warning_exit": 0.8,
        "bc_threshold": 0.6,  # looser than the default: raised
        "bc_exit_threshold": 0.7,
    }


def test_no_reset_warning_when_another_value_still_stops_the_load(store, caplog, monkeypatch):
    """Review r591 m-2: the warning comes after the build, so a file that still fails to load
    for another reason does not also blame the breaker pairs."""
    from stiracore.config import models

    monkeypatch.setattr(models, "_BP_RESET_WARNED", False)
    _write(
        store, {"stability_warning": 0.5, "stability_warning_exit": 0.4, "hitl_max_edit_rounds": -1}
    )
    with caplog.at_level("WARNING", logger=models.logger.name), pytest.raises(ValueError):
        store.load()
    assert not [r for r in caplog.records if "breaker_pairs_reset" in r.getMessage()]
