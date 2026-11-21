"""Strict validation of grants (ADR 0011 §2, §8; W4 of #296).

Two shapes are validated here, nothing else:

* the **issue request** a person sends from the dashboard (:func:`parse_issue`);
* the **grant record** as the store holds it (:func:`grant_from_dict` /
  :func:`grant_to_dict`).

Rules (each pinned by ``tests/test_grants_schema.py``):

* exactly the known keys — an unknown or missing key is refused, never ignored;
* exactly built-in values (``type(x) is int`` / ``str`` / ``list`` / ``dict``; a ``bool`` is
  not an ``int``, no subclass is evaluated);
* ``kind`` is ``"goal"``: the discriminator against YOLO windows (``kind="yolo"``), checked
  by :func:`stiracore.authority.require_kind` wherever a record is used;
* ``scope.tools`` is a non-empty subset of :data:`TOOLS` (``view_image`` only together
  with ``page_look``); ``scope.workspace_subtree`` is
  an existing directory inside the agent's write roots, resolved (no symlink escape:
  :func:`check_subtree`);
* ``budget`` ``{total_tokens, per_tick_tokens}``, ``ttl_sec`` (default
  :data:`TTL_DEFAULT_SEC`, cap :data:`TTL_CAP_SEC`), ``max_iterations`` (default
  :data:`MAX_ITERATIONS_DEFAULT`, cap :data:`MAX_ITERATIONS_CAP`), ``no_progress_ticks``
  (default :data:`NO_PROGRESS_DEFAULT`), ``verifier`` ``"user"`` — or ``"checks"`` exactly
  together with ``done_when`` (#683, :func:`check_done_when`: a non-empty set of
  ``run_checks`` kinds, run by the harness at a claim on the subtree; needs ``run_checks``
  in ``scope.tools``);
* ``report_files`` (ADR 0028 §4, default none): at most :data:`REPORT_FILES_MAX` relative
  file paths inside the subtree that the progress fingerprint ignores
  (:func:`check_report_files`: no glob, no directory, no ``..``, no symlink);
* the **goal** is free text, but only for the grant store: it is refused when
  :func:`stiracore.observability.masking.mask_secrets` would change it, and no function
  here puts it into an error, a code or a log.

Every refusal is a :class:`GrantRefused` with a fixed code of :data:`CODES`.
"""

from __future__ import annotations

import math
import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

__all__ = [
    "CHECK_COUNT_KEYS",
    "CHECK_KINDS",
    "CHECK_STATUSES",
    "CLAIM_CHECKS",
    "CLAIM_PROGRESS",
    "CLAIM_ROWS_KEPT",
    "CLAIM_WORKSPACE",
    "CODES",
    "GOAL_MAX_CHARS",
    "ISSUE_WORD",
    "KIND",
    "MAX_ITERATIONS_CAP",
    "MAX_ITERATIONS_DEFAULT",
    "NO_PROGRESS_CAP",
    "NO_PROGRESS_DEFAULT",
    "PAGE_TOOLS",
    "REPORT_FILES_MAX",
    "RESUME_WORD",
    "SHOW_WORD",
    "TOOLS",
    "TOTAL_TOKENS_CAP",
    "TTL_CAP_SEC",
    "TTL_DEFAULT_SEC",
    "TTL_MIN_SEC",
    "VERIFIERS",
    "BoardBusy",
    "ClaimEvidence",
    "Grant",
    "GrantRefused",
    "IssueRequest",
    "check_done_when",
    "check_goal",
    "check_report_files",
    "check_subtree",
    "evidence_from_dict",
    "evidence_to_dict",
    "grant_from_dict",
    "grant_to_dict",
    "parse_issue",
]

KIND = "goal"
# The scope allow-list (ADR 0011 §8, W5 brief): read tools, the editor and run_checks —
# and (ADR 0015 rev 6, S5) the page tools of :data:`PAGE_TOOLS`. ``page_check`` and
# ``page_review`` are not nameable.
PAGE_TOOLS = frozenset({"page_look", "view_image"})
TOOLS = frozenset(
    {
        "file_read",
        "editor",
        "run_checks",
        "memory_read",
        "memory_search",
        "memory_list",
        "current_time",
        *PAGE_TOOLS,
    }
)
# ``user``: a person verifies a claim (v1). ``checks`` (#683): the harness first runs the
# grant's ``done_when`` checks itself, then a person verifies — exactly with ``done_when``.
VERIFIERS = frozenset({"user", "checks"})
TTL_DEFAULT_SEC = 7200
TTL_CAP_SEC = 21600
TTL_MIN_SEC = 60
MAX_ITERATIONS_DEFAULT = 10
MAX_ITERATIONS_CAP = 20
NO_PROGRESS_DEFAULT = 3
NO_PROGRESS_CAP = 20
TOTAL_TOKENS_CAP = 5_000_000
GOAL_MAX_CHARS = 2000
SUBTREE_MAX_CHARS = 1024
# ADR 0028 §4: the files the progress fingerprint ignores (the person's list, at issue).
REPORT_FILES_MAX = 8
REPORT_FILE_MAX_CHARS = 255
# W6c: the evidence a completion claim carries (codes only, harness-measured).
CLAIM_CHECKS = ("pass", "fail", "not_run", "unknown")
CLAIM_WORKSPACE = ("changed", "unchanged", "unknown")
CLAIM_PROGRESS = ("new", "repeat", "none", "unknown")
# run_checks' kinds (``tools.run_checks.KINDS``, kept equal by a test) — anything else is
# ``other``; its per-check statuses; the counts a row keeps (clamped).
CHECK_KINDS = ("pytest", "unittest", "node_test", "ruff", "compileall")
CHECK_STATUSES = ("passed", "failed", "error", "limited")
CHECK_COUNT_KEYS = ("passed", "failed", "errors", "skipped", "findings")
CHECK_COUNT_CAP = 1_000_000
CLAIM_ROWS_KEPT = 16
ISSUE_WORD = "ISSUE"
RESUME_WORD = "RESUME"
SHOW_WORD = "SHOW"

