"""Provenance of procedural-memory entries (#144).

Procedural entries are put into the system prompt as rules, so an entry counts
only when the host-side approval wrote it (``control/procedural_review.py``,
``control/procedural_cli.py``). That approval adds the provenance fields below;
an entry without them (written by hand, by older versions, or a proposal still
waiting under ``staging/``) is not read into the prompt.

The approval fields are reserved: a proposal that already carries one of them is
refused, and only the approval itself writes them (the same idea as the score
fields of #293). This is provenance, not a signature: whoever can write the
procedural directory directly can write these fields too. The agent's tools
cannot (``memory_write`` writes episodic only; the file tools have
``memory/procedural`` on their floor; see docs/tool-execution-paths.md for the
paths that are not covered).

Pure functions only; no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

# Proposals wait under ``procedural/staging/`` (the protected procedural subtree).
STAGING_PREFIX = "staging/"
# Required on every procedural entry the prompt may use.
PROVENANCE_KEYS = ("source_turn", "approved_by", "approved_at")
REVIEW_APPROVED = "approved"
# Written only by the host-side approval. ``source_turn`` is given by the proposer
# as an argument, never through free-form metadata.
RESERVED_APPROVAL_KEYS = frozenset(
    {"approved_by", "approved_at", "review", "reviewed_at", "previous_sha256"}
)
# ``revises_sha256`` (#463): set by the host when a proposal revises an approved rule (the
# SHA-256 of the rule it was proposed against); never taken from free-form metadata.
RESERVED_PROPOSAL_KEYS = RESERVED_APPROVAL_KEYS | {"source_turn", "revises_sha256"}
# Fixed log/refusal code for an entry that is not read (no or invalid provenance).
E_PROCEDURAL_UNAPPROVED = "E_PROCEDURAL_UNAPPROVED"
# Logged when the bounded read window ended before enough approved rules were found.
E_PROCEDURAL_WINDOW_LIMIT = "E_PROCEDURAL_WINDOW_LIMIT"
MAX_FIELD_LENGTH = 200


def is_staged_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(STAGING_PREFIX)


def valid_field(value: Any) -> bool:
    """A non-empty, single-line string of bounded length (no control characters)."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_FIELD_LENGTH
        and value.isprintable()
    )


def _valid_timestamp(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not valid_field(value):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def provenance_problem(metadata: Any) -> str | None:
    """The first missing/invalid provenance field of an entry, or ``None`` if it counts."""
    if not isinstance(metadata, Mapping):
        return "metadata"
    if metadata.get("review") != REVIEW_APPROVED:
        return "review"
    for key in ("source_turn", "approved_by"):
        if not valid_field(metadata.get(key)):
            return key
    if not _valid_timestamp(metadata.get("approved_at")):
        return "approved_at"
    return None


__all__ = [
    "E_PROCEDURAL_UNAPPROVED",
    "E_PROCEDURAL_WINDOW_LIMIT",
    "MAX_FIELD_LENGTH",
    "PROVENANCE_KEYS",
    "RESERVED_APPROVAL_KEYS",
    "RESERVED_PROPOSAL_KEYS",
    "REVIEW_APPROVED",
    "STAGING_PREFIX",
    "is_staged_key",
    "provenance_problem",
    "valid_field",
]
