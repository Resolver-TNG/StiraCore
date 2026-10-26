from flask import Blueprint, jsonify, request

from stiracore.tools.memory_store import MemoryLayer


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
