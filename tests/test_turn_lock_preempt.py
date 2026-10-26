"""ADR 0011 W6b (#296): every turn-lock holder pre-empts a goal iteration.

One wrapper (``stiracore.core.turn_lock.TurnLock``) raises the pre-emption signal before
the turn lock is tried; every acquirer takes the lock only through it. The enumeration
tests below read the package (AST) and fail when a new acquirer or a new turn start
appears that is not on the allow-lists; the behaviour tests drive each acquirer through
its real route on the real chat server (offline models, no Bedrock).
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from grants_exec_helpers import Counter, ScriptedProvider, make_env, use
from test_grants_server import SAME, _build, _close, _issue
from test_progress_report import clock, make_app  # noqa: F401 - the progress app fixtures

from stiracore.core.turn_lock import TurnLock
from stiracore.grants.executor import PreemptSignal
from stiracore.grants.schema import GrantRefused

ROOT = Path(__file__).resolve().parents[1]
EXEC = {"autonomy": {"stage_c": True, "stage_c_exec": True, "allow_browser_confirm": True}}
WAIT = 10

# --- the enumeration ------------------------------------------------------------------------

CHAT = "src/stiracore/observability/chat_api.py"
HOOK = "src/stiracore/integrations/webhook.py"
PROC = "src/stiracore/observability/procedural_api.py"
SERVER = "dashboard/stira_server.py"

#: Every place that takes the turn lock: (file, enclosing function) -> what it is. Each one
#: takes it through a ``TurnLock`` (checked below), so each raises the signal first.
ACQUIRERS = {
    (CHAT, "create_chat_blueprint.chat"): "chat turn (a message, a report's reply / redirect)",
    (CHAT, "create_chat_blueprint.start_auto"): "the automatic continue (ADR 0014)",
    (CHAT, "create_chat_blueprint.run_reset"): "/new (not a turn)",
    (HOOK, "WebhookReceiver.receive"): "webhook turn",
    (PROC, "create_procedural_review_blueprint.approve"): "procedure approval (not a turn)",
    (SERVER, "_build_app.resume_paused_turn.run"): "the resume of a restored card (#240)",
}

#: Every call that may start work on an agent — matched by the callee's last name
#: (``agent`` / ``_agent`` / ``runner`` / ``run_turn``, any underscores), ``stream_async`` /
#: ``invoke_async``, ``resume_approved_request``, or a thread ``target=`` of those:
#: (file, function, callee) -> the acquirer whose lock it runs under, or why it is not a
#: turn of the server's agent. A start reached through a name that matches none of these
#: (a renamed callable) is not seen: the check is syntactic (docs/grants.md says so).
TURN_STARTS = {
    (CHAT, "create_chat_blueprint.chat", "target=run_turn"): "create_chat_blueprint.chat",
    (CHAT, "create_chat_blueprint.start_auto", "target=run_turn"): (
        "create_chat_blueprint.start_auto"
    ),
    (CHAT, "create_chat_blueprint.run_turn", "runner"): "under: the chat route / start_auto",
    (HOOK, "WebhookReceiver.receive", "self.runner"): "WebhookReceiver.receive",
    (SERVER, "_build_app.resume_paused_turn.run", "resume_approved_request"): (
        "_build_app.resume_paused_turn.run"
    ),
    (SERVER, "_build_app.runner", "agent"): "under: the runner of the chat route and webhook",
    ("src/stiracore/agent/hitl_resume.py", "resume_approved_request", "agent"): (
        "under: resume_paused_turn's lock"
    ),
    ("src/stiracore/agent/nxp_agent.py", "NXPAgent.__call__", "self.agent"): (
        "not a start: the agent's own call (its callers are listed)"
    ),
    ("src/stiracore/agent/subagent.py", "NXPSubagent.__call__", "self.agent"): (
        "not a start: a child's own call (its callers are listed)"
    ),
    ("src/stiracore/agent/subagent.py", "GuardedDelegate.stream_async", "super().stream_async"): (
        "not a start: the guarded child's own stream"
    ),
    ("src/stiracore/tools/delegate_tool.py", "DelegateTool._build_and_run", "child.agent"): (
        "not a turn: a delegate child inside a turn that holds the lock"
    ),
    ("src/stiracore/control/agent_jobs.py", "GatedJobExecutor.__call__", "child.agent"): (
        "independent background job: a scheduler agent job's own child agent (ADR 0024), "
        "never the turn lock; it does not pre-empt an iteration"
    ),
    ("src/stiracore/control/agent_jobs.py", "GatedJobExecutor.resume", "child.agent"): (
        "independent background job: the approved job run's resume (through the scheduler)"
    ),
    ("src/stiracore/scheduler/executors.py", "AgentJobExecutor.__call__", "agent"): (
        "independent background job: the plain scheduler agent-job executor"
    ),
    ("src/stiracore/grants/executor.py", "GoalIterationExecutor._call", "child.agent"): (
        "independent background job: the goal iteration itself (it is what is pre-empted)"
    ),
    ("src/stiracore/control/distillers.py", "LLMDistiller.distill", "agent"): (
        "independent background job: the distiller's own model-only agent (no tools)"
    ),
    ("src/stiracore/control/memory_jobs.py", "distiller_model_factory.factory.call", "agent"): (
        "independent background job: a memory job's own model-only agent"
    ),
    ("src/stiracore/grants/executor.py", "GoalIterationExecutor._run_checks", "runner"): (
        "not a turn: the iteration's own run_checks runner"
    ),
    ("src/stiracore/eval/runners.py", "run_nxp", "agent"): "not a turn: the eval CLI",
    ("src/stiracore/integrations/runtime.py", "ChatSession.runner", "self.agent"): (
        "not a turn: the terminal / bot chat session (its own agent, no server)"
    ),
    (
        "src/stiracore/integrations/slack_bot.py",
        "register_handlers.on_message",
        "sessions.get(key).runner",
    ): "not a turn: the Slack bot's own sessions (no server)",
    ("src/stiracore/yolo/host_dialog.py", "dialog_ready", "runner"): (
        "not a turn: the host dialog's subprocess runner"
    ),
    ("src/stiracore/yolo/host_dialog.py", "DialogConfirmer.ask", "self._runner"): (
        "not a turn: the host dialog's subprocess runner"
    ),
}

#: W6-H (#631-1): every ``TurnLock(...)`` construction (any alias of the class; one per site). Any other is
#: a problem — a module that stores an unsignalled ``TurnLock(threading.Lock())`` in place of
#: the shared one is reported even when it never acquires it there. The runtime wiring test
#: (``test_every_turn_lock_reachable_from_the_server_is_the_shared_one``) checks the rest.
CONSTRUCTIONS = {
    (SERVER, "_build_app"): "the shared lock, with the executor's pre-emption signal",
    ("dashboard/app.py", "create_app"): "the observer app's lock (no executor: no signal)",
}

#: W6d placeholder (#631-3): every turn or goal-iteration start in a heartbeat / scheduler
#: module (``UNATTENDED``) must be registered here — (file, function, callee) -> the
#: ``ACQUIRERS`` entry whose ``TurnLock`` it runs under, or ``"iteration: <why>"`` for a
#: press of the goal iteration executor (an iteration does not take the turn lock: it is
#: what a turn pre-empts). W6d: the scheduler's ``grant_tick`` press.
UNATTENDED = ("src/stiracore/heartbeat/", "src/stiracore/scheduler/")
W6D_STARTS: dict = {
    ("src/stiracore/scheduler/grant_tick.py", "GrantTicker._press", "self._executor.step"): (
        "iteration: the heartbeat start presses the executor's own step (W6d); every "
        "turn-lock holder pre-empts it like a person's press"
    ),
}
#: The independent background jobs already in an unattended module (ADR 0024; they never
#: take the turn lock and do not pre-empt): frozen — a new one goes to ``W6D_STARTS``.
UNATTENDED_JOBS = frozenset(
    {("src/stiracore/scheduler/executors.py", "AgentJobExecutor.__call__", "agent")}
)

#: TurnLock's private plain lock: no other module names it (attribute or string).
PRIVATE = "_plain_lock"
_SEED = re.compile(r"\bturn_lock\b")
_START = re.compile(r"^_*(agent|runner|run_turn)$")
_START_ATTRS = {"stream_async", "invoke_async"}
_WRAPPERS = ("TurnLock", "TurnLock.of")


def _files():
    return sorted((ROOT / "src/stiracore").rglob("*.py")) + sorted(
        (ROOT / "dashboard").rglob("*.py")
    )


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_lock(node, aliases, wrappers=_WRAPPERS):
    """``node`` denotes the turn lock (or its getter): an alias, ``x.turn_lock``,
    ``x["turn_lock"]``, ``x.get("turn_lock")``, ``TurnLock(...)``, a getter call."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr == "turn_lock" or ast.unparse(node) in aliases
    if isinstance(node, ast.Subscript):
        key = node.slice
        named = isinstance(key, ast.Constant) and _SEED.search(str(key.value))
        return bool(named) or ast.unparse(node) in aliases
    if isinstance(node, ast.Call):
        func = node.func
        if ast.unparse(func) in wrappers or _is_lock(func, aliases, wrappers):
            return True
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and any(isinstance(a, ast.Constant) and _SEED.search(str(a.value)) for a in node.args)
        )
    return False


