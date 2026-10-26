"""Generic OAuth Authorization Code + PKCE connector.

Application-owned configuration is separate from tokens. Tokens are plaintext
pending encryption support: keep the default .local directory private/ignored.
Authorization state/PKCE challenges are short-lived and process-local.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlencode, urlsplit

from .http import request_bytes, validate_https, validate_outbound_https

DEFAULT_TOKEN_DIR = Path(".local/stira/oauth")


def validate_provider(provider: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
        raise ValueError("Invalid OAuth provider name")


@runtime_checkable
class OAuthConnector(Protocol):
    def authorize_url(self) -> str: ...
    def exchange_code(self, code: str, state: str) -> dict: ...
    def refresh(self) -> dict: ...
    def revoke(self) -> None: ...


@dataclass(frozen=True)
class OAuthConfig:
    authorize_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    api_origins: tuple[str, ...]
    client_secret: str = field(default="", repr=False)
    scopes: tuple[str, ...] = ()
    revoke_endpoint: str | None = None
    allowed_methods: tuple[str, ...] = ("GET",)

    def __post_init__(self):
        # The browser opens authorize/redirect; this process fetches token/revoke/API
        # URLs, so those also pass the static destination check (no inward literals).
        validate_https(self.authorize_endpoint)
        validate_outbound_https(self.token_endpoint)
        validate_https(self.redirect_uri)
        if self.revoke_endpoint:
            validate_outbound_https(self.revoke_endpoint)
        if not self.client_id or not self.api_origins:
            raise ValueError("Client ID and API origins are required")
        # Authorization parameters are generated here, not overridden by config.
        if urlsplit(self.authorize_endpoint).query:
            raise ValueError("Authorization endpoint must not contain a query")
        for origin in self.api_origins:
            validate_outbound_https(origin)
            if urlsplit(origin).path not in {"", "/"} or urlsplit(origin).query:
                raise ValueError("API origins must not contain paths or queries")
        if not self.allowed_methods or any(
            method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            for method in self.allowed_methods
        ):
            raise ValueError("Invalid allowed HTTP method")


class GenericOAuthConnector:
    def __init__(self, provider: str, config: OAuthConfig, *, token_dir=DEFAULT_TOKEN_DIR):
        validate_provider(provider)
        self.provider = provider
        self.config = config
        self.token_dir = Path(token_dir)
        self.token_path = self.token_dir / f"{provider}.json"
        self._pending = {}
        self._lock = threading.RLock()

    def authorize_url(self) -> str:
        with self._lock:
            now = time.monotonic()
            self._pending = {k: v for k, v in self._pending.items() if v[1] > now}
            if len(self._pending) >= 32:
                raise ValueError("Too many pending authorizations")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self._pending[state] = (verifier, now + 600)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            return (
                self.config.authorize_endpoint
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": self.config.client_id,
                        "redirect_uri": self.config.redirect_uri,
                        "scope": " ".join(self.config.scopes),
                        "state": state,
                        "code_challenge": challenge.rstrip(b"=").decode(),
                        "code_challenge_method": "S256",
                    }
                )
            )

    def _form(self, **values):
        data = {"client_id": self.config.client_id, **values}
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        return data

    def _save(self, tokens: dict) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.token_dir, prefix=".oauth-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(tokens, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.token_path)  # mkstemp mode 0600 on POSIX
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load_tokens(self) -> dict:
        with self._lock:
            if self.token_path.is_symlink():
                raise ValueError("Token file must not be a symlink")
            if self.token_path.stat().st_size > 65536:
                raise ValueError("Token file exceeds limit")
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
                raise TypeError("Invalid token file")
            return data

    def _token_request(self, data, previous=None):
        _, body = request_bytes("POST", self.config.token_endpoint, data=data)
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"]
            or str(payload.get("token_type", "Bearer")).lower() != "bearer"
        ):
            raise ValueError("Expected a Bearer access token")
        # Persist only token fields, never arbitrary service responses/errors.
        tokens = {"access_token": payload["access_token"], "token_type": "Bearer"}
        refresh_token = payload.get("refresh_token", (previous or {}).get("refresh_token"))
        if refresh_token is not None:
            if not isinstance(refresh_token, str) or not refresh_token:
                raise ValueError("Invalid refresh token")
            tokens["refresh_token"] = refresh_token
        if "expires_in" in payload:
            expiry = float(payload["expires_in"])
            if not math.isfinite(expiry) or expiry < 0:
                raise ValueError("Invalid token expiry")
            tokens["expires_at"] = time.time() + expiry
        self._save(tokens)
        return tokens

    def exchange_code(self, code: str, state: str) -> dict:
        with self._lock:
            match = next((key for key in self._pending if hmac.compare_digest(key, state)), None)
            pending = self._pending.pop(match, None)
            if not pending or pending[1] < time.monotonic() or not code:
                raise ValueError("Invalid or expired OAuth state/code")
            return self._token_request(
                self._form(
                    grant_type="authorization_code",
                    code=code,
                    redirect_uri=self.config.redirect_uri,
                    code_verifier=pending[0],
                )
            )

    def refresh(self) -> dict:
        with self._lock:
            tokens = self.load_tokens()
            if not tokens.get("refresh_token"):
                raise ValueError("No refresh token; authorize again")
            return self._token_request(
                self._form(grant_type="refresh_token", refresh_token=tokens["refresh_token"]),
                previous=tokens,
            )

    def access_token(self) -> str:
        with self._lock:
            tokens = self.load_tokens()
            if tokens.get("expires_at", float("inf")) <= time.time() + 30:
                tokens = self.refresh()
            return tokens["access_token"]

    def revoke(self) -> None:
        with self._lock:
            if not self.config.revoke_endpoint:
                raise ValueError("No revocation endpoint configured")
            tokens = self.load_tokens()
            for key in ("refresh_token", "access_token"):
                if tokens.get(key):
                    request_bytes(
                        "POST",
                        self.config.revoke_endpoint,
                        data=self._form(token=tokens[key], token_type_hint=key),
                    )
            self.token_path.unlink()

    def check_api_url(self, method: str, url: str) -> None:
        origin = validate_outbound_https(url)
        if origin not in {validate_outbound_https(value) for value in self.config.api_origins}:
            raise ValueError("API origin is not allowed for this provider")
        if method not in self.config.allowed_methods:
            raise ValueError("HTTP method is not allowed for this provider")


# Host-owned registration; LLM tools cannot configure endpoints or token paths.
CONNECTORS: dict[str, GenericOAuthConnector] = {}


def register_connector(connector: GenericOAuthConnector) -> None:
    if connector.provider in CONNECTORS:
        raise ValueError("OAuth provider already registered")
    CONNECTORS[connector.provider] = connector
