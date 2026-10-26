"""No ambient credentials, redirects or unbounded external responses.

Outbound HTTP for integrations (and later web/ACP fetches, #98/#212) goes through
``fetch``: one URL check, one destination decision (``is_forbidden_address``) and a
connection pinned to the address that was checked, so DNS is never consulted twice
for one hop (no rebinding window between check and connect).
"""

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
import urllib3.connection
from requests.adapters import HTTPAdapter

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# WHATWG URL parsing reads a host whose last label is numeric as an IPv4 address
# (``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``). Only the canonical
# dotted-quad form is accepted; anything else is refused before any DNS lookup.
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)", re.IGNORECASE)
_HOST_LABELS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")


class DestinationRefused(ValueError):
    """The URL or a resolved address is not an allowed outbound destination."""


@dataclass(frozen=True)
class OutboundPolicy:
    """Code-level outbound limits. The defaults are the strictest settings.

    There is no switch that admits loopback/private/link-local destinations.
    """

    schemes: tuple[str, ...] = ("https",)
    max_redirects: int = 0  # 0 = report 3xx as an error (never follow)
    max_bytes: int | None = None  # None = MAX_RESPONSE_BYTES at call time
    timeout: tuple[float, float] = (5, 20)  # connect, per read
    deadline_seconds: float = 30.0  # whole call, all hops and the body

    def __post_init__(self):
        if not self.schemes or not set(self.schemes) <= {"http", "https"}:
            raise ValueError("Only http/https schemes can be allowed")
        if not 0 <= self.max_redirects <= 5:
            raise ValueError("max_redirects must be between 0 and 5")


DEFAULT_POLICY = OutboundPolicy()


@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    address: str  # the checked address the final hop connected to


def _embedded_ipv4(address):
    if address.version != 6:
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64 or (address in _V4_COMPATIBLE and int(address) > 1):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_forbidden_address(address) -> bool:
    """The single destination decision: True unless the address is plainly public.

    Loopback, link-local (incl. cloud metadata 169.254.169.254 / fd00:ec2::254),
    private, shared (100.64/10), multicast, reserved and unspecified addresses are
    refused, also when embedded in IPv6 (IPv4-mapped, 6to4, Teredo, NAT64). An
    unparsable value is refused.
    """
    try:
        if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            address = ipaddress.ip_address(str(address))
    except ValueError:
        return True
    if getattr(address, "scope_id", None):
        return True
    embedded = _embedded_ipv4(address)
    if address.version == 6 and address.ipv4_mapped is not None:
        candidates = [embedded]  # ::ffff:a.b.c.d is a.b.c.d on the wire
    else:
        candidates = [address] if embedded is None else [address, embedded]
    for value in candidates:
        if (
            not value.is_global
            or value.is_loopback
            or value.is_link_local
            or value.is_private
            or value.is_multicast
            or value.is_reserved
            or value.is_unspecified
            or (value.version == 6 and value.is_site_local)
        ):
            return True
    return False