def _where(node, parents):
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
    return ".".join(reversed(names))


def _audit(rel, source):
    """-> (turn-lock acquire sites, turn starts, problems) of one module.

    Conservative: every use of the turn lock (or of its getter) must be one of the known
    forms — ``.acquire()`` (an acquire site), ``.release()`` / ``.locked()``, ``with``, a
    binding to another name, the ``turn_lock=`` keyword, ``TurnLock(...)`` /
    ``TurnLock.of(...)``, a truth / ``is None`` test. Anything else — a bound method
    (``take = lock.acquire``), the lock passed to a helper, ``vars()`` / ``getattr()``,
    another attribute — is a problem, and so is any mention of the private plain lock."""
    tree = ast.parse(source)
    parents = _parents(tree)
    aliases = {"turn_lock"}
    classes = {"TurnLock"}  # W6-H: the class under any imported name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for name in node.names:
                if _SEED.search(name.name):
                    aliases.add(name.asname or name.name)
                if name.name.rsplit(".", 1)[-1] == "TurnLock":
                    classes.add(name.asname or "TurnLock")
    wrappers = {*_WRAPPERS, *classes, *(f"{c}.of" for c in classes)}

    def is_lock(node):
        return _is_lock(node, aliases, wrappers)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is not None and is_lock(node.value):
                    for target in targets:
                        if ast.unparse(target) not in aliases:
                            aliases.add(ast.unparse(target))
                            changed = True
    acquires, starts, problems, built = set(), set(), [], []
    for node in ast.walk(tree):
        where = _where(node, parents)
        private = (isinstance(node, ast.Attribute) and node.attr == PRIVATE) or (
            isinstance(node, ast.Constant) and PRIVATE in str(node.value)
        )
        if private and rel != "src/stiracore/core/turn_lock.py":
            problems.append(f"{rel}:{where}: the private plain lock")
        if isinstance(node, ast.Call):
            func = node.func
            named = ast.unparse(func)
            if named in classes:  # at most one per listed site
                built.append(where)
                if (rel, where) not in CONSTRUCTIONS or built.count(where) > 1:
                    problems.append(f"{rel}:{where}: an unlisted TurnLock construction")
            if named in {f"{c}.acquire" for c in classes}:
                acquires.add(where)  # W6-H: the unbound TurnLock.acquire(lock) (it signals)
            last = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            targets = [k.value for k in node.keywords if k.arg == "target"]
            for target in targets:
                tail = ast.unparse(target).rsplit(".", 1)[-1]
                if _START.match(tail):
                    starts.add((where, f"target={ast.unparse(target)}"))
            if _START.match(last) or last in _START_ATTRS or last == "resume_approved_request":
                starts.add((where, ast.unparse(func)))
        if not is_lock(node):
            continue
        parent = parents.get(node)
        if is_lock(parent) and isinstance(parent, ast.Call) and parent.func is node:
            continue  # a getter call: the call is the use
        use = None
        if isinstance(parent, ast.Attribute) and parent.value is node:
            called = isinstance(parents.get(parent), ast.Call) and parents[parent].func is parent
            if called and parent.attr == "acquire":
                acquires.add(where)
                use = "acquire"
            elif called and parent.attr in ("release", "locked"):
                use = parent.attr
        elif isinstance(parent, ast.withitem) and parent.context_expr is node:
            acquires.add(where)
            use = "with"
        elif isinstance(parent, ast.Call) and node in parent.args:
            if ast.unparse(parent.func) in wrappers:
                use = "wrapped"
        elif isinstance(parent, ast.keyword):
            if parent.arg == "turn_lock":
                use = "keyword"
        elif isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            if node is parent.value:
                use = "bound"
            elif is_lock(parent.value) or (
                isinstance(parent.value, ast.Call) and ast.unparse(parent.value.func) in wrappers
            ):
                use = "binding"
            else:
                problems.append(f"{rel}:{where}: bound to {ast.unparse(parent.value)[:60]}")
                continue
        elif isinstance(parent, ast.Lambda):
            holder = parents.get(parent)
            if isinstance(holder, ast.keyword) and holder.arg == "turn_lock":
                use = "getter"
        elif isinstance(parent, ast.BoolOp | ast.UnaryOp | ast.If | ast.IfExp | ast.While):
            if not isinstance(parent, ast.IfExp) or parent.test is node:
                use = "truth test"
        elif isinstance(parent, ast.Compare):
            nones = [c for c in parent.comparators if isinstance(c, ast.Constant)]
            if all(isinstance(o, ast.Is | ast.IsNot) for o in parent.ops) and nones:
                use = "is None"
        if use is None:
            problems.append(f"{rel}:{where}: {ast.unparse(parent)[:80]}")
    return acquires, starts, problems


