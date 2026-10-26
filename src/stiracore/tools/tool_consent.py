"""Route the upstream ``file_write`` and exec-tool consent to Stira's policy (#242, #362).

The exec class (:data:`EXEC_TOOLS`) is gated the same way since #362: its wrapper is
``exec_gate.ExecGatedTool``, its gate mark needs a human approval of the exact call
(:data:`HUMAN_APPROVAL_TOOLS`), and the ``shell``/``cron`` terminal prompts are answered
only inside that call's grant. Modules without a prompt are checked not to reach the
terminal prompt or the real environment by another name.

``editor`` is Stira's own since #245 (no terminal prompt), but still runs only
with a gate mark and inside a grant (:data:`GATED_TOOLS`); its sandbox reads the
grant's checked path and policies (:func:`active_grant`).

strands-agents-tools asks for ``[y/*]`` on the terminal before ``file_write`` and
the modifying (upstream) ``editor`` commands unless the process environment has
``BYPASS_TOOL_CONSENT=true``. Under a headless start (stdin is ``/dev/null``)
the prompt is cancelled at once with an empty reason; with stdin left open it
blocks until someone types. Setting the variable process-wide is not a fix
either: any caller would then pass, including paths Stira never approved (#167).

Here the decision belongs to Stira alone, in three steps:

1. **Gate mark.** Stira's last ``BeforeToolCall`` hook (``ApprovalBindingVerifier``,
   after breaker/HITL/identity checks) issues a single-use :class:`GateMark` for a
   ``file_write``/``editor`` call: the tool name, ``toolUseId``, argument hash and
   a random nonce, stored in the SDK's ``invocation_state`` (the same dict the SDK
   hands to ``tool.stream``). ``IdentityProtectedTool`` consumes it with
   :func:`claim_gate` (``dict.pop``), so a second run of the same call, a call the
   hooks never saw (a direct ``stream()`` call, an inner agent without Stira
   hooks, #145) or a hand-built tuple is refused. The arguments run from a
   snapshot whose hash must equal the gate's. Code that can rewrite
   ``invocation_state`` itself is trusted in-process code; this is not a sandbox.
2. **Bridge.** In each upstream module, ``get_user_input`` is replaced by a
   function that never reads the terminal, and ``os`` by a stand-in whose
   ``environ`` answers ``BYPASS_TOOL_CONSENT`` only while a grant for *that* tool
   is active in the current context. The real process environment is ignored.
   :func:`bridge_refusal` re-checks both replacements before every run (by the
   ``stira_consent_bridge`` attribute and the proxy type; the marked function's
   body is trusted, not inspected). If the bridge cannot be confirmed, the tool is
   refused, and ``tool_registry`` does not register it at all. Using the raw
   upstream modules without Stira's registry is outside these guarantees.
3. **Grant.** :func:`consent_granted` holds a grant in a ``ContextVar`` only while
   one step of the trusted upstream stream runs (``IdentityProtectedTool`` enters
   it around each ``__anext__``, not across ``yield``). ``asyncio.to_thread``
   copies the context into the SDK worker thread; executors that do not copy it
   see no grant and fail closed with :class:`ToolConsentError`.
"""

from __future__ import annotations

import functools
import inspect
import os
import secrets
import sys
import threading
import types
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

