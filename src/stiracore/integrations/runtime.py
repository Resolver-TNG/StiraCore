"""Shared, serialized chat runner; NXPAgent alone owns turn lifecycle hooks."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from stiracore import ContextAssembler, MemoryStore, NeuralState, NXPAgent, StatePersistence
from stiracore.agent.context_profile import assembler_budget, effective_name
from stiracore.config import AgentConfig, GatewayConfig
from stiracore.config.resolver import resolve_config
from stiracore.tools.episodic_emotion import capture_emotion_snapshot, emotion_snapshot_enabled
from stiracore.tools.episodic_formatter import record_episode

DIMENSIONS = ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction")
ICONS = {"normal": "🟢", "warning": "🟡", "critical": "🔴", "coherence": "🟡"}
ERROR_TEXT = "Unable to complete this turn. Check the bot service logs and breaker status."
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnResult:
    text: str
    before: dict[str, Any]
    after: dict[str, Any]


class ChatSession:
    """One conversation's agent, memory and state; safe for Socket Mode threads."""

    def __init__(self, agent, *, store=None, persistence=None, source="chat", emotion=True):
        self.agent = agent
        # AgentConfig.episodic_emotion_snapshot (#364); STIRA_EPISODIC_EMOTION wins per turn.
        self.emotion = emotion
        self.store = store
        self.persistence = persistence
        self.source = source
        self._lock = RLock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "neural_state": {name: getattr(self.agent.state, name) for name in DIMENSIONS},
                "breaker": self.agent.engine.breaker_status().level.value,
            }

    def runner(self, message: str) -> TurnResult:
        with self._lock:
            before = self.status()
            try:
                response = str(self.agent(message))
                try:
                    if self.store is not None:
                        record_episode(
                            self.store,
                            message,
                            response,
                            source=self.source,
                            emotion=capture_emotion_snapshot(self.agent)
                            if emotion_snapshot_enabled(self.emotion)
                            else None,
                        )
                # #601: a failed record keeps the reply the agent already gave.
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Chat episode record failed (%s)", type(exc).__name__)
            # Transport boundary: arbitrary provider failures must not leak to chat.
            except Exception as exc:  # noqa: BLE001
                logger.warning("Chat turn failed (%s)", type(exc).__name__)
                response = ERROR_TEXT
            finally:
                if self.persistence is not None:
                    try:
                        self.persistence.save()
                    except OSError as exc:
                        logger.warning("Chat state save failed (%s)", type(exc).__name__)
            return TurnResult(response, before, self.status())


def create_session(platform: str, conversation: str) -> ChatSession:
    """Create an isolated conversation with no host tools exposed to remote users."""
    import hashlib

    # Platform IDs never become paths, and different workspaces/guilds stay isolated.
    key = hashlib.sha256(conversation.encode()).hexdigest()
    base = Path(os.environ.get("STIRA_CHAT_DATA_DIR", ".local/chat")) / platform / key
    base.mkdir(parents=True, exist_ok=True)
    state = NeuralState()
    persistence = StatePersistence(state, base / "neural-state.json")
    persistence.load()
    gateway = GatewayConfig.load()
    identity = AgentConfig.load()
    resolved = resolve_config(gateway, identity)
    soul = Path(resolved.soul_path).expanduser() if resolved.soul_path else None
    store = MemoryStore(base / "memory", platform, read_only_paths=identity.read_only_paths)
    agent = NXPAgent(
        **resolved.nxp_model_kwargs(),
        state=state,
        config=resolved.stira_config,
        max_output_tokens=resolved.max_output_tokens,
        read_only_paths=identity.read_only_paths,
        tools=(),
        # Replies to remote users never go to the bot's stdout (integration review).
        callback_handler=None,
    )
    assembler = ContextAssembler(
        agent_id=platform,
        memory_store=store,
        engine=agent.engine,
        soul_path=soul,
        breaker_state_provider=agent.engine.breaker_status,
        include_identity=False,
        # #368: the context profile's budgets ("default" = none: built as before).
        **assembler_budget(effective_name(gateway.context_profile)),
    )
    agent.system_prompt_provider = assembler.build_system_prompt
    return ChatSession(
        agent,
        store=store,
        persistence=persistence,
        source=platform,
        emotion=identity.episodic_emotion_snapshot,
    )


class ChatSessions:
    """Lazy per-conversation sessions; token-less startup never creates an agent."""

    def __init__(self, platform: str, factory=create_session):
        self.platform = platform
        self.factory = factory
        self._sessions: dict[str, ChatSession] = {}
        self._lock = RLock()

    def get(self, key: str) -> ChatSession:
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = self.factory(self.platform, key)
            return self._sessions[key]


def status_text(status: dict[str, Any], before: dict[str, Any] | None = None) -> str:
    level = status["breaker"]
    stability = status["neural_state"]["stability"]
    change = ""
    if before is not None:
        previous = before["neural_state"]["stability"]
        change = f" ({stability - previous:+.3f}); breaker {before['breaker']} → {level}"
    return f"{ICONS.get(level, '⚪')} {level.upper()} | stability={stability:.3f}{change}"


def chunks(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or ["(empty response)"]
