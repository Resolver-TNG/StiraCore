"""``stira authority status`` / ``stira authority unlatch <latch|all>`` (W2 of #296).

The host side of the latch store (ADR 0011 §5, §9): a latch is cleared **only** here, by
a person at the server's console, with the **server stopped**:

* the command takes the data directory's owner lock (``server.lock``, #285) — while a
  server runs it refuses (exit 2), so no server process (and none of its agent tools)
  exists while a latch is cleared, and nothing else writes the store meanwhile;
* it needs a **TTY** (stdin and stdout) and the typed word ``UNLATCH``;
* the clear is a host operation (:class:`stiracore.yolo.host_ops.HostOperations`, kind
  ``authority_unlatch``): one op id bound to the binding digest (subject, stop ids,
  generation) and the target generation (a digest of the latch state), confirmed once,
  applied once; the store's own check (:meth:`LatchStore.clear`) binds it again to this
  process's incarnation, the stop id of every latch it clears (``(incarnation, epoch)``)
  and the expected generation (any change → ``resume_stale``);
* every cleared latch is recorded as ``latch: set → cleared (human_unlatch)`` in the
  lifecycle log.

``reset-latches`` repairs an unusable, missing or rolled-back store: the old file is kept
(renamed), the new store has **every** latch set and its commitment goes into the epoch
store; then each latch is cleared with ``unlatch``. ``status`` only reads (also while the
server runs). Exit status: 0 = done, 1 = refused /
not confirmed / a record failed, 2 = usage, no data directory or the server is running.
"""

from __future__ import annotations

import getpass
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import incarnation
from .latches import LATCHES, LatchRefused, LatchStore
from .lifecycle_events import LifecycleLog, make_event

__all__ = ["add_parser", "run"]

WORD = "UNLATCH"
_ROUTE = "cli:authority/unlatch"


def add_parser(commands) -> None:
    parser = commands.add_parser(
        "authority", help="the authority latches: show them, or clear one (server stopped)"
    )
    sub = parser.add_subparsers(dest="authority_command", required=True)
    status = sub.add_parser("status", help="show the latches (read only)")
    unlatch = sub.add_parser(
        "unlatch", help="clear a latch (needs a TTY, the server stopped and the word UNLATCH)"
    )
    unlatch.add_argument("latch", help="the latch (kill, unhealthy, halted:<reason>) or all")
    reset = sub.add_parser(
        "reset-latches",
        help="replace an unusable latch store by one with EVERY latch set (server stopped, "
        "needs a TTY and the word RESET); then clear them with unlatch",
    )
    for command in (status, unlatch, reset):
        command.add_argument("--data-dir", type=Path, default=None, help="the server's data dir")


def _data_dir(args) -> Path:
    if args.data_dir is not None:
        return Path(args.data_dir)
    from stiracore.yolo.host_cli import default_data_dir

    return default_data_dir()


def _describe(store: LatchStore) -> list[str]:
    view = store.view()
    if not view["healthy"]:
        return [
            f"latch store: UNUSABLE ({view['unhealthy_reason']}) — every latch counts as set",
            (
                "repair: stop the server, then `stira authority reset-latches` (a new store "
                "with every latch set), then `stira authority unlatch all`"
            ),
        ]
    if not view["latches"]:
        return [f"latches: none (generation {view['generation']})"]
    lines = [f"latches (generation {view['generation']}):"]
    for name in view["latches"]:
        lines.append(f"  {name}  (set at epoch {view['stops'][name]})")
    return lines


def run(
    args,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    is_tty: Callable[[], bool] | None = None,
    read_line: Callable[[str], str] | None = None,
) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if is_tty is None:

        def is_tty() -> bool:
            return stdin.isatty() and stdout.isatty()

    if read_line is None:
        read_line = input
    try:
        data_dir = _data_dir(args)
    except Exception:  # noqa: BLE001 - e.g. an invalid STIRA_DATA_DIR
        print("Stira: the data directory setting is invalid", file=stderr)
        return 2
    if not data_dir.is_dir():
        print("Stira: no data directory there", file=stderr)
        return 2
    if args.authority_command == "status":
        store = LatchStore(data_dir)
        for line in _describe(store):
            print(line, file=stdout)
        if store.unhealthy_reason is None:
            _commitment, problem = _committed_problem(data_dir, store)
            if problem is not None:
                what = (
                    "the kill marker is not usable"
                    if problem.startswith("kill_marker")
                    else "NOT THE COMMITTED ONE"
                )
                print(
                    f"latch store: {what} ({problem}) — the server treats every latch as "
                    "set; repair: docs/authority-service.md, Repairs",
                    file=stdout,
                )
        return 0
    if args.authority_command == "reset-latches":
        return _reset(data_dir, stdout, stderr, is_tty, read_line)
    return _unlatch(args.latch, data_dir, stdout, stderr, is_tty, read_line)


