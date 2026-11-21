"""#791 (ADR 0029 Q3 / B6 / B6b): a grant subtree that is a Stira checkout is refused by
default; the widening option needs the user setting and the person's separate confirmation,
and even then the never-editable list stays unwritable; run-later files always need a person.

Offline and scripted (no network, no model, no wall clock).
"""

from __future__ import annotations

import inspect
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from grants_exec_helpers import MARKER, grant, make_env, run_one, use
from grants_helpers import confirm_and_issue, issue_body

from stiracore import self_protect
from stiracore.agent import checkpoint_hooks
from stiracore.agent.checkpoint_hooks import CheckpointHooks
from stiracore.core import HITLAction, StiraConfig, StiraEngine
from stiracore.core.engine import HITLDecision
from stiracore.grants import schema
from stiracore.grants.schema import GrantRefused, check_subtree, parse_issue
from stiracore.grants.service import GrantConfirmation, settings_from
from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy

ROOT = Path(__file__).resolve().parents[1]
symlinks = pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")


def refused(code):
    return pytest.raises(GrantRefused, match=f"^{code}$")


def checkout(where, names=("src", "stiracore", "__init__.py")):
    """A directory that looks like a Stira checkout (the marker only)."""
    package = where.joinpath(*names[:-1])
    package.mkdir(parents=True)
    (package / names[-1]).write_text("")
    (where / "docs").mkdir()
    return where


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "proj" / "src" / "app").mkdir(parents=True)
    (root / "proj" / "src" / "app" / "__init__.py").write_text("")
    return root


# --- B6: refused by default -------------------------------------------------------------------


def test_a_checkout_equal_inside_or_below_the_subtree_is_refused(ws):
    stira = checkout(ws / "deep" / "er" / "stira")
    for subtree in (stira, stira / "docs", stira / "src", ws / "deep", ws):
        with refused("grant_subtree_protected"):
            check_subtree(str(subtree), [ws])
        with refused("grant_subtree_protected"):
            parse_issue(issue_body(subtree), roots=[ws])


def test_a_normal_project_and_a_docs_only_copy_are_allowed(ws):
    copy = ws / "stira-docs"
    (copy / "docs" / "adr").mkdir(parents=True)
    (copy / "src" / "stiracore").mkdir(parents=True)  # no __init__.py: not the marker
    (copy / "stiracore").mkdir()
    (copy / "stiracore" / "__init__.py").write_text("")  # not under src/
    for subtree in (ws / "proj", copy, copy / "docs"):
        assert check_subtree(str(subtree), [ws]) == os.path.realpath(subtree)
    assert parse_issue(issue_body(ws / "proj"), roots=[ws]).self_development is False


def test_dot_dot_and_case_spellings_are_resolved_before_the_check(ws):
    stira = checkout(ws / "stira")
    with refused("grant_subtree_protected"):
        check_subtree(str(ws / "proj" / ".." / "stira" / "docs"), [ws])
    shouting = checkout(ws / "other", names=("SRC", "StiraCore", "__INIT__.py"))
    # A checkout is what ``src/stiracore/__init__.py`` opens: on a volume that ignores case
    # this spelling is one (refused); on a case-sensitive volume Python cannot import it.
    same = (shouting / "src" / "stiracore" / "__init__.py").exists()
    for subtree in (shouting, shouting / "docs"):
        if same:
            with refused("grant_subtree_protected"):
                check_subtree(str(subtree), [ws])
        else:
            assert check_subtree(str(subtree), [ws])
    with refused("grant_subtree_protected"):  # the subtree's own spelling never matters
        check_subtree(str(ws / "proj" / ".." / "stira" / "." / "docs") + os.sep, [ws])
    assert stira.is_dir()


@symlinks
def test_a_symlinked_spelling_is_refused(ws):
    stira = checkout(ws / "stira")
    (ws / "alias").symlink_to(stira, target_is_directory=True)
    with refused("grant_subtree_protected"):
        check_subtree(str(ws / "alias" / "docs"), [ws])
    # A checkout whose ``src`` is a link to the package's parent is still a checkout.
    real = ws / "elsewhere"
    (real / "stiracore").mkdir(parents=True)
    (real / "stiracore" / "__init__.py").write_text("")
    (ws / "linked").mkdir()
    (ws / "linked" / "src").symlink_to(real, target_is_directory=True)
    with refused("grant_subtree_protected"):
        check_subtree(str(ws / "linked"), [ws])


def test_the_running_servers_own_tree_is_refused(ws, monkeypatch):
    package = ws / "site" / "stiracore"  # an installed package: no ``src/`` marker
    (package / "tools").mkdir(parents=True)
    (ws / "site" / "otherpkg").mkdir()
    for subtree in (ws / "site", package, package / "tools"):
        assert check_subtree(str(subtree), [ws])  # not this server's package: allowed
    monkeypatch.setattr(self_protect, "_server_package", lambda: os.path.realpath(package))
    for subtree in (ws / "site", package, package / "tools"):
        with refused("grant_subtree_protected"):
            check_subtree(str(subtree), [ws])
    assert check_subtree(str(ws / "site" / "otherpkg"), [ws])  # a sibling is not inside it


def test_the_real_server_tree_is_found():
    here = os.path.dirname(os.path.abspath(self_protect.__file__))
    assert self_protect._server_package() == os.path.realpath(here)
    assert self_protect.subtree_protected(here) is True


