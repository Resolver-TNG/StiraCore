"""SSRF guard for outbound integration HTTP (#98, shared with #212 / ADR 0012 (7)).

No real network: name resolution (``socket.getaddrinfo``) and the TCP connect
(``socket.socket.connect``) or the urllib3 pool are fakes. The socket-level fakes
sit *below* requests/urllib3, so they observe where a request would really go.
"""

import base64
import io
import ipaddress
import socket
import threading
import time

import pytest
import requests
import urllib3

from stiracore.integrations import http, oauth
from stiracore.tools import integration_tools as tools

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


class FakeNet:
    def __init__(self):
        self.names = {}  # host -> list of answers, or list of lists (one per lookup)
        self.lookups = []
        self.connects = []

    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        try:
            ipaddress.ip_address(host)
            answers = [host]
        except ValueError:
            try:  # like the C resolver: inet_aton reads 2130706433, 0x7f.1, 0177.0.0.1
                answers = [socket.inet_ntoa(socket.inet_aton(host))]
            except OSError:
                answers = None
        if answers is None:
            self.lookups.append(host)
            plan = self.names.get(host)
            if plan is None:
                raise socket.gaierror("unknown host")
            answers = plan.pop(0) if plan and isinstance(plan[0], list) else plan
        result = []
        for answer in answers:
            af = socket.AF_INET6 if ":" in answer else socket.AF_INET
            addr = (answer, port, 0, 0) if af == socket.AF_INET6 else (answer, port)
            result.append((af, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addr))
        return result


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()

    def connect(sock, address):
        fake.connects.append(address[:2])
        raise ConnectionRefusedError("fake network: no connections")

    monkeypatch.setattr(socket, "getaddrinfo", fake.getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(tools, "CONNECTORS", {})
    return fake


@pytest.fixture
def pool(monkeypatch, net):
    """Fake urllib3 pool: records where each hop would connect, returns scripted replies."""
    calls, replies = [], []

    def urlopen(self, method, url, body=None, headers=None, **kwargs):
        calls.append(
            {
                "address": self.host,
                "sni": self.conn_kw.get("server_hostname"),
                "assert_hostname": getattr(self, "assert_hostname", None),
                "host_header": (headers or {}).get("Host"),
                "authorization": (headers or {}).get("Authorization"),
                "cookie": (headers or {}).get("Cookie"),
                "path": url,
            }
        )
        status, extra, data = replies.pop(0) if replies else (200, {}, b"ok")
        return urllib3.HTTPResponse(
            body=data if hasattr(data, "read") else io.BytesIO(data),
            headers=extra,
            status=status,
            preload_content=False,
            decode_content=False,
        )

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    return calls, replies


ATTACK_URLS = [
    "https://127.0.0.1/",
    "https://127.0.0.2:8443/",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6
    "https://[::ffff:7f00:1]/",
    "https://[64:ff9b::7f00:1]/",  # NAT64-embedded loopback
    "https://[2002:7f00:1::]/",  # 6to4-embedded loopback
    "https://2130706433/",  # decimal 127.0.0.1
    "https://0x7f000001/",  # hexadecimal
    "https://0x7f.0.0.1/",
    "https://0177.0.0.1/",  # octal
    "https://127.1/",  # shorthand
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata
    "https://[fd00:ec2::254]/",  # IPv6 metadata
    "https://[fe80::1%25en0]/",  # zone identifier
    "https://10.0.0.5/",
    "https://192.168.0.10/",
    "https://172.16.0.1/",
    "https://100.100.100.200/",  # shared address space (metadata on some clouds)
    "https://0.0.0.0/",
    "https://[::]/",
    "https://224.0.0.1/",
    "https://user@public.example/",  # user info
    "https://user:pw@public.example/",
    "https://public.example@127.0.0.1/",  # user info hiding the real host
    "https://public.example\\@127.0.0.1/",
    "http://public.example/",  # scheme not on the allowlist
    "file:///etc/passwd",
]


@pytest.mark.parametrize("url", ATTACK_URLS)
def test_attack_urls_never_connect(net, url):
    net.names["public.example"] = [PUBLIC]
    assert tools.webhook_send(url, {"event": "x"})["status"] == "error"
    with pytest.raises((ValueError, requests.RequestException)):
        http.request_bytes("GET", url)
    assert net.connects == []


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["169.254.169.254"],
        ["fd00:ec2::254"],
        ["10.1.2.3"],
        ["fe80::1"],
        [PUBLIC, "127.0.0.1"],  # mixed answer: refused as a whole
        [],
    ],
)
def test_allowlisted_name_resolving_inward_is_refused(net, answers):
    net.names["hooks.example"] = answers
    assert tools.webhook_send("https://hooks.example/event", {})["status"] == "error"
    assert net.connects == []


