"""The start-up banner names which SOUL is used and whether it reads, never its path.

Integration bug hunt 2026-10-15: it printed the default SOUL path (and its existence)
even when agent-config.json set another ``soul_path``. Review of #289 (#291): the
path itself names users and directories in the log, and ``exists()`` is not "loaded".
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from stiracore.observability.config_api import ConfigStore


class _Agent:
    def __init__(self, *, tools=(), state=None, config=None, **_kwargs):
        from stiracore.core import StiraEngine

        self.engine = StiraEngine(state=state, config=config)
        self.tools = tuple(tools)
        # What /api/gateway/status reads (#291 status tests).
        self._stimulus_classifier = None
        self.prompt_cache = SimpleNamespace(enabled=False, ttl=None)
        self.context_management = SimpleNamespace(enabled=False)
        self.last_prompt_cache = self.last_context_management = None


def _banner(tmp_path, monkeypatch, capsys, *, soul_path=None, default_text=None):
    from dashboard import stira_server

    data_dir = tmp_path / "stira"
    data_dir.mkdir(parents=True)
    agent_config = {"soul_path": str(soul_path)} if soul_path is not None else {}
    (data_dir / "agent-config.json").write_text(json.dumps(agent_config), encoding="utf-8")
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    default = tmp_path / "default-SOUL.md"
    if default_text is not None:
        default.write_text(default_text, encoding="utf-8")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", default)
    monkeypatch.setattr(stira_server, "NXPAgent", _Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    app = stira_server.create_app(store)
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.strip().startswith("SOUL:"))
    _APPS.append(app)
    return out, line.strip()


_APPS: list = []


def _status_soul(tmp_path, monkeypatch, capsys, **kwargs):
    """``soul`` of ``/api/gateway/status`` and the whole response text (#291)."""
    _APPS.clear()
    _banner(tmp_path, monkeypatch, capsys, **kwargs)
    response = _APPS[-1].test_client().get("/api/gateway/status")
    assert response.status_code == 200
    return response.get_json()["soul"], response.get_data(as_text=True)


def test_start_up_banner_names_the_configured_soul_without_its_path(tmp_path, monkeypatch, capsys):
    soul = tmp_path / "private-dir" / "custom-soul.md"
    soul.parent.mkdir()
    soul.write_text("I am custom.", encoding="utf-8")
    out, line = _banner(tmp_path, monkeypatch, capsys, soul_path=soul)
    assert line == "SOUL: configured (✅ loaded)"
    for fragment in (str(soul), "private-dir", "custom-soul", str(tmp_path)):
        assert fragment not in out


def test_default_soul_is_reported_as_default(tmp_path, monkeypatch, capsys):
    _out, line = _banner(tmp_path, monkeypatch, capsys, default_text="I am Resolver.")
    assert line == "SOUL: default (✅ loaded)"


def test_a_missing_soul_is_not_loaded(tmp_path, monkeypatch, capsys):
    _out, line = _banner(tmp_path, monkeypatch, capsys, soul_path=tmp_path / "absent.md")
    assert line == "SOUL: configured (❌ missing)"


def test_an_empty_soul_is_not_reported_loaded(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.md"
    empty.write_text("  \n", encoding="utf-8")
    _out, line = _banner(tmp_path, monkeypatch, capsys, soul_path=empty)
    assert line == "SOUL: configured (⚠️ empty)"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_an_unreadable_soul_exists_but_is_not_loaded(tmp_path, monkeypatch, capsys):
    """exists() is true here; the banner must not call it loaded."""
    locked = tmp_path / "locked.md"
    locked.write_text("secret persona", encoding="utf-8")
    locked.chmod(0)
    try:
        _out, line = _banner(tmp_path, monkeypatch, capsys, soul_path=locked)
    finally:
        locked.chmod(0o600)
    assert line == "SOUL: configured (❌ unreadable: PermissionError)"


# --- #291 (rest): /api/gateway/status says the same, without the path -------------------


def test_status_names_the_soul_source_and_state_without_its_path(tmp_path, monkeypatch, capsys):
    soul = tmp_path / "private-dir" / "custom-soul.md"
    soul.parent.mkdir()
    soul.write_text("I am custom.", encoding="utf-8")
    view, body = _status_soul(tmp_path, monkeypatch, capsys, soul_path=soul)
    assert view == {"loaded": True, "state": "loaded", "source": "configured"}
    for fragment in (str(soul), "private-dir", "custom-soul"):
        assert fragment not in body


def test_status_default_soul_is_default(tmp_path, monkeypatch, capsys):
    view, _body = _status_soul(tmp_path, monkeypatch, capsys, default_text="I am Resolver.")
    assert view == {"loaded": True, "state": "loaded", "source": "default"}


@pytest.mark.parametrize(
    ("text", "state"), [(None, "missing"), ("  \n", "empty")], ids=["missing", "empty"]
)
def test_status_soul_that_does_not_read_is_not_loaded(tmp_path, monkeypatch, capsys, text, state):
    soul = tmp_path / "soul.md"
    if text is not None:
        soul.write_text(text, encoding="utf-8")
    view, _body = _status_soul(tmp_path, monkeypatch, capsys, soul_path=soul)
    assert view == {"loaded": False, "state": state, "source": "configured"}


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_status_unreadable_soul_is_not_loaded(tmp_path, monkeypatch, capsys):
    """exists() is true; the status must not call it loaded (it did before #291's rest)."""
    locked = tmp_path / "locked.md"
    locked.write_text("secret persona", encoding="utf-8")
    locked.chmod(0)
    try:
        view, _body = _status_soul(tmp_path, monkeypatch, capsys, soul_path=locked)
    finally:
        locked.chmod(0o600)
    assert view == {"loaded": False, "state": "unreadable", "source": "configured"}
