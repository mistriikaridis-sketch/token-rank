"""Canonical tool slugs + display names for ranking filters."""
from __future__ import annotations

from typing import Optional

# Canonical lowercase snake_case slugs
KNOWN_TOOLS = (
    "cursor",
    "workbuddy",
    "claude_code",
    "codex",
    "continue",
    "litellm",
    "openwebui",
    "procode",
    "custom_script",
    "zapier",
    "dify",
    "unknown",
)

TOOL_DISPLAY: dict[str, str] = {
    "cursor": "Cursor",
    "workbuddy": "WorkBuddy",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "continue": "Continue",
    "litellm": "LiteLLM",
    "openwebui": "OpenWebUI",
    "procode": "ProCode",
    "custom_script": "自定义脚本",
    "zapier": "Zapier",
    "dify": "Dify",
    "unknown": "未知",
}

# Map messy aliases → slug
_ALIASES: dict[str, str] = {
    "cursor": "cursor",
    "workbuddy": "workbuddy",
    "workbody": "workbuddy",  # common typo from product brief
    "work buddy": "workbuddy",
    "claude code": "claude_code",
    "claude_code": "claude_code",
    "claudecode": "claude_code",
    "codex": "codex",
    "openai codex": "codex",
    "continue": "continue",
    "continue.dev": "continue",
    "litellm": "litellm",
    "litellm proxy": "litellm",
    "litellm-proxy": "litellm",
    "openwebui": "openwebui",
    "open webui": "openwebui",
    "open-webui": "openwebui",
    "procode": "procode",
    "pro code": "procode",
    "canoryn": "procode",
    "canoryn ai": "procode",
    "自定义脚本": "custom_script",
    "custom script": "custom_script",
    "custom_script": "custom_script",
    "script": "custom_script",
    "zapier": "zapier",
    "dify": "dify",
    "未知": "unknown",
    "unknown": "unknown",
}


def _slugify_loose(raw: str) -> str:
    s = raw.strip().lower()
    s = s.replace("-", " ").replace("_", " ")
    s = " ".join(s.split())
    return s


def normalize_tool(value: Optional[str], default: str = "unknown") -> str:
    """Normalize any tool string to a canonical lowercase slug."""
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    key = _slugify_loose(raw)
    if key in _ALIASES:
        return _ALIASES[key]
    # already snake_case known
    snake = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if snake in TOOL_DISPLAY:
        return snake
    # collapse spaces to underscore for unknown tools
    if snake:
        return snake[:64]
    return default


def tool_display(value: Optional[str]) -> str:
    """Friendly label for UI; falls back to original-ish title case."""
    slug = normalize_tool(value) if value else "unknown"
    if slug in TOOL_DISPLAY:
        return TOOL_DISPLAY[slug]
    # unknown custom slug → readable
    return slug.replace("_", " ").title()


def tool_labels_map(slugs: list[str]) -> dict[str, str]:
    return {s: tool_display(s) for s in slugs}
