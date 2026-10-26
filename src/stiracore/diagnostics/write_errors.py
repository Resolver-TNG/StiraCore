"""Classify failed ``file_write``/``editor`` results and post permission cards (#244).

macOS answers a TCC denial, a locked (``uchg``) file, a SIP location and a
read-only volume alike with ``[Errno 1] Operation not permitted``; a relative
path resolved against ``/`` looks like ``Read-only file system``. Neither the
person nor the model can tell them apart from the text. :func:`classify_write_failure`
combines the errno with Stira's own refusal texts (policy, gate) and read-only
probes of the path (:mod:`.probes`) into one category, with the evidence used.

:class:`WriteFailureHook` (``AfterToolCallEvent``) appends a short diagnosis to
the failed tool result — telling the model that a *person* has to fix it — and
hands the failure to a sink (:class:`WriteFailureBoard`, the permission cards).
It never sets ``event.retry``: Stira does not retry by itself. The board allows
exactly one human "fixed it, retry" per card; that re-checks the cause (read
only) and the retry itself is an ordinary chat turn through every gate.
"""

from __future__ import annotations

import errno as errno_module
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from stiracore.identity_paths import (
    MASKED,
    OUTSIDE_WRITE_ROOTS_ERROR,
    READ_ONLY_ERROR,
    REFUSAL_DETAIL_PREFIX,
    WRITE_SECRET_PATH_CODE,
)
from stiracore.tools.file_editor_tool import EDITOR_TOOL_NAME, from_editor_path
from stiracore.tools.identity_tools import PATH_WRITE_TOOLS, editor_read_only
from stiracore.tools.os_protection import OS_PROTECTION_ERROR

from . import probes

_LOG = logging.getLogger(__name__)

