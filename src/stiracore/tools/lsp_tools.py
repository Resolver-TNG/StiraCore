"""Optional Python analysis without starting a persistent language server.

Diagnostics prefer Pyright (types), falling back to Ruff (lint). Hover uses
Jedi when installed, then a deliberately limited, local AST-only fallback.
Neither operation executes the inspected Python source.

The analyzers get the bytes Stira read through the read floor, not the file name to
open again (#315): Ruff reads them on stdin (``--stdin-filename``), Pyright receives
them as an open document of a one-shot ``pyright-langserver --stdio`` session (the
Pyright CLI has no way to read a file's text from stdin) and Jedi as ``code=``. The
name is still given for positions, configuration and import resolution; the
analyzers read *other* files (imports, stubs, their configuration) by name.
"""

from __future__ import annotations

import ast
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tokenize
from pathlib import Path
from typing import Any

from strands import tool

from .file_tools import MAX_TEXT_BYTES, active_read_floor, open_for_read

_CHANGED = "The file changed during analysis; the analyzer output was discarded."


def _checked(file_path: str) -> tuple[Path, bytes, tuple[int, int]]:
    """``(path, bytes, (st_dev, st_ino))`` read through the read floor (#312).

    ``path`` is the path the checked open resolved and opened (#315), not an earlier
    resolution of the name given: the analyzers get exactly the checked path.
    """
    path = Path(file_path).expanduser().resolve()
    if path.suffix not in {".py", ".pyi"}:
        raise ValueError("Only Python .py and .pyi files are supported")
    resolved, descriptor = open_for_read(str(path))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"File exceeds the {MAX_TEXT_BYTES}-byte limit")
    checked = Path(resolved)
    if checked.suffix not in {".py", ".pyi"}:
        raise ValueError("Only Python .py and .pyi files are supported")
    return checked, data, (info.st_dev, info.st_ino)


def _source(file_path: str) -> tuple[Path, str]:
    path, data, _identity = _checked(file_path)
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return path, data.decode(encoding)


def _unchanged(path: Path, identity: tuple[int, int]) -> bool:
    """Is ``path`` still the file that was checked, after the analysis?

    The analyzers analyse the checked bytes (#315); this check stays as defence in
    depth (they read their configuration by name next to the file) and so that a
    result about a file that changed meanwhile is not shown. Under a read floor the
    path must also still pass it and not be a protected file (#316).
    """
    try:
        now = os.stat(path)
    except OSError:
        return False
    if (now.st_dev, now.st_ino) != identity:
        return False
    floor = active_read_floor()
    if floor is None:
        return True
    try:
        return floor.check_read(str(path)).allowed and identity not in floor.protected_files()
    except OSError:
        return False


def _executable(name: str) -> str | None:
    # Prefer tools belonging to this interpreter over unrelated global installs.
    local = Path(sys.executable).parent / (name + (".exe" if sys.platform == "win32" else ""))
    return str(local) if local.is_file() else shutil.which(name)


_LSP_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "information"}
ANALYZER_TIMEOUT_SEC = 30.0
# Bounds on what an analyzer may send back in one call (#315 review). The text sent is at
# most MAX_TEXT_BYTES; real diagnostics are far below these.
MAX_HEADER_BYTES = 8 * 1024  # the headers of one LSP frame
MAX_FRAME_BYTES = 8 * 1024 * 1024  # one LSP message body
MAX_OUTPUT_BYTES = 16 * 1024 * 1024  # everything read from one analyzer process
MAX_MESSAGES = 10_000  # LSP frames per call (a notification flood)
MAX_QUEUED = 1_000  # frames read but not yet handled
# Only these variables reach an analyzer (no API keys or tokens of the server process).
_ENV_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        # Locale: listed one by one (an ``LC_`` prefix would also pass ``LC_API_TOKEN``).
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_MONETARY",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "VIRTUAL_ENV",
        # Windows (unverified): needed to start a process and find node/python.
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)


