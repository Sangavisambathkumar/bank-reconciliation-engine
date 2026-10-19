"""Generate a realistic pair of files to reconcile, plus the answer key.

The bank's internal ledger is clean. The processor's settlement file is what
real ones look like: different column names, DD/MM/YYYY dates, reference
numbers in three formats, merchant names in five formats, comma-formatted
amounts, and every common real-world problem mixed in:

  clean       settles exactly                         -> MATCHED_EXACT
  fee         processor deducted a small fee          -> MATCHED_TOLERANCE
  noref       reference missing on processor side     -> MATCHED_FUZZY
  batch       2-3 payments settled as one line        -> MATCHED_MANY_TO_ONE
  split       one payment settled in two lines        -> MATCHED_ONE_TO_MANY
  break       partial refund / wrong amount           -> BREAK
  missing     never settled                           -> UNMATCHED_INTERNAL
  timing      booked too close to cutoff to settle    -> UNMATCHED_INTERNAL
  duplicate   processor sent the same line twice      -> MATCHED_EXACT + DUPLICATE
  extra       processor-only items (chargebacks etc.) -> UNMATCHED_PROCESSOR
  bad rows    unparseable dates/amounts               -> Rejected
"""
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

MERCHANTS = [
    "Joe's Cafe", "Maple Leaf Hardware", "Northern Lights Bakery", "Bay Street Books",
    "Lakeshore Auto Repair", "Queen West Florist", "Union Fitness Club", "Harbourfront Pharmacy",
    "Kensington Grocers", "Yorkville Dental", "Danforth Pizza Co.", "Riverdale Pet Supply",
    "Annex Coffee Roasters", "Liberty Village Tech", "Distillery Wine Bar", "High Park Cycles",
    "Leslieville Bistro", "Junction Records", "Scarborough Tire", "Etobicoke Plumbing Ltd",
    "Midtown Optometry", "Parkdale Laundromat", "Rosedale Interiors", "Cabbagetown Deli",
    "Greektown Taverna",
]

INTERNAL_COLUMNS = ["txn_id", "booking_date", "amount", "currency", "reference", "counterparty", "narrative"]
PROCESSOR_COLUMNS = ["SettlementID", "SettleDate", "MerchantName", "Ref", "NetAmount", "Ccy"]


def _processor_name(rng: random.Random, name: str) -> str:
    plain = name.replace("'", "").replace(".", "")
    return rng.choice([
        plain.upper(),
        "SQ *" + plain.upper(),
        plain.upper() + " TORONTO ON",
        name + " Inc.",
        name,
    ])


def _processor_ref(rng: random.Random, seq: int) -> str:
    return rng.choice([f"{seq:06d}", f"txn{seq:06d}", f"TXN{seq}", f"TXN-{seq:06d}"])


def _fmt_amount(rng: random.Random, cents: int) -> str:
    value = cents / 100
    return f"{value:,.2f}" if rng.random() < 0.5 else f"{value:.2f}"