def _unlatch(subject, data_dir: Path, stdout, stderr, is_tty, read_line) -> int:
    from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
    from stiracore.owner_lock import UnsafeLockFileError
    from stiracore.yolo.host_ops import (
        HostOperations,
        HostOpRefused,
        binding_digest,
        target_digest,
    )

    if subject != "all" and subject not in LATCHES:
        print(f"Stira: unknown latch (one of: {', '.join(LATCHES)}, all)", file=stderr)
        return 2
    lock = DataDirOwnerLock(data_dir)
    try:
        lock.acquire()
    except (DataDirLockError, UnsafeLockFileError, OSError):
        print(
            "Stira: a server runs on this data directory (or its lock is unusable); stop it first",
            file=stderr,
        )
        return 2
    try:
        store = LatchStore(data_dir)
        for line in _describe(store):
            print(line, file=stdout)
        if store.unhealthy_reason is not None:
            return 1
        # Fix 3 (O-M1): the same check as the server's start, before anything is written.
        commitment, problem = _committed_problem(data_dir, store)
        if problem is not None:
            hint = (
                "the kill marker is not a safe file: check and remove it by hand "
                "(docs/authority-service.md, Repairs)"
                if problem.startswith("kill_marker")
                else "this latch store is not the committed one: run "
                "`stira authority reset-latches` (every latch set), then unlatch"
            )
            print(f"Stira: refused ({problem}); nothing was written — {hint}", file=stderr)
            return 1
        latched = store.latched()
        names = sorted(latched) if subject == "all" else [subject]
        if not names or any(name not in latched for name in names):
            print("Stira: that latch is not set (nothing to clear)", file=stdout)
            return 1
        stops = {name: latched[name] for name in names}
        generation = store.generation
        state = {"stops": {n: s.as_list() for n, s in stops.items()}, "generation": generation}
        body = {"subject": subject, **state}
        me = incarnation.current()
        ops = HostOperations(incarnation=me, channel="command")
        kind = "authority_unlatch"
        binding = binding_digest(kind, _ROUTE, body)
        view = ops.request(
            kind,
            binding=binding,
            generation=target_digest(state),
            summary={"action": "unlatch", "operation": subject, "count": len(names)},
        )
        print(
            f"operation {view['short']} (code {view['code']}): clear {', '.join(names)}",
            file=stdout,
        )
        if not is_tty():
            print("Stira: a confirmation needs a terminal (stdin and stdout)", file=stderr)
            return 1
        try:
            typed = read_line(f"確認するには {WORD} と入力 / Type {WORD} to confirm: ")
        except EOFError:
            typed = ""
        if typed.strip() != WORD:
            print("Stira: not confirmed (nothing was cleared)", file=stdout)
            return 1
        try:
            ops.decide(
                view["id"],
                incarnation=me,
                decision="confirm",
                principal=f"local:{_account()}",
                channel="command",
            )
            # The target re-read now: any change since the request is stale.
            fresh = LatchStore(data_dir)
            now = fresh.latched()
            current = {
                "stops": {n: now[n].as_list() for n in names if now.get(n) is not None},
                "generation": fresh.generation,
            }
            ops.consume(view["id"], kind=kind, binding=binding, generation=target_digest(current))
        except HostOpRefused as error:
            code = "resume_stale" if error.code == "stale_operation" else error.code
            print(f"Stira: refused ({code}); nothing was cleared", file=stderr)
            return 1
        # The record first (W2 fix 1): a log that shows a latch set which the store
        # lacks makes the next start refuse the store, so the clear is recorded before
        # it happens; a log that cannot be written refuses the clear.
        if _record(data_dir, names, stops, me, stdout, stderr) != 0:
            return 1
        if "kill" in names and not _disarm_kill_marker(data_dir, stderr):
            return 1
        try:
            result = fresh.clear(
                subject, stops=stops, expected_generation=generation, op_id=view["id"], by=me
            )
        except LatchRefused as error:
            print(f"Stira: refused ({error.code}); nothing was cleared", file=stderr)
            return 1
        print(f"Stira: cleared {', '.join(result['cleared'])}; start the server again", file=stdout)
        _commit(data_dir, fresh, commitment, stderr)
        return 0
    finally:
        lock.release()


def _disarm_kill_marker(data_dir: Path, stderr) -> bool:
    """The fired kill marker goes with the kill latch (before the clear: if the clear is
    then refused, the latch itself still holds the kill). The next start re-arms it."""
    import os

    from .service import KILL_FIRED

    try:
        os.unlink(data_dir / KILL_FIRED)
    except FileNotFoundError:
        pass
    except OSError as error:
        print(
            f"Stira: the kill marker cannot be removed ({type(error).__name__}); nothing "
            "was cleared",
            file=stderr,
        )
        return False
    return True


def _committed_problem(data_dir: Path, store: LatchStore):
    """``(commitment, problem)``: the epoch file's commitment and the server's own check
    of ``store`` against it and the lifecycle log (an unusable epoch file is a problem)."""
    from . import epoch_store
    from .service import kill_marker_problem, latch_store_problem

    loaded = epoch_store.load(data_dir)
    if loaded.status == "bad":
        return None, loaded.reason or "epoch_store_unreadable"
    commitment = None if loaded.last is None else loaded.last.commitment
    problem = latch_store_problem(data_dir, store, commitment)
    return commitment, problem or kill_marker_problem(data_dir, store)