BYPASS_ENV_VAR = "BYPASS_TOOL_CONSENT"
# ``shell`` also skips its prompt (and its terminal handling) when this is "true".
NON_INTERACTIVE_ENV_VAR = "STRANDS_NON_INTERACTIVE"
# The exec class (#163): exposed only with STIRA_ENABLE_EXEC_TOOLS; every call asks a
# human (ADR 0010 §3, never Always). ``tool_registry.EXEC_TOOL_NAMES`` is this set.
EXEC_TOOLS = frozenset({"shell", "batch", "cron", "use_agent", "swarm", "graph", "think"})
# Upstream tool name -> its module, where the bridge routes consent to Stira. ``editor``
# is no longer the upstream module: it is Stira's sandbox-routed file editor (#245),
# which has no terminal prompt to route. The exec tools are here since #362.
# ``http_request`` since #547: its terminal ``[y/*]`` for a modifying method.
CONSENT_TOOL_MODULES = {
    "file_write": "strands_tools.file_write",
    "http_request": "strands_tools.http_request",
    **{name: f"strands_tools.{name}" for name in sorted(EXEC_TOOLS)},
}
# Modules that must have a terminal prompt (``get_user_input``) to replace; a module
# without one anywhere else must not grow one unbridged (it is patched, or refused).
PROMPT_TOOLS = frozenset({"file_write", "http_request", "shell", "cron"})
# #547: the upstream ``http_request`` asks on the terminal before these methods (compared
# as the upstream does, ``method.upper()``). Such a call runs only with a gate mark that
# a person's approval of this exact call opened; other methods ask nothing and need none.
HTTP_REQUEST_TOOL = "http_request"
MODIFYING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Modules whose ``open`` is replaced by the checked write opener (#271).
OPEN_TOOLS = frozenset({"file_write"})
# Environment names the scoped view answers "true" only inside that tool's grant (and
# never from the real environment); every other name is the real one.
_CONSENT_KEYS = {"shell": frozenset({BYPASS_ENV_VAR, NON_INTERACTIVE_ENV_VAR})}
_DEFAULT_CONSENT_KEYS = frozenset({BYPASS_ENV_VAR})
# strands-agents-tools releases whose exec modules were audited for the bridge (#362):
# every route to the terminal prompt or the process environment in those modules is
# the module-level ``get_user_input`` / ``os`` that the bridge replaces (the source
# audit is ``tests/test_exec_tool_consent.py::test_pinned_upstream_source_audit``).
# Another release is refused for the exec class (not registered) until it is audited.
UPSTREAM_DISTRIBUTION = "strands-agents-tools"
VERIFIED_EXEC_UPSTREAM = frozenset({"0.8.9"})
# Tools that run only with a single-use gate mark (#242), whether or not they
# have an upstream consent prompt.
GATED_TOOLS = frozenset({"file_write", "editor"}) | EXEC_TOOLS
# Tools whose gate mark needs a human approval of this exact call (a verified HITL
# binding), whatever the rule reader answered (#362): an exec call is never automatic.
HUMAN_APPROVAL_TOOLS = EXEC_TOOLS


def modifying_http_call(tool_use: Any) -> bool:
    """An ``http_request`` call whose method the upstream tool confirms on the terminal."""
    if not isinstance(tool_use, Mapping) or tool_use.get("name") != HTTP_REQUEST_TOOL:
        return False
    arguments = tool_use.get("input")
    method = arguments.get("method") if isinstance(arguments, Mapping) else None
    return isinstance(method, str) and method.upper() in MODIFYING_HTTP_METHODS


def needs_gate_mark(tool_use: Any) -> bool:
    """This call runs only with a single-use gate mark (:data:`GATED_TOOLS`, #547)."""
    name = tool_use.get("name") if isinstance(tool_use, Mapping) else None
    return name in GATED_TOOLS or modifying_http_call(tool_use)


def needs_person(tool_use: Any) -> bool:
    """This call's gate mark needs a person's approval of it (exec class, #547 POST)."""
    name = tool_use.get("name") if isinstance(tool_use, Mapping) else None
    return name in HUMAN_APPROVAL_TOOLS or modifying_http_call(tool_use)


# invocation_state key: {toolUseId: (tool_name, args_hash)} issued by Stira's gate.
GATE_PASSED_KEY = "stira.tool_gate.passed"
# invocation_state key: {id(tool_use): (tool_use, GateMark)}, the call each mark
# was issued to (ApprovalBindingVerifier, #255).
GATE_ISSUED_KEY = "stira.tool_gate.issued"


class ToolConsentError(RuntimeError):
    """An upstream tool asked for confirmation that Stira's policy did not grant."""


@dataclass
class _Grant:
    tool_name: str
    mark: Any = None
    active: bool = True
    # The one path this approved step may write and every wrapper's policy on
    # the way down (outermost first); read by Stira's editor sandbox (#245).
    write_path: str | None = None
    policies: tuple = ()
    # (st_dev, st_ino) of every file this grant opened for writing; read by the
    # wrapper's observation to tell whether the path is still that file (#307).
    opened: list | None = None
    # An exec tool's upstream entry point ran under this grant (single entry, #362):
    # a child agent inside the approved step cannot re-enter the same tool with it.
    entered: bool = False


_GRANT: ContextVar[_Grant | None] = ContextVar("stira_tool_consent_grant", default=None)
_INSTALL_LOCK = threading.Lock()
_ENTRY_LOCK = threading.Lock()


def _granted(tool_name: str) -> bool:
    grant = _GRANT.get()
    return grant is not None and grant.active and grant.tool_name == tool_name


def active_grant(tool_name: str) -> _Grant | None:
    """The grant of the approved ``tool_name`` step running now, if any."""
    grant = _GRANT.get()
    if grant is not None and grant.active and grant.tool_name == tool_name:
        return grant
    return None


def held_mark(tool_name: str) -> Any:
    """The claimed mark of the approved step running now (for a nested wrapper), if any."""
    grant = _GRANT.get()
    if grant is not None and grant.active and grant.tool_name == tool_name:
        return grant.mark
    return None


# --- 1. Gate mark --------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class GateMark:
    """Single-use approval of one call, issued by Stira's gate (compared by identity)."""

    tool_name: str
    tool_use_id: str
    digest: str
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))