def test_dns_rebinding_one_lookup_and_connect_to_checked_address(net):
    # First answer public, every later answer loopback: a check-then-connect design
    # that resolves twice would connect to 127.0.0.1.
    net.names["rebind.example"] = [[PUBLIC], ["127.0.0.1"], ["127.0.0.1"], ["127.0.0.1"]]
    with pytest.raises(requests.ConnectionError):
        http.request_bytes("GET", "https://rebind.example/x")
    assert net.lookups == ["rebind.example"]
    assert net.connects == [(PUBLIC, 443)]


def test_pinned_connection_keeps_tls_name(pool):
    calls, _ = pool
    response = http.fetch("GET", "https://Hooks.Example:8443/a?b=1", resolver=lambda h, p: [PUBLIC])
    assert response.status == 200 and response.body == b"ok" and response.address == PUBLIC
    (call,) = calls
    assert call["address"] == PUBLIC
    assert call["sni"] == "hooks.example" and call["assert_hostname"] == "hooks.example"
    assert call["host_header"] == "hooks.example:8443"
    assert call["path"] == "/a?b=1"


def test_redirects_not_followed_by_default(pool):
    calls, replies = pool
    replies.append((302, {"Location": "https://other.example/"}, b""))
    with pytest.raises(ValueError, match="Redirects are not followed"):
        http.fetch("GET", "https://hooks.example/", resolver=lambda h, p: [PUBLIC])
    assert len(calls) == 1


@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/admin",
        "https://[::ffff:127.0.0.1]/",
        "https://2130706433/",
        "https://inward.example/",  # resolves to loopback
        "https://169.254.169.254/latest/meta-data/",
        "http://hooks.example/",  # scheme downgrade
        "https://user@hooks.example/",
    ],
)
def test_each_redirect_hop_is_checked(pool, location):
    calls, replies = pool
    replies.append((302, {"Location": location}, b""))
    names = {"hooks.example": [PUBLIC], "inward.example": ["127.0.0.1"]}
    policy = http.OutboundPolicy(max_redirects=3)
    with pytest.raises(http.DestinationRefused):
        http.fetch("GET", "https://hooks.example/", policy=policy, resolver=lambda h, p: names[h])
    assert [call["address"] for call in calls] == [PUBLIC]


def test_redirect_hop_limit_and_credentials_do_not_cross_origin(pool):
    calls, replies = pool
    names = {"a.example": [PUBLIC], "b.example": [PUBLIC_2]}
    policy = http.OutboundPolicy(max_redirects=2)
    replies.extend(
        [
            (302, {"Location": "/same-origin"}, b""),
            (301, {"Location": "https://b.example/other"}, b""),
            (200, {}, b"done"),
        ]
    )
    response = http.fetch(
        "GET",
        "https://a.example/",
        policy=policy,
        resolver=lambda h, p: names[h],
        headers={"Authorization": "Bearer t"},
    )
    assert response.body == b"done" and response.url == "https://b.example/other"
    assert [c["address"] for c in calls] == [PUBLIC, PUBLIC, PUBLIC_2]
    assert [c["authorization"] for c in calls] == ["Bearer t", "Bearer t", None]
    assert calls[2]["sni"] == "b.example"
    calls.clear()
    replies.extend([(302, {"Location": "/1"}, b""), (302, {"Location": "/2"}, b"")] * 2)
    with pytest.raises(http.DestinationRefused):
        http.fetch("GET", "https://a.example/", policy=policy, resolver=lambda h, p: names[h])
    assert len(calls) == 3  # initial request + two followed hops, then refused


def test_redirect_with_body_is_not_followed(pool):
    calls, replies = pool
    replies.append((307, {"Location": "/again"}, b""))
    policy = http.OutboundPolicy(max_redirects=3)
    with pytest.raises(http.DestinationRefused):
        http.fetch(
            "POST", "https://a.example/", policy=policy, resolver=lambda h, p: [PUBLIC], data=b"x"
        )
    assert len(calls) == 1


