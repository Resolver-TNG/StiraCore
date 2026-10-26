"""ADR 0019 phases 0-1 (#167): settings, discovery, plan and binding, the Always guard (Q3),
registration, defaults, YOLO, doctor, log and audit. The real runs are in
``test_run_checks_containment.py``."""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.diagnostics import doctor
from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.observability import audit_chain
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools import file_tools, tool_policy, tool_profiles, tool_registry
from stiracore.tools import run_checks as rc
from stiracore.tools.tool_config import ToolConfig
from stiracore.yolo import tiers
from stiracore.yolo.audit_schema import KNOWN_TOOLS

SANDBOXED = rc.Containment(rc.OS_SANDBOX, "run_checks_ready", "test")
LIMITS = rc.Containment(rc.LIMITS_ONLY, "run_checks_limits_only", "test host")


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.setitem(rc._HOST, "status", SANDBOXED)
    monkeypatch.setitem(rc._HOST, "enabled", True)
    rc._TURN.update(provider=None, id=None, calls=0, last=None, same=0)
    yield
    rc._TURN.update(provider=None, id=None, calls=0, last=None, same=0)


def _ws(tmp_path: Path, files: dict[str, str]) -> WritePathPolicy:
    workspace = (tmp_path / "ws").resolve()
    for name, body in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    workspace.mkdir(exist_ok=True)
    return WritePathPolicy(base_dir=workspace, write_roots=[WORKSPACE_ROOT])


# --- settings and platform --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "env", "enabled", "problem"),
    [
        (None, {}, False, None),
        ({}, {}, False, None),
        ({"phase": 1}, {}, True, None),
        ({"phase": 1}, {"STIRA_RUN_CHECKS": "0"}, False, "env_off"),
        ({"phase": 1}, {"STIRA_RUN_CHECKS": "off"}, False, "env_off"),
        ({"phase": 2}, {}, False, "phase_not_accepted"),
        ({"phase": True}, {}, False, "phase_not_accepted"),
        ({"phase": 1, "declared": []}, {}, False, "unknown_keys"),
        ([1], {}, False, "not_an_object"),
    ],
)
def test_settings_are_off_unless_phase_1(section, env, enabled, problem):
    settings = rc.settings_from(section, env)
    assert (settings.enabled, settings.problem) == (enabled, problem)


def test_platform_codes():
    assert rc.platform_code("win32", 1000) == "run_checks_platform"
    assert rc.platform_code("darwin", 0) == "run_checks_root"
    assert rc.platform_code("linux", 1000) is None


def test_linux_is_limits_only():
    status = rc.containment(platform="linux")
    assert (status.mode, status.code) == (rc.LIMITS_ONLY, "run_checks_limits_only")


# --- registration and defaults ---------------------------------------------------------------


def test_absent_section_registers_nothing_and_on_registers_the_tool():
    try:
        assert tool_registry.enable_run_checks(rc.settings_from(None)) == "run_checks_off"
        assert "run_checks" not in tool_registry.WRITE_TOOLS and not rc.is_enabled()
        code = tool_registry.enable_run_checks(rc.settings_from({"phase": 1}))
        expected = rc.platform_code() or "run_checks_on"
        assert code == expected
        assert ("run_checks" in tool_registry.WRITE_TOOLS) is (expected == "run_checks_on")
        policy = WritePathPolicy(base_dir=Path.cwd(), write_roots=[WORKSPACE_ROOT])
        names = {
            getattr(t, "tool_name", None) for t in tool_registry.get_tools(write_policy=policy)
        }
        assert ("run_checks" in names) is (expected == "run_checks_on")
        # Without a workspace policy it is not offered (fail closed).
        names = {getattr(t, "tool_name", None) for t in tool_registry.get_tools()}
        assert "run_checks" not in names
        # Hidden at WARNING and above with the other write tools.
        names = {
            getattr(t, "tool_name", None)
            for t in tool_registry.get_tools("warning", write_policy=policy)
        }
        assert "run_checks" not in names
    finally:
        tool_registry.enable_run_checks(rc.settings_from(None))


def test_new_install_asks_and_an_existing_custom_file_says_never(tmp_path):
    try:
        tool_registry.enable_run_checks(rc.settings_from({"phase": 1}))
        if "run_checks" not in tool_registry.WRITE_TOOLS:
            pytest.skip("run_checks is not offered on this platform/user")
        fresh = ToolConfig.first_run_defaults(tmp_path / "tool-config.json")
        assert fresh.policy_of("run_checks") == "ask"
        existing = ToolConfig(mode="custom", enabled_tools={"file_read"}, ask_tools=set())
        assert existing.policy_of("run_checks") == "never"
    finally:
        tool_registry.enable_run_checks(rc.settings_from(None))


