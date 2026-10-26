"""Build Strands ``Model`` instances from a :class:`ModelSpec` (#204).

Each builder maps the neutral spec onto the constructor of the installed
Strands provider class (strands-agents 1.57). Provider SDKs are imported only
when that provider is requested, so a core install (Bedrock only) never needs
``openai``/``anthropic``/``ollama`` and a missing extra fails with a clear
``ProviderUnavailableError`` that names the extra to install.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from typing import Any

from strands.models import Model

from .credentials import (
    ORGANIZATION_PROVIDERS,
    endpoint_warnings,
    resolve_api_key,
    resolve_named_env,
)
from .spec import PROVIDERS, ModelSpec, normalize_provider

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 8192

# Top-level generation options moved into ``params`` for providers whose
# Strands config only accepts ``model_id`` + ``params``.
_PARAM_KEYS = ("temperature", "top_p", "stop", "seed")


class ProviderUnavailableError(ImportError):
    """The provider's optional SDK is not installed."""

    def __init__(self, provider: str, extra: str | None, missing: str) -> None:
        hint = (
            f"pip install 'stiracore[{extra}]'"
            if extra
            else "reinstall stiracore with its core dependencies"
        )
        super().__init__(
            f"Model provider '{provider}' is not available: missing dependency "
            f"'{missing}'. Install it with: {hint}"
        )
        self.provider = provider
        self.extra = extra
        self.missing = missing


def load_provider_class(provider: str) -> type[Model]:
    """Import and return the Strands Model class for ``provider``."""
    info = PROVIDERS[normalize_provider(provider)]
    try:
        module = importlib.import_module(info.module)
    except ModuleNotFoundError as exc:
        raise ProviderUnavailableError(info.name, info.extra, exc.name or str(exc)) from exc
    except PackageNotFoundError as exc:  # openai_responses checks package metadata
        raise ProviderUnavailableError(info.name, info.extra, str(exc) or "?") from exc
    return getattr(module, info.class_name)


def provider_available(provider: str) -> bool:
    try:
        load_provider_class(provider)
    except ProviderUnavailableError:
        return False
    return True


def _split(spec: ModelSpec) -> dict[str, Any]:
    options = dict(spec.options)
    options.pop("capabilities", None)  # Stira metadata (#206), not a constructor argument
    if "model_id" in options:
        raise ValueError("Set the model through ModelSpec.model_id, not options['model_id']")
    return options


def _client_args(options: dict[str, Any]) -> dict[str, Any] | None:
    client_args = dict(options.pop("client_args", None) or {})
    base_url = options.pop("base_url", None)
    if base_url is not None:
        client_args.setdefault("base_url", base_url)
    return client_args or None


def _params(options: dict[str, Any], token_key: str, max_tokens: int | None) -> dict[str, Any]:
    params = dict(options.pop("params", None) or {})
    for key in _PARAM_KEYS:
        if key in options:
            params.setdefault(key, options.pop(key))
    explicit = options.pop("max_tokens", None)
    if explicit is not None:
        params.setdefault(token_key, explicit)
    if max_tokens is not None and not {"max_tokens", "max_completion_tokens", token_key} & set(
        params
    ):
        params[token_key] = max_tokens
    return params


def _apply_credentials(provider: str, options: dict[str, Any]) -> None:
    """Swap ``*_env`` references for values, only inside the constructor kwargs.

    The value never enters the ModelSpec, configuration, or a log message.
    """
    key = resolve_api_key(provider, options.pop("api_key_env", None))
    org_env = options.pop("organization_env", None)
    if org_env is not None and provider not in ORGANIZATION_PROVIDERS:
        raise ValueError(f"Model provider '{provider}' does not use organization_env")
    organization = resolve_named_env(provider, org_env, "organization_env")
    for warning in endpoint_warnings(provider, options.get("base_url"), key is not None):
        logger.warning("model provider %s: %s", provider, warning)
    if key is None and organization is None:
        return
    if provider == "bedrock":
        options["api_key"] = key  # Bedrock API key (bearer token) instead of SigV4
        return
    client_args = dict(options.get("client_args") or {})
    if key is not None:
        client_args["api_key"] = key
    if organization is not None:
        client_args["organization"] = organization
    options["client_args"] = client_args


def _bedrock(cls, spec, options, max_tokens):
    base_url = options.pop("base_url", None)
    if base_url is not None:
        options.setdefault("endpoint_url", base_url)
    if max_tokens is not None:
        options.setdefault("max_tokens", max_tokens)
    return cls(model_id=spec.model_id, **options)