def _literal_address(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def check_url(url: str, policy: OutboundPolicy = DEFAULT_POLICY) -> tuple[str, str, int]:
    """Static URL check (no DNS): scheme allowlist, no credentials, unambiguous host."""
    if not isinstance(url, str) or any(ord(char) < 33 or char == "\\" for char in url):
        raise DestinationRefused("URL contains spaces, control characters or backslashes")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise DestinationRefused("Malformed URL") from error
    scheme = parts.scheme.lower()
    if scheme not in policy.schemes:
        raise DestinationRefused("URL scheme is not allowed")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise DestinationRefused("Credentials in URLs are not supported")
    if parts.fragment:
        raise DestinationRefused("Fragments are not supported")
    host = (parts.hostname or "").lower()
    if not host or not host.isascii() or "%" in host:
        raise DestinationRefused("Use an ASCII (xn--) host without zone identifiers")
    literal = _literal_address(host)
    if literal is None:
        labels = host.rstrip(".").split(".")
        if _NUMERIC_LABEL.fullmatch(labels[-1]):
            raise DestinationRefused("Non-canonical numeric host")
        if not all(_HOST_LABELS.fullmatch(label) for label in labels) or len(host) > 253:
            raise DestinationRefused("Invalid host name")
    elif is_forbidden_address(literal):
        raise DestinationRefused("Destination address is not allowed")
    return scheme, host, port or (443 if scheme == "https" else 80)


def validate_https(url: str) -> tuple[str, str, int]:
    """Static HTTPS check for URLs this process does not fetch (e.g. redirect_uri)."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(ord(char) < 33 for char in url)
    ):
        raise ValueError("Use an absolute HTTPS URL without credentials or fragments")
    return parts.scheme, parts.hostname.lower(), parts.port or 443


def validate_outbound_https(url: str) -> tuple[str, str, int]:
    """validate_https plus the static destination check, for URLs that are fetched."""
    validate_https(url)
    return check_url(url)


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _resolve_within(resolver, host: str, port: int, deadline: float) -> list[str]:
    """Run the (blocking) resolver under the call deadline; a late answer is discarded."""
    result: dict = {}

    def run():
        try:
            result["answers"] = list(resolver(host, port))
        except BaseException as error:  # noqa: BLE001 - re-raised in the caller
            result["error"] = error

    worker = threading.Thread(target=run, name="stira-outbound-dns", daemon=True)
    worker.start()
    worker.join(_remaining(deadline))
    if worker.is_alive():
        raise DestinationRefused("Outbound request exceeded its time limit")
    if "error" in result:
        raise result["error"]
    return result["answers"]


def resolve_checked(host: str, port: int, resolver=None, deadline: float | None = None) -> str:
    """Resolve once and return the address to connect to; every answer must pass."""
    literal = _literal_address(host)
    if literal is not None:
        if is_forbidden_address(literal):
            raise DestinationRefused("Destination address is not allowed")
        return str(literal)
    resolver = resolver or system_resolver
    try:
        if deadline is None:
            answers = list(resolver(host, port))
        else:
            answers = _resolve_within(resolver, host, port, deadline)
    except OSError as error:
        raise DestinationRefused("Host could not be resolved") from error
    if not answers:
        raise DestinationRefused("Host could not be resolved")
    # A mixed answer (public + private) is refused as a whole: either may be used.
    if any(is_forbidden_address(answer) for answer in answers):
        raise DestinationRefused("Destination address is not allowed")
    return str(ipaddress.ip_address(answers[0]))


class _PinnedAdapter(HTTPAdapter):
    """Connect to an already checked address; TLS SNI/certificate stay on the host name.

    Every connection it opens is recorded so the call deadline can stop it while a
    read is blocked (a slow drip resets the per-read timeout on every byte). The
    abort uses the connection's *current* socket: for HTTPS that is the SSLSocket,
    because TLS wrapping detaches the plain socket ``_new_conn`` returned.
    """

    def __init__(self, host: str, port: int, address: str):
        self._host, self._port, self._address = host, port, address
        self._connections: list = []
        self._abort_lock = threading.Lock()
        self.aborted = False
        super().__init__(max_retries=0)
        adapter = self

        def tracked(base):
            class Connection(base):
                def _new_conn(self):
                    sock = super()._new_conn()
                    # self.sock is assigned by connect() right after this returns;
                    # record the plain socket too so a TLS handshake can be cut.
                    adapter._register(self, sock)
                    return sock

                def connect(self):
                    super().connect()
                    adapter._register(self, self.sock)  # the wrapped (TLS) socket

            return Connection

        class HTTPPool(urllib3.HTTPConnectionPool):
            ConnectionCls = tracked(urllib3.connection.HTTPConnection)

        class HTTPSPool(urllib3.HTTPSConnectionPool):
            ConnectionCls = tracked(urllib3.connection.HTTPSConnection)

        self.poolmanager.pool_classes_by_scheme = {"http": HTTPPool, "https": HTTPSPool}

    @staticmethod
    def _stop(sock) -> None:
        if sock is None:
            return
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                action()
            except (OSError, ValueError):
                pass

    def _register(self, connection, sock) -> None:
        with self._abort_lock:
            self._connections.append((connection, sock))
            late = self.aborted
        if late:  # registered after the deadline fired: stop it at once
            self._stop(getattr(connection, "sock", None))
            self._stop(sock)

    def abort(self) -> None:
        with self._abort_lock:
            self.aborted = True
            entries = list(self._connections)
        for connection, sock in entries:
            self._stop(getattr(connection, "sock", None))
            self._stop(sock)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        parts = urlsplit(request.url)
        if (parts.hostname or "").lower() != self._host:
            raise DestinationRefused("Request host changed after the destination check")
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        host_params["host"] = self._address
        if host_params.get("scheme") == "https":
            pool_kwargs["server_hostname"] = self._host
            pool_kwargs["assert_hostname"] = self._host
        return host_params, pool_kwargs

    def send(self, request, **kwargs):
        default = 443 if urlsplit(request.url).scheme == "https" else 80
        name = f"[{self._host}]" if ":" in self._host else self._host
        request.headers["Host"] = name if self._port == default else f"{name}:{self._port}"
        return super().send(request, **kwargs)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise DestinationRefused("Outbound request exceeded its time limit")
    return left


_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


def fetch(
    method: str,
    url: str,
    *,
    policy: OutboundPolicy = DEFAULT_POLICY,
    resolver=None,
    **kwargs,
) -> OutboundResponse:
    """One bounded outbound request; each hop is checked, resolved once and pinned.

    ``policy.deadline_seconds`` is a hard limit for the whole call: name resolution,
    connect, headers and body of every hop (blocked sockets are shut down).
    Credentials (``Authorization``/``Cookie`` headers, ``auth=``, ``cookies=``) are
    sent only to the first origin (scheme, host, port); once a redirect leaves it
    they are dropped for good. A redirect never downgrades https to http.
    """
    deadline = time.monotonic() + policy.deadline_seconds
    limit = MAX_RESPONSE_BYTES if policy.max_bytes is None else policy.max_bytes
    headers = dict(kwargs.pop("headers", None) or {})
    has_body = any(kwargs.get(key) is not None for key in ("data", "json", "files"))
    hops = 0
    origin = check_url(url, policy)
    while True:
        _, host, port = current = check_url(url, policy)
        address = resolve_checked(host, port, resolver, deadline)
        left = _remaining(deadline)
        timeout = (min(policy.timeout[0], left), min(policy.timeout[1], left))
        with requests.Session() as session:
            session.trust_env = False  # no proxies, .netrc or ambient credentials
            # Session.send reads a 3xx body to prepare the next hop, even with
            # allow_redirects=False; with no target it leaves the body unread.
            session.get_redirect_target = lambda _response: None
            adapter = _PinnedAdapter(host, port, address)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            watchdog = threading.Timer(left, adapter.abort)
            watchdog.daemon = True
            watchdog.start()
            try:
                with session.request(
                    method,
                    url,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                    **({"headers": headers} if headers else {}),
                    **kwargs,
                ) as response:
                    if 300 <= response.status_code < 400:
                        # Only the Location header is read; the body is never consumed.
                        location = response.headers.get("Location")
                        if (
                            hops >= policy.max_redirects
                            or not location
                            or method.upper() not in {"GET", "HEAD"}
                            or has_body
                        ):
                            raise DestinationRefused("Redirects are not followed")
                        hops += 1
                        following = urljoin(url, location)
                        scheme = urlsplit(following).scheme.lower()
                        if current[0] == "https" and scheme != "https":
                            raise DestinationRefused("Redirect downgrades https")
                        if check_url(following, policy) != origin:
                            # Left the first origin: drop every credential source.
                            headers = {
                                k: v
                                for k, v in headers.items()
                                if k.lower() not in _CREDENTIAL_HEADERS
                            }
                            kwargs.pop("auth", None)
                            kwargs.pop("cookies", None)
                            origin = None  # never re-attached, also on a way back
                        url = following
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        if len(body) + len(chunk) > limit:
                            raise ValueError("Response exceeds limit")
                        body.extend(chunk)
                        _remaining(deadline)
                    _remaining(deadline)
                    return OutboundResponse(
                        response.status_code, dict(response.headers), bytes(body), url, address
                    )
            except (requests.RequestException, OSError) as error:
                if time.monotonic() >= deadline:
                    raise DestinationRefused("Outbound request exceeded its time limit") from error
                raise
            finally:
                watchdog.cancel()


def request_bytes(method: str, url: str, **kwargs) -> tuple[int, bytes]:
    """HTTPS-only, no redirects, 2 MiB, pinned to a checked public address."""
    validate_https(url)
    response = fetch(method, url, **kwargs)
    return response.status, response.body
