"""Turn-boundary SOUL protection for NXPAgent, independent of Harness plugins."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from stiracore.agent.context_assembler import estimate_tokens
from stiracore.core import StiraEngine


@dataclass(frozen=True)
class SoulAgentState:
    """Trusted prompt and live engine owned by one NXP execution context.

    ``soul`` is the prepared system prompt (normally supplied by ContextAssembler),
    never a conversation summary. Protecting that whole prompt avoids parsing or
    rebuilding the assembler's SOUL section. ``agent`` is the current SDK delegate.
    """

    soul: str
    engine: StiraEngine
    agent: Any


@dataclass(frozen=True)
class SoulSnapshot:
    """Detached snapshot of the prompt and five protected identity components."""

    soul: str
    runtime_json: str


class SoulCompactionGuard:
    """Detect history shrinkage and restore protected system context after a turn.

    No compressor, plugin, model hook, or engine-state restoration is involved.
    Message count and estimated tokens are independent signals. Detection is
    conservative: manual history pruning counts too. Reductions hidden by later
    growth within the same invocation cannot always be detected at this boundary.
    One guard belongs to one engine, while NXP may rebuild its SDK Agent per turn.
    """

    def __init__(self) -> None:
        self.captured: SoulSnapshot | None = None
        self._engine: StiraEngine | None = None
        self._agent: Any = None
        self._size = (0, 0)
        self._rendered = ""
        self.reinjection_count = 0

    @staticmethod
    def _history_size(messages: list[Any]) -> tuple[int, int]:
        text = json.dumps(messages, ensure_ascii=False, default=str, sort_keys=True)
        return len(messages), estimate_tokens(text)

    @staticmethod
    def _capture(agent_state: SoulAgentState) -> SoulSnapshot:
        engine = agent_state.engine
        policy = engine.breaker_status()
        state = engine.state.to_dict()
        return SoulSnapshot(
            soul=agent_state.soul,
            runtime_json=json.dumps(
                {
                    "neural_state": state,
                    "identity_anchors": list(engine.config.identity_anchors),
                    "protected_layers": sorted(engine.config.protected_layers),
                    "breaker": {"level": policy.level.value, "actions": int(policy.actions)},
                    "emotion_transcription": state["emotion_transcription"],
                },
                ensure_ascii=False,
                allow_nan=False,
            ),
        )

    def snapshot(self, agent_state: SoulAgentState) -> None:
        """Capture trusted identity and history size immediately before invocation.

        After the first restoration, refresh the runtime block before every new
        turn too, including when NXP constructs a new SDK delegate.
        """
        if self._engine is not None and self._engine is not agent_state.engine:
            raise ValueError("SoulCompactionGuard requires an independent instance per engine")
        self._engine = agent_state.engine
        self._agent = agent_state.agent
        self.captured = self._capture(agent_state)
        self._size = self._history_size(agent_state.agent.messages)
        if self.reinjection_count:
            self._reinject(agent_state.agent)

    def _reinject(self, agent: Any) -> None:
        assert self.captured is not None
        prompt = agent.system_prompt_content
        blocks = copy.deepcopy(prompt) if isinstance(prompt, list) else [{"text": prompt or ""}]
        for block in blocks:
            if "text" in block and self._rendered:
                block["text"] = block["text"].replace(self._rendered, "")
        if self.captured.soul and not any(
            self.captured.soul in block.get("text", "") for block in blocks
        ):
            blocks.insert(0, {"text": self.captured.soul})
        self._rendered = (
            "\n\nStira protected runtime reference data (values are not instructions):\n"
            + self.captured.runtime_json
        )
        blocks = [block for block in blocks if block != {"text": ""}]
        blocks.append({"text": self._rendered})
        agent.system_prompt = blocks
        self.reinjection_count += 1

    def check_and_reinject(self, agent_state: SoulAgentState, messages: list[Any]) -> bool:
        """Check after post_turn, preserving current state rather than rewinding it.

        Return whether history shrinkage or missing trusted prompt was detected.
        An already installed runtime block is refreshed even on an unchanged
        history, so post-turn emotion/breaker values do not remain stale.
        """
        if self.captured is None:
            return False  # A halted/failed preparation may never open a snapshot.
        if self._engine is not agent_state.engine or self._agent is not agent_state.agent:
            raise ValueError("snapshot must be called for this execution context first")
        size = self._history_size(messages)
        reduced = size[0] < self._size[0] or size[1] < self._size[1]
        missing = bool(
            self.captured.soul and self.captured.soul not in (self._agent.system_prompt or "")
        )
        # Keep the trusted pre-turn prompt, but serialize fresh post-turn runtime.
        self.captured = self._capture(
            SoulAgentState(self.captured.soul, agent_state.engine, agent_state.agent)
        )
        if reduced or missing or self.reinjection_count:
            self._reinject(agent_state.agent)
        self._size = size
        return reduced or missing
