"""ADR 0012 phase 7 (§9 option C, operator decision 2026-10-19 12:28): exec in a window.

The person may choose "Include exec" at the press — only with ``STIRA_ENABLE_EXEC_TOOLS``
and ``STIRA_YOLO_ALLOW=external`` — and confirms it with its own checkbox. In such a
window ``shell`` and ``batch`` (``tiers.EXEC_AUTO_TOOLS``) are answered automatically in
a human turn; ``cron`` (runs later, outside the window) and the child-agent tools
(``use_agent`` / ``think`` / ``swarm`` / ``graph``: children without Stira's hooks) stay a
person's card. Never, the floor, the breaker's write block, ``CRITICAL_HALT``, secret-like
arguments (the command included), the exec sub-cap and non-human turns are unchanged; the
exec gate mark stays bound to the call's arguments and single-use; no rule is written.

Each check is shown to be the reason: the same call passes when only that check is
removed (monkeypatched). Offline: fake clocks, the fake audit chain, a scripted model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stiracore.core import hitl_reasons
from stiracore.core.engine import HITL_APPROVED_TOOL_IDS_KEY
from stiracore.tools import tool_consent
from stiracore.tools.tool_consent import EXEC_TOOLS, GATE_PASSED_KEY, claim_gate
from stiracore.yolo import origin, runtime, tiers
from stiracore.yolo import state as sm
from stiracore.yolo.allowance import YoloSettingError, YoloSettings, parse_settings
from stiracore.yolo.audit_schema import AuditContractError, build
from stiracore.yolo.loosening import NOT_SETTABLE, window_loosening
from stiracore.yolo.tiers import EXEC_AUTO_TOOLS, EXEC_PERSON_REASONS, exec_refusal
from stiracore.yolo.window import WindowMarker
from tests.test_yolo_auto_approve import (
    AGENT,
    Breaker,
    _approve,
    _call,
    _execute,
    _gate,
    _window,
)
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.test_yolo_window import _confirm, _service

COMMAND = {"command": "printf yolo > out.txt", "work_dir": "."}
_ARGS = {
    "shell": COMMAND,
    "batch": {"invocations": [{"name": "shell", "arguments": COMMAND}]},
    "cron": {"action": "add", "schedule": "0 0 1 1 *", "command": "true"},
    "use_agent": {"prompt": "hi", "system_prompt": "child"},
    "think": {"thought": "hi", "cycle_count": 1, "system_prompt": "child"},
    "swarm": {"task": "hi", "agents": [{"name": "c", "system_prompt": "child"}]},
    "graph": {"action": "create", "graph_id": "g", "topology": {"nodes": [], "edges": []}},
}


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


@pytest.fixture
def breaker():
    return Breaker()


def _svc(audit, clock, tmp_path, breaker=None, **kw):
    kw.setdefault("exec_offered", True)
    return _service(
        audit, clock, WindowMarker(tmp_path), breaker=breaker or (lambda: "normal"), **kw
    )


@pytest.fixture
def yolo(audit, clock, tmp_path, breaker):
    return _svc(audit, clock, tmp_path, breaker)


@pytest.fixture
def human():
    with origin.human_turn(AGENT):
        yield


def _open(yolo, tier="external", with_exec=True):
    request_id = yolo.press(tier, with_exec)["id"]
    _confirm(yolo, request_id)
    assert yolo.machine.window.state == "active", yolo.machine
    assert yolo.machine.window.with_exec is with_exec
    return request_id


def _rows(audit, event_type):
    return [row["attributes"] for row in audit.rows if row["event_type"] == event_type]


# --- settings: the exec sub-cap and where the choice is offered ----------------------------


def test_the_exec_subcap_variable():
    base = {"STIRA_YOLO_ALLOW": "external"}
    assert parse_settings(base).exec_cap == 200
    assert parse_settings({**base, "STIRA_YOLO_MAX_AUTO_APPROVALS": "50"}).exec_cap == 50
    assert parse_settings({**base, "STIRA_YOLO_MAX_EXEC": "0"}).exec_cap == 0
    for bad in ("-1", "x", "10001", "1.5", " 3"):
        with pytest.raises(YoloSettingError) as error:
            parse_settings({**base, "STIRA_YOLO_MAX_EXEC": bad})
        assert "STIRA_YOLO_MAX_EXEC" in str(error.value) and bad not in str(error.value)
    with pytest.raises(YoloSettingError):  # above the cap
        parse_settings({**base, "STIRA_YOLO_MAX_AUTO_APPROVALS": "10", "STIRA_YOLO_MAX_EXEC": "11"})
    with pytest.raises(YoloSettingError):  # with the allowance off
        parse_settings({"STIRA_YOLO_ALLOW": "off", "STIRA_YOLO_MAX_EXEC": "5"})
    # Unset = the default allowance (ADR 0012 addendum 2026-10-24): the sub-cap is read.
    assert parse_settings({"STIRA_YOLO_MAX_EXEC": "5"}).exec_cap == 5


@pytest.mark.parametrize(
    ("allow", "exec_env", "offered"),
    [
        ("external", "1", True),
        ("external", "true", True),
        ("external", "", False),  # exec tools hidden: no choice
        ("external", "no", False),
        ("internal", "1", False),  # exec implies the external tier
        ("", "1", False),  # the default external never offers exec (addendum 2026-10-24)
        ("off", "1", False),  # no allowance: nothing
    ],
)
def test_the_exec_choice_is_offered_only_with_exec_tools_and_external(allow, exec_env, offered):
    settings = parse_settings({"STIRA_YOLO_ALLOW": allow, "STIRA_ENABLE_EXEC_TOOLS": exec_env})
    assert settings.exec_offered is offered


def test_the_legacy_exec_variable_turns_nothing_on():
    """Revision 3's ``STIRA_YOLO_EXEC`` is not read: it neither offers nor enables exec."""
    settings = parse_settings({"STIRA_YOLO_ALLOW": "external", "STIRA_YOLO_EXEC": "1"})
    assert settings.exec_offered is False and settings == YoloSettings("external")


