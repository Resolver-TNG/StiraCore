import json
from pathlib import Path

import pytest

from stiracore.config import (
    AgentConfig,
    GatewayConfig,
    apply_env_overrides,
    resolve_config,
)
from stiracore.tools.tool_config import ToolConfig


def test_gateway_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    config = GatewayConfig(
        model_id="gateway-model",
        region="us-west-2",
        port=8765,
        state_base_path=str(tmp_path / "state"),
        hb_interval_floor_sec=120,
        hb_interval_ceil_sec=7200,
        jev_enabled=True,
        notify_targets=["dashboard", "slack"],
        path=path,
    )

    config.save()
    loaded = GatewayConfig.load(path)

    assert loaded == config
    assert loaded.as_dict() == config.as_dict()


def test_agent_config_round_trip_with_embedded_tool_config(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    tool_path = tmp_path / "legacy-tool-config.json"
    tool_config = ToolConfig(
        mode="custom",
        enabled_tools={"calculator"},
        shell_allowlist=["pwd"],
        path=tool_path,
    )
    config = AgentConfig(
        identity_anchors=["Keep memory coherent"],
        soul_path="SOUL.md",
        model_override="agent-model",
        tool_config=tool_config,
        hb_session_mode="continue",
        hb_task_policy={"task": "review"},
        autonomy_boundary={"pr_create": {"require_confirmation": False}},
        stira_config_overrides={"decay_min_interval_sec": 30.0},
        path=path,
    )

    config.save()
    loaded = AgentConfig.load(path, tool_config_path=tool_path)

    assert loaded == config
    assert loaded.as_dict() == config.as_dict()
    assert loaded.tool_config.path == tool_path


def test_defaults_are_safe_and_stable() -> None:
    gateway = GatewayConfig()
    agent = AgentConfig()

    assert gateway.model_id == "global.anthropic.claude-sonnet-5"
    assert gateway.region == "ap-northeast-1"
    assert gateway.port == 5555
    assert gateway.hb_interval_floor_sec == 300.0
    assert gateway.hb_interval_ceil_sec == 10800.0
    assert gateway.jev_enabled is False
    assert gateway.notify_targets == []
    assert agent.hb_session_mode == "fresh"
    for action in ("pr_create", "merge_main", "destructive", "external_send"):
        assert agent.autonomy_boundary[action]["require_confirmation"] is True


def test_resolve_uses_agent_then_gateway_then_code_defaults() -> None:
    code_default = resolve_config(environ={})
    gateway_value = resolve_config(GatewayConfig(model_id="gateway-model"), environ={})
    agent_value = resolve_config(
        GatewayConfig(model_id="gateway-model"),
        AgentConfig(model_override="agent-model"),
        environ={},
    )

    assert code_default.model_id == "global.anthropic.claude-sonnet-5"
    assert gateway_value.model_id == "gateway-model"
    assert agent_value.model_id == "agent-model"


def test_resolve_builds_stira_config_from_agent_overrides() -> None:
    agent = AgentConfig(
        identity_anchors=["Agent identity"],
        stira_config_overrides={"decay_min_interval_sec": 42.0},
    )

    resolved = resolve_config(agent=agent, environ={})

    assert resolved.stira_config.identity_anchors == ["Agent identity"]
    assert resolved.stira_config.decay_min_interval_sec == 42.0
    assert resolved.stira_config.stability_warning == 0.5


def test_environment_is_highest_priority() -> None:
    resolved = resolve_config(
        GatewayConfig(
            model_id="gateway-model",
            region="gateway-region",
            port=1234,
            state_base_path="gateway-state",
        ),
        AgentConfig(model_override="agent-model"),
        environ={
            "STIRA_MODEL": "env-model",
            "AWS_REGION": "env-region",
            "STIRA_PORT": "9876",
            "STIRA_STATE_PATH": "env-state",
        },
    )

    assert resolved.model_id == "env-model"
    assert resolved.region == "env-region"
    assert resolved.port == 9876
    assert resolved.state_base_path == "env-state"


def test_apply_env_overrides_does_not_mutate_gateway() -> None:
    gateway = GatewayConfig(model_id="stored-model")

    overridden = apply_env_overrides(gateway, {"STIRA_MODEL": "env-model"})

    assert gateway.model_id == "stored-model"
    assert overridden.model_id == "env-model"


@pytest.mark.parametrize("config_type", [GatewayConfig, AgentConfig])
def test_unknown_top_level_key_is_rejected(tmp_path: Path, config_type: type) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"unknown": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid"):
        config_type.load(path)


def test_unknown_embedded_tool_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps({"tool_config": {"mode": "custom", "unknown": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="embedded tool"):
        AgentConfig.load(path)


def test_legacy_tool_config_is_loaded_when_agent_section_is_absent(
    tmp_path: Path,
) -> None:
    agent_path = tmp_path / "agent.json"
    tool_path = tmp_path / "tool-config.json"
    tool_path.write_text(
        json.dumps(
            {
                "mode": "custom",
                "enabled_tools": ["calculator"],
                "shell_allowlist": ["pwd"],
            }
        ),
        encoding="utf-8",
    )

    loaded = AgentConfig.load(agent_path, tool_config_path=tool_path)

    assert loaded.tool_config.enabled_tools == {"calculator"}
    assert loaded.tool_config.path == tool_path


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: GatewayConfig(port=True), "port"),
        (lambda: GatewayConfig(port=70000), "port"),
        (lambda: GatewayConfig(hb_interval_floor_sec=-1), "hb_interval_floor_sec"),
        (
            lambda: GatewayConfig(hb_interval_floor_sec=600, hb_interval_ceil_sec=300),
            "hb_interval_floor_sec must be <= hb_interval_ceil_sec",
        ),
        (lambda: GatewayConfig(jev_enabled=1), "jev_enabled"),
        (lambda: AgentConfig(identity_anchors=[]), "identity_anchors"),
        (lambda: AgentConfig(hb_session_mode="reuse"), "hb_session_mode"),
        (lambda: AgentConfig(tool_config={}), "tool_config"),
        (
            lambda: AgentConfig(autonomy_boundary={"merge_main": {"require_confirmation": "yes"}}),
            "require_confirmation",
        ),
        (
            lambda: AgentConfig(stira_config_overrides={"not_a_setting": 1}),
            "Unknown StiraConfig",
        ),
        (
            lambda: AgentConfig(stira_config_overrides={"stability_warning": 2.0}),
            "stability_warning",
        ),
    ],
)
def test_invalid_values_are_rejected(factory, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()


def test_invalid_environment_port_is_rejected() -> None:
    with pytest.raises(ValueError, match="STIRA_PORT"):
        resolve_config(environ={"STIRA_PORT": "not-an-integer"})


@pytest.mark.parametrize(
    ("config_factory", "loader"),
    [
        (
            lambda path: GatewayConfig(model_id="new-model", path=path),
            GatewayConfig.load,
        ),
        (
            lambda path: AgentConfig(identity_anchors=["new"], path=path),
            AgentConfig.load,
        ),
    ],
)
def test_interrupted_atomic_save_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_factory,
    loader,
) -> None:
    path = tmp_path / "config.json"
    original = config_factory(path)
    original.save()
    original_bytes = path.read_bytes()

    replacement = config_factory(path)
    if isinstance(replacement, GatewayConfig):
        replacement.model_id = "replacement-model"
    else:
        replacement.identity_anchors = ["replacement"]

    def fail_replace(source, destination) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr("stiracore.config.models.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        replacement.save()

    assert path.read_bytes() == original_bytes
    assert loader(path).as_dict() == original.as_dict()
    assert not list(tmp_path.glob(".*-config-*"))
