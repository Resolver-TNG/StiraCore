"""``gateway-config.json`` ``agent_jobs`` (ADR 0021 S2, Q3 / Q5; #239).

``enabled`` (default **off**): the chat server builds the gated Agent-layer executor at the next
start, so a person may create Agent-layer scheduler jobs (an operator's prompt, read-only
tools only). ``agent_daily_token_budget``: one pool for every Agent-layer job of a UTC day
(50,000 by default; never unlimited). ``STIRA_SCHEDULER=0`` still turns the whole scheduler
off. Model-free: this module is read by the config loader and the settings page.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

__all__ = ["AGENT_JOB_LIMITS", "AgentJobSettings"]

# Same bounds as ``Scheduler(agent_daily_token_budget=…)``.
AGENT_JOB_LIMITS = {"agent_daily_token_budget": (1, 10_000_000)}


@dataclass(frozen=True)
class AgentJobSettings:
    enabled: bool = False
    agent_daily_token_budget: int = 50_000

    @classmethod
    def from_options(cls, options: Any) -> AgentJobSettings:
        """Validate an ``agent_jobs`` object (missing keys: the defaults). ``ValueError``."""
        if options is None:
            return cls()
        if not isinstance(options, Mapping):
            raise ValueError("agent_jobs must be an object")  # noqa: TRY004 - one config error type
        unknown = set(options) - set(AGENT_JOB_LIMITS) - {"enabled"}
        if unknown:
            raise ValueError(f"agent_jobs has unknown keys: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        if "enabled" in options:
            if type(options["enabled"]) is not bool:
                raise ValueError("agent_jobs.enabled must be a boolean")
            values["enabled"] = options["enabled"]
        for name, (low, high) in AGENT_JOB_LIMITS.items():
            if name in options:
                value = options[name]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"agent_jobs.{name} must be an integer in [{low}, {high}]")
                values[name] = value
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}