# --- the press and the confirmation --------------------------------------------------------


def test_exec_is_refused_where_it_is_not_offered(audit, clock, tmp_path):
    for offered, allowance in ((False, "external"), (True, "internal")):
        svc = _svc(audit, clock, tmp_path, exec_offered=offered, allowance=allowance)
        with pytest.raises(sm.Refused) as error:
            svc.press("external" if allowance == "external" else "internal", True)
        assert error.value.code == "yolo_exec_not_allowed"
        assert svc.machine.request is None
    # The same service accepts the press without exec (nothing else changed).
    assert svc.press("internal")["exec"] is False


@pytest.mark.parametrize("value", ["true", 1, None, [], {"x": 1}])
def test_only_a_builtin_bool_is_an_exec_choice(yolo, value):
    with pytest.raises(sm.Refused) as error:
        yolo.press("external", value)
    assert error.value.code == "yolo_exec_not_allowed"


def test_exec_needs_tier_external(yolo):
    with pytest.raises(sm.Refused) as error:
        yolo.press("internal", True)
    assert error.value.code == "yolo_exec_not_allowed"


def _browser(audit, clock, tmp_path):
    svc = _svc(audit, clock, tmp_path, channel="browser")
    return svc


def test_the_browser_confirmation_of_exec_needs_its_own_checkbox(audit, clock, tmp_path):
    svc = _browser(audit, clock, tmp_path)
    view = svc.press("external", True)
    assert view["exec"] is True and view["word"] == "EXEC"
    base = {
        "token": view["confirm_token"],
        "acknowledged": True,
        "external_acknowledged": True,
        "instance": svc.instance,
    }
    from stiracore.yolo.host_ops import HostOpRefused

    for missing in (None, "true", 1, False):
        with pytest.raises(HostOpRefused) as error:
            svc.confirm_in_browser(view["id"], **base, exec_acknowledged=missing)
        assert error.value.code == "host_not_acknowledged"
        assert svc.machine.request.state == "pending"  # not a denial: still open
    svc.confirm_in_browser(view["id"], **base, exec_acknowledged=True)
    window = svc.status()["window"]
    assert window["exec"] is True and window["exec_limit"] == 200 and window["tier"] == "external"


def test_without_exec_no_third_checkbox_is_needed(audit, clock, tmp_path):
    svc = _browser(audit, clock, tmp_path)
    view = svc.press("external")
    assert view["exec"] is False and view["word"] == "YOLO"
    svc.confirm_in_browser(
        view["id"],
        token=view["confirm_token"],
        acknowledged=True,
        external_acknowledged=True,
        instance=svc.instance,
    )
    window = svc.status()["window"]
    assert window["exec"] is False and window["exec_limit"] == 0


