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


def estimate_cost(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    pricing = load_pricing()
    rates = pricing.get(model) or pricing.get("default") or {
        "input_per_1m": 1.0,
        "output_per_1m": 3.0,
    }
    input_cost = (prompt_tokens / 1_000_000) * float(rates["input_per_1m"])
    output_cost = (completion_tokens / 1_000_000) * float(rates["output_per_1m"])
    return round(input_cost + output_cost, 6)


def list_priced_models() -> list[str]:
    pricing = load_pricing()
    return [k for k in pricing.keys() if k != "default"]
