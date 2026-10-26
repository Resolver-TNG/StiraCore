"""Persist tool permissions. Dispatchers must also enforce permissions at execution."""

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from . import tool_registry as registry
from .tool_policy import rule_policy

DEFAULT_PATH = Path(".local/stira/tool-config.json")
ALL_BLOCKED = {"critical", "emergency", "halt", "halted"}
WRITE_BLOCKED = ALL_BLOCKED | {"warning", "block_writes", "require_confirmation"}
# Files written before #190 have no "ask_tools": they load unchanged (ON -> Always,
# OFF -> Never; exec-class ON -> Ask, see tool_policy.rule_policy).
CONFIG_KEYS = frozenset({"mode", "enabled_tools", "shell_allowlist", "ask_tools"})
# Format 2 (#190, ADR 0010 §3): rules + a generation that only grows. The last
# generation and a digest of its rules are also kept outside the file, in
# "<file>.head": a file that went back (an older copy, a pre-#190 file restored, the
# file deleted, a hand edit) no longer matches and loads *locked* (every tool Never)
# until a human saves the rules again from the settings page.
SCHEMA_VERSION = 2
FILE_KEYS = CONFIG_KEYS | {"schema_version", "generation"}
# ADR 0015 Addendum rev 4 (#449, phase 3): the localhost ports ``page_check`` may open.
# Optional in the file (absent = none, so every existing file and its digest stay the
# same); part of the checked rules (file, head, anchor) but not of ``as_dict`` (the tool
# rules the APIs, the hub and the history show), so no other save path can set it. Only
# the person-only route of the settings page changes it; every other save keeps the
# value the head recorded.
PAGE_CHECK_PORTS = "page_check_ports"
OPTIONAL_KEYS = frozenset({PAGE_CHECK_PORTS})
PAGE_CHECK_PORTS_MAX = 8
PAGE_CHECK_PORT_MIN = 1024


def checked_page_check_ports(value: Any) -> list[int]:
    """Sorted distinct ports (integers 1024-65535, at most 8), else ``ValueError``."""
    if not isinstance(value, (list, tuple)) or len(value) > PAGE_CHECK_PORTS_MAX:
        raise ValueError(f"page_check_ports must be a list of at most {PAGE_CHECK_PORTS_MAX} ports")
    if not all(type(p) is int and PAGE_CHECK_PORT_MIN <= p <= 65535 for p in value):
        raise ValueError("page_check_ports must be ports 1024-65535")
    return sorted(set(value))


HISTORY_FILE = "settings-history.jsonl"  # same name as settings_hub.HISTORY_FILE
_LOG = logging.getLogger(__name__)
_LOGGED_LOCKS: set[tuple[str, str]] = set()


def head_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".head")