def test_the_host_text_names_exec():
    from stiracore.yolo.host_channel import describe

    base = {"kind": "yolo_request", "short": "0123abcd", "tier": "external", "t0": 1.0}
    plain = describe({**base, "exec": False})
    with_exec = describe({**base, "exec": True})
    assert not any("exec" in line for line in plain)
    [line] = [line for line in with_exec if "includes exec" in line]
    assert "実行系も含む" in line and "bypass" in line


def test_the_host_command_asks_for_the_word_exec():
    from tests.test_yolo_host_channel import Transport, _desk, _run

    desk = _desk()
    desk.yolo.exec_offered = True
    request = desk.yolo.press("external", True)
    # The server names the word (the command only types what it was told).
    shown = Transport(desk)({"cmd": "show", "id": request["short"]})
    assert shown["ok"] is True and shown["item"]["word"] == "EXEC"
    plain = _desk()
    plain_request = plain.yolo.press("external")
    assert Transport(plain)({"cmd": "show", "id": plain_request["short"]})["item"]["word"] == "YOLO"
    # "YOLO" fails because it is not the word EXEC: nothing is decided, the request stays.
    code, out, _err, transport = _run(["yolo", "confirm", request["short"]], desk, typed="YOLO")
    assert code == 1 and [m["cmd"] for m in transport.sent] == ["show"]  # nothing decided
    assert "not confirmed (nothing was decided" in out and "includes exec" in out
    assert desk.yolo.machine.request.state == "pending"
    code, out, _err, transport = _run(["yolo", "confirm", request["short"]], desk, typed="EXEC")
    assert code == 0 and transport.sent[-1]["decision"] == "confirm"


# --- the class table: every exec tool × window ----------------------------------------------


@pytest.mark.parametrize("name", sorted(EXEC_TOOLS))
@pytest.mark.parametrize(
    ("tier", "with_exec"), [("internal", False), ("external", False), ("external", True)]
)
def test_every_exec_tool_in_every_window(audit, clock, tmp_path, human, name, tier, with_exec):
    yolo = _svc(audit, clock, tmp_path)
    _open(yolo, tier, with_exec)
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, name, json.loads(json.dumps(_ARGS[name])))
    automatic = with_exec and name in EXEC_AUTO_TOOLS
    if automatic:
        assert event.cancel_tool is None and asked == [], name
        _execute(hooks, event)
        assert event.cancel_tool is None, event.cancel_tool
        # The exec gate mark exists, for exactly this call and these arguments.
        mark = event.invocation_state[GATE_PASSED_KEY][event.tool_use["toolUseId"]]
        assert mark.tool_name == name
        window = _window(yolo)
        assert (window["used"], window["exec_used"], window["external_used"]) == (1, 1, 0)
    else:
        assert [n for n, _ in asked] == [name], (name, tier, with_exec)
        assert event.cancel_tool.startswith("HITL rejected")
        assert _window(yolo)["auto_approved"] == 0


def test_the_exec_class_table():
    assert EXEC_AUTO_TOOLS == {"shell", "batch"}
    assert EXEC_PERSON_REASONS == {
        "cron": "exec_runs_later",
        "use_agent": "exec_child_ungated",
        "think": "exec_child_ungated",
        "swarm": "exec_child_ungated",
        "graph": "exec_child_ungated",
    }
    assert EXEC_AUTO_TOOLS | set(EXEC_PERSON_REASONS) == EXEC_TOOLS
    for name, reason in EXEC_PERSON_REASONS.items():
        assert exec_refusal(name, _ARGS[name]) == reason
    assert exec_refusal("diagram", {}) == "scope_unknown"  # not exec: a person-tool


@pytest.mark.parametrize("name", sorted(EXEC_PERSON_REASONS))
def test_removing_the_exclusion_would_approve_it(audit, clock, tmp_path, human, monkeypatch, name):
    """The exclusion is what keeps cron and the child-agent tools a card."""
    yolo = _svc(audit, clock, tmp_path)
    _open(yolo)
    monkeypatch.setattr(tiers, "EXEC_AUTO_TOOLS", EXEC_TOOLS)
    from stiracore.yolo import requests as yolo_requests

    monkeypatch.setattr(yolo_requests, "EXEC_AUTO_TOOLS", EXEC_TOOLS)
    grant, code = _approve(yolo, name)
    assert grant is not None and code == "approved", name


