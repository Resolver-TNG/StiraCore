"""Fixed reasons an HITL request is raised with, and their UI codes (#279).

The ``reason`` sentence stays in the request, the approval store and the logs (audit
text, unchanged). The approval API adds ``reason_code`` so the UI can word it in the
operator's language; only these server-owned sentences map to a code (exact match), so
free text never becomes a code and an unknown reason keeps ``reason_code: None``.

``reason_params`` (an object, ``{}`` when there is none) carries the variables the
sentence needs (only ``level``), from an allowlist; the UI builds the text from the code
and these variables and never trusts a sentence it cannot match.

The same holds for the sentence a request is closed with by the server
(``resolution_reason`` in the history): ``resolution_reason_code`` is set only for
the sentences below and only when the server itself resolved the request
(resolver ``system``); an operator's own reject reason is never turned into a code.
"""

from __future__ import annotations

from typing import Any

CRITICAL_HALT = "CRITICAL HALT requires human approval for this write operation."
RULE_ASK = "Tool policy is Ask: every call of this tool needs a human approval."

_CODES = {
    CRITICAL_HALT: "breaker_critical_halt",
    RULE_ASK: "rule_ask",
}


# Breaker levels a coded reason may carry as a variable (BreakLevel values).
_LEVELS = frozenset({"normal", "warning", "critical", "coherence"})


# Sentences the server closes a request with (resolver "system").
EXPIRED = "approval expired"
CLOSED = "approval closed"
REPLACED = "replaced by a new request"
BINDING_MISMATCH = "binding mismatch"
LIVE_ONLY = "approved live only (secret arguments); not restorable"
RULES_CHANGED = "tool rules changed (#190)"
# ADR 0024 §3: a claimed resume found the breaker stricter than at request time.
BREAKER_STRICTER = "circuit breaker stricter than at request time"
# ADR 0024 §4: a job-origin request found pending at start (job runs end with the process).
JOB_RESTART = "job runs do not survive a restart"
# ADR 0011 W5: a grant iteration's card ended because the iteration stopped (kill, revoke,
# a person's chat turn, its wall time) or because the process restarted.
GRANT_STOPPED = "the grant iteration stopped"
# ApprovalStore records its own expiry with this word.
STORE_EXPIRED = "expired"

_RESOLUTION_CODES = {
    EXPIRED: "expired",
    STORE_EXPIRED: "expired",
    CLOSED: "closed",
    REPLACED: "replaced",
    BINDING_MISMATCH: "binding_mismatch",
    LIVE_ONLY: "live_only",
    RULES_CHANGED: "rules_changed",
    BREAKER_STRICTER: "breaker_stricter",
    JOB_RESTART: "job_restart",
    GRANT_STOPPED: "grant_stopped",
}
SYSTEM_RESOLVER = "system"
# Every code the two functions below can return (the UI and /observability check against these).
REASON_CODES = frozenset(_CODES.values())
RESOLUTION_CODES = frozenset(_RESOLUTION_CODES.values())


def reason_code(reason: Any) -> str | None:
    """The UI code for a server-owned HITL reason sentence, else ``None``."""
    return _CODES.get(reason) if isinstance(reason, str) else None


def reason_params(reason: Any, break_level: Any = None) -> dict[str, str]:
    """Variables for the UI wording of a coded reason (#279), from an allowlist.

    Only a server-owned sentence (one that has a ``reason_code``) gets variables, and the
    only variable is ``level`` (a breaker level word). Free text never becomes a
    variable; an unknown reason or level gives ``{}``.
    """
    if reason_code(reason) is None:
        return {}
    level = getattr(break_level, "value", break_level)
    level = level.lower() if isinstance(level, str) else None
    return {"level": level} if level in _LEVELS else {}


def resolution_code(reason: Any, resolver: Any) -> str | None:
    """The UI code for a server-owned resolution sentence, else ``None``.

    Only a request the server resolved (``resolver == "system"``) gets a code, so an
    operator's reject reason is shown as typed even if it equals one of the sentences.
    """
    if resolver != SYSTEM_RESOLVER or not isinstance(reason, str):
        return None
    return _RESOLUTION_CODES.get(reason)
