"""Local web hardening: Host/Origin/CSRF checks, security headers, idempotency, masking."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlsplit

from flask import current_app, g, jsonify, request

# Re-exported: masking lives in a Flask-free module so exporters can use it too.
from .masking import MASK, looks_high_entropy, mask_secrets  # noqa: F401

logger = logging.getLogger(__name__)
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")

DEFAULT_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
IDEMPOTENCY_MAX = 512
IDEMPOTENCY_TTL = 300.0
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Worded so as not to promise more than is known: received, maybe still running (#185).
DUPLICATE_ERROR = (
    "同じ送信をすでに受け付けました（処理中か完了かは履歴で確認してください） / "
    "The same request was already received (it may still be running or be finished; "
    "check the history)"
)
KEY_REUSED_ERROR = (
    "この Idempotency-Key は別の内容の送信ですでに使われています / "
    "This Idempotency-Key was already used for a different request"
)


@dataclass
class SecuritySettings:
    token: str
    allowed_hosts: frozenset
    extra_allowed_origins: frozenset
    csrf_exempt_endpoints: frozenset
    idempotent_endpoints: tuple
    seen: OrderedDict = field(default_factory=OrderedDict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return _strip_dot(host.split(":")[0])


def _strip_dot(name: str) -> str:
    """``localhost.`` and ``localhost`` name the same host (one trailing root dot)."""
    return name[:-1] if name.endswith(".") and not name.endswith("..") else name


def _host_tuple(netloc: str, scheme: str) -> tuple[str, str, int] | None:
    """(scheme, normalized hostname, effective port); None if malformed or with userinfo."""
    if not netloc or "@" in netloc or _UNPRINTABLE.search(netloc) or " " in netloc:
        return None
    try:
        parts = urlsplit("//" + netloc)
        port = parts.port
    except ValueError:  # bad port, "Invalid IPv6 URL", ...
        return None
    if parts.netloc != netloc or parts.path or not parts.hostname:
        return None
    host = parts.hostname  # lowercased; IPv6 without brackets
    host = f"[{host}]" if ":" in host else _strip_dot(host)
    return scheme, host, _DEFAULT_PORTS[scheme] if port is None else port


def parse_origin(value) -> tuple[str, str, int] | None:
    """A serialized browser Origin as (scheme, hostname, port), or None when malformed.

    Only ``http``/``https`` with a host and nothing else: no userinfo, path, query or
    fragment; ``null``, scheme-relative (``//host``) and opaque values are malformed.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    scheme, sep, rest = value.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS or any(c in rest for c in "/?#"):
        return None
    return _host_tuple(rest, scheme)


def _configured_origins(values) -> frozenset:
    out = set()
    for value in values:
        value = str(value).strip().rstrip("/")
        if not value:
            continue
        parsed = parse_origin(value)
        if parsed is None:
            logger.warning("security: ignoring malformed allowed origin %r", _clean(value))
        else:
            out.add(parsed)
    return frozenset(out)


def _matches(patterns) -> str | None:
    endpoint = request.endpoint
    rule = request.url_rule.rule if request.url_rule is not None else None
    for pattern in patterns:
        if pattern == endpoint or (rule and pattern.startswith("/") and fnmatch(rule, pattern)):
            return endpoint or rule
    return None


def _clean(value, limit: int = 120) -> str:
    """Printable ASCII only (no log injection), bounded."""
    return _UNPRINTABLE.sub("?", str(value or ""))[:limit]


def _audit_denial(reason: str) -> None:
    """Audit a refused request without secrets (#148).

    Records method, path (no query string), endpoint, reason, the Origin's
    scheme+host, Sec-Fetch-Site, Host and client address. Never the CSRF token,
    cookies, headers, query or body.
    """
    origin = request.headers.get("Origin")
    if origin:
        try:
            parts = urlsplit(origin)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "invalid"
        except ValueError:
            origin = "invalid"
    logger.warning(
        "security: denied %s %s endpoint=%s reason=%r origin=%s sec_fetch_site=%s host=%s remote=%s",
        _clean(request.method, 10),
        _clean(request.path),
        _clean(request.endpoint),
        reason,
        _clean(origin),
        _clean(request.headers.get("Sec-Fetch-Site"), 20),
        _clean(request.host),
        _clean(request.remote_addr, 64),
    )


def install_security(
    app,
    *,
    allowed_hosts=None,
    extra_allowed_origins=(),
    csrf_exempt_endpoints=(),
    idempotent_endpoints=(),
):
    hosts = set(DEFAULT_HOSTS if allowed_hosts is None else allowed_hosts)
    hosts |= {
        _hostname(h) for h in os.environ.get("STIRA_ALLOWED_HOSTS", "").split(",") if h.strip()
    }
    # Full origins (scheme://host[:port]) trusted besides the request's own, e.g. the
    # public HTTPS origin of a reverse proxy in front (STIRA_ALLOWED_ORIGINS).
    origins = [*extra_allowed_origins, *os.environ.get("STIRA_ALLOWED_ORIGINS", "").split(",")]
    settings = SecuritySettings(
        token=secrets.token_urlsafe(32),
        allowed_hosts=frozenset(_hostname(h) for h in hosts),
        extra_allowed_origins=_configured_origins(origins),
        csrf_exempt_endpoints=frozenset(csrf_exempt_endpoints),
        idempotent_endpoints=tuple(idempotent_endpoints),
    )
    app.config["STIRA_CSRF_TOKEN"] = settings.token
    app.extensions["stira_security"] = settings

    def _deny(message):
        _audit_denial(message)
        return jsonify(error=message), 403

    def _cross_origin() -> bool:
        """True for a browser-flagged cross-site request or a foreign/malformed Origin.

        An Origin matches only by scheme, normalized hostname (case, one trailing dot)
        and effective port against this request (its scheme and Host), or one of the
        configured origins. No Origin at all (curl, same-origin GET) passes, as does
        ``Sec-Fetch-Site: same-origin`` / ``none``.
        """
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return True
        origin = request.headers.get("Origin")
        if origin is None:
            return False
        parsed = parse_origin(origin)
        if parsed is None:  # malformed, userinfo, "null", "//host": refuse, never 500
            return True
        if parsed in settings.extra_allowed_origins:
            return False
        return parsed != _host_tuple(request.host, request.scheme)

    @app.before_request
    def _stira_security_check():
        if _hostname(request.host) not in settings.allowed_hosts:
            return _deny("Host not allowed.")
        if request.method not in UNSAFE_METHODS:
            # API reads and SSE streams are same-origin only (#185). Same-origin
            # fetch/EventSource sends no Origin (or the own one); a cross-origin
            # EventSource/fetch sends a foreign Origin. Pages stay reachable by
            # links from elsewhere.
            if request.path.startswith("/api/") and _cross_origin():
                return _deny("Cross-origin API reads are not allowed.")
            return None
        if _cross_origin():
            return _deny("Cross-origin mutations are not allowed.")
        if request.endpoint not in settings.csrf_exempt_endpoints:
            supplied = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied.encode(), settings.token.encode()):
                return _deny("Missing or invalid CSRF token.")
        return None

    def _idempotency_check():
        """Runs after every before_request hook (blueprint body limits apply first).

        A key is scoped to this process (its CSRF token is the only session there is),
        the method and the concrete path (e.g. one approval id), and bound to a digest
        of the body: the same key with the same body is a replay, with another body a
        client error. Both are 409 with distinct codes; neither reaches the view.
        """
        key = request.headers.get("Idempotency-Key")
        if not key or request.method not in UNSAFE_METHODS:
            return None
        if not _matches(settings.idempotent_endpoints):
            return None
        record = (request.method, request.path, key)
        digest = hashlib.sha256(request.get_data(cache=True)).hexdigest()
        now = time.monotonic()
        with settings.lock:
            while settings.seen and next(iter(settings.seen.values()))[0] < now - IDEMPOTENCY_TTL:
                settings.seen.popitem(last=False)
            seen = settings.seen.get(record)
            if seen is not None:
                if seen[1] != digest:
                    return jsonify(error=KEY_REUSED_ERROR, code="idempotency_key_reused"), 409
                return jsonify(error=DUPLICATE_ERROR, code="duplicate_request"), 409
            settings.seen[record] = (now, digest)
            while len(settings.seen) > IDEMPOTENCY_MAX:
                settings.seen.popitem(last=False)
        g.stira_idempotency = record
        return None

    dispatch = app.dispatch_request

    def _dispatch_with_idempotency():
        refused = _idempotency_check()
        return refused if refused is not None else dispatch()

    app.dispatch_request = _dispatch_with_idempotency

    def _discard():
        record = g.pop("stira_idempotency", None)
        if record is not None:
            with settings.lock:
                settings.seen.pop(record, None)

    @app.after_request
    def _stira_security_headers(response):
        if not 200 <= response.status_code < 300:
            _discard()
        else:
            g.pop("stira_idempotency", None)
        # One view may set a stricter document policy of its own (the sandboxed artifact
        # preview, #454: ``sandbox allow-scripts`` and ``frame-ancestors 'self'``).
        own_csp = g.pop("stira_csp", None)
        response.headers["Content-Security-Policy"] = own_csp or CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if own_csp else "DENY"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.teardown_request
    def _stira_idempotency_teardown(exc):
        if exc is not None:
            _discard()

    @app.context_processor
    def _stira_csrf_context():
        return {"csrf_token": lambda: settings.token}

    def csrf_token_view():
        return jsonify(token=settings.token)

    app.add_url_rule("/api/csrf", "stira_csrf", csrf_token_view, methods=["GET"])
    return settings


