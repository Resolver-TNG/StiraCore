"""ADR 0012 §8b revision 6: the **browser** confirmation channel (the default with
``STIRA_YOLO_ALLOW``) and the three fixes carried over from the phase-2 review.

On the real chat server app (offline, no model call). Every layer of the browser
confirmation is pinned by a test that removes exactly that layer and sees the refusal:
the one-time token, the checkbox (phase 2c: ``acknowledged: true`` instead of a typed
word), the instance reference, ``Origin`` /
``Sec-Fetch-Site``, the own-dashboard guard, the running tool call, the host channel
setting. None of them proves a person (§8b); together they stop a confirmation that is
not the person's own press in the page.
"""

from __future__ import annotations

import secrets
import threading

import pytest

from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo import state as sm
from stiracore.yolo.host_ops import HEADER, HostOperations, HostOpRefused
from stiracore.yolo.requests import YoloRequests
from tests.test_yolo_host_gate import LOOSEN, TIGHTEN_OTHER, _build
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.yolo_host import confirmed_post, host_decide, post

BROWSER = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    # Premise 2 of §8b (the own-dashboard guard) is armed by main() at a real start.
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(allowance="internal", host_confirm="browser", **kw):
        app = _build(
            tmp_path / str(len(made)), monkeypatch, allowance, host_confirm=host_confirm, **kw
        )
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _operation(response):
    data = response.get_json() or {}
    assert response.status_code == 428, (response.status_code, data)
    assert data["code"] == "host_confirmation_required"
    return data["operation"]


def confirm(client, operation, *, headers=BROWSER, **override):
    body = {
        "token": operation.get("confirm_token"),
        "acknowledged": True,
        "instance": operation.get("instance"),
        **override,
    }
    return client.post(
        f"/api/host-operations/{operation['id']}/confirm", json=body, headers=dict(headers)
    )


# --- a confirmed operation through the browser channel (a loosening tool-rule save) ----

RULES = "/api/config/tools"


def _ask_tools(client):
    return client.get("/api/tools/config").get_json()["ask_tools"]


def test_a_loosening_save_is_confirmed_in_the_browser_then_applied_once(make_app):
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    assert operation["channel"] == "browser" and operation["kind"] == "rules_loosen"
    assert operation["command"] is None
    assert len(operation["confirm_token"]) == 32 and len(operation["instance"]) == 16
    # The fixed format the page renders: enums and short codes only.
    assert set(operation["summary"]) == {"source", "changes"}
    # Not confirmed yet: the re-send is refused, nothing saved.
    refused = post(client, RULES, LOOSEN, op_id=operation["id"])
    assert refused.get_json()["code"] == "host_operation_pending"
    assert "http_request" not in _ask_tools(client)
    response = confirm(client, operation)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["operation"]["state"] == "confirmed"
    assert "confirm_token" not in response.get_json()["operation"]
    applied = post(client, RULES, LOOSEN, op_id=operation["id"])
    assert applied.status_code == 200, applied.get_json()
    assert "http_request" in _ask_tools(client)
    # A second confirmation of the spent operation is refused.
    assert confirm(client, operation).get_json()["code"] == "host_operation_used"


def test_the_token_is_only_in_the_answer_that_created_the_operation(make_app):
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    status = client.get(f"/api/host-operations/{operation['id']}").get_json()["operation"]
    assert "confirm_token" not in status and status["state"] == "pending"
    assert operation["confirm_token"] not in str(runtime.desk().operations.pending())


