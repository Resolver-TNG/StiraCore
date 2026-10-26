"""Trust labels and the context ledger — phase 0: observe only (ADR 0022, #458).

A label has two parts (ADR 0022 §1): ``trust`` (``trusted`` / ``untrusted``) and
``sensitive`` (``False`` / ``True``). The source of a text is chosen by **where it came
from** — the tool name, the turn's origin, an injected block — from the constant tables
below (§2); nothing in a tool result, a memory entry or the model's text is read as a label
(§7). A tool missing from the table is ``external`` / untrusted.

The unit is the agent context (§3): the label of a context is the union (lowest trust, any
sensitive) of everything in it. :func:`context_label` scans the messages the next model call
would see, so ``/new`` clears it and nothing else does.

:func:`flow_would_hold` answers which code the flow layer of §6 *would* give an egress call
(F1–F4). **Phase 0 enforces nothing**: no gate, prompt, tool result or cached byte changes;
only codes and counts are recorded. No destination list exists yet, so none is listed.

Switch: ``taint`` in ``agent-config.json`` (``{"phase": 0}``; absent or ``{}`` = off).
``STIRA_TAINT_OBSERVE=0`` (``false`` / ``no`` / ``off``) forces it off; no value turns it on.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from stiracore.memory_autonomy import TRUSTED_RESULT_TOOLS
from stiracore.observability.masking import mask_secrets
from stiracore.yolo.tiers import EXTERNAL_TOOLS, external_refusal, mcp_tool, tool_class

ENV_VAR = "STIRA_TAINT_OBSERVE"
PHASES = (0,)
FRAME_VERSION = 0  # phase 1 adds the frame registry
_FALSY = frozenset({"0", "false", "no", "off"})
_KEYS = frozenset({"phase"})

SOURCES = (
    "system",
    "operator",
    "chat_user",
    "model",
    "memory",
    "workspace",
    "external",
    "program",
    "child",
)
TRUSTED_SOURCES = frozenset({"system", "operator", "model"})

# §2: tool name -> source of its *result*. Missing = external.
_TOOL_SOURCES: dict[str, str] = {
    **dict.fromkeys(TRUSTED_RESULT_TOOLS, "system"),
    **dict.fromkeys(
        ("memory_read", "memory_search", "handoff_read", "retrieve_offloaded_content"), "memory"
    ),
    **dict.fromkeys(
        ("file_read", "view_image", "image_reader", "lsp_diagnostics", "lsp_hover"), "workspace"
    ),
    **dict.fromkeys(("page_check", "run_checks", "shell", "batch", "python_repl"), "program"),
    **dict.fromkeys(("delegate", "use_agent", "swarm", "graph", "think"), "child"),
}
# Phase 0 has no trust record on memory or files (phase 1 adds them): both untrusted (Q3).
SENSITIVE_RESULT_TOOLS = frozenset({"oauth_api_call"})  # §5 S2
_BY_REFERENCE = frozenset({"auth_env_var", "aws_auth"})
FLOW_CODES = ("secret_in_args", "credential_by_reference", "sensitive_context",
              "untrusted_send", "long_payload")  # fmt: skip
_SEND_ALWAYS = frozenset({"webhook_send", "speak"})
_READ_METHODS = frozenset({"GET", "HEAD"})
_LONG = 256


def tool_source(name: Any) -> str:
    """The source of a tool's result (§2); unknown, odd or MCP names are ``external``."""
    if type(name) is not str:
        return "external"
    source = _TOOL_SOURCES.get(name)
    if source is None or source == "system" and egress_tool(name):
        return "external"  # an MCP server may reuse a trusted name
    return source


def origin_source(started_by_person: bool, automatic: bool = False) -> str:
    """The source of the turn's first message: a person at the dashboard, else outside."""
    return "operator" if started_by_person and not automatic else "external"


@dataclass(frozen=True)
class ContextLabel:
    trust: str = "trusted"
    sensitive: bool = False
    sources: Mapping[str, int] = field(default_factory=dict)
    recall_only: bool = False  # Q3: untrusted only because of recalled memory

    def as_row(self) -> dict[str, Any]:
        return {
            "context_trust": self.trust,
            "context_sensitive": self.sensitive,
            "input_labels": {k: int(v) for k, v in sorted(self.sources.items())},
            "untrusted_recall_only": self.recall_only,
            "frame_version": FRAME_VERSION,
        }


def _blocks(messages: Any) -> Iterable[Mapping]:
    for message in messages if isinstance(messages, (list, tuple)) else ():
        content = message.get("content") if isinstance(message, Mapping) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, Mapping):
                yield block


