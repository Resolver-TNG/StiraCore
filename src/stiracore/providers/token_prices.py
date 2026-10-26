"""Token unit prices per model (``gateway-config.json`` ``token_prices``, #451).

USD per million tokens, keyed by the model id exactly as the server uses it::

    "token_prices": {
      "global.anthropic.claude-sonnet-5": {
        "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75
      }
    }

Stira ships **no price data**: a model without an entry has no price, and its cost is
never shown (nor counted as free). ``input`` and ``output`` are required for an entry;
``cache_read`` / ``cache_write`` are optional — a turn that used the cache of a model
without that price stays unpriced (:func:`stiracore.providers.usage.estimate_cost`).

Read leniently at start-up: an entry or a value not understood is left out with one
warning code (codes only, never the value); nothing here refuses a start. Only built-in
values are evaluated (``type(x) in (int, float)``, a bool is never a number).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .usage import Pricing

__all__ = ["FIELDS", "MAX_MODELS", "parse_token_prices"]

SECTION = "token_prices"
# entry key -> Pricing field
FIELDS = {
    "input": "input_per_mtok",
    "output": "output_per_mtok",
    "cache_read": "cache_read_per_mtok",
    "cache_write": "cache_write_per_mtok",
}
_REQUIRED = ("input", "output")
MAX_MODELS = 100
_MODEL_CHARS = 200
_PRICE_MAX = 1_000_000.0  # USD per million tokens; anything larger is a typo


def _price(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if not 0 <= value <= _PRICE_MAX:
        return None
    return float(value)


def parse_token_prices(section: Any) -> tuple[dict[str, Pricing], tuple[str, ...]]:
    """``(model id -> Pricing, warning codes)``. Never raises."""
    if section is None:
        return {}, ()
    if not isinstance(section, Mapping):
        return {}, ("section",)
    prices: dict[str, Pricing] = {}
    warnings: list[str] = []
    for model, entry in section.items():
        if len(prices) >= MAX_MODELS:
            warnings.append("too_many_models")
            break
        if type(model) is not str or not model.strip() or len(model) > _MODEL_CHARS:
            warnings.append("model_invalid")
            continue
        if not isinstance(entry, Mapping):
            warnings.append("entry_invalid")
            continue
        values: dict[str, float] = {}
        bad = False
        for key, value in entry.items():
            if key not in FIELDS:
                warnings.append("unknown_field")
                continue
            price = _price(value)
            if price is None:
                bad = True
                continue
            values[FIELDS[key]] = price
        if bad or any(FIELDS[key] not in values for key in _REQUIRED):
            warnings.append("entry_invalid")
            continue
        prices[model] = Pricing(**values)
    return prices, tuple(dict.fromkeys(warnings))