def test_profile_table_minimal_never_and_no_profile_always():
    assert tool_profiles.PROFILE_TABLES["minimal"]["run_checks"] == "never"
    assert {tool_profiles.PROFILE_TABLES[p]["run_checks"] for p in ("dev", "research")} == {"ask"}


def test_yolo_keeps_it_a_card_in_every_window():
    assert "run_checks" in tiers.PERSON_TOOLS
    assert tiers.tool_class("run_checks") == tiers.EXEC
    assert "run_checks" in KNOWN_TOOLS


# --- the Always guard (Q3) ------------------------------------------------------------------


def _always_rules():
    return ToolConfig(mode="custom", enabled_tools={"run_checks"}, ask_tools=set())


def _assert_guard(monkeypatch) -> None:
    rules = _always_rules()
    monkeypatch.setitem(rc._HOST, "status", SANDBOXED)
    assert rules.policy_of("run_checks") == "always"
    monkeypatch.setitem(rc._HOST, "status", LIMITS)
    assert rules.policy_of("run_checks") == "ask"  # capped
    assert rules.policy_of("run_checks", effective=False) == "always"  # as saved
    row = tool_policy.tool_stack(rules, "run_checks", "normal", exposed=True)
    assert row["policy"] == "always" and row["always_capped"] is True
    assert row["stack"]["rule"] == {"decision": "prompt", "reason": "rule_always_capped"}
    assert row["grant"] is None and row["always_allowed"] is True


def test_always_is_effective_only_on_a_verified_sandbox(monkeypatch):
    _assert_guard(monkeypatch)


def test_mutation_drop_always_cap(monkeypatch):
    monkeypatch.setattr(rc, "always_effective", lambda: True)
    with pytest.raises(AssertionError):
        _assert_guard(monkeypatch)


def test_always_is_capped_when_the_feature_is_off_and_after_repeated_failure(monkeypatch):
    rules = _always_rules()
    monkeypatch.setitem(rc._HOST, "enabled", False)
    assert rules.policy_of("run_checks") == "ask"
    monkeypatch.setitem(rc._HOST, "enabled", True)
    for _ in range(rc.REPEATED_FAILURE_RUNS):
        rc._note_outcome(("pytest:t::x",))
    assert rules.policy_of("run_checks") == "ask" and rc.always_cap_reason() == "repeated_failure"
    rc._note_outcome(None)
    assert rules.policy_of("run_checks") == "always"


def test_saving_always_is_allowed_and_recorded_as_a_loosening(monkeypatch):
    monkeypatch.setitem(rc._HOST, "status", LIMITS)
    assert tool_policy.always_allowed("run_checks")
    before = ToolConfig(mode="custom", enabled_tools={"run_checks"}, ask_tools={"run_checks"})
    from stiracore.tools.tool_config import rules_loosening

    assert "tools" in rules_loosening(before.as_dict(), _always_rules().as_dict())


# --- discovery (phase 0: nothing executed) and the plan ---------------------------------


def test_discovery_starts_no_process(tmp_path, monkeypatch):
    policy = _ws(
        tmp_path,
        {
            "p/tests/test_a.py": "def test_a(): pass\n",
            "p/a.test.js": "",
            "p/mod.py": "x = 1\n",
            "p/node_modules/x/test_ignored.py": "",
        },
    )

    def boom(*_a, **_k):
        raise AssertionError("a process was started during discovery")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    found = rc.discover(policy.workspace / "p", policy.workspace)
    assert {"pytest", "unittest", "compileall"} <= set(found.available)
    described = rc.describe(policy.workspace / "p", policy.workspace)
    assert {row["kind"] for row in described["checks"]} >= {"pytest", "compileall"}
    plan = rc.build_plan({"path": "p"}, policy)
    assert "unittest" not in [k for k, _ in plan.checks]  # auto prefers pytest


def test_empty_folder_has_no_checks(tmp_path):
    policy = _ws(tmp_path, {"p/readme.txt": "hi"})
    with pytest.raises(rc.Refused) as refused:
        rc.build_plan({"path": "p"}, policy)
    assert refused.value.code == "no_checks"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"kinds": "curl"}, "unknown_kind"),
        ({"kinds": [1]}, "invalid_kinds"),
        ({"kinds": []}, "invalid_kinds"),
        ({"path": "../outside"}, "outside_workspace"),
        ({"path": "/etc"}, "outside_workspace"),
        ({"path": "~"}, "outside_workspace"),
        ({"path": "p/mod.py"}, "not_a_directory"),
        ({"path": "p", "targets": ["-p", "evil"]}, "invalid_target"),
        ({"path": "p", "targets": ["/etc/passwd"]}, "invalid_target"),
        ({"path": "p", "targets": ["p/missing.py"]}, "target_missing"),
        ({"path": "p", "targets": ["q/x.py"]}, "outside_workspace"),
        ({"path": "p", "kinds": ["node_test"]}, "kind_unavailable"),
    ],
)
def test_refusals_before_anything_runs(tmp_path, arguments, code):
    policy = _ws(
        tmp_path, {"p/tests/test_a.py": "def test_a(): pass\n", "p/mod.py": "", "q/x.py": ""}
    )
    (policy.workspace / "outside").mkdir(exist_ok=True)
    with pytest.raises(rc.Refused) as refused:
        rc.build_plan(arguments, policy)
    assert refused.value.code == code