def _anthropic(cls, spec, options, max_tokens):
    client_args = _client_args(options)
    options.setdefault("max_tokens", max_tokens or DEFAULT_MAX_TOKENS)  # required by Anthropic
    params = dict(options.pop("params", None) or {})
    for key in (*_PARAM_KEYS, "stop_sequences"):
        if key in options:
            params.setdefault(key, options.pop(key))
    if params:
        options["params"] = params
    return cls(client_args=client_args, model_id=spec.model_id, **options)


def _params_provider(token_key: str):
    def build(cls, spec, options, max_tokens):
        client_args = _client_args(options)
        params = _params(options, token_key, max_tokens)
        return cls(client_args=client_args, model_id=spec.model_id, params=params, **options)

    return build


def _llamacpp(cls, spec, options, max_tokens):
    base_url = options.pop("base_url", "http://localhost:8080")
    timeout = options.pop("timeout", None)
    params = _params(options, "max_tokens", max_tokens)
    return cls(base_url=base_url, timeout=timeout, model_id=spec.model_id, params=params, **options)


def _ollama(cls, spec, options, max_tokens):
    host = options.pop("base_url", options.pop("host", None))
    client_args = options.pop("client_args", None)
    if max_tokens is not None:
        options.setdefault("max_tokens", max_tokens)
    return cls(host, ollama_client_args=client_args, model_id=spec.model_id, **options)


def _max_tokens_provider(token_key: str):
    def build(cls, spec, options, max_tokens):
        client_args = _client_args(options)
        explicit = options.pop("max_tokens", None)
        value = explicit if explicit is not None else max_tokens
        if value is not None:
            options.setdefault(token_key, value)
        return cls(client_args=client_args, model_id=spec.model_id, **options)

    return build


def _sagemaker(cls, spec, options, max_tokens):
    payload = dict(options.pop("payload_config", None) or {})
    explicit = options.pop("max_tokens", None)
    value = explicit if explicit is not None else max_tokens
    if value is not None:
        payload.setdefault("max_tokens", value)
    boto_session = options.pop("boto_session", None)
    boto_client_config = options.pop("boto_client_config", None)
    endpoint = {"endpoint_name": spec.model_id, **options}
    return cls(
        endpoint_config=endpoint,
        payload_config=payload,
        boto_session=boto_session,
        boto_client_config=boto_client_config,
    )


_BUILDERS: dict[str, Callable[..., Model]] = {
    "bedrock": _bedrock,
    "anthropic": _anthropic,
    "openai": _params_provider("max_tokens"),
    "openai_responses": _params_provider("max_output_tokens"),
    "gemini": _params_provider("max_output_tokens"),
    "litellm": _params_provider("max_tokens"),
    "llamacpp": _llamacpp,
    "ollama": _ollama,
    "mistral": _max_tokens_provider("max_tokens"),
    "writer": _max_tokens_provider("max_tokens"),
    "llamaapi": _max_tokens_provider("max_completion_tokens"),
    "sagemaker": _sagemaker,
}


def create_model(
    spec: ModelSpec,
    *,
    max_tokens: int | None = None,
    model_class: type[Model] | None = None,
) -> Model:
    """Return the Strands ``Model`` described by ``spec``.

    ``max_tokens`` is a default output budget; an explicit ``max_tokens`` (or the
    provider's own key inside ``params``) in ``spec.options`` wins. Generation
    options ``temperature``/``top_p``/``stop``/``seed`` are moved into ``params``
    for providers that only accept ``params``. ``model_class`` replaces the
    imported provider class (tests / subclasses).
    """
    if not isinstance(spec, ModelSpec):
        raise TypeError("spec must be a ModelSpec")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise ValueError("max_tokens must be a positive integer")
    cls = model_class or load_provider_class(spec.provider)
    options = _split(spec)
    _apply_credentials(spec.provider, options)
    return _BUILDERS[spec.provider](cls, spec, options, max_tokens)


def provider_of(model: Any) -> str:
    """Best-effort provider name of a Model instance ("custom" if unknown)."""
    module = type(model).__module__
    for info in PROVIDERS.values():
        if module == info.module:
            return info.name
    return "custom"


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "ProviderUnavailableError",
    "create_model",
    "load_provider_class",
    "provider_available",
    "provider_of",
]
