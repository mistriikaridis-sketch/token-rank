#!/usr/bin/env python3
"""Pull LiteLLM /spend/logs and POST into Token Rankings ingest.

Usage:
  LITELLM_BASE_URL=http://litellm:4000 \\
  LITELLM_MASTER_KEY=sk-... \\
  RANKINGS_URL=http://localhost:8000 \\
  python scripts/sync_litellm.py
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
import json
from datetime import datetime, timedelta, timezone


def _rankings_auth_headers() -> dict[str, str]:
    key = (os.environ.get("TOKEN_RANKINGS_API_KEY") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def main() -> int:
    base = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    rankings = os.environ.get("RANKINGS_URL", "http://localhost:8000").rstrip("/")
    if not base or not key:
        print("LITELLM_BASE_URL and LITELLM_MASTER_KEY are required", file=sys.stderr)
        return 2

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(os.environ.get("LITELLM_SYNC_DAYS", "1")))
    # LiteLLM commonly expects "YYYY-MM-DD HH:MM:SS"
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")

    paths = [
        f"/spend/logs/v2?start_date={start_s}&end_date={end_s}&page_size=500",
        f"/spend/logs?start_date={start_s}&end_date={end_s}",
    ]
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    payload = None
    last_err = None
    for path in paths:
        req = urllib.request.Request(base + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    if payload is None:
        print(f"failed to fetch spend logs: {last_err}", file=sys.stderr)
        return 1

    body = json.dumps(payload).encode("utf-8")
    ingest_req = urllib.request.Request(
        rankings + "/api/v1/ingest/litellm",
        data=body,
        headers={"Content-Type": "application/json", **_rankings_auth_headers()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(ingest_req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"failed to ingest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
