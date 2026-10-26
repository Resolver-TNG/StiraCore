"""``stira doctor``: find write-environment problems before the agent runs (#244).

For every write root: macOS privacy (TCC) locations are recognised by name and
only warned about — never read, so no consent dialog appears — and everything
else is checked with read-only probes (file flags, read-only mount, ACL,
quarantine / iCloud extended attributes) plus one write test: a temporary file
is created, renamed and removed again. The doctor changes no setting, no flag
and no attribute; the only filesystem change is that short-lived file.

The report is JSON (:func:`run_doctor`) or text (:func:`render_text`);
:func:`summarize` is the ``/api/gateway/status`` summary.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy

from . import probes
from .write_errors import REMEDIES, classify_write_failure

REPORT_VERSION = 1
NO_POLICY = object()
"""``policy=NO_POLICY``: the write policy could not be built — no write test at all."""
_QUARANTINE = "com.apple.quarantine"
_ICLOUD_PREFIXES = ("com.apple.icloud", "com.apple.fileprovider", "com.apple.clouddocs")
_WINDOWS_CFA = ("Documents", "Desktop", "Pictures", "Videos", "Music", "Favorites")
_LEVELS = {"info": 0, "warning": 1, "error": 2}
FINDING_CODES = (
    # per write root
    "tcc_zone",
    "missing",
    "unreadable",
    "not_directory",
    "unresolvable",
    "sip",
    "locked_flag",
    "readonly_attr",
    "readonly_volume",
    "acl_deny",
    "acl",
    "quarantine",
    "icloud",
    "path_length",
    "onedrive",
    "controlled_folder_access",
    "write_test_skipped",
    "write_test_failed",
    "floor",
    # notes about the process
    "cwd_root",
    "selinux",
    "apparmor",
    "page_check_ready",
    "page_check_no_browser",
    "page_check_platform",
    "page_check_root",
    "run_checks_off",
    "run_checks_ready",
    "run_checks_limits_only",
    "run_checks_self_test_failed",
    "run_checks_platform",
    "run_checks_root",
    # checks that are not about a write root (#267)
    "classifier_sokudan",
    "classifier_rule_chosen",
    "classifier_rule_only",
    "classifier_missing",
)
"""Every finding code the doctor can emit; the settings tab words each of them from the catalog."""
CLASSIFIER_CHOICES = ("auto", "rule", "sokudan")
INSTALL_HINT = 'pip install -e ".[jev]"'


def _finding(code: str, level: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "message": message, **extra}


# Evaluated once at import (the functions themselves, before anything wraps them).
_DIR_FD_FUNCS = {os.open, os.rename, os.unlink} <= os.supports_dir_fd


def _fd_walk_supported() -> bool:
    """``O_NOFOLLOW`` + ``O_DIRECTORY`` + ``dir_fd`` for open/rename/unlink, else no write test.

    Missing support means *skipped*, never a weaker test (no ``getattr(..., 0)``).
    """
    return (
        bool(getattr(os, "O_NOFOLLOW", 0)) and bool(getattr(os, "O_DIRECTORY", 0)) and _DIR_FD_FUNCS
    )


class _SymlinkInPath(OSError):
    """A component of the root is (now) a symlink or not a directory."""


def _open_dir_nofollow(path: str) -> int:
    """A descriptor for ``path``, walked from the filesystem root one component at a time.

    Every component is opened ``O_DIRECTORY | O_NOFOLLOW`` relative to its parent's
    descriptor, so a symlink anywhere on the path — the root itself or an ancestor
    swapped in between checks — stops the walk (:class:`_SymlinkInPath`) instead
    of being followed (#266).
    """
    parts = Path(path).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1)):
                    raise _SymlinkInPath(error.errno, "symlink in path", part) from None
                raise
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _write_test(
    root: str, identity: tuple[int, int], still_allowed: Callable[[], bool]
) -> dict[str, Any]:
    """Create → rename → remove one temporary file in ``root``; report what failed.

    Order (#266): the root was judged by path (TCC, floor) and its identity recorded;
    here it is opened component by component without following any symlink; the
    descriptor must be the inspected directory (``fstat``); the judgment is made
    again (``still_allowed``) and the path walked once more to the *same*
    directory. Only then does anything get written, and every create/rename/unlink
    is relative to that descriptor. A symlink anywhere on the path or a directory
    that changed during the checks means *skipped* (nothing is written).
    """
    if not _fd_walk_supported():
        return {"ok": None, "skipped": "unsupported"}
    step = "open"
    created = renamed = None
    directory = None
    try:
        try:
            directory = _open_dir_nofollow(root)
        except _SymlinkInPath:
            return {"ok": None, "skipped": "symlink_in_path"}
        if _identity(os.fstat(directory)) != identity or not still_allowed():
            return {"ok": None, "skipped": "changed"}
        try:
            again = _open_dir_nofollow(root)
        except _SymlinkInPath:
            return {"ok": None, "skipped": "symlink_in_path"}
        try:
            if _identity(os.fstat(again)) != identity:
                return {"ok": None, "skipped": "changed"}
        finally:
            os.close(again)
        step = "create"
        name = f".stira-doctor-{secrets.token_hex(8)}"
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(name, create, 0o600, dir_fd=directory)
        created = name
        try:
            os.write(descriptor, b"stira doctor write test\n")
        finally:
            os.close(descriptor)
        step = "rename"
        os.rename(name, name + ".renamed", src_dir_fd=directory, dst_dir_fd=directory)
        created, renamed = None, name + ".renamed"
        step = "remove"
        os.unlink(renamed, dir_fd=directory)
        renamed = None
        return {"ok": True}
    except OSError as error:
        return {
            "ok": False,
            "step": step,
            "errno": errno.errorcode.get(error.errno or 0),
            "error": f"[Errno {error.errno}] {error.strerror}: '{root}'",
        }
    finally:
        if directory is not None:
            for leftover in (created, renamed):
                if leftover:
                    try:
                        os.unlink(leftover, dir_fd=directory)
                    except OSError:
                        pass
            os.close(directory)


def _check_root(raw: str, **kwargs: Any) -> dict[str, Any]:
    """One root; an unresolvable path (symlink loop …) is reported, never aborts the report."""
    try:
        return _inspect_root(raw, **kwargs)
    except OSError as error:
        lexical = (
            kwargs["workspace"]
            if raw == WORKSPACE_ROOT
            else probes.lexical_absolute(raw, kwargs["workspace"])
        )
        shown = probes.display_path(lexical, home=kwargs["home"], base=kwargs["workspace"])
        entry = {"root": raw if raw == WORKSPACE_ROOT else shown, "path": shown, "write_test": None}
        reason = errno.errorcode.get(error.errno or 0, type(error).__name__)
        entry["findings"] = [_finding("unresolvable", "error", f"Cannot resolve ({reason}).")]
        return _close(entry)


def _inspect_root(
    raw: str,
    *,
    workspace: str,
    platform: str,
    home: str | None,
    policy: Any,
    write_test: bool,
) -> dict[str, Any]:
    lexical = workspace if raw == WORKSPACE_ROOT else probes.lexical_absolute(raw, workspace)
    shown = probes.display_path(lexical, home=home, base=workspace)
    entry: dict[str, Any] = {
        "root": raw if raw == WORKSPACE_ROOT else shown,
        "path": shown,
        "findings": [],
        "write_test": None,
    }
    findings = entry["findings"]
    zone = probes.tcc_zone(lexical, home=home, platform=platform)
    resolved = lexical
    if zone is None:
        resolved, zone = probes.safe_resolve(lexical, home=home, platform=platform)
        via = " (through a symlink)" if zone else ""
    else:
        via = ""
    if zone:
        findings.append(
            _finding(
                "tcc_zone",
                "warning",
                f"Inside macOS privacy-protected {zone}{via}: not inspected (that could open a "
                "consent dialog). Keep write roots outside protected folders.",
                zone=zone,
                remedy="tcc",
            )
        )
        return _close(entry)
    if resolved != lexical:
        entry["resolved"] = probes.display_path(resolved, home=home, base=workspace)
    try:
        info = os.lstat(resolved)
    except FileNotFoundError:
        findings.append(
            _finding("missing", "warning", "Does not exist yet (created on the first write).")
        )
        return _close(entry)
    except OSError as error:
        findings.append(_finding("unreadable", "error", f"Cannot inspect: {error.strerror}."))
        return _close(entry)
    identity = _identity(info)
    if not stat.S_ISDIR(info.st_mode):
        findings.append(_finding("not_directory", "error", "Not a directory."))
        return _close(entry)
    flags = probes.file_flags(resolved, platform=platform) or frozenset()
    if "restricted" in flags:
        findings.append(
            _finding("sip", "error", "Protected by System Integrity Protection.", remedy="sip")
        )
    locked = sorted(flags & {"uchg", "uappnd", "schg", "sappnd", "immutable", "append"})
    if locked:
        findings.append(
            _finding(
                "locked_flag",
                "error",
                f"Locked by file flags ({', '.join(locked)}).",
                flags=locked,
                remedy="locked_flag_system" if {"schg", "sappnd"} & set(locked) else "locked_flag",
            )
        )
    if "readonly" in flags:
        findings.append(
            _finding("readonly_attr", "warning", "Read-only attribute set.", remedy="readonly_attr")
        )
    if probes.readonly_mount(resolved, platform=platform):
        findings.append(
            _finding("readonly_volume", "error", "On a read-only volume.", remedy="readonly_volume")
        )
    if probes.acl_denies(resolved, platform=platform):
        findings.append(_finding("acl_deny", "warning", "An ACL has deny entries.", remedy="acl"))
    elif platform.startswith("linux") and probes.acl_text(resolved, platform=platform):
        findings.append(_finding("acl", "info", "A POSIX ACL is set."))
    names = probes.xattr_names(resolved, platform=platform) or []
    if _QUARANTINE in names:
        findings.append(
            _finding(
                "quarantine",
                "warning",
                "Has the quarantine attribute (downloaded content). Files created here may be "
                "treated as untrusted.",
            )
        )
    if any(name.startswith(_ICLOUD_PREFIXES) for name in names):
        findings.append(
            _finding(
                "icloud",
                "warning",
                "Managed by iCloud / a file provider: files may be evicted or synced.",
            )
        )
    if platform == "win32":
        _windows_findings(resolved, findings, home=home)

    def refusal() -> str | None:
        """The path judgment, made again right before writing (#266); why not, or ``None``."""
        if policy is None:
            return "floor"  # fail closed: run_doctor always supplies at least the floor
        again, zone_now = probes.safe_resolve(lexical, home=home, platform=platform)
        if zone_now or again != resolved:
            return "changed"
        try:
            # The probe file itself, not the root: a root that merely *contains* a
            # protected directory is not on the floor for a new file at its top.
            on_floor = policy.is_read_only(os.path.join(resolved, ".stira-doctor-probe"))
        except (OSError, ValueError, RuntimeError):
            return "floor"
        return "floor" if on_floor else None

    def allowed() -> bool:
        return refusal() is None

    if policy is NO_POLICY:
        entry["write_test"] = {"ok": None, "skipped": "no_policy"}
        findings.append(
            _finding(
                "write_test_skipped",
                "warning",
                "Write test skipped: the write policy could not be built from the configuration.",
            )
        )
        return _close(entry)
    why = refusal()
    if why == "floor":
        findings.append(
            _finding("floor", "info", "On the identity floor: write test skipped (by design).")
        )
        return _close(entry)
    if why == "changed":
        entry["write_test"] = {"ok": None, "skipped": "changed"}
        findings.append(
            _finding(
                "write_test_skipped",
                "warning",
                "Write test skipped: the root changed while checked.",
            )
        )
        return _close(entry)
    if write_test:
        # The path as configured (absolute, not resolved): a symlink that exists from the
        # start — the root itself or an ancestor — is seen by the walk and skipped (#266).
        result = _write_test(lexical, identity, allowed)
        if result["ok"] is None:
            reason = {
                "unsupported": "not available on this platform",
                "symlink_in_path": "the path contains a symlink",
                "changed": "the root changed while it was checked",
            }.get(result["skipped"], result["skipped"])
            findings.append(
                _finding("write_test_skipped", "warning", f"Write test skipped: {reason}.")
            )
        elif not result["ok"]:
            if "category" not in result:
                failure = classify_write_failure(
                    "file_write",
                    {"path": os.path.join(resolved, ".stira-doctor")},
                    result["error"],
                    platform=platform,
                    home=home,
                    base=workspace,
                )
                if failure is not None:
                    result["category"] = failure.category
                    result["remedy"] = failure.remedy
                    result["evidence"] = list(failure.evidence)
            result["error"] = probes.mask_text(result["error"], home=home, base=workspace)
            findings.append(
                _finding(
                    "write_test_failed",
                    "error",
                    f"Write test failed at {result['step']} ({result.get('errno')}).",
                    step=result["step"],
                    errno=result.get("errno"),
                    remedy=result.get("remedy", "other"),
                )
            )
        entry["write_test"] = result
    return _close(entry)


def _windows_findings(resolved: str, findings: list, *, home: str | None) -> None:
    if len(resolved) >= 240:
        findings.append(_finding("path_length", "warning", "Path is close to the 260 limit."))
    parts = Path(resolved).parts
    if any(part.casefold().startswith("onedrive") for part in parts):
        findings.append(
            _finding("onedrive", "warning", "Under OneDrive: files on demand may be offline.")
        )
    home_text = probes._home(home)
    for name in _WINDOWS_CFA:
        if probes.within(resolved, os.path.join(home_text, name)):
            findings.append(
                _finding(
                    "controlled_folder_access",
                    "warning",
                    f"Under {name}: Defender Controlled Folder Access (if on) blocks writes.",
                )
            )
            break


def _sokudan_installed() -> bool:
    """Is the optional ``sokudan`` package findable? Looked up, never imported (no model load)."""
    try:
        return importlib.util.find_spec("sokudan") is not None
    except (ImportError, ValueError):
        return False


def _classifier_check(choice: str) -> dict[str, Any]:
    """The stimulus classifier the agent will run with, from the choice and the install state.

    ``auto`` (the default) and ``sokudan`` use Sokudan when it is installed; without it the
    agent runs on the rule-based fallback, which rarely reacts (#367). An explicit ``rule``
    choice is the operator's decision, not a warning. Display only: nothing is changed.
    """
    installed = _sokudan_installed()
    if choice == "rule":
        return _finding(
            "classifier_rule_chosen",
            "info",
            "Emotion classification uses the rule-based classifier (chosen in the settings).",
            mode="rule_only",
        )
    if installed:
        return _finding(
            "classifier_sokudan",
            "info",
            "Emotion classification uses Sokudan (installed).",
            mode="sokudan",
        )
    if choice == "sokudan":
        return _finding(
            "classifier_missing",
            "warning",
            "Sokudan is chosen for emotion classification but is not installed: the rule-based "
            f"fallback runs instead. Install it: {INSTALL_HINT}",
            mode="rule_only",
            remedy="install_jev",
        )
    return _finding(
        "classifier_rule_only",
        "warning",
        "Emotion classification runs on the rule-based fallback only: Sokudan is not installed, "
        f"so the emotions react rarely. Install it: {INSTALL_HINT}",
        mode="rule_only",
        remedy="install_jev",
    )


def _page_check_note(status: Any) -> dict[str, Any]:
    """Whether the verification browser tool ``page_check`` is offered (ADR 0015 §7).

    Informational: the tool is optional, so a missing browser is not a problem of the
    setup. Display only: nothing is changed and no browser is started.
    """
    if status.code == "page_check_ready":
        return _finding(
            "page_check_ready",
            "info",
            "page_check (verification browser: opens a workspace page with no network, runs "
            f"a few input actions and checks, takes screenshots) is available: {status.path}.",
        )
    if status.code == "page_check_platform":
        return _finding(
            "page_check_platform",
            "info",
            "page_check (verification browser) is hidden: Windows is not supported yet.",
        )
    if status.code == "page_check_root":
        return _finding(
            "page_check_root",
            "info",
            "page_check (verification browser) is hidden: Stira runs as root and the browser "
            "would need its sandbox switched off.",
        )
    return _finding(
        "page_check_no_browser",
        "info",
        "page_check (verification browser) is hidden: no Chrome, Chromium or Edge was found. "
        "Install one or set STIRA_BROWSER_PATH.",
    )


def _run_checks_note(code: str | None, host: Any = None) -> dict[str, Any]:
    """Whether and how ``run_checks`` would be contained (ADR 0019 §8). Display only: the
    self-test runs Stira's own probe, never the workspace's code."""
    if code in (None, "run_checks_off"):
        return _finding(
            "run_checks_off",
            "info",
            "run_checks (verification runner) is off: gateway-config.json run_checks: "
            '{"phase": 1} turns it on.',
        )
    if code == "run_checks_platform":
        return _finding(
            "run_checks_platform", "info", "run_checks is not registered: Windows is not supported."
        )
    if code == "run_checks_root":
        return _finding(
            "run_checks_root",
            "info",
            "run_checks is not registered: Stira runs as root.",
        )
    if host is None:
        from stiracore.tools.run_checks import containment

        host = containment()
    if host.code == "run_checks_ready":
        return _finding(
            "run_checks_ready",
            "info",
            "run_checks runs in a throwaway copy inside a macOS sandbox profile "
            "(network, home reads and writes outside refused; self-test passed). Memory is "
            "not limited on macOS. A saved Always is effective here.",
        )
    capped = " A saved Always for run_checks is read as Ask on this host (rule_always_capped)."
    if host.code == "run_checks_self_test_failed":
        return _finding(
            "run_checks_self_test_failed",
            "warning",
            f"run_checks is limits only: {host.detail}. The network, home reads and writes "
            "outside the copy are NOT held." + capped,
        )
    return _finding(
        "run_checks_limits_only",
        "warning",
        f"run_checks is limits only ({host.detail}): the network, home reads and writes "
        "outside the copy are NOT held; every call is a card." + capped,
    )


def _close(entry: dict[str, Any]) -> dict[str, Any]:
    worst = max((_LEVELS[f["level"]] for f in entry["findings"]), default=0)
    entry["status"] = ("ok", "warning", "error")[worst]
    return entry


def run_doctor(
    write_roots: Iterable[str],
    *,
    workspace: str | os.PathLike,
    platform: str | None = None,
    home: str | os.PathLike | None = None,
    policy: Any = None,
    write_test: bool = True,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    clock=time.time,
    classifier_choice: str = "auto",
    browser: Any = None,
    run_checks_code: str | None = None,
    run_checks_host: Any = None,
) -> dict[str, Any]:
    """Check every write root; never changes a setting (only a temp file per root).

    ``config_error`` is set (and ``ok`` is false) when the configuration itself is unusable —
    today: the write policy cannot be built. The CLI maps it to exit status 2 (#267).
    """
    platform = sys.platform if platform is None else platform
    home = os.fspath(home) if home is not None else None
    workspace_text = probes.lexical_absolute(workspace)
    if policy is None:
        # Never "anything goes": the identity floor (SOUL/IPD names, the TCC database
        # and directory, ...) always applies to the write test (#266).
        try:
            policy = WritePathPolicy(base_dir=Path(workspace_text))
        except (OSError, ValueError, RuntimeError):
            policy = NO_POLICY
    roots = [
        _check_root(
            str(raw),
            workspace=workspace_text,
            platform=platform,
            home=home,
            policy=policy,
            write_test=write_test,
        )
        for raw in dict.fromkeys(write_roots)
    ]
    notes = []
    cwd = os.getcwd()
    if cwd == os.sep:
        notes.append(
            _finding(
                "cwd_root",
                "info",
                "The process runs in '/': relative paths given to other tools would resolve "
                "there (Stira's file tools anchor them at the workspace).",
            )
        )
    if platform.startswith("linux"):
        if os.path.exists("/sys/fs/selinux/enforce"):
            notes.append(_finding("selinux", "info", "SELinux is present; it may deny writes."))
        if os.path.exists("/sys/module/apparmor/parameters/enabled"):
            notes.append(_finding("apparmor", "info", "AppArmor is present; it may deny writes."))
    if browser is None:
        from stiracore.tools.page_check import browser_status

        browser = browser_status(platform=platform)
    notes.append(_page_check_note(browser))
    notes.append(_run_checks_note(run_checks_code, run_checks_host))
    counts = {"ok": 0, "warning": 0, "error": 0}
    for entry in roots:
        counts[entry["status"]] += 1
    config_error = "policy_unbuildable" if policy is NO_POLICY else None
    checks = [
        _classifier_check(classifier_choice if classifier_choice in CLASSIFIER_CHOICES else "auto")
    ]
    return {
        "version": REPORT_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(clock())),
        "platform": platform,
        "ok": counts["error"] == 0 and config_error is None,
        "config_error": config_error,
        "counts": counts,
        "workspace": probes.display_path(workspace_text, home=home),
        "cwd": probes.display_path(cwd, home=home, base=workspace_text),
        "principal": probes.responsible_process(env, executable, platform=platform),
        "roots": roots,
        "checks": checks,
        "notes": notes,
    }


def summarize(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``/api/gateway/status`` summary of a doctor report."""
    if not report:
        return {"ran": False}
    findings = [f for entry in report.get("roots", ()) for f in entry["findings"]]
    checks = list(report.get("checks", ()))
    warnings = sum(1 for f in (*findings, *checks) if f["level"] == "warning")
    errors = sum(1 for f in (*findings, *checks) if f["level"] == "error")
    codes = sorted(
        ({f["code"] for f in findings} | {f["code"] for f in checks if f["level"] != "info"})
        - {"floor"}
    )
    return {
        "ran": True,
        "ok": bool(report.get("ok")),
        "generated_at": report.get("generated_at"),
        "roots": len(report.get("roots", ())),
        "errors": errors,
        "warnings": warnings,
        "codes": codes,
        **({"config_error": report["config_error"]} if report.get("config_error") else {}),
        **({"error": report["error"]} if report.get("error") else {}),
    }


def render_text(report: Mapping[str, Any]) -> str:
    """Human-readable report (English)."""
    counts = report["counts"]
    verdict = "OK" if report["ok"] else "PROBLEMS FOUND"
    if report.get("config_error"):
        verdict = "CONFIGURATION ERROR (the write policy cannot be built; fix the settings)"
    lines = [
        (
            f"stira doctor — {report['platform']} — {verdict} ({counts['ok']} ok, "
            f"{counts['warning']} warning, {counts['error']} error)"
        ),
        f"workspace: {report['workspace']}",
        f"permission is checked for: {probes.principal_text(report['principal'])}",
        "",
    ]
    for entry in report["roots"]:
        label = entry["root"] if entry["root"] == entry["path"] else f"{entry['root']} → "
        label += "" if entry["root"] == entry["path"] else entry["path"]
        lines.append(f"[{entry['status'].upper()}] {label}")
        test = entry.get("write_test")
        if test is not None:
            if test["ok"] is None:
                lines.append(f"  write test: skipped ({test.get('skipped', 'unknown')})")
            elif test["ok"]:
                lines.append("  write test: ok")
            else:
                category, error = test.get("category", "other"), test.get("error", "")
                lines.append(f"  write test: FAILED ({category}): {error}")
        for finding in entry["findings"]:
            lines.append(f"  - {finding['level']}: {finding['message']}")
            remedy = finding.get("remedy")
            if remedy and finding["level"] != "info":
                lines.append(f"    fix (by a person): {REMEDIES.get(remedy, REMEDIES['other'])}")
    for check in report.get("checks", ()):
        lines.append(f"[{check['level'].upper()}] {check['message']}")
    for note in report.get("notes", ()):
        lines.append(f"note: {note['message']}")
    lines.append("")
    lines.append("The doctor changed nothing (one temporary file per root, removed again).")
    return "\n".join(lines)


__all__ = ["FINDING_CODES", "REPORT_VERSION", "render_text", "run_doctor", "summarize"]
