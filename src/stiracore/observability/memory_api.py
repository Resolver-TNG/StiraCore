"""Memory browser routes.

``create_memory_blueprint`` (``/api/memory/browser``) returns entry bodies and raw front
matter; neither server registers it.

``create_memory_table_blueprint`` (#652) is the read-only table behind the Memory page:

    GET /api/memory/entries?layer=episodic|semantic|procedural[&limit=1..100]

One row per entry with an allow-list of fields — key, importance, retention — and never
the body or the raw front matter (:func:`memory_rows`). Listing does not access-touch.
"""

import math
from typing import Any

from flask import Blueprint, jsonify, request

from stiracore.memory_importance import (
    DERIVED,
    HALF_LIFE_FIELD,
    RETENTION_FIELD,
    binding_state,
    retention_binding,
    retention_of,
)
from stiracore.memory_rating import harness_rule_importance
from stiracore.observability.masking import mask_secrets
from stiracore.procedural_provenance import STAGING_PREFIX, is_staged_key
from stiracore.tools.memory_store import MemoryLayer

ENTRIES_ROUTE = "/api/memory/entries"
ROWS_DEFAULT = 50
ROWS_MAX = 100
KEY_MAX_CHARS = 200
# importance_state: where the number comes from. ``verified`` = the server's episode writer
# recorded it for this exact text (#506); ``recorded`` = front matter of a layer the agent's
# file tools cannot write; ``unverified`` = an Episodic claim (the number is not shown).
# retention.state: ``default`` (untagged), ``tagged`` (Semantic / Procedural front matter),
# ``verified`` / ``changed`` (a human's Episodic tag, #173), ``unverified`` (an Episodic
# front-matter claim nobody bound: it holds nothing), ``invalid`` (outside the contract).


def _importance(store, layer: MemoryLayer, key: str, text: str, metadata: dict) -> tuple:
    if layer is MemoryLayer.EPISODIC:
        value = harness_rule_importance(store, layer, key, text)
        if value is not None:
            return round(value, 2), "verified"
        return None, ("unverified" if "importance" in metadata else "none")
    value = metadata.get("importance")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    ):
        return round(float(value), 2), "recorded"
    return None, "none"


def _retention(store, layer: MemoryLayer, key: str, text: str, metadata: dict) -> dict:
    if layer is MemoryLayer.EPISODIC:
        # The front matter here is agent-writable: only a tag a human bound counts (#173).
        bound = retention_binding(store, layer, key)
        state = binding_state(bound, text)
        if state is not None and bound is not None:
            return {"kind": bound[0], "half_life_days": None, "state": state}
        claimed = RETENTION_FIELD in metadata or HALF_LIFE_FIELD in metadata
        return {
            "kind": DERIVED,
            "half_life_days": None,
            "state": "unverified" if claimed else "default",
        }
    tag = retention_of(metadata)
    state = "invalid" if tag.invalid else ("tagged" if tag.tagged else "default")
    # Half-lives apply to the Semantic layer only (memory_importance).
    semantic = layer is MemoryLayer.SEMANTIC and tag.kind == DERIVED
    return {
        "kind": tag.kind,
        "half_life_days": tag.half_life_days if semantic else None,
        "state": state,
    }


def memory_rows(store, layer: MemoryLayer, limit: int = ROWS_DEFAULT) -> dict[str, Any]:
    """The newest ``limit`` entries of ``layer`` as allow-listed rows (no body, #652).

    ``{"layer", "entries": [{"key", "importance", "importance_state", "retention"}],
    "total", "limit"}``. The key goes through :func:`mask_secrets` and is cut at
    :data:`KEY_MAX_CHARS`. Staged procedural proposals are not rules and are left out, as
    in ``/api/memory/tree``.
    """
    layer = MemoryLayer(layer)
    procedural = layer is MemoryLayer.PROCEDURAL
    rows = []
    for entry in store.list_entries(layer, limit=limit):
        key = entry.key
        if procedural and is_staged_key(key):
            continue
        text = store.read_text(layer, key)
        if text is None:
            continue
        try:
            _content, metadata = store._parse(text)
        except ValueError:
            continue
        importance, importance_state = _importance(store, layer, key, text, metadata)
        rows.append(
            {
                "key": str(mask_secrets(key))[:KEY_MAX_CHARS],
                "importance": importance,
                "importance_state": importance_state,
                "retention": _retention(store, layer, key, text, metadata),
            }
        )
    total = store.count_entries(layer, exclude_prefix=STAGING_PREFIX if procedural else None)
    return {"layer": layer.value, "entries": rows, "total": total, "limit": limit}


def create_memory_table_blueprint(store):
    """Read-only ``GET /api/memory/entries`` (#652); refusals carry a fixed code."""
    bp = Blueprint("memory_table", __name__)

    @bp.get(ENTRIES_ROUTE)
    def memory_entries():
        try:
            layer = MemoryLayer(request.args.get("layer", MemoryLayer.EPISODIC.value))
        except ValueError:
            return jsonify({"error": "invalid_layer", "code": "invalid_layer"}), 400
        raw = request.args.get("limit", str(ROWS_DEFAULT))
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 3 and 1 <= int(raw) <= ROWS_MAX):
            return jsonify({"error": "invalid_limit", "code": "invalid_limit"}), 400
        return jsonify(memory_rows(store, layer, int(raw)))

    return bp


def create_memory_blueprint(store):
    bp = Blueprint("memory_api", __name__)

    @bp.route("/api/memory/browser", methods=["GET"])
    def browse_memory():
        layer = request.args.get("layer", "episodic")
        limit = int(request.args.get("limit", 50))
        query = request.args.get("q", "").strip()

        try:
            if query:
                results = store.search(query)
            else:
                results = store.list_entries(MemoryLayer(layer), limit)
        except ValueError:
            return jsonify({"error": f"invalid layer: {layer}"}), 400

        entries = [
            {
                "key": getattr(r, "key", None),
                "content": getattr(r, "content", None),
                "metadata": getattr(r, "metadata", None),
            }
            for r in results[:limit]
        ]
        return jsonify(entries)

    return bp