def test_size_and_time_limits(pool, monkeypatch):
    _, replies = pool
    replies.append((200, {}, b"x" * 10))
    with pytest.raises(ValueError, match="exceeds"):
        http.fetch(
            "GET",
            "https://a.example/",
            policy=http.OutboundPolicy(max_bytes=5),
            resolver=lambda h, p: [PUBLIC],
        )
    clock = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(http.time, "monotonic", lambda: next(clock))
    with pytest.raises(http.DestinationRefused, match="time limit"):
        http.fetch("GET", "https://a.example/", resolver=lambda h, p: [PUBLIC])


def test_policy_defaults_are_strictest_and_cannot_add_schemes():
    policy = http.OutboundPolicy()
    assert policy.schemes == ("https",) and policy.max_redirects == 0
    assert policy.timeout == (5, 20) and policy.deadline_seconds == 30.0
    for bad in [{"schemes": ("ftp",)}, {"schemes": ()}, {"max_redirects": 6}]:
        with pytest.raises(ValueError):
            http.OutboundPolicy(**bad)
    assert not hasattr(policy, "allow_private")


@pytest.mark.parametrize(
    "address, forbidden",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("::ffff:10.0.0.1", True),
        ("169.254.169.254", True),
        ("fd00:ec2::254", True),
        ("fe80::1", True),
        ("fec0::1", True),
        ("100.64.0.1", True),
        ("198.18.0.1", True),
        ("240.0.0.1", True),
        ("255.255.255.255", True),
        ("ff02::1", True),
        ("not-an-ip", True),
        (PUBLIC, False),
        ("2606:2800:220:1:248:1893:25c8:1946", False),
        ("::ffff:93.184.216.34", False),
    ],
)
def test_single_destination_decision(address, forbidden):
    assert http.is_forbidden_address(address) is forbidden


def _connector(tmp_path, **endpoints):
    config = oauth.OAuthConfig(
        endpoints.get("authorize", "https://auth.example/authorize"),
        endpoints.get("token", "https://auth.example/token"),
        "client",
        "https://app.example/callback",
        (endpoints.get("api", "https://api.example"),),
        revoke_endpoint=endpoints.get("revoke", "https://auth.example/revoke"),
    )
    return oauth.GenericOAuthConnector("generic", config, token_dir=tmp_path)


@pytest.mark.parametrize("field", ["token", "revoke", "api"])
@pytest.mark.parametrize(
    "url", ["https://127.0.0.1", "https://[::ffff:127.0.0.1]", "https://2130706433"]
)
def test_oauth_config_refuses_inward_literals(tmp_path, field, url):
    with pytest.raises(ValueError):
        _connector(tmp_path, **{field: url + ("/x" if field != "api" else "")})


def test_loopback_refusal_is_one_switch_for_every_outbound_caller(net, tmp_path, monkeypatch):
    """Every integration path reaches the network only through is_forbidden_address.

    ADR 0012 (7) needs "the agent cannot reach its own dashboard over loopback" to be
    enforceable in one place: the names below resolve to loopback, and each caller
    refuses without connecting. Then the single decision is forced to "forbidden"
    and even a public answer is refused on every path.
    """
    for name in ("auth.example", "api.example", "hooks.example"):
        net.names[name] = ["127.0.0.1"]
    connector = _connector(tmp_path)
    connector._save({"access_token": "a", "refresh_token": "r", "expires_at": 4e9})
    tools.CONNECTORS["generic"] = connector

    def refused(call):
        try:
            call()
        except ValueError:
            return True
        return False

    def every_caller_refuses():
        return [
            tools.webhook_send("https://hooks.example/", {})["status"] == "error",
            tools.oauth_api_call("generic", "GET", "https://api.example/x")["status"] == "error",
            refused(connector.refresh),
            refused(connector.revoke),
        ]

    assert all(every_caller_refuses())
    assert net.connects == []
    for name in ("auth.example", "api.example", "hooks.example"):
        net.names[name] = [PUBLIC]
    decisions = []

    def forbid(address):
        decisions.append(str(address))
        return True

    monkeypatch.setattr(http, "is_forbidden_address", forbid)
    assert all(every_caller_refuses())
    assert net.connects == [] and decisions and set(decisions) == {PUBLIC}