@pytest.mark.parametrize(
    "layer",
    [
        "token",
        "unticked",
        "ticked_as_text",
        "ticked_as_one",
        "instance",
        "origin",
        "sec_fetch_site",
        "cross_site",
        "own_dashboard",
    ],
)
def test_each_layer_of_the_browser_confirmation_refuses_when_it_fails(make_app, monkeypatch, layer):
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    headers = dict(BROWSER)
    override = {}
    if layer == "token":
        override["token"] = secrets.token_hex(16)
    elif layer == "unticked":
        override["acknowledged"] = False
    elif layer == "ticked_as_text":
        override["acknowledged"] = "true"
    elif layer == "ticked_as_one":
        override["acknowledged"] = 1
    elif layer == "instance":
        override["instance"] = "0" * 16
    elif layer == "origin":
        headers.pop("Origin")
    elif layer == "sec_fetch_site":
        headers.pop("Sec-Fetch-Site")
    elif layer == "cross_site":
        headers["Sec-Fetch-Site"] = "same-site"
    elif layer == "own_dashboard":
        monkeypatch.setattr(own_dashboard, "armed", lambda: None)
    response = confirm(client, operation, headers=headers, **override)
    not_ticked = (400, "host_not_acknowledged")
    expected = {
        "token": (409, "host_operation_mismatch"),
        "unticked": not_ticked,
        "ticked_as_text": not_ticked,
        "ticked_as_one": not_ticked,
        "instance": (409, "host_operation_incarnation"),
        "origin": (403, "host_browser_refused"),
        "sec_fetch_site": (403, "host_browser_refused"),
        "cross_site": (403, "host_browser_refused"),
        "own_dashboard": (503, "host_channel_unavailable"),
    }[layer]
    assert (response.status_code, response.get_json()["code"]) == expected
    # Nothing confirmed: the re-send is still refused and nothing saved.
    resent = post(client, RULES, LOOSEN, op_id=operation["id"])
    assert resent.get_json()["code"] == "host_operation_pending"
    assert "http_request" not in _ask_tools(client)
    # A missing tick is not a denial: with the layer intact the same operation confirms.
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    assert confirm(client, operation).status_code == 200


@pytest.mark.parametrize(
    "instance", ["あ" * 16, "", "0" * 17, "0" * 4096, "ABCDEF0123456789", None]
)
def test_a_malformed_instance_is_a_refusal_not_an_exception(make_app, instance):
    """Review of #432 (minor 1): non-ASCII / empty / too long / upper-case / missing —
    ``compare_digest`` raises ``TypeError`` on non-ASCII text, so the shape is checked
    first; the answer is the ordinary 409 and the operation stays pending."""
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    response = confirm(client, operation, instance=instance)
    assert (response.status_code, response.get_json()["code"]) == (
        409,
        "host_operation_incarnation",
    )
    assert runtime.desk().operations.status(operation["id"])["state"] == "pending"


def test_a_confirmation_while_an_agent_tool_call_runs_is_refused(make_app):
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = confirm(client, operation)
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 409 and response.get_json()["code"] == "tool_call_running"
    assert runtime.desk().operations.status(operation["id"])["state"] == "pending"


def test_an_operation_made_earlier_is_never_confirmable_after_a_new_press(make_app):
    """§8b revision 6: the page confirms only what its own press just created."""
    client = make_app().test_client()
    early = _operation(post(client, RULES, LOOSEN))  # e.g. made by an MCP server
    pressed = _operation(post(client, RULES, LOOSEN))  # the person's press
    assert pressed["id"] != early["id"] and pressed["confirm_token"] != early["confirm_token"]
    assert runtime.desk().operations.status(early["id"])["state"] == "cancelled"
    assert confirm(client, early).get_json()["code"] == "host_operation_cancelled"
    swapped = {**pressed, "confirm_token": early["confirm_token"]}
    assert confirm(client, swapped).get_json()["code"] == "host_operation_mismatch"
    assert confirm(client, pressed).status_code == 200


def test_a_stale_target_after_the_browser_confirmation_applies_nothing(make_app):
    client = make_app().test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    assert confirm(client, operation).status_code == 200
    assert post(client, RULES, TIGHTEN_OTHER).status_code == 200  # a narrowing in between
    response = post(client, RULES, LOOSEN, op_id=operation["id"])
    assert response.get_json()["code"] == "stale_operation"
    assert "http_request" not in _ask_tools(client)


def test_with_the_host_channel_set_the_browser_cannot_confirm(make_app):
    client = make_app(host_confirm="command").test_client()
    operation = _operation(post(client, RULES, LOOSEN))
    assert operation["channel"] == "command" and "confirm_token" not in operation
    response = confirm(client, {**operation, "confirm_token": "a" * 32, "instance": "0" * 16})
    assert response.status_code == 409 and response.get_json()["code"] == "host_confirm_on_host"
    assert "http_request" not in _ask_tools(client)
    host_decide(operation["id"])  # the host still decides
    assert post(client, RULES, LOOSEN, op_id=operation["id"]).status_code == 200


def test_a_restart_forgets_the_operation(make_app):
    first = make_app()
    operation = _operation(post(first.test_client(), RULES, LOOSEN))
    second = make_app()
    response = confirm(second.test_client(), operation)
    assert response.get_json()["code"] in ("host_operation_incarnation", "host_operation_unknown")