_HEX32 = re.compile(r"[0-9a-f]{32}")
# Control characters other than tab / newline are refused in a goal.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Never in a report file path: control characters, glob characters, a backslash.
_REPORT_REFUSED = re.compile(r"[\x00-\x1f\x7f*?\[\]{}\\]")
# ... and at issue (#761): these Unicode categories and the slash / backslash lookalikes.
_REPORT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_REPORT_LOOKALIKES = frozenset("\u2044\u2215\u2216\u29f5\u29f8\ufe68\uff0f\uff3c")

# Refusal code -> HTTP status. The page maps each code to its wording (grants.code_<code>).
CODES = {
    "grant_body_invalid": 400,
    "grant_field_unknown": 400,
    "grant_field_missing": 400,
    "grant_field_invalid": 400,
    "grant_goal_invalid": 400,
    "grant_goal_secret": 400,
    "grant_tool_invalid": 400,
    "grant_subtree_invalid": 400,
    "grant_subtree_outside": 400,
    # #791 (ADR 0029 Q3 / B6): the subtree overlaps the protected part of a Stira checkout,
    # holds a checkout or lies inside a run-later directory (or could not be judged); it is
    # the running server's own checkout or package (refused also with the option); the
    # confirmation ``self_development`` was sent while the user setting
    # ``autonomy.grant_self_development`` is off.
    "grant_subtree_protected": 400,
    "grant_subtree_running_checkout": 400,
    "grant_self_development_off": 409,
    "grant_verifier_invalid": 400,
    "grant_cap_exceeded": 400,
    "grant_confirm_mismatch": 400,
    "grant_run_checks_setting_off": 409,
    "grant_run_checks_not_contained": 409,
    # ADR 0015 rev 6 (S5): a scope that names ``page_look`` — the user setting
    # ``autonomy.page_look_unattended`` is off; ``page_look`` is not registered on this
    # server (no browser); the axis check is not ``pass`` (``reason``: its fixed code).
    "grant_page_look_setting_off": 409,
    "grant_page_look_unavailable": 409,
    "grant_page_look_axis": 409,
    "grant_already_active": 409,
    "grant_unknown": 404,
    "grant_terminal": 409,
    "grant_not_active": 409,
    "grant_not_stopped": 409,
    "grant_not_awaiting": 409,
    "grant_still_dangerous": 409,
    "grant_budget_exhausted": 409,
    "grant_resume_needs_new_grant": 409,
    "grant_clock_unavailable": 409,
    "grant_store_unhealthy": 503,
    "grant_store_timeout": 503,
    "grant_issue_aborted_epoch": 409,
    "grant_authority_killed": 409,
    "grant_authority_unhealthy": 503,
    "grant_breaker_not_normal": 409,
    "grant_view_refused": 403,
    "grant_confirmation_required": 428,
    "grant_operation_refused": 409,
    # W5: a "Run one iteration" press that was not admitted (``reason``: a fixed code).
    "grant_iteration_refused": 409,
    # W6d: a grant asked for the heartbeat start while autonomy.heartbeat_start is off.
    "grant_heartbeat_start_off": 409,
}


class GrantRefused(Exception):
    """A refused grant operation; ``code`` is a key of :data:`CODES` (never free text)."""

    def __init__(self, code: str, **extra: Any) -> None:
        if code not in CODES:
            code = "grant_body_invalid"
        super().__init__(code)
        self.code = code
        self.status = CODES[code]
        self.extra = extra


class BoardBusy(RuntimeError):
    """W5b: a lock on the Stage board's read path was held — the board read never waits;
    ``/api/activity`` answers ``grant: {"error": "grant_busy"}`` instead."""

    board_busy = True

    def __init__(self) -> None:
        super().__init__("grant_busy")


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a huge int
        return False


