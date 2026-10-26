"""Why a chat turn's model call failed, in words an operator can act on.

Only a fixed reason code, an HTTP status, the provider name and the endpoint's host[:port]
leave this module: never an exception message (a provider can echo request text or a key),
never a URL path or query.
"""

from __future__ import annotations

import errno
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .connection_test import _chain, _status_of, classify_error

MODEL_FAILURE_CODE = "model_failure"

# The chat's reasons (a superset of the connection test's "unreachable": refused / DNS).
REASONS = (
    "refused",
    "dns_failed",
    "unreachable",
    "timeout",
    "auth_failed",
    "model_not_found",
    "throttled",
    "server_error",
    "missing_credential",
    "provider_unavailable",
    "bad_request",
    "context_overflow",
    "ends_with_assistant",
    "context_window_too_small",
)

# Built-in endpoints when none is configured (display only).
_DEFAULT_ENDPOINTS = {"llamacpp": "http://localhost:8080", "ollama": "http://localhost:11434"}

# The server's own fallback text (ja / en), used when the page has no catalog entry.
MESSAGES = {
    "refused": (
        "モデルのサーバーに接続できません(接続拒否)。サーバーが起動しているか、設定のエンドポイントを確認してください",
        "Cannot connect to the model server (connection refused). Check it runs and the endpoint",
    ),
    "dns_failed": (
        "モデルの接続先のホスト名を解決できません。設定のエンドポイントを確認してください",
        "The model endpoint's host name does not resolve. Check the endpoint in the settings",
    ),
    "unreachable": (
        "モデルの接続先に到達できません。ネットワークと設定のエンドポイントを確認してください",
        "The model endpoint cannot be reached. Check the network and the endpoint",
    ),
    "timeout": (
        "モデルの応答がタイムアウトしました。サーバーの負荷と接続先を確認してください",
        "The model did not answer in time. Check the server's load and the endpoint",
    ),
    "auth_failed": (
        "モデルの認証に失敗しました(401/403)。API キーまたは権限を確認してください",
        "The model rejected the credentials (401/403). Check the API key or permissions",
    ),
    "model_not_found": (
        "指定したモデルが見つかりません(404)。設定のモデル名を確認してください",
        "The model was not found (404). Check the model name in the settings",
    ),
    "throttled": (
        "レート制限に達しました(429)。しばらく待ってから再送してください",
        "Rate limited (429). Wait a moment and send again",
    ),
    "server_error": (
        "モデル側のサーバーエラーです(5xx)。時間をおいて再送してください",
        "The model's server failed (5xx). Try again later",
    ),
    "missing_credential": (
        "モデルの認証情報がありません。API キーの環境変数または AWS の認証情報を設定してください",
        "No credentials for the model. Set the API key variable or the AWS credentials",
    ),
    "provider_unavailable": (
        "このプロバイダの追加パッケージが入っていません。インストールしてから再起動してください",
        "This provider's extra package is not installed. Install it and restart",
    ),
    "bad_request": (
        "モデルがリクエストを受け付けませんでした(4xx)。モデル名と設定を確認してください",
        "The model refused the request (4xx). Check the model name and settings",
    ),
    "context_overflow": (
        "会話がモデルの入力上限を超えました。新しい会話を始めてください",
        "The conversation exceeds the model's input limit. Start a new conversation",
    ),
    # #256: the request ended with the model's own reply (no prefill support). One known path
    # (reproduced offline): a tool call returned as plain text, so no tool result follows.
    "ends_with_assistant": (
        (
            "モデルへの入力がモデル自身の返答で終わったため拒否されました"
            "(ツール呼び出しが文章として返された場合などに起きます)。もう一度送るか、別のモデルを選んでください"
        ),
        (
            "The model refused a conversation that ends with its own reply (this can happen, for "
            "example, when a tool call comes back as plain text). Send again or choose another model"
        ),
    ),
    # ADR 0026 B1: SOUL + Identity Anchors + tool definitions exceed 90% of the known window.
    "context_window_too_small": (
        (
            "SOUL・Identity Anchors・ツール定義だけでモデルのコンテキスト窓の 9 割を超えるため"
            "送信しませんでした。窓を増やす(context_window_tokens / サーバーの設定)、SOUL を短くする、"
            "窓の大きいモデルを使う、ツール定義が大きい場合は context_profile=small/tiny を試す、"
            "のいずれかをしてください"
        ),
        (
            "Not sent: SOUL, Identity Anchors and the tool definitions alone exceed 90% of the "
            "model's context window. Increase the window (context_window_tokens / the server), "
            "shorten the SOUL, use a model with a larger window, or, if the tool definitions "
            "are large, try context_profile=small or tiny"
        ),
    ),
}

# #256: Bedrock's refusal of a conversation that ends with the assistant (models without
# prefill support). Matched on the provider's text inside this module only; never shown.
_ENDS_WITH_ASSISTANT_MARKERS = ("assistant message prefill", "must end with a user message")


def endpoint_host(provider: str | None, base_url: Any) -> str | None:
    """``host[:port]`` of the endpoint (no scheme, path, query or user info)."""
    url = base_url if isinstance(base_url, str) and base_url else None
    url = url or _DEFAULT_ENDPOINTS.get(str(provider or ""))
    if url is None:
        return None
    try:
        parts = urlsplit(url if "//" in url else f"//{url}")
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port}" if port else host