def _rules_digest(rules: dict, generation: int) -> str:
    blob = json.dumps({"generation": generation, "rules": rules}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_head(path: Path) -> dict | None:
    """The recorded head, or None if there is none. Unreadable raises ValueError."""
    target = head_path(path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("tool-config head is unreadable") from error
    generation = data.get("generation") if isinstance(data, dict) else None
    if type(generation) is not int or generation < 1 or not isinstance(data.get("digest"), str):
        raise ValueError("tool-config head is invalid")
    return data


def _history_state(path: Path) -> str:
    """``absent`` / ``no_tool_changes`` / ``tool_changes`` / ``unreadable`` (#190 review)."""
    history = Path(path).parent / HISTORY_FILE
    try:
        text = history.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent"
    except (OSError, UnicodeError):
        return "unreadable"  # not "no history": an integrity error
    return "tool_changes" if '"tools.' in text else "no_tool_changes"


def latch_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".locked")


# Opt-in rollback anchor (#273): a restored data directory brings back a tool-config.json
# and a head that agree with each other, so the checks above cannot see that the rules
# went back to an older (possibly looser) generation, and any marker *inside* the data
# directory is rolled back with it. When this variable names a directory outside the
# data directory, the highest generation and its digest are also kept there; a file
# older than that, or the same generation with other rules, locks. Once the variable is
# set the anchor is required: a missing one locks (``anchor_missing``) and only a human
# creates it (``stira tool-rules init-anchor``) or restores it (a RECOVER save). Unset =
# unchanged behaviour.
ANCHOR_ENV = "STIRA_TOOL_RULES_ANCHOR_DIR"
_ANCHOR_LIMIT = 65536
# Data roots the anchor must not be inside (the server registers its data directory;
# the rules' own directory always counts).
_DATA_ROOTS: set[str] = set()


# Install identity (#410 review): a random ID made when the anchor is created, kept in the
# anchor and in the head of every save. A file + head from another install (no ID, or its
# own) never matches, whatever its generation or rules. Not a secret, not a path.
_INSTALL_ID = re.compile(r"[0-9a-f]{32}")
# One lock for every recording load and save in this process (#410 review): a load that
# moves the anchor up and a save cannot interleave. Across processes (a CLI next to a
# server, two servers), every anchor write — and a whole save with the anchor on — also
# holds an OS file lock next to the anchor (round 2), and re-reads the anchor under it:
# the check "never a lower generation" and the replace are one step for every process.
_RULES_LOCK = RLock()
_ANCHOR_LOCK_TIMEOUT = 5.0  # seconds; not obtained in time = the write fails (closed)
_ANCHOR_LOCKS_HELD: dict[str, int] = {}  # re-entry in this process (under _RULES_LOCK)


def valid_install_id(value: Any) -> bool:
    return isinstance(value, str) and _INSTALL_ID.fullmatch(value) is not None


def new_install_id() -> str:
    return secrets.token_hex(16)


class AnchorUnavailable(ValueError):
    """The anchor cannot be trusted: ``reason`` is the lock reason (no path)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AnchorWriteError(OSError):
    """Saved rules whose anchor could not be updated: the save counts as failed.

    ``reason``: a lock reason when the anchor, read again under its lock, turned out
    missing or unusable (``anchor_missing`` / ``anchor_unsafe`` / ``anchor_unreadable``)."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(message)


def register_data_root(path: Path) -> None:
    """The server's data directory: an anchor inside it is a configuration error."""
    _DATA_ROOTS.add(os.path.realpath(Path(path).expanduser()))


def anchor_dir() -> Path | None:
    value = os.environ.get(ANCHOR_ENV, "").strip()
    return Path(value).expanduser().absolute() if value else None


def anchor_path(path: Path) -> Path | None:
    """The anchor file of one tool-config path (named by a hash, never by the path)."""
    directory = anchor_dir()
    if directory is None:
        return None
    name = hashlib.sha256(_latch_key(path).encode("utf-8")).hexdigest()[:32]
    return directory / f"tool-rules-{name}.json"


def anchor_lock_path(path: Path) -> Path | None:
    """The OS lock file next to the anchor (fixed name: the anchor's + ``.lock``)."""
    target = anchor_path(path)
    return None if target is None else target.with_name(target.name + ".lock")


@contextmanager
def _anchor_file_lock(path: Path) -> Iterator[None]:
    """Hold the anchor's OS file lock (``flock``; ``msvcrt.locking`` on Windows,
    unverified), waiting up to ``_ANCHOR_LOCK_TIMEOUT``. Opened like the owner locks: no
    final symlink, no FIFO wait, a regular file with one link owned by this user.
    Anything else, or the timeout: :class:`AnchorWriteError` — the write does not happen.
    Call with ``_RULES_LOCK`` held (re-entry in this process is counted, not re-locked)."""
    from stiracore import owner_lock

    lock = anchor_lock_path(path)
    if lock is None:
        yield
        return
    key = str(lock)
    if _ANCHOR_LOCKS_HELD.get(key):
        _ANCHOR_LOCKS_HELD[key] += 1
        try:
            yield
        finally:
            _ANCHOR_LOCKS_HELD[key] -= 1
        return
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, owner_lock._OPEN_FLAGS, 0o600)
    except OSError as error:  # a symlink is refused here (O_NOFOLLOW)
        raise AnchorWriteError("the tool-rule anchor lock cannot be opened") from error
    try:
        if owner_lock._check_descriptor(fd) is not None:
            raise AnchorWriteError("the tool-rule anchor lock is unsafe")
        deadline = time.monotonic() + _ANCHOR_LOCK_TIMEOUT
        while True:
            try:
                owner_lock._try_lock(fd)
                break
            except OSError as error:
                if error.errno not in owner_lock._CONTENTION_ERRNOS:
                    raise AnchorWriteError("the tool-rule anchor lock failed") from error
                if time.monotonic() >= deadline:
                    raise AnchorWriteError("the tool-rule anchor lock is held") from error
                time.sleep(0.02)
        _ANCHOR_LOCKS_HELD[key] = 1
        try:
            yield
        finally:
            _ANCHOR_LOCKS_HELD.pop(key, None)
            owner_lock._unlock(fd)
    finally:
        os.close(fd)


def anchor_inside_data_dir(path: Path, data_roots=()) -> bool:
    """Whether the anchor directory is inside a data root (resolved, symlinks followed):
    the rules' own directory, a registered server data directory or ``data_roots``. Such
    an anchor is restored together with the data and protects nothing."""
    directory = anchor_dir()
    if directory is None:
        return False
    anchor = os.path.normcase(os.path.realpath(directory))
    roots = {os.path.realpath(Path(path).expanduser().absolute().parent), *_DATA_ROOTS}
    roots |= {os.path.realpath(Path(root).expanduser()) for root in data_roots}
    for root in roots:
        root = os.path.normcase(root)
        if anchor == root or anchor.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def read_anchor(path: Path) -> dict | None:
    """``{generation, digest[, rules]}``, or None when the variable is unset or no file.

    Read like a lock file (no final symlink, regular file, one link, this user's,
    bounded). Raises :class:`AnchorUnavailable` (``anchor_unsafe`` / ``anchor_unreadable``).
    """
    from stiracore.owner_lock import UnsafeLockFileError, read_private_file

    target = anchor_path(path)
    if target is None:
        return None
    try:
        data = json.loads(read_private_file(target, _ANCHOR_LIMIT).decode("utf-8"))
    except FileNotFoundError:
        return None
    except UnsafeLockFileError:
        raise AnchorUnavailable("anchor_unsafe") from None
    except (OSError, ValueError):
        raise AnchorUnavailable("anchor_unreadable") from None
    generation = data.get("generation") if isinstance(data, dict) else None
    if type(generation) is not int or generation < 1 or not isinstance(data.get("digest"), str):
        raise AnchorUnavailable("anchor_unreadable")
    if "install_id" in data and not valid_install_id(data["install_id"]):
        raise AnchorUnavailable("anchor_unreadable")
    return data


