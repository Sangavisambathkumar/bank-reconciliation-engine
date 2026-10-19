"""Reconciliation settings.

Every source file has its own column names and date format, so each source
gets a column mapping. Matching tolerances live here too, so operations staff
can tune them without touching code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceConfig:
    columns: dict[str, str]          # standard field -> column name in the file
    date_format: str = "%Y-%m-%d"
    default_currency: str = "CAD"


@dataclass
class MatchConfig:
    date_window_days: int = 3           # settlement can land up to N days after the booking
    amount_tolerance_cents: int = 250   # differences up to $2.50 are treated as fees, not breaks
    amount_tolerance_pct: float = 0.0   # optional %-based tolerance (the larger of the two applies)
    fuzzy_threshold: float = 0.80       # minimum score to accept a fuzzy match
    fuzzy_amount_tolerance_cents: int = 0  # with no reference to anchor on, require the amount to agree
    name_match_threshold: float = 0.80  # minimum name similarity for grouping candidates
    max_group_size: int = 4             # largest batch/split we try to assemble
    max_group_candidates: int = 20      # cap on combinations search per line
    settlement_lag_days: int = 2        # unmatched items this close to cutoff are flagged as timing


@dataclass
class ReconConfig:
    internal: SourceConfig
    processor: SourceConfig
    matching: MatchConfig = field(default_factory=MatchConfig)
    as_of_date: str | None = None       # cutoff date (YYYY-MM-DD); defaults to latest date seen

    @classmethod
    def load(cls, path: str | Path) -> "ReconConfig":
        data = json.loads(Path(path).read_text())
        return cls(
            internal=SourceConfig(**data["internal"]),
            processor=SourceConfig(**data["processor"]),
            matching=MatchConfig(**data.get("matching", {})),
            as_of_date=data.get("as_of_date"),
        )


REQUIRED_FIELDS = ("record_id", "txn_date", "amount")
OPTIONAL_FIELDS = ("currency", "reference", "counterparty", "description")
