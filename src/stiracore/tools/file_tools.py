"""Bounded local reads; native image blocks plus a base64 representation for callers.

The bare tools resolve a relative path against the process cwd; Stira's registry
anchors it at the workspace first (``WorkspaceAnchoredReadTool``, #306).

Observations (#307): a successful read appends a ``[Stira file observation]`` JSON
block (resolved path, bytes, characters, SHA-256, encoding, newlines); a failed one
appends a ``[Stira file diagnosis]`` block (resolved path, exists/is_file/
is_directory, a fixed ``reason``). Logs keep lengths only (:func:`payload_for_log`).
See docs/tool-execution-paths.md "File tool observations (#307)".
"""

import base64
import errno
import hashlib
import io
import json
import logging
import os
import stat
import types
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from PIL import Image, UnidentifiedImageError
from strands import tool

_LOG = logging.getLogger(__name__)
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 3_750_000
MAX_IMAGE_DIMENSION = 8000
_IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpeg", "GIF": "gif", "WEBP": "webp"}

OBSERVATION_MARKER = "[Stira file observation]"
DIAGNOSIS_MARKER = "[Stira file diagnosis]"
# Fixed reason words of a failed read (#307); never model-supplied text.
READ_REASONS = (
    "invalid_path",
    "path_not_found",
    "is_directory",
    "not_regular_file",
    "too_large",
    "binary",
    "permission_denied",
    "unsupported_format",
    "image_too_large",
    "invalid_image",
    "io_error",
    "floor",
    "outside_read_roots",
    "secret_path",
)
# Refusals by policy (#312 / #457): decided before any open, never with a file kind (the
# answer is the same whether the path exists or not). ``reason`` -> its code.
REFUSAL_CODES = {
    "floor": "E_READ_FLOOR",
    "outside_read_roots": "E_READ_OUTSIDE_ROOTS",
    "secret_path": "E_READ_SECRET_PATH",
}


class FileReadError(ValueError):
    """A read refused or failed: a fixed ``reason``, a text and the path it concerns.

    ``path`` is kept for the diagnosis (raw, never shown); the message and every
    shown field use :func:`shown_path`.
    """

    def __init__(self, reason: str, text: str, path: str | None, **details: Any) -> None:
        shown = shown_path(path)
        super().__init__(f"{text}: {shown}" if shown else text)
        self.reason = reason
        self.path = path
        self.details = details


def shown_path(path: Any) -> str | None:
    """How a path appears in a result, a diagnosis or an observation (#307)."""
    from stiracore.identity_paths import shown_text

    return shown_text(path, 1024)


_OPEN_FLAGS = ("O_NOFOLLOW", "O_NONBLOCK")
_MISSING_FLAGS = tuple(name for name in _OPEN_FLAGS if not hasattr(os, name))
if _MISSING_FLAGS:
    # Windows: no O_NOFOLLOW/O_NONBLOCK. Reads keep working (the pre-#307 behaviour);
    # only the regular-file check before and after the open protects them.
    _LOG.warning(
        "file_read/view_image: %s unavailable; a final symlink swapped in after resolving "
        "is followed (docs/tool-execution-paths.md, File tool observations)",
        "/".join(_MISSING_FLAGS),
    )


def _resolve(path: str) -> tuple[str, str]:
    """``(absolute, resolved)``: ``~`` expanded, relative to the cwd, symlinks resolved.

    Strict: a missing component (also ``missing/../existing``, which the OS refuses
    too) is ``path_not_found`` for the path as given, never a lexical shortcut.
    """
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise FileReadError("invalid_path", "A nonempty path without NUL is required", None)
    expanded = os.path.expanduser(path)
    # No lexical normpath: ``link/..`` must mean what the OS opens, as in the
    # write policy's ``Path.resolve`` (symlinks first, then ``..``).
    absolute = expanded if os.path.isabs(expanded) else os.path.join(os.getcwd(), expanded)
    try:
        return absolute, os.path.realpath(absolute, strict=True)
    except FileNotFoundError as error:
        raise FileReadError("path_not_found", "No such file", absolute) from error
    except NotADirectoryError as error:
        raise FileReadError("path_not_found", "No such file", absolute) from error
    except PermissionError as error:
        raise FileReadError("permission_denied", "Permission denied", absolute) from error
    except OSError as error:  # a symlink loop and the like
        raise FileReadError("not_regular_file", "Path cannot be resolved", absolute) from error