def anchor_problem(path: Path) -> str | None:
    """Why the anchor (when the variable is set) locks before any rule is read, or None."""
    if anchor_dir() is None:
        return None
    if anchor_inside_data_dir(path):
        return "anchor_misconfigured"
    try:
        anchor = read_anchor(path)
    except AnchorUnavailable as error:
        return error.reason
    return "anchor_missing" if anchor is None else None


def _rules_snapshot(rules: dict) -> tuple[dict, int]:
    """The rules without secret-looking commands (review round 5), as the head keeps them.
    Returns (snapshot, number of commands left out). Compared with ``rules_loosening``, a
    next generation that still holds a left-out command counts as looser (the command looks
    added); the anchor keeps no snapshot at all when one was left out (#410 review)."""
    found = secret_commands(rules["shell_allowlist"])
    snapshot = dict(rules)
    snapshot["shell_allowlist"] = [c for c in rules["shell_allowlist"] if c not in found]
    return snapshot, len(found)


def _write_anchor(
    path: Path,
    generation: int,
    digest: str,
    rules: dict | None = None,
    install_id: str | None = None,
    *,
    create_only: bool = False,
    recreate: bool = False,
) -> None:
    """``rules``: the rules of ``generation``, kept so a load can tell a save whose anchor
    update failed from a restored file (#273); left out when a secret-looking command
    would be dropped or the anchor would exceed its size limit (a newer generation then
    locks). ``install_id``: this install's identity. Never moves the anchor back: an
    anchor already at a higher generation (or the same one with other rules) refuses
    (``AnchorWriteError``): checked again under the OS file lock that every process
    takes, so the check and the replace are one step. ``create_only``: any existing
    anchor refuses (``init-anchor``). An anchor that is missing or unusable when read
    under the lock is written only by ``init-anchor`` (``create_only``) or a RECOVER save
    (``recreate``); for every other caller it refuses with that lock reason (round 3)."""
    target = anchor_path(path)
    if target is None:
        return
    body: dict = {"schema_version": 1, "generation": generation, "digest": digest}
    if valid_install_id(install_id):
        body["install_id"] = install_id
    if rules is not None:
        snapshot, left_out = _rules_snapshot(rules)
        if not left_out:
            body["rules"] = snapshot
            if len(json.dumps(body, ensure_ascii=False, indent=2).encode()) > _ANCHOR_LIMIT:
                body.pop("rules")
    with _RULES_LOCK, _anchor_file_lock(path):
        if create_only and os.path.lexists(target):
            raise AnchorWriteError("an anchor already exists")
        try:
            current = read_anchor(path)
        except AnchorUnavailable as error:
            if not (create_only or recreate):
                raise AnchorWriteError("the tool-rule anchor is unusable", error.reason) from None
            current = None
        if current is None and not (create_only or recreate):
            raise AnchorWriteError("the tool-rule anchor is missing", "anchor_missing")
        if current is not None and (
            current["generation"] > generation
            or (current["generation"] == generation and current["digest"] != digest)
        ):
            raise AnchorWriteError("the tool-rule anchor already holds a newer generation")
        _atomic_write(target, body)


def _probe_anchor_dir() -> None:
    """Before a save replaces anything: the anchor directory takes a file (#273)."""
    directory = anchor_dir()
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".tool-rules-probe-")
        os.close(fd)
        os.unlink(temporary)
    except OSError as error:
        raise AnchorWriteError("the tool-rule anchor directory is not writable") from error


def _save_identity(path: Path, head: dict | None) -> str | None:
    """The install identity a save writes (#410 review). With the anchor: the anchor's,
    or a new one when it has none (an older anchor, or a recovery of a missing/unusable
    one) — never the identity of the rules being replaced, which may be foreign.
    Without the anchor: the head's, kept as it was."""
    if anchor_dir() is None:
        value = head.get("install_id") if head else None
        return value if valid_install_id(value) else None
    try:
        anchor = read_anchor(path)
    except AnchorUnavailable:
        anchor = None
    value = anchor.get("install_id") if anchor else None
    return value if valid_install_id(value) else new_install_id()


def _anchor_generation(path: Path) -> int:
    """For numbering a save only: an unusable anchor counts as 0 (a recovery rewrites it)."""
    try:
        anchor = read_anchor(path)
    except AnchorUnavailable:
        return 0
    return anchor["generation"] if anchor else 0


# Integrity failures latch (#190 review B3): once a check failed, the rules stay locked
# in this process (and through the on-disk marker) even if the files are put back,
# until a human saves them again with the recovery confirmation.
_LATCHED: dict[str, str] = {}
_LATCH_LOCK = RLock()


