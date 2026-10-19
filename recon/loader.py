"""Load a source file into standard Records, rejecting rows that can't be trusted.

Bad rows are never silently dropped: each one is kept with a reason so it shows
up in the report's "Rejected" sheet and someone can fix it at the source.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .config import OPTIONAL_FIELDS, REQUIRED_FIELDS, SourceConfig
from .models import Record, RejectedRow
from .normalize import normalize_name, normalize_reference, parse_date, to_cents


def load_records(path: str | Path, source: str, cfg: SourceConfig) -> tuple[list[Record], list[RejectedRow]]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [cfg.columns[f] for f in REQUIRED_FIELDS if cfg.columns.get(f) not in header]
        if missing:
            raise ValueError(f"{path.name}: missing required column(s) {missing}; found {header}")
        rows = list(reader)

    records: list[Record] = []
    rejected: list[RejectedRow] = []
    seen_ids: set[str] = set()

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        def get(field: str) -> str:
            col = cfg.columns.get(field)
            return (row.get(col) or "").strip() if col else ""

        try:
            record_id = get("record_id")
            if not record_id:
                raise ValueError("record id is empty")
            if record_id in seen_ids:
                raise ValueError(f"record id {record_id} appears more than once")
            txn_date = parse_date(get("txn_date"), cfg.date_format)
            amount_cents = to_cents(get("amount"))
        except ValueError as exc:
            rejected.append(RejectedRow(source, i, str(exc), dict(row)))
            continue

        seen_ids.add(record_id)
        counterparty = get("counterparty")
        description = get("description")
        reference = get("reference")
        records.append(Record(
            source=source,
            record_id=record_id,
            txn_date=txn_date,
            amount_cents=amount_cents,
            currency=(get("currency") or cfg.default_currency).upper(),
            reference=reference,
            counterparty=counterparty,
            description=description,
            norm_ref=normalize_reference(reference),
            norm_name=normalize_name(counterparty or description),
            row_number=i,
        ))
    return records, rejected


__all__ = ["load_records", "OPTIONAL_FIELDS"]