CATEGORIES = (
    "locked_flag",
    "protected_by_os",
    "readonly_volume",
    "wrong_cwd",
    "outside_write_roots",
    "identity_protected",
    "consent_blocked",
    "permission_denied",
    "other",
)
# Remedy codes (card text keys ``diagnostics.fix_<code>``) and their English text.
REMEDIES = {
    "locked_flag": (
        "The file or its folder is locked (uchg). In Finder open Get Info and clear "
        "'Locked', or run `chflags nouchg <path>` yourself in a terminal. Then retry once."
    ),
    "locked_flag_system": (
        "A system-immutable flag (schg/sappnd) is set. Only an administrator can clear it, "
        "outside Stira (sometimes only in recovery mode). Prefer writing somewhere else."
    ),
    "readonly_attr": (
        "The file has the read-only attribute or an immutable flag. Clear it yourself "
        "(file properties, or chattr -i as the owner/administrator), then retry once."
    ),
    "tcc": (
        "macOS privacy protection (Files and Folders) guards this location. Recommended: "
        "keep the working folder outside protected folders (e.g. ~/projects/...) and add it "
        "to write_roots. Otherwise allow the responsible app under System Settings > Privacy "
        "& Security > Files and Folders. Full Disk Access is not recommended (far too broad)."
    ),
    "sip": (
        "System Integrity Protection guards this location. It cannot be written and SIP "
        "should not be disabled; write inside the workspace instead."
    ),
    "os": "The operating system refused the write. Check the location, then retry once.",
    "readonly_volume": (
        "The volume is mounted read-only. Remount it writable or choose a folder on a "
        "writable volume, then retry once."
    ),
    "wrong_cwd": (
        "A relative (or empty) path was resolved against the wrong directory. Use an "
        "absolute path or one relative to the agent workspace."
    ),
    "outside_write_roots": (
        "The path is outside the agent's write roots. If it should be writable, add the "
        "folder to write_roots in Settings (applies after a restart)."
    ),
    "identity_protected": (
        "This is an identity or protected file (SOUL, IPD, protected directories). By design "
        "no approval or setting lets the agent write it."
    ),
    "secret_path": (
        "This path is on the secret-path floor (credentials, keys, keychains, browser "
        "profiles, .env files). By design no write root, setting or approval lets file tools "
        "write it; edit it yourself if you really need to."
    ),
    "os_protection": (
        "The command would change an OS protection (file flags, extended attributes, privacy "
        "settings, SIP) or use sudo. Stira refuses tool calls that name such commands; do it "
        "yourself if you really want to."
    ),
    "consent_blocked": (
        "Stira's approval gate refused the call (no approval for this exact call). Approve it "
        "on its approval card, or ask the agent again."
    ),
    "breaker": "The circuit breaker blocks writes. Wait until it recovers, or approve the call.",
    "permission_denied": (
        "The user running Stira lacks write permission (owner/mode). Change it yourself "
        "(Finder Get Info > Sharing & Permissions) or use another folder, then retry once."
    ),
    "acl": (
        "An access control list (ACL) denies the write. Review it yourself (Finder Get Info > "
        "Sharing & Permissions, or ls -le), then retry once."
    ),
    "other": "The cause could not be determined. Read the message, then retry once.",
}
DEEP_LINKS = {
    "tcc": "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders",
}
SAFE_LINK_PREFIXES = ("x-apple.systempreferences:", "ms-settings:")
# Categories a person can fix and then retry once; the others are by design,
# need a settings change + restart, or an approval.
_RETRYABLE_REMEDIES = frozenset(
    {
        "locked_flag",
        "locked_flag_system",
        "readonly_attr",
        "tcc",
        "os",
        "readonly_volume",
        "wrong_cwd",
        "permission_denied",
        "acl",
        "other",
    }
)
MARKER = "[Stira write diagnosis:"
_ERRNO = re.compile(r"\[Errno (\d+)\]")
_QUOTED_PATH = re.compile(r"\[Errno \d+\][^:]*: '((?:[^'\\]|\\.)+)'")
_STRERRORS = {
    "Operation not permitted": "EPERM",
    "Permission denied": "EACCES",
    "Read-only file system": "EROFS",
    "No such file or directory": "ENOENT",
    "No space left on device": "ENOSPC",
}
_GATE_TEXTS = (
    "was not run:",
    "not inside a step Stira's approval gate granted",
    "Writes are blocked by the circuit breaker",
    "Write tools require confirmation at WARNING",
    "HITL rejected this write operation",
)
# Refusals of Stira's rule layer (Always/Ask/Never, #190): the human's own setting.
_RULE_TEXTS = (
    "Tool policy is Never for this tool",
    "Tool policy changed while waiting for approval",
    "Tool policy changed after approval",
)
# ``[refused by: floor; argument: "path"; ...]`` written by ``refusal_detail`` (#307).
_DETAIL = re.compile(
    re.escape(REFUSAL_DETAIL_PREFIX.strip())
    + r' ([a-z_]+); argument: "([A-Za-z0-9_.\[\]-]{1,80})";'
)
# Argument names a card may show (#307); any other key the model chose is "other".
_KNOWN_ARGUMENTS = frozenset(
    {
        "path",
        "file_path",
        "filename",
        "content",
        "file_text",
        "new_str",
        "old_str",
        "command",
        "patch",
        "source",
        "destination",
        "target",
        "input",
    }
)
_LOCK_FLAGS = {
    "uchg": "locked_flag",
    "uappnd": "locked_flag",
    "schg": "locked_flag_system",
    "sappnd": "locked_flag_system",
    "immutable": "readonly_attr",
    "append": "readonly_attr",
    "readonly": "readonly_attr",
}