def _has_secret(value: Any) -> bool:
    try:
        return mask_secrets(value) != value
    except Exception:  # noqa: BLE001 - detection only; an unreadable value is not counted
        return False


def context_label(
    messages: Any, *, origin: str = "external", injected: Iterable[str] = ()
) -> ContextLabel:
    """The union label of a context (§3). ``origin`` and ``injected`` are sources the
    harness itself names (the turn's starter, recalled blocks in the prompt)."""
    counts: Counter[str] = Counter()
    counts[origin if origin in SOURCES else "external"] += 1
    for name in injected:
        counts[name if name in SOURCES else "external"] += 1
    sensitive = False
    if not isinstance(messages, (list, tuple)):
        counts["external"] += 1  # unreadable: fail closed
        messages = ()
    for block in _blocks(messages):
        use = block.get("toolUse")
        if isinstance(use, Mapping):
            name = use.get("name")
            counts[tool_source(name)] += 1
            arguments = use.get("input")
            if name in SENSITIVE_RESULT_TOOLS or (
                isinstance(arguments, Mapping) and _BY_REFERENCE & set(arguments)
            ):
                sensitive = True  # S2: a credentialed call returned
        result = block.get("toolResult")
        if isinstance(result, Mapping) and not sensitive:
            sensitive = _has_secret(result.get("content"))  # S1
    untrusted = {s for s in counts if s not in TRUSTED_SOURCES}
    return ContextLabel(
        trust="untrusted" if untrusted else "trusted",
        sensitive=sensitive,
        sources=dict(counts),
        recall_only=untrusted == {"memory"},
    )


def egress_tool(name: Any) -> bool:
    """§6: the external tools plus every tool the table does not know (MCP, unclassified)."""
    if type(name) is not str or tool_class(name) == "external":
        return True
    try:
        return bool(mcp_tool(name))
    except Exception:  # noqa: BLE001 - unknown: egress (fail closed)
        return True


def is_send(name: Any, tool_input: Any) -> bool:
    """§6 send / fetch, read from the arguments; unreadable arguments are send."""
    if not isinstance(tool_input, Mapping):
        return True
    if name in _SEND_ALWAYS:
        return True
    if name in ("http_request", "oauth_api_call"):
        method = str(tool_input.get("method") or "GET").upper()
        return method not in _READ_METHODS or bool(tool_input.get("body"))
    if name == "memory":
        return tool_input.get("action") not in ("retrieve", "list", "get")
    return name not in ("web_fetch", "web_search", "perplexity_search", "rss")  # MCP: send


def _long_part(tool_input: Mapping) -> bool:
    for value in tool_input.values():
        if isinstance(value, str):
            for part in value.replace("?", "/").replace("&", "/").split("/"):
                if len(part) > _LONG:
                    return True
    return False


def flow_would_hold(name: Any, tool_input: Any, label: ContextLabel) -> str | None:
    """The code F1–F4 would give this call (§6), or ``None``. Nothing is held in phase 0."""
    if not egress_tool(name):
        return None
    if type(name) is str and name in EXTERNAL_TOOLS:
        code = external_refusal(name, tool_input, None)
        if code in ("secret_in_args", "credential_by_reference"):
            return code  # F1
    elif _has_secret(tool_input):
        return "secret_in_args"
    if label.sensitive:
        return "sensitive_context"  # F2 (no destination is listed in phase 0)
    if label.trust == "untrusted":
        if is_send(name, tool_input):
            return "untrusted_send"  # F3
        if isinstance(tool_input, Mapping) and _long_part(tool_input):
            return "long_payload"  # F4
    return None


@dataclass(frozen=True)
class TaintSettings:
    enabled: bool = False
    forced_off: bool = False


def clean_options(value: Any) -> dict[str, Any]:
    """``{}`` (off) or ``{"phase": 0}``; anything else is refused."""
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping) or set(value) - _KEYS:
        raise ValueError("taint must be {} or {'phase': 0}")
    phase = value.get("phase")
    if type(phase) is not int or phase not in PHASES:
        raise ValueError("taint.phase must be 0")
    return {"phase": phase}


def settings_from(value: Any, env: Mapping[str, str] | None = None) -> TaintSettings:
    env = os.environ if env is None else env
    forced = str(env.get(ENV_VAR, "")).strip().lower() in _FALSY and bool(env.get(ENV_VAR))
    try:
        options = clean_options(value)
    except ValueError:
        options = {}
    return TaintSettings(enabled=bool(options) and not forced, forced_off=forced)
