"""Catalog coverage: domestic + international list prices, unknown names kept."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tr-price-"))
os.environ.setdefault("SEED_ON_START", "0")
os.environ.setdefault("FORUM_SEED_ON_START", "0")
os.environ.pop("DATABASE_URL", None)

from app.adapters.claude_code import map_claude_code_event  # noqa: E402
from app.adapters.codex import map_codex_event  # noqa: E402
from app.adapters.continue_dev import map_continue_event  # noqa: E402
from app.adapters.workbuddy import map_workbuddy_event  # noqa: E402
from app.pricing import (  # noqa: E402
    describe_price,
    estimate_cost,
    list_priced_models,
    load_pricing,
    normalize_model_name,
    price_tier,
)


DOMESTIC = (
    "deepseek-chat",
    "deepseek-v3",
    "deepseek-reasoner",
    "qwen-plus",
    "qwen-max",
    "qwen2.5-72b-instruct",
    "moonshot-v1-8k",
    "moonshot-v1-128k",
    "kimi-k2",
    "glm-4-plus",
    "glm-4-flash",
    "doubao-pro-32k",
    "doubao-lite-32k",
    "baichuan4",
    "abab6.5s-chat",
    "minimax-text-01",
    "yi-lightning",
    "yi-large",
)

INTERNATIONAL = (
    "gpt-4o",
    "claude-sonnet-4-5",
    "gemini-2.5-pro",
    "llama-3.3-70b",
    "llama-4-scout",
    "mistral-large",
    "codestral",
    "grok-3",
    "grok-3-mini",
    "command-r",
    "command-r-plus",
)


class PricingCatalogTests(unittest.TestCase):
    def test_required_models_are_priced_off_the_default_tier(self) -> None:
        default_in = estimate_cost("totally-unknown-model", 1_000_000, 0)
        default_out = estimate_cost("totally-unknown-model", 0, 1_000_000)
        self.assertEqual(default_in, 1.0)
        self.assertEqual(default_out, 3.0)
        priced = set(list_priced_models())
        for name in DOMESTIC + INTERNATIONAL:
            self.assertIn(name, priced, name)
            info = describe_price(name)
            self.assertEqual(info["tier"], "catalog", name)
            self.assertEqual(info["cost_basis"], "estimate", name)
            self.assertNotEqual(
                (info["input_per_1m"], info["output_per_1m"]),
                (1.0, 3.0),
                name,
            )

    def test_aliases_and_vendor_prefixes_normalize(self) -> None:
        cases = {
            "DeepSeek-ai/DeepSeek-R1": "deepseek-reasoner",
            "ZhipuAI/GLM-4-Plus": "glm-4-plus",
            "dashscope/qwen-max": "qwen-max",
            "volcengine/doubao-pro-32k": "doubao-pro-32k",
            "moonshot/kimi": "kimi-k2",
            "openai/gpt-4o-2024-08-06": "gpt-4o",
            "claude-3-5-sonnet-20241022": "claude-3-5-sonnet",
            "ollama/qwen2.5-coder:7b": "qwen2.5-coder-7b",
            "meta-llama/Llama-3.1-70B-Instruct": "llama-3.1-70b",
            "claude-4.5-sonnet": "claude-sonnet-4-5",
            "grok-3-latest": "grok-3",
            "c4ai-command-r-plus": "command-r-plus",
            "Baichuan-4": "baichuan4",
            "yi-large-preview": "yi-large",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_model_name(raw), expected, raw)
            self.assertEqual(price_tier(raw), "catalog", raw)
            self.assertNotEqual(estimate_cost(raw, 1000, 1000), estimate_cost("no-such-model", 1000, 1000))

    def test_unknown_model_keeps_raw_string_and_uses_default_tier(self) -> None:
        raw = "企业内部-不存在的模型"
        self.assertEqual(normalize_model_name(raw), raw)
        self.assertEqual(price_tier(raw), "default")
        info = describe_price(raw)
        self.assertIsNone(info["pricing_key"])
        self.assertEqual(info["cost_basis"], "estimate")
        self.assertEqual(normalize_model_name("starcoder2:3b"), "starcoder2:3b")

    def test_alias_targets_exist(self) -> None:
        pricing = load_pricing()
        keys = set(list_priced_models())
        aliases = pricing["_aliases"]
        self.assertIsInstance(aliases, dict)
        for src, dst in aliases.items():
            self.assertIn(dst, keys, src)
            self.assertNotEqual(src.lower(), dst.lower())
        notes = pricing["_notes"]
        self.assertEqual(notes["cost_basis"], "estimate")
        self.assertIn("不是云厂商发票", notes["disclaimer_zh"])

    def test_adapters_keep_unknown_models_when_tokens_exist(self) -> None:
        wb = map_workbuddy_event(
            {
                "type": "llm.usage",
                "model": "企业内部-不存在的模型",
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "timestamp": "2026-10-25T00:00:00Z",
                "request_id": "wb-unknown-1",
            }
        )
        self.assertIsNotNone(wb)
        assert wb is not None
        self.assertEqual(wb["model"], "企业内部-不存在的模型")

        cc = map_claude_code_event(
            {
                "type": "assistant",
                "timestamp": "2026-10-25T00:00:00Z",
                "message": {
                    "id": "msg-unknown",
                    "model": "acme-private-llm",
                    "usage": {"input_tokens": 5, "output_tokens": 6},
                },
            }
        )
        self.assertIsNotNone(cc)
        assert cc is not None
        self.assertEqual(cc["model"], "acme-private-llm")

        cx = map_codex_event(
            {
                "model": "my-finetune",
                "response_id": "resp-unknown",
                "timestamp": "2026-10-25T00:00:00Z",
                "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
            }
        )
        self.assertIsNotNone(cx)
        assert cx is not None
        self.assertEqual(cx["model"], "my-finetune")

        cont = map_continue_event(
            {
                "eventName": "tokensGenerated",
                "model": "starcoder2:3b",
                "promptTokens": 9,
                "generatedTokens": 4,
                "timestamp": "2026-10-25T00:00:00Z",
                "request_id": "cont-unknown",
            }
        )
        self.assertIsNotNone(cont)
        assert cont is not None
        self.assertEqual(cont["model"], "starcoder2:3b")


if __name__ == "__main__":
    unittest.main()