def test_auth_and_cookies_are_not_regenerated_on_another_origin(pool):
    """auth=/cookies= are re-applied by requests on every send: they must stay same-origin."""
    calls, replies = pool
    names = {"a.example": [PUBLIC], "b.example": [PUBLIC_2]}
    replies.extend(
        [
            (302, {"Location": "/same"}, b""),
            (302, {"Location": "https://b.example/x"}, b""),
            (302, {"Location": "https://a.example/back"}, b""),
            (200, {}, b"done"),
        ]
    )
    basic = "Basic " + base64.b64encode(b"client:secret").decode()
    response = http.fetch(
        "GET",
        "https://a.example/",
        policy=http.OutboundPolicy(max_redirects=3),
        resolver=lambda h, p: names[h],
        auth=("client", "secret"),
        cookies={"session": "s3cret"},
    )
    assert response.body == b"done"
    assert [c["address"] for c in calls] == [PUBLIC, PUBLIC, PUBLIC_2, PUBLIC]
    assert [c["authorization"] for c in calls] == [basic, basic, None, None]
    assert [c["cookie"] for c in calls] == ["session=s3cret", "session=s3cret", None, None]


@pytest.mark.parametrize("location", ["http://a.example/", "http://a.example:443/"])
def test_https_to_http_downgrade_is_refused_even_when_http_is_allowed(pool, location):
    calls, replies = pool
    replies.append((302, {"Location": location}, b""))
    policy = http.OutboundPolicy(schemes=("http", "https"), max_redirects=3)
    with pytest.raises(http.DestinationRefused):
        http.fetch("GET", "https://a.example/", policy=policy, resolver=lambda h, p: [PUBLIC])
    assert len(calls) == 1


def test_redirect_with_files_is_not_followed(pool):
    calls, replies = pool
    replies.append((302, {"Location": "/again"}, b""))
    with pytest.raises(http.DestinationRefused):
        http.fetch(
            "GET",
            "https://a.example/",
            policy=http.OutboundPolicy(max_redirects=3),
            resolver=lambda h, p: [PUBLIC],
            files={"f": ("f.txt", b"x")},
        )
    assert len(calls) == 1


class CountingBody(io.RawIOBase):
    """A huge response body that records how much of it was read."""

    def __init__(self, size):
        self.left, self.read_bytes = size, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = min(len(buffer), self.left)
        buffer[:count] = b"x" * count
        self.left -= count
        self.read_bytes += count
        return count


@pytest.mark.parametrize("max_redirects", [0, 2])
def test_redirect_body_is_not_read(pool, max_redirects):
    calls, replies = pool
    body = CountingBody(64 * 1024 * 1024)
    replies.append((302, {"Location": "https://127.0.0.1/"}, body))
    policy = http.OutboundPolicy(max_redirects=max_redirects)
    with pytest.raises(http.DestinationRefused):
        http.fetch("GET", "https://a.example/", policy=policy, resolver=lambda h, p: [PUBLIC])
    assert body.read_bytes == 0 and len(calls) == 1


@pytest.mark.parametrize(
    "url, host_header",
    [
        ("https://[2606:2800:220:1:248:1893:25c8:1946]/", "[2606:2800:220:1:248:1893:25c8:1946]"),
        (
            "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/",
            "[2606:2800:220:1:248:1893:25c8:1946]:8443",
        ),
        ("https://93.184.216.34:443/", "93.184.216.34"),
    ],
)
def test_host_header_brackets_ipv6_literals(pool, url, host_header):
    calls, _ = pool
    http.fetch("GET", url)
    assert calls[0]["host_header"] == host_header


def test_deadline_covers_name_resolution():
    def slow(host, port):
        time.sleep(3)
        return [PUBLIC]

    started = time.monotonic()
    with pytest.raises(http.DestinationRefused, match="time limit"):
        http.fetch(
            "GET",
            "https://a.example/",
            policy=http.OutboundPolicy(deadline_seconds=0.3),
            resolver=slow,
        )
    assert time.monotonic() - started < 1.5


