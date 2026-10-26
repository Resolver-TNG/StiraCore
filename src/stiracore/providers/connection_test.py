"""Secret-free model connection test (#208).

One lightweight request (a one-word prompt, a tiny output budget, no tools, no
system prompt) checks that a provider/model answers. The result carries only a
reason *code*, an HTTP status and an exception class name — never an exception
message, a header, a URL with credentials, or a key — so it is safe to show in
the UI and to log.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from strands.models import Model

from .credentials import MissingCredentialError, require_credentials
from .factory import ProviderUnavailableError, create_model, load_provider_class
from .spec import ModelSpec

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 20.0
# Small, but large enough that providers which reject tiny budgets still answer.
PROBE_MAX_TOKENS = 16
PROBE_PROMPT = "ping"

REASONS = (
    "ok",
    "provider_unavailable",
    "missing_credential",
    "invalid_config",
    "auth_failed",
    "model_not_found",
    "bad_request",
    "throttled",
    "context_overflow",
    "unreachable",
    "server_error",
    "timeout",
    "busy",
    "error",
)


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    reason: str
    provider: str
    model_id: str
    latency_ms: int | None = None
    http_status: int | None = None
    error_type: str | None = None
    # Variable NAME for missing_credential, extra name for provider_unavailable.
    env: str | None = None
    extra: str | None = None
    output_tokens: int | None = None
    input_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _status_of(exc: BaseException) -> int | None:
    for candidate in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "status"):
            value = getattr(candidate, attr, None)
            if type(value) is int and 100 <= value <= 599:
                return value
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):  # botocore ClientError
        value = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if type(value) is int:
            return value
    return None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


_BOTO_CREDENTIAL_ERRORS = frozenset(
    {
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "TokenRetrievalError",
        "SSOTokenLoadError",
        "UnauthorizedSSOTokenError",
        "NoAuthTokenError",
    }
)
_AUTH_CODES = ("AccessDenied", "UnrecognizedClient", "ExpiredToken", "InvalidSignature")


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an exception (and its causes) to ``(reason, http_status)``."""
    status = None
    for item in _chain(exc):
        name = type(item).__name__
        status = status or _status_of(item)
        if isinstance(item, TimeoutError) or name in ("ReadTimeout", "APITimeoutError"):
            return "timeout", status
        if isinstance(item, MissingCredentialError) or name in _BOTO_CREDENTIAL_ERRORS:
            return "missing_credential", status
        if isinstance(item, ProviderUnavailableError):
            return "provider_unavailable", status
        if name == "ModelThrottledException":
            return "throttled", status or 429
        if name == "ContextWindowOverflowException":
            return "context_overflow", status
        response = getattr(item, "response", None)
        if isinstance(response, Mapping) and "Error" in response:
            code = str(response["Error"].get("Code", ""))
            if code.startswith(_AUTH_CODES):
                return "auth_failed", status
            if code == "ResourceNotFoundException":
                return "model_not_found", status
            if code == "ThrottlingException":
                return "throttled", status
        if "Connect" in name or name in ("EndpointConnectionError", "ConnectionError"):
            return "unreachable", status
    if status in (401, 403):
        return "auth_failed", status
    if status == 404:
        return "model_not_found", status
    if status == 429:
        return "throttled", status
    if status in (400, 413, 422):
        return "bad_request", status
    if status is not None and status >= 500:
        return "server_error", status
    if any(isinstance(item, (ConnectionError, OSError)) for item in _chain(exc)):
        return "unreachable", status
    return "error", status


async def _consume(model: Model) -> dict[str, Any]:
    messages = [{"role": "user", "content": [{"text": PROBE_PROMPT}]}]
    usage: dict[str, Any] = {}
    async for event in model.stream(messages, tool_specs=None, system_prompt=None):
        if isinstance(event, Mapping):
            metadata = event.get("metadata")
            if isinstance(metadata, Mapping) and isinstance(metadata.get("usage"), Mapping):
                usage = dict(metadata["usage"])
    return usage


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def run_connection_test(
    spec: ModelSpec,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    model_builder: Callable[[ModelSpec], Model] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ConnectionTestResult:
    """Send one tiny request through ``spec`` and report a secret-free result.

    ``model_builder`` replaces model construction (tests pass an offline fake).
    The request runs in a daemon thread and is abandoned after ``timeout``.
    """
    base = {"provider": spec.provider, "model_id": spec.model_id}
    if model_builder is None:
        try:
            load_provider_class(spec.provider)
        except ProviderUnavailableError as exc:
            return ConnectionTestResult(
                False,
                "provider_unavailable",
                extra=exc.extra,
                error_type=type(exc).__name__,
                **base,
            )
    try:
        require_credentials(
            spec.provider,
            api_key_env=spec.options.get("api_key_env"),
            base_url=spec.options.get("base_url"),
            environ=environ,
        )
    except MissingCredentialError as exc:
        return ConnectionTestResult(
            False, "missing_credential", env=exc.env_name, error_type=type(exc).__name__, **base
        )
    try:
        if model_builder is not None:
            model = model_builder(spec)
        else:
            model = create_model(spec, max_tokens=PROBE_MAX_TOKENS)
    except MissingCredentialError as exc:
        return ConnectionTestResult(
            False, "missing_credential", env=exc.env_name, error_type=type(exc).__name__, **base
        )
    except (TypeError, ValueError) as exc:
        return ConnectionTestResult(False, "invalid_config", error_type=type(exc).__name__, **base)
    except Exception as exc:  # noqa: BLE001 - #601: a result, never a 500 (no message)
        reason, status = classify_error(exc)
        return ConnectionTestResult(
            False, reason, http_status=status, error_type=type(exc).__name__, **base
        )

    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["usage"] = asyncio.run(_consume(model))
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised
            outcome["error"] = exc

    started = time.monotonic()
    worker = threading.Thread(target=work, name="stira-connection-test", daemon=True)
    worker.start()
    worker.join(timeout)
    latency = int((time.monotonic() - started) * 1000)
    if worker.is_alive():
        logger.info("connection test timed out: provider=%s", spec.provider)
        return ConnectionTestResult(False, "timeout", latency_ms=latency, **base)
    error = outcome.get("error")
    if error is not None:
        reason, status = classify_error(error)
        # Class name and status only: provider messages may quote request data.
        logger.info(
            "connection test failed: provider=%s reason=%s type=%s status=%s",
            spec.provider,
            reason,
            type(error).__name__,
            status,
        )
        return ConnectionTestResult(
            False,
            reason,
            latency_ms=latency,
            http_status=status,
            error_type=type(error).__name__,
            **base,
        )
    usage = outcome.get("usage") or {}
    return ConnectionTestResult(
        True,
        "ok",
        latency_ms=latency,
        input_tokens=_int_or_none(usage.get("inputTokens")),
        output_tokens=_int_or_none(usage.get("outputTokens")),
        **base,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "PROBE_MAX_TOKENS",
    "REASONS",
    "ConnectionTestResult",
    "classify_error",
    "run_connection_test",
]
