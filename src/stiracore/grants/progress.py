"""Progress by outcome, not by activity (ADR 0011 §8 "cross-tick", W6a of #296).

One **keyed fingerprint** per finished goal iteration that observed something says where
the workspace stands:

* the **workspace identity**, a fresh read at the iteration's end of every file the grant
  has written (at most :data:`FILES_KEPT`, the most recently written; older ones leave the
  identity silently): HMAC(relative path) and HMAC(current SHA-256) each, a missing file
  as the explicit :data:`ABSENT` marker (:func:`read_files`; a stat cache only saves
  re-reading an unchanged file, it never answers for a changed one);
* the iteration's check outcomes as **one set of per-check rows** (:func:`normalize_checks`
  per call, the union over every call: kind, status, reason code, exit class, counts —
  durations, pids, temp paths, timestamps, ids, failure texts, output, the call's overall
  status and how the checks were split over calls are dropped).

The agent's ``goal_report`` is not part of it (a label is not an outcome). The fingerprint
is ``HMAC-SHA256(key, canonical encoding)`` with a random per-grant key in this process's
memory only (a grant never survives a restart: it is ``revoked(restart)`` on start). Only
HMAC bytes and fixed codes are kept; the tracked paths and their stat cache are memory
only and never shown or logged.

**Classification** (the executor's, at the commit):

* ``none`` — nothing observed: no file the iteration changed and no check (only reading,
  only reporting, a rewrite that left the same bytes): no progress, one history place;
* ``unknown`` — anything needed could not be read (a digest, a torn read, a FIFO, a
  permission error, over a cap): **no progress (fail closed), not in the history, no
  other streak changes**; the next end reads everything fresh again;
* ``repeat`` — the fingerprint is among the last :data:`RING_SIZE` known outcomes;
* ``new`` — anything else. Only ``new`` is progress.

**Rules** (:meth:`ProgressTracker.record`, deterministic, no model call):

* ``count_iteration(progress=...)`` keeps its W5 meaning: ``no_progress_ticks`` iterations
  in a row that are not ``new`` → ``blocked(no_progress)``;
* a ``repeat`` within the last :data:`LOOP_WINDOW` (A-A, A-B-A-B, A-B-C-A) →
  ``blocked(loop_detected)``;
* the same ``goal_report(blocked, code)`` on :data:`SAME_BLOCKER_LIMIT` counted iterations
  in a row → ``blocked(same_blocker)`` instead of ``blocked(code)``
  (:meth:`ProgressTracker.blocker_code`);
* :data:`NO_ACTION_LIMIT` iterations in a row that ended with no tool call and no report
  (``no_action``) → ``blocked(no_progress)``.

**Interrupted iterations** (:data:`INTERRUPTED_ENDS`, an abandoned call, a person's turn,
the kill, or the grant no longer in the state the iteration left it in — read again at the
commit) change no streak and no history; an iteration that consumed no effect is not
counted at all (the executor).

Memory is bounded: :data:`RING_SIZE` HMACs and :data:`FILES_KEPT` paths per grant, at most
:data:`TRACKERS_KEPT` grants besides the live ones. File reads are I/O and run on the
iteration's threads with no lock held; the tracker's own state is changed under the
caller's leaf lock (memory only).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ABSENT",
    "CACHE_SETTLE_SEC",
    "CHECK_COUNTS",
    "COUNT_CAP",
    "DIGEST_BUDGET",
    "DIGEST_MAX_BYTES",
    "FILES_KEPT",
    "INTERRUPTED_ENDS",
    "LOOP_WINDOW",
    "NORMALIZERS",
    "NO_ACTION_LIMIT",
    "PROGRESS_CODES",
    "RING_SIZE",
    "SAME_BLOCKER_LIMIT",
    "SAMPLE_BYTES",
    "TRACKERS_KEPT",
    "ProgressTracker",
    "ReadBudget",
    "Trackers",
    "claim_checks",
    "combine_checks",
    "file_digest",
    "file_identity",
    "normalize_checks",
    "read_files",
]

# ADR 0011 §8 defaults (operator-approved W6 direction): ring 16, loop window 6, same
# blocker ×3; the no-progress streak is the grant's own ``no_progress_ticks`` (default 3).
RING_SIZE = 16
LOOP_WINDOW = 6
SAME_BLOCKER_LIMIT = 3
NO_ACTION_LIMIT = 2
TRACKERS_KEPT = 16
# The files a grant's tracker keeps reading (the ones it wrote, most recent first): older
# ones leave the identity silently — no counter (a cycle over more files than this is not
# guaranteed to be found; max_iterations and the token caps still bound the grant).
FILES_KEPT = 256
PROGRESS_CODES = ("new", "repeat", "none", "unknown")
# The ends that interrupt an iteration (a person's turn, shutdown, a kill / revoke / any
# narrowing, a permit refused at its consume): never a no-progress tick.
INTERRUPTED_ENDS = frozenset({"yielded_user_turn", "shutdown", "stopped", "permit_stale"})

# A file larger than this is not digested: the iteration counts as no progress (fail
# closed) rather than reading without bound.
DIGEST_MAX_BYTES = 64 * 1024 * 1024
# At most this many bytes are digested per iteration (before-write and end reads together);
# more → the outcome is unknown. O_NONBLOCK does not bound storage latency: this does.
DIGEST_BUDGET = 256 * 1024 * 1024
# A file over DIGEST_MAX_BYTES is identified by its size and a digest of three regions of
# this many bytes (its first, middle and last), never by its mtime or inode (W6-H, #632-1).
SAMPLE_BYTES = 1024 * 1024
# A digest is cached only for a file whose mtime is at least this old (1 s timestamps).
CACHE_SETTLE_SEC = 2.0
_now = time.time
ABSENT = b"absent"  # the "digest" of a file that does not exist
COUNT_CAP = 1_000_000  # check counts are clamped (bounded integers in the encoding)
CHECK_COUNTS = ("passed", "failed", "errors", "skipped", "findings")
_STATUS = frozenset({"passed", "failed", "error", "limited"})
_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_CHECKS_KEPT = 16  # checks per run_checks result that enter the encoding


def file_digest(path: str) -> bytes | None:
    """SHA-256 of the bytes at ``path``: :data:`ABSENT` when there is no file, ``None``
    when it cannot be read consistently — the caller then counts no progress.

    Never waits on a FIFO and never follows a symlink: opened ``O_NONBLOCK | O_NOFOLLOW |
    O_CLOEXEC`` and ``fstat``-ed **before** any read (``O_NONBLOCK`` does not bound storage
    latency: :class:`ReadBudget` bounds the bytes per iteration); only a regular file of at most
    :data:`DIGEST_MAX_BYTES` is read (a FIFO, a device, a directory, a socket → ``None``).
    A torn read is refused: the open file's identity (device, inode, size, mtime, ctime)
    before and after the read, and the path's own ``lstat`` after it, must all agree and
    the bytes read must equal the size. This catches an ordinary concurrent writer or a
    file replaced at the path; it is no defence against a same-user process that
    rewrites a file and restores its times (docs/grants.md, known limits)."""
    return _read_digest(path, sampled=False)


def _sample_digest(path: str) -> bytes | None:
    """The identity of a file over :data:`DIGEST_MAX_BYTES` (W6-H): its size and the
    SHA-256 of its first, middle and last :data:`SAMPLE_BYTES` — marked ``big:``, so it
    never equals a content digest (32 bytes). Opened and checked like :func:`file_digest`
    (no FIFO wait, no symlink, a torn read refused). A ``touch`` or a same-bytes copy does
    not move it; a change of the size or of a sampled region does; a same-size change
    confined to the unsampled regions is **not** seen (docs/grants.md, known limits)."""
    digest = _read_digest(path, sampled=True)
    return b"big:" + digest if digest is not None and digest != ABSENT else digest


def _read_digest(path: str, *, sampled: bool) -> bytes | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        size = before.st_size
        if not _regular(before.st_mode) or (size > DIGEST_MAX_BYTES) != sampled:
            return None
        digest = hashlib.sha256()
        if sampled:
            digest.update(_encode(("sample", size, SAMPLE_BYTES)))
            regions = [(0, SAMPLE_BYTES), ((size - SAMPLE_BYTES) // 2, SAMPLE_BYTES)]
            regions.append((size - SAMPLE_BYTES, SAMPLE_BYTES))
        else:
            regions = [(0, DIGEST_MAX_BYTES + 1)]
        read = 0
        for offset, length in regions:
            os.lseek(fd, offset, os.SEEK_SET)
            left = length
            while left > 0:
                chunk = os.read(fd, min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
                read += len(chunk)
                digest.update(chunk)
            if read > DIGEST_MAX_BYTES:
                return None
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        expected = 3 * SAMPLE_BYTES if sampled else size
        if read != expected or not _same(before, after) or not _same(after, named):
            return None
        return digest.digest()
    except OSError:
        return None
    finally:
        os.close(fd)


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


def _regular(mode: int) -> bool:
    import stat

    return stat.S_ISREG(mode)


def _code(value: Any) -> str:
    return value if type(value) is str and _CODE.fullmatch(value) else "other"


def _count(value: Any) -> int:
    return max(0, min(value, COUNT_CAP)) if type(value) is int else -1


def _exit_class(value: Any) -> str:
    if value is None:
        return "none"
    if type(value) is not int:
        return "invalid"
    return {0: "zero", 1: "one"}.get(value, "other")


def normalize_checks(result: Any) -> tuple | None:
    """The outcome of one ``run_checks`` call as **per-check rows** (fixed codes and
    bounded integers, sorted), or ``None`` when there is no check outcome (a refusal, an
    unreadable result). The caller takes the union of the rows over all calls of an
    iteration, so how the checks were split over calls (or ordered) is not part of it.

    Per check (at most 16): its kind, status class, reason code, exit class (``zero`` /
    ``one`` / ``other`` / ``none``) and the counts :data:`CHECK_COUNTS` (clamped to
    :data:`COUNT_CAP`). A result with no check rows is one row ``("run", status,
    reason)``. The call's overall status (the worst of its group) is not kept.
    Dropped (volatile or free text): ``duration_ms``, failure ids and texts, output,
    tree changes, limits, the directory, containment, pids, temp paths, timestamps."""
    if not isinstance(result, Mapping) or result.get("status") not in _STATUS:
        return None
    rows = []
    checks = result.get("checks")
    for check in (checks if isinstance(checks, list) else [])[:_CHECKS_KEPT]:
        if not isinstance(check, Mapping):
            rows.append(("invalid",))
            continue
        counts = check.get("counts") if isinstance(check.get("counts"), Mapping) else {}
        status = check.get("status")
        rows.append(
            (
                _code(check.get("kind")),
                status if status in _STATUS else "invalid",
                _code(check.get("reason")) if check.get("reason") is not None else "",
                _exit_class(check.get("exit_code")),
                tuple(_count(counts.get(k, 0)) for k in CHECK_COUNTS),
            )
        )
    if not rows:
        reason = result.get("reason")
        rows.append(("run", result["status"], _code(reason) if reason is not None else ""))
    return tuple(sorted(rows))


# The per-tool normalizer table (ADR 0011 §8): what of a tool's effect enters the
# fingerprint. ``editor``: the final bytes of a written file (``file_digest``; mtime,
# inode, mode and the command used are not part of it); ``run_checks``:
# ``normalize_checks``. Every other scope tool only reads and adds nothing.
NORMALIZERS = {"editor": file_digest, "run_checks": normalize_checks}


_SEVERITY = {"passed": 1, "failed": 2, "error": 3, "limited": 4}  # run_checks' order
_BAD_COUNTS = ("failed", "errors", "findings")


def claim_checks(result: Any) -> tuple[str, tuple]:
    """W6c: one ``run_checks`` result as the claim evidence's checks code and per-check
    rows ``(kind, status, passed, failed, errors, skipped, findings)`` — fixed codes and
    clamped counts only (a kind outside ``run_checks``' kinds is ``other``). **Fail
    closed**: ``pass`` only when it is positively established.

    * ``not_run`` — the call was refused (nothing ran);
    * ``pass`` — every check row passed, consistently (at least one row);
    * ``fail`` — every row readable and at least one did not pass (failed, error, limited);
    * ``unknown`` — anything else: an unreadable result, a result without check rows, more
      than :data:`~.schema.CLAIM_ROWS_KEPT` rows, a row with an unknown status, a row the
      runner marks inconsistent (``consistent: false``: its exit code and its report
      disagree), a passed row whose counts are missing, not numbers or report a failure, an
      overall status that is not the worst row's, or a tree that changed during the run
      (``real_tree_changed``)."""
    from .schema import CHECK_KINDS, CHECK_STATUSES, CLAIM_ROWS_KEPT

    if not isinstance(result, Mapping):
        return "unknown", ()
    if result.get("status") == "refused":
        return "not_run", ()
    if result.get("status") not in _STATUS or result.get("real_tree_changed") is True:
        return "unknown", ()
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks or len(checks) > CLAIM_ROWS_KEPT:
        return "unknown", ()
    rows = []
    for check in checks:
        if not isinstance(check, Mapping) or check.get("status") not in CHECK_STATUSES:
            return "unknown", ()
        status = check["status"]
        if check.get("consistent") is False:
            return "unknown", ()
        counts = check.get("counts")
        if counts is None and status != "passed":
            counts = {}  # a limit or a missing report: no counts, still not a pass
        if not isinstance(counts, Mapping):
            return "unknown", ()
        values = []
        for key in CHECK_COUNTS:
            value = counts.get(key, 0)
            if type(value) is not int or value < 0:
                return "unknown", ()
            values.append(min(value, COUNT_CAP))
        named = dict(zip(CHECK_COUNTS, values))
        if status == "passed" and any(named[key] for key in _BAD_COUNTS):
            return "unknown", ()  # a pass that reports a failure contradicts itself
        kind = check.get("kind")
        rows.append((kind if kind in CHECK_KINDS else "other", status, *values))
    worst = max((row[1] for row in rows), key=_SEVERITY.__getitem__)
    if worst != result["status"]:
        return "unknown", ()  # the overall status is not the rows' worst
    rows.sort()
    return ("pass" if worst == "passed" else "fail"), tuple(rows)


def combine_checks(a: tuple[str, tuple] | None, b: tuple[str, tuple]) -> tuple[str, tuple]:
    """W6c: the verdict of several ``run_checks`` calls on the same files (``a`` the ones
    so far, None: none): any ``fail`` → ``fail``; else any ``unknown`` → ``unknown``; else
    ``pass`` when at least one call passed; a refused call (``not_run``) adds nothing. The
    rows are the sorted union (a ``fail`` keeps its failing rows first, at most
    :data:`~.schema.CLAIM_ROWS_KEPT`; a ``pass`` with more rows than that is ``unknown``)."""
    from .schema import CLAIM_ROWS_KEPT

    calls = [c for c in (a, b) if c is not None and c[0] != "not_run"]
    if not calls:
        return "not_run", ()
    codes = {c[0] for c in calls}
    rows = sorted({row for c in calls for row in c[1]})
    if "fail" in codes:
        failing = [r for r in rows if r[1] != "passed"]
        rest = [r for r in rows if r[1] == "passed"]
        return "fail", tuple(sorted((failing + rest)[:CLAIM_ROWS_KEPT]))
    if codes != {"pass"} or len(rows) > CLAIM_ROWS_KEPT:
        return "unknown", ()
    return "pass", tuple(rows)


def _encode(value: Any) -> bytes:
    """A canonical, unambiguous encoding of nested tuples of str / bytes / int."""
    if isinstance(value, bytes):
        return b"b" + str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"s" + str(len(raw)).encode() + b":" + raw
    if type(value) is int:
        return b"i" + str(value).encode() + b";"
    if isinstance(value, tuple):
        return b"(" + b"".join(_encode(v) for v in value) + b")"
    raise TypeError("encode")


def _stat_key(path: str) -> tuple | None | bytes:
    """The stat cache key of ``path`` (no symlink followed): ``(dev, ino, size,
    mtime_ns, ctime_ns)``; :data:`ABSENT` when there is no file; ``None`` when it cannot be
    stat-ed or is not a regular file."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None
    if not _regular(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ReadBudget:
    """The bytes one iteration may digest in total (before-write and end reads together):
    :data:`DIGEST_BUDGET`. Over it, the outcome is unknown (fail closed)."""

    def __init__(self, total: int | None = None) -> None:
        self.left = DIGEST_BUDGET if total is None else int(total)

    def take(self, size: int) -> bool:
        if size > self.left:
            self.left = -1
            return False
        self.left -= size
        return True


def file_identity(path: str, budget: ReadBudget) -> bytes | None:
    """What a file contributes to the identity now: :data:`ABSENT` (no file), a content
    digest (:func:`file_digest`), or — over :data:`DIGEST_MAX_BYTES` — its size and a
    sampled digest (:func:`_sample_digest`, charged ``3 ×`` :data:`SAMPLE_BYTES`; never its
    mtime or inode). ``None``: it cannot be read, is not a regular file, or the
    iteration's read budget is spent."""
    key = _stat_key(path)
    if key is None:
        return None
    if key == ABSENT:
        return file_digest(path)  # ABSENT, unless it appeared just now (then read it)
    if key[2] > DIGEST_MAX_BYTES:
        return _sample_digest(path) if budget.take(3 * SAMPLE_BYTES) else None
    if not budget.take(key[2]):
        return None
    return file_digest(path)


def read_files(
    paths: Iterable[str],
    cache: Mapping[str, tuple],
    budget: ReadBudget | None = None,
    kept: dict[str, tuple] | None = None,
) -> tuple[dict[str, bytes], dict[str, tuple]] | None:
    """A **fresh** read of ``paths``: path → what it contributes now
    (:func:`file_identity`; :data:`ABSENT` for a missing file — an explicit marker, not an
    error), and the stat cache to keep. ``None`` when any of them cannot be read or the
    read budget is spent (the outcome is then unknown).

    ``cache`` (path → (stat key, digest)) is an optimisation only: a digest is reused when
    the file's stat key is unchanged, otherwise the file is read again. A digest is cached
    only when the stat key did not move while it was read **and** its mtime is at least
    :data:`CACHE_SETTLE_SEC` old (a coarse-timestamp file system could otherwise hide a
    second write within the same tick). ``kept``: the caller's dict the cache entries are
    collected in — W6-H: the entries validated before a failure stay there (a cache entry
    is only an optimisation). I/O: call with no lock held."""
    budget = budget if budget is not None else ReadBudget()
    digests: dict[str, bytes] = {}
    kept = {} if kept is None else kept
    for path in paths:
        key = _stat_key(path)
        if key is None:
            return None
        cached = cache.get(path)
        if key != ABSENT and cached is not None and cached[0] == key:
            digests[path], kept[path] = cached[1], cached
            continue
        digest = file_identity(path, budget)
        if digest is None:
            return None
        digests[path] = digest
        settled = key != ABSENT and key[3] <= (_now() - CACHE_SETTLE_SEC) * 1_000_000_000
        if settled and len(digest) == 32 and _stat_key(path) == key:
            kept[path] = (key, digest)
    return digests, kept


class ProgressTracker:
    """One grant's recent outcome history (memory only). The caller serializes access
    (the executor's leaf lock); the methods do no I/O (:func:`read_files` does, outside
    the lock)."""

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        # One entry per counted iteration whose outcome was known: its fingerprint, or None
        # (nothing observed). An unknown outcome never enters.
        self.ring: deque[bytes | None] = deque(maxlen=RING_SIZE)
        # The files the grant has written (absolute paths, memory only), most recently
        # written last, at most FILES_KEPT (older ones leave silently): path → the stat
        # cache entry ((stat key, digest), or None). Never part of a view or a log.
        self.files: OrderedDict[str, tuple | None] = OrderedDict()
        # The workspace identity of the last known outcome (an iteration with no check and
        # this identity observed nothing: ``none``).
        self.last_identity = self.identity([])
        # W6-H: path -> (relative path, digest) behind ``last_identity`` (memory only)
        self.basis: dict[str, tuple] = {}
        self.blocker = ""  # the last counted iteration's blocked code ("" otherwise)
        self.blocker_streak = 0
        self.no_action_streak = 0
        # W6c (memory only): the combined verdict of the run_checks calls since the grant's
        # last write (:func:`combine_checks`; None: none), the workspace identity they ran
        # on (read at the end of their iteration; None: unread), and the workspace identity
        # at the last claim (the issue's at first; None: that claim's could not be read).
        self.check_codes: tuple[str, tuple] | None = None
        self.check_identity: bytes | None = None
        self.claim_identity: bytes | None = self.last_identity

    def __repr__(self) -> str:  # never the key, never a path
        return f"ProgressTracker(ring={len(self.ring)}, files={len(self.files)})"

    def _mac(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, hashlib.sha256).digest()

    def paths(self) -> list[str]:
        return list(self.files)

    def cache(self) -> dict[str, tuple]:
        return {path: entry for path, entry in self.files.items() if entry is not None}

    def retained(self, added: Iterable[str]) -> list[str]:
        """The tracked paths after ``added`` were written (most recent last, at most
        :data:`FILES_KEPT`). Pure."""
        order = OrderedDict((path, None) for path in self.files)
        for path in added:
            order[path] = None
            order.move_to_end(path)
        return list(order)[-FILES_KEPT:]

    def adopt(
        self,
        added: Iterable[str],
        cache: Mapping[str, tuple],
        seeds: Mapping[str, tuple] | None = None,
    ) -> None:
        """Track ``added`` (written now) and keep ``cache`` (memory only; W6-H: with the
        entries already kept for paths it did not re-validate — a cache entry is reused only
        for the same stat key, so an old one is never wrong).

        ``seeds`` (W6-H, #632-3): an end whose outcome was not observed (unknown,
        interrupted) — path → (relative path, its before-write digest). A new path enters
        with that digest behind the last identity, so it moves the identity only when its
        bytes changed (a same-bytes rewrite stays ``none``, a real change is seen at the
        next end); one without a known before-write digest is not tracked."""
        added = list(added)
        if seeds is not None:
            added = [p for p in added if p in self.files or p in seeds]
        keep = self.retained(added)
        self.files = OrderedDict((p, cache.get(p) or self.files.get(p)) for p in keep)
        if seeds is not None:
            basis = {p: self.basis[p] if p in self.basis else seeds.get(p) for p in keep}
            if all(entry is not None for entry in basis.values()):
                self.basis = basis
                self.last_identity = self.identity(basis.values())

    def identity(self, files: Iterable[tuple[str, bytes]]) -> bytes:
        """The workspace identity: ``files`` = (relative path, current digest) of every
        tracked file (read now; :data:`ABSENT` for a missing one, a ``big:`` sampled digest
        for one over the cap), order-independent."""
        items = sorted(
            (self._mac(_encode(("path", rel))), self._mac(_encode(("bytes", digest))))
            for rel, digest in files
        )
        return self._mac(_encode(("files", tuple(items))))

    def fingerprint(self, identity: bytes, rows: Iterable[tuple]) -> bytes:
        """The outcome: the workspace identity and the iteration's per-check rows as one
        set (sorted, no duplicates: how the checks were run — how often, in which order,
        split over how many calls — is not part of it). The agent's report never enters
        (a label is not an outcome)."""
        return self._mac(_encode(("v5", identity, tuple(sorted(set(rows))))))

    def classify(self, fingerprint: bytes | None) -> str:
        if fingerprint is None:
            return "none"
        return "repeat" if fingerprint in self.ring else "new"

    def record(
        self,
        end: str,
        report: tuple,
        fingerprint: bytes | None,
        streak: int,
        identity: bytes | None = None,
        basis: Mapping[str, tuple] | None = None,
    ) -> tuple[str, str]:
        """Record one counted iteration whose outcome is known (never an interrupted or an
        unknown one); returns ``(progress code, rule)`` — the rule a detector fires
        (``loop_detected`` / ``no_progress``) or ``""``. ``streak``: the grant's
        no-progress streak after this iteration was counted (``count_iteration``; a
        person's resume sets it to 0, so the no-action rule needs two no-action
        iterations since the last resume). ``basis``: path → (relative path, digest) behind
        ``identity``."""
        code = self.classify(fingerprint)
        if identity is not None:
            self.last_identity = identity
            self.basis = dict(basis) if basis is not None else {}
        window = list(self.ring)[-LOOP_WINDOW:]
        rule = ""
        if code == "repeat" and fingerprint in window:
            rule = "loop_detected"
        self.ring.append(fingerprint if code != "none" else None)
        self.no_action_streak = self.no_action_streak + 1 if end == "no_action" else 0
        if not rule and self.no_action_streak >= NO_ACTION_LIMIT and streak >= NO_ACTION_LIMIT:
            rule = "no_progress"
        kind, blocked = report
        if end == "reported_blocked" and kind == "blocked":
            if blocked == self.blocker:
                self.blocker_streak += 1
            else:
                self.blocker, self.blocker_streak = blocked, 1
        else:
            self.blocker, self.blocker_streak = "", 0
        return code, rule

    def merge_checks(
        self, checks: tuple[str, tuple] | None, wrote: bool, identity: bytes | None
    ) -> None:
        """W6c, at every iteration's end (whatever the end: a check that ran, ran):
        ``checks`` = the combined verdict of its run_checks calls after its last editor
        write (None: none), ``wrote`` = its editor wrote, ``identity`` = the workspace
        identity read at its end (None: unread). A write by the grant makes every earlier
        check stale. Otherwise the calls **combine** (an earlier ``fail`` / ``unknown`` is
        kept) — they replace the earlier verdict only when both identities are known and
        differ (positively another workspace): an unread identity never lets a recorded
        failure be forgotten."""
        if wrote:
            self.check_codes = checks
            self.check_identity = identity if checks is not None else None
            return
        if checks is None:
            return
        other = (
            identity is not None
            and self.check_identity is not None
            and not hmac.compare_digest(identity, self.check_identity)
        )
        self.check_codes = checks if other else combine_checks(self.check_codes, checks)
        self.check_identity = identity

    def claim_codes(self, identity: bytes | None) -> tuple[str, tuple, str]:
        """W6c, at a claim's end (after :meth:`merge_checks`): ``(checks, rows,
        workspace)``. ``pass`` / ``fail`` only when the claim's workspace identity
        ``identity`` is the one the checks ran on — another one: ``not_run`` (the files
        changed after the check, by anyone); either unread: ``unknown``. The workspace
        against the previous claim's identity (``unknown`` when either is unknown);
        ``identity`` becomes the claim identity. Memory only, no I/O."""
        codes = self.check_codes
        if codes is None:
            checks, rows = "not_run", ()
        elif codes[0] in ("pass", "fail"):
            known = identity is not None and self.check_identity is not None
            if not known:
                checks, rows = "unknown", ()
            elif hmac.compare_digest(identity, self.check_identity):
                checks, rows = codes
            else:
                checks, rows = "not_run", ()
        else:
            checks, rows = codes[0], ()
        previous = self.claim_identity
        if identity is None or previous is None:
            workspace = "unknown"
        else:
            workspace = "unchanged" if hmac.compare_digest(identity, previous) else "changed"
        self.claim_identity = identity
        return checks, rows, workspace

    def blocker_code(self, code: str) -> str:
        """The goal's blocked reason for an agent's ``blocked(code)`` report now: the
        report's own code, or ``same_blocker`` when it is the
        :data:`SAME_BLOCKER_LIMIT`-th in a row."""
        if code == self.blocker and self.blocker_streak + 1 >= SAME_BLOCKER_LIMIT:
            return "same_blocker"
        return code


class Trackers:
    """The trackers of recent grants (memory only): at most :data:`TRACKERS_KEPT`, the
    least recently used leave first — **never** one the caller keeps (a live grant's: a
    grant that has not ended keeps its history)."""

    def __init__(self) -> None:
        self._by_grant: OrderedDict[str, ProgressTracker] = OrderedDict()

    def find(self, grant_id: str) -> ProgressTracker | None:
        return self._by_grant.get(grant_id)

    def get(
        self,
        grant_id: str,
        fresh: ProgressTracker | None = None,
        keep: frozenset[str] = frozenset(),
    ) -> ProgressTracker:
        """The grant's tracker (now the most recently used); a missing one becomes
        ``fresh`` (made by the caller outside its lock: the key is drawn from the OS) or
        a new one. ``keep``: the grant ids that are never evicted."""
        tracker = self._by_grant.get(grant_id)
        if tracker is not None:
            self._by_grant.move_to_end(grant_id)
            return tracker
        tracker = self._by_grant[grant_id] = fresh or ProgressTracker()
        for old in [g for g in self._by_grant if g not in keep and g != grant_id]:
            if len(self._by_grant) <= TRACKERS_KEPT:
                break
            del self._by_grant[old]
        return tracker

    def ids(self) -> frozenset[str]:
        return frozenset(self._by_grant)

    def __len__(self) -> int:
        return len(self._by_grant)