@pytest.mark.parametrize(
    "answer", [OSError("unreadable"), ValueError("odd"), None, "no", 0, False, True, "FREE"]
)
def test_a_check_that_cannot_judge_refuses(ws, monkeypatch, answer):
    def judge(_resolved):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(self_protect, "subtree_verdict", judge)
    with refused("grant_subtree_protected"):
        check_subtree(str(ws / "proj"), [ws])
    with refused("grant_subtree_protected"):  # the tick does not lift "cannot judge"
        check_subtree(str(ws / "proj"), [ws], self_development=True)
    monkeypatch.setattr(self_protect, "subtree_verdict", lambda _resolved: self_protect.FREE)
    assert check_subtree(str(ws / "proj"), [ws])


# --- F0: only the protected paths of a checkout refuse; its data places do not -----------------


def repo(where):
    """A directory laid out like this repository, with the default data directory in it."""
    checkout(where)
    for name in (
        "src/stiracore/tools",
        "dashboard/static/js",
        "tests",
        "docs/adr",
        "docs/eval",
        "docs/guide",
    ):
        (where / name).mkdir(parents=True)
    (where / ".local" / "stira" / "workspace" / "myproj").mkdir(parents=True)
    (where / "workspace" / "myproj").mkdir(parents=True)
    return where


FREE_IN_A_CHECKOUT = (
    ".local",
    ".local/stira/workspace",
    ".local/stira/workspace/myproj",
    "workspace",
    "workspace/myproj",
)
PROTECTED_IN_A_CHECKOUT = (
    "",
    "src",
    "src/stiracore",
    "src/stiracore/tools",
    "dashboard",
    "dashboard/static",
    "dashboard/static/js",
    "tests",
    "docs",
    "docs/eval",
    "docs/adr",
    "docs/guide",  # N1: any folder of a checkout outside its data places
)


@pytest.mark.parametrize("running", [False, True])
def test_the_default_layout_issues_ordinary_grants(ws, monkeypatch, running):
    """STIRA_DATA_DIR unset: the workspace is ``<checkout>/.local/stira/workspace``."""
    stira = repo(ws / "Stira-Strands")
    if running:
        package = os.path.realpath(stira / "src" / "stiracore")
        monkeypatch.setattr(self_protect, "_server_package", lambda: package)
    for name in FREE_IN_A_CHECKOUT:
        subtree = stira / name
        assert self_protect.subtree_verdict(str(subtree)) == self_protect.FREE, name
        assert check_subtree(str(subtree), [ws]) == os.path.realpath(subtree)
        # No option is needed, and the tick loosens nothing there.
        assert parse_issue(issue_body(subtree), roots=[ws]).self_development is False
        ticked = issue_body(subtree, self_development=True)
        assert parse_issue(ticked, roots=[ws], self_development=True).self_development is False
    for name in PROTECTED_IN_A_CHECKOUT:
        with refused("grant_subtree_protected"):
            check_subtree(str(stira / name), [ws])
    with refused("grant_subtree_protected"):  # a directory that holds the checkout
        check_subtree(str(ws), [ws])


def test_this_repository_is_judged_by_its_real_layout():
    assert self_protect.checkout_roots_above(str(ROOT)) == [os.path.realpath(ROOT)]
    for name in ("", "src", "src/stiracore", "dashboard", "tests", "docs"):
        assert self_protect.subtree_protected(str(ROOT / name)) is True, name
    assert self_protect.subtree_protected(str(ROOT / "docs" / "adr")) is True  # D2
    assert self_protect.subtree_protected(str(ROOT / "docs" / "tools")) is True  # N1
    assert self_protect._subtree_in_source((".local", "stira", "workspace")) is False
    assert self_protect._subtree_in_source((".local", "stira", "workspace", "p", "src")) is False
    assert self_protect._subtree_in_source(("workspace",)) is False
    assert self_protect._subtree_in_source(("workspaces",)) is True
    assert self_protect._subtree_in_source(("local", "stira")) is True


def test_a_checkout_cloned_into_the_default_workspace_is_still_refused(ws):
    stira = repo(ws / "Stira-Strands")
    clone = checkout(stira / ".local" / "stira" / "workspace" / "stira-wt")
    for subtree in (clone, clone / "src", stira / ".local" / "stira" / "workspace"):
        with refused("grant_subtree_protected"):
            check_subtree(str(subtree), [ws])
    assert check_subtree(str(stira / ".local" / "stira" / "workspace" / "myproj"), [ws])


def test_a_subtree_inside_a_run_later_directory_is_refused_with_or_without_the_tick(ws):
    for name in (".git", ".vscode/x", ".claude/skills"):
        (ws / "proj" / name).mkdir(parents=True)
        for ticked in (False, True):
            with refused("grant_subtree_protected"):
                check_subtree(str(ws / "proj" / name), [ws], self_development=ticked)
    assert check_subtree(str(ws / "proj"), [ws])  # holding ``.git`` is every project


