import base64
import hashlib
import json
import os
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from stiracore.integrations import oauth
from stiracore.integrations.oauth import GenericOAuthConnector, OAuthConfig, OAuthConnector


@pytest.fixture
def connector(tmp_path, monkeypatch):
    config = OAuthConfig(
        authorize_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        client_id="client",
        client_secret="client-secret",
        redirect_uri="https://app.example/callback",
        api_origins=("https://api.example",),
        scopes=("items.read",),
        revoke_endpoint="https://auth.example/revoke",
    )
    connector = GenericOAuthConnector("generic", config, token_dir=tmp_path / "oauth")
    http = Mock(
        return_value=(
            200,
            json.dumps(
                {
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                }
            ).encode(),
        )
    )
    monkeypatch.setattr(oauth, "request_bytes", http)
    return connector, http


def authorize(connector):
    query = parse_qs(urlsplit(connector.authorize_url()).query)
    return query, connector.exchange_code("code", query["state"][0])


def test_protocol_pkce_state_and_private_atomic_storage(connector):
    conn, http = connector
    assert isinstance(conn, OAuthConnector)
    query, tokens = authorize(conn)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["items.read"]
    data = http.call_args.kwargs["data"]
    digest = hashlib.sha256(data["code_verifier"].encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert data["grant_type"] == "authorization_code"
    assert conn.load_tokens() == tokens
    if os.name != "nt":
        assert conn.token_path.stat().st_mode & 0o777 == 0o600
    assert list(conn.token_dir.iterdir()) == [conn.token_path]
    assert "client-secret" not in repr(conn.config)
    with pytest.raises(ValueError):
        conn.exchange_code("code", query["state"][0])
    http.assert_called_once()


def test_invalid_and_expired_state_are_offline(connector, monkeypatch):
    conn, http = connector
    with pytest.raises(ValueError):
        conn.exchange_code("code", "incorrect")
    query = parse_qs(urlsplit(conn.authorize_url()).query)
    monkeypatch.setattr(oauth.time, "monotonic", lambda: 10**20)
    with pytest.raises(ValueError):
        conn.exchange_code("code", query["state"][0])
    http.assert_not_called()


def test_refresh_preserves_then_rotates_refresh_token(connector):
    conn, http = connector
    authorize(conn)
    http.return_value = (200, b'{"access_token":"new","expires_in":3600}')
    assert conn.refresh()["refresh_token"] == "refresh-secret"
    assert http.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    http.return_value = (200, b'{"access_token":"newer","refresh_token":"rotated"}')
    assert conn.refresh()["refresh_token"] == "rotated"


def test_expired_token_auto_refresh_and_revoke(connector):
    conn, http = connector
    authorize(conn)
    tokens = conn.load_tokens()
    tokens["expires_at"] = 0
    conn._save(tokens)
    http.return_value = (200, b'{"access_token":"renewed","expires_in":3600}')
    assert conn.access_token() == "renewed"
    http.return_value = (200, b"")
    conn.revoke()
    assert not conn.token_path.exists()
    assert http.call_args.kwargs["data"]["token_type_hint"] == "access_token"


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{}",
        b'{"access_token":"x","token_type":"mac"}',
        b'{"access_token":"x","expires_in":"NaN"}',
    ],
)
def test_bad_token_response_does_not_clobber_previous(connector, payload):
    conn, http = connector
    authorize(conn)
    previous = conn.token_path.read_bytes()
    http.return_value = (200, payload)
    with pytest.raises(ValueError):
        conn.refresh()
    assert conn.token_path.read_bytes() == previous


def test_failed_revocation_preserves_local_tokens(connector):
    conn, http = connector
    authorize(conn)
    http.side_effect = ValueError("failure")
    with pytest.raises(ValueError):
        conn.revoke()
    assert conn.token_path.exists()


@pytest.mark.parametrize("provider", ["../escape", "", "x/y", "..", "Upper"])
def test_invalid_provider(connector, provider):
    conn, _ = connector
    with pytest.raises(ValueError):
        GenericOAuthConnector(provider, conn.config)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example/x",
        "https://evil.example/x",
        "https://api.example.evil/x",
        "https://api.example:444/x",
        "https://user@api.example/x",
    ],
)
def test_api_origin_confinement(connector, url):
    conn, _ = connector
    with pytest.raises(ValueError):
        conn.check_api_url("GET", url)


def test_api_method_confinement(connector):
    conn, _ = connector
    conn.check_api_url("GET", "https://api.example/items")
    with pytest.raises(ValueError):
        conn.check_api_url("POST", "https://api.example/items")