def idempotency_release():
    """In a view: a callable that forgets this request's Idempotency-Key.

    For a response that is 2xx yet guarantees nothing ran (e.g. a chat stream that
    only says "busy"), so the same request may be sent again with the same key.
    """
    settings = current_app.extensions.get("stira_security")
    record = g.get("stira_idempotency")

    def release() -> None:
        if settings is not None and record is not None:
            with settings.lock:
                settings.seen.pop(record, None)

    return release


def _is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_host_setting(default: str = "127.0.0.1") -> str:
    """The listen address as configured, without the refusal of :func:`resolve_bind_host`.

    The one reading of the address: the server's start (through :func:`resolve_bind_host`)
    and the YOLO settings page's wildcard check (PR #444 M2) both call it, so a save the
    page accepts is one the next start accepts.
    """
    return os.environ.get("STIRA_HOST", "").strip() or default


def resolve_bind_host(default: str = "127.0.0.1") -> str:
    """Bind address; a non-loopback one only with ``STIRA_ALLOW_REMOTE=1`` (#185).

    The servers have no authentication and no TLS of their own, so exposing them is
    the operator's explicit statement that both are provided in front (a reverse
    proxy). Even then only ``Host`` names in the allow-list are served
    (``STIRA_ALLOWED_HOSTS``). Callers resolve this before starting anything.
    """
    host = bind_host_setting(default)
    if _is_loopback(host):
        return host
    if os.environ.get("STIRA_ALLOW_REMOTE") != "1":
        raise SystemExit(
            f"Refusing to bind to non-loopback host {_clean(host, 64)!r}: this server has "
            "no authentication or TLS. Put authentication/TLS in front (e.g. a reverse "
            "proxy) before exposing it, then set STIRA_ALLOW_REMOTE=1."
        )
    logger.warning(
        "security: binding to non-loopback host %s (STIRA_ALLOW_REMOTE=1). There is no "
        "built-in authentication or TLS; they must be provided in front.%s",
        _clean(host, 64),
        ""
        if os.environ.get("STIRA_ALLOWED_HOSTS", "").strip()
        else " STIRA_ALLOWED_HOSTS is empty: only loopback Host names are served.",
    )
    return host