def test_propose_procedure_stays_a_person_in_the_widest_window(audit, clock, tmp_path, human):
    """#463 (maintainer decision): a window of tier external with exec answers shell /
    batch, never ``propose_procedure`` — the person sees what the agent wants to learn."""
    yolo = _svc(audit, clock, tmp_path)
    _open(yolo)
    assert _approve(yolo, "propose_procedure") == (None, "tier_not_covered")
    assert _approve(yolo, "shell")[1] == "approved"  # control: the window does answer


def test_removing_the_procedure_exclusion_would_approve_it(
    audit, clock, tmp_path, human, monkeypatch
):
    """The PERSON_TOOLS entry is what keeps it a card: as an internal tool it would pass."""
    yolo = _svc(audit, clock, tmp_path)
    _open(yolo)
    monkeypatch.setattr(tiers, "PERSON_TOOLS", tiers.PERSON_TOOLS - {"propose_procedure"})
    monkeypatch.setattr(tiers, "INTERNAL_TOOLS", tiers.INTERNAL_TOOLS | {"propose_procedure"})
    grant, code = _approve(yolo, "propose_procedure")
    assert grant is not None and code == "approved"


def test_removing_the_window_flag_check_would_approve_exec(
    audit, clock, tmp_path, human, monkeypatch
):
    yolo = _svc(audit, clock, tmp_path)
    _open(yolo, "external", False)
    yolo._win["exec_cap"] = 200  # slots alone do not admit exec: the flag does
    assert _approve(yolo, "shell") == (None, "tier_not_covered")
    window = yolo.machine.window
    yolo.machine = sm.replace(yolo.machine, window=sm.replace(window, with_exec=True))
    grant, code = _approve(yolo, "shell")
    assert code == "approved" and grant.exec_class is True


# --- the invariants: Never, floor, breaker, CRITICAL, secrets, origin -------------------------


def test_never_and_the_floor_stay_for_shell(yolo, human):
    _open(yolo)
    floor = SimpleNamespace(reason="SOUL.md is protected")
    hooks, _engine, asked = _gate(
        yolo,
        rules={"batch": "never"},
        path_check=lambda name, args: floor if "SOUL" in json.dumps(args) else None,
    )
    never = _call(hooks, "batch", dict(_ARGS["batch"]))
    assert never.cancel_tool.startswith("Tool policy is Never")
    floored = _call(hooks, "shell", {"command": "echo x > SOUL.md"})
    assert floored.cancel_tool.startswith("SOUL.md is protected")
    assert asked == [] and _window(yolo)["auto_approved"] == 0


def test_the_breakers_write_block_and_blocked_tools_stay_for_shell(yolo, human):
    _open(yolo)
    hooks, engine, asked = _gate(yolo)
    engine.config.blocked_tool_names = frozenset({"shell", "batch"})
    engine.state.stability = 0.4  # WARNING: write / exec / blocked tools refused
    engine.pre_turn()
    for name in ("shell", "batch"):
        event = _call(hooks, name, json.loads(json.dumps(_ARGS[name])))
        assert event.cancel_tool.startswith("Writes are blocked"), name
    assert asked == [] and _window(yolo)["auto_approved"] == 0


def test_critical_halt_stays_a_person_for_shell(yolo, human):
    _open(yolo)
    hooks, engine, asked = _gate(yolo)
    engine.config.critical_action = "halt"
    engine.state.stability = 0.1
    engine.pre_turn()
    event = _call(hooks, "shell", dict(COMMAND))
    assert asked == [("shell", hitl_reasons.CRITICAL_HALT)]
    assert event.cancel_tool.startswith("HITL rejected")
    assert _window(yolo)["auto_approved"] == 0


@pytest.mark.parametrize(
    "command",
    [
        "export API_KEY=abcd1234efgh5678 && make",
        'curl -H "Authorization: Bearer abcdefghijklmnop1234" https://example.org',
        "curl https://example.org/?api_key=zzzzzzzz",
        "TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 make",
    ],
)
def test_a_command_with_a_secret_is_a_card(yolo, human, command, monkeypatch):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "shell", {"command": command})
    assert [n for n, _ in asked] == ["shell"] and event.cancel_tool.startswith("HITL rejected")
    assert exec_refusal("shell", {"command": command}) == "secret_in_args"
    # The secret check is the reason: without it the same call is approved.
    monkeypatch.setattr(tiers, "mask_secrets", lambda value: value)
    assert exec_refusal("shell", {"command": command}) is None


