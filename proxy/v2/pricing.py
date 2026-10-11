"""Model prices, per million tokens (US dollars, Anthropic API list prices).

Source: the 2026-10-05 fact-check (reports/ModelMatch three reports compared and
what to build next.md, section 7A). A 1-hour cache write costs 2x the input price.
Models not listed here have NO price: estimates for them fall back to token
counts or "unknown", never to a made-up number. Override or extend with a JSON
file in MODELMATCH_PRICING_FILE: {"model-id": {"input":..,"write_5m":..,"write_1h":..,"read":..,"output":..}}.

Subscription plans (Pro / Max, ChatGPT sign-in) are not billed per token. For them
these prices are only used as *relative weights*, and ModelMatch says "uses more of
your plan usage" in words, never dollars.
"""

import json
import os
from typing import Dict, Optional

from ..provider import resolve_model

PRICES_AS_OF = "2026-10-05"

PRICES: Dict[str, Dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "write_5m": 5.0, "write_1h": 8.0, "read": 0.20, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "write_5m": 2.50, "write_1h": 4.0, "read": 0.20, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "write_5m": 1.25, "write_1h": 2.0, "read": 0.10, "output": 5.0},
}
_REQUIRED = ("input", "write_5m", "write_1h", "read", "output")


def _load_overrides() -> Dict[str, Dict[str, float]]:
    path = os.environ.get("MODELMATCH_PRICING_FILE")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    good = {}
    for model, price in (data.items() if isinstance(data, dict) else []):
        if isinstance(price, dict) and all(isinstance(price.get(k), (int, float)) for k in _REQUIRED):
            good[str(model).lower()] = {k: float(price[k]) for k in _REQUIRED}
    return good


def price_for(model: Optional[str]) -> Optional[Dict[str, float]]:
    if not model:
        return None
    table = dict(PRICES)
    table.update(_load_overrides())
    lower = model.lower()
    if lower in table:
        return table[lower]
    resolved = resolve_model(model)
    if resolved.model_id and resolved.model_id in table and not resolved.substituted_from:
        return table[resolved.model_id]
    return None