def input_digest(tool_input: Any) -> str | None:
    from stiracore.core.approval import args_hash

    try:
        return args_hash(tool_input)
    except (TypeError, ValueError):
        return None


def _marks(invocation_state: Any) -> dict | None:
    marks = invocation_state.get(GATE_PASSED_KEY) if isinstance(invocation_state, dict) else None
    return marks if isinstance(marks, dict) else None


def mark_gate_passed(invocation_state: dict, tool_use: Mapping) -> GateMark | None:
    """Issue a single-use mark for this exact call; only a policy gate may call this.

    ``ApprovalBindingVerifier`` does in production; tests acting as the gate do too.
    Returns None (nothing issued) for an empty ``toolUseId``, input that cannot be
    hashed, or when an unclaimed mark for the same id is already in flight.
    """
    tool_use_id = tool_use.get("toolUseId")
    name = tool_use.get("name")
    digest = input_digest(tool_use.get("input", {}))
    if not isinstance(tool_use_id, str) or not tool_use_id or not isinstance(name, str):
        return None
    if digest is None:
        return None
    marks = invocation_state.setdefault(GATE_PASSED_KEY, {})
    mark = GateMark(name, tool_use_id, digest)
    if marks.setdefault(tool_use_id, mark) is not mark:
        return None
    return mark


def revoke_gate_mark(invocation_state: dict, tool_use_id: Any) -> None:
    """Drop any unclaimed mark for this id (the call was cancelled or not marked)."""
    marks = _marks(invocation_state)
    if marks is not None:
        marks.pop(tool_use_id, None)


def release_gate_mark(invocation_state: dict, mark: GateMark) -> None:
    """Drop ``mark`` if it is still unclaimed; never a mark issued for another call."""
    marks = _marks(invocation_state)
    if marks is not None and marks.get(mark.tool_use_id) is mark:
        marks.pop(mark.tool_use_id, None)


def _gate_refusal(tool_name: str, reason: str) -> str:
    return (
        f"{tool_name} was not run: {reason}. Write and exec tools run only after Stira's "
        "tool hooks (breaker, HITL, identity guard) approve the exact call, once (an exec "
        "tool: a human approval of that call); a direct call, a replay, or an inner agent "
        "without those hooks is refused."
    )