def _int(value: Any, low: int, high: int) -> int:
    if type(value) is not int:
        raise GrantRefused("grant_field_invalid")
    if value < low:
        raise GrantRefused("grant_field_invalid")
    if value > high:
        raise GrantRefused("grant_cap_exceeded")
    return value


def _keys(data: Any, required: Iterable[str], optional: Iterable[str] = ()) -> dict:
    if type(data) is not dict:
        raise GrantRefused("grant_body_invalid")
    required, optional = set(required), set(optional)
    for key in data:
        if type(key) is not str or key not in required | optional:
            raise GrantRefused("grant_field_unknown")
    if not required <= set(data):
        raise GrantRefused("grant_field_missing")
    return data


def check_goal(goal: Any) -> str:
    """The goal as given, if it is storable: non-empty text, at most
    :data:`GOAL_MAX_CHARS`, no control characters, and **unchanged by** ``mask_secrets``
    (a goal the masker would change is refused — ADR 0011 §7 — never stored masked)."""
    from stiracore.observability.masking import mask_secrets

    if type(goal) is not str or not goal.strip() or len(goal) > GOAL_MAX_CHARS:
        raise GrantRefused("grant_goal_invalid")
    if _CONTROL.search(goal):
        raise GrantRefused("grant_goal_invalid")
    if mask_secrets(goal) != goal:
        raise GrantRefused("grant_goal_secret")
    return goal


def _subtree_verdict(resolved: str) -> str:
    """#791: how ``resolved`` relates to Stira's own code — a ``self_protect`` verdict. A
    check that cannot judge (an error, an answer outside the closed set) answers ``""``:
    the caller refuses, with or without the option (fail closed)."""
    from stiracore import self_protect

    try:
        verdict = self_protect.subtree_verdict(resolved)
    except Exception:  # noqa: BLE001 - unreadable, too large: refused
        return ""
    return verdict if type(verdict) is str and verdict in self_protect.VERDICTS else ""


def _checked_subtree(
    raw: Any, roots: Iterable[str | Path], policy: Any, self_development: bool
) -> tuple[str, bool]:
    """:func:`check_subtree`'s resolved path, and whether the option was what let it
    pass (the subtree overlaps the protected part of a checkout that is not the running
    server's)."""
    if type(raw) is not str or not raw or len(raw) > SUBTREE_MAX_CHARS or "\0" in raw:
        raise GrantRefused("grant_subtree_invalid")
    if not os.path.isabs(raw):
        raise GrantRefused("grant_subtree_invalid")
    try:
        info = os.lstat(raw)
        if stat.S_ISLNK(info.st_mode):
            raise GrantRefused("grant_subtree_invalid")
        resolved = os.path.realpath(raw)
        if len(resolved) > SUBTREE_MAX_CHARS:
            # The stored (resolved) path obeys the same cap as the reader's.
            raise GrantRefused("grant_subtree_invalid")
        if not stat.S_ISDIR(os.stat(resolved).st_mode):
            raise GrantRefused("grant_subtree_invalid")
    except OSError as error:
        raise GrantRefused("grant_subtree_invalid") from error
    inside = False
    for root in roots:
        try:
            real_root = os.path.realpath(os.fspath(root))
        except (OSError, TypeError, ValueError):
            continue
        if resolved == real_root or resolved.startswith(real_root.rstrip(os.sep) + os.sep):
            inside = True
            break
    if not inside:
        raise GrantRefused("grant_subtree_outside")
    if policy is not None:
        try:
            decision = policy.check(os.path.join(resolved, ".stira-grant-probe"))
        except Exception as error:  # a policy that cannot judge refuses
            raise GrantRefused("grant_subtree_outside") from error
        if not getattr(decision, "allowed", False):
            raise GrantRefused("grant_subtree_outside")
    from stiracore import self_protect

    verdict = _subtree_verdict(resolved)
    if verdict == self_protect.FREE:
        return resolved, False
    if self_development is not True or verdict not in (
        self_protect.CHECKOUT,
        self_protect.RUNNING,
    ):
        raise GrantRefused("grant_subtree_protected")
    if verdict == self_protect.RUNNING:
        raise GrantRefused("grant_subtree_running_checkout")
    return resolved, True


def check_subtree(
    raw: Any, roots: Iterable[str | Path], policy: Any = None, *, self_development: bool = False
) -> str:
    """``raw`` resolved, if it is an existing directory inside one of ``roots``.

    #791 (ADR 0029 Q3 / B6): a subtree that overlaps the protected part of a Stira
    checkout (``stiracore.self_protect``: outside the checkout's data places and its
    allow-list, or inside or around a never-editable entry) or holds a checkout is
    refused (``grant_subtree_protected``; also when that cannot be judged, and — with or
    without the option — inside a run-later directory) unless ``self_development`` is
    exactly ``True`` — only :func:`parse_issue` passes it, for a person's issue with the
    user setting on and the separate confirmation ticked. The running server's own
    checkout or package is refused also then (``grant_subtree_running_checkout``).

    The path must be absolute and its **resolved** form (every symlink followed) must lie
    inside a resolved root; the final component must not be a symlink. The resolved path
    is what the grant keeps, so a symlink in the given path can never be re-pointed later.
    ``policy`` (the server's ``WritePathPolicy``) additionally applies the write floor
    (identity / data directory / secret paths): a subtree the agent could not write to is
    refused. Codes: ``grant_subtree_invalid`` / ``grant_subtree_outside``."""
    return _checked_subtree(raw, roots, policy, self_development)[0]


