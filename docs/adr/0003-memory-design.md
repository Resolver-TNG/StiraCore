# ADR 0003: Three-Layer Memory Design

**Path:** `docs/adr/0003-memory-design.md`
**Status:** Accepted
**Decision:** Hybrid storage (MD正本 + SQLite index), agent-scoped, gradual search evolution

## Context

StiraCore needs a memory system matching NXP's 3-layer model (episodic/semantic/procedural). The harness must support 1M-context models (Opus 5.5 class) and assist agents in memory retrieval at startup and on-demand.

## Decisions

### Storage: Hybrid (Option C)
- **Markdown files are the source of truth** — human-readable, git-trackable, "memory is files, files are me"
- **SQLite as index/search layer** — keyword search in v0.1, vector search later
- Both kept in sync; MD is authoritative on conflict

### Three Layers

| Layer | Content | Write Policy | Lifetime |
|---|---|---|---|
| **Episodic** | Daily logs, turn results, session state | Agent auto-records | Short (decay target) |
| **Semantic** | Distilled knowledge, patterns, learnings | Agent proposes → operator approves (v0.1 manual) | Medium (importance-scored) |
| **Procedural** | Skills, ADRs, workflows, config | Operator only | Long (near-permanent) |
| **L3 Protected** | Identity Anchors, SOUL | **Immutable** (breaker-enforced) | Permanent |

### Promotion Policy
- v0.1: Manual (operator triggers episodic → semantic distillation)
- Future: LLM-judged promotion with operator approval gate

### Search Evolution
1. **v0.1:** Keyword/grep search on MD files
2. **v0.2:** SQLite FTS5 full-text search
3. **v0.3:** Embedding-based vector search (like OC's llama-server)
4. **v1.0:** NXP associative recall (multi-signal → Neural State weighting → use/discard) — opt-in, model-free, Episodic only since #170 ([associative-recall.md](../associative-recall.md))

### Permissions
- Episodic write: Agent (automatic, every turn)
- Semantic write: Agent proposes, operator approves
- Procedural write: Operator only
- L3 mutation: **Blocked** (StiraEngine enforces)
- All reads: Agent (via tools or startup injection)

### Physical Layout
```
agents/{agent_id}/
├── soul.md                    # SOUL definition (v0.1: template only)
├── memory/
│   ├── episodic/              # Daily logs, session results
│   │   └── 2026-09-27.md
│   ├── semantic/              # Distilled knowledge
│   │   └── patterns.md
│   └── procedural/            # Skills, ADRs, workflows
│       └── rules.md
└── data/
    └── memory.db              # SQLite search index
```

Agent-scoped: each agent has its own memory directory. No cross-agent access in v0.1.

### Context Embedding (1M model assumed)

| Phase | What | Where | Size Budget |
|---|---|---|---|
| Startup | SOUL + Identity Anchors + procedural rules | system_prompt (fixed) | ~5K tokens |
| Per-turn | Stira state + latest 3 episodic entries | system_prompt (dynamic) | ~3K tokens |
| On-demand | memory_read/memory_search tools | tool results | As needed |

The harness manages context assembly:
1. Load SOUL → system_prompt head
2. Load procedural essentials → system_prompt
3. Pre-turn: inject Stira state + recent episodic
4. Agent uses memory tools for deeper retrieval

### Memory Retrieval Assistance
The harness actively helps agents find relevant memories:
- Startup loader selects recent + high-importance entries
- Search tools abstract away the storage backend
- Future: harness suggests relevant memories based on turn context (proactive recall)

---

## Amendment: Nightly Distillation Policy (2026-09-27)

**Decision:** A-base + B-hybrid (auto-promote with staging for edge cases)

### Promotion Rules

| Importance Score | Action | Destination |
|---|---|---|
| Low–Medium (below threshold) | **Auto-promote** | `semantic/` direct write |
| High (above threshold) | **Stage for review** | `staging/` + morning report |
| L3 Protected | **Never touch** | Breaker-enforced block |

### Nightly Distillation Flow

```
cron 2:00 AM → NXPAgent(model=Haiku, isolated session)
  → Read today's episodic entries
  → Score importance (Amygdala Pass)
  → Auto-promote routine entries to semantic/
  → Stage ambiguous entries to staging/
  → Write distillation report to episodic/
  → Emit heartbeat: "distilled N auto, M staged"

Morning → Commander reviews staging/ → approve or reject
```

### Additional Components Required
1. `staging/` directory in MemoryStore
2. Importance scoring function (Amygdala Pass)
3. Cron configuration (nightly distillation + HB scheduler)
4. Morning report generator
5. Isolated health session (never use 1M production context)

### Cost
- Haiku 4.5 for distillation: ~$3.47/month (including HB)
- Independent session prevents context cost explosion

## Amendment: Harvest marks keep the Episodic order (#292, 2026-10-16)

`MemoryStore.list_entries` orders by file modification time (newest first, then key),
and the prompt's Episodic section (`ContextAssembler`) takes the newest entries from it.
That is **not** creation order: an ordinary write or rewrite of an episode still updates
its time and moves it to the front. Only the Harvest marks (`harvested`, `harvest_*`,
metadata) keep the time: `HarvestScheduler` rewrites an episode with
`write(..., preserve_mtime=True)`, which sets the previous time on the open temporary file
(by descriptor) before it is fsynced, closed and atomically renamed into place, then
fsyncs the directory. Marking therefore never moves an episode and the entry is never
visible with a new time; if setting the time fails, nothing is replaced. On Windows
(no descriptor-based `utime`) the time is set by path after close, only while the path is
still the file just written (best effort).

Constraints: (a) a change to the entry between reading its time and the replace is not
detected — this relies on a single writer (the data-directory owner lock, #285/#321, and
not running `distillation_cli` while the server is running). (b) A hard kill between
creating and renaming the temporary file leaves a `.memory-*.tmp` next to the entries; it
is not an entry (only `*.md` is listed, counted, searched, indexed or harvested; measured)
and is not removed automatically.

Chosen over a sidecar/index for the marks (a second source of truth next to the Markdown)
and over ordering by key (episode keys are caller-chosen).