def claim_gate(
    tool_name: str, invocation_state: Any, tool_use_id: Any, tool_input: Any
) -> GateMark | str:
    """Consume (``dict.pop``) the gate's mark for this call; return it, or why not.

    ``tool_input`` is the snapshot that will run; it must hash to what the gate saw.
    """
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return _gate_refusal(tool_name, "the call has no toolUseId")
    marks = _marks(invocation_state)
    mark = marks.pop(tool_use_id, None) if marks is not None else None
    if not isinstance(mark, GateMark):
        return _gate_refusal(tool_name, "no Stira gate approval for this call")
    if mark.tool_name != tool_name or mark.tool_use_id != tool_use_id:
        return _gate_refusal(tool_name, "the gate approved a different call")
    digest = input_digest(tool_input)
    if digest is None or digest != mark.digest:
        return _gate_refusal(tool_name, "arguments changed after Stira's gate")
    return mark


# --- 2. Bridge -----------------------------------------------------------------


def _stdin_state() -> str:
    try:
        return "a terminal" if os.isatty(0) else "not a terminal"
    except OSError:
        return "closed"


def consent_error_message(tool_name: str) -> str:
    return (
        f"{tool_name} was not run: the upstream tool requested terminal confirmation "
        "outside Stira's approval gate (or the approval context did not reach the worker "
        f"thread). Stira never reads terminal input for tool consent (stdin is "
        f"{_stdin_state()}); approval comes only from Stira's tool policy and HITL, and "
        "the BYPASS_TOOL_CONSENT environment variable is ignored. Use the tool "
        "registered by stiracore.tools.tool_registry."
    )


class _ScopedConsentEnviron(MutableMapping):
    """``os.environ`` as seen by one upstream module: only the consent keys are replaced."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._keys = _CONSENT_KEYS.get(tool_name, _DEFAULT_CONSENT_KEYS)

    def __getitem__(self, key: str) -> str:
        if key in self._keys:
            if _granted(self._tool_name):
                return "true"
            raise KeyError(key)
        return os.environ[key]

    def __setitem__(self, key: str, value: str) -> None:
        if key in self._keys:
            raise ToolConsentError(f"{key} cannot be set from {self._tool_name}")
        os.environ[key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._keys:
            raise KeyError(key)
        del os.environ[key]

    def __iter__(self) -> Iterator[str]:
        names = [name for name in os.environ if name not in self._keys]
        if _granted(self._tool_name):
            names.extend(sorted(self._keys))
        return iter(names)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _ScopedConsentOs(types.ModuleType):
    """Stand-in for ``os`` inside one upstream module; everything but ``environ`` is real."""

    def __init__(self, tool_name: str) -> None:
        super().__init__("os")
        self.environ = _ScopedConsentEnviron(tool_name)

    def getenv(self, key: str, default: Any = None) -> Any:
        return self.environ.get(key, default)

    # #457: a program this module starts by ``exec*`` without an explicit environment
    # (``shell``: ``os.execvp("/bin/sh", ...)`` in the PTY child) gets the allowlisted
    # environment, never the server's keys and tokens (``child_env``).
    def execvp(self, file: Any, args: Any) -> Any:
        from .child_env import child_environment

        return os.execvpe(file, args, child_environment())

    def execv(self, path: Any, args: Any) -> Any:
        from .child_env import child_environment

        return os.execve(path, args, child_environment())

    def execlp(self, file: Any, *args: Any) -> Any:
        return self.execvp(file, args)

    def execl(self, path: Any, *args: Any) -> Any:
        return self.execv(path, args)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _terminal_prompt_replacement(tool_name: str):
    def get_user_input(
        prompt: str, default: str = "", keyboard_interrupt_return_default: bool = True
    ) -> str:
        """Never read the terminal; Stira's policy has already decided."""
        if _granted(tool_name):
            return "y"
        raise ToolConsentError(consent_error_message(tool_name))

    get_user_input.stira_consent_bridge = tool_name
    return get_user_input


