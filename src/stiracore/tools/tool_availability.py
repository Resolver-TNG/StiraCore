"""What the agent is told about the tools it cannot call, and about its permissions (#508).

A tool whose rule is Never is removed from the model's tool list (ADR 0010 §1), so without
this section the agent cannot tell "disabled by a person" from "does not exist" — it reported
``page_check`` and ``propose_procedure`` as missing features. It also could not tell which
of its calls ran under an Always rule and which under a YOLO window.

The section is a few lines of plain text: per disabled tool its name, one line of purpose, a
reason code and where a person can enable it — **never a tool schema** (#368). It is a pure
function of the saved tool rules, the start-up switches (exec tools, the verification
runner, memory autonomy, the browser found) and the YOLO window (open or not, its tier, its
fixed end): the same inputs give the same bytes, so it changes only when a person saves the
rules or a window opens or ends (#254 / #259), never with a turn. Nothing here grants
anything: a call of a Never tool is refused at the gate as before (``rule_never``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

TITLE = "Tool Availability"

# Reason codes (model-facing, stable; the settings page's own vocabulary is ``rule_*``).
POLICY_NEVER = "policy_never"
RULES_LOCKED = "rules_locked"
EXEC_NOT_ENABLED = "exec_not_enabled"
DEPENDENCY_MISSING = "dependency_missing"
NOT_CONFIGURED = "not_configured"
UNSUPPORTED_HOST = "unsupported_host"
REASONS = (
    POLICY_NEVER,
    RULES_LOCKED,
    EXEC_NOT_ENABLED,
    DEPENDENCY_MISSING,
    NOT_CONFIGURED,
    UNSUPPORTED_HOST,
)

# Where a person can enable a tool, per reason: one legend line for the codes in use.
WHERE = {
    POLICY_NEVER: "Settings → Tools (a person sets the rule to Ask or Always)",
    RULES_LOCKED: "Settings → Tools (the saved rules failed their check; a person reviews and "
    "saves them)",
    EXEC_NOT_ENABLED: "a person restarts the server with STIRA_ENABLE_EXEC_TOOLS=1",
    DEPENDENCY_MISSING: "a person installs what it needs on the host (`stira doctor` says what)",
    NOT_CONFIGURED: "a person turns it on in the server configuration and restarts",
    UNSUPPORTED_HOST: "not available on this host",
}
# The tool-specific place, shown on the tool's line (short).
WHERE_FOR = {
    ("page_check", DEPENDENCY_MISSING): "Chrome/Chromium or STIRA_BROWSER_PATH",
    ("run_checks", NOT_CONFIGURED): 'gateway-config.json "run_checks": {"phase": 1}',
    ("memory_rate", NOT_CONFIGURED): "agent-config.json memory_autonomy",
    ("web_search", NOT_CONFIGURED): "STIRA_WEB_SEARCH_ENDPOINT",
}

# One line of purpose where the tool's own description starts with its arguments.
PURPOSE = {
    "page_check": "open your own page in a disposable browser and check it (console errors, "
    "assertions, a screenshot)",
    "propose_procedure": "propose a procedure for procedural memory (a person reviews it)",
    "memory_rate": "rate how important this turn is to remember",
    "run_checks": "run the project's tests and linters on a throwaway copy of the workspace",
    "shell": "run shell commands",
    "batch": "run several tool calls from one request",
    "cron": "manage the system crontab",
    "graph": "run a multi-agent DAG",
    "swarm": "run a team of agents on one task",
    "think": "recursive thinking cycles in a nested agent",
    "use_agent": "run one nested agent",
    "current_time": "the current date and time",
    "http_request": "HTTP requests to any API",
}
_PURPOSE_MAX = 80


def purpose_of(name: str, tool: Any = None) -> str:
    """One line: the curated text, else the first sentence of the tool's description."""
    if name in PURPOSE:
        return PURPOSE[name]
    spec = getattr(tool, "tool_spec", None)
    description = spec.get("description", "") if isinstance(spec, Mapping) else ""
    text = " ".join(str(description).split())
    for stop in (". ", "; "):
        if stop in text:
            text = text.split(stop, 1)[0]
    text = text.rstrip(".")
    if len(text) > _PURPOSE_MAX:
        text = text[:_PURPOSE_MAX].rsplit(" ", 1)[0] + "…"
    return text or name


def effective_rule(row: Mapping[str, Any]) -> str:
    """The rule as the gate reads it: a capped Always is Ask, locked rules are Never."""
    reason = ((row.get("stack") or {}).get("rule") or {}).get("reason")
    if reason == "rule_locked":
        return "never"
    if reason == "rule_always_capped":
        return "ask"
    policy = row.get("policy")
    return policy if policy in ("always", "ask", "never") else "never"