def test_a_secret_inside_a_batch_is_a_card():
    batch = {
        "invocations": [{"name": "shell", "arguments": {"command": "export TOKEN=abcdefgh1234"}}]
    }
    assert exec_refusal("batch", batch) == "secret_in_args"


@pytest.mark.parametrize("value", [("ls",), b"ls", float("nan")])
def test_arguments_that_are_not_plain_json_are_a_card(value):
    assert exec_refusal("shell", {"command": value}) == "args_unknown"


def test_a_non_human_turn_is_a_card(yolo):
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "shell", dict(COMMAND))  # no human-turn mark (a job, a webhook…)
    assert [n for n, _ in asked] == ["shell"] and event.cancel_tool.startswith("HITL rejected")
    with origin.human_turn("another-agent"):
        assert _approve(yolo, "shell", agent_id=AGENT)[1] == "origin_not_covered"


def test_no_always_and_no_human_approval_mark(yolo, human):
    """An exec automatic approval is not a human approval (IdentityGuard's write block)
    and writes no rule: the rule reader answers Ask before and after."""
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    before = hooks.policy_check("shell")
    event = _call(hooks, "shell", dict(COMMAND))
    _execute(hooks, event)
    assert event.cancel_tool is None
    assert event.tool_use["toolUseId"] not in event.invocation_state.get(
        HITL_APPROVED_TOOL_IDS_KEY, set()
    )
    assert hooks.policy_check("shell") == before == ("ask", 7)


def test_the_exec_gate_mark_is_bound_to_the_arguments_and_single_use(yolo, human):
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "shell", dict(COMMAND))
    _execute(hooks, event)
    state, tool_use_id = event.invocation_state, event.tool_use["toolUseId"]
    # Other arguments than the approved ones: refused (and the mark is gone).
    changed = claim_gate("shell", state, tool_use_id, {"command": "rm -rf ~", "work_dir": "."})
    assert isinstance(changed, str) and "arguments changed" in changed
    # A second claim after a consumed mark: refused.
    event2 = _call(hooks, "shell", dict(COMMAND))
    _execute(hooks, event2)
    first = claim_gate("shell", event2.invocation_state, event2.tool_use["toolUseId"], COMMAND)
    again = claim_gate("shell", event2.invocation_state, event2.tool_use["toolUseId"], COMMAND)
    assert isinstance(first, tool_consent.GateMark) and isinstance(again, str)


def test_a_non_exec_grant_never_opens_the_exec_gate(yolo, human):
    """Defence in depth (#362 kept): a ``file_write`` approval smuggled into a shell
    call does not count; only an exec approval of the very tool does."""
    from stiracore.agent.checkpoint_hooks import YOLO_GRANTS_KEY
    from stiracore.core.approval import HITL_BINDINGS_KEY, ApprovalBinding, args_hash

    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    for smuggled in ("file_write", "batch"):
        grant, _ = _approve(yolo, smuggled)
        tool_use_id = "tu-" + smuggled
        binding = ApprovalBinding(
            request_id="yolo-" + grant.intent,
            agent_id=hooks.engine.agent_id,
            tool_name="shell",
            args_hash=args_hash(COMMAND),
            expires_at=float("inf"),
        )
        event = SimpleNamespace(
            tool_use={"name": "shell", "toolUseId": tool_use_id, "input": dict(COMMAND)},
            cancel_tool=None,
            invocation_state={
                HITL_BINDINGS_KEY: {tool_use_id: binding},
                YOLO_GRANTS_KEY: {tool_use_id: grant},
                "stira.hitl.policy_generations": {tool_use_id: ("ask", 7)},
            },
        )
        assert hooks.binding_verifier._verify_binding(event) is False, smuggled