def _guarded_open(tool_name: str):
    """The upstream module's ``open``: writes only the granted path, checked again.

    The upstream ``file_write`` opens with plain ``open(path, "w")``, which follows
    symlinks and truncates first. Here a write opens through ``open_checked``
    (no final symlink, the policy re-asked after the open, the same inode, one
    link, truncate last; #271); any other write mode, or no grant, is refused.
    """
    import builtins

    from .checked_open import open_checked

    def guarded(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if not any(flag in mode for flag in "wax+"):
            return builtins.open(file, mode, *args, **kwargs)
        grant = active_grant(tool_name)
        if grant is None or grant.write_path is None or mode not in ("w", "wt"):
            raise ToolConsentError(consent_error_message(tool_name))
        try:
            path = os.fspath(file)
        except TypeError as error:
            raise ToolConsentError(consent_error_message(tool_name)) from error
        descriptor = open_checked(path, grant.policies, grant.write_path)
        if grant.opened is not None:
            try:
                info = os.fstat(descriptor)
            except OSError:
                pass
            else:
                grant.opened.append((info.st_dev, info.st_ino))
        return os.fdopen(descriptor, mode, *args, **kwargs)

    guarded.stira_consent_bridge = tool_name
    return guarded


def upstream_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(UPSTREAM_DISTRIBUTION)
    except PackageNotFoundError:
        return None


def _entry_refusal(tool_name: str) -> str:
    return (
        f"{tool_name} was not run: it was called outside an approved step of Stira's gate "
        "(for example from a child agent of think/use_agent/swarm/graph, or by code that "
        "loaded the upstream tool itself). Exec tools run only inside a human approval of "
        "that exact call, and a child agent has no approval route (#362)."
    )


def _entry_guard(tool_name: str, func: Any):
    """The upstream exec function, runnable only inside a grant for ``tool_name``."""

    @functools.wraps(func)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        grant = active_grant(tool_name)
        with _ENTRY_LOCK:
            # One approved call, one entry: a nested call of the same tool inside it
            # (a child agent that loaded the upstream tool itself) is refused.
            usable = grant is not None and not grant.entered
            if usable:
                grant.entered = True
        if not usable:
            raise ToolConsentError(_entry_refusal(tool_name))
        return func(*args, **kwargs)

    guarded.stira_consent_bridge = tool_name
    return guarded


def _entry(module: Any, tool_name: str) -> tuple[Any, str] | None:
    """(holder, attribute) of the function an exec module runs, or None if unknown.

    ``@tool`` modules hold a ``DecoratedFunctionTool`` whose ``_tool_func`` runs; a
    ``TOOL_SPEC`` module (``batch``) runs its module-level function.
    """
    value = module.__dict__.get(tool_name)
    if hasattr(value, "_tool_func") and callable(value._tool_func):
        return value, "_tool_func"
    if callable(value) and isinstance(getattr(module, "TOOL_SPEC", None), Mapping):
        return module, tool_name
    return None


def _entry_ours(module: Any, tool_name: str) -> bool:
    entry = _entry(module, tool_name)
    if entry is None:
        return False
    holder, attribute = entry
    return getattr(getattr(holder, attribute), "stira_consent_bridge", None) == tool_name


def _install_entry(module: Any, tool_name: str) -> str | None:
    module_name = module.__name__
    entry = _entry(module, tool_name)
    if entry is None:
        return f"{module_name}.{tool_name} has no known entry point"
    holder, attribute = entry
    func = getattr(holder, attribute)
    marker = getattr(func, "stira_consent_bridge", None)
    if marker == tool_name:
        return None
    if marker is not None:
        return f"{module_name}.{tool_name} carries another tool's bridge"
    if inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func):
        return f"{module_name}.{tool_name} is asynchronous (not audited)"
    setattr(holder, attribute, _entry_guard(tool_name, func))
    return None


def _os_proxy_ours(proxy: Any, tool_name: str) -> bool:
    return (
        isinstance(proxy, _ScopedConsentOs)
        and isinstance(proxy.environ, _ScopedConsentEnviron)
        and proxy.environ._tool_name == tool_name
    )