def _report_path(value: Any) -> bool:
    """The shape of one report file path (no file system read): a ``/``-separated
    relative path of plain names — not empty, at most :data:`REPORT_FILE_MAX_CHARS`, no
    control or glob character, no backslash, not absolute, no ``~``, and no empty, ``.``
    or ``..`` component (so no trailing ``/``: a file, never a directory)."""
    if type(value) is not str or not value or len(value) > REPORT_FILE_MAX_CHARS:
        return False
    if _REPORT_REFUSED.search(value) or value.startswith("~") or os.path.isabs(value):
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def _report_name_plain(value: str) -> bool:
    """The stricter shape of a report file path **at issue** (#761; no file system read):
    NFC, no character of the categories ``Cc`` / ``Cf`` / ``Cs`` / ``Zl`` / ``Zp`` (C1
    controls, bidi and other format characters, lone surrogates, line / paragraph
    separators), no character that looks like a slash or backslash, and no component that
    ends with a space or a dot. The store's reader keeps :func:`_report_path` only, so a
    record issued before this check still loads."""
    if unicodedata.normalize("NFC", value) != value:
        return False
    for char in value:
        if char in _REPORT_LOOKALIKES or unicodedata.category(char) in _REPORT_CATEGORIES:
            return False
    return all(part[-1] != "." and not part[-1].isspace() for part in value.split("/"))


def check_report_files(raw: Any, subtree: str) -> tuple[str, ...]:
    """The report files as given (ADR 0028 §4), if every one names a file inside
    ``subtree`` (the resolved subtree of :func:`check_subtree`).

    At most :data:`REPORT_FILES_MAX` (``grant_cap_exceeded``); anything else is
    ``grant_field_invalid``: not a list of :func:`_report_path` shapes (and, at issue,
    :func:`_report_name_plain`), a duplicate (compared without case, #761), a name
    ``mask_secrets`` would change, a path with a symlink anywhere in it (its resolved form
    must be the path itself, so it cannot leave the subtree), or an existing path that is
    not a regular file (a directory, a FIFO). A file that does not exist yet is accepted.
    No path is put into an error."""
    from stiracore.observability.masking import mask_secrets

    if type(raw) is not list:
        raise GrantRefused("grant_field_invalid")
    if len(raw) > REPORT_FILES_MAX:
        raise GrantRefused("grant_cap_exceeded")
    for value in raw:
        if not _report_path(value) or not _report_name_plain(value):
            raise GrantRefused("grant_field_invalid")
        if mask_secrets(value) != value:
            raise GrantRefused("grant_field_invalid")
        target = os.path.join(subtree, *value.split("/"))
        try:
            if os.path.realpath(target) != target:
                raise GrantRefused("grant_field_invalid")
            info = os.lstat(target)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise GrantRefused("grant_field_invalid") from error
        if not stat.S_ISREG(info.st_mode):
            raise GrantRefused("grant_field_invalid")
    if len({value.casefold() for value in raw}) != len(raw):
        raise GrantRefused("grant_field_invalid")
    return tuple(raw)


#: #683: at most as many kinds as one ``run_checks`` call runs (``run_checks.MAX_CHECKS``;
#: a test pins the two equal) — more could never be met.
DONE_WHEN_MAX = 4


def check_done_when(raw: Any) -> tuple[str, ...]:
    """#683: the grant's completion checks as given — a non-empty list of distinct
    ``run_checks`` kinds (:data:`CHECK_KINDS`, at most :data:`DONE_WHEN_MAX`), returned
    sorted. A closed list: no path, no threshold, no free text (``grant_field_invalid`` for
    anything else)."""
    if type(raw) is not list or not raw or len(raw) > DONE_WHEN_MAX:
        raise GrantRefused("grant_field_invalid")
    if not all(type(kind) is str and kind in CHECK_KINDS for kind in raw):
        raise GrantRefused("grant_field_invalid")
    if len(set(raw)) != len(raw):
        raise GrantRefused("grant_field_invalid")
    return tuple(sorted(raw))