class AnalyzerOutputLimit(ValueError):
    """The analyzer sent more than the bounds allow; it was ended, nothing is shown."""


def _analyzer_env() -> dict[str, str]:
    """The allow-listed part of the environment for an analyzer."""
    return {name: value for name, value in os.environ.items() if name.upper() in _ENV_ALLOWED}


def _lsp_frame(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


_JOIN_SEC = 2.0  # how long ending waits for the reader/writer threads
_SENTINEL = "import time\nwhile True:\n    time.sleep(3600)\n"


class _Analyzer:
    """One analyzer process under one deadline, with bounded output (#315 review).

    Writes go through a writer thread (a server that stops reading cannot block the
    caller past the deadline); reads go through a reader thread that stops at the
    bounds above and flags it. On POSIX the analyzer runs in a process group whose
    leader is a sleeping *sentinel* started first: the sentinel is only reaped after the
    group is killed, so the group id stays Stira's for the whole call and :meth:`end`
    can always ``killpg`` it — also after the analyzer itself exited normally and was
    reaped — ending every descendant that stayed in the group. :meth:`end` kills, then
    joins the threads (bounded), then closes the pipes (never a pipe a thread still uses).

    Limits (docs): a descendant that leaves the group (``setsid``/``setpgid``) is not
    ended; on Windows only the analyzer process is killed (``CREATE_NEW_PROCESS_GROUP`` +
    ``kill()``; ending its descendants is unverified).
    """

    def __init__(self, args: list[str], *, timeout: float, frames: bool) -> None:
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.name = os.path.basename(args[0])
        env = _analyzer_env()
        self.sentinel: subprocess.Popen | None = None
        if os.name == "nt":  # pragma: no cover - Windows only (unverified)
            session: dict[str, Any] = {
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            }
        else:
            # The group's leader, in this session (setpgid needs the same session).
            self.sentinel = subprocess.Popen(
                [sys.executable, "-I", "-c", _SENTINEL],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                process_group=0,
            )
            session = {"process_group": self.sentinel.pid}
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                **session,
            )
        except BaseException:
            self._end_group()
            raise
        self.inbox: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self.output = bytearray()
        self.overflow = threading.Event()
        self.ended = threading.Event()
        self._outbox: queue.Queue = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_frames if frames else self._read_raw,
            name="stira-analyzer-out",
            daemon=True,
        )
        self._writer = threading.Thread(target=self._write, name="stira-analyzer-in", daemon=True)
        self._writer.start()
        self._reader.start()

    # -- threads ------------------------------------------------------------------------

    def _write(self) -> None:
        stdin = self.process.stdin
        try:
            while True:
                data = self._outbox.get()
                if data is None:
                    break
                stdin.write(data)
                stdin.flush()
        except (OSError, ValueError):
            pass  # the process ended or was ended
        finally:
            try:
                stdin.close()  # only this thread uses stdin
            except (OSError, ValueError):
                pass

    def _read_raw(self) -> None:
        fd = self.process.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                if len(self.output) + len(chunk) > MAX_OUTPUT_BYTES:
                    self.overflow.set()
                    return
                self.output += chunk
        except OSError:
            return
        finally:
            self.ended.set()

    def _read_frames(self) -> None:
        stream = self.process.stdout
        total = messages = 0
        try:
            while True:
                header = 0
                length = None
                while True:
                    line = stream.readline(MAX_HEADER_BYTES + 1)
                    if not line:
                        return
                    header += len(line)
                    total += len(line)
                    if header > MAX_HEADER_BYTES or total > MAX_OUTPUT_BYTES:
                        self.overflow.set()
                        return
                    line = line.strip()
                    if not line:
                        break
                    key, _, value = line.partition(b":")
                    if key.strip().lower() == b"content-length":
                        length = int(value.strip())
                if length is None or length < 0:
                    return
                if length > MAX_FRAME_BYTES or total + length > MAX_OUTPUT_BYTES:
                    self.overflow.set()
                    return
                body = stream.read(length)
                total += len(body)
                messages += 1
                if len(body) < length:
                    return
                if messages > MAX_MESSAGES:
                    self.overflow.set()
                    return
                try:
                    self.inbox.put_nowait(json.loads(body))
                except queue.Full:
                    self.overflow.set()
                    return
        except (OSError, ValueError):
            return
        finally:
            self.ended.set()

    # -- caller side ----------------------------------------------------------------------

    def _check(self) -> float:
        if self.overflow.is_set():
            raise AnalyzerOutputLimit(f"{self.name}: output exceeds the limit; not shown")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.name, self.timeout)
        return remaining

    def send(self, data: bytes | None) -> None:
        """Queue ``data`` for stdin (never blocks); ``None`` closes stdin."""
        self._outbox.put(data)

    def message(self) -> Any:
        """The next LSP message, within the deadline and the bounds."""
        while True:
            remaining = self._check()
            try:
                return self.inbox.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                if self.ended.is_set() and self.inbox.empty():
                    self._check()
                    raise ValueError(f"{self.name} ended before answering") from None

    def wait_exit(self, grace: float | None = None) -> int | None:
        """Wait for the analyzer itself to exit (it is then reaped; the group is not).

        Within the deadline (``TimeoutExpired``) and the bounds; with ``grace``, at most
        that long and ``None`` if it is still running (the caller ends it).
        """
        until = None if grace is None else time.monotonic() + grace
        while True:
            remaining = self._check()
            if until is not None:
                remaining = min(remaining, until - time.monotonic())
                if remaining <= 0:
                    return None
            try:
                return self.process.wait(timeout=min(remaining, 0.05))
            except subprocess.TimeoutExpired:
                continue

    def _end_group(self) -> None:
        if self.sentinel is not None:
            import signal

            try:
                # The sentinel is not reaped yet: the group id is still this call's.
                os.killpg(self.sentinel.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self.sentinel.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck kernel
                pass

    def end(self) -> None:
        """Kill the group, join the threads (bounded), reap, then close the pipes."""
        self.send(None)
        process = self.process
        if os.name == "nt" and process.poll() is None:  # pragma: no cover - Windows only
            process.kill()  # its descendants: unverified (docs)
        self._end_group()
        until = time.monotonic() + _JOIN_SEC
        for thread in (self._writer, self._reader):
            thread.join(timeout=max(0.0, until - time.monotonic()))
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - a stuck kernel
            pass
        # A pipe still in use by a thread (a descendant outside the group keeps it open)
        # is left to that thread: closing it would block on the buffer's lock.
        if not self._reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

    def verify(self) -> None:
        """After :meth:`end`: refuse a result whose output was not complete and bounded."""
        if self.overflow.is_set():
            raise AnalyzerOutputLimit(f"{self.name}: output exceeds the limit; not shown")
        if self._reader.is_alive():
            # Its output cannot be bounded or known complete (a descendant that left the
            # group keeps the pipe open): not shown.
            raise AnalyzerOutputLimit(f"{self.name}: output did not end; not shown")


def _run_bounded(args: list[str], data: bytes, *, timeout: float) -> tuple[int, bytes]:
    """``(exit code, stdout)`` of ``args`` fed ``data`` on stdin, bounded (Ruff).

    Done when the analyzer exits; its group is then killed, so a descendant holding the
    pipe cannot make the call wait for the deadline.
    """
    analyzer = _Analyzer(args, timeout=timeout, frames=False)
    try:
        analyzer.send(data)
        analyzer.send(None)
        code = analyzer.wait_exit()
    finally:
        analyzer.end()
    analyzer.verify()
    return code, bytes(analyzer.output)


def _pyright_rows(executable: str, path: Path, text: str, timeout: float) -> list[dict]:
    """Pyright's diagnostics of ``text`` (the checked bytes) as an open document.

    One short ``pyright-langserver --stdio`` session: ``initialize``, ``didOpen`` with
    the text, a pull ``textDocument/diagnostic`` request, ``shutdown``. The server never
    needs to open ``path`` itself: an open document wins over the disk. Rows are shaped
    like the CLI's ``--outputjson`` ``generalDiagnostics``. One deadline for everything
    (``subprocess.TimeoutExpired``); output beyond the bounds is
    :class:`AnalyzerOutputLimit`; the process group is always ended.
    """
    analyzer = _Analyzer([executable, "--stdio"], timeout=timeout, frames=True)
    uri = path.as_uri()

    def send(message: dict) -> None:
        analyzer.send(_lsp_frame({"jsonrpc": "2.0", **message}))

    def answer(request_id: int) -> Any:
        while True:
            message = analyzer.message()
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # A request from the server (configuration, capability registration…):
                # answer with defaults so it never waits on us.
                items = (message.get("params") or {}).get("items")
                result = [None] * len(items) if isinstance(items, list) else None
                send({"id": message["id"], "result": result})
                continue
            if message.get("id") != request_id or "method" in message:
                continue  # notifications (logs, progress, pushed diagnostics)
            if "error" in message:
                raise ValueError("pyright-langserver refused the request")
            return message.get("result")

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": os.getpid(),
                    "rootUri": None,
                    "workspaceFolders": None,
                    "capabilities": {
                        "textDocument": {"diagnostic": {"dynamicRegistration": False}},
                        "workspace": {"configuration": True},
                    },
                },
            }
        )
        answer(1)
        send({"method": "initialized", "params": {}})
        send(
            {
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}
                },
            }
        )
        send(
            {"id": 2, "method": "textDocument/diagnostic", "params": {"textDocument": {"uri": uri}}}
        )
        report = answer(2)
        send({"id": 3, "method": "shutdown"})
        send({"method": "exit"})
        analyzer.send(None)
        analyzer.wait_exit(grace=1.0)
    finally:
        analyzer.end()
    # Everything it sent, also after the answer, was within the bounds (else discarded).
    analyzer.verify()
    items = report.get("items") if isinstance(report, dict) else None
    if not isinstance(items, list):
        raise TypeError("Invalid analyzer diagnostic list")
    rows = []
    for item in items:
        rows.append(
            {
                "file": str(path),
                "severity": _LSP_SEVERITY.get(item.get("severity"), "warning"),
                "message": str(item["message"]),  # as the CLI's (no-break spaces kept)
                "range": item["range"],
                "rule": item.get("code"),
            }
        )
    return rows