def _kind(path: str) -> dict[str, bool]:
    """Existence and kind of ``path`` as the OS sees it now (at diagnosis time)."""
    try:
        info = os.stat(path)
    except OSError:
        return {"exists": os.path.lexists(path), "is_file": False, "is_directory": False}
    return {
        "exists": True,
        "is_file": stat.S_ISREG(info.st_mode),
        "is_directory": stat.S_ISDIR(info.st_mode),
    }


def _not_regular(mode: int, resolved: str) -> FileReadError:
    if stat.S_ISDIR(mode):
        return FileReadError("is_directory", "Path is a directory", resolved)
    return FileReadError("not_regular_file", "Path is not a regular file", resolved)


# --- The read floor (#312) -----------------------------------------------------------
#
# Stira's registry wraps every read tool so that, while one step of the tool runs,
# the server's ``WritePathPolicy`` is the active read floor (``read_floor``). Every
# open of a read tool then asks ``policy.check_read`` before opening (a refusal says
# ``floor`` and never whether the file exists) and again after opening, on the path
# as it resolves *now*, which must still be the opened inode (a parent swapped for a
# symlink, or another file put in its place, is refused). No floor is active for the
# bare tools (library use without a data directory): reads are unchanged there.

_READ_FLOOR: ContextVar[Any] = ContextVar("stira_read_floor", default=None)


class _ReadFloors:
    """Every read floor active now (nested wrappers): a read must pass all of them."""

    def __init__(self, policies: tuple, tool_name: str | None = None) -> None:
        self.policies = policies
        self.tool_name = tool_name  # for the audit row of a refusal (#457)
        # ``(path as given, bytes)`` of the last checked binary read by an upstream read
        # tool in this step (#315): its format detection decodes these bytes instead of
        # opening the name again. One entry; this object lives for one tool step.
        self.last_checked_read: tuple[str, bytes] | None = None

    def check_read(self, path: Any) -> Any:
        decision = None
        for policy in self.policies:
            decision = policy.check_read(path)
            if not decision.allowed:
                return decision
        return decision

    def protected_files(self) -> frozenset:
        """Identities of every file on any active floor (``OSError`` if not walkable)."""
        found: frozenset = frozenset()
        for policy in self.policies:
            found |= policy.floor_identities()[0]
        return found

    def listing(self, identity: tuple) -> tuple[frozenset, frozenset | None]:
        """``(protected files, names)`` for listing the directory ``identity``.

        ``names`` is ``None`` when the directory is not on any floor (list it all),
        else the only entry names it may show: its exception subdirectories.
        """
        files: frozenset = frozenset()
        names: frozenset | None = None
        for policy in self.policies:
            protected, directories = policy.floor_identities()
            files |= protected
            if identity in directories:
                relative = directories[identity]
                prefix = f"{relative}/" if relative else ""
                allowed = frozenset(
                    name[len(prefix) :]
                    for name in policy.floor_tree_writable
                    if name.startswith(prefix) and "/" not in name[len(prefix) :]
                )
                names = allowed if names is None else names & allowed
        return files, names


def _restricts_reads(policy: Any) -> bool:
    """A data-directory tree (#312) or the read roots / secret-path floor (#457)."""
    return getattr(policy, "floor_tree", None) is not None or (
        getattr(policy, "restricts_reads", False) is True
    )


