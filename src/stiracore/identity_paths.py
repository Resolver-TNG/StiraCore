"""Shared host-owned identity path policy (not an OS execution sandbox)."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import stat as stat_module
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

READ_ONLY_ERROR = "SOUL/identity files are read-only and cannot be modified by the agent"
# The read floor (#312): the same data-directory tree as the write floor (#271).
# Says nothing about whether the path exists.
READ_FLOOR_ERROR = (
    "Path is on the read floor [E_READ_FLOOR]: inside Stira's data directory file tools may read only "
    "workspace/, the episodic memory layer and context-stash/ (no approval lifts this)"
)
DEFAULT_READ_ONLY_PATHS = frozenset({"SOUL.md", "IPD.md"})
_IDENTITY_NAMES = frozenset({"soul.md", "ipd.md"})

REFUSAL_DETAIL_PREFIX = " [refused by: "
_REFUSAL_LAYERS = {"outside_write_roots": "write_roots", "invalid": "input"}
_DETAIL_TOKEN_CHARS = 200


MASKED = "[masked]"

# The protected-identity walk (#315): bounded, so one read cannot take unbounded time; a
# walk past a bound refuses (fail closed). Measured (M1 Max, APFS, by descriptor): 0.006 s
# per 1 000 entries, 0.034 s per 10 000, 0.34 s per 100 000 — on every checked open.
FLOOR_WALK_MAX_ENTRIES = 200_000
FLOOR_WALK_DEADLINE_SEC = 10.0
# The secret-path floor's hard-link walk (#457): only for a file with a second name.
SECRET_WALK_MAX_ENTRIES = 50_000
SECRET_WALK_DEADLINE_SEC = 3.0
# By directory descriptor where the platform allows it (POSIX): every directory is opened
# relative to its parent's descriptor without following a symlink, and must be the one
# that was listed. Elsewhere (Windows) the walk goes by path and compares ``lstat``s.
_FD_WALK = (
    os.scandir in os.supports_fd
    and os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class FloorWalkError(OSError):
    """The protected-identity walk could not finish reliably (#315): the read refuses.

    The message is a fixed text (never a path).
    """


def _same_entry(info: os.stat_result, listed: os.stat_result) -> bool:
    return stat_module.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
        listed.st_dev,
        listed.st_ino,
    )


def _open_listed_dir(name: str, parent: int | None, listed: os.stat_result) -> int:
    """A descriptor of the directory ``name`` (relative to ``parent``) as it was listed.

    Never follows a symlink (``O_NOFOLLOW``: a directory swapped for one fails); the
    opened directory must be the listed one (``fstat`` = the listing's ``lstat``).
    """
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        raise FloorWalkError("a directory changed during the floor walk") from error
    try:
        if not _same_entry(os.fstat(descriptor), listed):
            raise FloorWalkError("a directory changed during the floor walk")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _WalkBudget:
    def __init__(self) -> None:
        self.entries = 0
        self.deadline = time.monotonic() + FLOOR_WALK_DEADLINE_SEC

    def spend(self) -> None:
        """One more entry: past either bound raises."""
        self.entries += 1
        if self.entries > FLOOR_WALK_MAX_ENTRIES:
            raise FloorWalkError("the floor walk exceeded its entry bound")
        self.check()

    def check(self) -> None:
        """The deadline alone (monotonic clock): also around every directory's scan and
        before the walk returns, where no entry is counted (empty directories, the last
        entry's I/O)."""
        if time.monotonic() > self.deadline:
            raise FloorWalkError("the floor walk exceeded its deadline")


def shown_text(value: Any, limit: int | None = None) -> str | None:
    """A model-supplied path/name as Stira may show it again (#307).

    The one function for every exit of the file tools' diagnoses, observations and
    refusal details: a value ``mask_secrets`` would change is shown as ``[masked]``
    as a whole (never partially), the home directory as ``~`` (the tools expand it
    again), and the text is cut to ``limit`` characters. ``None`` for a non-string.
    """
    from stiracore.observability.masking import mask_secrets

    if not isinstance(value, str):
        return None
    if mask_secrets(value) != value:
        return MASKED
    home = os.path.expanduser("~").rstrip("/\\")
    if len(home) > 1 and (value == home or value.startswith((home + "/", home + os.sep))):
        value = "~" + value[len(home) :]
    if limit is not None and len(value) > limit:
        value = value[:limit] + "\u2026"
    return value


def refusal_detail(layer: str, argument: Any, token: Any) -> str:
    """`` [refused by: floor; argument: "path"; matched: "SOUL.md"]`` for a refusal (#307).

    Says which layer refused and which argument/token it reacted to, so a body
    token is not mistaken for the target again (#304). ``layer`` is a fixed word;
    the argument name and token come from the model's call and pass
    :func:`shown_text` (``[masked]``, ``~``, cut to 80/200 characters), JSON-quoted
    so control characters are escaped.
    """

    def safe(value: Any, limit: int) -> str:
        shown = shown_text(value, limit)
        return "null" if shown is None else json.dumps(shown, ensure_ascii=False)

    word = _REFUSAL_LAYERS.get(layer, layer)
    return (
        f"{REFUSAL_DETAIL_PREFIX}{word}; argument: {safe(argument, 80)}; "
        f"matched: {safe(token, _DETAIL_TOKEN_CHARS)}]"
    )


# Deny keys only (#243): code points HFS+ ignores when it compares names (Apple TN1150;
# measured on an HFS+ disk image: ``SO\u200cUL.md`` is ``SOUL.md`` there), and U+0131
# (dotless i), the one code point whose simple uppercase (``I``) casefold() does not
# unify (exhaustive search, tests/test_path_alias_normalization.py). Other volumes keep
# them distinct: dropping/mapping them only over-denies.
_FOLD_EXTRA = {
    **dict.fromkeys((*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070))),
    0xFEFF: None,
    0x0131: "i",
}


def _fold(text: str) -> str:
    # casefold() can denormalize some characters: normalize, fold, normalize again.
    # Dropping an ignorable code point can let a mark compose: normalize once more.
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    return unicodedata.normalize("NFC", folded.translate(_FOLD_EXTRA))


def path_key(path: str | os.PathLike) -> tuple[str, ...]:
    """Comparison key for deny checks: case- and Unicode-normalization-insensitive.

    ``os.path.normcase`` is a no-op on macOS, yet APFS (by default) treats
    ``PROCEDURAL``/``procedural`` and NFC/NFD spellings as the same entry, and
    NTFS is case-insensitive by default (it does not unify NFC/NFD; folding them
    here only over-denies). When unsure whether a volume distinguishes them,
    deny checks assume it does not (the safe side). Never use this key to
    *allow* a path (see ``_parts_within``). Separators are unified, Windows
    ``\\\\?\\`` and ``\\\\?\\UNC\\`` prefixes dropped, ``.``/``..`` collapsed
    lexically, NTFS stream suffixes (``:stream``) and trailing dots/spaces
    dropped from each component. Pure (no filesystem access): callers resolve
    symlinks first. 8.3 short names
    (``PROGRA~1``) cannot be expanded here; ``realpath`` does that on Windows for
    existing paths (unverified on a real Windows machine).
    """
    text = os.fspath(path).replace("\\", "/")
    if text[:8].casefold() == "//?/unc/":
        text = "//" + text[8:]
    elif text[:4] in ("//?/", "//./"):
        text = text[4:]
    text = _fold(text)
    lead = "//" if text.startswith("//") else "/" if text.startswith("/") else ""
    parts: list[str] = []
    for index, part in enumerate(text.split("/")):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        if part != ".." and not (index == 0 and len(part) == 2 and part[1] == ":"):
            # NTFS: ``name:stream`` is a stream of ``name``; Win32 drops trailing
            # dots/spaces. Folding them only over-denies on other systems.
            part = part.split(":", 1)[0].rstrip(". ") or part
        parts.append(part)
    return (lead, *parts)


def _key_within(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return len(child) >= len(parent) and child[: len(parent)] == parent


def _parts_within(child: os.PathLike, parent: os.PathLike) -> bool:
    """Exact, component-wise containment for *allow* decisions.

    Compares ``parts`` as plain strings: unlike ``WindowsPath.__eq__``/``parents``
    (case-insensitive), ``Allowed`` and ``allowed`` stay different, so a
    case-sensitive NTFS directory is never over-allowed (#243). Equivalent
    spellings must be proven by file identity instead.
    """
    child_parts, parent_parts = tuple(child.parts), tuple(parent.parts)
    return child_parts[: len(parent_parts)] == parent_parts


_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{base}{n}" for base in ("com", "lpt") for n in (*"123456789", "¹", "²", "³")}
)


def windows_path_problem(path: str) -> str | None:
    """Why a Windows write path is refused, or ``None`` (pure, testable anywhere).

    Fail closed on syntax whose target the floor cannot see: alternate data
    streams (``SOUL.md:stream`` — any ``:`` except the drive separator),
    trailing dots/spaces (Win32 strips them: ``SOUL.md.`` is ``SOUL.md``), the
    device/long-path namespaces (``\\\\?\\``, ``\\\\.\\``) and reserved device
    names (``NUL``, ``COM1`` …). Unverified on a real Windows machine.
    """
    text = path.replace("/", "\\")
    if text.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        return "Windows device or long-path namespaces are not allowed."
    rest = text[2:] if len(text) >= 2 and text[1] == ":" and text[0].isalpha() else text
    if ":" in rest:
        return "Alternate data streams (':' in a Windows path) are not allowed."
    for part in rest.split("\\"):
        if part in ("", ".", ".."):
            continue
        if part[-1] in ". ":
            return "Windows path components may not end with a dot or a space."
        if part.split(".", 1)[0].rstrip(" ").casefold() in _WINDOWS_RESERVED:
            return "Windows reserved device names are not allowed."
    return None


def windows_short_name(part: str) -> bool:
    """Whether ``part`` has the shape of a Windows 8.3 short name (``PROCED~1``,
    ``SOUL~1.MD``): at most 8 characters before an optional extension of at most 3,
    ending in ``~`` and digits, with something before the ``~``. Pure."""
    base, dot, extension = part.rpartition(".") if "." in part else (part, "", "")
    if dot and (not base or len(extension) > 3 or "." in base):
        return False
    stem, tilde, number = base.rpartition("~")
    return bool(tilde and stem and number.isdigit() and len(base) <= 8 and "~" not in stem)


def _entry_names(directory: Path) -> frozenset[str]:
    """The names ``directory`` lists (Windows lists long names, never 8.3 aliases)."""
    return frozenset(os.listdir(directory))


def unexpanded_short_name(resolved: Path) -> bool:
    """A resolved path with a short-shaped component that is not a listed name (#243).

    ``realpath`` turns an existing 8.3 short name into the long one on Windows. A
    short-shaped component (``NOTES~1``) that exists after resolving is accepted only
    when its directory lists exactly that name, i.e. it is the entry's real (long)
    name; an entry that opens but is not listed is an alias that was not expanded, so
    the floor's key cannot be trusted with it: fail closed. A short-shaped name that
    does not exist is a new literal name. Any other error (``PermissionError`` while
    listing …) propagates; ``check``/``check_read`` turn it into ``invalid``.
    Unverified on a real Windows machine.
    """
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        parent, current = current, current / part
        if not windows_short_name(part):
            continue
        try:
            os.lstat(current)
        except FileNotFoundError:
            return False  # nothing below a missing component exists either
        if part not in _entry_names(parent):
            return True
    return False


SHORT_NAME_ERROR = "A Windows 8.3 short name (NAME~1) is not allowed; use the long name."


def broad_root_problem(path: os.PathLike) -> str | None:
    """Why ``path`` (resolved) is too broad for a write root, or ``None``.

    Refuses a filesystem or drive root, the home directory and any ancestor of
    it: "workspace only" must never mean "everything" (#243). This is a deny
    check, so it is conservative: the casefold/NFC ``path_key`` (``/Users/ALICE``
    or an NFD spelling of home on APFS) and file identity with home or any of
    its existing ancestors both count.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved.parent == resolved:
        return f"{resolved} is a filesystem/drive root."
    home = Path.home().resolve()
    broad = f"{resolved} is the home directory or one of its ancestors."
    if _key_within(path_key(home), path_key(resolved)):
        return broad
    if resolved.exists() and any(_same(resolved, a) for a in _existing_ancestors(home)):
        return broad
    return None


def _existing_ancestors(path: Path) -> list[Path]:
    """``path`` (if it exists) and its existing ancestors, nearest first."""
    found = []
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                found.append(candidate)
        except OSError:
            continue
    return found


def _os_protected(key: tuple[str, ...]) -> bool:
    """macOS privacy (TCC) database and its directory: never a tool's write target (#244)."""
    return bool(key) and (key[-1].startswith("tcc.db") or "com.apple.tcc" in key)


def _identity_name(name: str) -> bool:
    # ``SOUL.md:stream`` (an NTFS alternate data stream) and ``SOUL.md.`` /
    # ``SOUL.md `` (Win32 strips trailing dots/spaces) name the same file there.
    folded = _fold(name.split(":", 1)[0]).rstrip(". ")
    return folded in _IDENTITY_NAMES


def _same(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


@functools.lru_cache(maxsize=32)
def _secret_table(
    home: str, extra: tuple[str, ...], base_dir: str
) -> tuple[tuple[Path, ...], tuple[tuple, ...], tuple[str, ...]]:
    """The secret-path floor for one home, additions and workspace (``on_secret_floor``)."""
    homes = {Path(home)}
    with contextlib.suppress(ImportError, KeyError, OSError, AttributeError):
        import pwd  # POSIX: the account's home, also when HOME was changed

        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
    for found in tuple(homes):
        with contextlib.suppress(OSError, RuntimeError):
            homes.add(found.resolve())
    paths = [found / relative for found in sorted(homes) for relative in SECRET_HOME_PATHS]
    paths.extend(Path(p) for p in SECRET_ABSOLUTE_PATHS)
    names = []
    for entry in extra:
        text = entry.replace("\\", "/")
        if "/" in text or text.startswith("~"):
            candidate = Path(text).expanduser()
            paths.append(candidate if candidate.is_absolute() else Path(base_dir) / candidate)
        else:
            names.append(_fold(text))
    return tuple(paths), tuple(path_key(p) for p in paths), tuple(names)


class IdentityPathPolicy:
    """Protect identity basenames everywhere plus exact configured paths.

    Relative additions are anchored to base_dir at construction. Default names
    are case-insensitive on every OS. Resolve aliases (new paths are judged
    after resolving their deepest existing ancestor) and check existing hard
    links. Configured paths and subtrees are compared case- and
    NFC/NFD-insensitively and by file identity (#243). This does not defend
    against concurrent filesystem replacement (TOCTOU).
    Configuration is host-owned, never supplied in tool arguments.

    ``floor_tree`` (#271): a whole directory (the server's data directory) on the
    floor, except strictly below the relative subdirectories in
    ``floor_tree_writable``. An exception counts only when it is an existing,
    real (not symlinked) directory inside the tree and the path's resolved
    components start with its components (exact strings: a case or Unicode
    variant stays on the floor); a file there with another hard link is on the
    floor too. Read-only paths, identity names and ``protected_dirs`` still apply
    inside an exception.
    """

    def __init__(
        self,
        paths: Iterable[str] = (),
        *,
        base_dir: Path | None = None,
        protected_dirs: Iterable[str | Path] = (),
        floor_tree: str | Path | None = None,
        floor_tree_writable: Iterable[str] = (),
    ) -> None:
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self.paths = frozenset(DEFAULT_READ_ONLY_PATHS | set(paths))
        self._targets = tuple(self._absolute(path) for path in self.paths)
        # Whole subtrees: any path resolving at/under these is read-only.
        self._protected_dirs = tuple(self._absolute(d) for d in protected_dirs)
        self._floor_tree = None if floor_tree is None else self._absolute(floor_tree)
        writable = []
        for raw in floor_tree_writable:
            relative = Path(str(raw).replace("\\", "/"))
            if (
                not str(raw).strip()
                or relative.is_absolute()
                # "." has no parts at all: it would make the whole tree an exception.
                or not relative.parts
                or any(part in ("", ".", "..") for part in relative.parts)
            ):
                raise ValueError("floor_tree_writable entries must be plain relative paths")
            writable.append(relative)
        self._floor_tree_writable = tuple(writable)

    def _in_protected_dir(self, resolved: Path) -> bool:
        if not self._protected_dirs:
            return False
        resolved_key = path_key(resolved)
        ancestors = _existing_ancestors(resolved)
        for directory in self._protected_dirs:
            canonical = directory.resolve()
            canonical_key = path_key(canonical)
            if _key_within(resolved_key, canonical_key):
                return True
            # Replacing/deleting an ancestor would also replace the subtree.
            if _key_within(canonical_key, resolved_key):
                return True
            # The same directory under a spelling the key cannot see.
            if canonical.exists() and any(_same(a, canonical) for a in ancestors):
                return True
            if resolved.is_dir() and any(
                _same(resolved, a) for a in _existing_ancestors(canonical)
            ):
                return True
            if resolved.is_file() and canonical.is_dir():
                for member in canonical.rglob("*"):
                    if member.is_file() and os.path.samefile(resolved, member):
                        return True
        return False

    @property
    def floor_tree(self) -> Path | None:
        """The directory on the floor as a whole (#271), or ``None``."""
        return self._floor_tree

    @property
    def floor_tree_writable(self) -> tuple[str, ...]:
        """Its writable subdirectories, relative POSIX paths (#271)."""
        return tuple(p.as_posix() for p in self._floor_tree_writable)

    def _writable_exception(
        self, resolved: Path, tree: Path, *, inclusive: bool = False
    ) -> Path | None:
        """The exception directory ``resolved`` is strictly below, or ``None``.

        ``inclusive`` (reads, #312): the exception directory itself counts too (listing
        ``workspace/`` is a read; writing over it is not a write inside it).
        """
        for relative in self._floor_tree_writable:
            exception = tree / relative
            try:
                # A symlinked exception (or one that is missing) is ignored: fail closed.
                if exception.resolve() != exception or not exception.is_dir():
                    continue
            except (OSError, RuntimeError):
                continue
            deeper = len(resolved.parts) > len(exception.parts) or (
                inclusive and len(resolved.parts) == len(exception.parts)
            )
            if deeper and _parts_within(resolved, exception):
                return exception
        return None

    def floor_identities(self) -> tuple[frozenset, dict]:
        """``(files, directories)`` on the data-directory floor, by file identity (#312).

        ``files``: ``(st_dev, st_ino)`` of every regular file in the tree outside the
        exception directories (a hard link of one of them, anywhere, has the same
        identity). ``directories``: ``(st_dev, st_ino)`` → the directory's relative
        POSIX path (``""`` for the tree) for every directory there that is not inside an
        exception. A pure ``lstat`` walk that never follows a symlink and never descends
        into a real exception directory. Read tools compare the *opened descriptor*
        against these sets, which no path swap can change. Any error raises (``OSError``);
        callers refuse the read (fail closed).

        On POSIX the walk goes by directory descriptor (#315): each directory is opened
        relative to its parent's descriptor with ``O_DIRECTORY | O_NOFOLLOW`` and must be
        the directory that was listed (``fstat`` = the listing's ``lstat``); a directory
        swapped for a symlink or another directory during the walk raises
        :class:`FloorWalkError` instead of being followed. Bounded by
        ``FLOOR_WALK_MAX_ENTRIES`` entries and ``FLOOR_WALK_DEADLINE_SEC`` seconds (past
        either: :class:`FloorWalkError`). Open descriptors are bounded by the depth.
        """
        if self._floor_tree is None:
            return frozenset(), {}
        tree = self._floor_tree.resolve()
        exceptions = set(self.floor_tree_writable)
        files: set = set()
        directories: dict = {}
        root = os.lstat(tree)
        if not stat_module.S_ISDIR(root.st_mode):
            return frozenset(), {}
        budget = _WalkBudget()

        def scan(where: Any, relative: str, info: os.stat_result) -> list:
            """Record ``where``'s files; its subdirectories to walk, as listed."""
            directories[(info.st_dev, info.st_ino)] = relative
            subdirectories = []
            budget.check()
            with os.scandir(where) as entries:
                for entry in entries:
                    budget.spend()
                    child = f"{relative}/{entry.name}" if relative else entry.name
                    found = entry.stat(follow_symlinks=False)
                    if stat_module.S_ISDIR(found.st_mode):
                        if child not in exceptions:
                            subdirectories.append((entry.name, child, found))
                    elif stat_module.S_ISREG(found.st_mode):
                        files.add((found.st_dev, found.st_ino))
            budget.check()
            return subdirectories

        if not _FD_WALK:
            pending = [(str(tree), "", root)]
            while pending:
                directory, relative, info = pending.pop()
                # By path: at least never a directory that is not the listed one now.
                if not _same_entry(os.lstat(directory), info):
                    raise FloorWalkError("a directory changed during the floor walk")
                for name, child, found in scan(directory, relative, info):
                    pending.append((os.path.join(directory, name), child, found))
            budget.check()
            return frozenset(files), directories

        stack: list = []
        descriptor = _open_listed_dir(str(tree), None, root)
        try:
            stack.append((descriptor, iter(scan(descriptor, "", root))))
        except BaseException:
            os.close(descriptor)
            raise
        try:
            while stack:
                parent, remaining = stack[-1]
                step = next(remaining, None)
                if step is None:
                    stack.pop()
                    os.close(parent)
                    continue
                name, child, found = step
                descriptor = _open_listed_dir(name, parent, found)
                try:
                    subdirectories = scan(descriptor, child, found)
                except BaseException:
                    os.close(descriptor)
                    raise
                stack.append((descriptor, iter(subdirectories)))
        finally:
            for descriptor, _remaining in stack:
                os.close(descriptor)
        budget.check()  # the last I/O may have passed the deadline
        return frozenset(files), directories

    def _symlink_inside_tree(self, lexical: Path) -> bool:
        """A path spelled inside the tree that passes a symlink there (#312, reads).

        Reads never follow a symlink inside the data directory (also not a symlinked
        exception pointing outside); an unreadable component counts as one.
        """
        if self._floor_tree is None:
            return False
        for base in {self._floor_tree, self._floor_tree.resolve()}:
            if not _parts_within(lexical, base):
                continue
            current = base
            for part in lexical.parts[len(base.parts) :]:
                current = current / part
                try:
                    info = os.lstat(current)
                except FileNotFoundError:
                    break
                except OSError:
                    return True
                if stat_module.S_ISLNK(info.st_mode):
                    return True
        return False

    def _in_floor_tree(self, resolved: Path, *, read: bool = False) -> bool:
        """Whether ``resolved`` is on the data-directory floor (#271).

        ``read`` (#312) is the same judgement for the read floor, with two
        differences that only concern writes: an ancestor of the tree is not on it
        (reading a directory above the data directory replaces nothing), and an
        exception directory itself is readable. A hard link of a file inside the
        tree (outside it, or inside an exception) stays on the floor for reads too.
        """
        if self._floor_tree is None:
            return False
        tree = self._floor_tree.resolve()
        tree_key, resolved_key = path_key(tree), path_key(resolved)
        ancestors = _existing_ancestors(resolved)
        inside = _key_within(resolved_key, tree_key) or (
            tree.exists() and any(_same(a, tree) for a in ancestors)
        )
        if not inside:
            if read:
                return self._hard_linked_into(resolved, tree)
            # Replacing/deleting an ancestor would also replace the tree.
            if _key_within(tree_key, resolved_key):
                return True
            if resolved.is_dir() and any(_same(resolved, a) for a in _existing_ancestors(tree)):
                return True
            return self._hard_linked_into(resolved, tree)
        if self._writable_exception(resolved, tree, inclusive=read) is None:
            return True
        # Inside an exception: an existing file with a second name may be a
        # store file under another path (a hard link); fail closed.
        try:
            return resolved.is_file() and os.stat(resolved).st_nlink > 1
        except OSError:
            return True

    def _hard_linked_into(self, resolved: Path, tree: Path) -> bool:
        """An existing file outside the tree that is a hard link of a file inside it."""
        try:
            if not resolved.is_file() or os.stat(resolved).st_nlink < 2 or not tree.is_dir():
                return False
            return any(
                member.is_file() and os.path.samefile(resolved, member)
                for member in tree.rglob("*")
            )
        except OSError:
            return True

    def _absolute(self, path: str | Path) -> Path:
        candidate = Path(str(path).replace("\\", "/")).expanduser()
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate

    def is_read_only(self, path: str | Path) -> bool:
        if not str(path).strip():
            return False
        if self._is_read_only(self._absolute(path)):
            return True
        # A tool may resolve a relative path against the process cwd instead of
        # base_dir: deny if either anchoring hits the floor (#243).
        relative = Path(str(path).replace("\\", "/")).expanduser()
        if not relative.is_absolute():
            cwd = Path.cwd().resolve()
            if cwd != self.base_dir:
                return self._is_read_only(cwd / relative)
        return False

    def _is_read_only(self, lexical: Path) -> bool:
        if _identity_name(lexical.name):
            return True
        resolved = lexical.resolve()
        if any(_identity_name(p.name) for p in (lexical, resolved)):
            return True
        if self._in_protected_dir(resolved):
            return True
        if self._in_floor_tree(resolved):
            return True
        resolved_key = path_key(resolved)
        if _os_protected(resolved_key) or _os_protected(path_key(lexical)):
            return True
        for target in self._targets:
            canonical = target.resolve()
            canonical_key = path_key(canonical)
            # Also reject replacing/deleting a directory containing an anchor,
            # and anything below an anchor (a read-only directory path).
            if _key_within(canonical_key, resolved_key) or _key_within(resolved_key, canonical_key):
                return True
            if resolved.exists() and canonical.exists() and os.path.samefile(resolved, canonical):
                return True
            if resolved.is_dir() and any(
                _same(resolved, a) for a in _existing_ancestors(canonical)
            ):
                return True
        return False

    def require_writable(self, path: str | Path) -> None:
        if self.is_read_only(path):
            raise ValueError(READ_ONLY_ERROR)


# --- Write roots (#243) ---------------------------------------------------------

WORKSPACE_ROOT = "@workspace"
"""``write_roots`` token for the agent's workspace (``WritePathPolicy.workspace``)."""

OUTSIDE_ACTIONS = ("deny",)
"""What happens to a write outside every write root. Only ``deny`` exists: an
``ask`` value (route to the HITL approval card, #146/#189) is reserved."""

OUTSIDE_WRITE_ROOTS_ERROR = "Path is outside the agent's write roots"


# --- Read roots and the secret-path floor (#457) ------------------------------------

READ_OUTSIDE_ROOTS_CODE = "E_READ_OUTSIDE_ROOTS"
READ_SECRET_PATH_CODE = "E_READ_SECRET_PATH"
READ_FLOOR_CODE = "E_READ_FLOOR"
# Never lists the roots or the matched secret entry: the refusal says which rule refused
# and how to change it, never what exists where (the same text for a missing path).
OUTSIDE_READ_ROOTS_ERROR = (
    f"Path is outside the agent's read roots [{READ_OUTSIDE_ROOTS_CODE}]: file tools read only "
    "inside read_roots (default: the workspace only; widened on the settings page)"
)
SECRET_PATH_ERROR = (
    f"Path is on the secret-path floor [{READ_SECRET_PATH_CODE}]: credentials, keys, "
    "keychains and browser profiles are never read by file tools (no setting lifts it)"
)
# The write side of the same floor (#536): ``WritePathPolicy.check`` refuses a write on
# the secret-path floor with this text, whatever ``write_roots`` allow. Like the read text
# it names the rule and never the matched entry or whether the path exists.
WRITE_SECRET_PATH_CODE = "E_WRITE_SECRET_PATH"
WRITE_SECRET_PATH_ERROR = (
    f"Path is on the secret-path floor [{WRITE_SECRET_PATH_CODE}]: credentials, keys, "
    "keychains and browser profiles are never written by file tools (no setting or "
    "approval lifts it)"
)
# The fixed secret-path floor (#457). Home-relative and absolute directories/files (a path
# at or below one is refused), and names refused as the final component, anywhere (ADR 0010:
# a ``.env/`` virtualenv directory stays readable; the file named ``.env`` does not). Settings may
# add entries (``read_secret_paths``), never remove these. Compared case- and
# NFC/NFD-insensitively (``path_key``) and by file identity for existing directories.
SECRET_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    ".config/gcloud",
    ".config/gh",
    ".config/op",
    ".config/hub",
    ".config/git/credentials",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".op",
    ".1password",
    ".mozilla",
    ".local/share/keyrings",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".netrc",
    ".git-credentials",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Safari",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Firefox",
    "Library/Group Containers/2BUA8C4S2C.com.1password",
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Local/BraveSoftware",
    "AppData/Roaming/Mozilla/Firefox",
    "AppData/Roaming/Microsoft/Credentials",
    "AppData/Roaming/Microsoft/Protect",
    "AppData/Local/Microsoft/Credentials",
)
SECRET_ABSOLUTE_PATHS = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/master.passwd",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/etc/ssh",
    "/root",
    "/Library/Keychains",
    "/var/db/dslocal",
    "/private/etc/shadow",
    "/private/etc/master.passwd",
    "/private/etc/sudoers",
    "/private/etc/sudoers.d",
    "/private/etc/ssh",
    "/private/var/db/dslocal",
)
# Glob patterns for one path component (``fnmatch`` on the folded name).
SECRET_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.keychain",
    "*.keychain-db",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".pgpass",
)