def _position(row: dict, *, zero_based: bool) -> dict[str, int]:
    return {
        "line": int(row.get("line" if zero_based else "row", 0 if zero_based else 1))
        + int(zero_based),
        "column": int(row.get("character" if zero_based else "column", 0 if zero_based else 1))
        + int(zero_based),
    }


@tool
def lsp_diagnostics(file_path: str) -> dict[str, Any]:
    """Return structured Python type/lint diagnostics with 1-based positions.

    Pyright is preferred; Ruff is the optional fallback. Missing analyzers
    return status=skipped, not a false clean bill of health. No fixes are run.

    Args:
        file_path: Path to a local Python source file (maximum 1 MiB).
    """
    try:
        path, data, identity = _checked(file_path)
    except (OSError, ValueError, SyntaxError) as exc:
        return {"status": "error", "diagnostics": [], "message": str(exc)}
    failures = []
    for backend in ("pyright", "ruff"):
        # Pyright: its language server (an open document carries the checked text).
        executable = _executable("pyright-langserver" if backend == "pyright" else backend)
        if executable is None:
            if backend == "pyright" and _executable("pyright") is not None:
                failures.append("pyright: pyright-langserver not found (needed to pass the text)")
            continue
        try:
            if backend == "pyright":
                encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
                rows = _pyright_rows(
                    executable, path, data.decode(encoding), timeout=ANALYZER_TIMEOUT_SEC
                )
            else:
                # The checked bytes on stdin; the name only for messages and config.
                args = [
                    executable,
                    "check",
                    "--no-fix",
                    "--no-cache",
                    "--output-format",
                    "json",
                    "--stdin-filename",
                    str(path),
                    "-",
                ]
                returncode, stdout = _run_bounded(args, data, timeout=ANALYZER_TIMEOUT_SEC)
            if not _unchanged(path, identity):
                return {
                    "status": "error",
                    "file": str(path),
                    "diagnostics": [],
                    "message": _CHANGED,
                }
            if backend == "ruff":
                if returncode not in (0, 1):
                    raise ValueError(f"Analyzer exited with code {returncode}")
                rows = json.loads(stdout.decode("utf-8", errors="replace"))
            if not isinstance(rows, list):
                raise TypeError("Invalid analyzer diagnostic list")
            diagnostics = []
            for row in rows:
                start = row["range"]["start"] if backend == "pyright" else row["location"]
                end = row["range"]["end"] if backend == "pyright" else row["end_location"]
                diagnostics.append(
                    {
                        "file": row.get("file", row.get("filename", str(path))),
                        "source": backend,
                        "code": row.get("rule", row.get("code")),
                        "severity": row.get("severity", "warning"),
                        "message": row["message"],
                        "start": _position(start, zero_based=backend == "pyright"),
                        "end": _position(end, zero_based=backend == "pyright"),
                    }
                )
            return {
                "status": "success",
                "backend": backend,
                "file": str(path),
                "diagnostics": diagnostics,
                "warnings": failures,
                "capability": "types" if backend == "pyright" else "lint",
            }
        except (
            OSError,
            subprocess.TimeoutExpired,
            ValueError,
            KeyError,
            TypeError,
            SyntaxError,
        ) as exc:
            failures.append(f"{backend}: {exc}")
    return {
        "status": "error" if failures else "skipped",
        "file": str(path),
        "diagnostics": [],
        "warnings": failures,
        "message": "No usable analyzer; install pyright or ruff to enable diagnostics.",
    }


