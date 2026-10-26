"""Read-only path probes for write-failure diagnosis (#244).

Rules every function here follows:

* **Read only.** ``lstat``/``statvfs``, extended-attribute and ACL *queries*,
  ``os.access``. Nothing changes a flag, an attribute, an ACL or a permission,
  and no external command (``xattr``, ``chflags``, ``ls`` …) is run.
* **TCC paths are never touched.** :func:`tcc_zone` recognises macOS
  privacy-protected locations (Desktop, Documents, Downloads, iCloud Drive,
  ``/Volumes/*`` …) *lexically*; every probe refuses such a path before any
  system call, so no consent dialog can appear and nothing waits for one.
* macOS specifics are behind ``sys.platform`` checks. Linux and Windows get a
  minimal fallback; anything not understood is reported as unknown (``None``).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno as errno_module
import getpass
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stiracore.identity_paths import path_key
from stiracore.observability.masking import mask_secrets

DARWIN = "darwin"

# macOS privacy (TCC) "Files and Folders" / "Full Disk Access" locations under the
# home directory. Any access below them may raise a consent dialog or be denied.
TCC_HOME_ZONES: tuple[tuple[str, ...], ...] = (
    ("Desktop",),
    ("Documents",),
    ("Downloads",),
    ("Library", "Mobile Documents"),  # iCloud Drive
    ("Library", "CloudStorage"),  # File Provider: iCloud, OneDrive, Dropbox ...
    ("Library", "Containers"),
    ("Library", "Group Containers"),
    ("Library", "Mail"),
    ("Library", "Messages"),
    ("Library", "Safari"),
    ("Library", "Application Support", "com.apple.TCC"),
)
TCC_SYSTEM_ZONES: tuple[str, ...] = (
    "/Library/Application Support/com.apple.TCC",
    "/private/var/db/TCC",
)
# The data volume's firmlinked spelling of the same locations.
_DATA_VOLUME = "/System/Volumes/Data"
# Locations whose full path is not personal and helps the person (system, apps,
# interpreters); any other absolute path outside home/workspace is shortened.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/Applications",
    "/opt/homebrew",
    "/private/tmp",
    "/tmp",
)
_ABSOLUTE_IN_TEXT = re.compile(r"(?<![\w~@:/.…])/[^\s'\"(),]+")
# System Integrity Protection: sealed system volume and Apple-owned locations.
SIP_PREFIXES: tuple[str, ...] = ("/System", "/usr", "/bin", "/sbin", "/Library/Apple")
SIP_EXCEPTIONS: tuple[str, ...] = ("/usr/local",)

# BSD file flags (st_flags). SF_RESTRICTED is not exported by the stat module.
SF_RESTRICTED = 0x00080000
_BSD_FLAGS = (
    (stat.UF_IMMUTABLE, "uchg"),
    (stat.UF_APPEND, "uappnd"),
    (stat.SF_IMMUTABLE, "schg"),
    (stat.SF_APPEND, "sappnd"),
    (SF_RESTRICTED, "restricted"),
)
# Linux inode flags (FS_IOC_GETFLAGS): chattr +i / +a.
_FS_IOC_GETFLAGS = 0x80086601
_LINUX_FLAGS = ((0x00000010, "immutable"), (0x00000020, "append"))
_XATTR_NOFOLLOW = 0x0001
_ACL_TYPE_EXTENDED = 0x00000100

_TERMINALS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm2",
    "vscode": "Visual Studio Code",
    "WezTerm": "WezTerm",
    "ghostty": "Ghostty",
    "WarpTerminal": "Warp",
    "tmux": "tmux (the terminal app that started it)",
}
# Apps that commonly start Stira (``__CFBundleIdentifier``). Anything else, and every
# launchd label, is shown as "unknown": environment values never reach a card (#266).
_KNOWN_BUNDLES = frozenset(
    {
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "com.microsoft.VSCode",
        "com.microsoft.VSCodeInsiders",
        "com.github.wez.wezterm",
        "com.mitchellh.ghostty",
        "dev.warp.Warp-Stable",
        "com.apple.dt.Xcode",
        "dev.zed.Zed",
        "net.kovidgoyal.kitty",
        "io.alacritty",
    }
)


class TCCPathError(PermissionError):
    """A probe was asked to touch a macOS privacy-protected path (refused, nothing read)."""


def _platform(platform: str | None) -> str:
    return sys.platform if platform is None else platform


def _home(home: str | os.PathLike | None) -> str:
    return os.fspath(home) if home is not None else os.path.expanduser("~")


def _within(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return len(child) >= len(parent) and child[: len(parent)] == parent


def lexical_absolute(path: str | os.PathLike, base: str | os.PathLike | None = None) -> str:
    """``~``-expanded absolute path without touching the filesystem."""
    text = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(text):
        text = os.path.join(os.fspath(base) if base is not None else os.getcwd(), text)
    return os.path.normpath(text)


def within(child: str | os.PathLike, parent: str | os.PathLike) -> bool:
    """Lexical containment (case/NFC folded); no filesystem access."""
    return _within(path_key(lexical_absolute(child)), path_key(lexical_absolute(parent)))


def tcc_zone(
    path: str | os.PathLike,
    *,
    home: str | os.PathLike | None = None,
    platform: str | None = None,
    base: str | os.PathLike | None = None,
) -> str | None:
    """The privacy-protected zone ``path`` lies in (e.g. ``~/Documents``), or ``None``.

    Purely lexical (``~`` expansion, ``..`` collapse, case/NFC folding): it never
    reads the filesystem, so it is safe to call *before* deciding whether a path
    may be probed. macOS only; other systems return ``None``.
    """
    if _platform(platform) != DARWIN:
        return None
    key = path_key(lexical_absolute(path, base))
    home_text = _home(home)
    for prefix in ("", _DATA_VOLUME):
        for zone in TCC_HOME_ZONES:
            if _within(key, path_key(prefix + os.path.join(home_text, *zone))):
                return "~/" + "/".join(zone)
        for zone in TCC_SYSTEM_ZONES:
            if _within(key, path_key(prefix + zone)):
                return zone
    # /Volumes/<name>/...: removable and network volumes (not /Volumes itself).
    if len(key) >= 3 and key[:2] == ("/", "volumes"):
        return "/Volumes/*"
    return None


def sip_path(path: str | os.PathLike, *, platform: str | None = None) -> bool:
    """Lexically inside a System Integrity Protection location (macOS)."""
    if _platform(platform) != DARWIN:
        return False
    key = path_key(lexical_absolute(path))
    if any(_within(key, path_key(prefix)) for prefix in SIP_EXCEPTIONS):
        return False
    return any(_within(key, path_key(prefix)) for prefix in SIP_PREFIXES)


def _guard(path: str | os.PathLike, platform: str | None, home: Any = None) -> str:
    """The path to probe: resolved component by component, refusing any TCC zone.

    A lexical check alone is not enough: ``work/link/file`` with ``link ->
    ~/Documents`` would make ``lstat`` follow the link into the zone (#266).
    """
    text = os.fspath(path)
    if tcc_zone(text, platform=platform, home=home):
        raise TCCPathError(f"refusing to probe a privacy-protected path: {display_path(text)}")
    resolved, zone = safe_resolve(text, home=home, platform=platform)
    if zone:
        raise TCCPathError(f"refusing to probe a privacy-protected path: {display_path(text)}")
    return resolved


# --- Display / masking ------------------------------------------------------------


def _user() -> str | None:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is fine
        return None
    return name if name and len(name) >= 2 else None


def mask_text(
    text: str,
    *,
    home: str | os.PathLike | None = None,
    base: str | os.PathLike | None = None,
) -> str:
    """Shown paths shortened (:func:`display_path`), user name → ``<user>``, secrets masked (#185)."""
    text = _ABSOLUTE_IN_TEXT.sub(lambda m: _short(m.group(0), home=home, base=base), text)
    home_text = _home(home).rstrip("/\\")
    if len(home_text) > 1:
        text = text.replace(home_text, "~")
    user = _user()
    if user:
        for sep in ("/", "\\"):
            text = text.replace(f"{sep}{user}{sep}", f"{sep}<user>{sep}")
            if text.endswith(f"{sep}{user}"):
                text = text[: -len(user)] + "<user>"
    return mask_secrets(text)


def _under(text: str, parent: str) -> bool:
    parent = parent.rstrip("/\\") or parent
    return len(parent) > 1 and (text == parent or text.startswith(parent + os.sep))


def _short(
    text: str, *, home: str | os.PathLike | None = None, base: str | os.PathLike | None = None
) -> str:
    """``@workspace/...``, ``~/...``, a public system path, or ``…/<last two parts>``."""
    if base is not None:
        base_text = os.path.normpath(os.fspath(base))
        if _under(text, base_text):
            return "@workspace" + text[len(base_text.rstrip("/\\")) :]
    home_text = _home(home)
    if _under(text, home_text):
        return "~" + text[len(home_text.rstrip("/\\")) :]
    if not os.path.isabs(text) or any(_under(text, prefix) for prefix in PUBLIC_PREFIXES):
        return text
    parts = Path(text).parts[1:]
    return os.path.join("…", *parts[-2:]) if parts else text


def display_path(
    path: str | os.PathLike | None,
    *,
    home: str | os.PathLike | None = None,
    base: str | os.PathLike | None = None,
):
    """A path safe to show on a card / in a status (#266).

    Inside the workspace → ``@workspace/...``; inside home → ``~/...``; system and
    application locations stay as they are; anything else (``/srv/private/...``)
    is shortened to ``…/<last two parts>``. Then user name and secret masking.
    """
    if path is None:
        return None
    return mask_text(_short(os.fspath(path), home=home, base=base), home=home, base=base)


# --- Probes -----------------------------------------------------------------------


def nearest_existing(path: str | os.PathLike, *, platform: str | None = None) -> str | None:
    """``path`` or its nearest existing ancestor (``lstat`` only, never into TCC zones).

    The path is resolved first (:func:`safe_resolve`), so no ``lstat`` follows a
    symlink into a zone; ``None`` when it leads into one.
    """
    try:
        resolved, zone = safe_resolve(path, platform=platform)
    except OSError:
        return None
    if zone:
        return None
    candidate = Path(resolved)
    for item in (candidate, *candidate.parents):
        if tcc_zone(item, platform=platform):
            return None
        try:
            os.lstat(item)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        return str(item)
    return None


def file_flags(path: str | os.PathLike, *, platform: str | None = None) -> frozenset[str] | None:
    """Lock-like flags on the file ``path`` resolves to (via :func:`_guard`), or ``None``.

    macOS/BSD: ``uchg``/``uappnd``/``schg``/``sappnd``/``restricted`` (SIP) from
    ``st_flags``. Linux: ``immutable``/``append`` (``chattr``). Windows:
    ``readonly`` (the read-only attribute).
    """
    text = _guard(path, platform)
    try:
        info = os.lstat(text)
    except OSError:
        return None
    names: set[str] = set()
    flags = getattr(info, "st_flags", None)
    if flags is not None:
        names.update(name for bit, name in _BSD_FLAGS if flags & bit)
    attributes = getattr(info, "st_file_attributes", None)
    if attributes is not None and attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1):
        names.add("readonly")
    if _platform(platform).startswith("linux") and not stat.S_ISLNK(info.st_mode):
        names.update(_linux_flags(text))
    return frozenset(names)


def _linux_flags(path: str) -> set[str]:
    try:
        import array
        import fcntl
    except ImportError:  # pragma: no cover - not Linux
        return set()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return set()
    try:
        buffer = array.array("l", [0])
        fcntl.ioctl(descriptor, _FS_IOC_GETFLAGS, buffer, True)
    except OSError:
        return set()
    finally:
        os.close(descriptor)
    return {name for bit, name in _LINUX_FLAGS if buffer[0] & bit}


def readonly_mount(path: str | os.PathLike, *, platform: str | None = None) -> bool | None:
    """Whether the filesystem holding ``path`` is mounted read-only (``statvfs``)."""
    text = _guard(path, platform)
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        return bool(statvfs(text).f_flag & getattr(os, "ST_RDONLY", 1))
    except OSError:
        return None


def _libc():
    name = ctypes.util.find_library("c")
    return ctypes.CDLL(name, use_errno=True) if name else None


def xattr_names(path: str | os.PathLike, *, platform: str | None = None) -> list[str] | None:
    """Extended-attribute names on ``path`` (no values read, symlinks not followed)."""
    text = _guard(path, platform)
    current = _platform(platform)
    if current.startswith("linux") and hasattr(os, "listxattr"):
        try:
            return sorted(os.listxattr(text, follow_symlinks=False))
        except OSError:
            return None
    if current != DARWIN or sys.platform != DARWIN:
        return None
    libc = _libc()
    if libc is None:
        return None
    listxattr = libc.listxattr
    listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    listxattr.restype = ctypes.c_ssize_t
    encoded = os.fsencode(text)
    size = listxattr(encoded, None, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    if size == 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    size = listxattr(encoded, buffer, size, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    return sorted(os.fsdecode(name) for name in buffer.raw[:size].split(b"\0") if name)


def acl_text(path: str | os.PathLike, *, platform: str | None = None) -> str | None:
    """The extended ACL of ``path`` as text (macOS), ``"posix_acl"`` (Linux), else ``None``."""
    text = _guard(path, platform)
    current = _platform(platform)
    if current.startswith("linux"):
        names = xattr_names(text, platform=platform) or []
        return "posix_acl" if "system.posix_acl_access" in names else None
    if current != DARWIN or sys.platform != DARWIN:
        return None
    libc = _libc()
    if libc is None:
        return None
    get_link = libc.acl_get_link_np
    get_link.argtypes = [ctypes.c_char_p, ctypes.c_int]
    get_link.restype = ctypes.c_void_p
    to_text = libc.acl_to_text
    to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    to_text.restype = ctypes.c_void_p
    acl_free = libc.acl_free
    acl_free.argtypes = [ctypes.c_void_p]
    handle = get_link(os.fsencode(text), _ACL_TYPE_EXTENDED)
    if not handle:
        return None  # no extended ACL (ENOENT) or unreadable
    try:
        raw = to_text(handle, None)
        if not raw:
            return None
        try:
            return ctypes.string_at(raw).decode("utf-8", "replace")
        finally:
            acl_free(raw)
    finally:
        acl_free(handle)


def acl_denies(path: str | os.PathLike, *, platform: str | None = None) -> bool:
    """An ACL with a ``deny`` entry (macOS) — a hint, the entry may not cover writes."""
    text = acl_text(path, platform=platform)
    return bool(
        text and any(":deny:" in line or line.endswith(":deny") for line in text.splitlines())
    )


def writable(path: str | os.PathLike, *, platform: str | None = None) -> bool | None:
    """``os.access(W_OK)`` for the real uid (a hint: flags, SIP and TCC can still deny)."""
    text = _guard(path, platform)
    try:
        return os.access(text, os.W_OK, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        return os.access(text, os.W_OK)
    except OSError:
        return None


def safe_resolve(
    path: str | os.PathLike,
    *,
    home: str | os.PathLike | None = None,
    platform: str | None = None,
    max_links: int = 40,
) -> tuple[str, str | None]:
    """``realpath`` that stops *before* entering a privacy-protected zone.

    Returns ``(resolved_path, zone)``: ``zone`` is set (and the rest of the path
    left unresolved) when a component — directly or through a symlink — lies in a
    TCC zone. Each component is checked lexically before it is ``lstat``-ed.
    """
    pending = list(Path(lexical_absolute(path)).parts)
    current = Path(pending.pop(0))
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        zone = tcc_zone(candidate, home=home, platform=platform)
        if zone:
            return str(candidate.joinpath(*pending)), zone
        try:
            info = os.lstat(candidate)
        except OSError:
            return str(candidate.joinpath(*pending)), None
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > max_links:
                raise OSError(errno_module.ELOOP, "Too many levels of symbolic links", str(path))
            target = Path(os.readlink(candidate))
            if not target.is_absolute():
                target = current / target
            parts = list(target.parts)
            current = Path(parts.pop(0))
            pending = parts + pending
            continue
        current = candidate
    return str(current), None


# --- Responsible process ----------------------------------------------------------


def _known_name(value: Any, known: Mapping[str, str] | frozenset[str]) -> str:
    """Only allow-listed values reach a card; anything else is the fixed ``unknown``."""
    if isinstance(known, Mapping):
        return known.get(value, "unknown") if isinstance(value, str) else "unknown"
    return value if isinstance(value, str) and value in known else "unknown"


def responsible_process(
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    *,
    platform: str | None = None,
) -> dict[str, Any]:
    """Estimate whose permission macOS checks for this process (the "responsible process").

    Started from a terminal, macOS attributes file access to the terminal app;
    from launchd (a LaunchAgent) to the executable itself; from an app, to that
    app. An estimate from the environment, never a guarantee.
    """
    env = os.environ if env is None else env
    executable = sys.executable if executable is None else executable
    shown = display_path(os.path.realpath(executable)) if executable else None
    current = _platform(platform)
    if current != DARWIN:
        return {"kind": "user", "name": None, "executable": shown, "note": "os_user"}
    terminal = env.get("TERM_PROGRAM")
    bundle = env.get("__CFBundleIdentifier")
    service = env.get("XPC_SERVICE_NAME", "")
    python_app = bool(executable and "Python.framework" in os.path.realpath(executable))
    if terminal:
        info = {"kind": "terminal", "name": _known_name(terminal, _TERMINALS), "note": "terminal"}
    elif bundle:
        info = {"kind": "app", "name": _known_name(bundle, _KNOWN_BUNDLES), "note": "app"}
    elif service and service != "0":
        # launchd labels are arbitrary (and can be personal): never shown.
        info = {"kind": "launchd", "name": "unknown", "note": "launchd"}
    else:
        info = {"kind": "process", "name": None, "note": "process"}
    info["executable"] = shown
    info["python_app"] = python_app
    return info


def principal_text(info: Mapping[str, Any]) -> str:
    """One English sentence for the CLI/doctor text output."""
    kind, name, exe = info.get("kind"), info.get("name"), info.get("executable")
    if kind == "terminal":
        return f"the terminal app that started Stira ({name})"
    if kind == "app":
        return f"the app that started Stira ({name})"
    if kind == "launchd":
        extra = " (Homebrew binaries are ad-hoc signed; an update may reset it)"
        return f"the executable itself, started by launchd ({exe}){extra}"
    if kind == "user":
        return "the operating-system user running Stira"
    return f"this process ({exe})"
