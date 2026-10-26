"""ADR 0022 phase 0 (#458) on the real chat server app: off by default, read-only when on."""

from __future__ import annotations

import pytest

from stiracore.middleware.taint_observer import TaintObserverHook
from tests.test_containment_server import _app, _reset_read_audit, _spy_agent  # noqa: F401


def _hooks(seen):
    return [hook for hook in seen["extra_hooks"] if isinstance(hook, TaintObserverHook)]


def test_absent_key_is_off_no_hook(tmp_path, monkeypatch):
    seen = _spy_agent(monkeypatch)
    app = _app(tmp_path, monkeypatch)
    assert _hooks(seen) == [] and app.extensions["taint"] is None
    body = app.test_client().get("/api/taint").get_json()
    assert body["enabled"] is False and body["observation"] is None
    assert body["observe_only"] is True and body["enforced"] is False


def test_on_registers_the_hook_and_shows_counts(tmp_path, monkeypatch):
    seen = _spy_agent(monkeypatch)
    app = _app(tmp_path, monkeypatch, {"taint": {"phase": 0}})
    assert len(_hooks(seen)) == 1
    body = app.test_client().get("/api/taint").get_json()
    assert body["enabled"] is True
    assert body["observation"] == {"last_turn": None, "totals": {}}


@pytest.mark.parametrize("value", ["0", "off"])
def test_env_forces_off(tmp_path, monkeypatch, value):
    seen = _spy_agent(monkeypatch)
    app = _app(tmp_path, monkeypatch, {"taint": {"phase": 0}}, env={"STIRA_TAINT_OBSERVE": value})
    assert _hooks(seen) == []
    body = app.test_client().get("/api/taint").get_json()
    assert body["enabled"] is False and body["forced_off"] is True
