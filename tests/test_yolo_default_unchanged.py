"""ADR 0012 phase 1: with no allowance — and even with one — nothing the model or the UI
sees changes. Each server is built **in its own process** (``tests/yolo_fingerprint.py``),
so ``STIRA_YOLO_ALLOW`` is read exactly as at a real start and nothing is shared between
the two: the tool specs handed to the agent, the system prompt and the existing read
APIs are byte-identical between a server without ``STIRA_YOLO_ALLOW`` and one with
``STIRA_YOLO_ALLOW=external``; the only differences are the hook-only tool-call tracker,
the added status route and the YOLO refusals — which the OFF process shows are absent.

ADR 0012 addendum 2026-10-24: unset is the ``external`` default now, so the OFF process
sets ``STIRA_YOLO_ALLOW=off``; a third process with the variable unset (the default) must
look exactly like the ON one to the model (tool specs, prompt) and expose the same routes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fingerprint(tmp_path: Path, name: str, allowance: str | None) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("STIRA_YOLO")}
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{ROOT}"
    if allowance is not None:
        env["STIRA_YOLO_ALLOW"] = allowance
    if allowance not in (None, "off"):
        # The host confirmation by command: this process never shows a desktop dialog.
        env["STIRA_YOLO_HOST_CONFIRM"] = "command"
    out = tmp_path / f"{name}.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "yolo_fingerprint.py"),
            str(out),
            str(tmp_path / name),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(out.read_text("utf-8"))


@pytest.fixture(scope="module")
def prints(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("yolo-fingerprint")
    return {
        "off": _fingerprint(tmp_path, "off", "off"),
        "on": _fingerprint(tmp_path, "on", "external"),
        # Unset: the default allowance (the browser confirmation, no dialog either).
        "default": _fingerprint(tmp_path, "default", None),
    }


def test_the_default_is_the_external_allowance_to_the_model_and_the_routes(prints):
    """ADR 0012 addendum 2026-10-24: unset offers YOLO like ``external`` — same tool specs
    and prompt (the two YOLO lines, no window open), the same added routes and hook — and,
    like it, nothing is approved before a press (the approver exists, no window)."""
    off, on, default = prints["off"], prints["on"], prints["default"]
    assert default["tool_spec_sha256"] == off["tool_spec_sha256"] == on["tool_spec_sha256"]
    assert default["tool_names"] == off["tool_names"]
    assert default["prompt"] == on["prompt"]
    assert set(default["routes"]) == set(on["routes"])
    assert default["extra_hooks"] == on["extra_hooks"]
    assert default["auto_approver"] == "AutoApprover"
    assert default["reads"] == on["reads"]


def test_tool_specs_and_prompt_are_identical(prints):
    off, on = prints["off"], prints["on"]
    assert off["tool_spec_sha256"] == on["tool_spec_sha256"]
    assert off["tool_names"] == on["tool_names"]
    # #508: the allowance adds exactly two lines to the Tool Availability section (no
    # window is open); every other byte of the prompt is the same, and without the
    # allowance the prompt never mentions YOLO.
    from stiracore.tools.tool_availability import YOLO_SCOPE

    on_lines = on["prompt"].splitlines()
    assert [line for line in on_lines if "yolo" not in line.lower()] == off["prompt"].splitlines()
    assert [line for line in on_lines if "yolo" in line.lower()] == [
        "YOLO: no window is open.",
        YOLO_SCOPE,
    ]
    assert "yolo" not in off["prompt"].lower()


def test_only_a_hook_is_added_and_none_without_the_allowance(prints):
    off, on = prints["off"], prints["on"]
    assert "ToolCallTracker" not in off["extra_hooks"]
    assert on["extra_hooks"] == [*off["extra_hooks"], "ToolCallTracker"]


def test_the_approver_is_connected_only_with_the_allowance(prints):
    """Phase 5a: with ``STIRA_YOLO_ALLOW=off`` the gate has no approver (every Ask goes to
    a person, exactly as on ``main``); with it, the approver is the only addition."""
    assert prints["off"]["auto_approver"] == "NoneType"
    assert prints["on"]["auto_approver"] == "AutoApprover"


def test_existing_read_apis_are_byte_identical(prints):
    off, on = prints["off"], prints["on"]
    assert set(off["reads"]) == set(on["reads"])
    for path, answer in off["reads"].items():
        assert answer == on["reads"][path], path


def test_route_inventory_differs_only_by_the_yolo_and_host_operation_routes(prints):
    off, on = set(prints["off"]["routes"]), set(prints["on"]["routes"])
    assert on - off == {
        "/api/yolo/status GET",
        "/api/yolo/request POST",
        "/api/yolo/request/cancel POST",
        "/api/yolo/stop POST",
        "/api/yolo/unlatch POST",
        "/api/host-operations/<op_id> GET",
        "/api/host-operations/<op_id>/cancel POST",
        "/api/host-operations/<op_id>/confirm POST",
        # ADR 0014 phase 1 (#451): progress reports, wired only with the allowance.
        "/api/progress/current GET",
        "/api/progress/<report_id>/stop POST",
        "/api/progress/<report_id>/hold POST",
    }
    assert not off - on
    # Phase 7c: the settings routes exist on both (where YOLO is enabled for the next start).
    assert {route for route in off if "yolo" in route} == {
        "/api/yolo/settings GET",
        "/api/yolo/settings POST",
    }


def test_without_the_allowance_no_yolo_refusal_exists(prints):
    off, on = prints["off"], prints["on"]
    # OFF: the legacy rule API ignores an unknown key as on main; the hub refuses it as an
    # unknown field (400) as on main. ON: both are 409 yolo_not_settable.
    assert off["save_with_yolo_key"]["/api/config/tools"] == [200, None]
    assert off["save_with_yolo_key"]["/api/config/hub"][0] == 400
    assert off["save_with_yolo_key"]["/api/config/hub"][1] != "yolo_not_settable"
    for path in ("/api/config/tools", "/api/config/hub"):
        assert on["save_with_yolo_key"][path] == [409, "yolo_not_settable"], path
    # A loosening save while a tool call runs: only refused with the allowance.
    assert off["loosening_while_tool_runs"] == [200, None]
    assert on["loosening_while_tool_runs"] == [409, "tool_call_running"]
    # Phase 2: the same loosening after the call — applied as on main without the
    # allowance; with it, only a host confirmation applies it (428, nothing saved).
    assert off["loosening_after_tool_call"] == [200, None]
    assert on["loosening_after_tool_call"] == [428, "host_confirmation_required"]
    # (OFF already applied it during the call above: the rule is Ask there.) ON: nothing.
    assert "http_request" in json.loads(off["rules_after"])["ask_tools"]
    assert on["rules_after"] == on["rules_before"]
    assert "http_request" not in json.loads(on["rules_after"])["enabled_tools"]
