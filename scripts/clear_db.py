#!/usr/bin/env python3
"""Clear usage events. Usage: python scripts/clear_db.py [all|seed]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import clear_events, count_events, init_db  # noqa: E402


def main() -> None:
    scope = (sys.argv[1] if len(sys.argv) > 1 else "seed").strip().lower()
    if scope not in ("all", "seed"):
        print("usage: clear_db.py [all|seed]", file=sys.stderr)
        sys.exit(2)
    init_db()
    deleted = clear_events(scope=scope)
    print(
        f"deleted={deleted} scope={scope} "
        f"total={count_events()} non_seed={count_events(exclude_seed=True)}"
    )


if __name__ == "__main__":
    main()
