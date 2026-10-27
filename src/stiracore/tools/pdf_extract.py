"""Text from a PDF for ``web_fetch`` (#585), in a separate, bounded process.

The parser (``pypdf``, the optional extra ``stiracore[pdf]``) never runs in the server
process: a child ``python -I`` reads the bytes on stdin, with an empty environment (no keys,
no proxies), a wall-clock timeout, a CPU-time limit and a memory limit (``RLIMIT_AS`` on
Linux; a parent watchdog on the child's RSS wherever ``ps`` exists; neither = not parsed,
``pdf_limits_unavailable``), and answers one JSON line: ``{"code": ..., "text": ...,
"pages": ..., "chars": ...}``. Resource bounding, not an OS sandbox. Every failure is a code
of the web_fetch table (``too_large``, ``pdf_unavailable``, ``pdf_broken``,
``pdf_encrypted``, ``pdf_no_text``, ``pdf_timeout``); the child's own messages are dropped.
Scanned (image-only) PDFs give ``pdf_no_text``; there is no OCR.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 100
PDF_TIMEOUT_SECONDS = 20.0
# Characters the child collects at most (the tool cuts its output at MAX_TEXT_CHARS).
MAX_PDF_CHARS = 400_000
_CPU_SECONDS = 30
_MEMORY_BYTES = 1024 * 1024 * 1024

PDF_UNAVAILABLE = "pdf_unavailable"
PDF_BROKEN = "pdf_broken"
PDF_ENCRYPTED = "pdf_encrypted"
PDF_NO_TEXT = "pdf_no_text"
PDF_TIMEOUT = "pdf_timeout"
# No memory limit can be guaranteed here (no enforced RLIMIT_AS and no watchdog): not parsed.
PDF_LIMITS_UNAVAILABLE = "pdf_limits_unavailable"
TOO_LARGE = "too_large"
PDF_CODES = (
    PDF_UNAVAILABLE,
    PDF_BROKEN,
    PDF_ENCRYPTED,
    PDF_NO_TEXT,
    PDF_TIMEOUT,
    PDF_LIMITS_UNAVAILABLE,
)
_POLL_SECONDS = 0.05

# Runs in the child. Only pypdf and the standard library; the limits come as argv:
# max_pages max_chars cpu_seconds memory_bytes watched(0/1). The memory limit is set before
# anything is parsed; when it cannot be set and no parent watchdog bounds the child, the
# child stops with ``pdf_limits_unavailable`` (review r592 #1: never parse unbounded).
_CHILD = r"""
import json, sys, time
def out(**data):
    sys.stdout.write(json.dumps(data)); sys.stdout.flush(); raise SystemExit(0)
max_pages, max_chars = int(sys.argv[1]), int(sys.argv[2])
cpu, mem, watched = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
limited = False
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    # macOS accepts RLIMIT_AS but does not enforce it. A limit <= 0 is no limit (as in the
    # parent): Linux takes a negative value as a huge unsigned one, i.e. unlimited (#662).
    if sys.platform != "darwin" and mem > 0:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        limited = True
except (ImportError, ValueError, OSError):
    limited = False
if not (limited or watched):
    out(code="pdf_limits_unavailable")
def main():
    # The input first: the parent sends it only once its watchdog has read this process's
    # memory (review r592b B1); until then nothing is parsed.
    data = sys.stdin.buffer.read()
    # PARSE
    try:
        import io, logging
        logging.disable(logging.CRITICAL)
        from pypdf import PdfReader
    except MemoryError:
        raise
    except Exception:
        out(code="pdf_unavailable")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                out(code="pdf_encrypted")
        pages = len(reader.pages)
    except MemoryError:
        raise
    except Exception:
        out(code="pdf_broken")
    if pages > max_pages:
        out(code="too_large", limit="pages", pages=pages)
    # Every page is read (bounded by the page, time, CPU and memory limits) so that the
    # full length is known; only the first max_chars characters are kept.
    kept, size, total = [], 0, 0
    try:
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            total += len(text) + (2 if total else 0)
            if size < max_chars:
                kept.append(text)
                size += len(text) + 2
    except MemoryError:
        raise
    except Exception:
        out(code="pdf_broken")
    text = "\n\n".join(kept)
    if not text.strip():
        out(code="pdf_no_text", pages=pages)
    out(code="ok", text=text[:max_chars], pages=pages, chars=total)
try:
    main()
except MemoryError:
    out(code="too_large", limit="memory")
"""


_PS_DIRS = "/bin:/usr/bin"
# Readings in a row that may fail while the child runs before it is stopped (fail closed).
_MAX_MISSED_READINGS = 3


def _ps_path() -> str | None:
    """The one ``ps`` the watchdog runs (an absolute path in /bin or /usr/bin)."""
    if sys.platform == "win32":
        return None
    path = shutil.which("ps", path=_PS_DIRS)
    return path if path and path.startswith("/") else None


def _watchdog_available() -> bool:
    """A ``ps`` to read the child's resident size exists (confirmed per child, see below)."""
    return _ps_path() is not None