@dataclass(frozen=True)
class IssueRequest:
    """A validated issue request (the goal text is held separately by the caller)."""

    goal: str
    tools: tuple[str, ...]
    workspace_subtree: str
    total_tokens: int
    per_tick_tokens: int
    ttl_sec: int
    max_iterations: int
    no_progress_ticks: int
    verifier: str
    # W6d: the scheduler's grant_tick may start this grant's iterations (default off).
    heartbeat_start: bool = False
    # ADR 0028 §4: relative paths the progress fingerprint ignores (default none).
    report_files: tuple[str, ...] = ()
    # #791: the subtree is a Stira checkout, issued with the setting on and the person's
    # separate confirmation (the host summary names it: ``loosen:self_development``).
    self_development: bool = False
    # #683: the run_checks kinds the harness runs at a claim (``verifier`` ``checks``).
    done_when: tuple[str, ...] = ()

    def __repr__(self) -> str:  # never the goal
        return f"IssueRequest(tools={self.tools!r}, ttl_sec={self.ttl_sec})"


_ISSUE_REQUIRED = ("goal", "scope", "budget", "verifier", "confirm")
_ISSUE_OPTIONAL = (
    "ttl_sec",
    "max_iterations",
    "no_progress_ticks",
    "heartbeat_start",
    "report_files",
    "self_development",
    "done_when",
)


def parse_issue(
    body: Any,
    *,
    roots: Iterable[str | Path],
    policy: Any = None,
    self_development: bool = False,
) -> IssueRequest:
    """Validate the issue body. Typed confirmation: ``confirm`` must be exactly
    :data:`ISSUE_WORD`. The containment floor for ``run_checks`` is checked by the
    service (it needs the live settings), not here.

    #791: ``self_development`` is the user setting ``autonomy.grant_self_development``
    (default off). The body's own ``self_development`` (the person's separate
    confirmation, exactly ``true``) is refused while the setting is off
    (``grant_self_development_off``); a Stira checkout subtree is accepted only with both."""
    data = _keys(body, _ISSUE_REQUIRED, _ISSUE_OPTIONAL)
    if type(data["confirm"]) is not str or data["confirm"] != ISSUE_WORD:
        raise GrantRefused("grant_confirm_mismatch")
    if type(data["verifier"]) is not str or data["verifier"] not in VERIFIERS:
        raise GrantRefused("grant_verifier_invalid")
    scope = _keys(data["scope"], ("tools", "workspace_subtree"))
    tools = scope["tools"]
    if type(tools) is not list or not tools or len(tools) > len(TOOLS):
        raise GrantRefused("grant_tool_invalid")
    for tool in tools:
        if type(tool) is not str or tool not in TOOLS:
            raise GrantRefused("grant_tool_invalid")
    if len(set(tools)) != len(tools):
        raise GrantRefused("grant_tool_invalid")
    if "view_image" in tools and "page_look" not in tools:
        # The narrowed view_image reads only what this grant's page_look calls wrote.
        raise GrantRefused("grant_tool_invalid")
    done_when = check_done_when(data["done_when"]) if "done_when" in data else ()
    if done_when and "run_checks" not in tools:
        # #683: the harness runs them under the grant's own run_checks scope only.
        raise GrantRefused("grant_field_invalid")
    if (data["verifier"] == "checks") != bool(done_when):
        raise GrantRefused("grant_verifier_invalid")
    budget = _keys(data["budget"], ("total_tokens", "per_tick_tokens"))
    total = _int(budget["total_tokens"], 1, TOTAL_TOKENS_CAP)
    per_tick = _int(budget["per_tick_tokens"], 1, total)
    ttl = _int(data.get("ttl_sec", TTL_DEFAULT_SEC), TTL_MIN_SEC, TTL_CAP_SEC)
    iterations = _int(data.get("max_iterations", MAX_ITERATIONS_DEFAULT), 1, MAX_ITERATIONS_CAP)
    no_progress = _int(data.get("no_progress_ticks", NO_PROGRESS_DEFAULT), 1, NO_PROGRESS_CAP)
    heartbeat = data.get("heartbeat_start", False)
    if type(heartbeat) is not bool:
        raise GrantRefused("grant_field_invalid")
    ticked = data.get("self_development", False)
    if type(ticked) is not bool:
        raise GrantRefused("grant_field_invalid")
    if ticked and self_development is not True:
        raise GrantRefused("grant_self_development_off")
    goal = check_goal(data["goal"])
    subtree, loosened = _checked_subtree(scope["workspace_subtree"], roots, policy, ticked)
    report_files = check_report_files(data.get("report_files", []), subtree)
    return IssueRequest(
        goal=goal,
        tools=tuple(sorted(tools)),
        workspace_subtree=subtree,
        total_tokens=total,
        per_tick_tokens=per_tick,
        ttl_sec=ttl,
        max_iterations=iterations,
        no_progress_ticks=no_progress,
        verifier=data["verifier"],
        heartbeat_start=heartbeat,
        report_files=report_files,
        self_development=loosened,
        done_when=done_when,
    )