def test_loosening_rule_save_through_the_browser(make_app):
    app = make_app()
    client = app.test_client()
    operation = _operation(post(client, "/api/config/tools", LOOSEN))
    assert operation["kind"] == "rules_loosen"
    assert "http_request" not in client.get("/api/tools/config").get_json()["ask_tools"]
    assert confirm(client, operation).status_code == 200
    assert post(client, "/api/config/tools", LOOSEN, op_id=operation["id"]).status_code == 200
    assert "http_request" in client.get("/api/tools/config").get_json()["ask_tools"]


def test_new_session_revokes_a_confirmed_operation_before_it_is_applied(make_app):
    app = make_app()
    client = app.test_client()
    operation = _operation(post(client, "/api/config/tools", LOOSEN))
    assert confirm(client, operation).status_code == 200
    reset = client.post("/api/chat/session/new", headers={"Idempotency-Key": "new-1"})
    assert reset.status_code == 200, reset.get_json()
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.get_json()["code"] == "host_operation_cancelled"
    assert "http_request" not in client.get("/api/tools/config").get_json()["ask_tools"]


# --- the YOLO request through the browser channel --------------------------------------


def _press(client, tier):
    response = client.post("/api/yolo/request", json={"tier": tier})
    assert response.status_code == 202, response.get_json()
    data = response.get_json()
    assert data["command"] is None and "confirm_token" not in data["request"]
    return data


def _confirm_request(client, data, **override):
    body = {
        "token": data["confirm"]["token"],
        "acknowledged": True,
        "instance": data["confirm"]["instance"],
        **override,
    }
    return client.post(
        f"/api/host-operations/{data['request']['id']}/confirm", json=body, headers=BROWSER
    )


def test_a_yolo_request_is_confirmed_in_the_browser_and_opens_the_window(make_app):
    app = make_app("internal")
    client = app.test_client()
    data = _press(client, "internal")
    assert data["confirm"]["exec"] is False  # phase 7: no third checkbox
    assert data["confirm"]["external"] is False and set(data["confirm"]) == {
        "token",
        "external",
        "exec",
        "instance",
    }
    unticked = _confirm_request(client, data, acknowledged=False)
    assert unticked.status_code == 400 and unticked.get_json()["code"] == "host_not_acknowledged"
    malformed = _confirm_request(client, data, instance="あ" * 16)
    assert malformed.get_json()["code"] == "host_operation_incarnation"
    assert client.get("/api/yolo/status").get_json()["request"]["state"] == "pending"
    response = _confirm_request(client, data)
    assert response.status_code == 200, response.get_json()
    status = client.get("/api/yolo/status").get_json()
    assert status["window"]["state"] in ("active", "paused") and status["offered"] is False
    assert status["request"]["state"] == "opened" and status["latched"] is False
    assert "token" not in str(status)


def test_tier_external_needs_the_second_checkbox(make_app):
    app = make_app("external")
    client = app.test_client()
    data = _press(client, "external")
    assert data["confirm"]["external"] is True
    for value in (None, False, "true", 1):
        missing = _confirm_request(client, data, external_acknowledged=value)
        assert missing.status_code == 400 and missing.get_json()["code"] == "host_not_acknowledged"
    assert client.get("/api/yolo/status").get_json()["request"]["state"] == "pending"
    response = _confirm_request(client, data, external_acknowledged=True)
    assert response.status_code == 200, response.get_json()


def test_new_session_fences_a_pending_yolo_request(make_app):
    app = make_app("internal")
    client = app.test_client()
    data = _press(client, "internal")
    reset = client.post("/api/chat/session/new", headers={"Idempotency-Key": "new-2"})
    assert reset.status_code == 200, reset.get_json()
    request = client.get("/api/yolo/status").get_json()["request"]
    assert request["state"] == "fenced" and request["reason"] == "new_session"
    assert _confirm_request(client, data).status_code == 409


# --- unit: the operation store (fake clocks) -------------------------------------------


def _ops(clock, **kw):
    return HostOperations(incarnation=secrets.token_hex(16), clock=clock, channel="browser", **kw)


def _new(ops, n=0):
    return ops.request(
        "rules_loosen",
        binding=f"{n:064x}",
        generation="b" * 64,
        summary={"source": "tools_page"},
    )