def _rss_bytes(pid: int) -> int | None:
    path = _ps_path()
    if path is None:
        return None
    try:
        done = subprocess.run(
            [path, "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            timeout=2,
            check=False,
            env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = done.stdout.decode("ascii", errors="replace").strip()
    return int(text) * 1024 if text.isdigit() else None


def _first_reading(child: subprocess.Popen) -> bool:
    """The watchdog reads the child's memory before the PDF is handed over."""
    for _ in range(_MAX_MISSED_READINGS):
        if _rss_bytes(child.pid) is not None:
            return True
        if child.poll() is not None:
            return False
        time.sleep(_POLL_SECONDS)
    return False


def _stop(child: subprocess.Popen) -> None:
    try:
        child.kill()
        child.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


@dataclass(frozen=True)
class PdfResult:
    code: str  # "ok" or a failure code
    text: str = ""
    pages: int | None = None
    limit: str | None = None
    # Characters of the whole extracted text (``text`` keeps at most MAX_PDF_CHARS of them).
    chars: int | None = None


def looks_like_pdf(data: bytes) -> bool:
    """The PDF header in the first KiB (the specification allows leading bytes)."""
    return PDF_MAGIC in data[:1024]


def extract_text(
    data: bytes,
    *,
    max_pages: int | None = None,
    timeout: float = PDF_TIMEOUT_SECONDS,
    max_chars: int | None = None,
) -> PdfResult:
    """The PDF's text, or a failure code. Never raises for a bad PDF.

    The child is bounded by a wall clock, a CPU-time limit and a memory limit: its own
    ``RLIMIT_AS`` on Linux, and the parent's watchdog (``ps`` RSS polled every 50 ms, the
    child killed above ``_MEMORY_BYTES``) wherever ``ps`` exists — the only memory bound on
    macOS. With neither, nothing is parsed (``pdf_limits_unavailable``). This is resource
    bounding, not an OS sandbox: the child can still read files the server user can read.
    """
    max_pages = MAX_PDF_PAGES if max_pages is None else max_pages
    max_chars = MAX_PDF_CHARS if max_chars is None else max_chars
    if len(data) > MAX_PDF_BYTES:
        return PdfResult(TOO_LARGE, limit="bytes")
    if not looks_like_pdf(data):
        return PdfResult(PDF_BROKEN)
    watched = _watchdog_available()
    if not watched and (sys.platform in ("darwin", "win32") or _MEMORY_BYTES <= 0):
        return PdfResult(PDF_LIMITS_UNAVAILABLE)  # no enforced limit either: never started
    command = [
        sys.executable,
        "-I",
        "-c",
        _CHILD,
        str(int(max_pages)),
        str(int(max_chars)),
        str(_CPU_SECONDS),
        str(_MEMORY_BYTES),
        "1" if watched else "0",
    ]
    env = {}
    if sys.platform == "win32":  # Python needs these to start on Windows
        import os

        env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH")}
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return PdfResult(PDF_BROKEN)
    if watched and not _first_reading(child):
        # The watchdog cannot read this child: it is stopped before it gets the input.
        _stop(child)
        return PdfResult(PDF_LIMITS_UNAVAILABLE)
    output: dict[str, bytes] = {}

    def talk() -> None:
        try:
            output["stdout"] = child.communicate(input=data)[0]
        except (OSError, ValueError):
            output["stdout"] = b""

    talker = threading.Thread(target=talk, daemon=True)
    talker.start()
    deadline = time.monotonic() + timeout
    stopped = None
    missed = 0
    while talker.is_alive():
        if time.monotonic() >= deadline:
            stopped = PdfResult(PDF_TIMEOUT)
        elif watched:
            rss = _rss_bytes(child.pid)
            if rss is None and child.poll() is None:
                # A reading that fails while the child runs: the limit cannot be kept.
                missed += 1
                if missed >= _MAX_MISSED_READINGS:
                    stopped = PdfResult(PDF_LIMITS_UNAVAILABLE)
            elif rss is not None:
                missed = 0
                if rss > _MEMORY_BYTES:
                    stopped = PdfResult(TOO_LARGE, limit="memory")
        if stopped is not None:
            _stop(child)
            talker.join(5)
            return stopped
        talker.join(_POLL_SECONDS)
    try:
        answer = json.loads(output.get("stdout", b"").decode("utf-8", errors="strict") or "null")
    except (UnicodeError, ValueError):
        answer = None
    if not isinstance(answer, dict):
        # Killed by a limit (CPU, memory) or crashed: its stderr is never read back.
        return PdfResult(PDF_TIMEOUT if (child.returncode or 0) < 0 else PDF_BROKEN)
    code = answer.get("code")
    pages = answer.get("pages") if type(answer.get("pages")) is int else None
    if code == "ok" and isinstance(answer.get("text"), str):
        chars = answer.get("chars") if type(answer.get("chars")) is int else None
        return PdfResult("ok", answer["text"][:max_chars], pages, chars=chars)
    if code == TOO_LARGE:
        limit = answer.get("limit") if answer.get("limit") in ("pages", "memory") else None
        return PdfResult(TOO_LARGE, pages=pages, limit=limit)
    if code in PDF_CODES:
        return PdfResult(code, pages=pages)
    return PdfResult(PDF_BROKEN)


__all__ = [
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "PDF_CODES",
    "PDF_LIMITS_UNAVAILABLE",
    "PdfResult",
    "extract_text",
    "looks_like_pdf",
]
