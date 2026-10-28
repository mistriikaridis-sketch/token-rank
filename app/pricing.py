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


FORMULA_ZH = (
    "预估费用 = 输入 Token × 输入单价 + 输出 Token × 输出单价"
    " + 缓存读 Token × 缓存读单价 + 缓存写 Token × 缓存写单价。"
    "单价是每百万 Token 的公开列表价。价目表没有单独写出缓存价时，"
    "缓存读按输入价的 10% 估算，缓存写按输入价的 125% 估算。"
    "这是列表价估算，不是实扣账单。"
)

LEADERBOARD_COST_ZH = (
    "个人榜、组织榜、项目榜、Agent 榜上的费用默认只按输入和输出估算，"
    "和以前一样，避免历史数字突然变化。"
    "榜上可以打开「含缓存」，把缓存读、缓存写按同一套列表价算进去。"
    "「我的」里的预估费用一直含缓存，并标明列表价估算，不是实扣账单。"
)


def _cache_ratios() -> tuple[float, float]:
    notes = pricing_notes()
    try:
        read_ratio = float(notes.get("cache_read_ratio_of_input", 0.1))
    except (TypeError, ValueError):
        read_ratio = 0.1
    try:
        write_ratio = float(notes.get("cache_write_ratio_of_input", 1.25))
    except (TypeError, ValueError):
        write_ratio = 1.25
    return read_ratio, write_ratio


def cache_rates(model: str) -> dict[str, Any]:
    """Per-million list prices, including cache read/write.

    Explicit ``cache_read_per_1m`` / ``cache_write_per_1m`` on a model win.
    Otherwise the ratios in ``_notes`` (default 10% and 125% of input) apply.
    """
    rates = _rates_for(model)
    pin = float(rates["input_per_1m"])
    pout = float(rates["output_per_1m"])
    read_ratio, write_ratio = _cache_ratios()
    if "cache_read_per_1m" in rates:
        cache_read = float(rates["cache_read_per_1m"])
        read_default = False
    else:
        cache_read = pin * read_ratio
        read_default = True
    if "cache_write_per_1m" in rates:
        cache_write = float(rates["cache_write_per_1m"])
        write_default = False
    else:
        cache_write = pin * write_ratio
        write_default = True
    return {
        "input_per_1m": pin,
        "output_per_1m": pout,
        "cache_read_per_1m": cache_read,
        "cache_write_per_1m": cache_write,
        "cache_rates_are_default": read_default or write_default,
        "tier": price_tier(model),
        "currency": rates.get("currency") or "USD",
        "cost_basis": "estimate",
    }


def estimate_usage_parts(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> dict[str, Any]:
    """List-price split used on 「我的」. Does not change leaderboard totals.

    ``estimate_cost`` stays prompt + completion only. This function adds
    cache read and cache write so the formula on the page matches the number.
    """
    rates = cache_rates(model)

    def _usd(tokens: int, per_million: float) -> float:
        return (max(int(tokens or 0), 0) / 1_000_000) * float(per_million)

    input_usd = _usd(prompt_tokens, rates["input_per_1m"])
    output_usd = _usd(completion_tokens, rates["output_per_1m"])
    cache_read_usd = _usd(cache_read_tokens, rates["cache_read_per_1m"])
    cache_write_usd = _usd(cache_write_tokens, rates["cache_write_per_1m"])
    billed_as_input = _usd(cache_read_tokens, rates["input_per_1m"])
    savings = max(billed_as_input - cache_read_usd, 0.0)
    return {
        "input_usd": round(input_usd, 6),
        "output_usd": round(output_usd, 6),
        "cache_read_usd": round(cache_read_usd, 6),
        "cache_write_usd": round(cache_write_usd, 6),
        "total_usd": round(input_usd + output_usd + cache_read_usd + cache_write_usd, 6),
        "cache_savings_usd": round(savings, 6),
        **rates,
    }


def pricing_formula() -> dict[str, Any]:
    notes = pricing_notes()
    read_ratio, write_ratio = _cache_ratios()
    return {
        "formula_zh": FORMULA_ZH,
        "leaderboard_zh": LEADERBOARD_COST_ZH,
        "cost_basis": "estimate",
        "cache_read_ratio_of_input": read_ratio,
        "cache_write_ratio_of_input": write_ratio,
        "rates_url": "/api/v1/pricing",
        "disclaimer_zh": (notes.get("disclaimer_zh") or FORMULA_ZH),
        "unknown_models_zh": notes.get("unknown_models_zh") or "",
        "table_version": notes.get("table_version") or "",
        "updated_at": notes.get("updated_at") or "",
        "include_cache_default": False,
        "include_cache_zh": (
            "榜默认不含缓存，和以前的数字一致。需要时在榜上打开「含缓存」。"
            "这是列表价估算，不是实扣账单。"
        ),
    }


def list_priced_models() -> list[str]:
    pricing = load_pricing()
    return [k for k in pricing.keys() if k != "default" and not str(k).startswith("_")]


def pricing_notes() -> dict[str, Any]:
    notes = load_pricing().get("_notes")
    return notes if isinstance(notes, dict) else {}
