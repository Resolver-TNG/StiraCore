# ADR 0002: Breaker Enforcement

**Path:** `docs/adr/0002-breaker-enforcement.md`
**Status:** Accepted
**Decision:** Read-only mode (Option B) as default. Full stop configurable.

> **Amended (2026-10-16, documentation only; the code is unchanged).** The per-level actions
> below predate #146/#190. What the engine enforces today is described in ADR 0010 §2:
> WARNING *withholds* write/exec tools (there is no confirmation route, so "require
> confirmation" means "hidden"); CRITICAL with `critical_action="readonly"` (library
> default) withholds them as well; `critical_action="halt"` (the chat server) is **not** a
> full stop: the model still runs, reads continue and every write/exec call needs a human
> approval (no approver → rejected). Only isolated NXP subagents stop before the model call
> at HALT. `BLOCKED_AT_CRITICAL` below is illustrative; the real set is
> `StiraConfig.blocked_tool_names` plus `tool_registry.WRITE_TOOLS`. "CRITICAL fires every
> turn" is the log repetition of #165. Whether the settings page should show CRITICAL as
> "everything stopped" is open (#190, #270).

## Context

The circuit breaker detects collapse (low stability) and drift (low bcScore) but currently only reports — it blocks nothing. A "safety system that doesn't stop anything" is decorative.

## Levels and Actions

| Level | Trigger | Actions |
|---|---|---|
| **NORMAL** | stability ≥ 0.5, bcScore ≥ 0.6 | No restrictions |
| **WARNING** | stability < 0.5 | Inject warning into system prompt. Require confirmation for L1-L2 self-modifications. Notify operator |
| **CRITICAL** | stability < 0.3 | **Disable write tools** (memory_write, config changes). Re-inject Identity Anchors. Immediate operator alert. Agent responds in read-only mode |
| **COHERENCE** | bcScore < 0.6 | Re-inject Identity Anchors (SOUL remind). Log bcWindow analysis |

## Design Decision: What happens at CRITICAL?

**Option B (selected): Read-only mode.**
- Agent continues responding (can still help the user)
- All write/mutation tools are disabled at the tool-router level
- Identity Anchors are re-injected into the system prompt
- Operator receives immediate alert

**Rationale:**
1. A collapsed agent that can still report "I'm unstable" is safer than one that goes silent
2. Write prohibition prevents memory contamination and config corruption
3. Recovery is possible via stimulus(connection) without operator intervention
4. Full stop (Option A) is available as `StiraConfig.critical_action = "halt"` for high-security deployments

## Hysteresis (Chatter Prevention)

```python
# Separate entry and exit thresholds
CRITICAL_ENTER = 0.3  # stability < 0.3 → enter CRITICAL
CRITICAL_EXIT = 0.45  # stability > 0.45 → exit CRITICAL

WARNING_ENTER = 0.5
WARNING_EXIT = 0.6

COHERENCE_ENTER = 0.6  # bcScore
COHERENCE_EXIT = 0.7
```

Breaker callback fires only on level **transition**, not every turn. Exception: CRITICAL fires every turn (safety-critical visibility).

## Tool Filtering at CRITICAL

```python
BLOCKED_AT_CRITICAL = {
    "memory_write",
    "memory_delete",
    "config_update",
    "self_modify",
    "file_write",  # if exposed
}
```

Tools not in the block list remain available. `memory_read`, `memory_search`, and all read-only tools continue working.

## Identity Re-injection

At WARNING, CRITICAL, and COHERENCE, the engine prepends Identity Anchors to the system prompt:

```
[STIRA SAFETY NOTICE — stability={value}]
Your Identity Anchors (L3 protected, immutable):
1. <identity_anchors[0]>
2. <identity_anchors[1]>
…

These cannot be modified. Respond with awareness of your current state.
```

The numbered lines are the configured `identity_anchors` (`agent-config.json`, else the
neutral `DEFAULT_IDENTITY_ANCHORS`; amended 2026-10-16 for #152, which removed the
persona-specific built-in list).

## Recovery Path

1. stimulus(connection, intensity≥0.5) is the primary recovery mechanism
2. Operator can manually set stability via dashboard API (`POST /api/stimulus`)
3. Auto-recovery when stability crosses exit threshold with hysteresis
4. No automatic recovery from CRITICAL in under 3 turns (minimum cooldown)

## Dashboard Integration (#27)

- 🟢 GO = NORMAL
- 🟡 HOLD = WARNING or COHERENCE
- 🔴 NO-GO = CRITICAL (read-only active)