# --- the record -------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimEvidence:
    """W6c: what the harness observed when the agent claimed completion — **codes and
    counters only**, never a text the model wrote, never the goal, a path, a digest or a
    file name. One per grant, replaced (never appended) by the next claim; kept after the
    person's verification as the audit trail (read-only).

    Taken at the claim (:func:`.state.claim`, under the service's lock with the clock
    reading of that instant): ``claimed_at`` (wall seconds), the iteration ``k`` of
    ``max_iterations``, tokens used and budget left, TTL left. Measured at the claiming
    iteration's end and published only if nothing changed up to the publication
    (:meth:`.service.GrantService.measure_claim`; ``unknown`` until then and whenever
    something could not be read — never ``pass``): ``checks`` (:data:`CLAIM_CHECKS`, fail
    closed: the combined verdict of every ``run_checks`` call since the grant's last
    editor write — any fail ``fail``, anything unreadable or inconsistent ``unknown``, an
    unread workspace never lets a recorded failure be forgotten — shown as ``pass`` /
    ``fail`` only for the workspace identity the checks ran on, else ``not_run`` /
    ``unknown``) with its per-check ``check_rows`` ``(kind, status, passed, failed, errors,
    skipped, findings)`` (only with ``pass`` / ``fail``), ``workspace``
    (:data:`CLAIM_WORKSPACE`, the W6a workspace identity against the previous claim's;
    the first claim: against the issue) and ``progress`` (:data:`CLAIM_PROGRESS`, the
    claiming iteration's W6a outcome class)."""

    claimed_at: float
    iteration: int
    max_iterations: int
    used_tokens: int
    budget_left: int
    ttl_left_sec: int
    checks: str = "unknown"
    check_rows: tuple[tuple, ...] = ()
    workspace: str = "unknown"
    progress: str = "unknown"


_EVIDENCE_INTS = ("iteration", "max_iterations", "used_tokens", "budget_left", "ttl_left_sec")
_EVIDENCE_KEYS = frozenset(
    {"claimed_at", *_EVIDENCE_INTS, "checks", "check_rows", "workspace", "progress"}
)


def check_row(row: Any) -> tuple | None:
    """A valid evidence row ``(kind, status, *counts)`` or None (the strict shape)."""
    if type(row) is not tuple or len(row) != 2 + len(CHECK_COUNT_KEYS):
        return None
    kind, status, *counts = row
    if kind not in (*CHECK_KINDS, "other") or status not in CHECK_STATUSES:
        return None
    if not all(type(c) is int and 0 <= c <= CHECK_COUNT_CAP for c in counts):
        return None
    return row


def evidence_consistent(evidence: ClaimEvidence) -> bool:
    """The codes are known ones, rows only with ``pass`` / ``fail`` (at most
    :data:`CLAIM_ROWS_KEPT`, every row valid), ``pass`` only when every row passed."""
    rows = evidence.check_rows
    if type(rows) is not tuple or len(rows) > CLAIM_ROWS_KEPT:
        return False
    if any(check_row(r) is None for r in rows):
        return False
    if evidence.checks not in CLAIM_CHECKS or evidence.workspace not in CLAIM_WORKSPACE:
        return False
    if evidence.progress not in CLAIM_PROGRESS:
        return False
    if evidence.checks in ("pass", "fail") and not rows:
        return False
    if evidence.checks not in ("pass", "fail") and rows:
        return False
    passed = all(r[1] == "passed" for r in rows)
    return not (evidence.checks == "pass" and not passed) and not (
        evidence.checks == "fail" and passed
    )


def evidence_to_dict(evidence: ClaimEvidence | None) -> dict | None:
    """The stored (and viewed) form: codes, counters, the claim instant."""
    if evidence is None:
        return None
    out: dict[str, Any] = {"claimed_at": evidence.claimed_at}
    out.update({name: getattr(evidence, name) for name in _EVIDENCE_INTS})
    out["checks"] = evidence.checks
    out["check_rows"] = [
        {"kind": r[0], "status": r[1], "counts": dict(zip(CHECK_COUNT_KEYS, r[2:]))}
        for r in evidence.check_rows
    ]
    out["workspace"] = evidence.workspace
    out["progress"] = evidence.progress
    return out