def _package():
    acquires, starts, problems = set(), set(), []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == "src/stiracore/core/turn_lock.py":
            continue
        found, started, wrong = _audit(rel, path.read_text(encoding="utf-8"))
        acquires |= {(rel, where) for where in found}
        starts |= {(rel, where, call) for where, call in started}
        problems += wrong
    return acquires, starts, problems


def test_every_turn_lock_acquirer_is_on_the_list_and_takes_it_through_turnlock():
    acquires, _starts, problems = _package()
    assert problems == []
    assert acquires == set(ACQUIRERS), sorted(acquires ^ set(ACQUIRERS))


def test_every_turn_start_is_on_the_list_and_its_holder_is_an_acquirer():
    _acquires, starts, _problems = _package()
    assert starts == set(TURN_STARTS), sorted(starts ^ set(TURN_STARTS))
    holders = {where for (_rel, where) in ACQUIRERS}
    for holder in TURN_STARTS.values():
        assert holder in holders or holder.startswith(
            ("under:", "not a", "independent background job:")
        ), holder


#: Negative controls (Astra / Opus W6b round 1): each, added to a package module, must be
#: reported — as a problem, an unlisted acquire site or an unlisted turn start.
BYPASSES = {
    "direct acquire": "def fresh(turn_lock):\n    turn_lock.acquire()\n",
    "with": "def fresh(turn_lock):\n    with turn_lock:\n        pass\n",
    "bound method": "def fresh(turn_lock):\n    take = turn_lock.acquire\n    take()\n",
    "helper": "def helper(m):\n    m.acquire()\ndef fresh(turn_lock):\n    helper(turn_lock)\n",
    "imported alias": "from x import turn_lock as mutex\ndef fresh():\n    mutex.acquire()\n",
    "imported, plain": "from x import turn_lock\ndef fresh():\n    turn_lock.acquire()\n",
    "plain lock bound": "from threading import Lock as M\nturn_lock = M()\n",
    "plain lock attr": "class R:\n    def __init__(self):\n        self.turn_lock = object()\n",
    "vars escape": "def fresh(turn_lock):\n    return vars(turn_lock)['_x']\n",
    "getattr escape": "def fresh(turn_lock):\n    getattr(turn_lock, 'x').acquire()\n",
    "private by name": "def fresh(lock):\n    lock._plain_lock.acquire()\n",
    "private by string": "def fresh(lock):\n    getattr(lock, '_plain_' + 'lock')\n"
    "    return '_plain_lock'\n",
    "alias from a getter": (
        "def fresh(app):\n    lk = app.extensions.get('turn_lock')\n    lk.__dict__\n"
    ),
    "subscript alias": "def fresh(app):\n    lk = app.extensions['turn_lock']\n    lk.acquire()\n",
    "attribute of another": "def fresh(hook):\n    hook.turn_lock.acquire()\n",
    "returned": "def fresh(turn_lock):\n    return turn_lock\n",
    "other keyword": "def fresh(turn_lock):\n    run(mutex=turn_lock)\n",
    "start: self._agent": "class C:\n    def go(self):\n        self._agent('x')\n",
    "start: stream_async": "async def go(agent):\n    agent.stream_async('x')\n",
    "start: child.agent": "def go(child):\n    child.agent('x')\n",
    "start: thread target": "def go(t, w):\n    t.Thread(target=w.run_turn)\n",
    # W6-H (#631-1): an unsignalled lock stored in place of the shared one, an aliased
    # constructor, the unbound acquire
    "unsignalled lock stored": (
        "import threading\nfrom stiracore.core.turn_lock import TurnLock\n"
        "def wire(app):\n    app.extensions['turn_lock'] = TurnLock(threading.Lock())\n"
    ),
    "aliased constructor": (
        "from stiracore.core.turn_lock import TurnLock as Mutex\n"
        "def wire():\n    m = Mutex()\n    m.acquire()\n"
    ),
    "unbound acquire": (
        "from stiracore.core.turn_lock import TurnLock\n"
        "def fresh(lock):\n    TurnLock.acquire(lock)\n"
    ),
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_the_enumeration_reports_each_negative_control(name):
    rel = "src/stiracore/w6b_probe.py"
    acquires, starts, problems = _audit(rel, BYPASSES[name])
    unlisted_acquires = {(rel, w) for w in acquires} - set(ACQUIRERS)
    unlisted_starts = {(rel, w, c) for w, c in starts} - set(TURN_STARTS)
    assert problems or unlisted_acquires or unlisted_starts, name


def test_a_second_turn_lock_built_at_a_listed_site_is_reported():
    """W6-H (#631-1): the server builds exactly one (an unsignalled second one handed to a
    holder is reported even inside ``_build_app``)."""
    head = "import threading\nfrom stiracore.core.turn_lock import TurnLock as T\n"
    one = "def _build_app(p):\n    lock = T(threading.Lock(), p)\n"
    assert _audit(SERVER, head + one)[2] == []
    _acquires, _starts, problems = _audit(SERVER, head + one + "    hook(T(threading.Lock()))\n")
    assert any("unlisted TurnLock construction" in p for p in problems), problems


def _unattended(rel, source):
    """W6d placeholder (#631-3): the turn and goal-iteration starts of an unattended
    (heartbeat / scheduler) module that are not registered."""
    _acquires, starts, _problems = _audit(rel, source)
    found = {(rel, where, call) for where, call in starts}
    tree = ast.parse(source)
    parents = _parents(tree)
    for node in ast.walk(tree):  # a press of the goal iteration executor
        func = getattr(node, "func", None)
        named = isinstance(func, ast.Attribute) and func.attr == "step"
        if named and "executor" in ast.unparse(func.value):
            found.add((rel, _where(node, parents), ast.unparse(func)))
    return found - set(W6D_STARTS) - UNATTENDED_JOBS


def test_w6d_every_unattended_start_is_registered_and_holds_the_turn_lock():
    """W6d placeholder (#631-3): a heartbeat- or scheduler-started turn must take the
    shared ``TurnLock`` — its holder is an ``ACQUIRERS`` entry — and a heartbeat-started
    goal iteration registers itself (``iteration: ...``). Fails until W6d does."""
    unregistered = set()
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(UNATTENDED):
            unregistered |= _unattended(rel, path.read_text(encoding="utf-8"))
    assert unregistered == set(), sorted(unregistered)
    holders = {where for (_rel, where) in ACQUIRERS}
    for key, holder in W6D_STARTS.items():
        assert key[0].startswith(UNATTENDED), key
        assert holder in holders or holder.startswith("iteration: "), (key, holder)
    assert UNATTENDED_JOBS <= set(TURN_STARTS)


@pytest.mark.parametrize(
    "source",
    [
        "class Tick:\n    def run(self):\n        self.agent('heartbeat')\n",
        "def tick(grant_executor, gid):\n    grant_executor.step(gid)\n",
        "def tick(app, gid):\n    app.extensions['grant_executor'].step(gid)\n",
    ],
)
def test_w6d_an_unregistered_heartbeat_start_is_reported(source):
    assert _unattended("src/stiracore/heartbeat/w6d_probe.py", source)


def test_the_enumeration_scans_dashboard_subpackages():
    rels = {p.relative_to(ROOT).as_posix() for p in _files()}
    assert any(r.startswith("dashboard/") and r.count("/") > 1 for r in rels)


# --- the wrapper ----------------------------------------------------------------------------


class _Raw:
    """A plain lock whose acquire runs ``during(...)`` first (the instant before the try)."""

    def __init__(self):
        self.inner = threading.Lock()
        self.during = None

    def acquire(self, blocking=True, timeout=-1):
        if self.during is not None:
            self.during()
        return self.inner.acquire(blocking, timeout)

    def release(self):
        self.inner.release()

    def locked(self):
        return self.inner.locked()


def test_the_signal_is_raised_before_the_lock_is_tried_and_its_mark_ends_after():
    signal, raw = PreemptSignal(), _Raw()
    seen = []
    raw.during = lambda: seen.append((signal.seq, signal.pending, raw.locked()))
    lock = TurnLock(raw, signal)
    assert lock.acquire(blocking=False) is True
    assert seen == [(1, 1, False)] and signal.pending == 0 and lock.locked()
    # A refused attempt signals too (the chat route's order) and leaves nothing marked.
    assert lock.acquire(blocking=False) is False
    assert seen[-1] == (2, 1, True) and signal.pending == 0
    lock.release()
    assert not lock.locked()


def test_the_unbound_acquire_still_signals():
    """W6-H (#631-1): ``TurnLock.acquire(lock)`` is the wrapper's own method (the audit
    lists it as an acquire site, too)."""
    signal = PreemptSignal()
    lock = TurnLock(threading.Lock(), signal)
    assert TurnLock.acquire(lock, blocking=False) is True
    assert signal.seq == 1 and signal.pending == 0
    TurnLock.release(lock)
    assert not lock.locked()


def test_without_a_signal_it_is_the_plain_lock():
    raw = _Raw()
    raw.during = lambda: None
    lock = TurnLock(raw)
    assert not lock.signalled and TurnLock.of(lock) is lock
    assert lock.acquire(blocking=False) and not lock.acquire(blocking=False)
    lock.release()
    assert isinstance(TurnLock.of(None), TurnLock) and not TurnLock.of(None).signalled


def _lock_contract(lock):
    """What a caller of a plain lock can observe: return values and exception types."""
    out = []

    def record(name, fn):
        try:
            out.append((name, "ok", fn()))
        except Exception as error:  # noqa: BLE001 - the type is the observation
            out.append((name, "raises", type(error)))

    record("acquire()", lambda: lock.acquire())
    record("acquire(False) held", lambda: lock.acquire(False))
    record("acquire(timeout=0.01) held", lambda: lock.acquire(timeout=0.01))
    record("acquire(True, 0.01) held", lambda: lock.acquire(True, 0.01))
    record("locked() held", lambda: lock.locked())
    other = {}
    thread = threading.Thread(target=lambda: other.setdefault("r", lock.release()))
    thread.start()
    thread.join(WAIT)
    out.append(("release() from another thread", other))
    record("locked() after", lambda: lock.locked())
    record("release() unlocked", lambda: lock.release())
    record("acquire(False, 1)", lambda: lock.acquire(False, 1))
    record("acquire(timeout=-2)", lambda: lock.acquire(timeout=-2))
    record("acquire(blocking='x')", lambda: lock.acquire(blocking="x", timeout=0.01))
    if lock.locked():
        lock.release()
    with lock as value:
        out.append(("with ... as", value, lock.locked()))
    out.append(("after with", lock.locked()))
    try:
        with lock:
            raise KeyError("inside")
    except KeyError:
        out.append(("raised inside with", lock.locked()))
    out.append(("__exit__ returns", lock.__enter__(), lock.__exit__(None, None, None)))
    record("acquire(False) free", lambda: lock.acquire(False))
    record("release() -> None", lambda: lock.release())
    return out


@pytest.mark.parametrize("signalled", [False, True])
def test_a_turn_lock_is_a_drop_in_for_a_plain_lock(signalled):
    """Astra W6b B1: with the switches off (no signal) — and with one — every return value
    and exception type a caller sees is the plain lock's."""
    signal = PreemptSignal() if signalled else None
    assert _lock_contract(TurnLock(threading.Lock(), signal)) == _lock_contract(threading.Lock())
    assert _lock_contract(TurnLock(None, signal)) == _lock_contract(threading.Lock())
    if signal is not None:
        assert signal.pending == 0  # no attempt leaves the mark behind, raising ones included


def test_a_press_between_the_signal_and_the_lock_is_refused(tmp_path):
    """The instant between an acquirer's signal and its lock: no press is admitted."""
    env = make_env(tmp_path)
    raw = _Raw()
    lock = TurnLock(raw, env.executor.preempt_signal)
    env.executor._turn_busy = lock.locked
    env.provider.script = [
        [use("editor", {"command": "create", "path": str(env.sub / "w.md"), "file_text": "x"})]
    ]
    refused = []

    def press():
        try:
            env.executor.step(env.grant_id)
        except GrantRefused as error:
            refused.append(error.extra["reason"])

    raw.during = press
    assert lock.acquire(blocking=False)
    try:
        assert refused == ["user_turn"] and env.provider.calls == 0
        assert not (env.sub / "w.md").exists()
    finally:
        lock.release()


def test_a_waiting_acquirer_holds_presses_off_until_it_has_the_lock(tmp_path):
    """resume_paused_turn waits for the lock: while it waits (another turn holds it) and
    in the instant the other releases, no press is admitted."""
    env = make_env(tmp_path)
    lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
    env.executor._turn_busy = lock.locked
    other = TurnLock(lock._plain_lock)  # another holder (the signal does not matter here)
    assert other.acquire(blocking=False)
    got = threading.Event()
    waiter = threading.Thread(target=lambda: lock.acquire(timeout=WAIT) and got.set())
    waiter.start()
    deadline = time.monotonic() + WAIT
    while env.executor.preempt_signal.pending == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    other.release()  # the lock is free for an instant before the waiter has it
    with pytest.raises(GrantRefused) as info:
        env.executor.step(env.grant_id)
    assert info.value.extra["reason"] == "user_turn"
    waiter.join(WAIT)
    assert got.is_set() and env.provider.calls == 0
    lock.release()


def test_a_turn_after_the_consume_stops_the_request_through_the_real_lock(tmp_path):
    """The W5 window (between the model call's consume and its request), now with the
    real wrapper: the request is never sent."""
    env = make_env(tmp_path)
    lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
    env.executor._turn_busy = lock.locked
    consume = env.executor._consume
    took = []

    def race(it, permit, kind, reservation_id=None):
        entry = consume(it, permit, kind, reservation_id)
        if kind == "model" and not took:
            took.append(lock.acquire(blocking=False))
        return entry

    env.executor._consume = race
    env.executor.step(env.grant_id)
    try:
        assert env.executor.wait_idle(WAIT)
        view = env.executor.view(env.grant_id)
        assert took == [True] and env.provider.calls == 0
        assert view["end"] == "yielded_user_turn"
    finally:
        lock.release()


# --- the real server: each acquirer through its route ---------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    from test_new_session import LoopModel

    from stiracore.agent import nxp_agent
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    build, _data = _build(tmp_path, monkeypatch, EXEC)
    model = LoopModel()
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    app = build()
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    ws = (roots[0] if roots else tmp_path) / "grant-ws"
    ws.mkdir(parents=True, exist_ok=True)
    yield SimpleNamespace(app=app, model=model, ws=ws, client=app.test_client())
    _close(app)


def _headers(app, key):
    return {**SAME, "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"], "Idempotency-Key": key}