def generate(out_dir: str | Path, n: int = 1000, seed: int = 42,
             start: date = date(2026, 9, 1), as_of: date = date(2026, 9, 30)) -> dict:
    rng = random.Random(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    internal, processor, truth = [], [], []
    proc_seq = 0
    days = (as_of - start).days + 1

    def add_proc(settle: date, merchant: str, ref: str, cents: int) -> str:
        nonlocal proc_seq
        proc_seq += 1
        pid = f"PRC-{proc_seq:06d}"
        processor.append({
            "SettlementID": pid, "SettleDate": settle.strftime("%d/%m/%Y"),
            "MerchantName": _processor_name(rng, merchant), "Ref": ref,
            "NetAmount": _fmt_amount(rng, cents), "Ccy": "CAD",
        })
        return pid

    internal_seq = 0

    def add_internal(booked: date, merchant: str, cents: int) -> tuple[str, int]:
        nonlocal internal_seq
        internal_seq += 1
        seq = 100000 + internal_seq
        iid = f"INT-{internal_seq:05d}"
        internal.append({
            "txn_id": iid, "booking_date": booked.isoformat(), "amount": f"{cents/100:.2f}",
            "currency": "CAD", "reference": f"TXN-{seq:06d}", "counterparty": merchant,
            "narrative": f"Card payment - {merchant}",
        })
        return iid, seq

    def random_cents() -> int:
        return max(500, int(rng.lognormvariate(4.2, 0.9) * 100))

    batch_no = 0
    while internal_seq < n:
        booked = start + timedelta(days=rng.randrange(days))
        merchant = rng.choice(MERCHANTS)
        cents = random_cents()
        iid, seq = add_internal(booked, merchant, cents)

        settle = booked + timedelta(days=rng.choice([1, 1, 1, 2]))
        if settle > as_of:
            truth.append({"scenario": "timing", "expected": "UNMATCHED_INTERNAL", "internal": [iid], "processor": []})
            continue

        r = rng.random()
        if r < 0.70:
            scenario = "clean"
        elif r < 0.78:
            scenario = "fee"
        elif r < 0.83:
            scenario = "noref"
        elif r < 0.86:
            scenario = "batch" if internal_seq + 2 <= n else "clean"
        elif r < 0.88:
            scenario = "split" if cents >= 2000 else "clean"
        elif r < 0.90:
            scenario = "break" if cents >= 5000 else "clean"
        elif r < 0.93:
            scenario = "missing"
        elif r < 0.94:
            scenario = "duplicate"
        else:
            scenario = "clean"

        ref = _processor_ref(rng, seq)
        if scenario == "clean":
            pid = add_proc(settle, merchant, ref, cents)
            truth.append({"scenario": "clean", "expected": "MATCHED_EXACT", "internal": [iid], "processor": [pid]})
        elif scenario == "fee":
            fee = rng.randint(30, 200)
            pid = add_proc(settle, merchant, ref, cents - fee)
            truth.append({"scenario": "fee", "expected": "MATCHED_TOLERANCE", "internal": [iid], "processor": [pid]})
        elif scenario == "noref":
            pid = add_proc(settle, merchant, rng.choice(["", "-", " "]), cents)
            truth.append({"scenario": "noref", "expected": "MATCHED_FUZZY", "internal": [iid], "processor": [pid]})
        elif scenario == "batch":
            # Same merchant, same day: the processor nets them into one deposit line.
            ids, total = [iid], cents
            for _ in range(rng.choice([1, 2])):
                if internal_seq >= n:
                    break
                extra_cents = random_cents()
                extra_id, _ = add_internal(booked, merchant, extra_cents)
                ids.append(extra_id)
                total += extra_cents
            batch_no += 1
            pid = add_proc(settle, merchant, f"BATCH-{batch_no:04d}", total)
            truth.append({"scenario": "batch", "expected": "MATCHED_MANY_TO_ONE", "internal": ids, "processor": [pid]})
        elif scenario == "split":
            first = int(cents * rng.uniform(0.3, 0.7))
            p1 = add_proc(settle, merchant, ref, first)
            p2 = add_proc(settle, merchant, _processor_ref(rng, seq), cents - first)
            truth.append({"scenario": "split", "expected": "MATCHED_ONE_TO_MANY", "internal": [iid], "processor": [p1, p2]})
        elif scenario == "break":
            paid = int(cents * rng.uniform(0.5, 0.85))
            pid = add_proc(settle, merchant, ref, paid)
            truth.append({"scenario": "break", "expected": "BREAK", "internal": [iid], "processor": [pid]})
        elif scenario == "missing":
            truth.append({"scenario": "missing", "expected": "UNMATCHED_INTERNAL", "internal": [iid], "processor": []})
        elif scenario == "duplicate":
            # Either copy can be treated as the original; the evaluator accepts both.
            p1 = add_proc(settle, merchant, ref, cents)
            proc_seq += 1
            p2 = f"PRC-{proc_seq:06d}"
            processor.append({**processor[-1], "SettlementID": p2})
            truth.append({"scenario": "duplicate", "expected": "MATCHED_EXACT+DUPLICATE",
                          "internal": [iid], "processor": [p1, p2]})

    # Processor-only items: chargebacks and unidentified credits.
    for j in range(1, 13):
        when = start + timedelta(days=rng.randrange(days))
        if j % 2:
            merchant = rng.choice(MERCHANTS)
            pid = add_proc(when, merchant, f"CB-{j:04d}", -rng.randint(1500, 40000))
            scenario = "extra-chargeback"
        else:
            pid = add_proc(when, "Interac e-Transfer Deposit", f"ETR{rng.randint(10**7, 10**8)}",
                           rng.randint(2000, 90000))
            scenario = "extra-deposit"
        truth.append({"scenario": scenario, "expected": "UNMATCHED_PROCESSOR", "internal": [], "processor": [pid]})

    # Unparseable rows, the kind every real file eventually contains.
    for bad_date, bad_amount in (("31/09/2026", "45.00"), ("15/09/2026", "12..50"), ("16/09/2026", "")):
        proc_seq += 1
        processor.append({"SettlementID": f"PRC-{proc_seq:06d}", "SettleDate": bad_date,
                          "MerchantName": "KENSINGTON GROCERS", "Ref": "", "NetAmount": bad_amount, "Ccy": "CAD"})
    internal.append({"txn_id": f"INT-{internal_seq + 1:05d}", "booking_date": "2026-13-01", "amount": "20.00",
                     "currency": "CAD", "reference": "TXN-999999", "counterparty": "Junction Records",
                     "narrative": "Card payment - Junction Records"})

    rng.shuffle(processor)  # settlement files never arrive in booking order

    with (out_dir / "internal_ledger.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INTERNAL_COLUMNS)
        w.writeheader()
        w.writerows(internal)
    with (out_dir / "processor_settlement.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PROCESSOR_COLUMNS)
        w.writeheader()
        w.writerows(processor)
    (out_dir / "ground_truth.json").write_text(json.dumps(
        {"as_of": as_of.isoformat(), "seed": seed, "groups": truth}, indent=1))

    counts = defaultdict(int)
    for t in truth:
        counts[t["scenario"]] += 1
    return {"internal_rows": len(internal), "processor_rows": len(processor), "scenarios": dict(counts)}
