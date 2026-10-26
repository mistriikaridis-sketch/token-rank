"""Shared parse/skip counters for local adapters and probe reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParseStats:
    lines_total: int = 0
    files_scanned: int = 0
    parsed_ok: int = 0
    skipped_json_error: int = 0
    skipped_not_usage: int = 0
    skipped_missing_model: int = 0
    skipped_missing_tokens: int = 0
    skipped_forbidden_content: int = 0
    skipped_other: int = 0
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "ParseStats") -> "ParseStats":
        self.lines_total += other.lines_total
        self.files_scanned += other.files_scanned
        self.parsed_ok += other.parsed_ok
        self.skipped_json_error += other.skipped_json_error
        self.skipped_not_usage += other.skipped_not_usage
        self.skipped_missing_model += other.skipped_missing_model
        self.skipped_missing_tokens += other.skipped_missing_tokens
        self.skipped_forbidden_content += other.skipped_forbidden_content
        self.skipped_other += other.skipped_other
        self.notes.extend(other.notes)
        return self

    @property
    def skipped_total(self) -> int:
        return (
            self.skipped_json_error
            + self.skipped_not_usage
            + self.skipped_missing_model
            + self.skipped_missing_tokens
            + self.skipped_forbidden_content
            + self.skipped_other
        )

    @property
    def success_rate(self) -> float:
        if self.lines_total <= 0:
            return 0.0
        return round(100.0 * self.parsed_ok / self.lines_total, 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["skipped_total"] = self.skipped_total
        d["success_rate_pct"] = self.success_rate
        return d