def _iteration_in_its_model_call(app, client, ws, grant_id=None):
    """A grant iteration inside its first model call (held there); its answer asks for an
    editor write, a second call would follow."""
    executor = app.extensions["grant_executor"]
    grant_id = grant_id or _issue(client, ws)["grant"]["grant_id"]
    in_call, release = threading.Event(), threading.Event()
    provider = ScriptedProvider(
        [
            [use("editor", {"command": "create", "path": str(ws / "during.md"), "file_text": "x"})],
            [{"text": "done"}],
        ]
    )
    provider.on_call = lambda n: (in_call.set(), release.wait(WAIT)) if n == 1 else None
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    started = client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME)
    assert started.status_code == 200, started.get_json()
    assert in_call.wait(WAIT)
    return SimpleNamespace(
        executor=executor,
        provider=provider,
        release=release,
        grant_id=grant_id,
        seq=executor.preempt_signal.seq,
        ws=ws,
    )


def _hold_turn(model):
    """The server agent's next model call blocks (inside the turn) until released."""
    entered, release = threading.Event(), threading.Event()

    def during():
        model.during = None
        entered.set()
        assert release.wait(WAIT)

    model.during = during
    return entered, release


def _yields_while_the_turn_runs(app, client, it, *, signals=1):
    """The turn holds the lock now: the iteration's held call returns; it yields at the
    next boundary (no write, no second call); a new press is refused; one signal."""
    assert it.executor.preempt_signal.seq == it.seq + signals
    assert app.extensions["turn_lock"].locked()
    it.release.set()
    assert it.executor.wait_idle(WAIT)
    assert app.extensions["turn_lock"].locked()  # the turn still runs
    view = it.executor.view(it.grant_id)
    assert view["end"] == "yielded_user_turn" and view["state"] == "yielded", view
    assert it.provider.calls == 1 and view["writes"] == 0 and view["model_calls"] == 1
    assert not (it.ws / "during.md").exists()
    again = client.post(f"/api/grants/{it.grant_id}/step", json={}, headers=SAME)
    assert again.status_code == 409 and again.get_json()["reason"] == "user_turn"
    assert it.provider.calls == 1