def test_an_ordinary_grant_in_the_default_workspace_writes_its_project(tmp_path):
    (tmp_path / "src" / "stiracore").mkdir(parents=True)
    (tmp_path / "src" / "stiracore" / "__init__.py").write_text("")
    (tmp_path / ".local" / "stira").mkdir(parents=True)
    env = make_env(tmp_path / ".local" / "stira")  # issues a grant on <checkout>/.local/…/proj
    assert self_protect.checkout_roots_above(str(env.sub)) == [os.path.realpath(tmp_path)]
    assert grant(env).self_development is False
    (env.sub / "pkg").mkdir()
    view = _iterate(
        env,
        _create(env.sub / "pyproject.toml"),
        _create(env.sub / "conftest.py", 2),
        _create(env.sub / "pkg" / "__init__.py", 3),
    )
    assert view["end"] != "tool_refused", view
    for name in ("pyproject.toml", "conftest.py", "pkg/__init__.py"):
        assert (env.sub / name).read_text() == "changed\n"
    (env.sub / ".git").mkdir()
    view = _iterate(env, _create(env.sub / ".git" / "config"))
    assert (view["end"], view["detail"]) == ("tool_refused", "protected_path")
    assert not (env.sub / ".git" / "config").exists()


# --- F1: the running server's own checkout is refused, also with the option --------------------


def test_the_option_never_reaches_the_running_servers_checkout(ws, monkeypatch):
    stira = repo(ws / "Stira-Strands")
    other = repo(ws / "stira-wt")
    package = os.path.realpath(stira / "src" / "stiracore")
    monkeypatch.setattr(self_protect, "_server_package", lambda: package)
    assert schema.CODES["grant_subtree_running_checkout"] == 400
    for name in PROTECTED_IN_A_CHECKOUT:
        subtree = stira / name
        assert self_protect.subtree_verdict(str(subtree)) == self_protect.RUNNING, name
        with refused("grant_subtree_running_checkout"):
            check_subtree(str(subtree), [ws], self_development=True)
        with refused("grant_subtree_running_checkout"):
            ticked = issue_body(subtree, self_development=True)
            parse_issue(ticked, roots=[ws], self_development=True)
        with refused("grant_subtree_protected"):  # without the tick: the default refusal
            check_subtree(str(subtree), [ws])
    with refused("grant_subtree_running_checkout"):  # a directory that holds it
        check_subtree(str(ws), [ws], self_development=True)
    # Another checkout (a separate worktree) is what the option is for.
    assert self_protect.subtree_verdict(str(other)) == self_protect.CHECKOUT
    ticked = issue_body(other, self_development=True)
    assert parse_issue(ticked, roots=[ws], self_development=True).self_development is True


def test_the_option_never_reaches_an_installed_server_package(ws, monkeypatch):
    package = ws / "site" / "stiracore"
    (package / "tools").mkdir(parents=True)
    monkeypatch.setattr(self_protect, "_server_package", lambda: os.path.realpath(package))
    for subtree in (ws / "site", package, package / "tools"):
        with refused("grant_subtree_running_checkout"):
            check_subtree(str(subtree), [ws], self_development=True)
    assert self_protect.grant_write_refused(str(package / "tools" / "new.py")) is True


def test_the_real_running_checkout_is_refused_with_the_option():
    here = os.path.dirname(os.path.abspath(self_protect.__file__))
    assert self_protect.subtree_verdict(here) == self_protect.RUNNING
    for root in self_protect.checkout_roots_above(here):
        assert self_protect.subtree_verdict(root) == self_protect.RUNNING


def test_the_subtree_is_judged_again_when_the_grant_is_issued(tmp_path):
    """A checkout that appears between the press and the host confirmation."""
    env = make_env(tmp_path)
    env.service.revoke(env.grant_id)
    body = _self_dev_body(env, env.sub)
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    checkout(env.sub / "stira")
    with refused("grant_subtree_protected"):
        env.service.issue(body, op_id=view["id"])


def test_the_grant_record_and_view_carry_the_flag(tmp_path):
    from stiracore.grants.schema import grant_from_dict, grant_to_dict

    env = _self_dev_env(tmp_path)
    plain = grant(env)
    assert plain.self_development is False
    assert "self_development" not in grant_to_dict(plain, "g")
    assert "self_development" not in env.service.view(env.grant_id)
    body = _self_dev_body(env, env.stira, self_development=True)
    env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    flagged = grant(env)
    assert flagged.self_development is True
    stored = grant_to_dict(flagged, "g")
    assert stored["self_development"] is True
    assert grant_from_dict(stored)[0].self_development is True
    assert env.service.view(env.grant_id)["self_development"] is True
    for odd in (False, 1, "true", None):  # written only as exactly true
        with refused("grant_body_invalid"):
            grant_from_dict({**stored, "self_development": odd})
    assert grant_from_dict(grant_to_dict(plain, "g"))[0].self_development is False


def test_the_walk_is_bounded_and_past_the_bound_it_cannot_judge(ws):
    for n in range(5):
        (ws / "proj" / f"d{n}").mkdir()
    assert self_protect.subtree_protected(str(ws / "proj"), max_dirs=8) is False
    with pytest.raises(self_protect.CannotJudge):
        self_protect.subtree_protected(str(ws / "proj"), max_dirs=7)
    assert issubclass(self_protect.CannotJudge, OSError)


# --- B6b: the widening option -------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_exactly_true_turns_it_on():
    assert settings_from({"stage_c": True}).grant_self_development is False
    assert settings_from({"stage_c": True, "grant_self_development": True}).grant_self_development
    loose = settings_from({"stage_c": True, "grant_self_development": "yes"})
    assert loose.grant_self_development is False
    assert "grant_self_development_invalid" in loose.warnings
    assert "autonomy_unknown_key" not in settings_from({"grant_self_development": True}).warnings


