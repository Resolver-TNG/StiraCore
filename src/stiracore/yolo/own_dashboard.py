"""Refuse connections from this process to its own dashboard (ADR 0012 §8b, #495).

The server arms this guard with its listen address and port at every start — with or
without ``STIRA_YOLO_ALLOW`` (#495; before, only while the allowance was set). From then on
a TCP connection **from inside the server process** to that port on a
loopback address, an unspecified address (``0.0.0.0`` / ``::`` reach the local host), the
listen address itself or an address of this host's name is refused with
:class:`OwnDashboardRefused` (reason code ``E_OWN_DASHBOARD``) — whatever made it:
``web_fetch``, ``web_search``,
``http_request``, ``rss`` (feedparser / urllib), ``webhook_send`` / ``oauth_api_call``
(already refused by the guarded fetch), an in-process MCP HTTP client, a redirect, a DNS
name that resolves to loopback, or a rebinding answer. ``http_request`` additionally checks
its URL (and every redirect hop) before anything is sent (:func:`url_refusal`,
``tools/own_dashboard_gate.py``), so the model gets the reason code instead of a
connection error.

It is checked where the connection is made: one ``sys.addaudithook`` hook on the
``socket.connect`` audit event (also raised by ``connect_ex`` and asyncio), so the
address checked is the address connected to — no redirect and no time between the check
and the connect. HTTP clients resolve names themselves and connect to a literal address;
a host **name** handed to ``connect()`` for the dashboard's port is refused without a
lookup of its own (no second DNS answer to race with). The guard evaluates only built-in
immutable values (exactly ``str`` / ``bytes`` hosts, exactly ``int`` ports, the C-level
tuple items): the socket converts the address before the audit event, so calling a
caller's ``__index__`` / ``__str__`` / ``__eq__`` again could answer differently. Any
other host or port object is treated as possibly local / possibly the dashboard and
refused unless the known parts show another port or another host. Connections to **any other port** — a local
model server, an embeddings server, an MCP server on loopback — are never touched.

This is **not** the general destination guard (private / link-local destinations stay
reachable, ADR 0012 Open (7), decided). It does not hold processes other than the
server's: MCP servers started as their own processes, exec children and any program of
the same OS user can still reach the dashboard; the host confirmation (phase 2) is what
holds against them.

A wildcard listen address (``0.0.0.0`` / ``::``) is refused while the allowance is set
(the server does not start, exit 2). Without the allowance a wildcard listener is armed
with ``any_interface``: an address at the dashboard's port is also refused when this host
can bind it (one ``bind`` to port 0, only for that port), so the other interfaces are
covered too. An audit hook cannot be removed once added; disarming leaves a hook that does
nothing.

Since #499 the guard also covers the **observer** dashboard (``dashboard/app.py``), which
reads the same default config files and always listens on :data:`OBSERVER_PORT`: the server
arms it as a second port (``also_ports``), where any address of this host counts as the
dashboard (one ``bind`` probe, only for that port). This host name's addresses are looked
up on first need, not at start-up (an mDNS ``.local`` name can take seconds), then kept.
Every refusal is logged once per connection attempt (``E_OWN_DASHBOARD``, no address), so
an in-process caller that swallows the error is still visible.
"""

from __future__ import annotations

import _socket
import ipaddress
import logging
import socket
import sys
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

__all__ = [
    "OBSERVER_PORT",
    "REASON_CODE",
    "OwnDashboardRefused",
    "Target",
    "WildcardBindRefused",
    "arm",
    "armed",
    "disarm",
    "is_own_dashboard",
    "is_wildcard",
    "normalize_host",
    "normalize_port",
    "target_for",
    "url_refusal",
]

_LOG = logging.getLogger(__name__)

REASON_CODE = "E_OWN_DASHBOARD"
# The observer dashboard's fixed port (``dashboard/app.py``; same config files, #499).
OBSERVER_PORT = 5555
_REFUSAL = (
    f"{REASON_CODE}: Stira refuses connections from its own process to its own dashboard "
    "(ADR 0012 §8b, #495)"
)


class OwnDashboardRefused(ConnectionRefusedError):
    """A connection from the server process to its own dashboard."""

    def __init__(self) -> None:
        super().__init__(_REFUSAL)


