"""W6-F (#296): the fixes from the supervised real-model smoke (W6e) — offline.

* #640 F1 — a stop during an in-flight model call charges the call's reported usage when
  the call completes (``model_completed_after_stop``); the whole reservation only when the
  usage is unknown or the call is given up (a stalled provider: abandoned).
* #641 D1 — run_checks discovery reads a workspace venv's own packages, also when its
  ``python`` is a symlink to the server's base interpreter.
* #642 D2 — the tools view shows run_checks as ``os_sandbox`` on a verified host.
* #643 D3 — an iteration that consumed an effect counts toward ``max_iterations`` whatever
  stopped it (also when the stop ended the grant); one without an effect never counts.
* #644 U3 — the heartbeat start keeps ``tick_min_spacing_sec`` from a resume too.

Fake providers and fake clocks only: no Bedrock, no model.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import venv
from pathlib import Path

import pytest
from grants_exec_helpers import ScriptedProvider, grant, make_env, use
from grants_helpers import confirm_and_resume

from stiracore.grants import executor as ex_mod
from stiracore.tools import run_checks as rc
from stiracore.tools import tool_policy as tp

# make_env: the estimator says 10, the provider's max_tokens is 50 → a reservation of 60;
# the scripted provider reports 7 in + 3 out → 10 spent.
RESERVED, SPENT = 60, 10


def _edit(env, n=1, name="notes.md"):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": "x"}, n)


def _tick_used(env) -> int:
    return env.executor.board(env.grant_id)["outcomes"][-1]["tick_used"]


STOPS = {
    "revoke": lambda e: e.service.revoke(e.grant_id),
    "pause": lambda e: e.service.pause(e.grant_id),
    "new": lambda e: e.service.session_reset(),
    "kill": lambda e: e.authority.kill_switch(),
    # The TTL passes; the service's look expires the grant and narrows (in-flight cancel).
    "ttl": lambda e: (e.clocks.tick(7201), e.service.sync()),
}


def _held_call(env):
    """The first model call blocks (inside the provider, after its consume) until released."""
    started, release = threading.Event(), threading.Event()
    env.provider.on_call = lambda n: (started.set(), release.wait(20)) if n == 1 else None
    env.provider.script = [[_edit(env)], [_edit(env, 2, "b.md")]]
    env.executor.step(env.grant_id)
    assert started.wait(10)
    return release


# --- #640 F1: the charge of a stop during a model call ----------------------------------------


@pytest.mark.parametrize("after", [0.0, 0.8], ids=["at_once", "after_the_grace"])
@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_stop_during_a_model_call_that_completes_charges_its_usage(tmp_path, stop, after):
    """The call finishes after the stop (at once, or later than the 0.5 s grace but within
    the drain): its reported usage is charged — total and per-tick — and the rest of the
    reservation released once; no effect follows; it counts as one iteration (#643)."""
    env = make_env(tmp_path)
    release = _held_call(env)
    STOPS[stop](env)
    timer = threading.Timer(after, release.set)
    timer.start()
    try:
        assert env.executor.wait_idle(10)
    finally:
        timer.cancel()
        release.set()
    view = env.executor.view(env.grant_id)
    assert view["end"] in ("stopped", "permit_stale"), view
    assert "model_completed_after_stop" in view["residual"]
    assert not view["abandoned"] and "abandoned_call" not in view["residual"]
    g = grant(env)
    assert g.used_tokens == SPENT, g.used_tokens  # not the reservation (60)
    assert _tick_used(env) == SPENT
    assert g.iterations == 1  # #643: the same for every stop (it consumed a model call)
    assert env.provider.calls == 1 and not (env.sub / "notes.md").exists()
    assert env.executor.status()["abandoned_calls"] == 0


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_stalled_call_after_a_stop_keeps_the_whole_reservation(tmp_path, stop):
    """No result within the grace and the drain: the call is abandoned, its provider request
    told to stop, and the whole reservation stays charged — a late answer changes nothing."""
    env = make_env(tmp_path, stop_drain_sec=0.3)
    release = _held_call(env)
    try:
        STOPS[stop](env)
        assert env.executor.wait_idle(10)
        view = env.executor.view(env.grant_id)
        assert view["abandoned"] and "abandoned_call" in view["residual"]
        assert grant(env).used_tokens == RESERVED and _tick_used(env) == RESERVED
        assert grant(env).iterations == 1  # #643
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)
    assert grant(env).used_tokens == RESERVED and _tick_used(env) == RESERVED
    assert not (env.sub / "notes.md").exists() and env.provider.calls == 1