@contextmanager
def read_floor(policy: Any, tool_name: str | None = None) -> Iterator[None]:
    """Add ``policy`` (a ``WritePathPolicy``) to the active read floor.

    Hold it only while one step of the read tool runs; ``asyncio.to_thread`` copies
    it into the SDK worker thread. Nesting never weakens: an inner wrapper whose
    policy restricts nothing keeps the outer floors, and with several each applies.
    ``tool_name`` names the tool in the audit row of a refusal (#457).
    """
    outer = _READ_FLOOR.get()
    policies = outer.policies if outer is not None else ()
    if _restricts_reads(policy) and policy not in policies:
        policies = (*policies, policy)
    if tool_name is None and outer is not None:
        tool_name = outer.tool_name
    token = _READ_FLOOR.set(_ReadFloors(policies, tool_name) if policies else None)
    try:
        yield
    finally:
        _READ_FLOOR.reset(token)


def active_read_floor() -> Any:
    """The active read floors (``check_read``) while a wrapped tool step runs, else ``None``."""
    return _READ_FLOOR.get()


def _cwd_absolute(path: str) -> str:
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.join(os.getcwd(), expanded)


# --- The audit row of a refused read (#457) --------------------------------------------

_READ_AUDIT: dict[str, Any] = {"record": None}
_AUDIT_KEY = os.urandom(32)  # keyed hashes of this process only (as run_checks / YOLO)


def set_read_audit(record: Any) -> None:
    """The server's ``AuditChain.record_read_refusal`` (one row per refused read)."""
    _READ_AUDIT["record"] = record


def path_ref(path: str) -> str:
    """``path:<keyed hash>`` of an absolute path for the audit row (never the path)."""
    import hmac

    digest = hmac.new(_AUDIT_KEY, os.fsencode(os.path.normpath(path)), hashlib.sha256)
    return "path:" + digest.hexdigest()


def _audit_refusal(tool_name: Any, path: str, reason: str) -> None:
    record = _READ_AUDIT["record"]
    if record is None:
        return
    try:
        record(
            tool_name=tool_name if isinstance(tool_name, str) else None,
            target=path_ref(path),
            reason_code=REFUSAL_CODES.get(reason, "unknown"),
        )
    except Exception:  # noqa: BLE001 - the audit must never break (or open) a read
        _LOG.error("read refusal: the audit row could not be written", exc_info=True)


_WRITE_AUDIT: dict[str, Any] = {"record": None}


def set_write_audit(record: Any) -> None:
    """The server's ``AuditChain.record_write_refusal`` (one row per refused write, #536)."""
    _WRITE_AUDIT["record"] = record


def audit_write_refusal(tool_name: Any, path: Any, code: str, base: Any = None) -> None:
    """One ``audit.write.denied`` row: the tool, ``path_ref`` of the path and ``code``.

    ``path`` is the argument as given; a relative one is anchored at ``base`` (the
    workspace, as the write policy does). Never raises.
    """
    record = _WRITE_AUDIT["record"]
    if record is None or not isinstance(path, str) or not path.strip() or "\0" in path:
        return
    try:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(os.fspath(base) if base is not None else os.getcwd(), expanded)
        record(
            tool_name=tool_name if isinstance(tool_name, str) else None,
            target=path_ref(expanded),
            reason_code=code,
        )
    except Exception:  # noqa: BLE001 - the audit must never break the turn
        _LOG.error("write refusal: the audit row could not be written", exc_info=True)


def floor_refusal(policy: Any, path: str, tool_name: str | None = None) -> "FileReadError | None":
    """``None`` if ``policy`` lets file tools read ``path``, else the refusal (#312, #457).

    The refusal carries ``path`` as given (absolute, not resolved): never a symlink
    target, never whether something exists there. A policy refusal (``floor`` /
    ``outside_read_roots`` / ``secret_path``) is also one audit row: the tool, a keyed
    hash of the path and the code, nothing else.
    """
    decision = policy.check_read(path)
    if decision.allowed:
        return None
    if decision.code in REFUSAL_CODES:
        if tool_name is None:
            floors = _READ_FLOOR.get()
            tool_name = getattr(floors, "tool_name", None)
        _audit_refusal(tool_name, path, decision.code)
        return FileReadError(
            decision.code, decision.reason, path, code=REFUSAL_CODES[decision.code]
        )
    return FileReadError("invalid_path", decision.reason.rstrip("."), path)