def disabled_tools(
    rows: Iterable[Mapping[str, Any]],
    *,
    absent: Mapping[str, str] | None = None,
    unbound: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """``name -> reason codes`` of every tool this server will not run now (sorted names).

    ``rows``: ``/api/config/tools`` rows (the registry tools). A row is disabled when its
    effective rule is Never (``policy_never``, or ``rules_locked``) or its exec class is
    not exposed (``exec_not_enabled``). ``absent``: tools of this build the registry did
    not register (e.g. ``page_check`` without a browser, ``run_checks`` switched off);
    ``unbound``: registry tools the server does not bind (``memory_rate`` without memory
    autonomy). Breaker restrictions are not listed: they change with the turn and are
    stated in the Breaker section.
    """
    found: dict[str, list[str]] = {}
    for row in rows:
        name = str(row.get("name"))
        codes = []
        if effective_rule(row) == "never":
            locked = ((row.get("stack") or {}).get("rule") or {}).get("reason") == "rule_locked"
            codes.append(RULES_LOCKED if locked else POLICY_NEVER)
        if row.get("exposed") is False:
            codes.append(EXEC_NOT_ENABLED)
        if codes:
            found[name] = codes
    for extra in (absent or {}, unbound or {}):
        for name, code in extra.items():
            codes = found.setdefault(str(name), [])
            if code not in codes:
                codes.append(code)
    return {name: tuple(found[name]) for name in sorted(found)}


def rule_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"always": 0, "ask": 0, "never": 0}
    for row in rows:
        counts[effective_rule(row)] += 1
    return counts


def _when(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes")


YOLO_SCOPE = (
    "YOLO only approves Ask calls automatically; Always calls never needed an approval, so "
    "they are not YOLO's doing."
)


def yolo_line(allowed: bool, window: Mapping[str, Any] | None) -> str:
    """The window: open or not, its tier and fixed end — never a count or remaining time."""
    if window:
        tier = "external allowed" if window.get("tier") == "external" else "internal only"
        exec_part = ", exec included" if window.get("exec") else ""
        until = _when(window.get("ends_at"))
        end = f" until {until}" if until else ""
        return (
            f"YOLO: a window is open ({tier}{exec_part}){end}; it pauses while the breaker "
            "is not NORMAL and applies only to turns a person's message started."
        )
    if allowed:
        return "YOLO: no window is open."
    return ""


def section(
    rows: Iterable[Mapping[str, Any]],
    *,
    tools: Mapping[str, Any] | None = None,
    absent: Mapping[str, str] | None = None,
    unbound: Mapping[str, str] | None = None,
    yolo_allowed: bool = False,
    yolo_window: Mapping[str, Any] | None = None,
) -> str:
    """The ``## Tool Availability`` text: disabled tools, then the current permissions.

    ``tools``: ``name -> tool object`` for the fallback purpose line (its description, not
    its schema). Deterministic: sorted names, fixed wording, no clock but the window's
    fixed end. Without the YOLO allowance (and no window) there is no YOLO line at all.
    """
    rows = list(rows)
    disabled = disabled_tools(rows, absent=absent, unbound=unbound)
    lines = [f"## {TITLE}"]
    if disabled:
        lines.append(
            "These tools exist in this Stira but are disabled. You cannot use them. You can "
            "ask the user to enable one (suggest the wording of the request)."
        )
        used: list[str] = []
        for name, codes in disabled.items():
            purpose = purpose_of(name, (tools or {}).get(name))
            places = [WHERE_FOR[(name, code)] for code in codes if (name, code) in WHERE_FOR]
            place = f" [{'; '.join(places)}]" if places else ""
            lines.append(f"- {name}: {purpose} — {', '.join(codes)}{place}")
            used.extend(code for code in codes if code not in used)
        legend = "; ".join(f"{code} = {WHERE[code]}" for code in REASONS if code in used)
        lines.append(f"Who can enable them: {legend}.")
    else:
        lines.append("Disabled tools: none.")
    counts = rule_counts(rows)
    lines.append(
        f"Tool rules now ({len(rows)} rule-governed tools): Always {counts['always']} / "
        f"Ask {counts['ask']} / Never {counts['never']}."
    )
    if yolo_allowed or yolo_window:
        # ADR 0012: without the start-up allowance the prompt never mentions YOLO.
        lines.append(yolo_line(yolo_allowed, yolo_window))
        lines.append(YOLO_SCOPE)
    return "\n".join(lines)


def with_section(prompt: str, text: str) -> str:
    """``prompt`` followed by the section (either may be empty)."""
    prompt, text = (prompt or "").strip(), (text or "").strip()
    return f"{prompt}\n\n{text}" if prompt and text else prompt or text


__all__ = [
    "DEPENDENCY_MISSING",
    "EXEC_NOT_ENABLED",
    "NOT_CONFIGURED",
    "POLICY_NEVER",
    "PURPOSE",
    "REASONS",
    "RULES_LOCKED",
    "TITLE",
    "UNSUPPORTED_HOST",
    "WHERE",
    "WHERE_FOR",
    "YOLO_SCOPE",
    "disabled_tools",
    "effective_rule",
    "purpose_of",
    "rule_counts",
    "section",
    "with_section",
    "yolo_line",
]