@dataclass(frozen=True)
class WriteFailure:
    """One classified write failure. ``path``/``message`` are masked for display."""

    category: str
    remedy: str
    tool: str
    operation: str
    path: str | None
    errno: str | None
    message: str
    evidence: tuple[str, ...] = ()
    principal: Mapping[str, Any] = field(default_factory=dict)
    deep_link: str | None = None
    # Unmasked target for re-checking; never serialized (``as_dict``) or logged.
    target: str | None = field(default=None, repr=False, compare=False)

    @property
    def retryable(self) -> bool:
        return self.remedy in _RETRYABLE_REMEDIES

    @property
    def card_worthy(self) -> bool:
        """A human rejection or rule is the human's own decision: no permission card for it."""
        return "gate:hitl_rejected" not in self.evidence and "layer:rule" not in self.evidence

    @property
    def remedy_text(self) -> str:
        return REMEDIES.get(self.remedy, REMEDIES["other"])

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("target", None)
        data["evidence"] = list(self.evidence)
        data["principal"] = dict(self.principal)
        data["retryable"] = self.retryable
        data["remedy_text"] = self.remedy_text
        return data

    def model_note(self) -> str:
        """Appended to the tool result: what the model should (not) do next."""
        why = ", ".join(self.evidence) or "no further evidence"
        head = f"{MARKER} {self.category}] ({why})."
        if self.category == "identity_protected":
            return f"{head} This path is protected by design; do not retry or work around it."
        if self.category == "consent_blocked":
            return f"{head} Stira's approval gate refused this call; it is not an OS problem."
        if self.category == "outside_write_roots":
            return (
                f"{head} Do not write elsewhere to get around it; ask the user whether the "
                "folder should be added to the write roots."
            )
        return (
            f"{head} A person has to resolve this. Do not try to change file flags, extended "
            "attributes, privacy (TCC) settings or SIP, and do not use sudo (tool calls naming "
            "those commands are refused). Tell the user what failed; the permission card shows them how to fix it."
        )


def _errno_name(text: str) -> str | None:
    match = _ERRNO.search(text)
    if match:
        return errno_module.errorcode.get(int(match.group(1)))
    for phrase, name in _STRERRORS.items():
        if phrase in text:
            return name
    return None


def _target_from(text: str, raw_path: Any, base: str | None) -> str | None:
    match = _QUOTED_PATH.search(text)
    # The wrapper shows the path with ~ or as [masked] (#307): expand ~, and use the
    # call's own argument for a masked one.
    if match and match.group(1).strip() and match.group(1) != MASKED:
        return os.path.normpath(os.path.expanduser(match.group(1)))
    if isinstance(raw_path, str) and raw_path.strip() and "\0" not in raw_path:
        return probes.lexical_absolute(raw_path, base)
    return None


_EDITOR_COMMANDS = frozenset({"create", "str_replace", "insert", "undo_edit", "write"})