def _wait_free(app):
    lock = app.extensions["turn_lock"]
    assert lock._plain_lock.acquire(timeout=WAIT)  # the turn has ended (a test-only plain read)
    lock._plain_lock.release()


def test_the_server_builds_one_signalled_turn_lock_for_every_holder(server):
    app = server.app
    lock = app.extensions["turn_lock"]
    executor = app.extensions["grant_executor"]
    assert isinstance(lock, TurnLock) and lock._preempt is executor.preempt_signal
    assert app.blueprints["webhooks"].turn_lock is lock


def _reachable(app):
    """W6-H (#631-1): every lock reachable from the server's wiring — the extensions, the
    blueprints, the view functions and (recursively) their closures, defaults, bound
    objects and the attributes of the package's objects: (where, object) for each
    ``TurnLock``, and each plain lock bound to a turn-ish name."""
    import functools
    import types

    plain = type(threading.Lock())
    turnish = re.compile(r"turn|generation")
    seen, locks, named_plain = set(), [], []
    todo = [
        ("extensions", app.extensions),
        ("blueprints", dict(app.blueprints)),
        ("views", dict(app.view_functions)),
    ]
    while todo:
        name, obj = todo.pop(0)  # breadth first: the shortest path names each object
        if isinstance(obj, TurnLock):  # every path to a lock is recorded
            locks.append((name, obj))
            continue
        if isinstance(obj, plain):
            if turnish.search(name.rsplit(">", 1)[-1]):
                named_plain.append(name)
            continue
        if id(obj) in seen or name.count(">") > 12:
            continue
        seen.add(id(obj))
        if isinstance(obj, dict):
            todo += [(f"{name}>{k}", v) for k, v in obj.items()]
        elif isinstance(obj, list | tuple | set | frozenset):
            todo += [(f"{name}>[]", v) for v in obj]
        elif isinstance(obj, types.FunctionType):
            for var, cell in zip(obj.__code__.co_freevars, obj.__closure__ or (), strict=True):
                try:
                    todo.append((f"{name}>{var}", cell.cell_contents))
                except ValueError:  # an empty cell
                    pass
            todo += [(f"{name}>default", v) for v in obj.__defaults__ or ()]
            todo += [(f"{name}>{k}", v) for k, v in (obj.__kwdefaults__ or {}).items()]
        elif isinstance(obj, types.MethodType):
            todo += [(f"{name}>self", obj.__self__), (f"{name}>func", obj.__func__)]
        elif isinstance(obj, functools.partial):
            todo += [(f"{name}>func", obj.func), (f"{name}>args", obj.args)]
            todo.append((f"{name}>kw", obj.keywords))
        elif type(obj).__module__.startswith(("stiracore", "dashboard", "stira_server")):
            try:
                todo += [(f"{name}>{k}", v) for k, v in vars(obj).items()]
            except TypeError:  # no __dict__
                pass
    return locks, named_plain