def test_option_off_on_and_the_separate_confirmation(ws):
    stira = checkout(ws / "stira")
    ticked = issue_body(stira, self_development=True)
    with refused("grant_self_development_off"):  # the tick while the setting is off
        parse_issue(ticked, roots=[ws])
    with refused("grant_self_development_off"):
        parse_issue(issue_body(ws / "proj", self_development=True), roots=[ws])
    for setting in (False, True):  # no tick: refused whatever the setting says
        with refused("grant_subtree_protected"):
            parse_issue(issue_body(stira), roots=[ws], self_development=setting)
        with refused("grant_subtree_protected"):
            body = issue_body(stira, self_development=False)
            parse_issue(body, roots=[ws], self_development=setting)
    for odd in (1, "true", None, [True]):
        with refused("grant_field_invalid"):
            parse_issue(issue_body(stira, self_development=odd), roots=[ws], self_development=True)
    for setting in (1, "true", object()):  # the setting must be exactly True
        with refused("grant_self_development_off"):
            parse_issue(ticked, roots=[ws], self_development=setting)
    accepted = parse_issue(ticked, roots=[ws], self_development=True)
    assert accepted.self_development is True
    assert accepted.workspace_subtree == os.path.realpath(stira)
    # Ticked on a normal project: accepted, and nothing is loosened.
    plain = issue_body(ws / "proj", self_development=True)
    assert parse_issue(plain, roots=[ws], self_development=True).self_development is False


def test_nothing_but_a_persons_issue_can_enable_it(ws):
    """The proposal clamp (ADR 0029) and every other caller get the default: refused."""
    stira = checkout(ws / "stira")
    for function in (check_subtree, parse_issue):
        parameter = inspect.signature(function).parameters["self_development"]
        assert parameter.default is False and parameter.kind is parameter.KEYWORD_ONLY
    for almost in (1, "true", "yes", object()):
        with refused("grant_subtree_protected"):
            check_subtree(str(stira), [ws], self_development=almost)
    assert check_subtree(str(stira), [ws], self_development=True) == os.path.realpath(stira)
    assert schema.CODES["grant_subtree_protected"] == 400
    assert schema.CODES["grant_self_development_off"] == 409


def _self_dev_body(env, stira, **overrides):
    return issue_body(
        stira,
        goal=f"tidy the notes {MARKER}",
        scope={"tools": ["file_read", "editor"], "workspace_subtree": str(stira)},
        budget={"total_tokens": 10_000, "per_tick_tokens": 2_000},
        **overrides,
    )


def _self_dev_env(tmp_path, setting=True):
    env = make_env(tmp_path)
    env.service.revoke(env.grant_id)
    env.stira = checkout(env.ws / "stira")
    env.service.settings = replace(env.service.settings, grant_self_development=setting)
    return env


def test_the_service_refuses_by_default_and_names_the_loosening_when_confirmed(tmp_path):
    env = _self_dev_env(tmp_path, setting=False)
    assert env.service.list_view()["grant_self_development"] is False
    with refused("grant_subtree_protected"):
        env.service.issue(_self_dev_body(env, env.stira))
    with refused("grant_self_development_off"):
        env.service.issue(_self_dev_body(env, env.stira, self_development=True))
    env.service.settings = replace(env.service.settings, grant_self_development=True)
    assert env.service.list_view()["grant_self_development"] is True
    with refused("grant_subtree_protected"):  # the setting alone: the tick is missing
        env.service.issue(_self_dev_body(env, env.stira))
    body = _self_dev_body(env, env.stira, self_development=True, report_files=["NOTES.md"])
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    assert view["summary"]["changes"] == ["report_files:1", "loosen:self_development"]
    assert "stira" not in repr(view["summary"])  # a code, never a path
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    with refused("grant_subtree_protected"):  # the confirmed operation does not replace the tick
        env.service.issue({**body, "self_development": False}, op_id=view["id"])
    # A normal grant carries no loosening line.
    with pytest.raises(GrantConfirmation) as plain:
        env.service.issue(_self_dev_body(env, env.sub))
    assert "changes" not in plain.value.view["summary"]


# --- the never-editable list stays unwritable ---------------------------------------------------

