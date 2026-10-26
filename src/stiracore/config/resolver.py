"""Resolve code defaults, persisted layers, and compatibility environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from stiracore.core.engine import StiraConfig
from stiracore.providers import ModelSpec, resolve_max_output_tokens
from stiracore.providers.credentials import credential_status, require_credentials
from stiracore.tools.tool_config import ToolConfig

from .models import AgentConfig, GatewayConfig, _normalise_stira_overrides


@dataclass(frozen=True)
class ResolvedConfig:
    """Effective runtime values after all configuration layers are applied."""

    model_id: str
    max_output_tokens: int
    region: str
    port: int
    state_base_path: str
    hb_interval_floor_sec: float
    hb_interval_ceil_sec: float
    jev_enabled: bool
    notify_targets: tuple[str, ...]
    identity_anchors: tuple[str, ...]
    soul_path: str | None
    read_only_paths: tuple[str, ...]
    distiller_mode: str
    distiller_model_id: str
    tool_config: ToolConfig
    hb_session_mode: str
    hb_task_policy: str | dict
    autonomy_boundary: dict
    stira_config: StiraConfig
    # Provider selection (#205); defaults keep the historical Bedrock behaviour.
    provider: str = "bedrock"
    base_url: str | None = None
    api_key_env: str | None = None
    organization_env: str | None = None
    # LLM distiller provider (#207); Bedrock Haiku stays the default.
    distiller_provider: str = "bedrock"
    distiller_base_url: str | None = None
    distiller_api_key_env: str | None = None
    # The agent file's bc_window_size was rounded up to the minimum on load (#337).
    bc_window_clamped: bool = False
    protected_layers_clamped: bool = False
    hitl_timeout_clamped: bool = False
    # The gateway file's heartbeat bounds were inverted; the defaults are used (#512).
    hb_intervals_reset: bool = False
    # The agent file's breaker thresholds broke a pair rule; the defaults are used (#512).
    breaker_pairs_reset: bool = False

    def model_spec(self) -> ModelSpec:
        """Secret-free spec; ``*_env`` names are resolved only when the model is built."""
        return model_spec_from(
            self.provider,
            self.model_id,
            region=self.region,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            organization_env=self.organization_env,
        )

    def distiller_spec(self) -> ModelSpec:
        """Secret-free spec for the LLM distiller (used only in ``llm`` mode)."""
        return model_spec_from(
            self.distiller_provider,
            self.distiller_model_id,
            region=self.region,
            base_url=self.distiller_base_url,
            api_key_env=self.distiller_api_key_env,
        )

    def nxp_model_kwargs(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Keyword arguments selecting this model for ``NXPAgent`` (see ``model_kwargs``)."""
        return model_kwargs(
            self.provider,
            self.model_id,
            region=self.region,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            organization_env=self.organization_env,
            environ=environ,
        )

    def credential_status(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        return credential_status(
            self.provider, api_key_env=self.api_key_env, base_url=self.base_url, environ=environ
        )


def model_spec_from(
    provider: str,
    model_id: str,
    *,
    region: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    organization_env: str | None = None,
) -> ModelSpec:
    options: dict[str, Any] = {}
    if provider == "bedrock" and region:
        options["region_name"] = region
    for key, value in (
        ("base_url", base_url),
        ("api_key_env", api_key_env),
        ("organization_env", organization_env),
    ):
        if value is not None:
            options[key] = value
    return ModelSpec(provider, model_id, options)


def model_kwargs(
    provider: str,
    model_id: str,
    *,
    region: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    organization_env: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """``NXPAgent`` kwargs: the legacy Bedrock pair, or ``model=ModelSpec``.

    Plain Bedrock (no endpoint/key overrides) keeps ``model_id``/``region_name``
    so its behaviour is unchanged. Other providers are checked first: a missing
    credential raises ``MissingCredentialError`` naming the variable, never its value.
    """
    if provider == "bedrock" and base_url is None and api_key_env is None:
        return {"model_id": model_id, "region_name": region}
    require_credentials(provider, api_key_env=api_key_env, base_url=base_url, environ=environ)
    spec = model_spec_from(
        provider,
        model_id,
        region=region,
        base_url=base_url,
        api_key_env=api_key_env,
        organization_env=organization_env,
    )
    return {"model": spec}


def apply_env_overrides(
    config: GatewayConfig,
    environ: Mapping[str, str] | None = None,
) -> GatewayConfig:
    """Return a validated gateway config with legacy STIRA_* values applied."""

    env = os.environ if environ is None else environ
    values: dict[str, object] = {}
    if "STIRA_MODEL" in env:
        values["model_id"] = env["STIRA_MODEL"]
    if "AWS_REGION" in env:
        values["region"] = env["AWS_REGION"]
    if "STIRA_PORT" in env:
        try:
            values["port"] = int(env["STIRA_PORT"])
        except (TypeError, ValueError) as exc:
            raise ValueError("STIRA_PORT must be an integer") from exc
    if "STIRA_STATE_PATH" in env:
        values["state_base_path"] = env["STIRA_STATE_PATH"]
    if "STIRA_PROVIDER" in env:
        # Empty means the code default, as in stira_server._provider_config (#272).
        values["provider"] = env["STIRA_PROVIDER"] or "bedrock"
    if "STIRA_BASE_URL" in env:
        values["base_url"] = env["STIRA_BASE_URL"] or None
    if "STIRA_API_KEY_ENV" in env:
        values["api_key_env"] = env["STIRA_API_KEY_ENV"] or None
    return replace(config, **values)


def resolve_config(
    gateway: GatewayConfig | None = None,
    agent: AgentConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve env > agent > gateway > code defaults into runtime values."""

    effective_gateway = apply_env_overrides(gateway or GatewayConfig(), environ)
    effective_agent = agent or AgentConfig()

    # env > agent > gateway. apply_env_overrides already folded STIRA_MODEL into
    # effective_gateway.model_id, so a present env value wins here without a
    # second environ lookup; otherwise the agent override applies.
    env = os.environ if environ is None else environ
    if "STIRA_MODEL" in env:
        model_id = effective_gateway.model_id
    else:
        model_id = effective_agent.model_override or effective_gateway.model_id

    # Provider: env > agent override > gateway. An agent provider override
    # brings its own endpoint/key reference; gateway values never leak into it.
    provider_values = {
        "provider": effective_gateway.provider,
        "base_url": effective_gateway.base_url,
        "api_key_env": effective_gateway.api_key_env,
        "organization_env": effective_gateway.organization_env,
    }
    if effective_agent.provider_override is not None and "STIRA_PROVIDER" not in env:
        provider_values = {
            "provider": effective_agent.provider_override,
            "base_url": effective_agent.base_url_override,
            "api_key_env": effective_agent.api_key_env_override,
            "organization_env": None,
        }
        if "STIRA_MODEL" not in env:
            model_id = effective_agent.model_override

    stira_values = _normalise_stira_overrides(effective_agent.stira_config_overrides or {})
    stira_values["identity_anchors"] = list(effective_agent.identity_anchors)
    stira_config = StiraConfig(**stira_values)

    return ResolvedConfig(
        model_id=model_id,
        # Explicit gateway value, else the bound of the model actually selected (#363).
        max_output_tokens=resolve_max_output_tokens(
            effective_gateway.max_output_tokens,
            provider_values["provider"],
            model_id,
        ),
        region=effective_gateway.region,
        port=effective_gateway.port,
        state_base_path=effective_gateway.state_base_path,
        hb_interval_floor_sec=effective_gateway.hb_interval_floor_sec,
        hb_interval_ceil_sec=effective_gateway.hb_interval_ceil_sec,
        jev_enabled=effective_gateway.jev_enabled,
        notify_targets=tuple(effective_gateway.notify_targets),
        identity_anchors=tuple(effective_agent.identity_anchors),
        soul_path=effective_agent.soul_path,
        read_only_paths=tuple(effective_agent.read_only_paths),
        distiller_mode=effective_agent.distiller_mode,
        distiller_model_id=effective_agent.distiller_model_id,
        distiller_provider=effective_agent.distiller_provider,
        distiller_base_url=effective_agent.distiller_base_url,
        distiller_api_key_env=effective_agent.distiller_api_key_env,
        tool_config=effective_agent.tool_config,
        hb_session_mode=effective_agent.hb_session_mode,
        hb_task_policy=effective_agent.hb_task_policy,
        autonomy_boundary=effective_agent.autonomy_boundary,
        stira_config=stira_config,
        bc_window_clamped=getattr(effective_agent, "bc_window_clamped", False),
        protected_layers_clamped=getattr(effective_agent, "protected_layers_clamped", False),
        hitl_timeout_clamped=getattr(effective_agent, "hitl_timeout_clamped", False),
        # ``apply_env_overrides`` copies the config: the load flag is read from the original.
        hb_intervals_reset=getattr(gateway, "hb_intervals_reset", False) is True,
        # Read from the loaded object itself (review r591 M-1): only the gateway is copied
        # by ``apply_env_overrides``; ``effective_agent`` is ``agent`` or a fresh default.
        breaker_pairs_reset=getattr(agent, "breaker_pairs_reset", False) is True,
        **provider_values,
    )


resolve = resolve_config