def agent_endpoint(agent: Any) -> str | None:
    """The configured ``base_url`` of an agent's model (or ``None``)."""
    spec = getattr(agent, "model_spec", None)
    options = getattr(spec, "options", None)
    if isinstance(options, Mapping):
        value = options.get("base_url") or options.get("host")
        if isinstance(value, str):
            return value
    model = getattr(agent, "model", None)
    value = getattr(model, "base_url", None)
    return value if isinstance(value, str) else None


def _ends_with_assistant(exc: BaseException) -> bool:
    for item in _chain(exc):
        response = getattr(item, "response", None)
        if not isinstance(response, Mapping) or not isinstance(response.get("Error"), Mapping):
            continue
        error = response["Error"]
        if str(error.get("Code", "")) != "ValidationException":
            continue
        message = str(error.get("Message", "")).lower()
        if any(marker in message for marker in _ENDS_WITH_ASSISTANT_MARKERS):
            return True
    return False


def _refined(reason: str, exc: BaseException) -> str:
    if reason == "bad_request" and _ends_with_assistant(exc):
        return "ends_with_assistant"
    for item in _chain(exc):
        name = type(item).__name__
        if name.endswith(("Timeout", "TimeoutError")):
            return "timeout"
    if reason != "unreachable":
        return reason
    for item in _chain(exc):
        if isinstance(item, socket.gaierror):
            return "dns_failed"
        if isinstance(item, ConnectionRefusedError) or (
            isinstance(item, OSError) and item.errno == errno.ECONNREFUSED
        ):
            return "refused"
        text_free_name = type(item).__name__
        if text_free_name == "gaierror":
            return "dns_failed"
    # httpx/httpcore keep the OS error only in the message: its errno text, not request data.
    for item in _chain(exc):
        message = str(item).lower()
        if "nodename nor servname" in message or "name or service not known" in message:
            return "dns_failed"
        if "connection refused" in message or "errno 61" in message or "errno 111" in message:
            return "refused"
    return reason


def describe_model_failure(exc: BaseException, agent: Any = None) -> dict[str, Any] | None:
    """``{code, reason, http_status, provider, host, error}`` or ``None`` when unknown."""
    # A refusal Stira raised itself before any request (no status, no provider text).
    own = next(
        (
            code
            for item in _chain(exc)
            if (code := getattr(type(item), "stira_failure_reason", None)) in REASONS
        ),
        None,
    )
    reason, _ = (own, None) if own else classify_error(exc)
    # The status the endpoint really sent (classify_error assumes 429 for any throttling).
    status = next((code for item in _chain(exc) if (code := _status_of(item)) is not None), None)
    reason = reason if own else _refined(reason, exc)
    if reason not in REASONS:
        return None
    provider = getattr(agent, "provider", None)
    provider = provider if isinstance(provider, str) and provider else None
    detail: dict[str, Any] = {"code": MODEL_FAILURE_CODE, "reason": reason}
    if status is not None:
        detail["http_status"] = status
    if provider is not None:
        detail["provider"] = provider
    host = endpoint_host(provider, agent_endpoint(agent))
    if host is not None:
        detail["host"] = host
    ja, en = MESSAGES[reason]
    where = " · ".join(x for x in (provider, host) if x)
    detail["error"] = f"{ja} / {en}" + (f" ({where})" if where else "")
    return detail


def model_banner(agent: Any, fallback_model_id: str, fallback_provider: str) -> str:
    """Start-up line: the model and provider the agent really uses (and its endpoint host)."""
    provider = getattr(agent, "provider", None) or fallback_provider
    model_id = getattr(agent, "model_id", None) or fallback_model_id
    host = endpoint_host(provider, agent_endpoint(agent))
    where = f", {host}" if host and provider != "bedrock" else ""
    if provider == "llamacpp":
        # llama.cpp serves the model it was started with; the id is only a label.
        return f"{provider} ({host or 'default endpoint'}; the model loaded by the server)"
    if provider == "bedrock":
        region = getattr(agent, "region_name", None)
        where = f", {region}" if isinstance(region, str) and region else ""
    return f"{model_id} ({provider}{where})"


def status_model(agent: Any, fallback_model_id: str, fallback_provider: str) -> dict[str, Any]:
    """``/api/gateway/status`` view of the model really in use: ``{model, provider, host}``.

    Bedrock keeps the plain model id. llama.cpp serves whatever model it was started
    with, so its id is only a label and ``model`` names the provider and endpoint instead.
    Other providers show ``id (provider · host)``. Language-neutral (no UI text).
    """
    provider = getattr(agent, "provider", None) or fallback_provider
    model_id = getattr(agent, "model_id", None) or fallback_model_id
    host = None if provider == "bedrock" else endpoint_host(provider, agent_endpoint(agent))
    if provider == "bedrock":
        label = model_id
    elif provider == "llamacpp":
        label = " · ".join(x for x in (provider, host) if x)
    else:
        label = f"{model_id} ({' · '.join(x for x in (provider, host) if x)})"
    return {"model": label, "provider": provider, "host": host}
