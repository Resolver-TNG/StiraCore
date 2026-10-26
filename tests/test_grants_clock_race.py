"""#627 (Refs #296): concurrent looks never revoke a live grant as ``clock_anomaly``.

The look reads the clocks inside the leaf-lock hold that applies them, so readings reach a
grant in the order they were taken. Before the fix the reading was taken outside the lock:
a second look that read later but applied first made the first reading look like a
monotonic clock going back (a false stop-class ``clock_anomaly``).

* the seam tests put a whole concurrent look (``sync``, the watchdog's pass and the list
  view) between each clock reading of an actor and whatever the actor does next —
  deterministic: no sleep, no timing; the concurrent look either finishes or is seen
  blocked on the leaf lock held by the actor;
* the stress test runs N lookers with fixed iteration counts (no wall-clock timing) and
  asserts no false anomaly; a real clock jump injected at a fixed reading during the
  stress is still detected, by a reading taken after the jump.
"""

from __future__ import annotations

import sys
import threading

import pytest

from stiracore.grants.schema import GrantRefused
from stiracore.yolo.allowance import ClockReading
from stiracore.yolo.host_ops import HostOpRefused
from tests.grants_helpers import confirm_and_issue, confirm_and_verify, issue_body, make_service

SAFETY_SEC = 10.0  # a hang guard only: every wait below ends on an event, never on time


class TickClock:
    """Every reading moves all three clocks forward by ``step`` (time passing between any
    two readings). ``jump_at``: the reading number at which a real jump happens first;
    ``hook``: called after a reading is taken and before it is returned."""

    def __init__(self, step: float = 0.001) -> None:
        self._lock = threading.Lock()
        self.wall, self.boot, self.mono = 1_800_000_000.0, 5000.0, 5000.0
        self.step = step
        self.reads = 0
        self.jump_at: int | None = None
        self.jump = (0.0, 0.0, 0.0)  # (wall, boot, mono) deltas
        self.after_jump: set[float] = set()  # the walls of every reading at / after it
        self.hook = None

    def __call__(self) -> ClockReading:
        with self._lock:
            self.reads += 1
            if self.jump_at is not None and self.reads == self.jump_at:
                d_wall, d_boot, d_mono = self.jump
                self.wall, self.boot, self.mono = (
                    self.wall + d_wall,
                    self.boot + d_boot,
                    self.mono + d_mono,
                )
            self.wall += self.step
            self.boot += self.step
            self.mono += self.step
            reading = ClockReading(wall=self.wall, boot=self.boot, mono=self.mono)
            if self.jump_at is not None and self.reads >= self.jump_at:
                self.after_jump.add(reading.wall)
        hook = self.hook
        if hook is not None:
            hook(reading)
        return reading


