"""#337: the status tells "not measured yet" from "measured, completely stable".

Both score 1.0 (`docs/coherence.md`). `/api/gateway/status` now also says how many stability
samples the score rests on (`bc_samples`) and whether that is enough (`bc_measured`). The
fields are derived for display from the window; NeuralState is read, not changed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

from stiracore.core import NeuralState, compute_bc_score
from tests.test_config_location import _start


def _status(monkeypatch, tmp_path, state: NeuralState | None) -> dict:
    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    data_dir.mkdir(parents=True)
    if state is not None:
        # As the engine saves it: the score is the one of the window.
        state.bc_score = compute_bc_score(state.bc_window)
        state.save(data_dir / "neural-state.json")
    app = _start(monkeypatch, data_dir, repo)
    return app.test_client().get("/api/gateway/status").get_json()


def test_a_fresh_start_is_unmeasured(monkeypatch, tmp_path) -> None:
    status = _status(monkeypatch, tmp_path, None)
    assert status["bc_score"] == 1.0
    assert status["bc_samples"] == 0
    assert status["bc_measured"] is False
    assert status["bc_min_samples"] == 5


@pytest.mark.parametrize("count", [1, 4])
def test_fewer_than_five_samples_is_unmeasured(monkeypatch, tmp_path, count) -> None:
    status = _status(monkeypatch, tmp_path, NeuralState(bc_window=[0.7] * count))
    assert status["bc_score"] == 1.0
    assert (status["bc_samples"], status["bc_measured"]) == (count, False)


def test_five_identical_samples_is_measured_and_stable(monkeypatch, tmp_path) -> None:
    status = _status(monkeypatch, tmp_path, NeuralState(bc_window=[0.7] * 5))
    assert status["bc_score"] == 1.0
    assert (status["bc_samples"], status["bc_measured"]) == (5, True)


def test_a_moving_window_is_measured(monkeypatch, tmp_path) -> None:
    status = _status(monkeypatch, tmp_path, NeuralState(bc_window=[0.9, 0.5] * 5))
    assert status["bc_score"] < 0.6
    assert (status["bc_samples"], status["bc_measured"]) == (10, True)


def test_existing_status_fields_are_unchanged(monkeypatch, tmp_path) -> None:
    status = _status(monkeypatch, tmp_path, None)
    for key in ("agent_id", "model", "neural_state", "breaker", "bc_score", "soul", "memory"):
        assert key in status


def _status_and_config(monkeypatch, tmp_path, agent_config: dict | None):
    import json

    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    data_dir.mkdir(parents=True)
    if agent_config is not None:
        (data_dir / "agent-config.json").write_text(json.dumps(agent_config))
    app = _start(monkeypatch, data_dir, repo)
    client = app.test_client()
    return client.get("/api/gateway/status").get_json(), client.get("/api/config").get_json()


def test_a_rounded_window_size_is_visible_in_status_and_settings(monkeypatch, tmp_path) -> None:
    from stiracore.config import models

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", False)
    status, config = _status_and_config(
        monkeypatch, tmp_path, {"stira_config_overrides": {"bc_window_size": 3}}
    )
    assert status["bc_window_clamped"] is True
    assert config["bc_window_clamped"] is True
    assert config["stira_config"]["bc_window_size"] == 5


def test_an_untouched_window_size_is_not_flagged(monkeypatch, tmp_path) -> None:
    status, config = _status_and_config(monkeypatch, tmp_path, None)
    assert status["bc_window_clamped"] is False
    assert config["bc_window_clamped"] is False


def test_after_a_settings_save_status_config_and_hub_agree(monkeypatch, tmp_path) -> None:
    # Review of #409 (M-1): the flag is read from the file each time, so a save that writes
    # the rounded 5 clears it in the status as well, not only in /api/config.
    import json

    from stiracore.config import models

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", False)
    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    data_dir.mkdir(parents=True)
    agent_path = data_dir / "agent-config.json"
    agent_path.write_text(json.dumps({"stira_config_overrides": {"bc_window_size": 3}}))
    client = _start(monkeypatch, data_dir, repo).test_client()

    def seen() -> tuple[bool, bool, bool]:
        return (
            client.get("/api/gateway/status").get_json()["bc_window_clamped"],
            client.get("/api/config").get_json()["bc_window_clamped"],
            client.get("/api/config/hub").get_json()["bc_window_clamped"],
        )

    assert seen() == (True, True, True)
    token = client.get("/api/csrf").get_json()["token"]
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"breaker": {"bc_threshold": 0.61}}},
        headers={"X-CSRF-Token": token},
    )
    assert saved.status_code == 200, saved.get_json()
    assert json.loads(agent_path.read_text())["stira_config_overrides"]["bc_window_size"] == 5
    assert seen() == (False, False, False)