def test_a_user_turn_during_a_model_call_charges_its_usage(tmp_path):
    """A person's turn does not cancel the call: it ends, is charged its usage, and the
    iteration yields at the next boundary (unchanged; one counted iteration)."""
    env = make_env(tmp_path)
    release = _held_call(env)
    env.executor.preempt_signal.arrive()
    release.set()
    assert env.executor.wait_idle(10)
    view = env.executor.view(env.grant_id)
    assert view["end"] == "yielded_user_turn" and not view["abandoned"]
    assert grant(env).used_tokens == SPENT and _tick_used(env) == SPENT
    assert grant(env).iterations == 1 and not (env.sub / "notes.md").exists()


class _CancelSpy(ScriptedProvider):
    """Records the cancel event the provider request was given, and whether it was set
    when the call answered."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.cancels: list = []
        self.set_at_answer: list[bool] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        cancel = kwargs.get("cancel_signal")
        self.cancels.append(cancel)
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            if "messageStart" in event:
                self.set_at_answer.append(bool(cancel is not None and cancel.is_set()))
            yield event


@pytest.mark.parametrize("stall", [False, True])
def test_the_provider_request_is_cancelled_only_when_the_call_is_given_up(tmp_path, stall):
    env = make_env(tmp_path, stop_drain_sec=0.3)
    env.provider = _CancelSpy()
    release = _held_call(env)
    try:
        env.service.revoke(env.grant_id)
        if not stall:
            release.set()
        assert env.executor.wait_idle(10)
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)
    cancel = env.provider.cancels[0]
    assert cancel is not None and env.provider.set_at_answer, env.provider.set_at_answer
    if stall:
        assert cancel.is_set() and grant(env).used_tokens == RESERVED
    else:
        # The child's own cancel was set by the stop; the provider's was not: it answered.
        assert env.provider.set_at_answer == [False] and not cancel.is_set()
        assert grant(env).used_tokens == SPENT


@pytest.mark.parametrize("stop", ["shutdown", "wall_time"])
def test_shutdown_and_the_wall_time_do_not_wait_for_a_running_call(tmp_path, stop):
    """No drain for these: the provider request is told to stop at once (the grace only)."""
    import time as _time

    env = make_env(tmp_path, stop_drain_sec=5.0)
    release = _held_call(env)
    try:
        t0 = _time.monotonic()
        if stop == "shutdown":
            env.executor.stop(0.05)
        else:
            env.mono[0] += 601
            env.executor.poll()
        assert env.executor.wait_idle(10)
        assert _time.monotonic() - t0 < ex_mod.ABANDON_GRACE_SEC + 2.0
        assert env.executor.view(env.grant_id)["abandoned"]
        assert grant(env).used_tokens == RESERVED
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)


def test_a_tick_cost_row_of_a_stopped_call_carries_the_reported_tokens(tmp_path):
    from test_grants_tick import fire_and_wait, tick_env

    env = tick_env(tmp_path)
    env.provider.on_call = lambda n: env.service.pause(env.grant_id) if n == 1 else None
    env.provider.script = [[_edit(env)]]
    assert fire_and_wait(env) == "started"
    rows = env.executor.tick_costs(env.grant_id)
    assert len(rows) == 1 and rows[0]["model_calls"] == 1
    assert (rows[0].get("tokens_in"), rows[0].get("tokens_out")) == (7, 3), rows
    assert grant(env).used_tokens == SPENT and grant(env).iterations == 1


# --- #643 D3: the iteration count does not depend on the stop ---------------------------------


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_stop_before_any_effect_never_counts(tmp_path, stop):
    """Stopped between the first model call's permit and its consume: no effect consumed,
    nothing counted — the same for a pause as for the stops that end the grant."""
    fired = []

    def probe(point, it):
        if point == "model:authorized" and not fired:
            fired.append(point)
            STOPS[stop](env)

    env = make_env(tmp_path, probe=probe)
    env.provider.script = [[_edit(env)]]
    env.executor.step(env.grant_id)
    assert env.executor.wait_idle(10)
    assert fired and env.provider.calls == 0
    assert grant(env).iterations == 0 and grant(env).used_tokens == 0


# --- #644 U3: the spacing from a resume ---------------------------------------------------------


def test_the_heartbeat_start_keeps_the_spacing_from_a_resume(tmp_path):
    from test_grants_tick import fire, fire_and_wait, tick_env

    env = tick_env(tmp_path, spacing=300)  # its clocks are past the issue's spacing
    env.provider.script = [[{"text": "nothing"}]]
    env.service.pause(env.grant_id)
    confirm_and_resume(env.service, env.grant_id, {})
    assert fire(env) == "min_spacing"
    env.clocks.tick(299)
    assert fire(env) == "min_spacing"
    env.clocks.tick(1)
    assert fire_and_wait(env) == "started"


def test_the_spacing_counts_from_the_later_of_the_end_and_the_resume(tmp_path):
    from test_grants_tick import fire, fire_and_wait, tick_env

    env = tick_env(tmp_path, spacing=300)
    env.provider.script = [[{"text": "one"}], [{"text": "two"}]]
    assert fire_and_wait(env) == "started"  # an end now (the executor's clock)
    env.mono[0] += 200
    env.clocks.tick(200)
    env.service.pause(env.grant_id)
    confirm_and_resume(env.service, env.grant_id, {})  # 200 s after the end
    env.mono[0] += 100
    env.clocks.tick(100)  # 300 s after the end, 100 s after the resume
    assert fire(env) == "min_spacing"
    env.mono[0] += 200
    env.clocks.tick(200)
    assert fire_and_wait(env) == "started"


def test_a_resume_with_an_unreadable_clock_skips(tmp_path, monkeypatch):
    """A resume whose clock reading failed (``None`` recorded) keeps the start skipped."""
    from test_grants_tick import fire, tick_env

    env = tick_env(tmp_path, spacing=300)
    env.service.pause(env.grant_id)
    confirm_and_resume(env.service, env.grant_id, {})
    known, at = env.service.resumed_at(env.grant_id)
    assert known and at is not None and at.mono == env.clocks.mono
    monkeypatch.setattr(env.service, "resumed_at", lambda _gid: (True, None))
    env.clocks.tick(400)  # past the spacing of the resume, within the TTL
    assert fire(env) == "min_spacing"  # unknown is a skip (fail closed)
    monkeypatch.undo()
    assert fire(env) == "started"


# --- #641 D1: a workspace venv is not the server ----------------------------------------------


def _make_venv(path: Path, *, system_site: bool = False) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=True, system_site_packages=system_site).create(path)
    python = path / "bin" / "python"
    assert python.is_symlink() or python.exists()
    return python


def _site(venv_dir: Path) -> Path:
    sites = list(venv_dir.glob("lib/python*/site-packages"))
    assert len(sites) == 1
    return sites[0]


def _proj(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    (proj / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    return ws, proj


def test_a_symlinked_workspace_venv_is_read_for_its_own_packages(tmp_path):
    ws, proj = _proj(tmp_path)
    python = _make_venv(ws / ".venv")
    # The bug's precondition: both interpreters resolve to the same base binary.
    import os

    assert os.path.realpath(python) == os.path.realpath(sys.executable)
    assert rc._has_module(str(python), "pytest") is False  # the server has it; the venv not
    found = rc.discover(proj, ws)
    assert found.python == str(python)
    assert found.missing.get("pytest") == "pytest_not_installed"
    (_site(ws / ".venv") / "pytest").mkdir()
    (_site(ws / ".venv") / "pytest" / "__init__.py").write_text("", encoding="utf-8")
    assert rc._has_module(str(python), "pytest") is True
    found = rc.discover(proj, ws)
    assert found.available.get("pytest") == "test_files" and "pytest" not in found.missing


def test_a_module_only_the_venv_has_is_found_although_the_server_lacks_it(tmp_path):
    """The smoke's direction: the server's environment has no such module, the venv has."""
    ws, _proj_dir = _proj(tmp_path)
    python = _make_venv(ws / ".venv")
    name = "stira_w6f_only_in_the_venv"
    (_site(ws / ".venv") / f"{name}.py").write_text("", encoding="utf-8")
    assert rc._has_module(str(python), name) is True
    assert rc._has_module(sys.executable, name) is False


def test_a_venv_with_the_system_site_packages_reads_the_base_site(tmp_path, monkeypatch):
    ws, _proj_dir = _proj(tmp_path)
    python = _make_venv(ws / ".venv", system_site=True)
    base = tmp_path / "base-site"
    base.mkdir()
    monkeypatch.setattr(rc, "_base_sites", lambda: [str(base)])
    assert rc._has_module(str(python), "pytest") is False
    (base / "pytest").mkdir()
    assert rc._has_module(str(python), "pytest") is True
    # Without the flag the base site is never read.
    other = _make_venv(tmp_path / "plain")
    assert rc._has_module(str(other), "pytest") is False


def test_a_venv_of_another_base_interpreter_is_unknown_for_the_system_site(tmp_path):
    venv_dir = tmp_path / "other"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "lib" / "python3.99" / "site-packages").mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text(
        "home = /nonexistent\ninclude-system-site-packages = true\n", encoding="utf-8"
    )
    python = venv_dir / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    assert rc._has_module(str(python), "pytest") is None


def test_the_servers_own_interpreter_is_still_answered_in_process(tmp_path):
    assert rc._has_module(sys.executable, "pytest") is True
    assert rc._has_module(sys.executable, "stira_w6f_no_such_module") is False


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="the tests run outside a venv")
def test_another_path_into_the_servers_own_venv_is_the_server(tmp_path):
    """Its own venv by another name (``python3`` for ``python``) is answered in process —
    ``stiracore`` is importable here (the source path), but not a file in site-packages."""
    alias = (
        Path(sys.prefix)
        / "bin"
        / ("python3" if Path(sys.executable).name != "python3" else "python")
    )
    if not alias.exists():
        pytest.skip("no second name for the interpreter in this venv")
    assert str(alias) != sys.executable
    assert rc._has_module(str(alias), "stiracore") is True


def test_discovery_with_a_venv_starts_no_process(tmp_path, monkeypatch):
    ws, proj = _proj(tmp_path)
    _make_venv(ws / ".venv")

    def boom(*_a, **_k):
        raise AssertionError("a process was started during discovery")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    found = rc.discover(proj, ws)
    assert found.missing.get("pytest") == "pytest_not_installed"


# --- #642 D2: the tools view on a verified host ------------------------------------------------


def _host(monkeypatch, *, verified: bool, enabled: bool = True):
    mode = rc.OS_SANDBOX if verified else rc.LIMITS_ONLY
    status = rc.Containment(mode, "run_checks_ready" if verified else "run_checks_limits_only")
    monkeypatch.setattr(rc, "containment", lambda **_k: status)
    monkeypatch.setattr(rc, "is_enabled", lambda: enabled)


@pytest.mark.parametrize(
    ("verified", "enabled", "kind"),
    [(True, True, "os_sandbox"), (False, True, "none"), (True, False, "none")],
)
def test_run_checks_isolation_follows_the_verified_containment(
    monkeypatch, verified, enabled, kind
):
    _host(monkeypatch, verified=verified, enabled=enabled)
    assert tp.isolation("run_checks") == {"kind": kind, "os_sandbox": kind == "os_sandbox"}
    assert tp.isolation("editor")["kind"] == "path_confined"
    assert tp.isolation("shell") == {"kind": "none", "os_sandbox": False}


def test_an_unreadable_containment_is_not_shown_as_isolated(monkeypatch):
    def broken(**_k):
        raise RuntimeError("self-test")

    monkeypatch.setattr(rc, "containment", broken)
    monkeypatch.setattr(rc, "is_enabled", lambda: True)
    assert tp.isolation("run_checks") == {"kind": "none", "os_sandbox": False}


@pytest.mark.parametrize("verified", [True, False])
def test_the_tools_view_row_shows_the_sandbox(monkeypatch, verified):
    from stiracore.observability.config_api import _tool_rows
    from stiracore.tools import tool_registry
    from stiracore.tools.tool_config import ToolConfig

    _host(monkeypatch, verified=verified)
    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "run_checks", rc.run_checks)
    config = ToolConfig(mode="custom", enabled_tools={"run_checks"}, ask_tools={"run_checks"})
    rows, _level = _tool_rows(config, "normal")
    row = next(r for r in rows if r["name"] == "run_checks")
    assert row["isolation"]["os_sandbox"] is verified
    assert row["isolation"]["kind"] == ("os_sandbox" if verified else "none")
    # Display only: the rule stays Ask; isolation is not approval.
    assert row["policy"] == "ask"


def test_the_isolation_text_has_both_languages():
    import json

    root = Path(__file__).resolve().parents[1] / "dashboard" / "static"
    for lang in ("en", "ja"):
        catalog = json.loads((root / "i18n" / f"catalog.{lang}.json").read_text("utf-8"))
        assert catalog.get("policy.isolation_os_sandbox")
    hub = (root / "js" / "settings_hub.js").read_text("utf-8")
    assert 'os_sandbox: () => [t("policy.isolation_os_sandbox"), "ok"]' in hub


# --- review round 1 -----------------------------------------------------------------------------

DISCARD = "REVIEW_RESPONSE_MUST_BE_DISCARDED"


def _held_text(env, text):
    started, release = threading.Event(), threading.Event()
    env.provider.script = [[{"text": text}]]
    env.provider.on_call = lambda n: (started.set(), release.wait(10))
    env.executor.step(env.grant_id)
    assert started.wait(10)
    return release, env.executor._current.child


@pytest.mark.parametrize("stop", ["user_turn", "revoke"])
def test_a_text_only_response_after_a_stop_never_reaches_the_child(tmp_path, stop):
    """Astra B1 (repro adopted): a person's turn (or a stop) during the call — the completed
    text-only answer is charged but discarded: not in the child's history, never classified;
    one call, no write, one counted iteration."""
    import json

    env = make_env(tmp_path)
    release, child = _held_text(env, DISCARD)
    classified = []

    class Classifier:
        name = "review-spy"

        def classify(self, **kwargs):
            classified.append(kwargs.get("agent_response"))

    child._stimulus_classifier = Classifier()
    child._get_classifier = lambda: child._stimulus_classifier
    if stop == "user_turn":
        env.executor.preempt_signal.arrive()
    else:
        env.service.revoke(env.grant_id)
    release.set()
    assert env.executor.wait_idle(10)
    assert DISCARD not in json.dumps(child.agent.messages, default=str)
    assert not any(DISCARD in str(c) for c in classified), classified
    view = env.executor.view(env.grant_id)
    if stop == "user_turn":
        assert view["end"] == "yielded_user_turn", view
    assert env.provider.calls == 1 and grant(env).used_tokens == SPENT
    assert _tick_used(env) == SPENT and grant(env).iterations == 1


def test_a_discarded_response_does_not_move_the_childs_state(tmp_path):
    """Astra B1, the real classifier: an "I am happy" answer after a person's turn leaves the
    child's turn summary empty (no emotion applied)."""
    env = make_env(tmp_path)
    release, child = _held_text(env, f"I am happy {DISCARD}")
    env.executor.preempt_signal.arrive()
    release.set()
    assert env.executor.wait_idle(10)
    assert (child._last_turn_summary or {}).get("category") is None
    assert grant(env).used_tokens == SPENT