def test_every_turn_lock_reachable_from_the_server_is_the_shared_one(server):
    """W6-H (#631-1): a module that stores another (e.g. an unsignalled) lock in place of
    the shared ``TurnLock`` anywhere in the server's wiring fails here, whether or not it
    acquires it in that module."""
    app = server.app
    shared = app.extensions["turn_lock"]
    assert shared.signalled
    locks, named_plain = _reachable(app)
    others = sorted(name for name, lock in locks if lock is not shared)
    assert others == [] and named_plain == [], (others, named_plain)
    names = " ".join(name for name, _lock in locks)
    for holder in ("generation_lock", "webhooks>turn_lock", "resume_paused_turn>turn_lock"):
        assert holder in names, (holder, sorted(name for name, _ in locks))


def test_a_chat_turn_pre_empts_the_running_iteration(server):
    app, client = server.app, server.client
    it = _iteration_in_its_model_call(app, client, server.ws)
    entered, release = _hold_turn(server.model)
    response = client.post("/api/chat", json={"message": "hello"}, headers=_headers(app, "c1"))
    assert response.status_code == 200
    assert entered.wait(WAIT)
    try:
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    response.get_data()
    _wait_free(app)


def test_a_webhook_turn_pre_empts_the_running_iteration(server, monkeypatch):
    app, client = server.app, server.client
    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "offline-key")
    it = _iteration_in_its_model_call(app, client, server.ws)
    entered, release = _hold_turn(server.model)
    raw = b'{"event":"ping"}'
    sig = "sha256=" + hmac.new(b"offline-key", raw, hashlib.sha256).hexdigest()
    out = {}

    def send():
        out["response"] = app.test_client().post(
            "/api/webhooks/generic",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    assert entered.wait(WAIT)
    try:
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    sender.join(WAIT)
    assert out["response"].status_code == 200


def test_the_resume_of_a_restored_card_pre_empts_the_running_iteration(server, monkeypatch):
    from dashboard import stira_server

    app, client = server.app, server.client
    it = _iteration_in_its_model_call(app, client, server.ws)
    entered, release = threading.Event(), threading.Event()

    def held_resume(agent, store, request_id, on_replay=None):
        entered.set()
        assert release.wait(WAIT)
        return SimpleNamespace(resumed=False, reason="test", result=None)

    monkeypatch.setattr(stira_server, "resume_approved_request", held_resume)
    store = app.extensions["approval_store"]
    store.submit(
        request_id="req-1",
        agent_id="stira",
        tool_name="file_write",
        tool_input={"path": "a.txt", "content": "x"},
        expires_at=time.time() + 600,
        checkpoint_id="cp-1",
    )
    decided = client.post(
        "/api/hitl/req-1/decision",
        json={"action": "approve", "args_hash": store.get("req-1").args_hash},
        headers=_headers(app, "d1"),
    )
    assert decided.status_code == 202, decided.get_json()
    assert entered.wait(WAIT)
    try:
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    _wait_free(app)


def test_a_procedure_approval_signals_before_it_takes_the_lock(server):
    """Not a turn, but a turn-lock holder: it raises the signal once, before its lock; the
    iteration then performs no further effect."""
    app, client = server.app, server.client
    it = _iteration_in_its_model_call(app, client, server.ws)
    approve = client.post(
        "/api/memory/procedural/proposals/k1/approve",
        json={"sha256": "0" * 64, "confirm": True},
        headers=_headers(app, "p1"),
    )
    assert approve.status_code in (404, 409, 400), approve.get_json()
    assert it.executor.preempt_signal.seq == it.seq + 1
    it.release.set()
    assert it.executor.wait_idle(WAIT)
    view = it.executor.view(it.grant_id)
    assert view["end"] == "yielded_user_turn" and it.provider.calls == 1
    assert not (it.ws / "during.md").exists()


def test_new_during_an_active_iteration_signals_then_revokes_and_nothing_more_runs(server):
    """Astra W6b N2 / Opus NB1: ``/new`` while an iteration is inside its model call — the
    signal before the lock, the reset revokes the grant (``revoked(session_reset)``), the
    held call's answer produces no write and no further call, and no press passes."""
    app, client = server.app, server.client
    it = _iteration_in_its_model_call(app, client, server.ws)
    reset = client.post("/api/chat/session/new", headers=_headers(app, "n1"))
    assert reset.status_code == 200, reset.get_json()
    assert it.executor.preempt_signal.seq == it.seq + 1
    grant = client.get(f"/api/grants/{it.grant_id}").get_json()["grant"]
    assert grant["state"] == "revoked", grant
    it.release.set()
    assert it.executor.wait_idle(WAIT)
    view = it.executor.view(it.grant_id)
    assert view["end"] in ("stopped", "yielded_user_turn"), view
    assert it.provider.calls == 1 and view["writes"] == 0 and view["model_calls"] == 1
    assert not (it.ws / "during.md").exists()
    again = client.post(f"/api/grants/{it.grant_id}/step", json={}, headers=SAME)
    assert again.status_code == 409 and it.provider.calls == 1


def _iteration_waiting_on_a_card(server, monkeypatch):
    """An iteration waiting on a person's card (Ask editor)."""
    app, client = server.app, server.client
    executor = app.extensions["grant_executor"]
    broker = app.extensions["hitl_broker"]
    parent = executor._parent()
    monkeypatch.setattr(
        parent.checkpoint_hooks,
        "policy_check",
        lambda name: "ask" if name == "editor" else "always",
    )
    grant_id = _issue(client, server.ws)["grant"]["grant_id"]
    provider = ScriptedProvider(
        [
            [
                use(
                    "editor",
                    {"command": "create", "path": str(server.ws / "c.md"), "file_text": "x"},
                )
            ],
            [{"text": "done"}],
        ]
    )
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
    deadline = time.monotonic() + WAIT
    while not broker.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert broker.pending(), "no card"
    return executor, broker, grant_id, provider


def test_new_withdraws_the_iterations_waiting_card_before_it_resets(server, monkeypatch):
    """W6-H (#631-2): ``/new`` with an ACTIVE iteration — the signal itself, not the
    revoke that follows: the reset is held right after ``/new`` took the turn lock (before
    it revokes anything); the iteration's waiting card is already withdrawn and the
    iteration ends ``yielded_user_turn`` while the grant is still active. Without the
    signal the card would wait until the reset's revoke."""
    app = server.app
    executor, broker, grant_id, provider = _iteration_waiting_on_a_card(server, monkeypatch)
    host = app.extensions["new_session"]
    ready, entered, release = host.audit_ready, threading.Event(), threading.Event()

    def held():
        entered.set()
        assert release.wait(WAIT)
        return ready()

    monkeypatch.setattr(host, "audit_ready", held)
    out = {}
    sender = threading.Thread(
        target=lambda: out.setdefault(
            "reset",
            app.test_client().post("/api/chat/session/new", headers=_headers(app, "n2")),
        ),
        daemon=True,
    )
    sender.start()
    try:
        assert entered.wait(WAIT)
        assert app.extensions["turn_lock"].locked()  # /new holds the lock, has not reset
        assert executor.wait_idle(5), "the card was not withdrawn at /new's signal"
        assert broker.pending() == []
        view = executor.view(grant_id)
        assert view["end"] == "yielded_user_turn" and view["writes"] == 0, view
        assert provider.calls == 1 and not (server.ws / "c.md").exists()
        grant = server.client.get(f"/api/grants/{grant_id}").get_json()["grant"]
        assert grant["state"] == "active", grant  # the revoke has not run yet
    finally:
        release.set()
        sender.join(WAIT)
    assert out["reset"].status_code == 200, out["reset"].get_json()
    grant = server.client.get(f"/api/grants/{grant_id}").get_json()["grant"]
    assert grant["state"] == "revoked", grant


def test_a_webhook_turn_withdraws_the_iterations_waiting_card_at_once(server, monkeypatch):
    """Opus W6b NB1: the signal itself, through a route — not only its counter. An
    iteration waits on a card (Ask editor); a webhook turn arrives and holds the lock: the
    card is withdrawn at the signal and the iteration ends ``yielded_user_turn`` while the
    webhook turn still runs. Without the signal the card would wait on (the lock alone
    does not reach a blocked approval wait)."""
    app = server.app
    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "offline-key")
    executor, broker, grant_id, provider = _iteration_waiting_on_a_card(server, monkeypatch)
    entered, release = _hold_turn(server.model)
    raw = b'{"event":"card"}'
    sig = "sha256=" + hmac.new(b"offline-key", raw, hashlib.sha256).hexdigest()
    sender = threading.Thread(
        target=lambda: app.test_client().post(
            "/api/webhooks/generic",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        ),
        daemon=True,
    )
    sender.start()
    try:
        assert entered.wait(WAIT)
        assert executor.wait_idle(5), "the card was not withdrawn while the turn ran"
        assert app.extensions["turn_lock"].locked()  # the webhook turn still runs
        assert broker.pending() == []
        view = executor.view(grant_id)
        assert view["end"] == "yielded_user_turn" and view["writes"] == 0, view
        assert provider.calls == 1 and not (server.ws / "c.md").exists()
    finally:
        release.set()
        sender.join(WAIT)


