#!/usr/bin/env python3
"""Scan local Codex CLI rollout jsonl and POST to rankings ingest.

Usage:
  CODEX_SESSIONS_DIR=./fixtures/codex_sample \\
  RANKINGS_URL=http://localhost:8000 \\
  TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me \\
  python scripts/scan_codex.py

Or call the API:
  curl -X POST 'http://localhost:8000/api/v1/scan/codex?root=./fixtures/codex_sample' \\
    -H "Authorization: Bearer $TOKEN_RANKINGS_API_KEY"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.codex import scan_codex_dir  # noqa: E402


def _rankings_auth_headers() -> dict[str, str]:
    key = (os.environ.get("TOKEN_RANKINGS_API_KEY") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def main() -> int:
    scan_root = os.environ.get("CODEX_SESSIONS_DIR") or str(
        Path.home() / ".codex" / "sessions"
    )
    rankings = os.environ.get("RANKINGS_URL", "http://localhost:8000").rstrip("/")
    dry = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

    rows = scan_codex_dir(scan_root)
    print(
        f"scanned {scan_root}: {len(rows)} usage rows "
        f"(token_usage_record preferred; response_id deduped)",
        file=sys.stderr,
    )
    if dry:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    q = urllib.parse.urlencode({"root": scan_root})
    req = urllib.request.Request(
        rankings + "/api/v1/scan/codex?" + q,
        data=b"",
        headers={**_rankings_auth_headers()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            print(f"scan endpoint failed: {exc}", file=sys.stderr)
            body = exc.read().decode("utf-8", errors="replace")
            print(body, file=sys.stderr)
        else:
            print(
                "scan 503 (dir missing on server?), falling back to ingest push",
                file=sys.stderr,
            )
    except urllib.error.URLError as exc:
        print(f"scan unreachable ({exc}), falling back to ingest push", file=sys.stderr)

    # Fallback: push usage-only events (no conversation bodies)
    payload_events = []
    for r in rows:
        payload_events.append(
            {
                "model": r["model"],
                "cwd": r.get("project_id"),
                "project_id": r.get("project_id"),
                "account": r.get("account_id"),
                "team": r.get("team"),
                "participant": r.get("participant"),
                "response_id": r.get("request_id"),
                "timestamp": r.get("ts"),
                "usage": {
                    # Re-encode as Codex shape: input = fresh + cached
                    "input_tokens": int(r.get("prompt_tokens") or 0)
                    + int(r.get("cache_read_tokens") or 0),
                    "cached_input_tokens": int(r.get("cache_read_tokens") or 0),
                    "output_tokens": int(r.get("completion_tokens") or 0),
                    "reasoning_output_tokens": int(r.get("reasoning_tokens") or 0),
                    "cache_write_input_tokens": int(r.get("cache_write_tokens") or 0),
                    "total_tokens": int(r.get("total_tokens") or 0),
                },
            }
        )
    body = json.dumps({"events": payload_events}).encode("utf-8")
    req = urllib.request.Request(
        rankings + "/api/v1/ingest/codex",
        data=body,
        headers={"Content-Type": "application/json", **_rankings_auth_headers()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"failed to ingest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
