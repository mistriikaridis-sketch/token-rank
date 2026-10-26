"""Model price table loader and cost estimator.

Prices are public list-price estimates in USD (国内标价按约 7.2 CNY = 1 USD 折算).
They are never invoice or cloud-bill amounts. ``cost_basis`` for these figures
is ``estimate``.

Unknown model strings are not rewritten and are not a reason to drop usage.
Only the price lookup falls back to the ``default`` tier.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

PRICING_FILE = Path(os.environ.get("PRICING_FILE", "pricing.json"))

# Vendor prefixes used by LiteLLM, OpenRouter, Ollama, and domestic gateways.
# Longer prefixes are tried first inside one pass via the sorted tuple.
_PROVIDER_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "openai/",
            "anthropic/",
            "google/",
            "gemini/",
            "meta-llama/",
            "meta/",
            "mistralai/",
            "mistral/",
            "x-ai/",
            "xai/",
            "cohere/",
            "deepseek-ai/",
            "deepseek/",
            "dashscope/",
            "aliyun/",
            "alibaba/",
            "tongyi/",
            "qwen/",
            "moonshotai/",
            "moonshot/",
            "kimi/",
            "zhipuai/",
            "zhipu/",
            "bigmodel/",
            "glm/",
            "volcengine/",
            "volces/",
            "doubao/",
            "bytedance/",
            "baichuan-inc/",
            "baichuan/",
            "minimax/",
            "01-ai/",
            "01ai/",
            "yi/",
            "groq/",
            "together/",
            "fireworks/",
            "openrouter/",
            "azure/",
            "bedrock/",
            "vertex_ai/",
            "vertex-ai/",
            "ollama/",
            "huggingface/",
        ),
        key=len,
        reverse=True,
    )
)

_TRAILING_DATE = re.compile(
    r"(?:[-_@:](?:20\d{6}|20\d{2}-\d{2}-\d{2}|\d{2}-20\d{2}))+$",
    re.IGNORECASE,
)
_CHANNEL_SUFFIX = re.compile(
    r"-(latest|preview|exp|experimental|beta|stable)$",
    re.IGNORECASE,
)
_SIZE_SUFFIX = re.compile(r":(\d+(?:\.\d+)?b)$", re.IGNORECASE)
_SNAPSHOT_REST = re.compile(
    r"^(?:latest|preview|exp|experimental|beta|stable|20\d{6}|20\d{2}-\d{2}-\d{2}|\d{2}-20\d{2})$",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def load_pricing() -> dict[str, Any]:
    path = PRICING_FILE
    if not path.is_file():
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


def _catalog_keys(pricing: dict[str, Any]) -> set[str]:
    return {
        k
        for k in pricing.keys()
        if k != "default" and not str(k).startswith("_")
    }


def _alias_map(pricing: dict[str, Any]) -> dict[str, str]:
    raw = pricing.get("_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for src, dst in raw.items():
        if isinstance(src, str) and isinstance(dst, str) and dst.strip():
            out[src.strip().lower()] = dst.strip().lower()
    return out


def _add_candidate(out: list[str], value: str) -> None:
    text = value.strip().strip("/")
    if text and text not in out:
        out.append(text)


def _candidates(model: str) -> list[str]:
    """Lookup forms, most specific first. Does not decide the stored name."""
    raw = (model or "").strip().lower().replace(" ", "")
    if not raw:
        return []
    forms: list[str] = [raw]
    cur = raw
    for _ in range(3):
        nxt = cur
        for prefix in _PROVIDER_PREFIXES:
            if nxt.startswith(prefix):
                nxt = nxt[len(prefix) :]
                break
        else:
            break
        if not nxt or nxt == cur:
            break
        cur = nxt
        forms.append(cur)

    expanded: list[str] = []
    for form in forms:
        expanded.append(form)
        dated = _TRAILING_DATE.sub("", form)
        if dated != form:
            expanded.append(dated)
        channeled = _CHANNEL_SUFFIX.sub("", form)
        if channeled != form:
            expanded.append(channeled)
        both = _CHANNEL_SUFFIX.sub("", dated)
        if both not in (form, dated, channeled):
            expanded.append(both)
        sized = _SIZE_SUFFIX.search(form)
        if sized:
            base = form[: sized.start()]
            size = sized.group(1).lower()
            expanded.append(base)
            expanded.append(f"{base}-{size}")
            expanded.append(_TRAILING_DATE.sub("", base))

    out: list[str] = []
    for form in expanded:
        _add_candidate(out, form)
    return out


def resolve_pricing_model(model: str) -> Optional[str]:
    """Return a catalog key when the name is known, else None.

    None means "price with the default tier" and "keep the raw string".
    """
    pricing = load_pricing()
    keys = _catalog_keys(pricing)
    aliases = _alias_map(pricing)
    names = _candidates(model)
    for name in names:
        if name in keys:
            return name
    for name in names:
        target = aliases.get(name)
        if target in keys:
            return target
    best: Optional[str] = None
    best_len = -1
    for name in names:
        for key in keys:
            if len(key) <= best_len:
                continue
            for sep in ("-", "@", ":"):
                prefix = key + sep
                if not name.startswith(prefix):
                    continue
                rest = name[len(prefix) :]
                if _SNAPSHOT_REST.fullmatch(rest):
                    best = key
                    best_len = len(key)
                    break
    return best


def normalize_model_name(model: Optional[str]) -> str:
    """Canonical catalog id when known; otherwise the original stripped string.

    Unknown names are preserved (including case and vendor prefix) so ingest
    never replaces them with ``unknown`` or ``default``.
    """
    if model is None:
        return ""
    original = str(model).strip()
    if not original:
        return ""
    canonical = resolve_pricing_model(original)
    if canonical is None:
        return original
    return canonical


def _default_rates(pricing: dict[str, Any]) -> dict[str, Any]:
    default = pricing.get("default")
    if isinstance(default, dict) and "input_per_1m" in default:
        return default
    return {"input_per_1m": 1.0, "output_per_1m": 3.0, "currency": "USD"}


def _rates_for(model: str) -> dict[str, Any]:
    """Catalog match, else the documented default tier."""
    pricing = load_pricing()
    key = resolve_pricing_model(model)
    if key and isinstance(pricing.get(key), dict):
        return pricing[key]
    return _default_rates(pricing)


def price_tier(model: str) -> str:
    """``catalog`` when a list price matches, else ``default``."""
    return "catalog" if resolve_pricing_model(model) else "default"


def describe_price(model: str) -> dict[str, Any]:
    """Explain how a model string will be stored and priced.

    ``cost_basis`` is always ``estimate`` for this table.
    """
    rates = _rates_for(model)
    key = resolve_pricing_model(model)
    return {
        "input_model": model,
        "stored_as": normalize_model_name(model),
        "pricing_key": key,
        "tier": "catalog" if key else "default",
        "input_per_1m": float(rates["input_per_1m"]),
        "output_per_1m": float(rates["output_per_1m"]),
        "currency": rates.get("currency") or "USD",
        "cost_basis": "estimate",
    }


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """List-price estimate from prompt + completion tokens. Not an invoice.

    Cache and reasoning tokens are stored on the event but are not added again
    here (callers already put fresh prompt tokens in ``prompt_tokens``).
    """
    rates = _rates_for(model)
    input_cost = (prompt_tokens / 1_000_000) * float(rates["input_per_1m"])
    output_cost = (completion_tokens / 1_000_000) * float(rates["output_per_1m"])
    return round(input_cost + output_cost, 6)


def list_priced_models() -> list[str]:
    pricing = load_pricing()
    return [k for k in pricing.keys() if k != "default" and not str(k).startswith("_")]


def pricing_notes() -> dict[str, Any]:
    notes = load_pricing().get("_notes")
    return notes if isinstance(notes, dict) else {}