def test_the_harness_builds_every_argv(tmp_path):
    policy = _ws(tmp_path, {"p/tests/test_a.py": "def test_a(): pass\n"})
    plan = rc.build_plan(
        {"path": "p", "kinds": ["pytest"], "targets": ["p/tests/test_a.py::test_a"]}, policy
    )
    (kind, argv) = plan.checks[0]
    assert kind == "pytest" and argv[1:4] == ["-m", "pytest", "-q"]
    assert argv[-1] == "tests/test_a.py::test_a"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "no:cacheprovider"


def test_card_shows_the_plan_and_binds_it(tmp_path):
    policy = _ws(tmp_path, {"p/mod.py": "x = 1\n"})
    prepared = rc.prepare_input({"path": "p", "kinds": ["auto"], "plan": {"binding": "x"}}, policy)
    card = prepared["plan"]
    assert card["binding"] != "x"  # the model's plan is replaced
    assert card["containment"] == rc.OS_SANDBOX and card["dir"] == "@workspace/p"
    assert "compileall" in [c["kind"] for c in card["checks"]]
    assert card["always"] == "effective"
    # A test file appears between the card and the run: the argv differs -> refused.
    (policy.workspace / "p" / "test_new.py").write_text("def test_n(): pass\n")
    result = rc.run(prepared, policy)
    assert (result["status"], result["reason"]) == ("refused", "binding_changed")
    missing = rc.run({"path": "p"}, policy)
    assert (missing["status"], missing["reason"]) == ("refused", "binding_missing")


def test_card_says_why_it_asks_on_limits_only(tmp_path, monkeypatch):
    monkeypatch.setitem(rc._HOST, "status", LIMITS)
    policy = _ws(tmp_path, {"p/mod.py": "x = 1\n"})
    card = rc.prepare_input({"path": "p"}, policy)["plan"]
    assert card["always"] == "capped:limits_only"
    assert card["asks_because"] == "rule_always_capped"
    assert set(card["not_held"]) == set(rc.NOT_HELD)


def test_calls_per_turn(tmp_path, monkeypatch):
    policy = _ws(tmp_path, {"p/mod.py": "x = 1\n"})
    monkeypatch.setattr(rc, "_count_call", lambda: False)
    prepared = rc.prepare_input({"path": "p"}, policy)
    assert rc.run(prepared, policy)["reason"] == "call_limit"


# --- the gate puts the plan in before the card -----------------------------------------------


def test_checkpoint_hooks_prepare_the_input_before_the_card(tmp_path):
    from stiracore.agent.checkpoint_hooks import CheckpointHooks
    from stiracore.core import HITLAction, StiraConfig, StiraEngine
    from stiracore.core.engine import HITLDecision

    policy = _ws(tmp_path, {"p/mod.py": "x = 1\n"})
    engine = StiraEngine(config=StiraConfig())
    seen = []

    def hitl(request):
        seen.append(request.tool_input)
        return HITLDecision(action=HITLAction.REJECT, reason="no")

    engine.on_hitl_request = hitl
    hooks = CheckpointHooks(engine, policy_check=lambda name: ("ask", 1))
    hooks.input_preparers["run_checks"] = lambda i: rc.prepare_input(i, policy)
    event = SimpleNamespace(
        tool_use={"name": "run_checks", "toolUseId": secrets.token_hex(4), "input": {"path": "p"}},
        cancel_tool=None,
        invocation_state={},
    )
    hooks.before_tool(event)
    assert seen and "compileall" in [c["kind"] for c in seen[0]["plan"]["checks"]]

    def broken(_input):
        raise RuntimeError("x")

    hooks.input_preparers["run_checks"] = broken
    event.cancel_tool = None
    hooks.before_tool(event)
    assert event.cancel_tool and "not executed" in event.cancel_tool