def _latch_key(path: Path) -> str:
    return str(Path(path).expanduser().absolute())


def latched_reason(path: Path) -> str | None:
    with _LATCH_LOCK:
        reason = _LATCHED.get(_latch_key(path))
    if reason is not None:
        return reason
    marker = latch_path(path)
    if marker.exists():
        try:
            reason = json.loads(marker.read_text(encoding="utf-8")).get("reason") or "latched"
        except (OSError, ValueError, AttributeError):
            reason = "latched"
        # A lock read from disk (e.g. after a restart) latches in this process too: an
        # external process deleting only the marker does not unlock (review round 3).
        with _LATCH_LOCK:
            _LATCHED.setdefault(_latch_key(path), str(reason))
        return str(reason)
    return None


def _latch(path: Path, reason: str) -> None:
    with _LATCH_LOCK:
        _LATCHED.setdefault(_latch_key(path), reason)
    try:
        if not latch_path(path).exists():
            _atomic_write(latch_path(path), {"reason": reason})
    except OSError:
        _LOG.warning("Could not record the tool-rule lock on disk (kept in memory)")


def _unlatch(path: Path) -> None:
    """Only a confirmed human recovery save calls this. Raises if the marker stays."""
    marker = latch_path(path)
    if marker.exists():
        marker.unlink()
    with _LATCH_LOCK:
        _LATCHED.pop(_latch_key(path), None)


def rules_loosening(
    previous: Any,
    current: Any,
    *,
    locked: bool = False,
    context: str | None = None,
    allowance: str = "off",
) -> list[str]:
    """Every way ``current`` may be less strict than ``previous`` (reviews round 3-4).

    The one loosening check for every save path (new/legacy API, hub, rollback) and for
    completing an interrupted save. ``locked``: the previous rules are a lock (every tool
    Never, unregistered ones too); saving over them is a recovery, always a loosening.

    Field by field, with an order of strictness: ``mode`` (custom < trust_all), each
    tool's Always/Ask/Never rule (from ``enabled_tools``/``ask_tools``/``mode``), and
    ``shell_allowlist`` (a command added is looser). A field without a known order, or a
    snapshot that cannot be compared, counts as possibly looser.

    The YOLO window (ADR 0012 §2) is judged here too: ``context`` names a YOLO context
    (``stiracore.yolo.loosening.YOLO_CONTEXTS``) and the snapshots are window
    snapshots. In every other context a ``yolo*`` key is reserved: the reasons then
    start with ``yolo_not_settable`` (the caller refuses). Snapshots without such a key
    and without a context are judged exactly as before.

    ADR 0025 §8: the session allowance is the field ``session_allow`` (``none <
    active``). Its issuance is allowed only in the context ``card_allow_again``; every
    other context treats a ``session_allow*`` key as reserved (``allow_not_settable``).
    """
    from stiracore.yolo.admin_guard import reserved_keys
    from stiracore.yolo.loosening import (
        ALLOW_CONTEXT,
        ALLOW_NOT_SETTABLE,
        NOT_SETTABLE,
        YOLO_CONTEXTS,
        allow_loosening,
        allow_reserved_keys,
        window_loosening,
    )

    if context in YOLO_CONTEXTS:
        return window_loosening(previous, current, context, allowance=allowance)
    if context == ALLOW_CONTEXT:
        return allow_loosening(previous, current)
    if allow_reserved_keys(previous) or allow_reserved_keys(current):
        return [ALLOW_NOT_SETTABLE, *_tool_rules_loosening(previous, current, locked=locked)]
    if reserved_keys(previous) or reserved_keys(current):
        return [NOT_SETTABLE, *_tool_rules_loosening(previous, current, locked=locked)]
    return _tool_rules_loosening(previous, current, locked=locked)


def _tool_rules_loosening(previous: Any, current: Any, *, locked: bool = False) -> list[str]:
    from .tool_policy import loosened

    if locked:
        return ["locked_rules", *_tool_rules_loosening(previous, current)]
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return ["snapshot"]
    found = []
    if PAGE_CHECK_PORTS in previous or PAGE_CHECK_PORTS in current:
        # Optional (absent = none); a port added is looser, one removed is not.
        previous, current = dict(previous), dict(current)
        try:
            before_ports = set(checked_page_check_ports(previous.pop(PAGE_CHECK_PORTS, [])))
            after_ports = set(checked_page_check_ports(current.pop(PAGE_CHECK_PORTS, [])))
        except ValueError:
            return ["snapshot"]
        if after_ports - before_ports:
            found.append(PAGE_CHECK_PORTS)
    if set(previous) != set(current):
        return ["snapshot"]
    try:
        before = ToolConfig(**{key: previous[key] for key in CONFIG_KEYS})
        after = ToolConfig(**{key: current[key] for key in CONFIG_KEYS})
    except (KeyError, TypeError, ValueError):
        return ["snapshot"]
    for key in sorted(set(previous) - CONFIG_KEYS):
        if previous[key] != current[key]:
            found.append(key)  # unknown field: no order of strictness
    if before.mode != after.mode and after.mode == "trust_all":
        found.append("mode")
    if set(after.shell_allowlist) - set(before.shell_allowlist):
        found.append("shell_allowlist")
    names = (
        (set(registry.READ_TOOLS) | set(registry.WRITE_TOOLS) | before.enabled_tools)
        | after.enabled_tools
        | before.ask_tools
        | after.ask_tools
    )
    if loosened(
        {name: before.policy_of(name, effective=False) for name in names},
        {name: after.policy_of(name, effective=False) for name in names},
    ):
        found.append("tools")
    return found


