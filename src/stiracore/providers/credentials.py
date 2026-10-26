"""Provider credentials by reference only (#205).

Configuration stores the *name* of an environment variable (``api_key_env``),
never its value. The value is read from the process environment only when a
model client is built and is passed straight to that client. Status helpers
report "configured / not configured" and never return or log the value.
Error messages never echo user-supplied values (a pasted key must not end up
in an HTTP error or a log line).
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,127}\Z")

# Environment variables each SDK reads by default when no key is configured.
DEFAULT_API_KEY_ENV: Mapping[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_responses": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "writer": "WRITER_API_KEY",
    "llamaapi": "LLAMA_API_KEY",
}
# google-genai also accepts GOOGLE_API_KEY.
_ALTERNATE_API_KEY_ENV: Mapping[str, tuple[str, ...]] = {"gemini": ("GOOGLE_API_KEY",)}
AWS_CHAIN_PROVIDERS = frozenset({"bedrock", "sagemaker"})
LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})
# Providers whose client accepts ``api_key`` from ``api_key_env``.
API_KEY_PROVIDERS = frozenset(DEFAULT_API_KEY_ENV) | {"litellm", "bedrock"}
ORGANIZATION_PROVIDERS = frozenset({"openai", "openai_responses"})

_AWS_ENV_HINTS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


class MissingCredentialError(ValueError):
    """A required credential environment variable is not set (value never shown)."""

    def __init__(self, provider: str, env_name: str) -> None:
        super().__init__(
            f"Model provider '{provider}' needs an API key in the environment variable "
            f"{env_name}, which is not set (or empty). Export it before starting Stira; "
            "do not put the key in a configuration file."
        )
        self.provider = provider
        self.env_name = env_name


def validate_env_name(value: Any, field: str) -> str:
    """Accept an environment variable *name*; the message never echoes the input."""
    if not isinstance(value, str) or not ENV_NAME_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be the NAME of an environment variable (letters, digits, '_'), "
            "not a secret value"
        )
    return value


def validate_base_url(value: Any, field: str = "base_url") -> str:
    """http(s) URL without embedded credentials; the message never echoes the input."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty http(s) URL")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{field} must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must not contain credentials; use api_key_env instead")
    if parts.query or parts.fragment:
        raise ValueError(f"{field} must not contain a query string or fragment")
    return value.strip()


def is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_warnings(provider: str, base_url: str | None, has_key: bool) -> list[str]:
    """Human-readable warnings about where requests (and keys) would be sent."""
    if not base_url:
        return []
    warnings = []
    loopback = is_loopback_url(base_url)
    if provider in LOCAL_PROVIDERS and not loopback:
        warnings.append(
            f"{provider} base_url is not a loopback address; prompts and memory will "
            "leave this machine"
        )
    if has_key and not loopback and urlsplit(base_url).scheme == "http":
        warnings.append("an API key would be sent over plain HTTP to a non-loopback host")
    return warnings


def _env_value(name: str, environ: Mapping[str, str]) -> str | None:
    value = environ.get(name)
    return value if isinstance(value, str) and value.strip() else None


def resolve_api_key(
    provider: str,
    api_key_env: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the value of an explicitly named key variable (None if none named)."""
    if api_key_env is None:
        return None
    validate_env_name(api_key_env, "api_key_env")
    if provider not in API_KEY_PROVIDERS:
        raise ValueError(f"Model provider '{provider}' does not use api_key_env")
    value = _env_value(api_key_env, os.environ if environ is None else environ)
    if value is None:
        raise MissingCredentialError(provider, api_key_env)
    return value


def require_credentials(
    provider: str,
    *,
    api_key_env: str | None = None,
    base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail fast with a clear message before a server starts or a model is switched.

    A named variable must be set. Without one, providers that need a key must
    find their SDK default variable, unless a custom ``base_url`` is set
    (OpenAI-compatible local servers often accept any key or none).
    """
    env = os.environ if environ is None else environ
    if api_key_env is not None:
        resolve_api_key(provider, api_key_env, environ=env)
        return
    default = DEFAULT_API_KEY_ENV.get(provider)
    if default is None or base_url:
        return
    names = (default, *_ALTERNATE_API_KEY_ENV.get(provider, ()))
    if not any(_env_value(name, env) for name in names):
        raise MissingCredentialError(provider, default)


def resolve_named_env(
    provider: str, env_name: str | None, field: str, environ: Mapping[str, str] | None = None
) -> str | None:
    if env_name is None:
        return None
    validate_env_name(env_name, field)
    value = _env_value(env_name, os.environ if environ is None else environ)
    if value is None:
        raise MissingCredentialError(provider, env_name)
    return value


def _aws_chain_hint(environ: Mapping[str, str]) -> bool | None:
    if any(_env_value(name, environ) for name in _AWS_ENV_HINTS):
        return True
    home = Path(environ.get("HOME") or environ.get("USERPROFILE") or "~").expanduser()
    for name, default in (
        ("AWS_SHARED_CREDENTIALS_FILE", home / ".aws" / "credentials"),
        ("AWS_CONFIG_FILE", home / ".aws" / "config"),
    ):
        path = Path(environ[name]).expanduser() if environ.get(name) else default
        if path.is_file():
            return True
    return None  # may still come from an instance/role; only the SDK can tell


def credential_status(
    provider: str,
    *,
    api_key_env: str | None = None,
    base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Describe credential readiness without ever including a secret value.

    ``configured`` is True/False, or None when the AWS chain may still resolve
    credentials at call time (instance roles, SSO without local hints).
    """
    env = os.environ if environ is None else environ
    status: dict[str, Any] = {"provider": provider, "base_url": base_url}
    if provider in AWS_CHAIN_PROVIDERS and api_key_env is None:
        status.update(kind="aws_chain", env=None, configured=_aws_chain_hint(env))
    elif provider in LOCAL_PROVIDERS:
        status.update(kind="none", env=None, configured=True)
    else:
        name = api_key_env or DEFAULT_API_KEY_ENV.get(provider)
        if name is None:  # e.g. LiteLLM: backend-specific variables
            status.update(kind="backend", env=None, configured=None)
        else:
            present = _env_value(name, env) is not None or (
                api_key_env is None
                and any(_env_value(n, env) for n in _ALTERNATE_API_KEY_ENV.get(provider, ()))
            )
            status.update(kind="api_key", env=name, configured=present)
    status["warnings"] = endpoint_warnings(
        provider, base_url, has_key=status["kind"] == "api_key" and bool(status["configured"])
    )
    return status


__all__ = [
    "API_KEY_PROVIDERS",
    "AWS_CHAIN_PROVIDERS",
    "DEFAULT_API_KEY_ENV",
    "LOCAL_PROVIDERS",
    "ORGANIZATION_PROVIDERS",
    "MissingCredentialError",
    "credential_status",
    "endpoint_warnings",
    "is_loopback_url",
    "require_credentials",
    "resolve_api_key",
    "resolve_named_env",
    "validate_base_url",
    "validate_env_name",
]
