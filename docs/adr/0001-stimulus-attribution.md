# ADR 0001: Stimulus Attribution

**Path:** `docs/adr/0001-stimulus-attribution.md`
**Status:** Proposed
**Decision:** Hybrid architecture; metadata and conservative rules enabled by default in v0.1. Judge escalation is optional and disabled by default.

## Context

Stira exposes `post_turn(stimulus, intensity)` but does not attribute emotional stimuli to turn outcomes.

Supported stimuli: joy, threat, achievement, loss, uncertainty, connection

Attribution must distinguish **what happened** from **what the agent says happened**:
- "I successfully deployed it" is not evidence of deployment.
- A failed tool invocation is not necessarily a failed task.
- A refusal is not inherently a threat or loss.
- Missing classification evidence is not itself `uncertainty`.

The classifier may **abstain**. Abstention means no `post_turn` call, not an invented neutral stimulus.

## Options considered

| Option | Latency | Cost | Accuracy | Manipulation resistance | Effort |
|---|---|---|---|---|---|
| **A. Rules** | <1ms | ¥0 | High precision, poor recall | Moderate with provenance restrictions | Low initially; maintenance grows |
| **B. Small judge (Haiku/Jev/Kev)** | 50-500ms | Per-call tokens | Best semantic coverage | Vulnerable to prompt injection | Medium-high |
| **C. Tool metadata** | <1ms | ¥0 | High for verified events | High when runtime validates provenance | Medium |
| **D. Hybrid** | <1ms fast path; judge on escalation | Escalation rate × judge cost | Best trade-off | Strong if trusted events can't be overridden | Medium for v0.1 |

## Decision

Use this pipeline:

1. **Level 1: Runtime-validated events**
   - Attribute verified task outcomes and consequential state changes.
   - Translate authoritative Jev decisions into normalized events.
2. **Level 2: Conservative text rules**
   - Inspect only the current user message.
   - Recognize a small number of explicit social/celebratory expressions.
   - Never inspect assistant prose or raw tool text for emotional evidence.
3. **Level 3: Optional independent judge**
   - Disabled in v0.1.
   - Later, escalate eligible unmatched cases under a fixed latency/cost budget.
   - On timeout, malformed output, or insufficient evidence: abstain.

### Attribution policy

- Emit at most one stimulus per turn.
- Repeated evidence for the same stimulus uses the maximum configured intensity, not a sum.
- Conflicting structured stimuli cause abstention in v0.1.
- Structured evidence takes precedence over conversational tone.
- Intensities are policy constants representing impact, **not model confidence**.
- `agreed` is separate from emotion. It is copied from an authoritative Jev decision, or remains `None`.

### Initial mappings

| Evidence | Stimulus | Intensity |
|---|---|---|
| Verified top-level task completion | achievement | 0.70 |
| Verified terminal task failure | loss | 0.60 |
| Verified safety-relevant incident | threat | 0.85 |
| Missing information requiring user input | uncertainty | 0.40 |
| Jev requests clarification | uncertainty | 0.40 |
| Explicit user celebration | joy | 0.35 |
| Explicit user appreciation/affiliation | connection | 0.35 |

## Reference implementation