def test_browser_confirmation_expires_after_two_minutes():
    clock = Clocks()
    ops = _ops(clock)
    view = _new(ops)
    clock.tick(121)
    with pytest.raises(HostOpRefused) as refused:
        ops.confirm_in_browser(
            view["id"], token=view["confirm_token"], acknowledged=True, instance=ops.instance
        )
    assert refused.value.code == "host_operation_expired"


def test_browser_rate_limit_still_applies():
    ops = _ops(Clocks(), rate_max=3)
    for n in range(3):
        _new(ops, n)
    with pytest.raises(HostOpRefused) as refused:
        _new(ops, 9)
    assert refused.value.code == "host_rate_limited"


def test_only_builtin_values_are_evaluated_in_the_browser_confirmation():
    class Liar(str):
        def __eq__(self, other):  # pragma: no cover - must never be called
            raise AssertionError("a caller method was called")

        __hash__ = str.__hash__

        def strip(self, *a):  # pragma: no cover - must never be called
            raise AssertionError("a caller method was called")

    ops = _ops(Clocks())
    view = _new(ops)

    class Truthy(int):  # pragma: no cover - its methods must never be called
        def __bool__(self):
            raise AssertionError("a caller method was called")

    for field, value in (("token", None), ("instance", None), ("acknowledged", Truthy(1))):
        args = {"token": view["confirm_token"], "acknowledged": True, "instance": ops.instance}
        args[field] = Liar(args[field]) if value is None else value
        with pytest.raises(HostOpRefused):
            ops.confirm_in_browser(view["id"], **args)
    assert ops.status(view["id"])["state"] == "pending"


@pytest.mark.parametrize("instance", ["あ" * 16, "", "f" * 17, "f" * 100_000, "é" * 8])
def test_a_malformed_instance_never_raises_in_either_store(instance):
    """Review of #432 (minor 1), the stores themselves: refused with the ordinary code,
    never ``TypeError`` from ``compare_digest``; the operation / request stays pending."""
    ops = _ops(Clocks())
    view = _new(ops)
    with pytest.raises(HostOpRefused) as refused:
        ops.confirm_in_browser(
            view["id"], token=view["confirm_token"], acknowledged=True, instance=instance
        )
    assert refused.value.code == "host_operation_incarnation"
    assert ops.status(view["id"])["state"] == "pending"
    yolo = YoloRequests(
        allowance="internal",
        incarnation=secrets.token_hex(16),
        record=Recorder(),
        actor="local:t",
        clocks_supported=True,
        clock=Clocks(),
        channel="browser",
    )
    with pytest.raises(HostOpRefused) as refused:
        yolo.confirm_in_browser(
            "0" * 32,
            token="0" * 32,
            acknowledged=True,
            external_acknowledged=True,
            instance=instance,
        )
    assert refused.value.code == "host_operation_incarnation"


def test_revoke_all_ends_pending_and_confirmed_operations():
    ops = HostOperations(incarnation=secrets.token_hex(16), clock=Clocks(), max_open=2)
    first = ops.request("recover", binding="1" * 64, generation="2" * 64, summary={})
    second = ops.request("recover", binding="3" * 64, generation="4" * 64, summary={})
    ops.decide(
        first["id"],
        incarnation=ops.incarnation,
        decision="confirm",
        principal="local:t",
        channel="cli",
    )
    assert ops.revoke_all() == 2
    with pytest.raises(HostOpRefused) as refused:
        ops.consume(first["id"], kind="recover", binding="1" * 64, generation="2" * 64)
    assert refused.value.code == "host_operation_cancelled"
    assert ops.status(second["id"])["state"] == "cancelled"


def test_the_yolo_approver_never_answers_an_exec_tool():
    """Premise 1 of the browser channel (§8b): exec stays out of every window that was
    not pressed with "Include exec" (phase 7). The deciders are the tool class and the
    service's tier check (``requests.approve``); there is no second copy of the rule."""
    from stiracore.tools.tool_registry import EXEC_TOOL_NAMES
    from stiracore.yolo.tiers import EXEC, tool_class

    assert EXEC_TOOL_NAMES
    for name in EXEC_TOOL_NAMES:
        assert tool_class(name) == EXEC, name
    assert tool_class("file_write") == "internal"
    assert not hasattr(runtime, "may_answer")  # the unused duplicate is gone (#441 review)


# --- review of phase 2, major 3: a latch is never rolled back by a press ----------------


