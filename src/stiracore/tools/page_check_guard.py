"""G and the axis check of the page tools — observation only (ADR 0015 Addendum rev 6, S1).

**G** (:func:`measure_guard`) is the start-time measurement of rev 5 Part 1: Stira's own
fixed page is opened with the ordinary ``launch_flags`` and tries ``fetch`` / ``<img>`` /
``<iframe>`` / prefetch / ``sendBeacon`` / ``WebSocket`` / a WebRTC STUN request at a
loopback canary on another port, plus a ``file://`` read outside its directory. Pass = the
canary received nothing, the proxy counted refusals, the file read was counted as
``outside_workspace`` and the canary answers a direct probe from Stira (so "nothing
received" is not a dead canary).

**The axis** (``axis.check(scope)``) evaluates the invariants A1–A10 and keeps the last
result. Nothing consumes either result yet: no rule, tier, scope or list reads them.

Both results live in this module's memory only. They are never read from a file, a
setting, a route, a tool argument or the model, and every reader fails closed: a state
that cannot be read is ``unverified`` / ``axis_unreadable``. Results are fixed codes and
booleans; no URL, path, host or page text is kept or logged (this module does not log).

This module imports :mod:`page_check` only inside functions (``page_check`` re-exports
the names below).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERIFIED = "verified"
UNVERIFIED = "unverified"
UNMEASURED = "unmeasured"
GUARD_STATES = (VERIFIED, UNVERIFIED, UNMEASURED)
GUARD_CODES = (
    "guard_verified",
    "not_run",  # no measurement was started in this process
    "guard_pending",  # a measurement is running (or the wait for it ran out)
    "platform_unmeasured",  # A3: no measured run of this kind exists for the OS
    "no_browser",
    "browser_unidentified",  # the path / ``--version`` pair could not be read
    "browser_changed",  # A2: the pair differs from the measured one
    "canary_dead",
    "canary_heard",
    "proxy_silent",
    "file_read_uncounted",
    "guard_run_failed",
    "browser_guard_unverified",  # A4: a call ended with this code since the start
    "guard_unreadable",
)
# OS names (``sys.platform``) with a measured canary run of this kind (ADR 0015 Q17).
MEASURED_PLATFORMS = ("darwin",)
GUARD_WAIT_S = 10.0  # the first reader waits this long, then the state is ``unmeasured``
# A measurement that ended with one of these heard nothing at the canary and judged
# nothing (the browser did not start, the run timed out): it is measured again, at most
# ``len(GUARD_RETRY_AFTER_S)`` times per process, the n-th not before that many seconds
# after the failed one ended. Every other failure is final for the process.
GUARD_RETRY_CODES = ("guard_run_failed",)
GUARD_RETRY_AFTER_S = (5.0, 30.0, 120.0)
AXIS_MAX_AGE_S = 600.0
AXIS_CODES = (
    "pass",
    "not_run",
    "axis_stale",
    "axis_unreadable",
    "other",
    "guard_pending",  # A1 / A2: G is running or will be measured again (not sticky)
    "browser_guard_unverified",
    "platform_unmeasured",
    "rules_locked",
    "rules_changed",
    "record_unhealthy",
    "ledger_unhealthy",
    "write_floor_changed",
    "reviewer_changed",
    "reviewer_remote",
    "file_read_not_always",
    "not_in_scope",
    "breaker_not_normal",
)

_G_WAIT_MS = 2000
_G_TIME_LIMIT_S = 8.0
_VERSION_TIMEOUT_S = 5.0
# Stira's own page (never the agent's file). ``__PORT__`` is the canary's port.
_G_PAGE = """<!doctype html><meta charset="utf-8"><title>stira-guard</title>
<link rel="prefetch" href="http://127.0.0.1:__PORT__/p">
<img src="http://127.0.0.1:__PORT__/i.png" alt="">
<iframe src="http://127.0.0.1:__PORT__/frame"></iframe>
<img src="file:///etc/hosts" alt="">
<script>
var c = '127.0.0.1:__PORT__';
try { fetch('http://' + c + '/f').catch(function () {}); } catch (e) {}
try { navigator.sendBeacon('http://' + c + '/b', 'x'); } catch (e) {}
try { new WebSocket('ws://' + c + '/w'); } catch (e) {}
try {
  var rtc = new RTCPeerConnection({iceServers: [{urls: 'stun:' + c}]});
  rtc.createDataChannel('x');
  rtc.createOffer().then(function (o) { return rtc.setLocalDescription(o); })
    .catch(function () {});
} catch (e) {}
try { fetch('file:///etc/hosts').catch(function () {}); } catch (e) {}
</script>
"""


@dataclass(frozen=True)
class GuardState:
    """``state`` is one of :data:`GUARD_STATES`, ``code`` one of :data:`GUARD_CODES`."""

    state: str
    code: str
    elapsed_ms: int | None = None

    @property
    def verified(self) -> bool:
        return self.state == VERIFIED


@dataclass(frozen=True)
class AxisResult:
    """``code``: ``pass`` or the first failing item's code; ``items``: ``(id, ok)``;
    ``measured_at``: a reading of the injected monotonic clock."""

    code: str
    items: tuple[tuple[str, bool], ...]
    measured_at: float


# --- the canary ---------------------------------------------------------------------------


class Canary:
    """A loopback TCP listener and a UDP socket on the same port; counts what arrives."""

    def __init__(self) -> None:
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._tcp.bind(("127.0.0.1", 0))
            self._tcp.listen(32)
            self._tcp.settimeout(0.1)
            self.port = self._tcp.getsockname()[1]
            self._udp.bind(("127.0.0.1", self.port))
            self._udp.settimeout(0.1)
        except OSError:
            self._tcp.close()
            self._udp.close()
            raise
        self._hits = 0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._threads = [
            threading.Thread(target=self._serve_tcp, name="page-guard-canary", daemon=True),
            threading.Thread(target=self._serve_udp, name="page-guard-canary-udp", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _hit(self) -> None:
        with self._lock:
            self._hits += 1

    def _serve_tcp(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._tcp.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._hit()  # a connection is a leak, whatever it carries
            conn.close()

    def _serve_udp(self) -> None:
        while not self._closed.is_set():
            try:
                self._udp.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            self._hit()

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    def reset(self) -> None:
        with self._lock:
            self._hits = 0

    def _reaches(self, expected: int) -> bool:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.hits >= expected:
                return True
            time.sleep(0.01)
        return False

    def probe(self) -> bool:
        """A direct TCP connection and a UDP packet from Stira are both counted."""
        before = self.hits
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1.0) as stream:
                stream.sendall(b"x")
            if not self._reaches(before + 1):
                return False
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as packet:
                packet.sendto(b"x", ("127.0.0.1", self.port))
            return self._reaches(before + 2)
        except OSError:
            return False

    def close(self) -> None:
        self._closed.set()
        for thread in self._threads:
            thread.join(1.0)
        self._tcp.close()
        self._udp.close()


def _open_canary() -> Canary:
    last: OSError | None = None
    for _ in range(5):  # the UDP port of the same number may be taken
        try:
            return Canary()
        except OSError as error:
            last = error
    raise last or OSError("no canary port")


# --- the browser's identity (A2) ------------------------------------------------------------


class BrowserIdentity:
    """``(path, --version output)``; the version is cached per ``(st_dev, st_ino,
    st_mtime_ns)`` so a call compares with a ``stat`` only."""

    def __init__(
        self,
        *,
        stat: Callable[[str], Any] = os.stat,
        version: Callable[[str], str | None] | None = None,
    ) -> None:
        self._stat = stat
        self._version = version or _read_version
        self._cache: dict[tuple[str, int, int, int], str] = {}
        self._lock = threading.Lock()

    def _key(self, path: str) -> tuple[str, int, int, int] | None:
        try:
            info = self._stat(path)
            return (path, int(info.st_dev), int(info.st_ino), int(info.st_mtime_ns))
        except (OSError, AttributeError, TypeError, ValueError):
            return None

    def cached(self, path: str) -> tuple[str, str] | None:
        """The pair when this very file was read before; never starts a process."""
        key = self._key(path)
        with self._lock:
            version = self._cache.get(key) if key is not None else None
        return None if version is None else (path, version)

    def read(self, path: str) -> tuple[str, str] | None:
        """The pair, running ``<browser> --version`` when this file is new."""
        key = self._key(path)
        if key is None:
            return None
        with self._lock:
            version = self._cache.get(key)
        if version is None:
            version = self._version(path)
            if not version:
                return None
            with self._lock:
                self._cache[key] = version
        return (path, version)


def _read_version(path: str) -> str | None:
    from . import page_check as pc

    try:
        done = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT_S,
            env=pc._browser_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.decode("utf-8", "replace").strip()[:200]
    return text if done.returncode == 0 and text else None


# --- G --------------------------------------------------------------------------------------


def _run_fixed_page(browser: str, canary_port: int) -> dict[str, Any]:
    """Open the fixed page through the ordinary ``run_page_check`` (same flags, proxy,
    per-session confirmation) in a directory of its own, removed afterwards."""
    from ..identity_paths import WritePathPolicy
    from . import page_check as pc

    root = Path(tempfile.mkdtemp(prefix="stira-page-guard-")).resolve()
    try:
        (root / "guard.html").write_text(
            _G_PAGE.replace("__PORT__", str(int(canary_port))), encoding="utf-8"
        )
        policy = WritePathPolicy(base_dir=root, write_roots=[root])
        return pc.run_page_check(
            "guard.html",
            policy,
            browser=browser,
            wait_ms=_G_WAIT_MS,
            time_limit=_G_TIME_LIMIT_S,
            screenshot=False,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class Guard:
    """The guard state of one process. The module keeps one (:func:`guard_state`)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        platform: str | None = None,
        runner: Callable[[str, int], Any] | None = None,
        canary: Callable[[], Any] | None = None,
        identity: BrowserIdentity | None = None,
        find_browser: Callable[[], str | None] | None = None,
    ) -> None:
        self._clock = clock
        self._platform = platform
        self._probe_fn = runner or _run_fixed_page
        self._canary = canary or _open_canary
        self._identity = identity or BrowserIdentity()
        self._find_browser = find_browser or _find_browser
        self._lock = threading.Lock()
        self._state = GuardState(UNMEASURED, "not_run")
        self._measured: tuple[str, str] | None = None  # the pair of the last measurement
        self._measured_at: float | None = None  # the clock's reading when it ended
        self._dropped = False  # A4, sticky until the next start
        self._retries = 0  # re-measurements after a transient failure, per process
        self._thread: threading.Thread | None = None

    # -- readers --

    def state(self) -> GuardState:
        """The current state; anything unreadable is ``unverified`` (fail closed)."""
        try:
            with self._lock:
                state = self._state
                dropped = self._dropped
            if dropped:
                return GuardState(UNVERIFIED, "browser_guard_unverified", state.elapsed_ms)
            if state.state not in GUARD_STATES or state.code not in GUARD_CODES:
                return GuardState(UNVERIFIED, "guard_unreadable")
            return state
        except Exception:  # noqa: BLE001 - fail closed
            return GuardState(UNVERIFIED, "guard_unreadable")

    @property
    def dropped(self) -> bool:
        with self._lock:
            return self._dropped

    def platform_measured(self) -> bool:
        return (self._platform or sys.platform) in MEASURED_PLATFORMS

    def _retry_left(self) -> bool:
        """Under the lock: the last measurement failed transiently and may run again."""
        return (
            not self._dropped
            and self._state.code in GUARD_RETRY_CODES
            and self._measured_at is not None  # no clock reading: no backoff, so final
            and self._retries < len(GUARD_RETRY_AFTER_S)
        )

    def _retry_due(self) -> bool:
        """Under the lock: :meth:`_retry_left`, and the backoff has passed."""
        if not self._retry_left():
            return False
        try:
            waited = float(self._clock()) - self._measured_at
        except Exception:  # noqa: BLE001 - a clock that cannot be read starts nothing
            return False
        return waited >= GUARD_RETRY_AFTER_S[self._retries]

    @property
    def pending(self) -> bool:
        """No verdict yet, and one is coming: G is running (or the wait for it ran out),
        or its last run failed transiently and will be measured again. Never ``True``
        for a final failure or after A4 dropped the state."""
        with self._lock:
            if self._dropped:
                return False
            return self._state.code == "guard_pending" or self._retry_left()

    # -- the measurement --

    def measure(self, browser: str | None = None) -> GuardState:
        """Run G now (blocking) and keep the result. Never raises."""
        started: float | None = None
        measured: tuple[str, str] | None = None
        try:
            started = float(self._clock())
            state, code, measured = self._measure(browser)
        except Exception:  # noqa: BLE001 - a measurement that broke measured nothing good
            state, code = UNVERIFIED, "guard_run_failed"
        ended: float | None = None
        try:
            ended = float(self._clock())
            elapsed = None if started is None else max(0, int((ended - started) * 1000))
        except Exception:  # noqa: BLE001
            ended, elapsed = None, None
        result = GuardState(state, code, elapsed)
        with self._lock:
            self._state = result
            self._measured = measured
            self._measured_at = ended
        return self.state()

    def age_s(self) -> int | None:
        """Whole seconds since the last measurement ended (the injected clock). None:
        nothing was measured yet, or the clock cannot be read. Display only."""
        try:
            with self._lock:
                ended = self._measured_at
            if ended is None:
                return None
            age = int(float(self._clock()) - ended)
            return age if age >= 0 else None
        except Exception:  # noqa: BLE001 - shown as "not measured"
            return None

    def _measure(self, browser: str | None) -> tuple[str, str, tuple[str, str] | None]:
        if not self.platform_measured():
            return UNMEASURED, "platform_unmeasured", None
        browser = browser or self._find_browser()
        if not browser:
            return UNMEASURED, "no_browser", None
        pair = self._identity.read(browser)
        if pair is None:
            return UNMEASURED, "browser_unidentified", None
        try:
            canary = self._canary()
        except OSError:
            return UNVERIFIED, "canary_dead", pair
        try:
            if not canary.probe():
                return UNVERIFIED, "canary_dead", pair
            canary.reset()
            try:
                result = self._probe_fn(browser, canary.port)
            except BaseException:
                # The probe died after the page may already have reached the canary: read
                # the hits BEFORE any failure is classified, so a run that leaked can never
                # end as a transient (re-measured) `guard_run_failed` (review #778 F1).
                time.sleep(0.2)
                if canary.hits != 0:
                    return UNVERIFIED, "canary_heard", pair
                raise
            time.sleep(0.2)  # the listener's accept loop
            if canary.hits != 0:
                return UNVERIFIED, "canary_heard", pair
            if not isinstance(result, dict) or result.get("status") not in ("ok", "truncated"):
                return UNVERIFIED, "guard_run_failed", pair
            if result.get("reason") not in (None, "output_truncated") or not result.get("loaded"):
                return UNVERIFIED, "guard_run_failed", pair
            network = result.get("network")
            network = network if isinstance(network, dict) else {}
            if _count(network.get("proxy_refused")) < 1 or _count(network.get("blocked")) < 1:
                return UNVERIFIED, "proxy_silent", pair
            blocked = result.get("blocked")
            blocked = blocked if isinstance(blocked, dict) else {}
            if _count(blocked.get("outside_workspace")) < 1:
                return UNVERIFIED, "file_read_uncounted", pair
            if not canary.probe():  # still listening: "nothing received" was a real zero
                return UNVERIFIED, "canary_dead", pair
            return VERIFIED, "guard_verified", pair
        finally:
            canary.close()

    def start(self, browser: str | None = None) -> bool:
        """Start G on a background thread (never on the caller's). False when one is
        running or a measurement already exists for this process — unless that one
        failed transiently and its backoff has passed (:data:`GUARD_RETRY_AFTER_S`)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            retry = self._retry_due()
            if self._state.code not in ("not_run", "browser_changed") and not retry:
                return False
            if retry:
                self._retries += 1
            self._state = GuardState(UNMEASURED, "guard_pending")
            thread = threading.Thread(
                target=self.measure, args=(browser,), name="page-guard", daemon=True
            )
            self._thread = thread
        thread.start()
        return True

    def ensure(self, wait_s: float = GUARD_WAIT_S, browser: str | None = None) -> GuardState:
        """The state for a caller that needs it: start G if it never ran, wait at most
        ``wait_s`` for a running one. Still running after that = ``unmeasured``."""
        try:
            self.start(browser)
            with self._lock:
                thread = self._thread
            if thread is not None:
                thread.join(max(0.0, min(float(wait_s), GUARD_WAIT_S)))
        except Exception:  # noqa: BLE001 - fail closed
            return GuardState(UNVERIFIED, "guard_unreadable")
        return self.state()

    def wait(self, timeout_s: float = GUARD_WAIT_S) -> bool:
        """Wait at most ``timeout_s`` (capped at :data:`GUARD_WAIT_S`) for a running
        measurement to end, so that its browser is closed before the process exits.
        Starts none. True: none is running any more."""
        try:
            with self._lock:
                thread = self._thread
            if thread is None:
                return True
            thread.join(max(0.0, min(float(timeout_s), GUARD_WAIT_S)))
            return not thread.is_alive()
        except Exception:  # noqa: BLE001 - a stop never fails on the wait
            return False

    # -- per call (A2, A4) --

    def note_call(self, reason: Any) -> None:
        """A4: a call that ended ``browser_guard_unverified`` drops the state until the
        next start. It can only drop; nothing here raises the state."""
        if reason == "browser_guard_unverified":
            with self._lock:
                self._dropped = True

    def same_browser(self, browser: str | None) -> bool:
        """A2: is ``browser`` the file G measured (a ``stat`` and a memory comparison)?
        If not, the state becomes ``unmeasured`` and G runs again in the background."""
        with self._lock:
            measured = self._measured
        if measured is None:
            return False
        browser = browser or measured[0]
        if self._identity.cached(browser) == measured:
            return True
        with self._lock:
            self._state = GuardState(UNMEASURED, "browser_changed")
            self._measured = None
        self.start(browser)
        return False


def _find_browser() -> str | None:
    from . import page_check as pc

    status = pc.browser_status()
    return status.path if status.available else None


_GUARD = Guard()


def measure_guard(browser: str | None = None) -> GuardState:
    """Run G now and keep the result in memory (blocking: call it off the request path)."""
    return _GUARD.measure(browser)


def start_guard_measurement(browser: str | None = None) -> bool:
    """Run G once on a background thread (the server's start; the doctor)."""
    try:
        return _GUARD.start(browser)
    except Exception:  # noqa: BLE001 - the measurement never stops a start
        return False


def wait_guard_measurement(timeout_s: float = GUARD_WAIT_S) -> bool:
    """The server's stop: wait (bounded) for a running G to close its browser. Starts none."""
    try:
        return _GUARD.wait(timeout_s)
    except Exception:  # noqa: BLE001 - a stop never fails on the wait
        return False


def ensure_guard(wait_s: float = GUARD_WAIT_S) -> GuardState:
    return _GUARD.ensure(wait_s)


def guard_state() -> GuardState:
    """``verified`` / ``unverified`` / ``unmeasured`` with a fixed code. Memory only."""
    try:
        return _GUARD.state()
    except Exception:  # noqa: BLE001 - fail closed
        return GuardState(UNVERIFIED, "guard_unreadable")


def note_call_result(reason: Any) -> None:
    """Called by ``run_page_check`` with each call's reason code (A4)."""
    with contextlib.suppress(Exception):  # never breaks the call that reports
        _GUARD.note_call(reason)


def note_browser(browser: str | None) -> None:
    """Called at each page tool call (A2): a changed browser file makes the state
    ``unmeasured`` and starts G again in the background. A ``stat``; never blocks on G."""
    with contextlib.suppress(Exception):  # never breaks the call that reports
        _GUARD.same_browser(browser)


# --- the axis -------------------------------------------------------------------------------


@dataclass(frozen=True)
class AxisScope:
    """What one check reads. A missing reader fails its item (fail closed)."""

    policy: Any = None  # the active WritePathPolicy (A7)
    tool_config: Callable[[], Any] | None = None  # the gate's current ToolConfig (A5)
    event_store: Any = None  # the audit event store (A6)
    engine: Any = None  # the StiraEngine (A10)
    file_read_rule: Callable[[], Any] | None = None  # file_read's effective rule (A9)
    grant_tools: frozenset[str] | None = None  # a grant's scope.tools (A9, grant: also this)
    ledger: Any = None  # the grant's round ledger (A6, grant)
    browser: str | None = None  # the path the tool would start (A2)
    tool: str = "page_look"


def _on_disk(path: Any) -> list[Any]:
    """What a root is on disk now: its resolved path and ``[st_dev, st_ino]`` (``None``
    for a root that is not set; no pair for one that cannot be read)."""
    if path is None:
        return [None, None]
    real = os.path.realpath(str(path))
    try:
        info = os.stat(real)
        return [real, [int(info.st_dev), int(info.st_ino)]]
    except OSError:
        return [real, None]


def _floor_digest(policy: Any) -> str:
    """A7: the policy's own values (constant while the policy object lives) and what the
    roots are on disk — the workspace root and the floor tree as resolved path and
    inode, each protected directory as its resolved path. A root that was replaced
    (another directory, a link put in its place) changes the digest."""
    parts = [
        str(policy.workspace),
        str(policy.floor_tree),
        sorted(str(p) for p in policy.floor_tree_writable),
        sorted(str(p) for p in policy._protected_dirs),
        _on_disk(policy.workspace),
        _on_disk(policy.floor_tree),
        sorted(os.path.realpath(str(p)) for p in policy._protected_dirs),
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


class Axis:
    """A1–A10. ``check`` measures and keeps the result; ``current`` only reads it."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, guard: Guard | None = None
    ) -> None:
        self._clock = clock
        self._guard_override = guard
        self._lock = threading.Lock()
        self._result: AxisResult | None = None
        self._full_at: float | None = None
        self._generation: Any = None
        self._floor: str | None = None
        self._floor_changed = False  # sticky until restart
        self._chain: bool | None = None  # verify_chain(), once per start

    @property
    def _guard(self) -> Guard:
        return self._guard_override or _GUARD

    def current(self) -> AxisResult:
        """The cached result: ``not_run`` before any check, ``axis_stale`` when the last
        full check is older than :data:`AXIS_MAX_AGE_S`, ``axis_unreadable`` on any error."""
        try:
            now = float(self._clock())
            with self._lock:
                result, full_at = self._result, self._full_at
            if result is None or full_at is None:
                return AxisResult("not_run", (), now)
            if result.code not in AXIS_CODES:
                return AxisResult("axis_unreadable", (), now)
            if not 0 <= now - full_at <= AXIS_MAX_AGE_S:
                return AxisResult("axis_stale", result.items, result.measured_at)
            return result
        except Exception:  # noqa: BLE001 - fail closed
            return AxisResult("axis_unreadable", (), 0.0)

    def check(self, scope: AxisScope, *, full: bool = False) -> AxisResult:
        """Evaluate the invariants. ``full``: A1–A10 (waits for G up to
        :data:`GUARD_WAIT_S`); otherwise the light check (A2, A4–A10), which becomes a full
        one when no fresh full result exists. A failure replaces the cached result at
        once; a light pass leaves it. Never raises: an error is ``axis_unreadable``."""
        try:
            now = float(self._clock())
            with self._lock:
                full_at = self._full_at
            full = full or full_at is None or not 0 <= now - full_at <= AXIS_MAX_AGE_S
            rows = self._rows(scope, full)
            code = next((c for _, ok, c in rows if not ok), "pass")
            if code not in AXIS_CODES:
                code = "other"
            result = AxisResult(code, tuple((name, ok) for name, ok, _ in rows), now)
        except Exception:  # noqa: BLE001 - fail closed
            result, full = AxisResult("axis_unreadable", (), self._now_or_zero()), True
        with self._lock:
            if full:
                self._result, self._full_at = result, result.measured_at
            elif result.code != "pass":
                self._result = result
        return result

    def _now_or_zero(self) -> float:
        try:
            return float(self._clock())
        except Exception:  # noqa: BLE001
            return 0.0

    def _rows(self, scope: AxisScope, full: bool) -> list[tuple[str, bool, str]]:
        guard = self._guard
        rows: list[tuple[str, bool, str]] = []
        if full:
            state = guard.ensure(GUARD_WAIT_S, scope.browser)
            verified = state.state == VERIFIED
            if state.code == "platform_unmeasured":
                code = "platform_unmeasured"
            elif not verified and guard.pending:
                code = "guard_pending"  # no verdict yet: measured again, not sticky
            else:
                code = "browser_guard_unverified"
            rows.append(("A1", verified, code))
        same = guard.same_browser(scope.browser)
        # Nothing to compare with while G runs (its first run, or the re-run a changed
        # browser file started): the light check must not turn "pending" into a final code.
        waiting = not same and guard.pending
        rows.append(("A2", same, "guard_pending" if waiting else "browser_guard_unverified"))
        if full:
            rows.append(("A3", guard.platform_measured(), "platform_unmeasured"))
        rows.append(("A4", not guard.dropped, "browser_guard_unverified"))
        rows.append(self._rules(scope, full))
        rows.append(self._record(scope, full))
        rows.append(self._write_floor(scope))
        rows.append(("A8", True, "reviewer_changed"))  # S1: no reviewer exists yet
        rows.append(self._file_read(scope))
        rows.append(("A10", self._breaker_normal(scope), "breaker_not_normal"))
        return rows

    def _rules(self, scope: AxisScope, full: bool) -> tuple[str, bool, str]:
        if scope.tool_config is None:
            return ("A5", False, "rules_locked")
        config = scope.tool_config()
        if config is None or config.integrity_error is not None:
            return ("A5", False, "rules_locked")
        generation = config.generation
        with self._lock:
            if full:
                self._generation = generation
            same = self._generation == generation
        return ("A5", same, "rules_changed")

    def _record(self, scope: AxisScope, full: bool) -> tuple[str, bool, str]:
        store = scope.event_store
        if store is None or store.healthy is not True:
            return ("A6", False, "record_unhealthy")
        with self._lock:
            chain = self._chain
        if chain is None:  # at start only: it re-hashes every row
            chain = store.verify_chain()[0] is True
            with self._lock:
                self._chain = chain
        if not chain or store.healthy is not True:
            return ("A6", False, "record_unhealthy")
        ledger = scope.ledger
        if ledger is not None:
            if ledger.unhealthy_reason is not None:
                return ("A6", False, "ledger_unhealthy")
            if full and ledger.verify() is not None:
                return ("A6", False, "ledger_unhealthy")
        return ("A6", True, "record_unhealthy")

    def _write_floor(self, scope: AxisScope) -> tuple[str, bool, str]:
        if scope.policy is None:
            return ("A7", False, "write_floor_changed")
        digest = _floor_digest(scope.policy)
        with self._lock:
            if self._floor is None:
                self._floor = digest
            if digest != self._floor:
                self._floor_changed = True
            return ("A7", not self._floor_changed, "write_floor_changed")

    def _file_read(self, scope: AxisScope) -> tuple[str, bool, str]:
        if scope.grant_tools is not None and "file_read" not in scope.grant_tools:
            return ("A9", False, "not_in_scope")
        # Chat and grant alike: a page reads workspace files, so no call runs without a
        # card unless ``file_read`` itself needs none now (its live rule is Always).
        if scope.file_read_rule is None:
            return ("A9", False, "file_read_not_always")
        return ("A9", scope.file_read_rule() == "always", "file_read_not_always")

    def _breaker_normal(self, scope: AxisScope) -> bool:
        if scope.engine is None:
            return False
        from ..agent.checkpoint_hooks import fresh_breaker_rank

        return fresh_breaker_rank(scope.engine) == 0


axis = Axis()


def guard_report() -> dict[str, str]:
    """What ``stira doctor`` and the settings page show: three fixed codes, read only."""
    try:
        state = guard_state()
        code = axis.current().code
        return {
            "guard": state.state if state.state in GUARD_STATES else UNVERIFIED,
            "guard_code": state.code if state.code in GUARD_CODES else "guard_unreadable",
            "axis": code if code in AXIS_CODES else "axis_unreadable",
        }
    except Exception:  # noqa: BLE001 - fail closed
        return {"guard": UNVERIFIED, "guard_code": "guard_unreadable", "axis": "axis_unreadable"}


def guard_row() -> dict[str, Any]:
    """The read-only settings row: :func:`guard_report` plus ``measured_age_s`` (whole
    seconds since G ended, ``None`` when nothing was measured). Codes and a number only."""
    row: dict[str, Any] = dict(guard_report())
    try:
        age = _GUARD.age_s()
    except Exception:  # noqa: BLE001 - shown as "not measured"
        age = None
    ok = isinstance(age, int) and not isinstance(age, bool) and age >= 0
    row["measured_age_s"] = age if ok else None
    return row
