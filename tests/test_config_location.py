"""#272: configuration location, stored-only fields and an empty STIRA_PROVIDER.

- Started outside the repository root, the server read its configuration from the
  working directory and its data from the repository, silently. It now says so
  (start-up warning without a path, ``config_location`` in the status).
- Fields that nothing reads at run time are listed in ``/api/config``.
- ``STIRA_PROVIDER=""`` made the settings resolver fail while the server used
  Bedrock; both now read it as the code default.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.config import resolve_config
from stiracore.config.models import AgentConfig, GatewayConfig
from stiracore.observability.config_api import (
    STORED_ONLY_FIELDS,
    ConfigStore,
    create_config_blueprint,
)

WARNING = "WARNING: the configuration files are not in the data directory"


class _Agent:
    def __init__(self, *, tools=(), state=None, config=None, **_kwargs):
        from stiracore.core import StiraEngine

        self.engine = StiraEngine(state=state, config=config)
        self.tools = tuple(tools)
        self._stimulus_classifier = None
        self.prompt_cache = SimpleNamespace(enabled=False, ttl=None)
        self.context_management = SimpleNamespace(enabled=False)
        self.last_prompt_cache = self.last_context_management = None


def _start(monkeypatch, data_dir, cwd):
    from dashboard import stira_server
    from stiracore.config import models
    from stiracore.observability import config_api
    from stiracore.tools import tool_config

    # The production defaults (the root conftest points them at a temporary root):
    # relative paths, resolved against the working directory set here.
    monkeypatch.setattr(config_api, "DEFAULT_TOOL_CONFIG_PATH", tool_config.DEFAULT_PATH)
    monkeypatch.setattr(
        config_api, "DEFAULT_GATEWAY_CONFIG_PATH", models.DEFAULT_GATEWAY_CONFIG_PATH
    )
    monkeypatch.setattr(config_api, "DEFAULT_AGENT_CONFIG_PATH", models.DEFAULT_AGENT_CONFIG_PATH)
    assert not tool_config.DEFAULT_PATH.is_absolute()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "DATA_DIR_FROM_ENV", False)  # no STIRA_DATA_DIR
    monkeypatch.setattr(stira_server, "NXPAgent", _Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    # No config_store: the server's own defaults, as in production.
    return stira_server.create_app()


def test_started_elsewhere_the_server_warns_without_a_path(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app = _start(monkeypatch, data_dir, elsewhere)
    out = capsys.readouterr().out
    assert WARNING in out
    warning = next(line for line in out.splitlines() if line.startswith("WARNING: the config"))
    assert str(tmp_path) not in warning and "elsewhere" not in warning
    status = app.test_client().get("/api/gateway/status").json
    assert status["config_location"] == {"matches_data_dir": False, "data_dir": "default"}


def test_started_from_the_repository_root_there_is_no_warning(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    data_dir.mkdir(parents=True)
    app = _start(monkeypatch, data_dir, repo)
    assert WARNING not in capsys.readouterr().out
    status = app.test_client().get("/api/gateway/status").json
    assert status["config_location"] == {"matches_data_dir": True, "data_dir": "default"}
    # The rules the server saves are the ones next to its data.
    store = app.extensions["config_store"]
    assert os.path.samefile(store.tool_path.resolve().parent, data_dir)


def test_stored_only_fields_are_listed_and_exist(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store))
    data = app.test_client().get("/api/config").json
    assert data["stored_only_fields"] == list(STORED_ONLY_FIELDS)
    for name in STORED_ONLY_FIELDS:
        assert name in data, name
    gateway, agent = GatewayConfig(), AgentConfig()
    for name in STORED_ONLY_FIELDS:
        assert hasattr(gateway, name) or hasattr(agent, name), name


def test_stored_only_fields_are_not_read_by_the_runtime():
    """Guard for the list: no module outside the config layer reads these names.

    When one starts being used (e.g. #67 enforces autonomy_boundary), drop it from
    STORED_ONLY_FIELDS and from docs/configuration.md.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    allowed = {
        root / "src/stiracore/config/models.py",
        root / "src/stiracore/config/resolver.py",
        root / "src/stiracore/observability/config_api.py",
    }
    sources = [*(root / "src").rglob("*.py"), *(root / "dashboard").glob("*.py")]
    for path in sources:
        if path in allowed or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for name in STORED_ONLY_FIELDS:
            used = [line for line in text.splitlines() if name in line and "TODO(#67)" not in line]
            assert not used, (path, name, used)


@pytest.mark.parametrize("environ", [{"STIRA_PROVIDER": ""}, {"STIRA_PROVIDER": "bedrock"}, {}])
def test_an_empty_provider_variable_is_the_default(environ):
    assert resolve_config(GatewayConfig(), AgentConfig(), environ=environ).provider == "bedrock"


def test_the_server_and_the_resolver_agree_on_an_empty_provider(monkeypatch):
    from dashboard import stira_server

    monkeypatch.setenv("STIRA_PROVIDER", "")
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(provider="ollama"))
    server_side = stira_server._provider_config().provider
    resolver_side = resolve_config(
        GatewayConfig(provider="ollama"), AgentConfig(), environ={"STIRA_PROVIDER": ""}
    ).provider
    assert server_side == resolver_side == "bedrock"
