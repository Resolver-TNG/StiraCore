"""StiraCore: NXP/Stira-powered agent harness built on Strands.

Quick start for AI agents: read AGENTS.md in the repository root.

Core exports:
    NXPAgent            — Strands Agent wrapper with pre/post-turn safety
    NeuralState         — 6-dimensional emotional state
    StiraEngine         — Breaker + decay + state management
    StiraConfig         — Runtime configuration
    BreakLevel          — NORMAL / WARNING / CRITICAL
    BreakAction         — NONE / BLOCK_WRITES / HALT / INJECT_IDENTITY / ...
    BreakResult         — pre_turn() return value

Memory:
    MemoryStore         — 3-layer memory (MD + SQLite)
    MemoryLayer         — EPISODIC / SEMANTIC / PROCEDURAL
    MemoryEntry         — Single memory record

Context assembly:
    ContextAssembler    — SOUL + memory + state → system_prompt
    StatePersistence    — Auto save/load Neural State

Safety:
    IdentityGuard       — Protected path enforcement
    AmygdalaPass        — Importance scoring for memory routing

Model routing:
    ModelRole           — ORCHESTRATOR / CODER / REVIEWER / RESEARCHER / FAST_GATE
    ExecutionContext     — NORMAL / CRON / HEARTBEAT
    create_model_for_context — Context-aware model selection
    ModelSpec           — Provider-neutral model selection (bedrock/openai/ollama/...)

Observability:
    DashboardHookProvider — Strands HookProvider for lifecycle events
    InMemoryEventSink     — Thread-safe event buffer
    SQLiteEventStore      — Persistent event store
    ObservabilityEvent    — Normalized event dataclass
"""

__version__ = "0.1.0-dev"

# Core
from stiracore.agent.context_assembler import ContextAssembler
from stiracore.agent.model_factory import (
    ExecutionContext,
    ModelRole,
    create_model,
    create_model_for_context,
)

# Agent
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.state_persistence import StatePersistence
from stiracore.core.amygdala import AmygdalaPass
from stiracore.core.engine import (
    BreakAction,
    BreakLevel,
    BreakResult,
    HITLAction,
    HITLDecision,
    HITLRequest,
    StiraConfig,
    StiraEngine,
)
from stiracore.core.neural_state import NeuralState
from stiracore.observability.event_store import SQLiteEventStore

# Observability
from stiracore.observability.hooks import (
    DashboardHookProvider,
    EventSink,
    InMemoryEventSink,
    ObservabilityEvent,
    create_dashboard_hooks,
)
from stiracore.providers import ModelSpec

# Memory
from stiracore.tools.memory_store import MemoryEntry, MemoryLayer, MemoryStore

__all__ = [  # noqa: RUF022 - grouped by public API area
    "AmygdalaPass",
    # Core
    "BreakAction",
    "BreakLevel",
    "BreakResult",
    "ContextAssembler",
    "HITLAction",
    "HITLDecision",
    "HITLRequest",
    # Observability
    "DashboardHookProvider",
    "EventSink",
    "ExecutionContext",
    "InMemoryEventSink",
    # Memory
    "MemoryEntry",
    "MemoryLayer",
    "MemoryStore",
    "ModelRole",
    "ModelSpec",
    # Agent
    "NXPAgent",
    "NeuralState",
    "ObservabilityEvent",
    "SQLiteEventStore",
    "StatePersistence",
    "StiraConfig",
    "StiraEngine",
    "create_dashboard_hooks",
    "create_model",
    "create_model_for_context",
]