def test_the_stop_wait_is_5_s_as_documented_and_shutdown_cuts_it(tmp_path):
    """Opus N1: the production drain (5 s after the 0.5 s grace) for a stalled call, and a
    shutdown inside that wait ends it at once; the docs state the same number."""
    import time as _time

    assert ex_mod.STOP_DRAIN_SEC == 5.0 and ex_mod.ABANDON_GRACE_SEC == 0.5
    root = Path(__file__).resolve().parents[1] / "docs"
    assert "up to 5 s more (`STOP_DRAIN_SEC`" in (root / "grants.md").read_text("utf-8")
    assert "plus up to 5 s while it reads its response" in (root / "autonomy.md").read_text("utf-8")
    assert "最大\n  5 秒（`STOP_DRAIN_SEC`" in (root / "grants_ja.md").read_text("utf-8")

    env = make_env(tmp_path, stop_drain_sec=None)  # the executor's default
    assert env.executor.stop_drain_sec == ex_mod.STOP_DRAIN_SEC
    release = _held_call(env)
    try:
        t0 = _time.monotonic()
        env.service.revoke(env.grant_id)
        assert env.executor.wait_idle(10)
        took = _time.monotonic() - t0
        assert 5.0 <= took < 5.5 + 1.5, took
        assert env.executor.view(env.grant_id)["abandoned"]
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)

    (tmp_path / "second").mkdir()
    env2 = make_env(tmp_path / "second", stop_drain_sec=None)
    release = _held_call(env2)
    try:
        env2.service.revoke(env2.grant_id)
        _time.sleep(ex_mod.ABANDON_GRACE_SEC + 0.5)  # inside the drain
        assert not env2.executor.wait_idle(0)
        t1 = _time.monotonic()
        env2.executor.stop(0.05)
        assert env2.executor.wait_idle(5)
        assert _time.monotonic() - t1 < 1.0
    finally:
        release.set()
        assert env2.executor.wait_abandoned(10)


