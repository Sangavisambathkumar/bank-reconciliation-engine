"""Core data structures shared by the loader, matcher and report."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Status(str, Enum):
    MATCHED_EXACT = "MATCHED_EXACT"            # same reference, same amount
    MATCHED_TOLERANCE = "MATCHED_TOLERANCE"    # same reference, small difference (e.g. processor fee)
    MATCHED_FUZZY = "MATCHED_FUZZY"            # no usable reference; matched on amount + date + name
    MATCHED_MANY_TO_ONE = "MATCHED_MANY_TO_ONE"  # several bank records settled as one batch line
    MATCHED_ONE_TO_MANY = "MATCHED_ONE_TO_MANY"  # one bank record settled in several pieces
    BREAK = "BREAK"                            # same reference but amount/date disagree -> investigate
    UNMATCHED_INTERNAL = "UNMATCHED_INTERNAL"  # in the bank's books, not in the settlement file
    UNMATCHED_PROCESSOR = "UNMATCHED_PROCESSOR"  # in the settlement file, not in the bank's books
    DUPLICATE = "DUPLICATE"                    # repeated line inside one file

    @property
    def is_match(self) -> bool:
        return self.value.startswith("MATCHED")


@dataclass
class Record:
    source: str            # "internal" or "processor"
    record_id: str
    txn_date: date
    amount_cents: int
    currency: str
    reference: str         # as received
    counterparty: str      # as received
    description: str
    norm_ref: str = ""
    norm_name: str = ""
    row_number: int = 0    # line in the original file, for audit trail

    @property
    def amount(self) -> float:
        return self.amount_cents / 100


@dataclass
class MatchGroup:
    match_id: str
    status: Status
    rule: str
    internal: list[Record] = field(default_factory=list)
    processor: list[Record] = field(default_factory=list)
    score: float = 1.0
    note: str = ""

    @property
    def internal_cents(self) -> int:
        return sum(r.amount_cents for r in self.internal)

    @property
    def processor_cents(self) -> int:
        return sum(r.amount_cents for r in self.processor)

    @property
    def difference_cents(self) -> int:
        """Internal minus processor. Positive = bank booked more than it was paid."""
        return self.internal_cents - self.processor_cents

    @property
    def date_diff_days(self) -> int | None:
        if not self.internal or not self.processor:
            return None
        return (max(r.txn_date for r in self.processor) - min(r.txn_date for r in self.internal)).days


@dataclass
class RejectedRow:
    source: str
    row_number: int
    reason: str
    raw: dict
