"""Model price table loader and cost estimator."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PRICING_FILE = Path(os.environ.get("PRICING_FILE", "pricing.json"))


@lru_cache(maxsize=1)
def load_pricing() -> dict[str, Any]:
    path = PRICING_FILE
    if not path.is_file():
        # Fallback relative to app package / project root
        candidates = [
            Path(__file__).resolve().parent.parent / "pricing.json",
            Path("pricing.json"),
        ]
        for c in candidates:
            if c.is_file():
                path = c
                break
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _candidate_names(model: str) -> list[str]:
    """Exact id, plus the segment after a provider prefix (LiteLLM-style)."""
    name = (model or "").strip()
    if not name:
        return []
    names = [name]
    if "/" in name:
        tail = name.rsplit("/", 1)[-1].strip()
        if tail and tail not in names:
            names.append(tail)
    return names


def _rates_for(model: str) -> dict[str, Any]:
    """Exact match, then longest dated-snapshot prefix. Unknown models use default.

    Prices are list-price estimates. The UI keeps labeling cost as 估算.
    """
    pricing = load_pricing()
    default = pricing.get("default") or {
        "input_per_1m": 1.0,
        "output_per_1m": 3.0,
    }
    names = _candidate_names(model)
    for name in names:
        if name in pricing and name != "default" and not str(name).startswith("_"):
            return pricing[name]
    keys = [k for k in pricing.keys() if k != "default" and not str(k).startswith("_")]
    best_key = None
    best_len = -1
    for name in names:
        for key in keys:
            if (
                name.startswith(key + "-")
                or name.startswith(key + "@")
                or name.startswith(key + ":")
            ) and len(key) > best_len:
                best_key = key
                best_len = len(key)
    if best_key:
        return pricing[best_key]
    return default


def estimate_cost(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    rates = _rates_for(model)
    input_cost = (prompt_tokens / 1_000_000) * float(rates["input_per_1m"])
    output_cost = (completion_tokens / 1_000_000) * float(rates["output_per_1m"])
    return round(input_cost + output_cost, 6)


def list_priced_models() -> list[str]:
    pricing = load_pricing()
    return [k for k in pricing.keys() if k != "default" and not str(k).startswith("_")]