def _recheck_floor(policy: Any, resolved: str, descriptor: int) -> None:
    """After the open: the descriptor is no protected file, and the path still passes.

    The decisive check is the descriptor's own ``(st_dev, st_ino)`` against every
    protected file's (``floor_identities``); the name re-check and the ``stat``
    comparison come on top.
    """
    from stiracore.identity_paths import READ_FLOOR_ERROR

    refused = FileReadError("floor", READ_FLOOR_ERROR, resolved, code=REFUSAL_CODES["floor"])
    try:
        opened = os.fstat(descriptor)
        # The opened file itself, not a name: no swap of the path between the checks
        # can make a protected file pass (#316). A walk that fails refuses.
        if (opened.st_dev, opened.st_ino) in policy.protected_files():
            raise refused
        decision = policy.check_read(resolved)
        if not decision.allowed and decision.code in REFUSAL_CODES:
            # The path changed into a refused place between the checks (#457).
            refusal = floor_refusal(policy, resolved)
            if refusal is not None:
                raise refusal
        if not decision.allowed or decision.path != resolved:
            raise refused
        now = os.stat(resolved)
    except OSError as error:
        raise refused from error
    if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
        raise refused


def open_for_read(path: str) -> tuple[str, int]:
    """``(resolved path, descriptor)`` of a regular file, under the active read floor.

    The descriptor is the caller's to close. Shared by ``file_read``/``view_image``/
    ``lsp_*`` (via :func:`_read_checked`), the editor's sandbox and the
    ``image_reader`` opener (#312).
    """
    policy = _READ_FLOOR.get()
    if policy is not None and isinstance(path, str) and path.strip() and "\0" not in path:
        refusal = floor_refusal(policy, _cwd_absolute(path))
        if refusal is not None:
            raise refusal
    _absolute, resolved = _resolve(path)
    try:
        before = os.stat(resolved)
    except FileNotFoundError as error:
        raise FileReadError("path_not_found", "No such file", resolved) from error
    except PermissionError as error:
        raise FileReadError("permission_denied", "Permission denied", resolved) from error
    except OSError as error:
        raise FileReadError("io_error", type(error).__name__, resolved) from error
    if not stat.S_ISREG(before.st_mode):
        raise _not_regular(before.st_mode, resolved)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    for name in _OPEN_FLAGS:
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(resolved, flags)
    except FileNotFoundError as error:
        raise FileReadError("path_not_found", "No such file", resolved) from error
    except PermissionError as error:
        raise FileReadError("permission_denied", "Permission denied", resolved) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            # The resolved path is a symlink now: it changed after resolving. Not followed.
            raise FileReadError(
                "not_regular_file", "Path became a symbolic link", resolved
            ) from error
        raise FileReadError("io_error", type(error).__name__, resolved) from error
    try:
        mode = os.fstat(descriptor).st_mode
    except OSError:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(mode):
        os.close(descriptor)
        raise _not_regular(mode, resolved)
    if policy is not None:
        try:
            _recheck_floor(policy, resolved, descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    return resolved, descriptor


def _read_checked(path: str, limit: int) -> tuple[str, bytes]:
    """``(resolved path, bytes)`` of a regular file of at most ``limit`` bytes.

    The path is resolved first (strictly) and only a regular file is opened at all
    (``stat`` before the open, as the old ``is_file()`` check: no device or FIFO is
    ever opened). The *resolved* path is then opened without following a final
    symlink and non-blocking, like ``file_write``'s ``open_checked`` (#271), and
    only a descriptor that ``fstat`` shows as a regular file is read. The active
    read floor (#312) is asked before the open and again after it.
    """
    resolved, descriptor = open_for_read(path)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise FileReadError(
            "too_large", f"File exceeds the {limit}-byte limit", resolved, limit=limit
        )
    return resolved, data


def _read_bounded(path: str, limit: int) -> bytes:
    """Bytes of a regular file of at most ``limit`` bytes (``ValueError`` otherwise)."""
    return _read_checked(path, limit)[1]


def _newlines(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [name for name, count in (("lf", lf), ("crlf", crlf), ("cr", cr)) if count]
    if not kinds:
        return "none"
    return kinds[0] if len(kinds) == 1 else "mixed"


def observe(path: str, data: bytes) -> dict[str, Any]:
    """Measured facts of ``data`` a caller can check against its own (#307).

    ``path`` is shown through :func:`shown_path`. ``chars`` counts the decoded text
    without a UTF-8 BOM (``None`` if not UTF-8).
    """
    encoding: str | None = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text: str | None = data.decode(encoding)
    except UnicodeDecodeError:
        encoding, text = None, None
    return {
        "path": shown_path(path),
        "bytes": len(data),
        "chars": None if text is None else len(text),
        "sha256": hashlib.sha256(data).hexdigest(),
        "encoding": encoding,
        "newline": None if text is None else _newlines(text),
        "trailing_newline": None if text is None else text.endswith(("\n", "\r")),
    }


def _verify_written(path: str, expected: bytes, opened: Any) -> str | None:
    """``None`` if ``path`` is still the inode the write opened and holds ``expected``.

    Otherwise a fixed reason. Nothing read here is ever returned or hashed: the
    observation is computed from ``expected`` (the bytes Stira asked to write).
    """
    if not opened:
        return "not_opened"
    if len(opened) != 1:
        return "opened_more_than_once"
    if not hasattr(os, "O_NOFOLLOW"):
        # Without it a final symlink to the renamed original passes every check below
        # (same inode, one link, same bytes): never claim "verified" then.
        return "unverifiable_no_nofollow"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    for name in _OPEN_FLAGS:
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "target_changed"
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink > 1
            or (info.st_dev, info.st_ino) != tuple(opened[0])
        ):
            return "target_changed"
        data = stream.read(len(expected) + 1)
    return None if data == expected else "content_differs"


def observe_written(path: str, content: Any, opened: Any = None) -> dict[str, Any]:
    """The observation of a ``file_write``: facts of the bytes it asked to write (#307).

    Computed from ``content`` encoded as UTF-8, never by reading the path back (a
    target swapped after the write must not report another file's length or digest).
    ``verified`` says whether the file is still the inode the write opened
    (``open_checked``: same device/inode, regular, one link, no final symlink) and
    holds exactly these bytes; otherwise ``verify_error`` gives a fixed reason
    (``not_opened``, ``target_changed``, ``content_differs``,
    ``unverifiable_no_nofollow`` where the OS has no ``O_NOFOLLOW``, ...).
    """
    if not isinstance(content, str):
        return {"path": shown_path(path), "observation_error": "no_text_content"}
    expected = content.encode("utf-8", "surrogatepass")
    observed = observe(path, expected)
    observed["source"] = "requested_bytes"
    try:
        problem = _verify_written(path, expected, opened)
    except OSError:
        problem = "io_error"
    observed["verified"] = problem is None
    if problem is not None:
        observed["verify_error"] = problem
    return observed


def marked_block(marker: str, data: Mapping[str, Any]) -> dict[str, str]:
    return {"text": f"{marker} {json.dumps(dict(data), ensure_ascii=False)}"}


def diagnosis(error: FileReadError) -> dict[str, Any]:
    """The diagnosis block of a failed read: fixed reason, shown path, file kind.

    A policy refusal (``floor`` #312, ``outside_read_roots`` / ``secret_path`` #457) has
    no file kind: it never tells whether the file exists.
    """
    if error.reason in REFUSAL_CODES:
        return {"reason": error.reason, "resolved_path": shown_path(error.path), **error.details}
    found = _kind(error.path) if error.path else {}
    return {
        "reason": error.reason,
        "resolved_path": shown_path(error.path),
        "exists": found.get("exists", False),
        "is_file": found.get("is_file", False),
        "is_directory": found.get("is_directory", False),
        **error.details,
    }


def _error(message: str, error: FileReadError | None = None) -> dict[str, Any]:
    content = [{"text": message}]
    if error is not None:
        content.append(marked_block(DIAGNOSIS_MARKER, diagnosis(error)))
    return {"status": "error", "content": content}


def _unexpected(prefix: str, error: BaseException) -> dict[str, Any]:
    """An unexpected exception: its class name only (its text may carry a raw path)."""
    return _error(f"{prefix}: {type(error).__name__}")


@tool
def file_read(path: str) -> dict[str, Any]:
    """Read a UTF-8 text file, rejecting binary input or files larger than 1 MiB.

    Args:
        path: Local absolute or relative path; ~ is expanded.
    """
    try:
        resolved, data = _read_checked(path, MAX_TEXT_BYTES)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise FileReadError("binary", "File is not UTF-8 text", resolved) from error
        if "\x00" in text:
            raise FileReadError(
                "binary", "Binary files are not supported; use view_image for images", resolved
            )
        return {
            "status": "success",
            "content": [{"text": text}, marked_block(OBSERVATION_MARKER, observe(resolved, data))],
        }
    except FileReadError as error:
        return _error(f"File read failed: {error} ({error.reason})", error)
    except (OSError, ValueError) as error:
        return _unexpected("File read failed", error)


@tool
def view_image(path: str) -> dict[str, Any]:
    """Read PNG/JPEG/GIF/WebP for multimodal input (3.75 MB, 8000px maximum).

    Returns a native Strands image content block (bytes, not textual base64),
    and a base64 field for direct callers building JSON multimodal requests.

    Args:
        path: Local absolute or relative image path; ~ is expanded.
    """
    resolved = None
    try:
        resolved, data = _read_checked(path, MAX_IMAGE_BYTES)
        with Image.open(io.BytesIO(data)) as image:
            image_format = _IMAGE_FORMATS.get(image.format)
            if image_format is None:
                raise FileReadError(
                    "unsupported_format",
                    "Supported image formats are PNG, JPEG, GIF, and WebP",
                    resolved,
                )
            if max(image.size) > MAX_IMAGE_DIMENSION:
                raise FileReadError(
                    "image_too_large", "Image dimensions exceed 8000 pixels", resolved
                )
            image.verify()
        return {
            "status": "success",
            "content": [{"image": {"format": image_format, "source": {"bytes": data}}}],
            "base64": base64.b64encode(data).decode("ascii"),
            "mime_type": f"image/{image_format}",
        }
    except FileReadError as error:
        return _error(f"Image read failed: {error} ({error.reason})", error)
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        wrapped = FileReadError(
            "invalid_image", f"Invalid image ({type(error).__name__})", resolved
        )
        return _error(f"Image read failed: {wrapped} (invalid_image)", wrapped)
    except (OSError, ValueError) as error:
        return _unexpected("Image read failed", error)


# --- Upstream read tools under the read floor (#312) -----------------------------------

# Upstream modules that read with a plain ``open(path, "rb")``; Stira's opener replaces
# their module-level ``open`` (as the ``file_write`` consent bridge does, #242).
READ_FLOOR_MODULES = ("strands_tools.image_reader",)


def _floor_checked_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
    """The upstream module's ``open``: plain reads through :func:`open_for_read`.

    Without an active read floor it is the builtin ``open`` (the bare upstream tool).
    Under one, only a read of a regular file the floor allows is opened (checked
    before and after the open); any write mode is refused.
    """
    import builtins

    floors = _READ_FLOOR.get()
    if floors is None:
        return builtins.open(file, mode, *args, **kwargs)
    if mode not in ("r", "rb", "rt"):
        raise PermissionError("Stira's read floor: this tool may only read")
    name = os.fspath(file)
    _resolved, descriptor = open_for_read(name)
    if mode != "rb" or args or kwargs or not isinstance(name, str):
        return os.fdopen(descriptor, mode, *args, **kwargs)
    # A binary read: the checked bytes, remembered for this step's format detection
    # (``_FloorCheckedImage.open``), so the tool never opens the name a second time.
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read()
    floors.last_checked_read = (name, data)
    return io.BytesIO(data)


_floor_checked_open.stira_read_floor = True


class _FloorCheckedImage(types.ModuleType):
    """``PIL.Image`` inside an upstream read module: ``open(path)`` decodes checked bytes.

    ``image_reader`` reads the file, then opens the path a second time by name for the
    format. Under a read floor the format is detected from the very bytes the checked
    read returned (#315): no second open, so the format always belongs to the returned
    bytes and a swap after the read changes neither. A path without such a read goes
    through :func:`_floor_checked_open` (checked before and after its open, #316).
    """

    stira_read_floor = True

    def __init__(self) -> None:
        super().__init__("PIL.Image")

    def open(self, fp: Any, *args: Any, **kwargs: Any):
        floors = _READ_FLOOR.get()
        if floors is not None and isinstance(fp, (str, os.PathLike)):
            name = os.fspath(fp)
            remembered, floors.last_checked_read = floors.last_checked_read, None
            if remembered is not None and remembered[0] == name:
                data = remembered[1]
            else:
                with _floor_checked_open(fp, "rb") as handle:
                    data = handle.read()
            return Image.open(io.BytesIO(data), *args, **kwargs)
        return Image.open(fp, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(Image, name)


class _FloorCheckedOsPath(types.ModuleType):
    """``os.path`` inside an upstream read module: ``exists`` asks no name under a floor.

    ``image_reader`` checks ``os.path.exists(path)`` before its (checked) open. Under a
    read floor that name check could follow a parent swapped after the wrapper's check
    and tell whether a protected file exists (#315). There it answers ``True`` and the
    checked open decides (a missing file is then the open's ``No such file``; a floor
    path the same refusal whether it exists or not).
    """

    stira_read_floor = True

    def __init__(self) -> None:
        super().__init__("os.path")

    def exists(self, path: Any) -> bool:
        if _READ_FLOOR.get() is not None:
            return True
        return os.path.exists(path)

    def __getattr__(self, name: str) -> Any:
        return getattr(os.path, name)


class _FloorCheckedOs(types.ModuleType):
    """``os`` inside an upstream read module, with :class:`_FloorCheckedOsPath`."""

    stira_read_floor = True

    def __init__(self) -> None:
        super().__init__("os")
        self.path = _FloorCheckedOsPath()

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def read_floor_opener_live(module: Any) -> bool:
    namespace = getattr(module, "__dict__", {})
    opener = namespace.get("open")
    image = namespace.get("Image")
    os_module = namespace.get("os")
    return (
        getattr(opener, "stira_read_floor", False) is True
        and (image is None or isinstance(image, _FloorCheckedImage))
        and (os_module is None or isinstance(os_module, _FloorCheckedOs))
    )


def install_read_floor_opener(module: Any) -> bool:
    """Route ``module``'s ``open``, ``Image.open`` and ``os.path.exists`` through the floor.

    ``False`` if any was replaced by someone else: it cannot be verified, so the tool
    stays refused.
    """
    current = module.__dict__.get("open")
    if current is not None and getattr(current, "stira_read_floor", False) is not True:
        return False
    image = module.__dict__.get("Image")
    if image is not None and image is not Image and not isinstance(image, _FloorCheckedImage):
        return False
    os_module = module.__dict__.get("os")
    if os_module is not None and os_module is not os and not isinstance(os_module, _FloorCheckedOs):
        return False
    module.open = _floor_checked_open
    if image is not None and not isinstance(image, _FloorCheckedImage):
        module.Image = _FloorCheckedImage()
    if os_module is not None and not isinstance(os_module, _FloorCheckedOs):
        module.os = _FloorCheckedOs()
    return read_floor_opener_live(module)


def _without_digest(text: str) -> str:
    try:
        data = json.loads(text[len(OBSERVATION_MARKER) + 1 :])
    except ValueError:
        return OBSERVATION_MARKER + " {}"
    if not isinstance(data, dict):
        return OBSERVATION_MARKER + " {}"
    data.pop("sha256", None)
    return marked_block(OBSERVATION_MARKER, data)["text"]


def _page_check_for_log(value: Mapping) -> Any:
    """``page_check`` output for a log: counts and codes only, no page content (ADR 0015 §5)."""
    from .page_check import for_log

    kept = []
    for block in value.get("content") or ():
        text = block.get("text") if isinstance(block, Mapping) else None
        try:
            data = json.loads(text) if isinstance(text, str) else None
        except ValueError:
            data = None
        kept.append({"text": json.dumps(for_log(data))})
    return {**value, "content": kept}


def payload_for_log(tool_name: Any, kind: str, value: Any) -> Any:
    """What an observation log may keep of a file tool's input/output (#307).

    The model gets the full result (body, SHA-256). A log (turn events, #188)
    gets lengths only: the ``file_write`` body and the ``file_read`` text are
    replaced by their sizes, and every observation block loses its SHA-256 (a
    digest of a short body can be brute-forced back). ``page_check`` (ADR 0015 §5):
    the input keeps its target as ``@workspace/<relative path>`` or a refusal code, the
    output counts and codes only. Anything else is unchanged.
    """
    if tool_name == "run_checks":
        # ADR 0019 §5: kinds, @workspace/<folder>, counts and codes only.
        from . import run_checks as _rc

        if kind == "input":
            return _rc.input_for_log(value)
        if kind == "output" and isinstance(value, Mapping):
            blocks = value.get("content") or ()
            kept = [
                {"text": json.dumps(_rc.for_log(_rc.parse_content(b.get("text"))))}
                for b in blocks
                if isinstance(b, Mapping)
            ]
            return {**value, "content": kept}
        return value
    if kind == "input" and tool_name == "page_check":
        from .page_check import input_for_log

        return input_for_log(value)  # @workspace/<relative path> or a refusal code
    if kind == "input" and tool_name == "file_write" and isinstance(value, Mapping):
        body = value.get("content")
        if isinstance(body, str):
            size = len(body.encode("utf-8", "surrogatepass"))
            return {**value, "content": f"[file body omitted: {len(body)} chars, {size} bytes]"}
        return value
    if kind != "output" or not isinstance(value, Mapping):
        return value
    if tool_name == "page_check":
        return _page_check_for_log(value)
    blocks = value.get("content")
    if not isinstance(blocks, list):
        return value
    body_of_read = tool_name == "file_read" and value.get("status") == "success"
    kept = []
    for index, block in enumerate(blocks):
        text = block.get("text") if isinstance(block, Mapping) else None
        if body_of_read and index == 0:
            # The text is always the first block, whatever it starts with (a file
            # may begin with a marker of its own).
            size = len(text) if isinstance(text, str) else 0
            block = {"text": f"[file body omitted: {size} chars]"}
        elif isinstance(text, str) and text.startswith(OBSERVATION_MARKER):
            block = {**block, "text": _without_digest(text)}
        elif body_of_read and isinstance(text, str):
            block = {"text": f"[file body omitted: {len(text)} chars]"}
        kept.append(block)
    return {**value, "content": kept}
