"""Collector config: file, env file, and process environment.

Secret precedence at runtime: process env, then collector.env, then json.
``TOKEN_RANKINGS_API_KEY`` is not written into the json. ``install --save-key``
puts it in ``collector.env`` with mode 0600.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_TOOLS = ("workbuddy", "claude_code", "codex", "continue")
SUPPORTED_TOOLS = DEFAULT_TOOLS


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "token-rankings"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "token-rankings"


def config_path() -> Path:
    return config_dir() / "collector.json"


def env_file_path() -> Path:
    return config_dir() / "collector.env"


def state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "token-rankings"
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return root / "token-rankings"


@dataclass
class CollectorConfig:
    rankings_url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    account_id: Optional[str] = None
    participant: Optional[str] = None
    team: Optional[str] = None
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    roots: dict[str, list[str]] = field(default_factory=dict)
    identity_map: dict[str, dict[str, str]] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """Config safe to print. The write key is only a boolean."""
        return {
            "rankings_url": self.rankings_url,
            "api_key_configured": bool(self.api_key),
            "interval_minutes": self.interval_minutes,
            "account_id": self.account_id,
            "participant": self.participant,
            "team": self.team,
            "tools": list(self.tools),
            "roots": self.roots,
            "identity_map_keys": sorted(self.identity_map.keys()),
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            out[key] = value
    return out


def _as_opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_tools(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_TOOLS)
    from app.tools import normalize_tool

    out: list[str] = []
    for item in value:
        slug = normalize_tool(str(item)) if item else ""
        if slug in SUPPORTED_TOOLS and slug not in out:
            out.append(slug)
    return out or list(DEFAULT_TOOLS)


def _as_roots(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, paths in value.items():
        if not isinstance(paths, list):
            continue
        cleaned = [str(p).strip() for p in paths if str(p).strip()]
        if cleaned:
            out[str(key)] = cleaned
    return out


def _as_identity_map(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, mapped in value.items():
        if not isinstance(mapped, dict):
            continue
        entry: dict[str, str] = {}
        for field_name in ("account_id", "participant", "team"):
            text = _as_opt_str(mapped.get(field_name))
            if text:
                entry[field_name] = text
        if entry and str(key).strip():
            out[str(key).strip()] = entry
    return out


def load_config() -> CollectorConfig:
    """Defaults, then json, then collector.env, then process environment."""
    cfg = CollectorConfig()
    file_data = _read_json(config_path())
    if file_data:
        if file_data.get("rankings_url"):
            cfg.rankings_url = str(file_data["rankings_url"]).rstrip("/")
        if file_data.get("api_key"):
            cfg.api_key = str(file_data["api_key"]).strip()
        if file_data.get("interval_minutes") is not None:
            try:
                cfg.interval_minutes = max(int(file_data["interval_minutes"]), 1)
            except (TypeError, ValueError):
                pass
        cfg.account_id = _as_opt_str(file_data.get("account_id"))
        cfg.participant = _as_opt_str(file_data.get("participant"))
        cfg.team = _as_opt_str(file_data.get("team"))
        if "tools" in file_data:
            cfg.tools = _as_tools(file_data.get("tools"))
        cfg.roots = _as_roots(file_data.get("roots"))
        cfg.identity_map = _as_identity_map(file_data.get("identity_map"))

    env_file = _parse_env_file(env_file_path())
    # Process environment wins over the env file.
    def _pick(name: str) -> Optional[str]:
        if os.environ.get(name):
            return os.environ.get(name, "").strip()
        if name in env_file and env_file[name].strip():
            return env_file[name].strip()
        return None

    url = _pick("RANKINGS_URL")
    if url:
        cfg.rankings_url = url.rstrip("/")
    key = _pick("TOKEN_RANKINGS_API_KEY")
    if key:
        cfg.api_key = key
    account = _pick("COLLECTOR_ACCOUNT_ID")
    if account:
        cfg.account_id = account
    participant = _pick("COLLECTOR_PARTICIPANT")
    if participant:
        cfg.participant = participant
    team = _pick("COLLECTOR_TEAM")
    if team:
        cfg.team = team
    interval = _pick("COLLECTOR_INTERVAL_MINUTES")
    if interval:
        try:
            cfg.interval_minutes = max(int(interval), 1)
        except ValueError:
            pass
    return cfg


def save_config(cfg: CollectorConfig, *, api_key: Optional[str] = None) -> Path:
    """Write json (no secret) and optionally collector.env."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    body = {
        "rankings_url": cfg.rankings_url,
        "interval_minutes": cfg.interval_minutes,
        "account_id": cfg.account_id,
        "participant": cfg.participant,
        "team": cfg.team,
        "tools": list(cfg.tools),
        "roots": cfg.roots,
        "identity_map": cfg.identity_map,
    }
    path = config_path()
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if api_key is not None:
        env_path = env_file_path()
        lines = [
            "# 写入密钥。不要提交到仓库，不要发到聊天里。",
            f"TOKEN_RANKINGS_API_KEY={api_key}",
            f"RANKINGS_URL={cfg.rankings_url}",
        ]
        if cfg.account_id:
            lines.append(f"COLLECTOR_ACCOUNT_ID={cfg.account_id}")
        if cfg.participant:
            lines.append(f"COLLECTOR_PARTICIPANT={cfg.participant}")
        if cfg.team:
            lines.append(f"COLLECTOR_TEAM={cfg.team}")
        lines.append(f"COLLECTOR_INTERVAL_MINUTES={cfg.interval_minutes}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    return path