def _bridge_live(module: Any, tool_name: str) -> bool:
    prompt = module.__dict__.get("get_user_input")
    proxy = module.__dict__.get("os")
    if (tool_name in PROMPT_TOOLS or prompt is not None) and getattr(
        prompt, "stira_consent_bridge", None
    ) != tool_name:
        return False
    if (tool_name in PROMPT_TOOLS or proxy is not None) and not _os_proxy_ours(proxy, tool_name):
        return False
    if tool_name in OPEN_TOOLS and (
        getattr(module.__dict__.get("open"), "stira_consent_bridge", None) != tool_name
    ):
        return False
    if tool_name in EXEC_TOOLS and not _entry_ours(module, tool_name):
        return False
    return _unrouted_path(module) is None


def _unrouted_path(module: Any) -> str | None:
    """A module global that reaches the terminal prompt or the real environment unbridged.

    ``from os import environ``, ``import os as _os`` or the prompt module itself would
    bypass the scoped view: such a module is refused (fail closed, #362).

    Scope: this compares the module's *globals* by identity. It does not see a relay
    (``env_get = os.environ.get``, an object holding ``os``), an import inside a
    function, or what other libraries the tool calls do. For the exec class those are
    covered by the release pin (:data:`VERIFIED_EXEC_UPSTREAM`, audited source) and
    the entry guard; for ``file_write`` this check and the existing #242 tests are all.
    """
    try:
        from strands_tools.utils import user_input
    except ImportError:  # the upstream prompt module itself is gone: nothing to reach
        user_input = None
    prompts = (
        ()
        if user_input is None
        else (user_input, user_input.get_user_input, user_input.get_user_input_async)
    )
    real_environment = [os, os.environ, os.getenv]
    if hasattr(os, "environb"):  # POSIX only
        real_environment.append(os.environb)
    for name, value in list(module.__dict__.items()):
        if name == "__builtins__":
            continue
        if any(value is real for real in real_environment):
            return f"{module.__name__}.{name} reaches the real process environment"
        if any(value is prompt for prompt in prompts):
            return f"{module.__name__}.{name} is an unbridged terminal prompt"
    return None


def _install_one(tool_name: str) -> str | None:
    """Patch one upstream module if it has the known shape; return why not otherwise."""
    module_name = CONSENT_TOOL_MODULES[tool_name]
    try:
        module = sys.modules.get(module_name) or import_module(module_name)
    except ImportError as error:
        return f"{module_name} cannot be imported ({error})"
    if tool_name in EXEC_TOOLS:
        installed = upstream_version()
        if installed not in VERIFIED_EXEC_UPSTREAM:
            return (
                f"{UPSTREAM_DISTRIBUTION} {installed} is not a release the exec bridge was "
                f"audited against ({', '.join(sorted(VERIFIED_EXEC_UPSTREAM))})"
            )
    if _bridge_live(module, tool_name):
        return None
    # Patch only what is still the upstream shape: a callable get_user_input
    # without the bridge attribute (e.g. after a reload) and the real os module.
    # A function carrying the attribute for this tool is trusted as ours without
    # inspecting it; anything else (missing, the other tool's bridge, a foreign
    # os) cannot be verified and stays refused. A module without a prompt (or
    # without ``os``) has nothing to patch there, unless it must have one.
    prompt = module.__dict__.get("get_user_input")
    proxy = module.__dict__.get("os")
    needs_prompt = tool_name in PROMPT_TOOLS or prompt is not None
    prompt_ours = getattr(prompt, "stira_consent_bridge", None) == tool_name
    if (
        needs_prompt
        and not prompt_ours
        and (not callable(prompt) or hasattr(prompt, "stira_consent_bridge"))
    ):
        return f"{module_name}.get_user_input is missing or replaced"
    needs_os = tool_name in PROMPT_TOOLS or proxy is not None
    os_ours = _os_proxy_ours(proxy, tool_name)
    if needs_os and proxy is not os and not os_ours:
        return f"{module_name}.os is replaced"
    opener = module.__dict__.get("open")
    open_ours = getattr(opener, "stira_consent_bridge", None) == tool_name
    if tool_name in OPEN_TOOLS and opener is not None and not open_ours:
        return f"{module_name}.open is replaced"
    if needs_prompt and not prompt_ours:
        module.get_user_input = _terminal_prompt_replacement(tool_name)
    if needs_os and not os_ours:
        module.os = _ScopedConsentOs(tool_name)
    if tool_name in OPEN_TOOLS and not open_ours:
        module.open = _guarded_open(tool_name)
    if tool_name in EXEC_TOOLS:
        problem = _install_entry(module, tool_name)
        if problem is not None:
            return problem
    unrouted = _unrouted_path(module)
    if unrouted is not None:
        return unrouted
    return None if _bridge_live(module, tool_name) else f"{module_name} patch did not hold"


