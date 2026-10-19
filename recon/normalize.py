"""Normalization helpers.

Real reconciliation is mostly a data-cleaning problem: the same payment shows up
as "TXN-000123" in one system and "txn000123" in another, "$1,234.56" vs
"1234.56", "Joe's Cafe" vs "SQ *JOES CAFE TORONTO". Everything here turns those
variants into one canonical form so the matcher can compare like with like.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher

# Prefixes systems commonly bolt onto the same underlying reference number.
_REF_PREFIXES = ("TXN", "TRX", "REF", "INV", "PMT", "ID")

# Words that carry no identifying signal in merchant / counterparty names.
_NOISE_TOKENS = {
    "sq", "pos", "purchase", "payment", "pmt", "inc", "ltd", "llc", "corp",
    "co", "the", "and", "of", "online", "www", "com", "ca",
}


def to_cents(raw) -> int:
    """Parse a money value into integer cents.

    Integer cents (never floats) avoid rounding drift when summing thousands
    of amounts. Handles "$1,234.56", "1234.5", "(12.00)" (accounting negative),
    "-12.00" and "12.00 CR".
    """
    if raw is None:
        raise ValueError("amount is empty")
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        raise ValueError("amount is empty")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.upper().endswith("CR"):
        negative, s = True, s[:-2]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        negative, s = not negative, s[1:]

    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a valid amount: {raw!r}") from exc

    cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def parse_date(raw, fmt: str) -> date:
    if raw is None or str(raw).strip() == "":
        raise ValueError("date is empty")
    try:
        return datetime.strptime(str(raw).strip(), fmt).date()
    except ValueError as exc:
        raise ValueError(f"date {raw!r} does not match format {fmt}") from exc


def normalize_reference(raw) -> str:
    """'TXN-000123', 'txn000123', 'Ref: 123' and '000123' all become '123'.

    Non-numeric references (e.g. 'BATCH-77A') are kept as upper-case
    alphanumerics so they can still be compared.
    """
    if raw is None:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    if s in {"", "NAN", "NONE", "NULL"}:
        return ""
    for prefix in _REF_PREFIXES:
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            s = s[len(prefix):]
            break
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def normalize_name(raw) -> str:
    """Canonical form of a merchant / counterparty name."""
    if raw is None:
        return ""
    s = str(raw).lower()
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in _NOISE_TOKENS and not t.isdigit()]
    return " ".join(tokens)


def name_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two *normalized* names.

    Takes the best of a character-level ratio and token containment, so
    'joes cafe' vs 'joes cafe toronto' scores 1.0 (all tokens of the shorter
    name appear in the longer one) while typos still get partial credit.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    containment = len(ta & tb) / min(len(ta), len(tb))
    char_ratio = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(containment, char_ratio)


def reference_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