class _Gated(Recorder):
    """Each event type can be held (entered / release) and made to fail."""

    def __init__(self):
        super().__init__()
        self.gates = {}

    def gate(self, event_type):
        self.gates[event_type] = (threading.Event(), threading.Event())
        return self.gates[event_type]

    def __call__(self, event_type, **fields):
        gate = self.gates.pop(event_type, None)
        if gate is not None:
            entered, release = gate
            entered.set()
            assert release.wait(5)
        return super().__call__(event_type, **fields)


def test_a_latch_set_while_a_press_writes_its_row_survives_the_press():
    audit = _Gated()
    service = YoloRequests(
        allowance="external",
        incarnation=secrets.token_hex(16),
        record=audit,
        actor="local:tester",
        clocks_supported=True,
        clock=Clocks(),
        channel="command",
    )
    first = service.press("internal")
    cancel_in, cancel_go = audit.gate("audit.yolo.request_cancelled")
    audit.fail.add("audit.yolo.request_cancelled")
    cancelling = threading.Thread(target=service.cancel, args=(first["id"],))
    cancelling.start()
    assert cancel_in.wait(5)  # 1. cancelled in memory; its row is being written
    press_in, press_go = audit.gate("audit.yolo.requested")
    outcome = {}

    def second_press():
        try:
            outcome["view"] = service.press("internal")
        except sm.Refused as error:
            outcome["refused"] = error.code

    pressing = threading.Thread(target=second_press)
    pressing.start()
    assert press_in.wait(5)  # 2. a new press built its candidate; its row is held
    cancel_go.set()  # 3. the cancel row fails -> latched
    cancelling.join(5)
    assert service.machine.latched
    press_go.set()  # 4. the press row succeeds
    pressing.join(5)
    assert service.machine.latched, "the press rolled the latch back"
    assert outcome == {"refused": "yolo_latched"}
    with pytest.raises(sm.Refused) as refused:
        service.press("internal")
    assert refused.value.code == "yolo_latched"
    assert "request_fenced" in audit.types()  # the orphan requested row is closed


# --- review of phase 2, major 1: target read, check, consume and change are one step ---


def test_a_scheduler_change_cannot_slip_between_the_target_check_and_the_resume(make_app):
    from stiracore.control.harvest_job import ACTION_NAME

    app = make_app(scheduler=True, host_confirm="command")
    scheduler = app.extensions["scheduler"]
    client = app.test_client()
    job = {
        "job_id": "nightly",
        "name": "nightly",
        "action": ACTION_NAME,
        "schedule": {"type": "interval", "every_sec": 3600, "anchor": 1_800_000_000.0},
    }
    assert (
        confirmed_post(client, "/api/scheduler/jobs", {"job": job, "confirm": True}).status_code
        == 200
    )
    assert post(client, "/api/scheduler/jobs/nightly/pause", {}).status_code == 200
    body = {"confirm": True}
    response = post(client, "/api/scheduler/jobs/nightly/resume", body)
    operation = response.get_json()["operation"]
    host_decide(operation["id"])

    order = []
    original_record = scheduler._record

    def record(kind, payload, now):
        order.append(kind)
        return original_record(kind, payload, now)

    scheduler._record = record
    snapshot, go = threading.Event(), threading.Event()
    original_jobs = scheduler.jobs
    resume_thread = {}

    def jobs():
        result = original_jobs()
        if threading.current_thread() is resume_thread.get("thread"):
            snapshot.set()  # the confirmed target was just read
            assert go.wait(5)
        return result

    scheduler.jobs = jobs
    results = {}

    def resume():
        results["resume"] = post(
            app.test_client(), "/api/scheduler/jobs/nightly/resume", body, op_id=operation["id"]
        )

    def update():
        results["update"] = scheduler.update_job(
            "nightly", {"name": "renamed"}, by="local:operator", confirm=True
        )

    worker = threading.Thread(target=resume)
    resume_thread["thread"] = worker
    worker.start()
    assert snapshot.wait(5)
    updater = threading.Thread(target=update)
    updater.start()
    updater.join(0.5)  # on main the change completes here, between check and resume
    go.set()
    worker.join(5)
    updater.join(5)
    scheduler.jobs = original_jobs
    resumed = results["resume"]
    if resumed.status_code == 200:
        # Applied only to the state that was confirmed: the change came after it.
        assert order.index("job_resumed") < order.index("job_updated"), order
    else:
        assert resumed.get_json()["code"] == "stale_operation"
        assert "job_resumed" not in order
    assert HEADER  # the page header name is unchanged
