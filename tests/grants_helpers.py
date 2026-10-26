"""Shared helpers for the grant tests (W4 of #296): fake clocks, a request body, a service."""

from __future__ import annotations

from pathlib import Path

from stiracore.authority import AuthorityService
from stiracore.authority.outbox import LifecycleRecorder
from stiracore.grants.schema import IssueRequest
from stiracore.grants.service import AutonomySettings, GrantService
from stiracore.yolo.allowance import ClockReading


class FakeClocks:
    """wall / boot (sleep-including) / mono (sleep-excluding), moved by hand."""

    def __init__(self, wall: float = 1_800_000_000.0) -> None:
        self.wall, self.boot, self.mono = wall, 5000.0, 5000.0
        self.broken = False

    def __call__(self) -> ClockReading:
        if self.broken:
            raise RuntimeError("clock")
        return ClockReading(wall=self.wall, boot=self.boot, mono=self.mono)

    def tick(self, seconds: float) -> None:  # awake: all three move
        self.wall += seconds
        self.boot += seconds
        self.mono += seconds

    def sleep(self, seconds: float) -> None:  # host asleep: mono stands still
        self.wall += seconds
        self.boot += seconds

    def jump_wall(self, seconds: float) -> None:  # someone set the wall clock
        self.wall += seconds


def request(**overrides) -> IssueRequest:
    values = {
        "goal": "make the tests green",
        "tools": ("editor", "file_read"),
        "workspace_subtree": "/tmp/ws",
        "total_tokens": 1000,
        "per_tick_tokens": 100,
        "ttl_sec": 7200,
        "max_iterations": 10,
        "no_progress_ticks": 3,
        "verifier": "user",
    }
    values.update(overrides)
    return IssueRequest(**values)


def issue_body(subtree: Path | str, **overrides) -> dict:
    body = {
        "goal": "make the tests green",
        "scope": {"tools": ["file_read", "editor"], "workspace_subtree": str(subtree)},
        "budget": {"total_tokens": 1000, "per_tick_tokens": 100},
        "verifier": "user",
        "confirm": "ISSUE",
    }
    body.update(overrides)
    return body


class Breaker:
    def __init__(self) -> None:
        self.level = "normal"

    def __call__(self):
        return self.level


def make_service(
    tmp_path: Path,
    *,
    data_dir: Path | None | str = "default",
    clocks: FakeClocks | None = None,
    breaker: Breaker | None = None,
    contained: bool = False,
    unattended: bool = True,
    authority: AuthorityService | None = None,
    recorder: LifecycleRecorder | None = None,
    start: bool = True,
    operations=None,
    browser: bool = True,
):
    """A real in-memory authority + recorder and a grant service over ``tmp_path``."""
    if data_dir == "default":
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    authority = authority or AuthorityService(None)
    recorder = recorder or LifecycleRecorder(None, on_failure=authority.report_unhealthy)
    authority.recorder = recorder
    clocks = clocks or FakeClocks()
    breaker = breaker or Breaker()
    service = GrantService(
        data_dir=data_dir,
        authority=authority,
        recorder=recorder,
        settings=AutonomySettings(
            stage_c=True, run_checks_unattended=unattended, allow_browser_confirm=browser
        ),
        clock=clocks,
        breaker=breaker,
        containment_verified=lambda: contained,
        write_roots=lambda: [workspace],
        tool_call_running=lambda: False,
        **({} if operations is None else {"operations": operations}),
    )
    if start:
        service.start()
    return service, authority, recorder, clocks, breaker, workspace


def confirm_and_issue(service: GrantService, body: dict) -> dict:
    """The three steps of an issue: 428 → browser confirmation → the re-sent request."""
    from stiracore.grants.service import GrantConfirmation

    try:
        service.issue(body)
    except GrantConfirmation as needed:
        view = needed.view
    else:  # pragma: no cover - the first press always needs the operation
        raise AssertionError("no confirmation asked")
    service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    result = service.issue(body, op_id=view["id"])
    result["op_id"] = view["id"]
    return result


def confirm_and_resume(service: GrantService, grant_id: str, body: dict) -> dict:
    from stiracore.grants.service import GrantConfirmation

    try:
        service.resume(grant_id, body)
    except GrantConfirmation as needed:
        view = needed.view
    else:  # pragma: no cover
        raise AssertionError("no confirmation asked")
    service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    result = service.resume(grant_id, body, op_id=view["id"])
    result["op_id"] = view["id"]
    return result


def events(recorder: LifecycleRecorder) -> list[dict]:
    """Every queued / written event (codes only) in order."""
    with recorder._queue_lock:
        pending = list(recorder._pending)
    return [*recorder.log.events(), *pending]


def confirm_op(service, call):
    """Run ``call(op_id)``: first without an operation (428), confirm it in the browser,
    then again with it. Returns the result with ``op_id``."""
    from stiracore.grants.service import GrantConfirmation

    try:
        call(None)
    except GrantConfirmation as needed:
        view = needed.view
    else:  # pragma: no cover
        raise AssertionError("no confirmation asked")
    service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    result = call(view["id"])
    result["op_id"] = view["id"]
    return result


def confirm_and_verify(service, grant_id: str, decision: str) -> dict:
    body = {"decision": decision}
    return confirm_op(service, lambda op: service.verify(grant_id, body, op_id=op))


def settle(service, timeout: float = 5.0) -> str:
    """Wait (bounded) for the writer: the shutdown step's answer."""
    return service.stop(timeout)