def test_with_the_switches_off_the_turn_lock_has_no_signal(tmp_path, monkeypatch):
    build, _data = _build(tmp_path, monkeypatch, {})
    app = build()
    try:
        lock = app.extensions["turn_lock"]
        assert isinstance(lock, TurnLock) and not lock.signalled
        assert "grant_executor" not in app.extensions
        assert lock.acquire(blocking=False)
        try:
            busy = app.test_client().post(
                "/api/chat", json={"message": "hi"}, headers=_headers(app, "off1")
            )
            assert busy.get_data(as_text=True).count('"code": "turn_running"') == 1
        finally:
            lock.release()
    finally:
        _close(app)


# --- the progress report's automatic continue and its reply (ADR 0014) ---------------------


@pytest.fixture
def progress_server(tmp_path, monkeypatch):
    """The progress test app (YOLO window, progress reports, a fake clock) with the
    executor switched on."""
    import json

    from test_progress_report import ClockModel, _on, _open_window, _report_turn

    (tmp_path / "0").mkdir()
    (tmp_path / "0" / "gw.json").write_text(json.dumps(EXEC), encoding="utf-8")
    return SimpleNamespace(
        ClockModel=ClockModel, on=_on, open_window=_open_window, report_turn=_report_turn
    )


def _progress_app(progress_server, make_app, clock):  # noqa: F811
    model = progress_server.ClockModel(clock)
    app = make_app(model, env=progress_server.on())
    assert "grant_executor" in app.extensions
    client = app.test_client()
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    ws = roots[0] / "grant-ws"
    ws.mkdir(parents=True, exist_ok=True)
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    progress_server.open_window(client)
    _frame, report = progress_server.report_turn(app, client, model)
    return app, client, model, ws, grant_id, report


