"""Flask endpoints for persisted observability events and SSE replay."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .event_store import SQLiteEventStore, event_to_dict

_POLL_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0
_BATCH_SIZE = 100


def create_sse_blueprint(event_store: SQLiteEventStore) -> Blueprint:
    """Create observability endpoints backed by a shared event store.

    For an app that writes a hash-chained ``SQLiteEventStore`` and wants it streamed. No app in
    this repository uses it today: the chat server reads ``turns.db`` (``turns_view``) and
    ``dashboard/app.py`` serves no event stream. It does not serve ``GET /api/turns`` or ``GET /api/turns/<turn_id>/events``: the
    chat API owns them (``turns.db`` is the turn source of truth, #286), and the chat server reads
    ``/api/observability/*`` from ``turns_view`` instead.

    The SSE endpoint accepts Last-Event-ID, or last_id/since_id query
    parameters for clients that cannot set that header. Without a cursor,
    it replays retained events before following live events.

    Deploy SSE with streaming-capable workers and proxy buffering disabled.
    The application, not an individual request, owns the event store.
    """
    blueprint = Blueprint("observability", __name__)

    @blueprint.get("/api/observability/events")
    def stream_events() -> Response | tuple[Response, int]:
        last_id = request.headers.get("Last-Event-ID")
        if not last_id:
            last_id = request.args.get("last_id") or request.args.get("since_id")

        if last_id is not None and any(character in last_id for character in ("\r", "\n", "\0")):
            return jsonify(error="Invalid event cursor."), 400

        @stream_with_context
        def generate() -> Iterator[str]:
            cursor = last_id
            last_heartbeat = time.monotonic()

            # Flush headers even when no events have been recorded.
            yield ": connected\n\n"

            while True:
                events = event_store.get_events(
                    since_id=cursor,
                    limit=_BATCH_SIZE,
                )
                for event in events:
                    payload = event_to_dict(event)
                    event_id = payload["event_id"]
                    payload["id"] = event_id
                    data = json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    cursor = event_id
                    yield f"id: {event_id}\ndata: {data}\n\n"

                now = time.monotonic()
                if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

                # Drain backlog immediately; poll once per second when caught up.
                if len(events) < _BATCH_SIZE:
                    time.sleep(_POLL_SECONDS)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @blueprint.get("/api/observability/snapshot")
    def snapshot() -> Response:
        response = jsonify(event_store.get_snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint


__all__ = ["create_sse_blueprint"]
