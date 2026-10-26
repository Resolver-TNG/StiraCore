"""Always / Ask / Never tool policy and the effective gate stack (#190, ADR 0010).

Pure logic, no Flask. Three layers decide what a tool call may do:

1. **rule** - the operator's per-tool policy (Always / Ask / Never, ``ToolConfig``);
2. **breaker** - the live circuit-breaker level;
3. **floor** - protections no setting or approval lifts (exec exposure gate #163,
   SOUL/identity read-only paths, OS-protection commands #244).

Every layer returns ``allow`` < ``prompt`` < ``forbidden`` and the effective decision
is the strictest one (monotonic: a lower layer's ``allow`` never overrides a higher
layer's ``forbidden``). The runtime enforces each layer on its own (tool filtering,
``CheckpointHooks``, ``IdentityGuard``); this module describes the same decisions
for the settings hub so the UI can say *which* layer decided.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from stiracore.identity_paths import DEFAULT_READ_ONLY_PATHS

from . import tool_registry as registry
from .os_protection import is_command_tool

ALWAYS, ASK, NEVER = "always", "ask", "never"
POLICIES = (ALWAYS, ASK, NEVER)
ALLOW, PROMPT, FORBIDDEN = "allow", "prompt", "forbidden"
RANK = {ALLOW: 0, PROMPT: 1, FORBIDDEN: 2}
RULE_DECISION = {ALWAYS: ALLOW, ASK: PROMPT, NEVER: FORBIDDEN}
POLICY_RANK = {ALWAYS: 0, ASK: 1, NEVER: 2}
LAYERS = ("rule", "breaker", "floor")

# Breaker levels under which nothing restricts tools at the breaker layer. "none" is a
# server without a live engine (the observer dashboard). Anything else unrecognised
# (including "unknown", a failed read) fails closed.
_OPEN_LEVELS = frozenset({"normal", "coherence", "none"})
_ALL_BLOCKED = frozenset({"critical", "emergency", "halt", "halted"})
_WRITE_BLOCKED = frozenset({"warning", "block_writes", "require_confirmation"})
# Levels under which no rule may be loosened (ADR 0010 §4).
LOOSENING_LEVELS = frozenset({"normal", "none"})
# Stira's path-confined file tools (#243/#260): writes go through WritePathPolicy.
PATH_CONFINED_TOOLS = frozenset({"file_write", "editor"})
REVOKE_TARGET = "/settings#sec-tools"
# Tools whose effect leaves this machine (requests/notifications to other hosts).
EXTERNAL_SEND_TOOLS = frozenset({"http_request", "webhook_send", "oauth_api_call"})
# Not exec class, but Ask or Never only (ADR 0015 phase 1, #449): ``page_check`` runs
# JavaScript the agent wrote — possibly under prompt injection — and an Always rule would
# also run it in turns no person watches (jobs, webhooks, bots). A saved Always is read as
# Ask, with one warning per process.
# ``propose_procedure`` (#463): Ask or Never only too — an Always rule would let turns no
# person watches (jobs, webhooks, bots) stage rules for the review queue unseen.
NO_ALWAYS_TOOLS = frozenset({"page_check", "propose_procedure"})

# ADR 0019 Q3 (#167): a person may save Always for ``run_checks``, but it is **effective**
# only while this host's containment is verified ``os_sandbox`` (``run_checks.
# always_effective``); elsewhere the gate reads it as Ask (``rule_always_capped``) and the
# settings page shows the saved Always with that reason.
CONDITIONAL_ALWAYS_TOOLS = frozenset({"run_checks"})

_LOG = logging.getLogger(__name__)
_WARNED_ALWAYS: set[str] = set()


def always_capped(name: str) -> bool:
    """Whether a saved Always for ``name`` is read as Ask right now (Q3 of ADR 0019)."""
    if name not in CONDITIONAL_ALWAYS_TOOLS:
        return False
    from .run_checks import always_effective

    try:
        return not always_effective()
    except Exception:  # noqa: BLE001 - a guard that cannot be read caps (fail closed)
        return True


def level_name(level: Any) -> str:
    value = getattr(level, "name", None)
    return (
        str(value if value is not None else getattr(level, "value", level))
        .rsplit(".", 1)[-1]
        .strip()
        .lower()
    )


def category_of(name: str) -> str:
    if registry.is_exec_tool(name):
        return "exec"
    return "write" if name in registry.WRITE_TOOLS else "read"


def always_allowed(name: str) -> bool:
    """Whether an Always (persistent, no per-call approval) grant can be stated.

    Exec-class tools run arbitrary commands or tool-using agents: the scope of such a
    grant cannot be bounded, so they can be Ask or Never only (#163, ADR 0010 §5). The
    same holds for :data:`NO_ALWAYS_TOOLS` (``page_check``, ADR 0015 phase 1).
    """
    return not registry.is_exec_tool(name) and name not in NO_ALWAYS_TOOLS


def required_ask(enabled: Iterable[str], ask: Iterable[str], mode: str = "custom") -> set[str]:
    """``ask`` plus every :data:`NO_ALWAYS_TOOLS` name that is on: what a save writes, so a
    saved file never says Always for them (the runtime reads one as Ask anyway)."""
    on = set(enabled)
    return set(ask) | {n for n in NO_ALWAYS_TOOLS if mode == "trust_all" or n in on}


def warn_saved_always(tool_config: Any) -> list[str]:
    """Names in :data:`NO_ALWAYS_TOOLS` whose saved rule says Always (read as Ask).

    Logs one warning per tool per process; called at start-up and by :func:`rule_policy`.
    """
    enabled = set(getattr(tool_config, "enabled_tools", ()) or ())
    trust_all = getattr(tool_config, "mode", "custom") == "trust_all"
    ask = set(getattr(tool_config, "ask_tools", ()) or ())
    found = sorted(n for n in NO_ALWAYS_TOOLS if (trust_all or n in enabled) and n not in ask)
    for name in found:
        if name not in _WARNED_ALWAYS:
            _WARNED_ALWAYS.add(name)
            _LOG.warning(
                "The saved rule for %s is Always; it is treated as Ask (Ask or Never only: "
                "ADR 0015 phase 1, #463). Choose Ask or Never in the settings to silence this.",
                name,
            )
    return found


def rule_policy(tool_config: Any, name: str, *, effective: bool = True) -> str:
    """The rule for ``name``, derived from ``ToolConfig`` (migration, ADR 0010 §3).

    OFF (not enabled in ``custom`` mode) is Never; ON is Always unless the tool is in
    ``ask_tools`` or cannot be Always (exec class), in which case it is Ask.
    ``effective`` (the gate's view, default): a saved Always of a
    :data:`CONDITIONAL_ALWAYS_TOOLS` name is Ask while its guard does not hold;
    ``effective=False`` is the rule as saved (loosening checks, the settings page).
    """
    enabled = getattr(tool_config, "mode", "custom") == "trust_all" or name in set(
        getattr(tool_config, "enabled_tools", ()) or ()
    )
    if not enabled:
        return NEVER
    if name in set(getattr(tool_config, "ask_tools", ()) or ()) or not always_allowed(name):
        if name in NO_ALWAYS_TOOLS:
            warn_saved_always(tool_config)
        return ASK
    if effective and always_capped(name):
        return ASK
    return ALWAYS


def policies_from_values(tools: Mapping, names: Iterable[str]) -> dict[str, str]:
    """``rule_policy`` for the hub's ``tools`` value group (a plain dict)."""

    view = SimpleNamespace(
        mode=tools.get("mode", "custom"),
        enabled_tools=tools.get("enabled_tools") or (),
        ask_tools=tools.get("ask_tools") or (),
    )
    return {name: rule_policy(view, name, effective=False) for name in names}


def loosened(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Names whose rule became less strict (Never->Ask/Always, Ask->Always)."""
    return sorted(
        name
        for name, policy in after.items()
        if POLICY_RANK.get(policy, 2) < POLICY_RANK.get(before.get(name, NEVER), 2)
    )


def newly_always_writes(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Write/external tools the change makes Always: the operator types their names."""
    return sorted(
        name
        for name, policy in after.items()
        if policy == ALWAYS and before.get(name) != ALWAYS and category_of(name) != "read"
    )


def rule_layer(policy: str) -> dict:
    return {"decision": RULE_DECISION.get(policy, FORBIDDEN), "reason": f"rule_{policy}"}


def breaker_layer(level: Any, category: str, critical_action: str | None = None) -> dict:
    """The breaker's contribution, as the runtime really enforces it.

    WARNING: write/exec are hidden (stricter than Ask). CRITICAL with
    ``critical_action="halt"``: every write/exec call needs a human approval, whatever
    the rule (``CheckpointHooks``); ``"readonly"``: write/exec are hidden. Reads stay
    available at CRITICAL (ADR 0002). Unknown action or level: everything stopped.
    """
    name = level_name(level)
    if name == "critical" and critical_action in ("halt", "readonly"):
        if category == "read":
            return {"decision": ALLOW, "reason": "breaker_critical_read"}
        if critical_action == "halt":
            return {"decision": PROMPT, "reason": "breaker_critical_halt"}
        return {"decision": FORBIDDEN, "reason": "breaker_critical_readonly"}
    if name in _ALL_BLOCKED:
        return {"decision": FORBIDDEN, "reason": "breaker_critical"}
    if name in _WRITE_BLOCKED:
        if category == "read":
            return {"decision": ALLOW, "reason": "breaker_warning_read"}
        # WARNING requires confirmation for writes; with no confirmation route at that
        # level the runtime hides them (NXPAgent._turn_tools) - stricter than Ask.
        return {"decision": FORBIDDEN, "reason": "breaker_warning"}
    if name in _OPEN_LEVELS:
        return {"decision": ALLOW, "reason": f"breaker_{name}"}
    return {"decision": FORBIDDEN, "reason": "breaker_unknown"}


def floor_layer(name: str, *, exposed: bool) -> dict:
    """Protections no setting or approval lifts; ``constraints`` apply per argument."""
    category = category_of(name)
    constraints = []
    if category != "read":
        constraints.append("identity_read_only")
    if name in PATH_CONFINED_TOOLS:
        constraints.append("write_roots")
    if is_command_tool(name):
        constraints.append("os_protection")
    if not exposed:
        return {"decision": FORBIDDEN, "reason": "exec_not_exposed", "constraints": constraints}
    reason = "floor_constraints" if constraints else "floor_none"
    return {"decision": ALLOW, "reason": reason, "constraints": constraints}


def effective(layers: Mapping[str, Mapping]) -> dict:
    """Strictest decision of the layers and every layer that reached it."""
    decision = max((layer["decision"] for layer in layers.values()), key=RANK.__getitem__)
    decided_by = [key for key in LAYERS if key in layers and layers[key]["decision"] == decision]
    return {
        "decision": decision,
        "decided_by": decided_by,
        "reasons": [layers[key]["reason"] for key in decided_by],
    }


# #642 (W6-F): the tools that run in the host's OS sandbox when it is verified.
OS_SANDBOX_TOOLS = frozenset({"run_checks"})


def isolation(name: str) -> dict:
    """Whether the tool runs isolated. Independent of approval: approved != isolated.

    ``run_checks`` (#642): ``os_sandbox`` while the tool is registered and this host's
    containment is verified (the start-up self-test passed and no run's confirmation has
    dropped it since) — the same reading that lets it run at all; otherwise ``none``."""
    if name in OS_SANDBOX_TOOLS:
        from .run_checks import containment, is_enabled

        try:
            verified = is_enabled() and containment().verified
        except Exception:  # noqa: BLE001 - unreadable: not shown as isolated
            verified = False
        if verified:
            return {"kind": "os_sandbox", "os_sandbox": True}
        return {"kind": "none", "os_sandbox": False}
    if name in PATH_CONFINED_TOOLS:
        # StiraWriteSandbox / WritePathPolicy: a path check on the host, not an OS sandbox.
        return {"kind": "path_confined", "os_sandbox": False}
    return {"kind": "none", "os_sandbox": False}


def persistent_grant(name: str, policy: str) -> dict | None:
    """What an Always rule grants, stated in full (scope, expiry, where to revoke)."""
    if policy != ALWAYS or not always_allowed(name):
        return None
    return {
        "tool": name,
        "scope": "tool_any_arguments",
        "applies_to": "tool-config.json",
        "expires": "until_revoked",
        "revoke": REVOKE_TARGET,
        "still_gated_by": ["breaker", "floor"],
    }


def tool_stack(
    tool_config: Any,
    name: str,
    breaker_level: Any,
    *,
    exposed: bool,
    critical_action: str | None = None,
) -> dict:
    """Row fields for one tool: rule, layer stack, effective decision, isolation, grant."""
    policy = rule_policy(tool_config, name, effective=False)
    category = category_of(name)
    rule = rule_layer(policy)
    capped = policy == ALWAYS and always_capped(name)
    if capped:
        # ADR 0019 Q3: saved Always, read as Ask until the host's sandbox is verified.
        rule = {"decision": PROMPT, "reason": "rule_always_capped"}
    if getattr(tool_config, "integrity_error", None):
        rule = {"decision": FORBIDDEN, "reason": "rule_locked"}
    layers = {
        "rule": rule,
        "breaker": breaker_layer(breaker_level, category, critical_action),
        "floor": floor_layer(name, exposed=exposed),
    }
    return {
        "policy": policy,
        "always_allowed": always_allowed(name),
        "stack": layers,
        "effective": effective(layers),
        "isolation": isolation(name),
        "grant": None if capped else persistent_grant(name, policy),
        **({"always_capped": True} if capped else {}),
    }


def floor_items(
    read_only_paths: Iterable[str] = (), *, data_directory: Mapping | None = None
) -> list[dict]:
    """Read-only list of the floor for the settings page; nothing here is editable.

    ``data_directory`` (#271): the server's data-directory floor, when it has one
    (``writable``: the data-directory-relative subdirectories file tools may write;
    ``readable``, #312: the ones they may read, when the server reports them).
    """
    defaults = sorted(_display_name(p) for p in DEFAULT_READ_ONLY_PATHS)
    extra = {str(p) for p in read_only_paths} - set(DEFAULT_READ_ONLY_PATHS)
    items = [
        {
            "id": "identity_read_only",
            "files": defaults,
            "extra_count": len(extra),
            "approvable": False,
        },
    ]
    if isinstance(data_directory, Mapping):
        writable = data_directory.get("writable")
        item = {
            "id": "data_directory",
            "writable": [str(w) for w in writable] if isinstance(writable, list) else [],
            "approvable": False,
        }
        readable = data_directory.get("readable")
        if isinstance(readable, list):
            item["readable"] = [str(r) for r in readable]
        items.append(item)
    return [
        *items,
        {
            "id": "os_protection",
            "commands": ["xattr", "chflags", "sudo", "tccutil", "csrutil"],
            "approvable": False,
        },
        {
            "id": "exec_exposure",
            "env": registry.EXEC_ENV_VAR,
            "exposed": registry.exec_tools_enabled(),
            "approvable": False,
        },
    ]


def _display_name(path: str) -> str:
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


__all__ = [
    "ALLOW",
    "ALWAYS",
    "ASK",
    "FORBIDDEN",
    "LOOSENING_LEVELS",
    "NEVER",
    "POLICIES",
    "PROMPT",
    "RANK",
    "always_allowed",
    "always_capped",
    "breaker_layer",
    "category_of",
    "effective",
    "floor_items",
    "floor_layer",
    "isolation",
    "level_name",
    "loosened",
    "newly_always_writes",
    "persistent_grant",
    "policies_from_values",
    "rule_layer",
    "rule_policy",
    "tool_stack",
]