@tool
def lsp_hover(file_path: str, line: int, col: int) -> dict[str, Any]:
    """Describe a Python symbol at a 1-based line and Unicode character column.

    Jedi supplies static inference if installed. Otherwise only local AST
    definitions are reported; this fallback does not infer imported types.

    Args:
        file_path: Path to a local Python source file (maximum 1 MiB).
        line: 1-based line number.
        col: 1-based Unicode character column.
    """
    try:
        path, source = _source(file_path)
        lines = source.splitlines()
        if (
            type(line) is not int
            or type(col) is not int
            or not 1 <= line <= len(lines)
            or not 1 <= col <= len(lines[line - 1]) + 1
        ):
            raise ValueError("line and col must be valid 1-based source coordinates")
        try:
            import jedi
        except ImportError:
            jedi = None
        if jedi is not None:
            symbols = jedi.Script(code=source, path=str(path)).infer(line, col - 1)
            return {
                "status": "success",
                "backend": "jedi",
                "file": str(path),
                "symbols": [
                    {
                        "name": s.name,
                        "kind": s.type,
                        "description": s.description,
                        "documentation": s.docstring(),
                        "line": s.line,
                    }
                    for s in symbols
                ],
            }
        token = next(
            (
                t.string
                for t in tokenize.generate_tokens(io.StringIO(source).readline)
                if t.type == tokenize.NAME and t.start <= (line, col - 1) < t.end
            ),
            None,
        )
        symbols = []
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == token
            ):
                symbols.append(
                    {
                        "name": node.name,
                        "kind": type(node).__name__,
                        "description": lines[node.lineno - 1].strip(),
                        "documentation": ast.get_docstring(node) or "",
                        "line": node.lineno,
                    }
                )
        return {
            "status": "success",
            "backend": "ast",
            "file": str(path),
            "symbols": symbols,
            "limited": True,
            "message": "Local definitions only; install jedi for type inference.",
        }
    except (OSError, ValueError, SyntaxError, tokenize.TokenError) as exc:
        return {"status": "error", "symbols": [], "message": str(exc)}