def evidence_from_dict(data: Any) -> ClaimEvidence | None:
    """The strict reader of :func:`evidence_to_dict` (``None`` stays ``None``); anything
    else raises :class:`GrantRefused` (``grant_body_invalid``)."""
    if data is None:
        return None
    if type(data) is not dict or set(data) != _EVIDENCE_KEYS:
        raise GrantRefused("grant_body_invalid")
    claimed = data["claimed_at"]
    if not _finite(claimed) or not 0 <= claimed <= 1e12:
        raise GrantRefused("grant_body_invalid")
    ints = {}
    for name in _EVIDENCE_INTS:
        value = data[name]
        if type(value) is not int or not 0 <= value < (1 << 53):
            raise GrantRefused("grant_body_invalid")
        ints[name] = value
    raw_rows = data["check_rows"]
    if type(raw_rows) is not list or len(raw_rows) > CLAIM_ROWS_KEPT:
        raise GrantRefused("grant_body_invalid")
    rows = []
    for item in raw_rows:
        if type(item) is not dict or set(item) != {"kind", "status", "counts"}:
            raise GrantRefused("grant_body_invalid")
        counts = item["counts"]
        if type(counts) is not dict or set(counts) != set(CHECK_COUNT_KEYS):
            raise GrantRefused("grant_body_invalid")
        row = check_row((item["kind"], item["status"], *(counts[k] for k in CHECK_COUNT_KEYS)))
        if row is None:
            raise GrantRefused("grant_body_invalid")
        rows.append(row)
    for name in ("checks", "workspace", "progress"):
        if type(data[name]) is not str:
            raise GrantRefused("grant_body_invalid")
    evidence = ClaimEvidence(
        claimed_at=float(claimed),
        checks=data["checks"],
        check_rows=tuple(rows),
        workspace=data["workspace"],
        progress=data["progress"],
        **ints,
    )
    if not evidence_consistent(evidence):
        raise GrantRefused("grant_body_invalid")
    return evidence


@dataclass(frozen=True)
class Grant:
    """One grant and its goal's state (ADR 0011 §2, §8). Immutable: every transition is a
    new record (:mod:`.state`). **The goal text is not here** — the service keeps it next
    to the record and only the store writes it, so no view, event, ``repr`` or error built
    from a record can contain it."""

    grant_id: str
    kind: str
    session_ref: str
    tools: tuple[str, ...]
    workspace_subtree: str
    total_tokens: int
    per_tick_tokens: int
    ttl_sec: int
    max_iterations: int
    no_progress_ticks: int
    verifier: str
    issuer: str
    issued_wall: float
    issued_boot: float
    issued_mono: float
    # The clock reading of the last check (backwards / forward jumps are judged from it).
    last_wall: float
    last_boot: float
    last_mono: float
    generation: int
    # Grant state: status (``state.GRANT_STATUSES``) + reason (fixed codes, "" for none).
    status: str
    reason: str
    # Where a resume returns to (the state before the stop).
    resume_status: str
    resume_reason: str
    # The goal (OpenClaw names + awaiting_verification / unmet).
    goal_status: str
    goal_reason: str
    # Where a goal pause returns to: "" (active) or "awaiting_verification" (ADR §8).
    goal_resume: str
    used_tokens: int
    iterations: int
    no_progress: int
    stop_epoch: int
    recheck: bool
    late_verification: str
    ended_wall: float
    # W6c: the evidence of the last completion claim (codes only), or None.
    claim_evidence: ClaimEvidence | None = None
    # W6d: issued with the heartbeat start (set at issue only, never changed afterwards;
    # stored only when true, so a record without it — every older one — reads as false).
    heartbeat_start: bool = False
    # ADR 0028 §4: the report files (set at issue only, never changed afterwards; stored
    # only when not empty, so a record without it — every older one — reads as none).
    report_files: tuple[str, ...] = ()
    # #791: issued as a self-development grant (the option and the person's separate
    # confirmation; set at issue only, never changed afterwards; stored only when true, so
    # a record without it — every older one — reads as false). For the audit: the write
    # rules do not read it.
    self_development: bool = False
    # #683: the completion checks (set at issue only, never changed afterwards; stored only
    # when not empty, so a record without it — every older one — has none; exactly with
    # ``verifier`` ``checks``).
    done_when: tuple[str, ...] = ()


_FIELD_TYPES = {f.name: f.type for f in fields(Grant)}
_STR = {
    "grant_id",
    "kind",
    "session_ref",
    "workspace_subtree",
    "verifier",
    "issuer",
    "status",
    "reason",
    "resume_status",
    "resume_reason",
    "goal_status",
    "goal_reason",
    "goal_resume",
    "late_verification",
}
_INT = {
    "total_tokens",
    "per_tick_tokens",
    "ttl_sec",
    "max_iterations",
    "no_progress_ticks",
    "generation",
    "used_tokens",
    "iterations",
    "no_progress",
    "stop_epoch",
}
_FLOAT = {
    "issued_wall",
    "issued_boot",
    "issued_mono",
    "last_wall",
    "last_boot",
    "last_mono",
    "ended_wall",
}
_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}|")


def grant_to_dict(grant: Grant, goal: str) -> dict:
    """The stored form: every field plus the goal (only the grant store writes this)."""
    out = {name: getattr(grant, name) for name in _FIELD_TYPES}
    out["tools"] = list(grant.tools)
    out["claim_evidence"] = evidence_to_dict(grant.claim_evidence)
    if not grant.heartbeat_start:
        del out["heartbeat_start"]  # W6d: only a flagged grant carries the key
    out["report_files"] = list(grant.report_files)
    if not grant.report_files:
        del out["report_files"]  # only a grant issued with a list carries the key
    if not grant.self_development:
        del out["self_development"]  # #791: only a self-development grant carries the key
    out["done_when"] = list(grant.done_when)
    if not grant.done_when:
        del out["done_when"]  # #683: only a grant issued with done_when carries the key
    out["goal"] = goal
    return out