class _HostNameAddresses:
    """This host name's addresses, looked up on first use and then kept (#499).

    Resolving ``gethostname()`` can take seconds (a ``.local`` name asks mDNS on macOS), so
    a start does not wait for it. It is needed only for a connection to a guarded port at
    an address that is not loopback, not unspecified and not the listen address.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._found: frozenset | None = None

    def get(self) -> frozenset:
        found = self._found
        if found is None:
            with self._lock:
                if self._found is None:
                    try:
                        self._found = frozenset(_resolve(socket.gethostname()))
                    except OSError:
                        self._found = frozenset()
                found = self._found
        return found


@dataclass(frozen=True)
class Target:
    port: int
    addresses: frozenset  # ipaddress objects of the listen address (besides loopback/unspecified)
    any_interface: bool = False  # a wildcard listener: every local address is the dashboard
    # Other dashboards of this checkout (the observer, #499): every local address counts.
    also_ports: frozenset = frozenset()
    # This host name's addresses, resolved lazily (``target_for``); ``None``: not looked up.
    host_name: _HostNameAddresses | None = field(default=None, compare=False, repr=False)

    @property
    def ports(self) -> frozenset:
        return self.also_ports | {self.port}


def _address(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _resolve(host: str) -> set:
    found = set()
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return found
    for info in infos:
        address = _address(info[4][0])
        if address is not None:
            found.add(address)
    return found


def _bindable(address) -> bool:
    """Whether this host owns ``address`` (a ``bind`` to port 0 succeeds only for a local one)."""
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(address), 0))
    except OSError:
        return False
    return True


def target_for(host: str, port: int, *, any_interface: bool = False, also_ports=()) -> Target:
    """The listen address(es) to refuse: the bind host now, this host name's on first need."""
    addresses = set()
    literal = _address(host.strip("[]"))
    if literal is not None:
        addresses.add(literal)
    else:
        addresses |= _resolve(host)
    # Kept even when one equals ``port`` (PR #563 review): an observer on another interface
    # can share the number, and its port is refused at every local address.
    others = frozenset(int(other) for other in also_ports)
    return Target(int(port), frozenset(addresses), any_interface, others, _HostNameAddresses())


def is_own_dashboard(address, port, target: Target | None) -> bool:
    """Pure decision for one resolved ``(address, port)``; an unparsable address is not own."""
    if target is None or port not in target.ports:
        return False
    parsed = (
        address
        if isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address))
        else (_address(address))
    )
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if parsed is None:
        return False
    if parsed.is_loopback or parsed.is_unspecified or parsed in target.addresses:
        return True
    if (target.any_interface or port in target.also_ports) and _bindable(parsed):
        return True
    return target.host_name is not None and parsed in target.host_name.get()


_DEFAULT_PORTS = {"http": 80, "https": 443}
_ARMED = object()


def url_refusal(url, target=_ARMED) -> str | None:
    """The refusal text (with ``E_OWN_DASHBOARD``) when ``url`` reaches the own dashboard.

    The tool-side check of #495, before anything is sent: the URL's port must be the
    dashboard's (other ports are never touched, like the connect hook), then the host is
    **resolved** and every answer is checked with :func:`is_own_dashboard` — so ``127.1``,
    ``0x7f.1``, ``2130706433``, ``[::ffff:127.0.0.1]``, ``localhost.``, this host's name and
    a name that resolves to loopback are all the dashboard. On the dashboard's port a URL
    that cannot be parsed or a host that cannot be resolved is refused too (the HTTP client
    could still read it differently). ``None``: not the dashboard, or nothing armed. The
    connect hook stays the backstop for a second, different DNS answer.
    """
    if target is _ARMED:
        target = _TARGET
    if target is None:
        return None
    if not isinstance(url, str):
        return _REFUSAL + " — the URL could not be checked"
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        host = parts.hostname
    except ValueError:
        return _REFUSAL + " — the URL could not be checked"
    if port is None:
        port = _DEFAULT_PORTS.get(parts.scheme.lower())
        if port is None:
            return None  # not an HTTP URL: the client makes no connection
    if port not in target.ports:
        return None
    if not host:
        return _REFUSAL + " — the URL has no host"
    literal = _address(host)
    if literal is not None:
        found = {literal}
    else:
        found = _resolve(host)
        if not found:
            return _REFUSAL + " — the host could not be resolved on the dashboard's port"
    if any(is_own_dashboard(address, port, target) for address in found):
        return _REFUSAL
    return None


_TARGET: Target | None = None
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()
_LOCAL = threading.local()


def normalize_host(host) -> tuple[str, object] | None:
    """The host of a ``connect()`` address, **only** for the built-in immutable types.

    Returns ``("address", ip)`` for an IP literal and ``("name", text)`` for any other
    text (also ``""`` and short forms such as ``127.1``) when ``host`` is exactly ``str``
    or exactly ``bytes`` (ASCII). Everything else — ``bytearray`` / ``memoryview``
    (mutable), a ``str`` / ``bytes`` subclass (``__str__``, ``__eq__``, ``decode`` may
    answer differently than what the socket read), an ``ipaddress`` object, any other
    object — returns ``None``: the guard does not evaluate it (no code of the caller runs).
    """
    if type(host) is bytes:
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    elif type(host) is not str:
        return None
    address = _address(host)
    return ("address", address) if address is not None else ("name", host)