def _commit(data_dir: Path, store: LatchStore, before, stderr) -> None:
    """Best effort: the cleared store's commitment into the epoch file — only for a store
    that descends from the commitment checked before (never a lower or foreign one)."""
    from . import epoch_store

    try:
        loaded = epoch_store.load(data_dir)
        commitment = store.commitment()
        if loaded.status != "ok" or loaded.last is None or commitment is None:
            return
        if loaded.last.commitment != before or loaded.last.commitment == commitment:
            return
        if before is not None and not store.descends_from(before):
            return
        record = epoch_store.next_record(loaded.last, loaded.last.epoch, commitment)
        epoch_store.write(data_dir, (*loaded.records, record))
    except OSError as error:
        print(f"Stira: note: the commitment was not written ({type(error).__name__})", file=stderr)


def _record(data_dir: Path, cleared, stops, me: str, stdout, stderr) -> int:
    """One ``human_unlatch`` lifecycle event per latch to clear, before the clear. A
    failure refuses the clear (nothing changed)."""
    log = None
    try:
        log = LifecycleLog(data_dir, rotate=False)
        for name in cleared:
            log.append(
                make_event(
                    "latch",
                    name,
                    "set",
                    "cleared",
                    "human_unlatch",
                    incarnation=me,
                    epoch=stops[name].epoch,
                    ts_wall=time.time(),
                )
            )
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        reason = getattr(log, "unhealthy_reason", None)
        print(
            f"Stira: the lifecycle log cannot be written ({reason or type(error).__name__}); "
            "nothing was cleared — repair the log first (docs/authority-service.md)",
            file=stderr,
        )
        return 1
    return 0


def _reset(data_dir: Path, stdout, stderr, is_tty, read_line) -> int:
    """``reset-latches``: the repair of an unusable, missing or rolled-back latch store.
    The old file is kept (renamed), the new store has **every** latch set, and its
    commitment is written into the epoch store — nothing is cleared here; each latch is
    then cleared with ``unlatch`` (a token-bound, recorded operation)."""
    import os

    from stiracore.config.data_dir_lock import DataDirLockError, DataDirOwnerLock
    from stiracore.owner_lock import UnsafeLockFileError

    from . import epoch_store
    from .latches import LATCH_FILE

    lock = DataDirOwnerLock(data_dir)
    try:
        lock.acquire()
    except (DataDirLockError, UnsafeLockFileError, OSError):
        print(
            "Stira: a server runs on this data directory (or its lock is unusable); stop it first",
            file=stderr,
        )
        return 2
    try:
        loaded = epoch_store.load(data_dir)
        if loaded.status == "bad":
            print(
                f"Stira: the epoch store is unusable ({loaded.reason}); repair it first "
                "(docs/authority-service.md)",
                file=stderr,
            )
            return 1
        for line in _describe(LatchStore(data_dir)):
            print(line, file=stdout)
        print("this replaces the latch store by one with EVERY latch set", file=stdout)
        if not is_tty():
            print("Stira: a confirmation needs a terminal (stdin and stdout)", file=stderr)
            return 1
        try:
            typed = read_line("確認するには RESET と入力 / Type RESET to confirm: ")
        except EOFError:
            typed = ""
        if typed.strip() != "RESET":
            print("Stira: not confirmed (nothing was changed)", file=stdout)
            return 1
        path = data_dir / LATCH_FILE
        if os.path.lexists(path):
            kept = data_dir / f"{LATCH_FILE}.replaced-{os.urandom(4).hex()}"
            os.rename(path, kept)  # the evidence is kept, never overwritten
            print(f"Stira: the old store is kept as {kept.name}", file=stdout)
        me = incarnation.current()
        last = loaded.last
        epoch = 0 if last is None else last.epoch
        # The generation continues from the newest commitment (never restarts), and the
        # new store has a new id: any older copy of any store is refused afterwards.
        after = None if last is None else last.commitment
        commitment = LatchStore(data_dir).reset_all(incarnation=me, epoch=epoch, after=after)
        record = epoch_store.next_record(last, max(1, epoch), commitment)
        epoch_store.write(data_dir, (*loaded.records, record))
        print("Stira: every latch is set; clear them with `stira authority unlatch`", file=stdout)
        try:
            log = LifecycleLog(data_dir, rotate=False)
            for name in LATCHES:
                log.append(
                    make_event(
                        "latch",
                        name,
                        "unknown",
                        "set",
                        "latch_store_reset",
                        incarnation=me,
                        epoch=epoch,
                        ts_wall=time.time(),
                    )
                )
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            print(
                f"Stira: the latches are set, but their lifecycle record failed "
                f"({type(error).__name__}); repair the lifecycle log too",
                file=stderr,
            )
            return 1
        return 0
    except OSError as error:
        print(f"Stira: the reset failed ({type(error).__name__}); nothing is cleared", file=stderr)
        return 1
    finally:
        lock.release()


def _account() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"
    safe = "".join(c for c in name if c.isalnum() or c in "._-")[:48]
    return safe or "unknown"