def secret_commands(commands: Any) -> list[str]:
    """Allowlist entries that ``mask_secrets`` would change (a token in a command)."""
    from stiracore.observability.masking import mask_secrets

    if not isinstance(commands, list):
        return []
    return [c for c in commands if isinstance(c, str) and mask_secrets(c) != c]


class SecretInAllowlist(ValueError):
    """A command containing a secret cannot be saved in the shell allowlist (#190): not
    added, and not written again when an older install had saved one (#270)."""


class ToolRulesLocked(Exception):
    """Saving locked rules needs the typed recovery confirmation (``recover=True``)."""


RECOVER_WORD = "RECOVER"


def _atomic_write(target: Path, data: dict, precommit: Callable[[], None] | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tool-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if precommit is not None:
            precommit()  # may raise: nothing is replaced
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def level_name(level: Any) -> str:
    value = getattr(level, "name", None)
    return (
        str(value if value is not None else getattr(level, "value", level))
        .rsplit(".", 1)[-1]
        .strip()
        .lower()
    )


@dataclass
class ToolConfig:
    mode: str = "trust_all"
    enabled_tools: set[str] = field(
        default_factory=lambda: (set(registry.READ_TOOLS) | set(registry.WRITE_TOOLS)) - {"shell"}
    )
    shell_allowlist: list[str] = field(
        default_factory=lambda: ["pwd", "ls", "cat", "echo", "date", "whoami"]
    )
    # Enabled tools that still need a human approval for every call: the "Ask" rule
    # (#190, ADR 0010). Enabled and not listed is "Always"; not enabled is "Never".
    ask_tools: set[str] = field(default_factory=set)
    path: Path = field(default=DEFAULT_PATH, repr=False)
    lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)
    # Monotonic rule generation (0 = never saved in format 2) and, for a configuration
    # that failed its integrity check, why it is locked (every tool Never).
    generation: int = field(default=0, repr=False, compare=False)
    integrity_error: str | None = field(default=None, repr=False, compare=False)
    # The install identity the head carried (#410 review); compared with the anchor's.
    install_id: str | None = field(default=None, init=False, repr=False, compare=False)
    # ``None``: not given (a save keeps the ports the head recorded); a list: these.
    page_check_ports: list[int] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.mode not in ("custom", "trust_all"):
            raise ValueError("mode must be custom or trust_all")
        if not isinstance(self.enabled_tools, (list, set)) or not all(
            isinstance(n, str) for n in self.enabled_tools
        ):
            raise ValueError("enabled_tools must be an array of tool names")
        if not isinstance(self.shell_allowlist, list) or not all(
            isinstance(c, str) for c in self.shell_allowlist
        ):
            raise ValueError("shell_allowlist must be an array of commands")
        if any(not c.strip() or any(x in c for x in "\r\n;&|<>`$()") for c in self.shell_allowlist):
            raise ValueError("Allow only nonempty literal commands without shell operators")
        if not isinstance(self.ask_tools, (list, set)) or not all(
            isinstance(n, str) for n in self.ask_tools
        ):
            raise ValueError("ask_tools must be an array of tool names")
        if self.page_check_ports is not None:
            self.page_check_ports = checked_page_check_ports(self.page_check_ports)
        self.enabled_tools = set(self.enabled_tools)
        self.ask_tools = set(self.ask_tools)
        self.shell_allowlist = list(dict.fromkeys(c.strip() for c in self.shell_allowlist))
        self.path = Path(self.path)

    @classmethod
    def locked(cls, path: Path, reason: str, generation: int = -1) -> "ToolConfig":
        """Every tool Never: the saved rules cannot be trusted (ADR 0010 §3)."""
        config = cls(mode="custom", enabled_tools=set(), shell_allowlist=[], path=path)
        config.integrity_error = reason
        config.generation = generation
        if (str(path), reason) not in _LOGGED_LOCKS:
            _LOGGED_LOCKS.add((str(path), reason))
            _LOG.error("Tool rules locked (every tool Never): %s", reason)
        return config

    @classmethod
    def first_run_defaults(cls, path: Path) -> "ToolConfig":
        """A genuinely new install: reads Always, local writes Ask, external sends and
        exec Never/hidden. Never "everything Always" (#190 review B1)."""
        from .tool_policy import EXTERNAL_SEND_TOOLS

        writes = set(registry.WRITE_TOOLS) - EXTERNAL_SEND_TOOLS - set(registry.EXEC_TOOL_NAMES)
        return cls(
            mode="custom",
            enabled_tools=set(registry.READ_TOOLS) | writes,
            ask_tools=writes,
            path=path,
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH, *, readonly: bool = False) -> "ToolConfig":
        """Load the rules; any failed integrity check locks (and latches) them.

        ON -> Always migration happens only for a pre-#190 file with neither head nor
        settings history. Without file, head and history: the safe first-run defaults.
        ``readonly`` (display-only callers such as ``AgentConfig.load``) never writes:
        no migration/first-run record and no lock marker; the rules enforced at runtime
        always come from a recording load.
        """
        path = Path(path)
        if readonly:
            return cls._load(path, readonly=True)
        with _RULES_LOCK:  # a recording load may move the anchor up (#410 review)
            return cls._load(path, readonly=False)

    @classmethod
    def _load(cls, path: Path, *, readonly: bool) -> "ToolConfig":
        reason = latched_reason(path)
        if reason is not None:
            return cls.locked(path, reason)
        config, reason = None, anchor_problem(path)  # before anything is recorded (#273)
        if reason is None:
            config, reason = cls._load_checked(path, record=not readonly)
        if reason is None:
            reason = config._check_anchor(record=not readonly)
        if reason is not None:
            if not readonly:
                _latch(path, reason)
            return cls.locked(path, reason, config.generation if config else -1)
        return config

    @classmethod
    def check_only(cls, path: Path) -> str | None:
        """Why a load would lock, or None — reading only: no record, marker or latch."""
        path = Path(path)
        marker = latch_path(path)
        if marker.exists():
            try:
                return str(
                    json.loads(marker.read_text(encoding="utf-8")).get("reason") or "latched"
                )
            except (OSError, ValueError, AttributeError):
                return "latched"
        reason = anchor_problem(path)
        if reason is None:
            config, reason = cls._load_checked(path, record=False)
        if reason is None:
            reason = config._check_anchor(record=False)
        return reason

    @classmethod
    def _load_checked(
        cls, path: Path, *, record: bool = True
    ) -> tuple["ToolConfig | None", str | None]:
        try:
            head = _read_head(path)
        except ValueError:
            return None, "head_unreadable"
        history = _history_state(path)
        if history == "unreadable":
            return None, "history_unreadable"
        # Only "nothing at all" is a new install: no head, no lock marker and no history
        # file (even an empty or tool-free one counts as evidence, review round 3).
        no_evidence = head is None and history == "absent" and not latch_path(path).exists()
        try:
            anchored = read_anchor(path) is not None
        except AnchorUnavailable as error:
            return None, error.reason
        if no_evidence and anchored:
            # A reset data directory (or one restored from before the first save) while
            # the opt-in anchor outside it remembers rules (#273): not a new install.
            return None, "anchor_rollback"
        if not path.exists():
            if not no_evidence:
                return None, "missing"
            config = cls.first_run_defaults(path)
            if not record:
                return config, None
            try:
                config.save()  # recorded at once: from now on there is a head to check
            except (OSError, ValueError, ToolRulesLocked):
                return None, "init_failed"
            return config, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "malformed"
        if not isinstance(data, dict):
            return None, "malformed"
        if "schema_version" not in data:
            if set(data) - (CONFIG_KEYS - {"ask_tools"}):
                return None, "incomplete" if not set(data) - CONFIG_KEYS else "malformed"
            if not no_evidence:
                return None, "rolled_back_to_legacy"
            dropped = secret_commands(data.get("shell_allowlist"))
            if dropped:
                # Never carried into the new format (#270): dropping a command only
                # tightens the rules. The count only, never the value.
                _LOG.warning(
                    "Migration dropped %d secret-looking command(s) from the shell allowlist",
                    len(dropped),
                )
                data = {
                    **data,
                    "shell_allowlist": [c for c in data["shell_allowlist"] if c not in dropped],
                }
            try:
                config = cls(**data, path=path)
                if record:
                    config.save()  # migrate once: later rollbacks are detected
            except (OSError, ValueError, TypeError):
                return None, "migration_failed"
            return config, None
        if (
            set(data) - FILE_KEYS - OPTIONAL_KEYS
            or set(FILE_KEYS) - set(data)
            or data["schema_version"] != SCHEMA_VERSION
            or type(data["generation"]) is not int
            or data["generation"] < 1
        ):
            return None, "incomplete"
        generation = data["generation"]
        try:
            config = cls(
                **{key: data[key] for key in CONFIG_KEYS},
                page_check_ports=data.get(PAGE_CHECK_PORTS, []),
                path=path,
            )
        except (TypeError, ValueError):
            return None, "malformed"
        if secret_commands(config.shell_allowlist):
            # Count only, never the value; the head does not copy these entries.
            _LOG.warning(
                "The shell allowlist holds %d secret-looking command(s); remove them",
                len(secret_commands(config.shell_allowlist)),
            )
        config.generation = generation
        if head is None:
            return config, "head_missing"
        if valid_install_id(head.get("install_id")):
            config.install_id = head["install_id"]
        digest = _rules_digest(config.rules_dict(), generation)
        if generation == head["generation"]:
            return (config, None) if head["digest"] == digest else (config, "edited")
        if generation == head["generation"] + 1:
            # An interrupted save (file replaced, head not yet) — or a hand edit that
            # bumped the generation. Completed only if it loosens nothing compared with
            # the full rules the head recorded (review B2); otherwise, or without that
            # snapshot, or if the head cannot be written, the rules stay locked.
            previous = head.get("rules")
            if not isinstance(previous, dict) or config._looser_than(previous):
                return config, "unconfirmed_generation"
            if not record:
                return config, None  # read-only: completed in memory, recorded on next save
            try:
                config._write_head()
            except OSError:
                return config, "head_repair_failed"
            return config, None
        return config, (
            "generation_rollback" if generation < head["generation"] else "generation_jump"
        )

    def _check_anchor(self, *, record: bool) -> str | None:
        """#273: compare checked rules with the opt-in anchor outside the data directory.

        Older than the anchor (a restored backup) or the same generation with other
        rules: locked. One generation newer (a save whose anchor update failed and was
        reported): the anchor moves up only if the rules loosen nothing compared with the
        anchor's snapshot (``rules_loosening``) — otherwise, without a snapshot, more than
        one generation newer (a file restored from another install), or if the anchor
        cannot be written, the rules lock. A missing, unsafe or misplaced anchor locks
        (:func:`anchor_problem`).
        """
        if anchor_dir() is None:
            return None
        reason = anchor_problem(self.path)
        if reason is not None:
            return reason
        anchor = read_anchor(self.path)
        if anchor is None:  # removed since the check above
            return "anchor_missing"
        rules = self.rules_dict()
        digest = _rules_digest(rules, self.generation)
        if self.generation < anchor["generation"]:
            return "anchor_rollback"
        identity = anchor.get("install_id")
        if identity is not None and self.install_id != identity:
            # A file + head written by another install (or before init-anchor), whatever
            # its generation and rules (#410 review).
            return "anchor_foreign_install"
        if self.generation > anchor["generation"] + 1:
            # Not a save of this install (each numbers one above the anchor): a file and
            # head from another install or another path, restored here (#273).
            return "anchor_generation_jump"
        if self.generation == anchor["generation"] + 1:
            # A save whose anchor update failed (reported as failed) — or a foreign file
            # one step ahead. Completed only if it loosens nothing compared with the rules
            # the anchor recorded (the one loosening check); no snapshot: locked.
            previous = anchor.get("rules")
            if identity is None or not isinstance(previous, dict) or self._looser_than(previous):
                return "anchor_unconfirmed_generation"
            if record:
                try:
                    _write_anchor(self.path, self.generation, digest, rules, identity)
                except AnchorWriteError as error:
                    return error.reason or "anchor_unwritable"
                except OSError:
                    return "anchor_unwritable"
            return None
        return None if digest == anchor["digest"] else "anchor_mismatch"

    def _looser_than(self, previous: dict) -> bool:
        """Whether these rules may be looser than ``previous`` in *any* field."""
        return bool(rules_loosening(previous, self.rules_dict()))

    def rules_dict(self) -> dict:
        """The checked rules as the file, head and anchor keep them: :meth:`as_dict` plus
        ``page_check_ports`` when there are any (absent = none: older files unchanged)."""
        rules = self.as_dict()
        if self.page_check_ports:
            rules[PAGE_CHECK_PORTS] = list(self.page_check_ports)
        return rules

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode,
                "enabled_tools": sorted(self.enabled_tools),
                "shell_allowlist": list(self.shell_allowlist),
                "ask_tools": sorted(self.ask_tools),
            }

    def policy_of(self, name: str, *, effective: bool = True) -> str:
        """ "always" / "ask" / "never" for ``name`` (ADR 0010; exec class is never Always).

        ``effective=False``: the rule as saved (a capped Always stays Always, ADR 0019)."""
        with self.lock:
            return rule_policy(self, name, effective=effective)

    def _write_head(self) -> None:
        rules = self.rules_dict()
        # Never copy a secret-looking command into the head (review round 5); a next
        # generation still holding it then looks looser and is not completed.
        snapshot, left_out = _rules_snapshot(rules)
        body = {
            "schema_version": 1,
            "generation": self.generation,
            "digest": _rules_digest(rules, self.generation),
            # The rules of this generation: an interrupted next save is compared with
            # them (review B2).
            "rules": snapshot,
        }
        if left_out:
            body["redacted_commands"] = left_out
        if valid_install_id(self.install_id):
            body["install_id"] = self.install_id
        _atomic_write(head_path(self.path), body)

    def save(self, precommit: Callable[[], None] | None = None, *, recover: bool = False) -> None:
        """Write the rules as the next generation: replace the file, then its head.

        ``precommit`` runs right before the file is replaced and may raise to abort.
        Locked (latched) rules are only overwritten with ``recover=True`` — a human
        save with the typed recovery confirmation — which also clears the lock.
        """
        with _RULES_LOCK, self.lock:
            locked_reason = latched_reason(self.path)
            if locked_reason is None:
                # #273: a missing/unsafe anchor is restored by a RECOVER save only.
                problem = anchor_problem(self.path)
                if problem is not None:
                    _latch(self.path, problem)
                    locked_reason = problem
            if locked_reason is not None and not recover:
                raise ToolRulesLocked(locked_reason)
            # Not only a new one (#190): an entry already saved by an older install is
            # never written again either (#270) — remove it to save the rules.
            found = secret_commands(self.shell_allowlist)
            if found:
                raise SecretInAllowlist(
                    f"The shell allowlist holds {len(found)} secret-looking command(s); "
                    "remove them to save the tool rules"
                )
            with ExitStack() as stack:
                if anchor_dir() is not None:
                    if anchor_inside_data_dir(self.path):
                        raise AnchorWriteError("the tool-rule anchor is inside the data directory")
                    _probe_anchor_dir()  # nothing is replaced when the anchor cannot follow
                    # The whole save under the anchor's OS lock (round 2): its numbering,
                    # rules, head and anchor, against every other process. Not obtained:
                    # AnchorWriteError before anything is replaced.
                    stack.enter_context(_anchor_file_lock(self.path))
                    if locked_reason is None:
                        # Read again under the lock (round 3): an anchor that went missing
                        # or unusable since the check above is restored by RECOVER only.
                        problem = anchor_problem(self.path)
                        if problem is not None:
                            _latch(self.path, problem)
                            raise ToolRulesLocked(problem)
                self._save_locked(precommit, locked_reason, recover)

    def _save_locked(
        self, precommit: Callable[[], None] | None, locked_reason: str | None, recover: bool
    ) -> None:
        """The writes of :meth:`save`, with its locks held."""
        try:
            head = _read_head(self.path)
        except ValueError:
            head = None  # only reachable through a recovery save
        current = head["generation"] if head else 0
        # Above the anchor too (#273): a recovery after a restore never reuses a
        # generation number the anchor already holds for other rules.
        generation = max(current, self.generation, _anchor_generation(self.path)) + 1
        if self.page_check_ports is None:
            # Not given by this save (every path but the person-only ports route): keep
            # the ports of the generation the head recorded. No head (first save) or a
            # save over locked rules (a recovery: the record is not trusted): none.
            recorded = (head or {}).get("rules") if locked_reason is None else None
            try:
                self.page_check_ports = checked_page_check_ports(
                    recorded.get(PAGE_CHECK_PORTS, []) if isinstance(recorded, dict) else []
                )
            except ValueError:
                self.page_check_ports = []
        rules = self.rules_dict()
        identity = _save_identity(self.path, head)
        # ADR 0025 §5: every session allowance ends before the new rules become visible
        # (fence + 1, in memory; a save that fails later only ended them early).
        from stiracore.yolo.session_allow import publish_end

        publish_end("rule_changed")
        _atomic_write(
            self.path,
            {"schema_version": SCHEMA_VERSION, "generation": generation, **rules},
            precommit,
        )
        self.generation = generation
        self.integrity_error = None
        self.install_id = identity
        self._write_head()
        if anchor_dir() is not None:
            # #273: the save counts only once the anchor holds it. A failure is the
            # caller's error and a lock stays latched (no unlatch below).
            try:
                digest = _rules_digest(rules, generation)
                # Re-creating a missing/unusable anchor: a RECOVER save of locked rules only.
                recreate = recover and locked_reason is not None
                _write_anchor(self.path, generation, digest, rules, identity, recreate=recreate)
            except AnchorWriteError as error:
                if error.reason is not None:
                    _latch(self.path, error.reason)
                _LOG.error("Could not update the tool-rule anchor (%s)", ANCHOR_ENV)
                raise
            except OSError as error:
                _LOG.error("Could not update the tool-rule anchor (%s)", ANCHOR_ENV)
                raise AnchorWriteError("the tool-rule anchor could not be written") from error
        if locked_reason is not None:
            _unlatch(self.path)  # raises (still locked) if the marker stays

    def get_allowed_tools(self, breaker_level: Any = None) -> list[Any]:
        level = level_name(breaker_level)
        with self.lock:
            tools = {**registry.READ_TOOLS, **registry.WRITE_TOOLS}
            if not registry.exec_tools_enabled():
                tools = {n: t for n, t in tools.items() if not registry.is_exec_tool(n)}
            for name in registry.HOST_BOUND_TOOLS:
                tools.pop(name, None)  # bound by the chat server only (#463)
            for name in registry.unconfigured_tools():
                tools.pop(name, None)  # #583: e.g. web_search without an endpoint
            return [
                tool
                for name, tool in tools.items()
                if level not in ALL_BLOCKED
                and (self.mode == "trust_all" or name in self.enabled_tools)
                and not (level in WRITE_BLOCKED and name in registry.WRITE_TOOLS)
            ]

    def check_shell_command(self, cmd: str, breaker_level: Any = None) -> bool:
        """Match entire commands, not executable prefixes; pass the live breaker level."""
        with self.lock:
            return (
                isinstance(cmd, str)
                and level_name(breaker_level) not in WRITE_BLOCKED
                and (
                    self.mode == "trust_all"
                    or ("shell" in self.enabled_tools and cmd.strip() in self.shell_allowlist)
                )
            )