def test_an_exec_approval_is_a_human_approval_nowhere_else(yolo, human, monkeypatch, tmp_path):
    """Review of #441 M2: the verifier's answer is ``exec_mark_allowed`` — it opens the
    exec gate mark of this one call and nothing else. IdentityGuard's breaker exception
    reads ``HITL_APPROVED_TOOL_IDS_KEY`` (a person's answer only); the file tools' marks
    do not read the flag at all; a smuggled exec grant opens no other exec tool."""
    import inspect

    from stiracore.agent.checkpoint_hooks import YOLO_GRANTS_KEY, ApprovalBindingVerifier
    from stiracore.core.approval import HITL_BINDINGS_KEY, ApprovalBinding, args_hash
    from stiracore.middleware.identity_guard import IdentityGuard

    params = inspect.signature(ApprovalBindingVerifier._issue_gate_mark).parameters
    assert "human_approved" not in params
    assert params["exec_mark_allowed"].kind is inspect.Parameter.KEYWORD_ONLY
    _open(yolo)
    hooks, engine, _asked = _gate(yolo)
    event = _call(hooks, "shell", dict(COMMAND))
    _execute(hooks, event)
    assert event.cancel_tool is None  # the exec mark was issued
    tool_use_id = event.tool_use["toolUseId"]
    assert tool_use_id not in event.invocation_state.get(HITL_APPROVED_TOOL_IDS_KEY, set())
    # IdentityGuard under a write block: the exec approval does not lift it, a person's does.
    guard = IdentityGuard(engine)
    monkeypatch.setattr(engine, "is_write_blocked", lambda: True)
    elsewhere = {**event.tool_use, "input": {"command": "true", "work_dir": str(tmp_path)}}
    blocked = SimpleNamespace(
        tool_use=elsewhere, cancel_tool=None, invocation_state=event.invocation_state
    )
    guard.on_before_tool_call(blocked)
    assert blocked.cancel_tool and "blocked" in blocked.cancel_tool.lower()
    person = SimpleNamespace(
        tool_use=dict(elsewhere),
        cancel_tool=None,
        invocation_state={HITL_APPROVED_TOOL_IDS_KEY: {tool_use_id}},
    )
    guard.on_before_tool_call(person)
    assert person.cancel_tool is None
    monkeypatch.undo()
    # The file tools' marks: the flag changes nothing for them (True and False alike).
    marks = []
    for allowed in (False, True):
        call = SimpleNamespace(
            tool_use={"name": "file_write", "toolUseId": f"fw-{allowed}", "input": {"path": "a"}},
            cancel_tool=None,
            invocation_state={},
        )
        hooks.binding_verifier._issue_gate_mark(call, exec_mark_allowed=allowed)
        marks.append((call.cancel_tool, sorted(call.invocation_state.get(GATE_PASSED_KEY, {}))))
    assert marks == [(None, ["fw-False"]), (None, ["fw-True"])]
    # A real exec grant for ``shell`` smuggled into a ``cron`` call: no mark, cancelled.
    grant, _ = _approve(yolo, "shell")
    cron_input = dict(_ARGS["cron"])
    cron = SimpleNamespace(
        tool_use={"name": "cron", "toolUseId": "tu-cron", "input": cron_input},
        cancel_tool=None,
        invocation_state={
            HITL_BINDINGS_KEY: {
                "tu-cron": ApprovalBinding(
                    request_id="yolo-" + grant.intent,
                    agent_id=engine.agent_id,
                    tool_name="cron",
                    args_hash=args_hash(cron_input),
                    expires_at=float("inf"),
                )
            },
            YOLO_GRANTS_KEY: {"tu-cron": grant},
            "stira.hitl.policy_generations": {"tu-cron": ("ask", 7)},
        },
    )
    hooks.binding_verifier.before_tool(cron)
    assert "exec-class tools run only after a human approved" in cron.cancel_tool
    assert "tu-cron" not in cron.invocation_state.get(GATE_PASSED_KEY, {})


# --- the exec sub-cap ----------------------------------------------------------------------


def test_the_exec_subcap_used_up_asks_again_and_the_window_goes_on(audit, clock, tmp_path, human):
    yolo = _svc(audit, clock, tmp_path, exec_cap=1)
    _open(yolo)
    hooks, _engine, asked = _gate(yolo)
    first = _call(hooks, "shell", dict(COMMAND))
    _execute(hooks, first)
    assert first.cancel_tool is None
    second = _call(hooks, "shell", dict(COMMAND))
    assert [n for n, _ in asked] == ["shell"] and second.cancel_tool.startswith("HITL rejected")
    assert _approve(yolo, "shell")[1] == "exec_cap_used"
    # Internal tools go on; the window is active.
    assert _approve(yolo, "file_write")[1] == "approved"
    window = _window(yolo)
    assert window["state"] == "active" and window["exec_used"] == 1
    assert window["exec_effective"] is False


def test_the_exec_subcap_temporarily_full(audit, clock, tmp_path, human):
    yolo = _svc(audit, clock, tmp_path, exec_cap=1)
    _open(yolo)
    held, _ = _approve(yolo, "shell")
    assert held is not None
    assert _approve(yolo, "batch")[1] == "exec_cap_full"
    assert yolo.void(held)  # the slot comes back
    assert _approve(yolo, "batch")[1] == "approved"


