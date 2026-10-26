"""#152: neutral built-in identity anchors; a configured list always wins.

- The built-in anchors are generic (no persona, name or role) and live in one constant.
- ``agent-config.json`` ``identity_anchors`` wins over them: in the library resolver, in
  The chat server (which used to ignore the file and always use the built-in list) and
  in the observer's anchors panel (which used to show a hard-coded list).
- The persona-specific example lives in ``examples/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from stiracore.config import resolve_config
from stiracore.config.models import AgentConfig, GatewayConfig
from stiracore.core import DEFAULT_IDENTITY_ANCHORS, StiraConfig, StiraEngine
from stiracore.core.engine import BreakLevel
from stiracore.observability.config_api import ConfigStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "personas" / "resolver" / "agent-config.json"
MINE = ["Answer in plain words", "Keep the user's files intact"]


def test_the_built_in_anchors_are_the_neutral_constant() -> None:
    assert StiraConfig().identity_anchors == list(DEFAULT_IDENTITY_ANCHORS)
    assert AgentConfig().identity_anchors == list(DEFAULT_IDENTITY_ANCHORS)
    assert len(DEFAULT_IDENTITY_ANCHORS) == 3
    # Plain English text with no persona-specific script or role words.
    for anchor in DEFAULT_IDENTITY_ANCHORS:
        assert anchor.isascii() and anchor.strip() == anchor and anchor
    # Every config gets its own list (no shared mutable default).
    first, second = StiraConfig(), StiraConfig()
    first.identity_anchors.append("x")
    assert second.identity_anchors == list(DEFAULT_IDENTITY_ANCHORS)


def test_a_configured_list_wins_and_a_missing_key_takes_the_defaults(tmp_path) -> None:
    configured = tmp_path / "configured.json"
    configured.write_text(json.dumps({"identity_anchors": MINE}), encoding="utf-8")
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"harvest_stage_auto": False}), encoding="utf-8")
    tools = tmp_path / "tool-config.json"

    agent = AgentConfig.load(configured, tool_config_path=tools)
    assert agent.identity_anchors == MINE
    assert resolve_config(GatewayConfig(), agent).stira_config.identity_anchors == MINE
    other = AgentConfig.load(partial, tool_config_path=tools)
    assert other.identity_anchors == list(DEFAULT_IDENTITY_ANCHORS)
    assert AgentConfig.load(tmp_path / "absent.json", tool_config_path=tools).identity_anchors == (
        list(DEFAULT_IDENTITY_ANCHORS)
    )


def test_saving_keeps_a_configured_list(tmp_path) -> None:
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"identity_anchors": MINE}), encoding="utf-8")
    agent = AgentConfig.load(path, tool_config_path=tmp_path / "tool-config.json")
    agent.save()
    assert json.loads(path.read_text(encoding="utf-8"))["identity_anchors"] == MINE


def test_the_example_is_a_valid_agent_config(tmp_path) -> None:
    agent = AgentConfig.load(EXAMPLE, tool_config_path=tmp_path / "tool-config.json")
    assert agent.identity_anchors
    assert agent.identity_anchors != list(DEFAULT_IDENTITY_ANCHORS)
    assert set(json.loads(EXAMPLE.read_text(encoding="utf-8"))) == {"identity_anchors"}


def test_the_identity_notice_lists_the_configured_anchors() -> None:
    from stiracore.core import NeuralState

    engine = StiraEngine(NeuralState(stability=0.4), StiraConfig(identity_anchors=MINE))
    assert engine.pre_turn().level == BreakLevel.WARNING
    notice = engine.identity_injection()
    assert "1. Answer in plain words\n2. Keep the user's files intact\n" in notice
    assert DEFAULT_IDENTITY_ANCHORS[0] not in notice


# --- The chat server reads agent-config.json ----------------------------------------


class _Agent:
    instances: ClassVar[list] = []

    def __init__(self, *, tools=(), state=None, config=None, **_kwargs):
        from types import SimpleNamespace

        self.engine = StiraEngine(state=state, config=config)
        self.tools = tuple(tools)
        self._stimulus_classifier = None
        self.prompt_cache = SimpleNamespace(enabled=False, ttl=None)
        self.context_management = SimpleNamespace(enabled=False)
        self.last_prompt_cache = self.last_context_management = None
        _Agent.instances.append(self)


def _stira(monkeypatch, data_dir: Path):
    from dashboard import stira_server

    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "NXPAgent", _Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    _Agent.instances = []
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    app = stira_server.create_app(store)
    return app, _Agent.instances[-1]


@pytest.mark.parametrize(
    ("file", "expected", "banner", "warned"),
    [
        ({"identity_anchors": MINE}, MINE, "configured (2)", False),
        (
            {"identity_anchors": list(DEFAULT_IDENTITY_ANCHORS)},
            list(DEFAULT_IDENTITY_ANCHORS),
            "configured (3, same as built-in)",
            False,
        ),
        ({"harvest_stage_auto": False}, list(DEFAULT_IDENTITY_ANCHORS), "default (3)", True),
        (None, list(DEFAULT_IDENTITY_ANCHORS), "default (3)", True),
    ],
)
def test_the_chat_server_uses_the_anchors_of_its_agent_config(
    tmp_path, monkeypatch, capsys, file, expected, banner, warned
) -> None:
    from dashboard import stira_server

    data_dir = tmp_path / "stira"
    data_dir.mkdir()
    if file is not None:
        (data_dir / "agent-config.json").write_text(json.dumps(file), encoding="utf-8")
    app, agent = _stira(monkeypatch, data_dir)
    try:
        assert agent.engine.config.identity_anchors == expected
        assert agent.engine.config.critical_action == "halt"
        out = capsys.readouterr().out
        lines = [line.strip() for line in out.splitlines()]
        assert f"Identity anchors: {banner}" in lines
        # The banner names the origin and the count, never the text.
        assert not any(anchor in out for anchor in expected)
        # Migration notice: once per start, fixed code, no path, only without the key.
        assert out.count(stira_server.ANCHORS_DEFAULT_CODE) == (1 if warned else 0)
        if warned:
            warning = next(line for line in lines if stira_server.ANCHORS_DEFAULT_CODE in line)
            assert warning == stira_server.ANCHORS_DEFAULT_WARNING
            assert str(tmp_path) not in warning and "stira/" not in warning
        shown = app.test_client().get("/api/config").get_json()
        assert shown["identity_anchors"] == expected
    finally:
        app.extensions["lifecycle"].shutdown()


def test_the_anchors_banner_handles_every_case() -> None:
    from dashboard.stira_server import _anchors_banner

    assert _anchors_banner(None, configured=True) == "unavailable"
    assert _anchors_banner(None, configured=False) == "unavailable"
    assert _anchors_banner(DEFAULT_IDENTITY_ANCHORS, configured=False) == "default (3)"
    assert _anchors_banner(list(DEFAULT_IDENTITY_ANCHORS), configured=True) == (
        "configured (3, same as built-in)"
    )
    assert _anchors_banner(MINE, configured=True) == "configured (2)"


# --- The observer's anchors panel ----------------------------------------------------


def _observer(tmp_path, agent_file: str | None, chat_agent=None):
    from dashboard.app import create_app

    agent_path = tmp_path / "agent-config.json"
    if agent_file is not None:
        agent_path.write_text(agent_file, encoding="utf-8")
    return create_app(
        state_path=tmp_path / "state.json",
        gateway_config_path=tmp_path / "gateway-config.json",
        agent_config_path=agent_path,
        tool_config_path=tmp_path / "tool-config.json",
        chat_agent=chat_agent,
    )


def _panel(app) -> str:
    html = app.test_client().get("/").get_data(as_text=True)
    start = html.index('class="panel anchors"')
    return html[start : html.index("</section>", start)]


def test_the_observer_shows_saved_anchors_escaped_and_labelled_as_saved(tmp_path) -> None:
    anchors = [*MINE, "<b>not markup</b>"]
    panel = _panel(_observer(tmp_path, json.dumps({"identity_anchors": anchors})))
    assert panel.count("<li>") == 3
    assert "Answer in plain words" in panel
    assert "Keep the user&#39;s files intact" in panel
    assert "&lt;b&gt;not markup&lt;/b&gt;" in panel and "<b>" not in panel
    assert 'data-anchors-source="saved"' in panel
    assert 'data-i18n="observer.anchors_saved_note"' in panel


def test_the_observer_shows_the_defaults_without_a_config(tmp_path) -> None:
    panel = _panel(_observer(tmp_path, None))
    assert panel.count("<li>") == len(DEFAULT_IDENTITY_ANCHORS)
    for anchor in DEFAULT_IDENTITY_ANCHORS:
        assert anchor in panel
    assert 'data-anchors-source="saved"' in panel


def test_with_a_chat_agent_the_panel_shows_the_running_anchors_not_the_file(tmp_path) -> None:
    from unittest.mock import Mock

    agent = Mock(return_value="ok")
    agent.engine = StiraEngine(config=StiraConfig(identity_anchors=MINE))
    app = _observer(tmp_path, json.dumps({"identity_anchors": MINE}), chat_agent=agent)
    # Edited after start-up: the running agent still uses MINE, and the panel says so.
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"identity_anchors": ["Edited after start-up"]}), encoding="utf-8"
    )
    panel = _panel(app)
    assert "Answer in plain words" in panel and "Edited after start-up" not in panel
    assert 'data-anchors-source="running"' in panel
    assert "anchors_saved_note" not in panel


def test_without_a_chat_agent_an_edit_is_shown_as_saved_not_running(tmp_path) -> None:
    app = _observer(tmp_path, json.dumps({"identity_anchors": MINE}))
    (tmp_path / "agent-config.json").write_text(
        json.dumps({"identity_anchors": ["Edited after start-up"]}), encoding="utf-8"
    )
    panel = _panel(app)
    assert "Edited after start-up" in panel
    assert 'data-anchors-source="saved"' in panel and "anchors_running_note" not in panel


def test_the_observer_says_unavailable_for_an_unreadable_config(tmp_path) -> None:
    panel = _panel(_observer(tmp_path, "{not json"))
    assert "<li>" not in panel
    assert 'data-i18n="observer.anchors_unavailable"' in panel


class _OddError(Exception):
    pass


@pytest.mark.parametrize("error", [KeyError("zq-odd-91"), _OddError("zq-odd-91")])
def test_any_error_shows_the_fixed_unavailable_text(tmp_path, monkeypatch, error) -> None:
    app = _observer(tmp_path, None)

    def broken():
        raise error

    monkeypatch.setattr(app.extensions["config_store"], "resolved", broken)
    response = app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-i18n="observer.anchors_unavailable"' in html
    assert "zq-odd-91" not in html


def test_a_broken_running_agent_shows_unavailable(tmp_path) -> None:
    from unittest.mock import Mock, PropertyMock

    agent = Mock(return_value="ok")
    type(agent).engine = PropertyMock(side_effect=_OddError("zq-odd-91"))
    response = _observer(tmp_path, None, chat_agent=agent).test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-i18n="observer.anchors_unavailable"' in html and "zq-odd-91" not in html


def test_an_empty_list_is_reported_as_empty_not_unreadable(tmp_path) -> None:
    panel = _panel(_observer(tmp_path, json.dumps({"identity_anchors": []})))
    assert "<li>" not in panel
    assert 'data-i18n="observer.anchors_empty"' in panel
    assert "observer.anchors_unavailable" not in panel
    # The file itself is invalid: AgentConfig (and so the chat server) refuses it.
    with pytest.raises(ValueError, match="identity_anchors"):
        AgentConfig.load(tmp_path / "agent-config.json", tool_config_path=tmp_path / "t.json")
