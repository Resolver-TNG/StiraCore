"""External integration tools; registered as WRITE even for OAuth GET requests."""

import json
from typing import Any

import requests
from strands import tool

from stiracore.integrations.http import request_bytes
from stiracore.integrations.oauth import CONNECTORS, validate_provider

MAX_PAYLOAD_BYTES = 64 * 1024


def _body(payload):
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("Payload exceeds limit")
    return encoded


def _error():
    # URLs, query strings, response errors and token material are not safe logs.
    return {"status": "error", "content": [{"text": "Integration request failed or not allowed"}]}


@tool
def webhook_send(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a bounded JSON webhook to an explicit HTTPS endpoint (no redirects).

    Args:
        url: HTTPS destination authorized by the operator.
        payload: JSON object to send.
    """
    try:
        if not isinstance(payload, dict):
            raise TypeError("Payload must be a JSON object")
        status, _ = request_bytes(
            "POST", url, data=_body(payload), headers={"Content-Type": "application/json"}
        )
        return {"status": "success", "content": [{"json": {"http_status": status}}]}
    except (requests.RequestException, ValueError, TypeError, OSError):
        return _error()


@tool
def oauth_api_call(
    provider: str, method: str, url: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call an operator-configured OAuth API. Origins and methods are allowlisted.

    Args:
        provider: Previously registered OAuth provider name.
        method: Allowed HTTP method (GET is still write-capable for breaker policy).
        url: Absolute HTTPS URL on that provider's allowed API origin.
        body: Optional JSON request object.
    """
    try:
        validate_provider(provider)
        connector = CONNECTORS.get(provider)
        if connector is None:
            raise ValueError("Provider is not configured")
        method = method.upper()
        connector.check_api_url(method, url)  # Before any token read or refresh.
        if body is not None and not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        data = _body(body) if body is not None else None
        token = connector.access_token()
        status, response = request_bytes(
            method,
            url,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        text = response.decode("utf-8", errors="replace")
        # A service may echo an Authorization header. Never return known token
        # material to the model; response content remains untrusted source data.
        # The token this request sent first: the stored ones may have changed since
        # (a refresh or a new authorization between the read and the reply).
        for secret in (token, *connector.load_tokens().values()):
            if isinstance(secret, str) and secret and secret != "Bearer":
                text = text.replace(secret, "[REDACTED]")
        return {
            "status": "success",
            "http_status": status,
            "content": [{"text": text[:50000]}],
            "truncated": len(text) > 50000,
        }
    except (requests.RequestException, ValueError, TypeError, OSError):
        return _error()