_IN_PACKAGE = (
    "__init__.py core/approval.py core/approval_store.py core/engine.py core/containment.py",
    "core/turn_lock.py core/checkpoint.py control/job_approvals.py",
    "agent/checkpoint_hooks.py agent/nxp_agent.py agent/grant_child.py agent/model_factory.py",
    "tools/tool_policy.py tools/tool_consent.py tools/tool_config.py tools/tool_registry.py",
    "tools/exec_gate.py yolo grants authority labels.py tools/own_dashboard_gate.py",
    "tools/http_consent.py tools/web_tools.py integrations providers observability/masking.py",
    "observability/security.py middleware/taint_observer.py middleware/containment_observer.py",
    "tools/episodic_formatter.py identity_paths.py self_protect.py tools/checked_open.py",
    "tools/file_editor_tool.py tools/file_tools.py tools/child_env.py",
    "middleware/identity_guard.py tools/os_protection.py core/neural_state.py",
    "core/coherence.py core/amygdala.py agent/stimulus_classifier.py agent/ja_hybrid",
    "middleware/sycophancy_monitor.py eval core/breaker_sim.py tools/run_checks.py",
    "tools/page_check.py tools/page_check_guard.py observability/event_store.py",
    "observability/audit_chain.py observability/event_chain.py observability/turn_events.py",
    "tools/delegate_tool.py scheduler diagnostics config",
)
NEVER_EDITABLE = [
    *(f"src/stiracore/{name}" for line in _IN_PACKAGE for name in line.split()),
    "tests",
    "ci",
    "scripts",
    "docs/eval",
    "docs/adr",
    "docs/proposer-edit-boundary.md",
    "conftest.py",
    "pyproject.toml",
    "release-exclude.txt",
    # everything the server serves is the browser half of the approval layer (F2)
    "dashboard",
]
NEVER_EDITABLE_NAMES = [
    "__init__.py",
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "pyproject.toml",
    "ruff.toml",
    ".ruff.toml",
    "sitecustomize.py",
    "usercustomize.py",
]
RUN_LATER_LIST = [
    ".git",
    ".gitattributes",
    ".gitmodules",
    ".github",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    ".vscode",
    ".idea",
    ".devcontainer",
    ".envrc",
    ".husky",
    ".kiro",
    ".claude",
    ".cursor",
]
_FILES = (".gitattributes", ".gitmodules", ".gitlab-ci.yml", ".pre-commit-config.yaml", ".envrc")
# What a write names (the review's list, F3), each under one of the entries above.
RUN_LATER = [
    ".git/hooks/pre-commit",
    ".git/config",
    ".git",  # a worktree's pointer file
    ".git/info/attributes",
    ".gitattributes",
    ".gitmodules",
    ".github/workflows/ci.yml",
    ".github/actions/a/action.yml",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    ".vscode/tasks.json",
    ".vscode/settings.json",
    "proj.code-workspace",
    ".envrc",
    ".devcontainer/devcontainer.json",
    ".idea/workspace.xml",
    ".kiro/hooks/x.json",
    ".claude/settings.json",
    ".husky/pre-commit",
    ".cursor/rules/x.mdc",
]


def test_the_closed_lists_are_pinned():
    assert sorted("/".join(entry) for entry in self_protect.NEVER_EDITABLE) == sorted(
        NEVER_EDITABLE
    )
    assert len(self_protect.NEVER_EDITABLE) == len(NEVER_EDITABLE) == 68
    assert list(self_protect.NEVER_EDITABLE_NAMES) == NEVER_EDITABLE_NAMES
    # F4: what a grant may write in a checkout's source is a closed allow-list ...
    assert self_protect.CHECKOUT_WRITABLE == (("docs",),)
    # ... and the places that are not its source (F0) are a closed list too.
    assert self_protect.CHECKOUT_DATA == ((".local",), ("workspace",))
    assert ["/".join(e) for e in self_protect.GRANT_RUN_LATER_PATTERNS] == RUN_LATER_LIST
    # F3: what needs a person in a chat is everything a grant never writes.
    assert self_protect.PERSON_WRITE_PATTERNS == self_protect.GRANT_RUN_LATER_PATTERNS
    assert self_protect.RUN_LATER_SUFFIXES == (".code-workspace",)
    for must in ("tests/attack/x.py", ".github/workflows/ci.yml", "ci/linux/run.sh"):
        assert any(must.startswith(entry + "/") for entry in NEVER_EDITABLE + RUN_LATER_LIST)


def test_the_listed_files_exist_in_this_checkout():
    """The entries name real files of this checkout (a renamed file would leave the list)."""
    for name in NEVER_EDITABLE:
        if name not in ("conftest.py", "release-exclude.txt"):
            assert (ROOT / name).exists(), name
    for served in ("static/js", "templates", "static/i18n", "stage"):
        assert (ROOT / "dashboard" / served).is_dir()


@pytest.mark.parametrize("name", NEVER_EDITABLE_NAMES)
def test_evaluator_and_import_time_names_are_refused_at_any_depth(ws, name):
    stira = checkout(ws / "stira")
    refuse = self_protect.grant_write_refused
    for where in ("", "docs", "docs/guide/deep", "dashboard", "src/stiracore/newpkg"):
        assert refuse(str(stira / where / name)) is True, where
        assert refuse(str(stira / where / name.upper())) is True
    assert refuse(str(stira / "docs" / f"x{name}.md")) is False  # a name, not a substring
    assert refuse(str(ws / "proj" / name)) is False  # an ordinary project


def test_the_data_places_are_free_only_for_a_grant_that_lives_there(ws):
    stira = repo(ws / "Stira-Strands")
    home = stira / ".local" / "stira" / "workspace" / "myproj"
    refuse = self_protect.grant_write_refused
    for name in ("main.py", "pyproject.toml", "conftest.py", "pkg/__init__.py", "tests/t.py"):
        assert refuse(str(home / name), str(home)) is False, name
        assert refuse(str(home / name)) is True  # no subtree given: judged as source
        assert refuse(str(home / name), str(stira)) is True
        assert refuse(str(home / name), str(stira / "docs")) is True
    assert refuse(str(home / ".git" / "config"), str(home)) is True
    assert refuse(str(stira / "src" / "x.py"), str(home)) is True  # outside its data place
    assert refuse(str(stira / "docs" / "x.md"), str(home)) is False


