# Checkpoint persistence

## Requirements and implementation (#95)

- [x] Snapshot NeuralState, conversation, pending tool calls, timestamp and thread.
- [x] Atomic JSON storage and bounded generations (default: 20 per thread).
- [x] Save before a turn can HALT and before SDK tool execution.
- [x] Explicit restoration, retaining breaker cooldown/hysteresis and state references.
- [x] Offline tests with the real SDK: stored tool execution without regenerating it.

`StiraEngine` lives in `core/engine.py` (not `core/stira_engine.py`).
`pre_turn()` saves after decay/breaker evaluation and before operator callbacks.
`NXPAgent` supplies the current conversation **including the unexecuted prompt**.
`CheckpointHooks` saves at `BeforeToolsEvent`, before any tool in the batch starts.
This is deliberately a batch boundary: SDK tools may run in parallel and cannot
be given a meaningful single-tool total order. Delegated SDK agents use the same
hook. Checkpointing does not advance the cooldown a second time within a turn.

## Usage

```python
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import FileCheckpointStore

store = FileCheckpointStore(".local/stira/checkpoints", max_checkpoints=20)
agent = NXPAgent(
    model_id="your-model-id",
    region_name="us-east-1",
    agent_id="assistant",  # thread_id; give separate agents distinct IDs
    checkpoint_store=store,
)

# Inspect retained generations (newest first); restore only after reviewing
# whether repeating their pending operations is safe.
checkpoint_ids = store.list("assistant")
checkpoint = agent.resume_from_checkpoint(checkpoint_ids[0])
result = agent()  # None continues restored messages; no duplicate user prompt
```

The engine-only API `engine.resume_from_checkpoint(id)` restores its state,
`conversation_history`, and `pending_tool_calls`; a custom host remains responsible
for passing that conversation to its executor and enforcing `pre_turn()` actions.
The wrapper API additionally restores the SDK conversation. It overrides automatic
latest-session loading, but rebuilds the system prompt from the **current trusted**
provider: old prompts/SOUL are not restored from conversation data.

## Safety and recovery contract

- Replay is **at least once**, not exactly once. A crash after one parallel tool
  succeeds can replay that tool with the rest of its batch. Use idempotency keys
  for external writes. Explicit restore is not an approval mechanism.
  HITL-approved operations are deduplicated by the operation ledger (committed
  ones are skipped, unknown outcomes are reconciled or refused):
  see `docs/hitl-approvals.md` (#146). Since ADR 0024 phase 1b the same holds for
  every write-capable call that ran without an approval (rule Always) and for
  `delegate`; only read-only calls run again.
- HALT checkpoints remain halted if policy still requires it. CRITICAL retains
  the existing configurable `readonly`/`halt` policy; this feature does not change
  the default to a universal HALT. Cooldown and hysteresis survive restoration.
- A store failure aborts the turn/tool batch rather than executing without a
  recoverable checkpoint. Missing or malformed checkpoints raise; no default
  NeuralState is silently substituted.
- JSON supports binary image/document blocks through explicit base64 encoding.
  Files include full private conversation content, not redacted logs. New files
  and directories use owner-only POSIX permissions; storage is not encrypted.
- IDs are opaque UUID hex strings, not filesystem paths. Cross-agent restore is
  rejected. Files under the store are trusted local application data, not an
  import format for untrusted user uploads.
- Retention is per thread; `store.cleanup(thread_id)` also applies a changed
  retention limit. One sequential writer per thread is expected. Separate agents
  sharing a store require unique `agent_id` values.
- No UI/approval transport, scheduler, transaction rollback, or replay of arbitrary
  Python stack frames is provided. These checkpoints are the foundation for HITL.

## Verification

Local suite: 661 passed, 1 existing skip; Ruff check and format check pass.
Tests cover exact round trips, multigenerational cleanup, per-thread isolation,
atomic-write failure, permissions, HALT prompt retention, cooldown restoration,
multimodal bytes, path traversal, crash snapshots and real-SDK tool replay.
