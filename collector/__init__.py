"""Local usage collector for the private Token 消耗排行榜.

Scans AI coding tool logs on a developer machine and uploads token counts
only. This is not a public TokenRank client and it does not upload chat
or source code.
"""

from pathlib import Path


def _read_version() -> str:
    path = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "0.21.1"


__version__ = _read_version()