# --- doctor ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "host", "expected", "level"),
    [
        (None, None, "run_checks_off", "info"),
        ("run_checks_off", None, "run_checks_off", "info"),
        ("run_checks_platform", None, "run_checks_platform", "info"),
        ("run_checks_root", None, "run_checks_root", "info"),
        ("run_checks_on", SANDBOXED, "run_checks_ready", "info"),
        ("run_checks_on", LIMITS, "run_checks_limits_only", "warning"),
        (
            "run_checks_on",
            rc.Containment(rc.LIMITS_ONLY, "run_checks_self_test_failed", "failed: dns", ("dns",)),
            "run_checks_self_test_failed",
            "warning",
        ),
    ],
)
def test_doctor_notes(code, host, expected, level):
    note = doctor._run_checks_note(code, host)
    assert (note["code"], note["level"]) == (expected, level)
    assert expected in doctor.FINDING_CODES
    if host is not None and not host.verified:
        assert "read as Ask" in note["message"]


def test_cli_discover_runs_nothing(tmp_path, capsys, monkeypatch):
    from stiracore.diagnostics.__main__ import main

    policy = _ws(tmp_path, {"p/tests/test_a.py": "def test_a(): pass\n"})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert main(["checks", "discover", str(policy.workspace / "p")]) == 0
    out = capsys.readouterr().out
    assert "pytest:" in out and "Nothing was run" in out


# --- log and audit: codes and counts only ----------------------------------------------------

SECRETISH = "tests/test_private_name.py::test_secret_marker"


def _result():
    return {
        "status": "failed",
        "reason": None,
        "containment": rc.OS_SANDBOX,
        "checks": [
            {
                "kind": "pytest",
                "status": "failed",
                "counts": {"passed": 1, "failed": 1, "errors": 0, "skipped": 0, "source": "junit"},
                "failures": [{"id": SECRETISH, "message": "boom marker", "text": "t"}],
            }
        ],
        "tree_changes": {"added": ["private_file.txt"], "changed": [], "removed": [], "more": 0},
        "real_tree_changed": False,
        "output_tail": "output marker",
    }


def test_log_keeps_codes_and_counts_only():
    logged = file_tools.payload_for_log(
        "run_checks", "output", {"content": rc.content_for_model(_result())}
    )
    text = json.dumps(logged)
    assert "failed" in text and "pytest" in text
    for leak in (SECRETISH, "boom marker", "private_file", "output marker"):
        assert leak not in text
    shown = file_tools.payload_for_log(
        "run_checks",
        "input",
        {"kinds": ["pytest"], "path": "p", "targets": ["a", "b"], "plan": {"argv": "/Users/x"}},
    )
    assert shown == {"kinds": ["pytest"], "path": "@workspace/p", "targets": 2}
    assert file_tools.payload_for_log("run_checks", "input", {"path": "/Users/x"})["path"] == (
        "refused:invalid_path"
    )


def _audit_rows(tmp_path, monkeypatch) -> str:
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        rc.set_audit(AuditChain(store).record_checks)
        rc._audit(_result(), tmp_path / "folder-with-private-name")
        events = store.get_events(limit=10, event_type="audit.checks.failed")
        return json.dumps([e.attributes for e in events])
    finally:
        rc.set_audit(None)
        store.close()


def _assert_no_text(rows: str) -> None:
    assert '"kinds": ["pytest"]' in rows and '"failed": 1' in rows
    for leak in (SECRETISH, "boom marker", "private_file", "output marker", "private-name"):
        assert leak not in rows


def test_audit_row_has_kinds_counts_mode_and_a_keyed_hash(tmp_path, monkeypatch):
    rows = _audit_rows(tmp_path, monkeypatch)
    _assert_no_text(rows)
    attributes = json.loads(rows)[0]
    assert attributes["target"].startswith("workspace:") and len(attributes["target"]) == 74
    assert attributes["containment"] == "os_sandbox" and attributes["tree_changes"] == 1


def test_mutation_audit_text_leak(tmp_path, monkeypatch):
    class _Anything(frozenset):
        def __contains__(self, _item):
            return True

    monkeypatch.setattr(audit_chain, "_CHECKS_KINDS", _Anything())
    original = rc._audit

    def leaky(result, directory):
        result = dict(result)
        result["checks"] = [{**c, "kind": c["failures"][0]["id"]} for c in result["checks"]] + list(
            result["checks"]
        )
        return original(result, directory)

    monkeypatch.setattr(rc, "_audit", leaky)
    with pytest.raises(AssertionError):
        _assert_no_text(_audit_rows(tmp_path, monkeypatch))


def test_audit_outcomes_are_fixed(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        with pytest.raises(ValueError):
            AuditChain(store).record_checks("ran")
        AuditChain(store).record_checks("refused", reason_code="free text here", target="/x")
        attributes = store.get_events(limit=5, event_type="audit.checks.refused")[0].attributes
        assert attributes == {"reason_code": "unknown", "target": "invalid"}
    finally:
        store.close()


def test_text_is_masked_and_defanged():
    shown = rc._shown("<b>AKIAIOSFODNN7EXAMPLE</b>\u200b", 200)
    assert "<" not in shown and "AKIAIOSFODNN7EXAMPLE" not in shown and "\u200b" not in shown