def normalize_port(port) -> int | None:
    """The port **only** if it is exactly a built-in ``int`` (not ``bool``, not a subclass,
    not an object with ``__index__``): ``None`` otherwise.

    The socket converts the address *before* the ``socket.connect`` audit event and the
    hook receives the original objects, so evaluating ``__index__`` again could answer
    differently than the conversion did (a value that changes per call). The guard never
    calls into such an object; an unknown port counts as "could be the dashboard".
    """
    return port if type(port) is int else None


def _refuses(host, port, target: Target) -> bool:
    """Only the dashboard's port is ever refused; other ports pass untouched.

    Only built-in immutable values are evaluated (:func:`normalize_host`,
    :func:`normalize_port`); an unknown value is treated as possibly local / possibly the
    dashboard's port, fail closed:

    * known port, not the dashboard's → allowed (whatever the host);
    * known port, the dashboard's → refused for an own address, a **name** (the socket
      resolves it itself; a second lookup would race) and an unknown host;
    * unknown port → refused for an own address, a name and an unknown host; allowed for a
      literal address that is not this host's (other hosts are not guarded).
    """
    number = normalize_port(port)
    if number is not None and number not in target.ports:
        return False
    normalized = normalize_host(host)
    if normalized is None or normalized[0] == "name":
        return True
    address = normalized[1]
    if number is None:
        return is_own_dashboard(address, target.port, target)  # local → refused
    return is_own_dashboard(address, number, target)


def _hook(event: str, args: tuple) -> None:
    if event != "socket.connect":
        return
    target = _TARGET
    if target is None or getattr(_LOCAL, "busy", False):
        return
    try:
        sock, address = args
    except ValueError:
        return
    # The C-level family and tuple items: a socket subclass's ``family`` property or a
    # tuple subclass's ``__getitem__`` / ``__len__`` could tell something else than what
    # the socket read.
    try:
        family = _socket.socket.family.__get__(sock)
    except TypeError:
        return  # not a socket object: the event was not raised by a real connect
    if family not in (socket.AF_INET, socket.AF_INET6):
        return
    if not isinstance(address, tuple) or tuple.__len__(address) < 2:
        return
    _LOCAL.busy = True
    try:
        try:
            host, port = tuple.__getitem__(address, 0), tuple.__getitem__(address, 1)
            refused = _refuses(host, port, target)
        except Exception:  # noqa: BLE001 - a check that cannot finish refuses
            refused = True
        if refused:
            # Still ``busy``: a log handler's own connection is not checked again (#499).
            try:
                _LOG.warning("%s: refused a connection to its own dashboard", REASON_CODE)
            except Exception:  # noqa: BLE001, S110 - logging never decides the refusal
                pass
    finally:
        _LOCAL.busy = False
    if refused:
        raise OwnDashboardRefused()


class WildcardBindRefused(ValueError):
    """A wildcard listen address cannot be guarded: every local address reaches it."""


def is_wildcard(host: str) -> bool:
    """``0.0.0.0``, ``::``, ``[::]`` or empty: the listener answers on every interface."""
    text = (host or "").strip().strip("[]")
    if text == "":
        return True
    address = _address(text)
    return address is not None and address.is_unspecified


def arm(host: str, port: int, *, wildcard: bool = False, also_ports=()) -> Target:
    """Refuse own-dashboard connections from now on (idempotent; re-arm replaces).

    A wildcard listen address is refused (:class:`WildcardBindRefused`) unless
    ``wildcard`` is true: the YOLO allowance keeps needing one local address (the server
    refuses such a start before anything is created, ``stira_server.main``). With
    ``wildcard`` (the server without the allowance, #495) the target is ``any_interface``:
    an address of this host at the dashboard's port is refused too (:func:`_bindable`).
    ``also_ports``: other dashboards of this checkout (the observer's :data:`OBSERVER_PORT`,
    #499), refused at every address of this host.
    """
    global _TARGET, _INSTALLED
    any_interface = is_wildcard(host)
    if any_interface and not wildcard:
        raise WildcardBindRefused("YOLO needs a listener on one local address")
    target = target_for(host, port, any_interface=any_interface, also_ports=also_ports)
    with _INSTALL_LOCK:
        if not _INSTALLED:
            sys.addaudithook(_hook)
            _INSTALLED = True
        _TARGET = target
    return target


def armed() -> Target | None:
    return _TARGET


def disarm() -> None:
    """Tests only: the hook stays installed but checks nothing."""
    global _TARGET
    _TARGET = None