def install_consent_bridge() -> set[str]:
    """Route the upstream consent prompts to Stira where possible; return the covered tools."""
    with _INSTALL_LOCK:
        return {name for name in CONSENT_TOOL_MODULES if _install_one(name) is None}


def bridge_refusal(tool_name: str) -> str | None:
    """None if ``tool_name``'s upstream prompt is verifiably routed to Stira (re-checked now)."""
    if tool_name not in CONSENT_TOOL_MODULES:
        return None
    with _INSTALL_LOCK:
        reason = _install_one(tool_name)
    if reason is None:
        return None
    return (
        f"{tool_name} was not run: Stira cannot route the upstream terminal confirmation "
        f"to its approval policy ({reason}). Refusing rather than asking on the terminal "
        "or honouring BYPASS_TOOL_CONSENT."
    )


# --- 3. Grant ------------------------------------------------------------------


# ADR 0025 §3: a class policy bound to one gate mark (by its nonce) — a write that a
# session allowance authorized opens its file with the class as one more policy. Memory
# only; an entry ends with the mark's call (``release_mark_policy``) or is replaced.
_MARK_POLICIES: dict[str, Any] = {}
_MARK_POLICIES_LOCK = threading.Lock()
_MARK_POLICIES_KEPT = 256


def bind_mark_policy(mark: Any, policy: Any) -> None:
    nonce = getattr(mark, "nonce", None)
    if type(nonce) is not str:
        return
    with _MARK_POLICIES_LOCK:
        _MARK_POLICIES[nonce] = policy
        while len(_MARK_POLICIES) > _MARK_POLICIES_KEPT:
            _MARK_POLICIES.pop(next(iter(_MARK_POLICIES)))


def release_mark_policy(mark: Any) -> None:
    with _MARK_POLICIES_LOCK:
        _MARK_POLICIES.pop(getattr(mark, "nonce", None), None)


def _mark_policy(mark: Any) -> Any:
    nonce = getattr(mark, "nonce", None)
    if type(nonce) is not str:
        return None
    with _MARK_POLICIES_LOCK:
        return _MARK_POLICIES.get(nonce)


@contextmanager
def consent_granted(
    tool_name: str,
    mark: GateMark | None = None,
    *,
    write_path: str | None = None,
    policy: Any = None,
    opened: list | None = None,
) -> Iterator[None]:
    """Let the upstream ``tool_name`` run without its terminal prompt, in this context only.

    Call only after Stira's gate approved this exact call, and hold it only while
    the upstream code runs (not across a ``yield`` to the caller). ``write_path``
    and ``policy`` are the target the wrapper checked and the policy it used; a
    nested grant keeps the outer wrappers' policies too. ``opened`` collects the
    ``(st_dev, st_ino)`` of files opened for writing under this grant (#307).
    """
    refusal = bridge_refusal(tool_name)
    if refusal is not None:
        raise ToolConsentError(refusal)
    outer = active_grant(tool_name)
    policies = (outer.policies if outer is not None else ()) + (
        (policy,) if policy is not None else ()
    )
    bound = _mark_policy(mark)
    if bound is not None and bound not in policies:
        policies = (*policies, bound)
    grant = _Grant(tool_name, mark, write_path=write_path, policies=policies, opened=opened)
    token = _GRANT.set(grant)
    try:
        yield
    finally:
        # Deactivate first: a task or thread that copied this context keeps the
        # object, but a stale grant no longer opens anything.
        grant.active = False
        try:
            _GRANT.reset(token)
        except ValueError:
            pass
