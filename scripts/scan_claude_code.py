#!/usr/bin/env python3
"""Scan local Claude Code jsonl transcripts and POST to rankings ingest.

Usage:
  CLAUDE_PROJECTS_DIR=./fixtures/claude_code_sample \\
  RANKINGS_URL=http://localhost:8000 \\
  TOKEN_RANKINGS_API_KEY=dev-token-rankings-change-me \\
  python scripts/scan_claude_code.py

Or call the API:
  curl -X POST 'http://localhost:8000/api/v1/scan/claude_code?root=./fixtures/claude_code_sample' \\
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

from app.adapters.claude_code import scan_claude_code_dir  # noqa: E402


def _rankings_auth_headers() -> dict[str, str]:
    key = (os.environ.get("TOKEN_RANKINGS_API_KEY") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def main() -> int:
    scan_root = os.environ.get("CLAUDE_PROJECTS_DIR") or str(
        Path.home() / ".claude" / "projects"
    )
    rankings = os.environ.get("RANKINGS_URL", "http://localhost:8000").rstrip("/")
    dry = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

    rows = scan_claude_code_dir(scan_root)
    print(f"scanned {scan_root}: {len(rows)} usage rows (after message.id dedupe)", file=sys.stderr)
    if dry:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    # Prefer server-side scan when root is reachable there
    q = urllib.parse.urlencode({"root": scan_root})
    req = urllib.request.Request(
        rankings + "/api/v1/scan/claude_code?" + q,
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
            # fall through to push mapped events
        else:
            print(f"scan 503 (dir missing on server?), falling back to ingest push", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"scan unreachable ({exc}), falling back to ingest push", file=sys.stderr)

    # Fallback: push as Claude Code-shaped events (usage only, no content)
    payload_events = []
    for r in rows:
        payload_events.append(
            {
                "type": "assistant",
                "cwd": r.get("project_id"),
                "timestamp": r.get("ts"),
                "account": r.get("account_id"),
                "team": r.get("team"),
                "participant": r.get("participant"),
                "project_id": r.get("project_id"),
                "message": {
                    "id": r.get("request_id"),
                    "model": r["model"],
                    "usage": {
                        "input_tokens": r["prompt_tokens"],
                        "output_tokens": r["completion_tokens"],
                        "cache_read_input_tokens": r.get("cache_read_tokens", 0),
                        "cache_creation_input_tokens": r.get("cache_write_tokens", 0),
                    },
                },
            }
        )
    body = json.dumps({"events": payload_events}).encode("utf-8")
    req = urllib.request.Request(
        rankings + "/api/v1/ingest/claude_code",
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