class OwnedLock:
    """The grant service's leaf lock with its owner recorded, so a test can see that a
    thread is blocked on it while a given thread holds it (no timing)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner: int | None = None
        self.on_blocked = None  # (waiter ident, owner ident) -> None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(False):
            self.owner = threading.get_ident()
            return True
        if not blocking:
            return False
        report = self.on_blocked
        if report is not None:
            report(threading.get_ident(), self.owner)
        if self._lock.acquire(True, timeout):
            self.owner = threading.get_ident()
            return True
        return False

    def release(self) -> None:
        self.owner = None
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    __enter__ = acquire

    def __exit__(self, *exc) -> None:
        self.release()


@pytest.fixture
def race(tmp_path):
    clock = TickClock()
    service, authority, _recorder, _c, _breaker, ws = make_service(
        tmp_path, clocks=clock, start=False
    )
    lock = OwnedLock()
    service._lock = lock
    service.start()
    env = type("Race", (), {})()
    env.service, env.clock, env.lock, env.ws, env.authority = service, clock, lock, ws, authority
    yield env
    clock.hook = None
    service.stop(5.0)


def _issue(env, **overrides) -> str:
    body = issue_body(env.ws, **overrides)
    return confirm_and_issue(env.service, body)["grant"]["grant_id"]


def _interleave(env, actor: int, other):
    """After each clock reading of ``actor``: run ``other`` (a whole look) on its own
    thread, and continue only once it finished or is blocked on the leaf lock held by
    ``actor`` (then it runs after the actor's hold). Returns the threads (to join)."""
    threads: list[threading.Thread] = []
    others: set[int] = set()

    def blocked(waiter: int, owner: int | None) -> None:
        if waiter in others and owner == actor:
            settled[waiter].set()

    settled: dict[int, threading.Event] = {}
    env.lock.on_blocked = blocked

    def between(_reading) -> None:
        if threading.get_ident() != actor:
            return
        ready = threading.Event()
        box: dict = {}

        def run() -> None:
            me = threading.get_ident()
            box["event"] = settled[me] = threading.Event()
            others.add(me)
            ready.set()
            try:
                other()
            finally:
                settled[me].set()

        thread = threading.Thread(target=run, name="w627-other", daemon=True)
        threads.append(thread)
        thread.start()
        assert ready.wait(SAFETY_SEC)
        assert box["event"].wait(SAFETY_SEC), "the concurrent look neither ran nor blocked"

    env.clock.hook = between
    return threads


def _finish(env, threads) -> None:
    env.clock.hook = None
    env.lock.on_blocked = None
    for thread in threads:
        thread.join(SAFETY_SEC)
        assert not thread.is_alive()


def _state(env, grant_id: str) -> tuple:
    grant = env.service._get(grant_id)
    return grant.status, grant.reason


@pytest.mark.parametrize("actor_call", ["check_now", "sync", "executor_record", "list_view"])
def test_a_look_between_a_reading_and_its_apply_is_no_clock_anomaly(race, actor_call):
    """The seam: a later look (``sync``) runs between every clock reading of the actor and
    the rest of the actor's call. Before #627 the later reading was applied first and the
    actor's earlier one looked like the monotonic clock going back."""
    grant_id = _issue(race)
    calls = {
        "check_now": lambda: race.service.check_now(grant_id),
        "sync": race.service.sync,
        "executor_record": lambda: race.service.executor_record(grant_id),
        "list_view": race.service.list_view,  # GET /api/grants
    }
    threads = _interleave(race, threading.get_ident(), race.service.sync)
    try:
        calls[actor_call]()
    finally:
        _finish(race, threads)
    assert threads, "the seam never ran"
    assert _state(race, grant_id) == ("active", "")
    race.service.sync()
    assert _state(race, grant_id) == ("active", "")


def test_a_look_inside_a_verification_is_no_clock_anomaly(race):
    """The verification's look (``_verify_now``) reads its clocks in the same hold too."""
    grant_id = _issue(race)
    race.service.claim_complete(grant_id)
    real_verify = race.service.verify
    holder: dict = {}

    def seamed_verify(gid, body, *, op_id=None):
        if op_id is None:
            return real_verify(gid, body, op_id=op_id)
        holder["threads"] = _interleave(race, threading.get_ident(), race.service.sync)
        try:
            return real_verify(gid, body, op_id=op_id)
        finally:
            _finish(race, holder["threads"])

    race.service.verify = seamed_verify
    try:
        out = confirm_and_verify(race.service, grant_id, "confirm")
    except (GrantRefused, HostOpRefused) as error:  # before #627: revoked meanwhile
        out = error
    assert not isinstance(out, Exception), (out, _state(race, grant_id))
    assert holder["threads"], "the seam never ran"
    assert (out["recorded_only"], out["grant"]["state"], out["grant"]["goal_state"]) == (
        False,
        "ended",
        "complete",
    )
    assert out["grant"]["reason"] == "complete"


LOOKERS = 4
ITERATIONS = 150


def _stress(env, grant_id: str, *, jump_after: int | None = None) -> list[BaseException]:
    """``LOOKERS`` threads × ``ITERATIONS`` looks (check_now / sync / executor_record /
    list_view in turn), started together; fixed counts, no timing."""
    errors: list[BaseException] = []
    start = threading.Barrier(LOOKERS)
    service = env.service
    calls = (
        lambda: service.check_now(grant_id),
        service.sync,
        lambda: service.executor_record(grant_id),
        service.list_view,
    )
    if jump_after is not None:
        env.clock.jump_at = env.clock.reads + jump_after

    def looker(index: int) -> None:
        try:
            start.wait(SAFETY_SEC)
            for n in range(ITERATIONS):
                try:
                    calls[(index + n) % len(calls)]()
                except GrantRefused:  # a stopped grant may refuse; the state is asserted
                    pass
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible
    try:
        threads = [threading.Thread(target=looker, args=(i,), daemon=True) for i in range(LOOKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60.0)
            assert not thread.is_alive()
    finally:
        sys.setswitchinterval(old)
    return errors


def test_concurrent_lookers_never_revoke_a_live_grant(race):
    grant_id = _issue(race)
    assert _stress(race, grant_id) == []
    assert _state(race, grant_id) == ("active", "")
    assert race.clock.reads > LOOKERS * ITERATIONS  # every look read the clocks


@pytest.mark.parametrize(
    "jump",
    [
        (120.0, 0.0, 0.0),  # the wall clock set forward (R5: wall vs boot since issue)
        (-120.0, 0.0, 0.0),  # the wall clock set back
        (0.0, -1.0, -1.0),  # a monotonic clock going back (a broken clock)
    ],
    ids=["wall_forward", "wall_back", "mono_back"],
)
def test_a_real_jump_during_the_stress_is_still_a_clock_anomaly(race, jump):
    grant_id = _issue(race)
    race.clock.jump = jump
    assert _stress(race, grant_id, jump_after=LOOKERS * ITERATIONS // 2) == []
    grant = race.service._get(grant_id)
    assert (grant.status, grant.reason) == ("revoked", "clock_anomaly")
    # Seen by a reading taken at / after the jump — never a false one before it.
    assert grant.ended_wall in race.clock.after_jump