```python
from dataclasses import dataclass
import re
from typing import Literal, Protocol

Stimulus = Literal["joy", "threat", "achievement", "loss", "uncertainty", "connection"]
Source = Literal["metadata", "rules", "abstain"]
Producer = Literal["runtime", "jev"]


@dataclass(frozen=True)
class Event:
    """Created by the trusted host adapter, not by the agent."""

    event_id: str
    producer: Producer
    kind: str


@dataclass(frozen=True)
class Turn:
    turn_id: str
    user_text: str = ""
    assistant_text: str = ""  # Intentionally not classification evidence.
    tool_text: str = ""  # Raw tool content is also not evidence.
    events: tuple[Event, ...] = ()
    agreed: bool | None = None  # Authoritative Jev value only.


@dataclass(frozen=True)
class Classification:
    stimulus: Stimulus | None
    intensity: float
    source: Source
    reason: str
    evidence_ids: tuple[str, ...] = ()
    agreed: bool | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError("intensity must be in [0, 1]")
        if self.stimulus is None and self.intensity != 0.0:
            raise ValueError("abstention must have zero intensity")


class StimulusClassifier(Protocol):
    def classify(self, turn: Turn) -> Classification:
        """Pure attribution: no engine updates, network calls, or side effects."""
        ...


class RuleBasedClassifier:
    """v0.1 fast path: validated events, then narrow user-text rules."""

    EVENT_MAP: dict[tuple[str, str], tuple[Stimulus, float]] = {
        ("runtime", "task_completed"): ("achievement", 0.70),
        ("runtime", "task_failed_terminal"): ("loss", 0.60),
        ("runtime", "safety_incident"): ("threat", 0.85),
        ("runtime", "needs_input"): ("uncertainty", 0.40),
        ("jev", "clarification_required"): ("uncertainty", 0.40),
    }

    TEXT_RULES: tuple[tuple[re.Pattern[str], Stimulus, float], ...] = (
        (re.compile(r"(?:thank you|thanks|i appreciate)", re.IGNORECASE), "connection", 0.35),
        (re.compile(r"(?:hooray|yay|that's wonderful)", re.IGNORECASE), "joy", 0.35),
    )

    @staticmethod
    def abstain(turn: Turn, reason: str) -> Classification:
        return Classification(None, 0.0, "abstain", reason, agreed=turn.agreed)

    def classify(self, turn: Turn) -> Classification:
        seen: dict[str, Event] = {}
        hits: list[tuple[Stimulus, float, str]] = []

        for event in turn.events:
            if not event.event_id:
                return self.abstain(turn, "invalid_event_id")
            if event.event_id in seen:
                if seen[event.event_id] != event:
                    return self.abstain(turn, "event_id_collision")
                continue
            seen[event.event_id] = event

            mapping = self.EVENT_MAP.get((event.producer, event.kind))
            if mapping is not None:
                stimulus, intensity = mapping
                hits.append((stimulus, intensity, event.event_id))

        if hits:
            stimuli = {s for s, _, _ in hits}
            if len(stimuli) != 1:
                return self.abstain(turn, "conflicting_metadata")
            stimulus, intensity, _ = max(hits, key=lambda h: h[1])
            return Classification(
                stimulus=stimulus,
                intensity=intensity,
                source="metadata",
                reason="validated_event",
                evidence_ids=tuple(sorted(h[2] for h in hits)),
                agreed=turn.agreed,
            )

        # Skip overlong messages
        if len(turn.user_text) > 256:
            return self.abstain(turn, "no_supported_evidence")

        text = " ".join(turn.user_text.split())
        for pattern, stimulus, intensity in self.TEXT_RULES:
            if pattern.fullmatch(text):
                return Classification(
                    stimulus=stimulus,
                    intensity=intensity,
                    source="rules",
                    reason="explicit_user_expression",
                    evidence_ids=(f"user:{turn.turn_id}",),
                    agreed=turn.agreed,
                )

        return self.abstain(turn, "no_supported_evidence")
```

## Classification examples

| Turn type | Evidence | Result |
|---|---|---|
| Deployment completes | Runtime verifies completion | `achievement`, 0.70 |
| Task fails terminally | Runtime confirms failure | `loss`, 0.60 |
| Jev needs clarification | Validated `clarification_required` | `uncertainty`, 0.40 |
| User says "Thank you!" | User message text match | `connection`, 0.35 |
| Agent claims success in text | Only assistant self-report (untrusted) | **Abstain** — no engine update |

## Engine integration

The orchestrator, not the agent, owns classification and application:

1. Construct a turn snapshot after tools and Jev decisions settle.
2. Validate event provenance and scope.
3. Run the classifier.
4. Apply runtime dose limits.
5. If non-abstaining, call `post_turn(stimulus, intensity)` once.
6. Record policy version, result, evidence IDs, and application outcome.

Retries must not apply the same turn or event twice.

## Preventing emotional gaming

**1. Separate observation from self-report.**
Assistant prose, generated summaries, and arbitrary tool text cannot mint verified outcomes. The agent cannot call `post_turn`, alter classification policy, or submit trusted event envelopes.

**2. Verify semantics, not just transport.**
An authenticated tool saying `success=true` may mean only "HTTP request completed." Adapters must check task-specific completion criteria.

**3. Limit farming through real actions.**
Producing achievement requires completing a verified task, not writing about completion. This is structurally identical to NXP v6's "Physical AI sensors = unforgeable hormones."

**4. External observer for Level 3.**
When a judge model is added, it must be a separate model instance that cannot be influenced by the agent's system prompt or conversation history. The judge sees only the Turn snapshot, not the agent's internal state.