def _secret_name(name: str) -> bool:
    """Whether one path component is on the secret-name floor (``.env``, ``id_rsa`` …).

    ``id_*`` is an SSH key name: without a dot (``id_rsa``, ``id_ed25519_sk``) or with
    ``.pub``; ``id_utils.py`` is a source file and stays readable.
    """
    import fnmatch

    folded = _fold(name.split(":", 1)[0]).rstrip(". ")
    if not folded:
        return False
    if folded.startswith("id_") and ("." not in folded or folded.endswith(".pub")):
        return True
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in _SECRET_FOLDED_PATTERNS)


_SECRET_FOLDED_PATTERNS = tuple(_fold(p) for p in SECRET_NAME_PATTERNS)


def secret_name(name: str) -> bool:
    """Public form of the name check, for listings (the editor hides such entries)."""
    return isinstance(name, str) and _secret_name(name)


@dataclass(frozen=True)
class PathDecision:
    """Outcome of ``WritePathPolicy.check``.

    ``code`` is ``ok``, ``floor`` (identity/read-only/protected: no approval can
    lift it), ``outside_write_roots`` or ``invalid``. ``path`` is the path the
    tool should be given: absolute (relative paths anchor to the workspace, not
    the process cwd) and resolved at check time. It is not re-validated at
    write time (TOCTOU is not prevented).
    """

    allowed: bool
    code: str
    reason: str
    path: str | None = None