def test_exec_subcap_zero_opens_the_window_and_every_command_asks(audit, clock, tmp_path, human):
    yolo = _svc(audit, clock, tmp_path, exec_cap=0)
    _open(yolo)
    assert _window(yolo)["exec_limit"] == 0
    assert _approve(yolo, "shell")[1] == "exec_cap_used"
    assert _approve(yolo, "web_search")[1] == "approved"


def test_an_exec_approval_holds_a_cap_slot_and_not_an_external_one(audit, clock, tmp_path, human):
    yolo = _svc(audit, clock, tmp_path, cap=2, external_cap=0)
    _open(yolo)
    for _ in range(2):
        grant, code = _approve(yolo, "shell")
        assert code == "approved"
        assert yolo.consume(grant)[0] is True
    status = yolo.status()
    assert status["window"] is None and status["last_end"]["reason"] == "expired_cap"


# --- rows, status, log line: never the command ---------------------------------------------


def test_the_rows_name_exec_and_never_the_command(yolo, audit, human, caplog):
    import logging

    caplog.set_level(logging.INFO)
    _open(yolo)
    hooks, _engine, _asked = _gate(yolo)
    event = _call(hooks, "shell", {"command": "printf unique-command-text"})
    _execute(hooks, event)
    [requested] = _rows(audit, "audit.yolo.requested")
    [confirmed] = _rows(audit, "audit.yolo.confirmed")
    [started] = _rows(audit, "audit.yolo.started")
    assert requested["exec"] is confirmed["exec"] is started["exec"] is True
    assert started["exec_cap"] == 200
    [row] = _rows(audit, "audit.yolo.auto_approved")
    assert (row["operation"], row["tool_class"], row["exec_slot"], row["exec_limit"]) == (
        "shell",
        "exec",
        1,
        200,
    )
    assert row["tier"] == "external" and row["external"] is False
    assert "destination_ref" not in row
    yolo.fence("stop")
    [ended] = _rows(audit, "audit.yolo.ended")
    assert ended["exec_used"] == 1
    text = json.dumps([r["attributes"] for r in audit.rows]) + caplog.text
    assert "unique-command-text" not in text and "printf" not in text
    assert "Auto-approved (YOLO exec): shell #" in caplog.text


def test_the_log_line_and_status(yolo, human):
    _open(yolo)
    grant, _ = _approve(yolo, "shell")
    yolo.consume(grant)
    status = yolo.status()
    [line] = status["auto_approvals"]["items"]
    assert line == {
        "n": 1,
        "tool": "shell",
        "ref": grant.intent[:4],
        "limit": 2000,
        "tier": "exec",
        "exec_n": 1,
        "exec_limit": 200,
    }
    window = status["window"]
    assert (window["exec"], window["exec_used"], window["exec_reserved"]) == (True, 1, 0)
    assert window["exec_effective"] is True


def test_an_internal_row_names_its_class_and_no_exec_slot(yolo, audit, human):
    _open(yolo, "external", False)
    _approve(yolo, "file_write")
    [row] = _rows(audit, "audit.yolo.auto_approved")
    assert row["tool_class"] == "internal" and "exec_slot" not in row


def test_the_schema_refuses_exec_rows_that_do_not_fit(yolo, audit, human):
    _open(yolo)
    _approve(yolo, "shell")
    [row] = [r for r in audit.rows if r["event_type"] == "audit.yolo.auto_approved"]
    fields = dict(row["attributes"], event_id=row["event_id"])
    build("audit.yolo.auto_approved", **fields)  # as written: valid
    without = {k: v for k, v in fields.items() if k != "exec_slot"}
    with pytest.raises(AuditContractError):
        build("audit.yolo.auto_approved", **without)
    [requested] = [r for r in audit.rows if r["event_type"] == "audit.yolo.requested"]
    internal = dict(requested["attributes"], event_id=requested["event_id"], tier="internal")
    with pytest.raises(AuditContractError):
        build("audit.yolo.requested", **internal)


# --- the one loosening judgement ------------------------------------------------------------


def _snap(**kw):
    base = {
        "yolo": "external",
        "state": "active",
        "cap": 2000,
        "expires_at": 100.0,
        "grant_id": "a" * 32,
        "incarnation": "b" * 32,
    }
    return {**base, **kw}


