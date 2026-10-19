"""The multi-pass matching engine.

Matching runs from most certain to least certain. Each pass only sees records
that earlier passes left open, so a weak rule can never steal a record that a
strong rule would have matched correctly:

  0. Duplicates          repeated lines inside one file are set aside
  1. Exact               same reference + same amount + date in window
  2. Tolerance           same reference + small difference (processor fees)
  3. Fuzzy               no usable reference: amount + date + name similarity
  4. One-to-many (ref)   one booking settled in pieces that share its reference
     Many-to-one         several bookings settled as one batch line
     One-to-many (name)  split settlements that lost the reference
  5. Breaks              same reference but amounts/dates disagree
  6. Leftovers           unmatched items, with timing vs. genuine exceptions
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

from .config import MatchConfig
from .models import MatchGroup, Record, RejectedRow, Status
from .normalize import name_similarity, reference_similarity


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


@dataclass
class ReconResult:
    groups: list[MatchGroup]
    as_of: date
    internal_records: list[Record]
    processor_records: list[Record]
    rejected: list[RejectedRow] = field(default_factory=list)

    def by_status(self, *statuses: Status) -> list[MatchGroup]:
        return [g for g in self.groups if g.status in statuses]

    def summary(self) -> dict:
        int_total = sum(r.amount_cents for r in self.internal_records)
        proc_total = sum(r.amount_cents for r in self.processor_records)
        matched_int = sum(len(g.internal) for g in self.groups if g.status.is_match)
        matched_proc = sum(len(g.processor) for g in self.groups if g.status.is_match)
        counts = defaultdict(int)
        for g in self.groups:
            counts[g.status.value] += 1
        return {
            "as_of": self.as_of.isoformat(),
            "internal_records": len(self.internal_records),
            "processor_records": len(self.processor_records),
            "rejected_rows": len(self.rejected),
            "internal_total_cents": int_total,
            "processor_total_cents": proc_total,
            "internal_match_rate": matched_int / len(self.internal_records) if self.internal_records else 0.0,
            "processor_match_rate": matched_proc / len(self.processor_records) if self.processor_records else 0.0,
            "groups_by_status": dict(counts),
        }

    def proof(self) -> list[tuple[str, int]]:
        """Explain the gap between the two files, line by line.

        Internal total - processor total must equal the sum of every
        explained difference. If the 'unexplained' line is not zero, the
        engine has lost or double-counted a record.
        """
        int_total = sum(r.amount_cents for r in self.internal_records)
        proc_total = sum(r.amount_cents for r in self.processor_records)
        buckets: dict[str, int] = defaultdict(int)
        labels = {
            Status.MATCHED_EXACT: "Exact matches (should be 0)",
            Status.MATCHED_TOLERANCE: "Fees / small differences on tolerance matches",
            Status.MATCHED_FUZZY: "Differences on fuzzy matches",
            Status.MATCHED_MANY_TO_ONE: "Differences on batched settlements",
            Status.MATCHED_ONE_TO_MANY: "Differences on split settlements",
            Status.BREAK: "Breaks: amount disagreements to investigate",
            Status.UNMATCHED_INTERNAL: "Booked internally, not yet settled",
            Status.UNMATCHED_PROCESSOR: "Settled by processor, not booked internally",
            Status.DUPLICATE: "Duplicate lines",
        }
        for g in self.groups:
            buckets[labels[g.status]] += g.difference_cents
        explained = sum(buckets.values())
        rows = [("Internal ledger total", int_total), ("Processor settlement total", proc_total),
                ("Gap to explain (internal - processor)", int_total - proc_total)]
        rows += [(label, buckets.get(label, 0)) for label in labels.values()]
        rows.append(("Unexplained (must be 0)", (int_total - proc_total) - explained))
        return rows


class Reconciler:
    def __init__(self, cfg: MatchConfig | None = None):
        self.cfg = cfg or MatchConfig()

    # ------------------------------------------------------------------ helpers
    def tolerance(self, amount_cents: int) -> int:
        pct = round(abs(amount_cents) * self.cfg.amount_tolerance_pct)
        return max(self.cfg.amount_tolerance_cents, pct)

    def in_window(self, internal: Record, processor: Record) -> bool:
        """Settlement normally lands on or after the booking date; allow one day early for time zones."""
        lag = (processor.txn_date - internal.txn_date).days
        return -1 <= lag <= self.cfg.date_window_days

    def _new_group(self, status: Status, rule: str, internal, processor, score=1.0, note="") -> MatchGroup:
        self._counter += 1
        group = MatchGroup(f"M{self._counter:05d}", status, rule, list(internal), list(processor), round(score, 3), note)
        for r in group.internal:
            self.open_int.pop(r.record_id, None)
        for r in group.processor:
            self.open_proc.pop(r.record_id, None)
        self.groups.append(group)
        return group

    def _proc_by_ref(self) -> dict[tuple[str, str], list[Record]]:
        index: dict[tuple[str, str], list[Record]] = defaultdict(list)
        for r in self.open_proc.values():
            if r.norm_ref:
                index[(r.norm_ref, r.currency)].append(r)
        return index

    # --------------------------------------------------------------------- run
    def run(self, internal: list[Record], processor: list[Record], as_of: date | None = None,
            rejected: list[RejectedRow] | None = None) -> ReconResult:
        self.groups: list[MatchGroup] = []
        self._counter = 0
        self.open_int = {r.record_id: r for r in sorted(internal, key=lambda r: (r.txn_date, r.row_number))}
        self.open_proc = {r.record_id: r for r in sorted(processor, key=lambda r: (r.txn_date, r.row_number))}
        all_dates = [r.txn_date for r in internal + processor]
        self.as_of = as_of or (max(all_dates) if all_dates else date.today())

        self._pass_duplicates()
        self._pass_reference(exact=True)
        self._pass_reference(exact=False)
        self._pass_fuzzy()
        self._pass_one_to_many(by_reference=True)
        self._pass_many_to_one()
        self._pass_one_to_many(by_reference=False)
        self._pass_breaks()
        self._pass_leftovers()

        return ReconResult(self.groups, self.as_of, list(internal), list(processor), list(rejected or []))

    # ------------------------------------------------------------ pass 0: dups
    def _pass_duplicates(self) -> None:
        for source, pool in (("internal", self.open_int), ("processor", self.open_proc)):
            seen: dict[tuple, Record] = {}
            for rec in list(pool.values()):
                if not rec.norm_ref:
                    continue  # without a reference, identical amounts on one day can be legitimate
                key = (rec.norm_ref, rec.amount_cents, rec.txn_date, rec.currency)
                if key in seen:
                    first = seen[key]
                    note = (f"Same reference, amount and date as {first.record_id} "
                            f"(row {first.row_number}); possible double posting")
                    args = ([rec], []) if source == "internal" else ([], [rec])
                    self._new_group(Status.DUPLICATE, "duplicate-key", *args, note=note)
                else:
                    seen[key] = rec

    # ----------------------------------------------- pass 1 & 2: by reference
    def _pass_reference(self, exact: bool) -> None:
        index = self._proc_by_ref()
        for rec in list(self.open_int.values()):
            if not rec.norm_ref:
                continue
            best, best_key = None, None
            for cand in index.get((rec.norm_ref, rec.currency), []):
                if cand.record_id not in self.open_proc or not self.in_window(rec, cand):
                    continue
                diff = abs(rec.amount_cents - cand.amount_cents)
                ok = diff == 0 if exact else 0 < diff <= self.tolerance(rec.amount_cents)
                if not ok:
                    continue
                key = (diff, abs((cand.txn_date - rec.txn_date).days))
                if best_key is None or key < best_key:
                    best, best_key = cand, key
            if best is None:
                continue
            if exact:
                self._new_group(Status.MATCHED_EXACT, "ref+amount+date", [rec], [best])
            else:
                diff = rec.amount_cents - best.amount_cents
                self._new_group(Status.MATCHED_TOLERANCE, "ref+amount~tolerance", [rec], [best],
                                note=f"Amount differs by {_money(diff)} (within tolerance; likely processor fee)")

    # ------------------------------------------------------- pass 3: fuzzy
    def _fuzzy_score(self, a: Record, b: Record) -> float | None:
        if a.currency != b.currency or not self.in_window(a, b):
            return None
        # Without a shared reference, amount is the main anchor, so its tolerance is
        # tighter than for reference matches (busy merchants produce near-identical amounts).
        tol = min(self.tolerance(a.amount_cents), self.cfg.fuzzy_amount_tolerance_cents)
        diff = abs(a.amount_cents - b.amount_cents)
        if diff > tol:
            return None
        name = name_similarity(a.norm_name, b.norm_name)
        amount = 1.0 - diff / (tol + 1)
        days = abs((b.txn_date - a.txn_date).days)
        timing = 1.0 - days / (self.cfg.date_window_days + 1)
        score = 0.55 * name + 0.25 * amount + 0.20 * timing
        if a.norm_ref and b.norm_ref:  # partial credit for a mistyped reference
            score += 0.10 * reference_similarity(a.norm_ref, b.norm_ref)
        return min(score, 1.0)

    def _pass_fuzzy(self) -> None:
        # Records whose reference still has a partner on the other side are reserved
        # for the split/break passes, so fuzzy matching cannot pull them apart.
        int_refs = {(r.norm_ref, r.currency) for r in self.open_int.values() if r.norm_ref}
        proc_refs = {(r.norm_ref, r.currency) for r in self.open_proc.values() if r.norm_ref}
        ints = [r for r in self.open_int.values() if not r.norm_ref or (r.norm_ref, r.currency) not in proc_refs]
        procs = [r for r in self.open_proc.values() if not r.norm_ref or (r.norm_ref, r.currency) not in int_refs]

        by_date: dict[date, list[Record]] = defaultdict(list)
        for p in procs:
            by_date[p.txn_date].append(p)

        pairs: list[tuple[float, Record, Record]] = []
        for a in ints:
            for offset in range(-1, self.cfg.date_window_days + 1):
                for b in by_date.get(date.fromordinal(a.txn_date.toordinal() + offset), []):
                    s = self._fuzzy_score(a, b)
                    if s is not None and s >= self.cfg.fuzzy_threshold:
                        pairs.append((s, a, b))

        # Greedy best-first assignment; flag pairs where an equally good alternative existed.
        top_for: dict[str, list[float]] = defaultdict(list)
        for s, a, b in pairs:
            top_for[a.record_id].append(s)
            top_for[b.record_id].append(s)
        pairs.sort(key=lambda t: (-t[0], t[1].row_number, t[2].row_number))
        for s, a, b in pairs:
            if a.record_id not in self.open_int or b.record_id not in self.open_proc:
                continue
            ambiguous = sum(1 for x in top_for[a.record_id] if abs(x - s) < 1e-9) > 1 or \
                sum(1 for x in top_for[b.record_id] if abs(x - s) < 1e-9) > 1
            note = f"Matched on amount, date and name (score {s:.2f})"
            if ambiguous:
                note += "; AMBIGUOUS: another record scored the same, review"
            diff = a.amount_cents - b.amount_cents
            if diff:
                note += f"; differs by {_money(diff)}"
            self._new_group(Status.MATCHED_FUZZY, "amount+date+name", [a], [b], score=s, note=note)

    # ---------------------------------------------- pass 4: batches & splits
    def _related(self, one: Record, other: Record) -> bool:
        if one.currency != other.currency:
            return False
        if one.norm_ref and one.norm_ref == other.norm_ref:
            return True
        return name_similarity(one.norm_name, other.norm_name) >= self.cfg.name_match_threshold

    def _find_combo(self, target: Record, candidates: list[Record], same_day: bool = True) -> list[Record] | None:
        """Find 2..max_group_size candidates that sum to the target (within tolerance).

        Processors batch per merchant per day, so by default a combination must
        come from one booking date; that keeps the search small and stops
        payments from different days being glued together by coincidence.
        Ranks by smallest difference, then smallest group, so an exact three-way
        batch beats a two-way combination that only happens to land within tolerance.
        """
        tol = self.tolerance(target.amount_cents)
        buckets: dict = defaultdict(list)
        for c in candidates:
            buckets[c.txn_date if same_day else None].append(c)
        best, best_key = None, None
        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            bucket = sorted(bucket, key=lambda c: c.row_number)[: self.cfg.max_group_candidates]
            for size in range(2, min(self.cfg.max_group_size, len(bucket)) + 1):
                for combo in combinations(bucket, size):
                    diff = abs(sum(c.amount_cents for c in combo) - target.amount_cents)
                    if diff <= tol and (best_key is None or (diff, size) < best_key):
                        best, best_key = list(combo), (diff, size)
                if best_key and best_key[0] == 0:
                    break  # exact at this size; larger groups can't do better
        return best

    def _reserved(self) -> tuple[set, set]:
        """Records whose reference still has a partner on the other side.

        These belong to the reference-based passes (splits and breaks), so the
        name-based passes must not use them.
        """
        int_refs = {(r.norm_ref, r.currency) for r in self.open_int.values() if r.norm_ref}
        proc_refs = {(r.norm_ref, r.currency) for r in self.open_proc.values() if r.norm_ref}
        res_int = {r.record_id for r in self.open_int.values() if r.norm_ref and (r.norm_ref, r.currency) in proc_refs}
        res_proc = {r.record_id for r in self.open_proc.values() if r.norm_ref and (r.norm_ref, r.currency) in int_refs}
        return res_int, res_proc

    def _pass_many_to_one(self) -> None:
        res_int, res_proc = self._reserved()
        for proc in sorted(self.open_proc.values(), key=lambda r: -abs(r.amount_cents)):
            if proc.record_id not in self.open_proc or proc.record_id in res_proc:
                continue
            cands = [i for i in self.open_int.values()
                     if i.record_id not in res_int and self.in_window(i, proc) and self._related(proc, i)
                     and (i.amount_cents > 0) == (proc.amount_cents > 0)
                     and abs(i.amount_cents) <= abs(proc.amount_cents) + self.tolerance(proc.amount_cents)]
            combo = self._find_combo(proc, cands)
            if combo:
                g = self._new_group(Status.MATCHED_MANY_TO_ONE, "sum(internal)=processor", combo, [proc])
                g.note = f"{len(combo)} bookings settled as one line {proc.reference or proc.record_id}"
                if g.difference_cents:
                    g.note += f"; differs by {_money(g.difference_cents)}"

    def _pass_one_to_many(self, by_reference: bool) -> None:
        res_int, res_proc = self._reserved()
        for rec in sorted(self.open_int.values(), key=lambda r: -abs(r.amount_cents)):
            if rec.record_id not in self.open_int:
                continue
            if by_reference:
                if not rec.norm_ref:
                    continue
                related = lambda p: p.norm_ref == rec.norm_ref and p.currency == rec.currency
            else:
                if rec.record_id in res_int:
                    continue
                related = lambda p: p.record_id not in res_proc and self._related(rec, p)
            cands = [p for p in self.open_proc.values()
                     if self.in_window(rec, p) and related(p)
                     and (p.amount_cents > 0) == (rec.amount_cents > 0)
                     and abs(p.amount_cents) <= abs(rec.amount_cents) + self.tolerance(rec.amount_cents)]
            combo = self._find_combo(rec, cands)
            if combo:
                rule = "internal=sum(processor) by ref" if by_reference else "internal=sum(processor) by name"
                g = self._new_group(Status.MATCHED_ONE_TO_MANY, rule, [rec], combo)
                g.note = f"One booking settled in {len(combo)} parts"
                if g.difference_cents:
                    g.note += f"; differs by {_money(g.difference_cents)}"

    # ------------------------------------------------------- pass 5: breaks
    def _pass_breaks(self) -> None:
        index = self._proc_by_ref()
        for rec in list(self.open_int.values()):
            if not rec.norm_ref:
                continue
            cands = [c for c in index.get((rec.norm_ref, rec.currency), []) if c.record_id in self.open_proc]
            if not cands:
                continue
            best = min(cands, key=lambda c: (abs(c.amount_cents - rec.amount_cents),
                                             abs((c.txn_date - rec.txn_date).days)))
            reasons = []
            diff = rec.amount_cents - best.amount_cents
            if abs(diff) > self.tolerance(rec.amount_cents):
                pct = diff / rec.amount_cents * 100 if rec.amount_cents else 0
                reasons.append(f"amount differs by {_money(diff)} ({pct:.1f}%)")
            if not self.in_window(rec, best):
                reasons.append(f"settled {(best.txn_date - rec.txn_date).days} days after booking "
                               f"(window is {self.cfg.date_window_days})")
            note = "Same reference but " + " and ".join(reasons or ["details disagree"])
            self._new_group(Status.BREAK, "ref-only", [rec], [best], score=0.5, note=note)

    # ---------------------------------------------------- pass 6: leftovers
    def _pass_leftovers(self) -> None:
        for rec in list(self.open_int.values()):
            age = (self.as_of - rec.txn_date).days
            if age < self.cfg.settlement_lag_days:
                note = f"TIMING: booked {age} day(s) before cutoff; settlement expected next cycle"
            else:
                note = f"Not settled after {age} days; chase processor"
            self._new_group(Status.UNMATCHED_INTERNAL, "no-match", [rec], [], score=0.0, note=note)
        for rec in list(self.open_proc.values()):
            if rec.amount_cents < 0:
                note = "Debit from processor with no booking; possible chargeback or reversal"
            else:
                note = "Credit from processor with no booking; unidentified deposit"
            self._new_group(Status.UNMATCHED_PROCESSOR, "no-match", [], [rec], score=0.0, note=note)