def _operation(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """A fixed word (the model chose the arguments: never echo free text onto a card)."""
    if tool_name == EDITOR_TOOL_NAME:
        command = arguments.get("command")
        return command if command in _EDITOR_COMMANDS else "edit"
    return "write"


def _probe_os(
    target: str, errno_name: str | None, platform: str, home: Any = None
) -> tuple[str, str, list[str]]:
    """``(category, remedy, evidence)`` from read-only probes of ``target`` and its folder.

    ``target`` is first resolved component by component (:func:`probes.safe_resolve`):
    a symlink into a privacy-protected zone stops the walk *before* the zone and the
    result is ``protected_by_os``/``tcc`` without any further system call (#266).
    """
    evidence: list[str] = []
    try:
        target, zone = probes.safe_resolve(target, home=home, platform=platform)
    except OSError:
        return "other", "other", [f"errno:{errno_name}"] if errno_name else []
    if zone:
        return "protected_by_os", "tcc", [f"tcc:{zone}", "path:symlink"]
    existing = probes.nearest_existing(target, platform=platform)
    folder = probes.nearest_existing(os.path.dirname(target) or target, platform=platform)
    checked = [(p, label) for p, label in ((target, "file"), (folder, "folder")) if p]
    if existing != target:
        checked = [(folder, "folder")] if folder else []
    locks: list[str] = []
    for item, label in checked:
        flags = probes.file_flags(item, platform=platform) or frozenset()
        if "restricted" in flags:
            return "protected_by_os", "sip", [f"flag:restricted({label})"]
        locks.extend(f"{name}({label})" for name in sorted(flags) if name in _LOCK_FLAGS)
    if locks:
        remedy = _LOCK_FLAGS[locks[0].split("(", 1)[0]]
        return "locked_flag", remedy, [f"flag:{lock}" for lock in locks]
    if probes.sip_path(target, platform=platform):
        return "protected_by_os", "sip", ["sip:path"]
    if folder and probes.readonly_mount(folder, platform=platform):
        return "readonly_volume", "readonly_volume", ["statfs:rdonly"]
    if errno_name == "EROFS":
        return "readonly_volume", "readonly_volume", ["errno:EROFS"]
    for item, label in checked:
        if probes.acl_denies(item, platform=platform):
            return "permission_denied", "acl", [f"acl:deny({label})"]
    if errno_name in ("EACCES", "EPERM"):
        for item, label in checked:
            if probes.writable(item, platform=platform) is False:
                return "permission_denied", "permission_denied", [f"mode:not_writable({label})"]
    if errno_name:
        evidence.append(f"errno:{errno_name}")
    return "other", "other", evidence


def classify_write_failure(
    tool_name: str,
    arguments: Any,
    text: str,
    *,
    base: str | os.PathLike | None = None,
    cwd: str | None = None,
    platform: str | None = None,
    home: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> WriteFailure | None:
    """Classify one failed write result; ``None`` when it is not a write failure.

    ``base`` anchors a relative path (the agent workspace); ``cwd`` is the process
    working directory (for ``wrong_cwd``). Content errors of ``editor`` (e.g.
    ``old_str`` not found) carry no errno or refusal and are not classified.
    """
    if tool_name not in PATH_WRITE_TOOLS or not isinstance(arguments, Mapping):
        return None
    if editor_read_only(tool_name, arguments):
        return None
    platform = sys.platform if platform is None else platform
    cwd = os.getcwd() if cwd is None else cwd
    raw_path = arguments.get("path")
    if tool_name == EDITOR_TOOL_NAME and isinstance(raw_path, str):
        raw_path = from_editor_path(raw_path)
    text = text or ""
    errno_name = _errno_name(text)
    base = os.fspath(base) if base is not None else None
    target = _target_from(text, raw_path, base)

    def make(category: str, remedy: str, evidence: Iterable[str]) -> WriteFailure:
        return WriteFailure(
            category=category,
            remedy=remedy,
            tool=tool_name,
            operation=_operation(tool_name, arguments),
            path=probes.display_path(target, home=home, base=base),
            errno=errno_name,
            message=probes.mask_text(text, home=home, base=base)[:500],
            evidence=tuple(evidence),
            principal=probes.responsible_process(env, executable, platform=platform),
            deep_link=DEEP_LINKS.get(remedy),
            target=target,
        )

    stripped = text.strip()
    # Which argument the refusal reacted to (#307): a plain argument name only (the
    # token itself stays in the masked message, never in the evidence of a card).
    detail = _DETAIL.search(stripped)
    argument = []
    if detail:
        top = re.split(r"[.\[]", detail.group(2), maxsplit=1)[0]
        argument = [f"argument:{top if top in _KNOWN_ARGUMENTS else 'other'}"]
    if stripped.startswith(READ_ONLY_ERROR):
        evidence = ["layer:floor", "policy:floor", *argument]
        return make("identity_protected", "identity_protected", evidence)
    if f"[{WRITE_SECRET_PATH_CODE}]" in stripped:
        # #536: before the write-roots text (the floor wins whatever the roots allow).
        evidence = ["layer:secret_path", "policy:secret_path", *argument]
        return make("identity_protected", "secret_path", evidence)
    if stripped.startswith(OUTSIDE_WRITE_ROOTS_ERROR):
        evidence = ["layer:write_roots", "policy:write_roots", *argument]
        return make("outside_write_roots", "outside_write_roots", evidence)
    if stripped.startswith(OS_PROTECTION_ERROR):
        evidence = ["layer:os_protection", "policy:os_protection"]
        return make("identity_protected", "os_protection", evidence)
    for marker in _RULE_TEXTS:
        if marker in stripped:
            return make("consent_blocked", "consent_blocked", ["layer:rule", "gate:tool_policy"])
    for marker in _GATE_TEXTS:
        if marker in stripped:
            if "HITL rejected" in marker:
                evidence = ["layer:hitl", "gate:hitl_rejected"]
                return make("consent_blocked", "consent_blocked", evidence)
            remedy = "breaker" if "breaker" in marker or "WARNING" in marker else "consent_blocked"
            layer = "layer:breaker" if remedy == "breaker" else "layer:gate"
            return make("consent_blocked", remedy, [layer, f"gate:{remedy}"])
    if errno_name is None:
        return None
    relative = not (isinstance(raw_path, str) and os.path.isabs(os.path.expanduser(raw_path)))
    # Anchored at the workspace (Stira's wrapper does that) is not a cwd mistake.
    in_base = bool(base and target and probes.within(target, base))
    if relative and (
        (errno_name == "ENOENT" and "''" in text)
        or (cwd == os.sep and not in_base and errno_name in ("EROFS", "EPERM", "EACCES"))
    ):
        return make("wrong_cwd", "wrong_cwd", [f"errno:{errno_name}", "path:relative"])
    if errno_name not in ("EPERM", "EACCES", "EROFS"):
        return make("other", "other", [f"errno:{errno_name}"])
    if target is None:
        return make("other", "other", [f"errno:{errno_name}"])
    zone = probes.tcc_zone(target, home=home, platform=platform)
    if zone and errno_name in ("EPERM", "EACCES"):
        # Never probe: a privacy-protected path is recognised from its name only.
        return make("protected_by_os", "tcc", [f"tcc:{zone}", f"errno:{errno_name}"])
    if zone:
        return make("other", "other", [f"tcc:{zone}", f"errno:{errno_name}"])
    category, remedy, evidence = _probe_os(target, errno_name, platform, home)
    if remedy == "tcc" and errno_name not in ("EPERM", "EACCES"):
        category, remedy = "other", "other"
    if category == "other" and errno_name == "EPERM" and platform == probes.DARWIN:
        evidence.append("hint:privacy_or_sandbox")
    return make(category, remedy, evidence)


def recheck(failure: WriteFailure, *, platform: str | None = None) -> dict[str, Any]:
    """Re-check (read only) whether the cause is still present: ``clear``/``still_blocked``/``unverified``.

    Privacy-protected paths — also behind a symlink — are never probed
    (``unverified``); so are causes that only a write could reveal. The original
    ``EROFS`` is not reused: only the probes decide (a remounted volume is clear).
    """
    platform = sys.platform if platform is None else platform
    if failure.target is None or failure.remedy in ("tcc", "wrong_cwd", "other", "os"):
        return {"state": "unverified", "category": failure.category, "evidence": []}
    if probes.tcc_zone(failure.target, platform=platform):
        return {"state": "unverified", "category": failure.category, "evidence": []}
    errno_name = None if failure.errno == "EROFS" else failure.errno
    category, remedy, evidence = _probe_os(failure.target, errno_name, platform)
    if remedy == "tcc":
        return {"state": "unverified", "category": failure.category, "evidence": []}
    if category == "other":
        return {"state": "clear", "category": None, "evidence": []}
    return {"state": "still_blocked", "category": category, "evidence": evidence}


def _result_text(result: Mapping[str, Any]) -> str:
    parts = []
    for block in result.get("content") or ():
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


class WriteFailureHook(HookProvider):
    """Post-process failed ``file_write``/``editor`` results (#244).

    ``sink(failure, tool_use_id=...)`` receives every failure worth a permission
    card; ``base_provider`` returns the workspace that relative paths anchor to.
    A failing sink or classifier never changes the tool result beyond the note.
    """

    def __init__(
        self,
        sink: Callable[..., Any] | None = None,
        *,
        base_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.sink = sink
        self.base_provider = base_provider

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def _audit_secret_refusal(self, name: str, arguments: Any) -> None:
        """#536: one ``audit.write.denied`` row (tool, keyed path hash, code). Never raises."""
        try:
            from stiracore.tools.file_tools import audit_write_refusal

            path = arguments.get("path") if isinstance(arguments, Mapping) else None
            if name == EDITOR_TOOL_NAME and isinstance(path, str):
                path = from_editor_path(path)
            base = self.base_provider() if self.base_provider is not None else None
            audit_write_refusal(name, path, WRITE_SECRET_PATH_CODE, base)
        except Exception as error:  # noqa: BLE001
            _LOG.warning("Write refusal audit failed (%s)", type(error).__name__)

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use
        name = tool_use.get("name")
        result = event.result
        if name not in PATH_WRITE_TOOLS or not isinstance(result, Mapping):
            return
        if result.get("status") != "error":
            return
        text = _result_text(result)
        if MARKER in text:
            return
        if f"[{WRITE_SECRET_PATH_CODE}]" in text:
            self._audit_secret_refusal(name, tool_use.get("input"))
        try:
            base = self.base_provider() if self.base_provider is not None else None
            failure = classify_write_failure(
                name,
                tool_use.get("input"),
                text,
                base=os.fspath(base) if base is not None else None,
            )
        except Exception as error:  # noqa: BLE001 - diagnosis is best-effort
            # Class name only: an exception message or traceback may carry secrets.
            _LOG.warning("Write-failure classification failed (%s)", type(error).__name__)
            return
        if failure is None:
            return
        content = list(result.get("content") or [])
        event.result = {**result, "content": [*content, {"text": failure.model_note()}]}
        if self.sink is not None and failure.card_worthy:
            try:
                self.sink(failure, tool_use_id=tool_use.get("toolUseId"))
            except Exception as error:  # noqa: BLE001 - the board must not break the turn
                _LOG.warning("Permission card sink failed (%s)", type(error).__name__)


class WriteFailureBoard:
    """Bounded in-memory list of permission cards; one human retry per card.

    ``retry`` is atomic: the first call flips the card to ``retried`` before the
    (read-only) re-check runs, so two tabs cannot both retry. Nothing here writes
    the file or calls the agent; the UI sends the retry as an ordinary chat turn.
    """

    def __init__(self, capacity: int = 20, *, clock: Callable[[], float] = time.time) -> None:
        self.capacity = capacity
        self._clock = clock
        self._lock = threading.Lock()
        self._cards: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def add(self, failure: WriteFailure, *, tool_use_id: Any = None) -> str:
        card_id = secrets.token_hex(8)
        with self._lock:
            self._cards[card_id] = {
                "failure": failure,
                "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
                "created_at": self._clock(),
                "retry": "available" if failure.retryable else "unavailable",
                "recheck": None,
                "dismissed": False,
            }
            while len(self._cards) > self.capacity:
                self._cards.popitem(last=False)
        return card_id

    @staticmethod
    def _view(card_id: str, card: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": card_id,
            "tool_use_id": card["tool_use_id"],
            "created_at": card["created_at"],
            "retry": card["retry"],
            "recheck": card["recheck"],
            **card["failure"].as_dict(),
        }

    def cards(self, *, include_dismissed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._cards.items())
        return [
            self._view(card_id, card)
            for card_id, card in reversed(items)
            if include_dismissed or not card["dismissed"]
        ]

    def dismiss(self, card_id: str) -> bool:
        with self._lock:
            card = self._cards.get(card_id)
            if card is None:
                return False
            card["dismissed"] = True
            return True

    def retry(
        self, card_id: str, *, rechecker: Callable[[WriteFailure], dict] = recheck
    ) -> tuple[str, dict[str, Any] | None]:
        """``(outcome, card)``: ``ok`` | ``not_found`` | ``not_retryable`` | ``already_retried``."""
        with self._lock:
            card = self._cards.get(card_id)
            if card is None or card["dismissed"]:
                return "not_found", None
            if card["retry"] == "unavailable":
                return "not_retryable", self._view(card_id, card)
            if card["retry"] != "available":
                return "already_retried", self._view(card_id, card)
            card["retry"] = "retried"  # consumed before anything else runs
            failure = card["failure"]
        try:
            result = rechecker(failure)
        except Exception as error:  # noqa: BLE001 - an unknown outcome is not "clear"
            _LOG.warning("Permission card re-check failed (%s)", type(error).__name__)
            result = {"state": "unverified", "category": failure.category, "evidence": []}
        with self._lock:
            card["recheck"] = result
            return "ok", self._view(card_id, card)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            open_cards = [c for c in self._cards.values() if not c["dismissed"]]
        latest = open_cards[-1]["failure"] if open_cards else None
        return {
            "open": len(open_cards),
            "latest": (
                None if latest is None else {"category": latest.category, "path": latest.path}
            ),
        }


__all__ = [
    "CATEGORIES",
    "DEEP_LINKS",
    "MARKER",
    "REMEDIES",
    "SAFE_LINK_PREFIXES",
    "WriteFailure",
    "WriteFailureBoard",
    "WriteFailureHook",
    "classify_write_failure",
    "recheck",
]