def test_exec_is_allowed_only_at_the_issue_and_only_with_external():
    issue = window_loosening(
        {"yolo": "off"}, _snap(exec=True), "host_confirmed_request", allowance="external"
    )
    assert issue == ["yolo", "yolo_exec"]
    internal = window_loosening(
        {"yolo": "off"},
        _snap(yolo="internal", exec=True),
        "host_confirmed_request",
        allowance="external",
    )
    assert NOT_SETTABLE in internal
    for context in ("breaker_resume", "host_confirmed_request", "yolo_narrowing"):
        gained = window_loosening(_snap(), _snap(exec=True), context, allowance="external")
        assert NOT_SETTABLE in gained, context
    assert window_loosening(_snap(exec=True), _snap(), "yolo_narrowing") == []
    odd = window_loosening(
        {"yolo": "off"}, _snap(exec="yes"), "host_confirmed_request", allowance="external"
    )
    assert NOT_SETTABLE in odd


@pytest.mark.parametrize("end", ["expired_time", "revoked_stop", "restart"])
def test_shell_is_a_card_right_after_an_exec_window_ends(audit, clock, tmp_path, human, end):
    """Review of #441 (minor): the end of an exec window takes the exec answer with it —
    the next ``shell`` is ``no_window`` (and a card at the gate), and a window opened
    afterwards without "Include exec" answers it ``tier_not_covered``."""
    from stiracore.yolo.requests import WINDOW_SEC

    yolo = _svc(audit, clock, tmp_path)
    _open(yolo)
    held, code = _approve(yolo, "shell")
    assert code == "approved"
    if end == "expired_time":
        clock.tick(WINDOW_SEC)
    elif end == "revoked_stop":
        yolo.fence("stop")
    else:  # a new server instance on the same data directory
        yolo = _svc(audit, clock, tmp_path)
    assert yolo.consume(held)[0] is False  # an approval issued before the end is void
    assert _approve(yolo, "shell") == (None, "no_window")
    assert _approve(yolo, "batch") == (None, "no_window")
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "shell", dict(COMMAND))
    assert [n for n, _ in asked] == ["shell"] and event.cancel_tool.startswith("HITL rejected")
    if end == "restart":
        # The window had no end row: the new instance is latched (§8a), no new window.
        assert yolo.status()["latched"] is True
        return
    _open(yolo, "external", False)
    assert _approve(yolo, "shell") == (None, "tier_not_covered")
    assert _approve(yolo, "web_search")[1] == "approved"  # the new window itself works


def test_resume_keeps_the_exec_flag(yolo, breaker, clock, human):
    from stiracore.yolo.requests import DWELL_SEC

    _open(yolo)
    breaker.level = "warning"
    yolo.tick()
    breaker.level = "normal"
    yolo.tick()
    clock.tick(DWELL_SEC + 1)
    yolo.tick()
    window = _window(yolo)
    assert window["state"] == "active" and window["exec"] is True
    assert _approve(yolo, "shell")[1] == "approved"


# --- runtime --------------------------------------------------------------------------------


def test_status_names_the_exec_choice(tmp_path):
    for settings, offered in (
        (YoloSettings("external", exec_tools=True), True),
        (YoloSettings("external"), False),
        (YoloSettings("internal", exec_tools=True), False),
    ):
        runtime._reset_for_tests(settings)
        try:
            status = runtime.status()
            assert status["exec_offered"] is offered
            assert status["caps"]["exec"] == 200
        finally:
            runtime._reset_for_tests()
    runtime._reset_for_tests(YoloSettings())
    try:
        assert runtime.status()["exec_offered"] is False and runtime.status()["caps"] is None
    finally:
        runtime._reset_for_tests()


def test_install_host_passes_the_exec_settings(tmp_path):
    runtime._reset_for_tests(YoloSettings("external", exec_cap=7, exec_tools=True))
    try:
        host = runtime.install_host(
            data_dir=tmp_path, record=None, clocks_supported=True, watchdog=False
        )
        assert host.yolo.exec_offered is True and host.yolo.exec_cap == 7
    finally:
        runtime._reset_for_tests()
    runtime._reset_for_tests(YoloSettings("external"))
    try:
        host = runtime.install_host(
            data_dir=tmp_path, record=None, clocks_supported=True, watchdog=False
        )
        assert host.yolo.exec_offered is False
    finally:
        runtime._reset_for_tests()