class WritePathPolicy(IdentityPathPolicy):
    """The floor of ``IdentityPathPolicy`` plus an allowlist of write roots.

    ``check`` order: normalize (``~``; relative paths anchor at the workspace,
    never the process cwd; ``..`` through a missing directory is refused) →
    resolve symlinks of the existing prefix → floor → write roots. The floor
    and the roots apply to the target, to side files (``suffixes``) and to
    every directory ``makedirs`` would create. The tool is then given the
    *resolved* path, so what was checked is what is written and where side files
    land. This is a check at one instant, not an OS sandbox: a concurrent
    replacement (TOCTOU) and shell/MCP code are out of scope (#167).

    ``write_roots=None`` means no allowlist (library default, floor only,
    backward compatible); deny-by-default comes from ``AgentConfig.write_roots``
    (default: workspace only). A path is inside a root when its resolved
    components start with the resolved root's (exact strings), or an existing
    ancestor is the same directory as the root (file identity, so a
    case-insensitive volume's spellings are honoured without guessing). Roots
    that are a filesystem/drive root, the home directory or an ancestor of it
    are refused unless ``allow_broad_roots`` is set explicitly.
    """

    def __init__(
        self,
        paths: Iterable[str] = (),
        *,
        base_dir: Path | None = None,
        protected_dirs: Iterable[str | Path] = (),
        write_roots: Iterable[str | Path] | None = None,
        outside_action: str = "deny",
        allow_broad_roots: bool = False,
        windows: bool | None = None,
        floor_tree: str | Path | None = None,
        floor_tree_writable: Iterable[str] = (),
        read_roots: Iterable[str | Path] | None = None,
        read_secret_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(
            paths,
            base_dir=base_dir,
            protected_dirs=protected_dirs,
            floor_tree=floor_tree,
            floor_tree_writable=floor_tree_writable,
        )
        if outside_action not in OUTSIDE_ACTIONS:
            raise ValueError(f"outside_action must be one of {', '.join(OUTSIDE_ACTIONS)}")
        self.outside_action = outside_action
        self.allow_broad_roots = bool(allow_broad_roots)
        self.windows = os.name == "nt" if windows is None else bool(windows)
        self.workspace = self.base_dir
        self.write_roots: tuple[Path, ...] | None = (
            None if write_roots is None else tuple(self._root(root) for root in write_roots)
        )
        if self.write_roots is not None and not self.allow_broad_roots:
            for root in self.write_roots:
                problem = broad_root_problem(root)
                if problem:
                    raise ValueError(f"Write root too broad: {problem}")
        # Read roots (#457): ``None`` = no allowlist (library default); the server passes
        # ``AgentConfig.read_roots`` (default: the workspace only). A broad read root ("/")
        # is allowed: the secret-path floor below still applies to every read.
        self.read_roots: tuple[Path, ...] | None = (
            None if read_roots is None else tuple(self._root(root) for root in read_roots)
        )
        extra = []
        for entry in read_secret_paths:
            text = str(entry)
            if not text.strip() or "\0" in text:
                raise ValueError("read_secret_paths entries must be nonempty paths or names")
            extra.append(text)
        self.read_secret_paths: tuple[str, ...] = tuple(dict.fromkeys(extra))

    def _root(self, root: str | Path) -> Path:
        text = str(root)
        if not text.strip() or "\0" in text:
            raise ValueError("write roots must be nonempty paths")
        return self.workspace if text == WORKSPACE_ROOT else self._absolute(text)

    def absolute(self, path: str | Path) -> Path:
        """``~``-expanded, workspace-anchored absolute path (symlinks kept)."""
        return self._absolute(path)

    def _with(self, **changes) -> WritePathPolicy:
        values = {
            "paths": self.paths,
            "base_dir": self.base_dir,
            "protected_dirs": self._protected_dirs,
            "write_roots": self.write_roots,
            "outside_action": self.outside_action,
            "allow_broad_roots": self.allow_broad_roots,
            "windows": self.windows,
            "floor_tree": self._floor_tree,
            "floor_tree_writable": tuple(str(p) for p in self._floor_tree_writable),
            "read_roots": self.read_roots,
            "read_secret_paths": self.read_secret_paths,
            **changes,
        }
        return WritePathPolicy(values.pop("paths"), **values)

    def with_floor(
        self, paths: Iterable[str] = (), protected_dirs: Iterable[str | Path] = ()
    ) -> WritePathPolicy:
        """A copy whose floor also covers ``paths``/``protected_dirs`` (never narrower)."""
        return self._with(
            paths=self.paths | set(paths),
            protected_dirs=(*self._protected_dirs, *(self._absolute(d) for d in protected_dirs)),
        )

    def narrow(self, write_roots: Iterable[str | Path] | None) -> WritePathPolicy:
        """A delegate's policy: same floor, roots only at/below this policy's roots.

        ``None`` inherits these roots. Asking for a root outside them raises
        ``ValueError`` — a child can narrow but never widen (#212).
        """
        if write_roots is None:
            return self._with()
        roots = tuple(self._root(root) for root in write_roots)
        if self.write_roots is not None:
            wider = [str(root) for root in roots if not self.within_roots(root)]
            if wider:
                raise ValueError(
                    "A delegate cannot widen its parent's write roots: " + ", ".join(wider)
                )
        return self._with(write_roots=roots)

    @property
    def restricts_reads(self) -> bool:
        """Always: every ``WritePathPolicy`` carries the secret-path floor (#457)."""
        return True

    def check_read(self, path: Any) -> PathDecision:
        """May a file tool read ``path``? The read floor (#312) and read roots (#457).

        Order (the write side's order, ``check``): normalize (``~``; relative paths anchor
        at the workspace) → resolve symlinks of the existing prefix → the floors → the
        roots. Codes:

        * ``floor`` (#312): inside the data directory (``floor_tree``) only an exception
          (``workspace/``, the episodic layer, ``context-stash/``) is readable — also a
          missing path, a case/Unicode variant, a symlink into it, a hard link of a file
          in it and a path spelled inside the tree that passes a symlink there.
        * ``secret_path`` (#457): the secret-path floor (``SECRET_*`` plus
          ``read_secret_paths``), on the lexical *and* the resolved path, case- and
          NFC/NFD-insensitively and by file identity; never lifted by ``read_roots``.
        * ``outside_read_roots`` (#457): with ``read_roots`` set, the resolved path is
          inside none of them (exact components, or an existing ancestor that is the
          same directory as a root).

        Every refusal is decided before anything is opened and does not depend on whether
        the file exists (no ``exists`` in the answer). No approval lifts any of them.
        Write roots, identity names and ``protected_dirs`` do not apply to reads.
        ``path`` in the decision is the resolved path at check time (re-check after the
        open: ``file_tools.read_floor``).
        """
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return PathDecision(False, "invalid", "A nonempty path argument is required.")
        try:
            if self.windows:
                problem = windows_path_problem(path)
                if problem:
                    return PathDecision(False, "invalid", problem)
            absolute = self._absolute(path)
            resolved = absolute.resolve()
            if self.windows and unexpanded_short_name(resolved):
                return PathDecision(False, "invalid", SHORT_NAME_ERROR)
            lexical = Path(os.path.normpath(absolute))
            if self._floor_tree is not None and (
                self._in_floor_tree(resolved, read=True) or self._symlink_inside_tree(lexical)
            ):
                return PathDecision(False, "floor", READ_FLOOR_ERROR)
            if self.on_secret_floor(absolute, resolved):
                return PathDecision(False, "secret_path", SECRET_PATH_ERROR)
            if self.read_roots is not None and not self._within(resolved, self.read_roots):
                return PathDecision(False, "outside_read_roots", OUTSIDE_READ_ROOTS_ERROR)
        except (OSError, RuntimeError, ValueError) as error:
            return PathDecision(
                False, "invalid", f"Path could not be checked ({type(error).__name__})."
            )
        return PathDecision(True, "ok", "Path allowed.", str(resolved))

    def _secret_entries(self) -> tuple[tuple[Path, ...], tuple[tuple, ...], tuple[str, ...]]:
        """``(paths, their path keys, folded name patterns)`` of the secret-path floor.

        Home expanded now (``HOME`` / the account's home, also resolved); cached per home,
        additions and workspace, since computing the keys is most of a check's cost.
        """
        return _secret_table(os.path.expanduser("~"), self.read_secret_paths, str(self.base_dir))

    def on_secret_floor(self, lexical: str | Path, resolved: str | Path | None = None) -> bool:
        """Whether a read of ``lexical`` (absolute) resolving to ``resolved`` is a secret.

        A deny check, so conservative: ``path_key`` (casefold + NFC, ``..`` collapsed) of
        the lexical and the resolved path at or below a secret path, a final component
        whose name is a secret name, an existing ancestor (or the file itself) that is the
        same file as an existing secret path (another spelling, a bind mount, a hard link
        of a listed file), and a file with a second name that is a hard link of a file in
        a secret directory (a bounded walk; past its bound the read is refused).
        """
        import fnmatch

        lexical = Path(lexical)
        resolved = Path(resolved) if resolved is not None else lexical.resolve()
        paths, keys, names = self._secret_entries()
        for candidate in (lexical, resolved):
            key = path_key(candidate)
            if len(key) > 1:
                final = key[-1]
                if _secret_name(final) or any(fnmatch.fnmatchcase(final, n) for n in names):
                    return True
            if any(_key_within(key, secret) for secret in keys):
                return True
        identities = {}
        for secret in paths:
            try:
                info = os.stat(secret)
            except OSError:
                continue
            identities[(info.st_dev, info.st_ino)] = secret
        for ancestor in _existing_ancestors(resolved):
            try:
                info = os.stat(ancestor)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) in identities:
                return True
        try:
            info = os.stat(resolved)
        except OSError:
            return False
        if stat_module.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return self._hard_link_of_secret((info.st_dev, info.st_ino), identities.values())
        return False

    @staticmethod
    def _hard_link_of_secret(identity: tuple, secrets: Iterable[Path]) -> bool:
        """A bounded ``lstat`` walk of the existing secret directories for ``identity``.

        Never follows a symlink. Past ``SECRET_WALK_MAX_ENTRIES`` entries or
        ``SECRET_WALK_DEADLINE_SEC`` the answer is ``True`` (fail closed: a file with a
        second name is rare, a refused read is the safe side).
        """
        entries = 0
        deadline = time.monotonic() + SECRET_WALK_DEADLINE_SEC
        for secret in secrets:
            if not os.path.isdir(secret) or os.path.islink(secret):
                continue
            for directory, subdirectories, files in os.walk(secret, followlinks=False):
                for name in files:
                    entries += 1
                    if entries > SECRET_WALK_MAX_ENTRIES or time.monotonic() > deadline:
                        return True
                    try:
                        found = os.lstat(os.path.join(directory, name))
                    except OSError:
                        continue
                    if (found.st_dev, found.st_ino) == identity:
                        return True
                entries += len(subdirectories)
        return False

    @staticmethod
    def _within(resolved: Path, roots: Iterable[Path]) -> bool:
        """Allow check (#243 rule): exact components, or file identity with a root."""
        ancestors = _existing_ancestors(resolved)
        for root in roots:
            canonical = root.resolve()
            if _parts_within(resolved, canonical):
                return True
            if canonical.exists() and any(_same(a, canonical) for a in ancestors):
                return True
        return False

    def within_read_roots(self, path: str | Path) -> bool:
        """Whether ``path`` is inside the read roots (``True`` without an allowlist)."""
        if self.read_roots is None:
            return True
        return self._within(self._absolute(path).resolve(), self.read_roots)

    def within_roots(self, path: str | Path) -> bool:
        if self.write_roots is None:
            return True
        return self._within(self._absolute(path).resolve(), self.write_roots)

    @staticmethod
    def _dotdot_through_missing(lexical: Path) -> bool:
        """``a/new/../b``: ``makedirs`` would create ``a/new`` that ``resolve`` hides."""
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            if part == ".." and not current.is_dir():
                return True
            current = current / part
        return False

    @staticmethod
    def _created_dirs(path: Path) -> list[Path]:
        """Directories a ``makedirs(dirname(path))`` would create."""
        created = []
        for parent in path.parents:
            try:
                if parent.exists():
                    break
            except OSError:
                break
            created.append(parent)
        return created

    def check(self, path: Any, *, suffixes: Iterable[str] = ()) -> PathDecision:
        """Decide one write target, its side files (``path + suffix``) and new parents."""
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            if self.write_roots is None:
                # No allowlist: leave a missing/odd path to the tool itself.
                return PathDecision(True, "ok", "No path to check.", None)
            return PathDecision(False, "invalid", "A nonempty path argument is required.")
        try:
            if self.windows:
                problem = windows_path_problem(path)
                if problem:
                    return PathDecision(False, "invalid", problem)
            lexical = self._absolute(path)
            if self._dotdot_through_missing(lexical):
                return PathDecision(
                    False, "invalid", "'..' after a directory that does not exist is not allowed."
                )
            effective = lexical.resolve()
            if self.windows and unexpanded_short_name(effective):
                return PathDecision(False, "invalid", SHORT_NAME_ERROR)
            candidates = [
                effective,
                *(Path(f"{effective}{suffix}") for suffix in suffixes),
                *self._created_dirs(effective),
            ]
            if self.is_read_only(path) or any(self.is_read_only(c) for c in candidates):
                return PathDecision(False, "floor", READ_ONLY_ERROR)
            # #536: the secret-path floor of reads (#457), the same table and judgement,
            # on the lexical path and every candidate. Before the roots: no write root
            # (also a broad one, ``allow_broad_roots``) and no approval lifts it.
            # A side file may already exist as a symlink (``report.bak`` -> ``.env``): judge
            # it as named and as resolved, like the target (#601 C1).
            if self.on_secret_floor(lexical, effective) or any(
                self.on_secret_floor(candidate, candidate.resolve()) for candidate in candidates[1:]
            ):
                return PathDecision(False, "secret_path", WRITE_SECRET_PATH_ERROR)
            if self.write_roots is not None and any(
                not self.within_roots(candidate) for candidate in candidates
            ):
                return PathDecision(
                    False,
                    "outside_write_roots",
                    f"{OUTSIDE_WRITE_ROOTS_ERROR} ({', '.join(map(str, self.write_roots))}).",
                )
        except (OSError, RuntimeError, ValueError) as error:
            return PathDecision(False, "invalid", f"Path could not be checked: {error}")
        return PathDecision(True, "ok", "Path allowed.", str(effective))