def test_deadline_checked_before_returning_an_empty_body(pool, monkeypatch):
    _, replies = pool
    replies.append((204, {}, b""))
    real = time.monotonic
    ticks = {"n": 0}

    def clock():  # time jumps past the deadline while the (empty) response arrives
        ticks["n"] += 1
        return real() + (100.0 if ticks["n"] > 3 else 0.0)

    monkeypatch.setattr(http.time, "monotonic", clock)
    with pytest.raises(http.DestinationRefused, match="time limit"):
        http.fetch("GET", "https://a.example/", resolver=lambda h, p: [PUBLIC])


@pytest.fixture
def drip_server(monkeypatch):
    """A real loopback socket that drips bytes; the guard is told to allow it (test only)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    stop = threading.Event()
    mode = {"value": "body"}

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.recv(65536)
                    if mode["value"] == "body":
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                    else:
                        conn.sendall(b"HTTP/1.1 200 OK\r\n")
                    while not stop.is_set():
                        conn.sendall(b"x" if mode["value"] == "body" else b"X-Drip: 1\r\n")
                        time.sleep(0.05)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(http, "is_forbidden_address", lambda address: False)
    yield listener.getsockname()[1], mode
    stop.set()
    thread.join(5)
    listener.close()


@pytest.mark.parametrize("mode", ["body", "headers"])
def test_slow_drip_is_cut_at_the_deadline(drip_server, mode):
    port, server_mode = drip_server
    server_mode["value"] = mode
    policy = http.OutboundPolicy(schemes=("http",), deadline_seconds=0.8)
    started = time.monotonic()
    with pytest.raises((http.DestinationRefused, requests.RequestException)):
        http.fetch("GET", f"http://127.0.0.1:{port}/", policy=policy)
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, elapsed


TLS_NAME = "drip.test.example"


def _write_throwaway_cert(cert_path, key_path):
    """Self-signed certificate for TLS_NAME (``cryptography`` comes with the mcp dependency)."""
    pytest.importorskip("cryptography")
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_NAME)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(TLS_NAME)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@pytest.fixture
def tls_drip_server(monkeypatch, tmp_path):
    """A real loopback TLS server with a throwaway certificate that drips after the handshake.

    Modes: ``body`` (headers, then one body byte at a time), ``headers`` (status line,
    then one header line at a time), ``continue`` (``100 Continue`` forever, never a
    final response). The SSRF guard is opened for this test only.
    """
    import ssl

    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    _write_throwaway_cert(cert_path, key_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    stop = threading.Event()
    mode = {"value": "body"}
    served = []

    def handle(raw):
        try:
            with context.wrap_socket(raw, server_side=True) as conn:
                conn.recv(65536)
                served.append(mode["value"])
                if mode["value"] == "body":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                    drip = b"x"
                elif mode["value"] == "headers":
                    conn.sendall(b"HTTP/1.1 200 OK\r\n")
                    drip = b"X-Drip: 1\r\n"
                else:
                    drip = b"HTTP/1.1 100 Continue\r\n\r\n"
                while not stop.is_set():
                    conn.sendall(drip)
                    time.sleep(0.05)
        except OSError:
            pass

    def serve():
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except OSError:
                continue
            threading.Thread(target=handle, args=(raw,), daemon=True).start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(http, "is_forbidden_address", lambda address: False)
    yield listener.getsockname()[1], mode, str(cert_path), served
    stop.set()
    thread.join(5)
    listener.close()


@pytest.mark.parametrize("mode", ["body", "headers", "continue"])
def test_tls_slow_drip_is_cut_at_the_deadline(tls_drip_server, mode):
    """The deadline must stop the TLS socket that is really being read, not the
    plain socket that TLS wrapping detached."""
    port, server_mode, ca, served = tls_drip_server
    server_mode["value"] = mode
    policy = http.OutboundPolicy(deadline_seconds=0.8)
    result = {}

    def call():
        started = time.monotonic()
        try:
            http.fetch(
                "GET",
                f"https://{TLS_NAME}:{port}/",
                policy=policy,
                resolver=lambda h, p: ["127.0.0.1"],
                verify=ca,
            )
            result["outcome"] = "returned"
        except (http.DestinationRefused, requests.RequestException) as error:
            result["outcome"] = type(error).__name__
        result["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(6)  # a hanging fetch must fail the test, not hang the suite
    assert not worker.is_alive(), "fetch kept reading past the deadline"
    assert served == [mode]  # the TLS handshake completed and the drip started
    assert result["outcome"] == "DestinationRefused" and result["elapsed"] < 2.5, result
