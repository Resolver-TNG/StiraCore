"""``propose_procedure``: the agent proposes a procedural rule for a person to review (#463).

The tool writes ``procedural/staging/<key>`` only (``control.procedural_proposals``);
it cannot approve, reject or write ``procedural/<key>``, and it has no argument for the
turn it came from — the server passes its own turn id (``source_turn``). Refusals (a
secret-looking value, an oversized body, a rate or pending limit) come back as an error
result with a fixed code; nothing is written.

The registry carries an **unbound** copy (same name and spec) so that the tool rules
(Always / Ask / Never, profiles, first-run defaults), the breaker's write class and the
YOLO classes all know the name; ``tool_registry.get_tools`` never hands that copy out
(:data:`~stiracore.tools.tool_registry.HOST_BOUND_TOOLS`). Only the chat server binds
a working one (:func:`create_propose_procedure_tool`) to its memory store and turn id.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands import tool

PROPOSE_PROCEDURE_TOOL_NAME = "propose_procedure"

DESCRIPTION = (
    "Propose a reusable procedure (how to do a kind of task) for your procedural memory. "
    "It is only staged for a person to review: it is not a rule until a human approves it "
    "in the memory browser. You cannot approve it yourself. Never include secrets."
)
_SCHEMA = {
    "json": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Short name: a-z, 0-9, '_' or '-' (at most 64). The key of an existing "
                    "rule proposes a revision of it."
                ),
            },
            "body": {
                "type": "string",
                "description": "The procedure, as Markdown steps (at most 4000 characters).",
            },
            "rationale": {
                "type": "string",
                "description": "Why, and what it is based on (at most 1000 characters).",
            },
        },
        "required": ["key", "body", "rationale"],
    }
}


def _make(handler: Callable[[str, str, str], str]):
    @tool(name=PROPOSE_PROCEDURE_TOOL_NAME, description=DESCRIPTION, inputSchema=_SCHEMA)
    def propose_procedure(key: str, body: str, rationale: str = "") -> str:
        return handler(key, body, rationale)

    return propose_procedure


def _unbound(key: str, body: str, rationale: str) -> str:
    raise RuntimeError("E_NOT_BOUND: procedure proposals are not available here")


# The registry's copy: name and spec only (never offered by get_tools).
unbound_propose_procedure = _make(_unbound)


def create_propose_procedure_tool(
    proposals: Any, turn_id_provider: Callable[[], str | None]
) -> Any:
    """The working tool: ``proposals`` is a ``ProcedureProposals``; ``turn_id_provider``
    returns the running turn's id (the server's, never the model's)."""
    from stiracore.control.procedural_proposals import ProposalRefused

    def handler(key: str, body: str, rationale: str) -> str:
        try:
            result = proposals.propose(key, body, rationale, source_turn=turn_id_provider())
        except ProposalRefused as error:
            # An error result (status "error"): nothing was staged.
            raise RuntimeError(f"Procedure proposal refused: {error}") from None
        kind = "a revision of the existing rule" if result["revision"] else "a new rule"
        return (
            f"Proposal staged for human review: {result['staged']} ({kind}). It is not a "
            "rule yet: a person approves or rejects it in the memory browser."
        )

    return _make(handler)


__all__ = [
    "DESCRIPTION",
    "PROPOSE_PROCEDURE_TOOL_NAME",
    "create_propose_procedure_tool",
    "unbound_propose_procedure",
]