def test_the_automatic_continue_pre_empts_the_running_iteration(
    progress_server,
    make_app,  # noqa: F811 - the imported fixture
    clock,  # noqa: F811
):
    from test_progress_report import MIN

    app, client, model, ws, grant_id, _report = _progress_app(progress_server, make_app, clock)
    it = _iteration_in_its_model_call(app, client, ws, grant_id)
    release = threading.Event()
    model.script = [[{"wait": release}, {"text": "continued on my own"}]]
    tracker = app.extensions["progress"]
    clock.now += 10 * MIN
    try:
        assert tracker.tick() is True  # start_auto took the lock and started the turn
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    _wait_free(app)


def test_a_progress_reply_pre_empts_the_running_iteration(
    progress_server,
    make_app,  # noqa: F811 - the imported fixture
    clock,  # noqa: F811
):
    app, client, model, ws, grant_id, report = _progress_app(progress_server, make_app, clock)
    it = _iteration_in_its_model_call(app, client, ws, grant_id)
    release = threading.Event()
    model.script = [[{"wait": release}, {"text": "carrying on"}]]
    response = client.post(
        "/api/chat",
        json={
            "message": "continue",
            "progress_reply": {"report_id": report["report_id"], "answer": "continue"},
        },
        headers=_headers(app, "reply-1"),
    )
    assert response.status_code == 200
    try:
        deadline = time.monotonic() + WAIT
        while not app.extensions["turn_lock"].locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    response.get_data()
    _wait_free(app)


def test_a_progress_redirect_pre_empts_the_running_iteration(
    progress_server,
    make_app,  # noqa: F811 - the imported fixture
    clock,  # noqa: F811
):
    app, client, model, ws, grant_id, report = _progress_app(progress_server, make_app, clock)
    it = _iteration_in_its_model_call(app, client, ws, grant_id)
    release = threading.Event()
    model.script = [[{"wait": release}, {"text": "new direction taken"}]]
    response = client.post(
        "/api/chat",
        json={
            "message": "Change of direction: tidy b.md first",
            "progress_reply": {"report_id": report["report_id"], "answer": "redirect"},
        },
        headers=_headers(app, "redirect-1"),
    )
    assert response.status_code == 200
    try:
        deadline = time.monotonic() + WAIT
        while not app.extensions["turn_lock"].locked() and time.monotonic() < deadline:
            time.sleep(0.005)
        _yields_while_the_turn_runs(app, client, it)
    finally:
        it.release.set()
        release.set()
    body = response.get_data(as_text=True)
    assert "redirect_too_long" not in body
    _wait_free(app)