@pytest.mark.parametrize("entry", NEVER_EDITABLE)
def test_every_never_editable_entry_is_refused_in_a_checkout(ws, entry):
    stira = checkout(ws / "stira")
    refuse = self_protect.grant_write_refused
    assert refuse(str(stira / entry)) is True
    assert refuse(str(stira / entry / "below" / "new.py")) is True
    assert refuse(str(stira / entry.upper())) is True  # a case spelling
    assert refuse(str(stira / "docs" / ".." / entry)) is True
    # The same relative path in a normal project is not on the list.
    assert refuse(str(ws / "proj" / entry)) is False


@pytest.mark.parametrize("entry", RUN_LATER)
def test_every_run_later_entry_is_refused_for_a_grant_anywhere(ws, entry):
    refuse = self_protect.grant_write_refused
    home = ws / "proj"
    for base in (home, home / "sub" / "module", checkout(ws / "stira")):
        assert refuse(str(base / entry)) is True
        assert refuse(str(base / entry), str(home)) is True
        assert refuse(str(base / entry.upper())) is True
        assert refuse(str(base / "x" / ".." / "." / entry) + os.sep) is True
        assert refuse(str(base / entry) + ".") is True  # a trailing dot names the same file
        if entry not in _FILES:
            assert refuse(str(base / entry / "any" / "file")) is True
    relative = os.path.relpath(home / entry)
    assert not os.path.isabs(relative) and refuse(relative) is True


def test_what_a_self_development_grant_may_still_write(ws):
    """F4: an allow-list. Only ``docs/`` (less its protected entries) is writable."""
    stira = checkout(ws / "stira")
    refuse = self_protect.grant_write_refused
    for free in ("docs/grants.md", "docs/guide/new/page.md"):
        assert refuse(str(stira / free)) is False, free
        assert refuse(str(stira / free), str(stira)) is False, free
    for closed in (
        "README.md",
        "Makefile",
        "new.py",
        "examples/x.py",
        "dashboard/static/js/app.js",
        "dashboard/static/js/approvals.js",
        "dashboard/static/css/new.css",
        "dashboard/static/i18n/catalog.en.json",
        "dashboard/templates/settings.html",
        "dashboard/stage/js/activity.js",
        "dashboard/conftest.py",
        "dashboard/README.md",
        "src/stiracore/__init__.py",
        "src/stiracore/core/__init__.py",
        "src/stiracore/tools/memory_store.py",
        "src/stiracore/tools/checked_open.py",
        "src/stiracore/agent/context_assembler.py",
        "src/stiracore/grantsx/new.py",
        "src/other/new.py",
        "pytest.ini",
        "docs/eval/x.md",
        "docs/adr/0099-new.md",
        "docs/proposer-edit-boundary.md",
        "docs/conftest.py",
        # a self-development grant sits on the checkout: its data places are not its own
        ".local/stira/gateway-config.json",
        "workspace/x.py",
    ):
        assert refuse(str(stira / closed)) is True, closed
        assert refuse(str(stira / closed), str(stira)) is True, closed
    assert refuse(str(ws / "proj" / "src" / "app" / "main.py")) is False


@symlinks
def test_links_do_not_reach_a_protected_file(ws):
    stira = checkout(ws / "stira")
    (stira / "src" / "stiracore" / "tools").mkdir()
    policy = stira / "src" / "stiracore" / "tools" / "tool_policy.py"
    policy.write_text("RULES = {}\n")
    refuse = self_protect.grant_write_refused
    (stira / "docs" / "t").symlink_to(policy.parent, target_is_directory=True)
    assert refuse(str(stira / "docs" / "t" / "tool_policy.py")) is True  # resolved
    os.link(policy, stira / "docs" / "free.py")
    assert refuse(str(stira / "docs" / "free.py")) is True  # a second name of a file
    (ws / "proj" / "hooks").symlink_to(ws / "proj" / "sub", target_is_directory=True)
    (ws / "proj" / ".git").mkdir()
    (ws / "proj" / ".git" / "hooks").symlink_to(ws / "proj" / "src", target_is_directory=True)
    assert refuse(str(ws / "proj" / ".git" / "hooks" / "pre-commit")) is True  # as spelled


def _create(path, n=1):
    return use("editor", {"command": "create", "path": str(path), "file_text": "changed\n"}, n)


def _iterate(env, *steps):
    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


@pytest.mark.parametrize(
    "target",
    [
        "src/stiracore/tools/tool_policy.py",
        "src/stiracore/grants/schema.py",
        "tests/attack/test_new.py",
        "ci/gate.sh",
        "dashboard/stira_server.py",
        ".github/workflows/ci.yml",
        "src/stiracore/tools/page_check_guard.py",
        # F2 / F4: the served UI, import-time code, the write floor, evaluator overrides
        "dashboard/static/js/approvals.js",
        "dashboard/templates/settings.html",
        "dashboard/conftest.py",
        "src/stiracore/__init__.py",
        "src/stiracore/tools/checked_open.py",
        "src/stiracore/tools/memory_store.py",
        "pytest.ini",
        "docs/conftest.py",
        "README.md",
        ".git/config",
    ],
)
def test_a_self_development_grants_editor_cannot_write_the_list(tmp_path, target):
    env = _self_dev_env(tmp_path)
    file = env.stira / target
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("original\n")
    body = _self_dev_body(env, env.stira, self_development=True)
    env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    assert grant(env).workspace_subtree == os.path.realpath(env.stira)
    view = _iterate(env, _create(file))
    assert (view["end"], view["detail"]) == ("tool_refused", "protected_path")
    assert file.read_text() == "original\n"
    assert (grant(env).iterations, env.broker.requests) == (1, [])  # refused, no card