def grant_from_dict(data: Any) -> tuple[Grant, str]:
    """A stored grant and its goal; raises :class:`GrantRefused` (``grant_body_invalid``)
    for anything outside the schema (the store then treats the file as malformed).

    ``claim_evidence`` (W6c) may be absent — a record written before W6c has none — and
    then reads as ``None``; every other key is required and no unknown key is accepted."""
    from . import state

    expected = set(_FIELD_TYPES) | {"goal"}
    optional = {
        "claim_evidence",
        "heartbeat_start",
        "report_files",
        "self_development",
        "done_when",
    }
    if type(data) is not dict or not expected - optional <= set(data) <= expected:
        raise GrantRefused("grant_body_invalid")
    # W6d / #791: present only as exactly ``true`` (it is written only then).
    for flag in ("heartbeat_start", "self_development"):
        if flag in data and data[flag] is not True:
            raise GrantRefused("grant_body_invalid")
    values: dict[str, Any] = {}
    for name in _STR:
        value = data[name]
        if type(value) is not str or len(value) > SUBTREE_MAX_CHARS:
            raise GrantRefused("grant_body_invalid")
        values[name] = value
    for name in _INT:
        value = data[name]
        if type(value) is not int or not 0 <= value < (1 << 53):
            raise GrantRefused("grant_body_invalid")
        values[name] = value
    for name in _FLOAT:
        value = data[name]
        if not _finite(value) or abs(value) > 1e12:
            raise GrantRefused("grant_body_invalid")
        values[name] = float(value)
    if type(data["recheck"]) is not bool:
        raise GrantRefused("grant_body_invalid")
    values["recheck"] = data["recheck"]
    tools = data["tools"]
    if (
        type(tools) is not list
        or not tools
        or not all(type(t) is str and t in TOOLS for t in tools)
    ):
        raise GrantRefused("grant_body_invalid")
    values["tools"] = tuple(tools)
    if not _HEX32.fullmatch(values["grant_id"]) or values["kind"] != KIND:
        raise GrantRefused("grant_body_invalid")
    if values["session_ref"] != "grant:" + values["grant_id"]:
        raise GrantRefused("grant_body_invalid")
    if values["verifier"] not in VERIFIERS:
        raise GrantRefused("grant_body_invalid")
    for name in (
        "status",
        "reason",
        "resume_status",
        "resume_reason",
        "goal_status",
        "goal_reason",
    ):
        if not _CODE.fullmatch(values[name]):
            raise GrantRefused("grant_body_invalid")
    if (
        values["status"] not in state.GRANT_STATUSES
        or values["goal_status"] not in state.GOAL_STATUSES
    ):
        raise GrantRefused("grant_body_invalid")
    if values["goal_resume"] not in ("", "awaiting_verification"):
        raise GrantRefused("grant_body_invalid")
    if values["late_verification"] not in ("", "pass", "fail"):
        raise GrantRefused("grant_body_invalid")
    if values["ttl_sec"] > TTL_CAP_SEC or values["max_iterations"] > MAX_ITERATIONS_CAP:
        raise GrantRefused("grant_body_invalid")
    goal = data["goal"]
    if type(goal) is not str or not goal or len(goal) > GOAL_MAX_CHARS:
        raise GrantRefused("grant_body_invalid")
    values["claim_evidence"] = evidence_from_dict(data.get("claim_evidence"))
    values["heartbeat_start"] = data.get("heartbeat_start", False) is True
    values["self_development"] = data.get("self_development", False) is True
    # ADR 0028 §4: present only as a non-empty list of valid, distinct paths (the shape
    # only: the file system was checked at issue).
    report_files = data.get("report_files", [])
    if type(report_files) is not list or len(report_files) > REPORT_FILES_MAX:
        raise GrantRefused("grant_body_invalid")
    if ("report_files" in data and not report_files) or not all(
        _report_path(path) for path in report_files
    ):
        raise GrantRefused("grant_body_invalid")
    if len(set(report_files)) != len(report_files):
        raise GrantRefused("grant_body_invalid")
    values["report_files"] = tuple(report_files)
    # #683: present only as a valid, non-empty list, and exactly with verifier ``checks``.
    done_when: tuple[str, ...] = ()
    if "done_when" in data:
        raw = data["done_when"]
        try:
            done_when = check_done_when(raw)
        except GrantRefused as error:
            raise GrantRefused("grant_body_invalid") from error
        if list(done_when) != raw or "run_checks" not in values["tools"]:
            raise GrantRefused("grant_body_invalid")
    if (values["verifier"] == "checks") != bool(done_when):
        raise GrantRefused("grant_body_invalid")
    values["done_when"] = done_when
    return Grant(**values), goal
