"""Persistent gateway and agent configuration with layered resolution."""

from .models import (
    DEFAULT_AGENT_CONFIG_PATH,
    DEFAULT_GATEWAY_CONFIG_PATH,
    AgentConfig,
    GatewayConfig,
)
from .resolver import ResolvedConfig, apply_env_overrides, resolve, resolve_config

__all__ = [
    "DEFAULT_AGENT_CONFIG_PATH",
    "DEFAULT_GATEWAY_CONFIG_PATH",
    "AgentConfig",
    "GatewayConfig",
    "ResolvedConfig",
    "apply_env_overrides",
    "resolve",
    "resolve_config",
]