def test_a_self_development_grant_writes_and_reads_the_rest(tmp_path):
    env = _self_dev_env(tmp_path)
    (env.stira / "tests").mkdir()
    (env.stira / "tests" / "test_x.py").write_text("assert True\n")
    body = _self_dev_body(env, env.stira, self_development=True)
    env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    view = _iterate(
        env,
        use("editor", {"command": "view", "path": str(env.stira / "tests" / "test_x.py")}),
        _create(env.stira / "docs" / "note.md", 2),
    )
    assert view["end"] != "tool_refused", view
    assert (env.stira / "docs" / "note.md").read_text() == "changed\n"


@pytest.mark.parametrize("target", RUN_LATER)
def test_run_later_files_are_refused_in_a_normal_grant(tmp_path, target):
    env = make_env(tmp_path)
    file = env.sub / target
    file.parent.mkdir(parents=True, exist_ok=True)
    view = _iterate(env, _create(file))
    assert (view["end"], view["detail"]) == ("tool_refused", "protected_path")
    assert not file.exists() and env.broker.requests == []
    (tmp_path / "second").mkdir()
    env2 = make_env(tmp_path / "second")  # the control: a normal file is written
    view = _iterate(env2, _create(env2.sub / "main.py"), _create(env2.sub / ".gitignore", 2))
    assert view["end"] != "tool_refused" and (env2.sub / "main.py").exists()
    assert (env2.sub / ".gitignore").exists()


def test_the_tool_itself_checks_every_write_again(tmp_path, monkeypatch):
    """The check at the execution boundary (``GrantTool`` → ``_check_tool``), without the
    hook before it: arguments that changed after the hook are judged where the tool runs."""
    from stiracore.grants import executor

    monkeypatch.setattr(executor.IterationGuard, "before_tool", lambda self, event: None)
    env = _self_dev_env(tmp_path)
    body = _self_dev_body(env, env.stira, self_development=True)
    env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    view = _iterate(env, _create(env.stira / "docs" / "note.md"))
    assert view["end"] != "tool_refused" and (env.stira / "docs" / "note.md").exists()
    file = env.stira / "README.md"  # outside the allow-list; no card is involved
    view = _iterate(env, _create(file))
    assert (view["end"], view["detail"]) == ("tool_refused", "protected_path")
    assert not file.exists() and env.broker.requests == []


def test_an_unjudgeable_write_is_refused_in_a_grant(tmp_path, monkeypatch):
    env = make_env(tmp_path)

    def broken(*_args):
        raise OSError("unreadable")

    monkeypatch.setattr(self_protect, "grant_write_refused", broken)
    view = _iterate(env, _create(env.sub / "main.py"))
    assert (view["end"], view["detail"]) == ("tool_refused", "protected_path")
    assert not (env.sub / "main.py").exists()


def test_the_detail_code_is_on_every_closed_list():
    """``protected_path`` is the executor's, the ledger's and the board's (codes only)."""
    from stiracore.grants import executor, round_ledger

    assert "protected_path" in executor.DETAIL_CODES
    assert "protected_path" in round_ledger.DETAIL_CODES
    page = (ROOT / "dashboard" / "stage" / "js" / "activity.js").read_text(encoding="utf-8")
    codes = re.search(r"const DETAIL_CODES = new Set\(\[([^\]]*)\]\)", page).group(1)
    assert "'protected_path'" in codes


# --- always a person: run-later files at the tool gate ------------------------------------------


class _Yolo:
    """A YOLO approver that would approve everything it is asked."""

    def __init__(self):
        self.asked = []

    def approve(self, **kw):
        self.asked.append(kw["tool_name"])
        return SimpleNamespace(intent="i" * 16), "ok"

    def void(self, *args, **kw):
        return None


class _Allowance:
    def __init__(self):
        self.asked = []

    def answer(self, **kw):
        self.asked.append(kw["tool"])
        return SimpleNamespace(nonce="n" * 32)

    def terminal(self, *args, **kw):
        return None


class Gate:
    def __init__(self, tmp_path, monkeypatch, rule):
        self.ws = tmp_path / "ws"
        self.ws.mkdir()
        scope = WritePathPolicy(base_dir=self.ws, write_roots=[WORKSPACE_ROOT])
        engine = StiraEngine(config=StiraConfig())
        self.cards = []

        def card(request):
            self.cards.append(request.tool_name)
            return HITLDecision(action=HITLAction.REJECT, reason="test person says no")

        engine.on_hitl_request = card
        self.hooks = CheckpointHooks(
            engine, policy_check=lambda _name: (rule, 7), scope_policy=scope
        )
        self.yolo, self.allowance = _Yolo(), _Allowance()
        self.hooks.set_auto_approver(self.yolo)
        self.hooks.set_allow_approver(self.allowance)
        monkeypatch.setattr(checkpoint_hooks, "_allow_origin", lambda _agent_id: "chat_turn")

    def call(self, name, path, **more):
        arguments = {"path": str(path), **more}
        if name == "editor":
            arguments.setdefault("command", "create")
        event = SimpleNamespace(
            tool_use={"name": name, "toolUseId": secrets.token_hex(8), "input": arguments},
            cancel_tool=None,
            invocation_state={},
        )
        self.hooks.before_tool(event)
        return event


