"""#195: token usage totals and the per-model breakdown (``GET /api/observability/usage``).

Counts come from ``turn.completed`` rows of ``turns.db``. A count nobody reported is
``None`` ("unknown" on the page), never 0; only numbers and a checked model id leave.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability.turn_events import TurnEventStore
from stiracore.observability.turns_view import (
    create_turns_observability_blueprint,
    usage_summary,
)

SECRET = "sk-test-usage-0123456789abcdef"


def test_totals_and_per_model_rows():
    out = usage_summary(
        [
            {"model": "model-a", "usage": {"input_tokens": 10, "output_tokens": 4}},
            {"model": "model-a", "usage": {"input_tokens": 5, "output_tokens": 1}},
            {
                "model": "model-b",
                "usage": {"input_tokens": 7, "output_tokens": 2, "cache_read_tokens": 3},
            },
        ]
    )
    assert out["total"] == {
        "turns": 3,
        "turns_without_usage": 0,
        "input_tokens": 22,
        "output_tokens": 7,
        "cache_read_tokens": 3,
        "cache_write_tokens": None,
    }
    assert [row["model"] for row in out["by_model"]] == ["model-a", "model-b"]
    assert out["by_model"][0]["input_tokens"] == 15
    assert out["by_model"][0]["cache_read_tokens"] is None
    assert out["by_model"][1]["cache_read_tokens"] == 3
    assert out["truncated"] is False


def test_a_turn_without_usage_is_unknown_not_zero():
    out = usage_summary(
        [
            {"model": "local-model", "usage": {}},
            {"model": "local-model"},
            {"model": "model-a", "usage": {"input_tokens": 0, "output_tokens": 0}},
        ]
    )
    local = next(row for row in out["by_model"] if row["model"] == "local-model")
    assert local["turns"] == 2 and local["turns_without_usage"] == 2
    assert local["input_tokens"] is None and local["output_tokens"] is None
    reported = next(row for row in out["by_model"] if row["model"] == "model-a")
    assert reported["input_tokens"] == 0 and reported["turns_without_usage"] == 0
    assert out["total"]["turns_without_usage"] == 2
    assert out["total"]["input_tokens"] == 0  # the sum of what was reported


def test_no_turn_at_all_is_unknown():
    out = usage_summary([])
    assert out["total"]["turns"] == 0
    assert out["total"]["input_tokens"] is None
    assert out["by_model"] == []


def test_only_numbers_and_checked_model_ids_leave():
    out = usage_summary(
        [
            {"model": f"x {SECRET}", "usage": {"input_tokens": 1, "output_tokens": SECRET}},
            {"model": None, "usage": {"input_tokens": True, "output_tokens": -3}},
            {"usage": {"input_tokens": float("nan"), "output_tokens": 2, "note": SECRET}},
            "not a mapping",
        ]
    )
    assert SECRET not in json.dumps(out)
    assert [row["model"] for row in out["by_model"]] == [None]
    row = out["by_model"][0]
    assert row["turns"] == 3
    assert row["input_tokens"] == 1 and row["output_tokens"] == 2
    assert set(row) == set(out["total"]) | {"model"}


def test_truncated_when_the_limit_is_reached():
    turns = [{"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}] * 3
    assert usage_summary(turns, limit=3)["truncated"] is True
    assert usage_summary(turns, limit=4)["truncated"] is False


def test_route_reads_turns_db(tmp_path):
    store = TurnEventStore(tmp_path / "turns.db")
    try:
        store.append(
            "a" * 32,
            "turn.completed",
            {"model": "model-a", "usage": {"input_tokens": 12, "output_tokens": 3}},
        )
        store.append("b" * 32, "turn.completed", {"model": "model-b", "usage": {}})
        store.append("b" * 32, "turn.summary", {"category": "joy"})
        app = Flask(__name__)
        app.register_blueprint(create_turns_observability_blueprint(store))
        response = app.test_client().get("/api/observability/usage")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        data = response.get_json()
        assert data["source"] == "turns.db"
        assert data["total"]["turns"] == 2
        assert data["total"]["input_tokens"] == 12
        assert data["total"]["turns_without_usage"] == 1
        assert [row["model"] for row in data["by_model"]] == ["model-a", "model-b"]
        assert data["by_model"][1]["input_tokens"] is None
    finally:
        store.close()
