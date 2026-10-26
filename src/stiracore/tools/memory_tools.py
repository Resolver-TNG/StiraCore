"""Strands tools bound to an individual agent's MemoryStore."""

from __future__ import annotations

import sqlite3

from strands import tool

from stiracore.memory_origin import ORIGIN_MODEL, record_origin

from .memory_store import MemoryEntry, MemoryLayer, MemoryStore


def _parse_layer(value: str) -> MemoryLayer:
    try:
        return MemoryLayer(value.strip().lower())
    except ValueError as error:
        raise ValueError("Layer must be episodic, semantic, or procedural.") from error


def _format_entry(entry: MemoryEntry) -> str:
    header = f"[{entry.layer.value}/{entry.key}]\nModified: {entry.modified_at.isoformat()}"
    if entry.metadata:
        header += f"\nMetadata: {entry.metadata}"
    return f"{header}\n\n{entry.content}"


def create_memory_tools(store: MemoryStore) -> list:
    """Create independently bound tools without process-global store state."""

    @tool
    def memory_read(layer: str, key: str) -> str:
        """Read a memory entry.

        Args:
            layer: episodic, semantic, or procedural.
            key: Memory filename stem, without the .md extension.
        """
        try:
            parsed_layer = _parse_layer(layer)
            entry = store.read(parsed_layer, key)
            if entry is None:
                return f"Memory not found: {parsed_layer.value}/{key}"
            return _format_entry(entry)
        except (ValueError, OSError, sqlite3.Error) as error:
            return f"Memory read failed: {error}"

    @tool
    def memory_write(layer: str, key: str, content: str) -> str:
        """Write or replace an episodic memory.

        Args:
            layer: Must be episodic; other layers are read-only in v0.1.
            key: Memory filename stem, without the .md extension.
            content: Markdown content to store.
        """
        try:
            parsed_layer = _parse_layer(layer)
            if parsed_layer is not MemoryLayer.EPISODIC:
                return (
                    "Memory write denied: only episodic memories may be written by tools in v0.1."
                )
            # Provenance (#294): text the agent (a model) wrote with this tool, not a
            # turn recorded by the server (``source: web-chat``, ``origin: extracted``).
            store.write(
                parsed_layer, key, content, {"source": "memory_write", "origin": ORIGIN_MODEL}
            )
            record_origin(store, parsed_layer, key, ORIGIN_MODEL, "memory_write")
            return f"Memory written: {parsed_layer.value}/{key}"
        except (ValueError, OSError, sqlite3.Error) as error:
            return f"Memory write failed: {error}"

    @tool
    def memory_search(query: str, layer: str = "all", limit: int = 10) -> str:
        """Search memory using case-insensitive literal text matching.

        Args:
            query: Text to find in memory keys, content, or metadata.
            layer: all, episodic, semantic, or procedural.
            limit: Maximum number of results.
        """
        try:
            layers = None if layer.strip().lower() == "all" else [_parse_layer(layer)]
            entries = store.search(query, layers=layers, limit=limit)
            if not entries:
                return "No matching memories found."
            return "\n\n---\n\n".join(_format_entry(entry) for entry in entries)
        except (ValueError, OSError, sqlite3.Error) as error:
            return f"Memory search failed: {error}"

    @tool
    def memory_list(layer: str, limit: int = 20) -> str:
        """List a layer's memories, newest first.

        Args:
            layer: episodic, semantic, or procedural.
            limit: Maximum number of entries.
        """
        try:
            parsed_layer = _parse_layer(layer)
            entries = store.list_entries(parsed_layer, limit=limit)
            if not entries:
                return f"No memories found in {parsed_layer.value}."
            lines = [
                f"- {entry.layer.value}/{entry.key} (modified {entry.modified_at.isoformat()})"
                for entry in entries
            ]
            total = store.count_entries(parsed_layer)
            if total > len(entries):
                # #574: a cut list must not read as "everything" (the default limit is 20).
                lines.append(
                    f"(showing the newest {len(entries)} of {total}; "
                    "pass a larger limit to see more)"
                )
            return "\n".join(lines)
        except (ValueError, OSError, sqlite3.Error) as error:
            return f"Memory list failed: {error}"

    # The bounded index (ADR 0026 draft, wave 23): read-only and pathless like memory_list.
    from .memory_index import create_memory_index_tool

    return [memory_read, memory_write, memory_search, memory_list, create_memory_index_tool(store)]