@pytest.mark.parametrize("tool", ["editor", "file_write"])
@pytest.mark.parametrize("target", [*RUN_LATER, ".GIT/Hooks/pre-push", "sub/.github/workflows/a"])
def test_a_run_later_write_is_a_card_even_when_the_rule_is_always(
    tmp_path, monkeypatch, tool, target
):
    gate = Gate(tmp_path, monkeypatch, "always")
    event = gate.call(tool, gate.ws / target)
    assert gate.cards == [tool] and event.cancel_tool  # asked; the person said no
    assert gate.yolo.asked == [] and gate.allowance.asked == []
    relative = gate.call(tool, target)  # anchored at the workspace
    assert gate.cards == [tool, tool] and relative.cancel_tool
    for spelling in (f"./x/../{target}/", f"{target}.", str(gate.ws / target).upper()):
        gate.call(tool, spelling)
    assert gate.cards == [tool] * 5
    # The control: an ordinary file under the same rule runs without a card.
    for ordinary in ("src/main.py", ".gitignore", "git/hooks/x", "github/workflows.py", "a.code"):
        plain = gate.call(tool, gate.ws / ordinary)
        assert gate.cards == [tool] * 5 and plain.cancel_tool is None, ordinary


def test_a_tool_that_saves_a_file_where_an_argument_points_is_on_the_same_gate(
    tmp_path, monkeypatch
):
    """``speak`` (Polly mode) writes ``output_path``: the same card as ``file_write``."""
    gate = Gate(tmp_path, monkeypatch, "always")
    for target in (".git/hooks/pre-commit", ".git/config", "x.code-workspace"):
        assert self_protect.person_required("speak", {"text": "hi", "output_path": target})
    assert not self_protect.person_required("speak", {"text": "hi", "output_path": "out.mp3"})
    assert not self_protect.person_required("speak", {"text": ".git/config"})  # no path
    event = SimpleNamespace(
        tool_use={
            "name": "speak",
            "toolUseId": secrets.token_hex(8),
            "input": {"text": "hi", "output_path": str(gate.ws / ".git" / "config")},
        },
        cancel_tool=None,
        invocation_state={},
    )
    gate.hooks.before_tool(event)
    assert gate.cards == ["speak"] and gate.yolo.asked == [] and gate.allowance.asked == []


@pytest.mark.parametrize("tool", ["editor", "file_write"])
@pytest.mark.parametrize("target", RUN_LATER)
def test_in_a_yolo_window_or_under_an_allowance_a_run_later_write_is_a_card(
    tmp_path, monkeypatch, tool, target
):
    gate = Gate(tmp_path, monkeypatch, "ask")
    gate.call(tool, gate.ws / target)
    assert gate.cards == [tool]
    assert gate.yolo.asked == [] and gate.allowance.asked == []
    # The control: the YOLO approver answers an ordinary write (no card) ...
    plain = gate.call(tool, gate.ws / "main.py")
    assert gate.yolo.asked == [tool] and gate.cards == [tool] and plain.cancel_tool is None
    # ... and with YOLO disconnected the allowance does.
    gate.hooks.set_auto_approver(None)
    gate.call(tool, gate.ws / "main.py")
    assert gate.allowance.asked == [tool] and gate.cards == [tool]
    gate.call(tool, gate.ws / target)
    assert gate.allowance.asked == [tool] and gate.cards == [tool, tool]


def test_reading_a_run_later_file_is_not_a_write(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, "always")
    for command in ("view", "find_line"):
        event = gate.call("editor", gate.ws / ".git" / "hooks" / "pre-commit", command=command)
        assert event.cancel_tool is None
    gate.call("file_read", gate.ws / ".git" / "hooks" / "pre-commit")
    assert gate.cards == []
    assert self_protect.person_required("editor", {"command": "insert", "path": ".husky/x"})
    assert not self_protect.person_required("editor", {"command": "insert", "path": "husky/x"})
    assert not self_protect.person_required("editor", {"command": "create"})  # no path


@symlinks
def test_a_link_to_the_hooks_directory_needs_a_person_too(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, "always")
    (gate.ws / ".git" / "hooks").mkdir(parents=True)
    (gate.ws / "h").symlink_to(gate.ws / ".git" / "hooks", target_is_directory=True)
    gate.call("file_write", gate.ws / "h" / "pre-commit")
    assert gate.cards == ["file_write"]


def test_a_path_that_cannot_be_judged_needs_a_person(monkeypatch):
    def broken(_path, _patterns):
        raise OSError("unreadable")

    monkeypatch.setattr(self_protect, "_matches", broken)
    assert self_protect.person_required("file_write", {"path": "a.py"}) is True


def test_a_docs_subfolder_of_a_checkout_is_not_an_ordinary_project_folder(ws, monkeypatch):
    """Review #791 N1: ``docs/research`` of a Stira checkout needs the opt-in and the tick
    like the rest of the checkout (it is not the running checkout here, so the tick works)."""
    stira = repo(ws / "other-stira")
    (stira / "docs" / "research").mkdir(parents=True)
    with refused("grant_subtree_protected"):
        check_subtree(str(stira / "docs" / "research"), [ws])
    assert check_subtree(str(stira / "docs" / "research"), [ws], self_development=True)
