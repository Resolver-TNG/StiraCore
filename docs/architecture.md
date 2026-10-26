# StiraCore Architecture

## Design Principles

1. **Strands as foundation** — Don't reinvent the agent loop. Use Strands' tool/model/lifecycle abstraction.
2. **NXP as the soul layer** — Memory, identity, and coherence detection sit above Strands core.
3. **Stira as the safety layer** — Circuit breakers and drift detection are non-negotiable.
4. **Layer separation** — L0-L2 are optimizable. L3+ (Identity Anchors) are protected.

## Integration Points with Strands

```
Strands Agent.__call__()
    │
    ├── [PRE-TURN HOOK] ──→ StiraEngine.pre_turn()
    │       ├── Apply lazy decay to Neural State
    │       ├── Update bcWindow / bcScore
    │       ├── Check circuit breaker thresholds
    │       └── Return BreakLevel (NORMAL/WARNING/CRITICAL/COHERENCE)
    │
    ├── [TOOL SELECTION] ──→ tool_registry + breaker actions
    │       ├── Exec-class tools hidden unless STIRA_ENABLE_EXEC_TOOLS
    │       ├── Always/Ask/Never rules; rule → breaker → floor, strictest wins (ADR 0010)
    │       ├── WARNING/CRITICAL withhold write tools (CRITICAL+HALT: HITL approval)
    │       └── (a Jev decision router is not implemented)
    │
    ├── [TOOL EXECUTION] ──→ Memory Tools (read/write/search)
    │       ├── Episodic: session logs, daily state
    │       ├── Semantic: distilled knowledge
    │       └── Procedural: skills, workflows
    │
    ├── [POST-TURN HOOK] ──→ StiraEngine.post_turn()
    │       ├── Apply emotional stimulus from turn result
    │       └── Opt-in: SycophancyMonitor (agreement window),
    │           StiraEngine.check_layer_boundary() for L3+ changes
    │
    └── [PERSISTENCE] ──→ NeuralState.save()
```

Between turns, the opt-in heartbeat (#238, off by default) runs the same lazy decay and a
breaker *query* on a background thread of `stira_server`, under the same
`engine.state_lock`, and never calls a model. Active decay can release WARNING (and
CRITICAL once its cooldown was served by turns) without a turn; elapsed time is not an
approval. See [heartbeat-daemon.md](heartbeat-daemon.md).

## Multi-Model Strategy

| Role | Model | Use case |
|---|---|---|
| Orchestrator | Opus 5.5 | Complex reasoning, architecture decisions |
| Coder/Spec | GPT-6 Astra | Implementation, spec generation |
| Researcher | Sonnet 5 | Investigation, review, analysis |
| Fast gate | Haiku 4.5 | Binary decisions, routing; also every role in the heartbeat context |

These are the Bedrock defaults in `agent/model_factory.py` (`DEFAULT_MODELS`). Any role
can be routed to another provider with a `ModelSpec`; see [providers.md](providers.md).

## Key Files

| File | Purpose |
|---|---|
| `core/neural_state.py` | 6-dim emotion state, decay, bcWindow |
| `core/engine.py` | Stira circuit breaker, layer protection |
| `middleware/` | Pre/post hooks for Strands Agent lifecycle |
| `tools/` | Strands-compatible memory read/write tools |
| `agent/` | NXP-enhanced Agent subclass |
| `tools/workspace_artifacts.py` | Workspace artifacts: what `file_write` / `editor` wrote, as versions (#454, [ADR 0020](adr/0020-workspace-artifacts.md)); a fourth layer beside the three-layer memory |
| `heartbeat/` | Model-free tick (decay, snapshot, breaker query) and its opt-in daemon |

## Relationship to Existing Repos

- **Stira** (earlier engine, not published) → core engine algorithms. StiraCore integrates these.
- An earlier OpenClaw adapter (not published) → test patterns reused.
- A decision router prototype (not published) → may become a Strands Tool.


## Diagrams

Visual references live in [docs/diagrams/](./diagrams/) (SVG, GitHub light/dark aware):

- **#63 Neural State freeze** — [sequence (before/after)](./diagrams/63-sequence-before-after.svg), [detector flow](./diagrams/63-stimulus-detector-flow.svg)
- **Circuit Breaker** — [state machine](./diagrams/statemachine-circuit-breaker.svg) (NORMAL/WARNING/CRITICAL/COHERENCE, hysteresis)
- **Turn pipeline** — [pre_turn -> model -> detect -> post_turn](./diagrams/pipeline-turn-processing.svg)
- **Package layout** — [components](./diagrams/architecture-components.svg)

See [docs/diagrams/README.md](./diagrams/README.md) for details.