"""Context profiles for small model windows (#368).

A profile is a name for a set of **default** budgets of the existing
``ContextAssembler`` parameters, plus whether only the usable tool definitions are
sent. ``"default"`` changes nothing: the assembler keeps the values its caller passes
(the prompt and the tool list stay byte-identical). An explicit value given by the
caller (or the person) always wins over the profile's (:func:`assembler_budget`).

The numbers are provisional until measured on real small windows; they live only in
:data:`PROFILES`. The memory map and hand-off switches of other work are reserved
columns here (``None`` = not decided by the profile) and are not read yet.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ENV_NAME = "STIRA_CONTEXT_PROFILE"
DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class ContextProfile:
    """Default budgets of one profile; ``None`` = the caller's value is kept."""

    name: str
    max_tokens: int | None = None
    episodic_limit: int | None = None
    procedural_limit: int | None = None
    # Send only the tools the live rules allow (Always / Ask), #368.
    send_allowed_tools_only: bool = False
    # Shorter property descriptions in the long upstream tool schemas, #368.
    short_tool_schemas: bool = False
    # Reserved: decided by their own switches for now (not read by this module).
    memory_map: bool | None = None
    handoff: bool | None = None


# The one table. Provisional numbers (to be replaced by measurement on small windows).
PROFILES: Mapping[str, ContextProfile] = {
    "default": ContextProfile("default"),
    "small": ContextProfile(
        "small",
        max_tokens=3000,
        episodic_limit=2,
        procedural_limit=3,
        send_allowed_tools_only=True,
        short_tool_schemas=True,
    ),
    "tiny": ContextProfile(
        "tiny",
        max_tokens=1800,
        episodic_limit=1,
        procedural_limit=2,
        send_allowed_tools_only=True,
        short_tool_schemas=True,
    ),
}
PROFILE_NAMES = tuple(PROFILES)
_BUDGET_FIELDS = ("max_tokens", "episodic_limit", "procedural_limit")


def get_profile(name: str) -> ContextProfile:
    """The profile called ``name``; ``ValueError`` for an unknown name."""
    if not isinstance(name, str) or name not in PROFILES:
        raise ValueError(f"context_profile must be one of {list(PROFILE_NAMES)}")
    return PROFILES[name]


def effective_name(configured: Any, environ: Mapping[str, str] | None = None) -> str:
    """``STIRA_CONTEXT_PROFILE`` (when set to a known name) over the configured value.

    An unknown variable value is ignored with a warning (names only: the value is not
    logged); an unknown configured value is the default (the config loader refuses it).
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_NAME) or "").strip()
    if raw:
        if raw in PROFILES:
            return raw
        logger.warning("%s is not a known profile; the configured profile is used", ENV_NAME)
    return configured if isinstance(configured, str) and configured in PROFILES else DEFAULT_PROFILE


def assembler_budget(profile: ContextProfile | str, **explicit: int | None) -> dict[str, int]:
    """``ContextAssembler`` keyword arguments the profile decides.

    ``explicit``: the caller's own ``max_tokens`` / ``episodic_limit`` /
    ``procedural_limit`` (``None`` = not given); a given value wins. Only the keys with a
    value are returned, so ``default`` with nothing explicit returns ``{}``.
    """
    unknown = set(explicit) - set(_BUDGET_FIELDS)
    if unknown:
        raise TypeError(f"unknown budget fields: {sorted(unknown)}")
    if isinstance(profile, str):
        profile = get_profile(profile)
    budget: dict[str, int] = {}
    for name in _BUDGET_FIELDS:
        value = explicit.get(name)
        if value is None:
            value = getattr(profile, name)
        if value is not None:
            budget[name] = value
    return budget


def send_allowed_tools_only(profile: ContextProfile | str, switch: bool = False) -> bool:
    """The tool-definition filter is on for ``small`` / ``tiny`` or the explicit switch."""
    if isinstance(profile, str):
        profile = get_profile(profile)
    return bool(switch) or profile.send_allowed_tools_only


def short_tool_schemas(profile: ContextProfile | str, switch: bool = False) -> bool:
    """The shorter tool-schema descriptions are on for ``small`` / ``tiny`` or the switch."""
    if isinstance(profile, str):
        profile = get_profile(profile)
    return bool(switch) or profile.short_tool_schemas
