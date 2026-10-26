"""Tool profiles (#450): purpose-made Always / Ask / Never tables for the settings page.

A profile is a fixed table: tool name -> rule, for every built-in tool the registry can
register (``PROFILE_TOOLS``). Applying one puts the table in the settings draft; the save
goes through the hub's usual path (a loosening needs the confirmation checkbox, every
save is a ``settings-history`` row with ``source: "profile:<id>"``). Nothing here writes
a file or changes a saved rule by itself, and no profile is applied on install or update.

* **minimal** — reads only (Always); everything else Never (``memory_rate`` too).
* **dev** — reads Always; local writes, ``lsp_*``, Stira's memory write and ``shell``
  Ask (``shell`` keeps the saved allowlist and still needs ``STIRA_ENABLE_EXEC_TOOLS``);
  network and the other exec tools Never.
* **research** — dev, plus ``web_fetch`` / ``web_search`` / ``perplexity_search`` Ask.

No profile makes a write, external or exec tool Always (so no profile needs names typed)
and none uses ``trust_all``. Tools outside the table (MCP tools, a tool added later) keep
their saved rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from . import tool_registry as registry
from .tool_policy import ALWAYS, ASK, NEVER, policies_from_values

__all__ = [
    "PROFILE_IDS",
    "PROFILE_TABLES",
    "PROFILE_TOOLS",
    "profile_policies",
    "profile_tools",
]

PROFILE_IDS = ("minimal", "dev", "research")

# name -> (minimal, dev, research)
_A, _K, _N = ALWAYS, ASK, NEVER
_TABLE: dict[str, tuple[str, str, str]] = {
    # local reads and control
    "current_time": (_A, _A, _A),
    "file_read": (_A, _A, _A),
    "view_image": (_A, _A, _A),
    "image_reader": (_A, _A, _A),
    "stop": (_A, _A, _A),
    "handoff_to_user": (_A, _A, _A),
    # Stira memory (when the registry carries the memory tools)
    "memory_read": (_A, _A, _A),
    "memory_search": (_A, _A, _A),
    "memory_list": (_A, _A, _A),
    "memory_index": (_A, _A, _A),
    "handoff_read": (_A, _A, _A),
    # ADR 0018 Q3: one bounded rating per turn, no text, no file, no retention change; read
    # category, default Always (first run, dev, research; only offered when memory_autonomy
    # is on). minimal keeps the smallest surface, so applying it never loosens a rule.
    "memory_rate": (_N, _A, _A),
    "memory_write": (_N, _K, _K),
    "handoff_write": (_N, _K, _K),
    # #463: stages a procedural rule for a person's review; Ask or Never only, and YOLO
    # keeps it a card (tiers.PERSON_TOOLS)
    "propose_procedure": (_N, _K, _K),
    # local writes and checks
    "file_write": (_N, _K, _K),
    "editor": (_N, _K, _K),
    "journal": (_N, _K, _K),
    "lsp_diagnostics": (_N, _K, _K),
    "lsp_hover": (_N, _K, _K),
    # ADR 0015 §9: Ask in development, Never in minimal (never Always in phase 1); only
    # listed when a browser is found (YOLO keeps it a card: tiers.PERSON_TOOLS)
    "page_check": (_N, _K, _K),
    # ADR 0019: runs the agent's code in a throwaway copy; only registered when the
    # gateway section turns it on (YOLO keeps it a card: tiers.PERSON_TOOLS). No profile
    # makes it Always (a person may, effective only on a verified sandbox host).
    "run_checks": (_N, _K, _K),
    # starts processes and writes where its title points
    "diagram": (_N, _N, _N),
    # network
    "web_fetch": (_N, _N, _K),
    "web_search": (_N, _N, _K),
    "perplexity_search": (_N, _N, _K),
    "http_request": (_N, _N, _N),
    "webhook_send": (_N, _N, _N),
    "oauth_api_call": (_N, _N, _N),
    "rss": (_N, _N, _N),
    "speak": (_N, _N, _N),
    "memory": (_N, _N, _N),
    # exec class: Ask or Never only, and hidden without STIRA_ENABLE_EXEC_TOOLS
    "shell": (_N, _K, _K),
    "batch": (_N, _N, _N),
    "cron": (_N, _N, _N),
    "use_agent": (_N, _N, _N),
    "think": (_N, _N, _N),
    "swarm": (_N, _N, _N),
    "graph": (_N, _N, _N),
}
PROFILE_TOOLS = frozenset(_TABLE)
PROFILE_TABLES = MappingProxyType(
    {
        profile: MappingProxyType({name: rules[index] for name, rules in _TABLE.items()})
        for index, profile in enumerate(PROFILE_IDS)
    }
)


def profile_policies(profile_id: str, current: Mapping, names: Iterable[str]) -> dict[str, str]:
    """The rule of each of ``names`` once ``profile_id`` is applied over ``current`` (the
    hub's ``tools`` group): the table's rule, or the saved one for a name outside it."""
    if profile_id not in PROFILE_TABLES:
        raise KeyError(profile_id)
    names = sorted(set(names))
    saved = policies_from_values(current, names)
    table = PROFILE_TABLES[profile_id]
    return {name: table.get(name, saved[name]) for name in names}


def profile_tools(profile_id: str, current: Mapping, names: Iterable[str] | None = None) -> dict:
    """The hub ``tools`` value with ``profile_id`` applied (mode ``custom``, the saved
    shell allowlist kept). ``names``: the tools the registry knows (default: now)."""
    if names is None:
        names = set(registry.READ_TOOLS) | set(registry.WRITE_TOOLS)
    policies = profile_policies(profile_id, current, names)
    # A name saved earlier but not registered now (another environment) keeps its switch.
    kept = set(current.get("enabled_tools") or ()) - set(policies) - PROFILE_TOOLS
    kept_ask = set(current.get("ask_tools") or ()) & kept
    enabled = {name for name, rule in policies.items() if rule != NEVER} | kept
    ask = {name for name, rule in policies.items() if rule == ASK} | kept_ask
    return {
        "mode": "custom",
        "enabled_tools": sorted(enabled),
        "shell_allowlist": list(current.get("shell_allowlist") or ()),
        "ask_tools": sorted(ask),
    }
