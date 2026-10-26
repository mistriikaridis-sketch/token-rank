#!/usr/bin/env python3
"""Scan local WorkBuddy jsonl usage logs and POST to rankings ingest.

Usage:
  WORKBUDDY_PROJECTS_DIR=./fixtures/workbuddy_sample \\
  RANKINGS_URL=http://localhost:8000 \\
  python scripts/scan_workbuddy.py

Or call the API:
  curl -X POST 'http://localhost:8000/api/v1/scan/workbuddy?root=./fixtures/workbuddy_sample'
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Allow running without installing the package
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.workbuddy import scan_workbuddy_dir  # noqa: E402


def _rankings_auth_headers() -> dict[str, str]:
    key = (os.environ.get("TOKEN_RANKINGS_API_KEY") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def main() -> int:
    scan_root = os.environ.get("WORKBUDDY_PROJECTS_DIR") or str(
        Path.home() / ".workbuddy" / "projects"
    )
    rankings = os.environ.get("RANKINGS_URL", "http://localhost:8000").rstrip("/")
    dry = os.environ.get("DRY_RUN", "") in ("1", "true", "yes")

    rows = scan_workbuddy_dir(scan_root)
    print(f"scanned {scan_root}: {len(rows)} usage rows", file=sys.stderr)
    if dry:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    body = json.dumps({"events": rows}).encode("utf-8")
    # Re-map already-normalized rows via raw-ish push: send as usage-shaped events
    # Prefer posting through ingest/workbuddy with the original mapped fields packed back.
    # Easiest path: hit scan endpoint on server if ROOT is mounted, else push events.
    payload_events = []
    for r in rows:
        payload_events.append(
            {
                "type": "llm.usage",
                "model": r["model"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "cache_read_tokens": r.get("cache_read_tokens", 0),
                "cache_write_tokens": r.get("cache_write_tokens", 0),
                "reasoning_tokens": r.get("reasoning_tokens", 0),
                "total_tokens": r["total_tokens"],
                "project_id": r.get("project_id"),
                "account_id": r.get("account_id"),
                "participant": r.get("participant"),
                "team": r.get("team"),
                "timestamp": r.get("ts"),
            }
        )
    body = json.dumps({"events": payload_events}).encode("utf-8")
    req = urllib.request.Request(
        rankings + "/api/v1/ingest/workbuddy",
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