# --- review round 1: the documented Always recipe, followed against the real routes --------


def test_the_documented_api_recipe_makes_the_editor_always(tmp_path, monkeypatch):
    """docs/autonomy.md step 2 (API): the exact body answers 428 with a ``rules_loosen``
    operation; confirmed on the host and sent again with the header, the rule is Always.
    Without the typed name or ``confirm`` it is refused (400)."""
    import test_yolo_admin_guard as admin
    from yolo_host import host_decide, post

    app = admin._build(tmp_path, monkeypatch, "external")
    try:
        _follow_the_api_recipe(app.test_client(), host_decide, post)
    finally:
        admin.runtime._reset_for_tests()
        admin.runtime.TRACKER._calls.clear()
        app.extensions["lifecycle"].shutdown()


def _follow_the_api_recipe(client, host_decide, post):
    body = {"name": "editor", "policy": "always", "confirm": True, "confirm_always": ["editor"]}
    assert post(client, "/api/config/tools", {**body, "confirm_always": []}).status_code == 400
    assert post(client, "/api/config/tools", {**body, "confirm": False}).status_code == 400
    first = post(client, "/api/config/tools", body)
    data = first.get_json()
    assert first.status_code == 428 and data["error_code"] == "host_confirmation_required"
    assert data["operation"]["kind"] == "rules_loosen"
    host_decide(data["operation"]["id"])
    done = post(client, "/api/config/tools", body, op_id=data["operation"]["id"])
    assert done.status_code == 200, done.get_json()
    rows = client.get("/api/config/tools").get_json()
    rows = rows.get("tools", rows) if isinstance(rows, dict) else rows
    editor = next(r for r in rows if isinstance(r, dict) and r.get("name") == "editor")
    assert editor["policy"] == "always"


def test_the_documented_ui_recipe_labels_exist():
    """docs/autonomy.md step 2 (UI): the labels it names are the catalogs' (en / ja)."""
    import json

    root = Path(__file__).resolve().parents[1]
    want = {
        "en": ("Tool permissions", "Always", "Save", "ALLOW", "I understand — save"),
        "ja": ("ツールの許可", "常に許可", "保存", "許可する", "理解したうえで保存"),
    }
    keys = (
        "hub.tools_title",
        "policy.always",
        "tools.save",
        "hub.confirm_word",
        "hub.confirm_save",
    )
    for lang, labels in want.items():
        catalog = json.loads(
            (root / "dashboard/static/i18n" / f"catalog.{lang}.json").read_text("utf-8")
        )
        assert tuple(catalog[k] for k in keys) == labels
        doc = (root / "docs" / ("autonomy.md" if lang == "en" else "autonomy_ja.md")).read_text(
            "utf-8"
        )
        for label in labels:
            assert label in doc, (lang, label)
