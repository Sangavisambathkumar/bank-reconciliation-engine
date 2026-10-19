"""Each test builds a tiny scenario by hand and checks the engine puts it in the right bucket."""
from datetime import date, timedelta

import pytest

from recon.config import MatchConfig
from recon.matcher import Reconciler
from recon.models import Record, Status
from recon.normalize import normalize_name, normalize_reference

D = date(2026, 9, 10)
_row = iter(range(2, 10_000))


def rec(source, rid, cents, ref="", name="Joe's Cafe", day=0, currency="CAD"):
    return Record(source=source, record_id=rid, txn_date=D + timedelta(days=day), amount_cents=cents,
                  currency=currency, reference=ref, counterparty=name, description="",
                  norm_ref=normalize_reference(ref), norm_name=normalize_name(name), row_number=next(_row))


def bank(rid, cents, **kw):
    return rec("internal", rid, cents, **kw)


def proc(rid, cents, **kw):
    kw.setdefault("day", 1)
    return rec("processor", rid, cents, **kw)


def run(internal, processor, **cfg):
    return Reconciler(MatchConfig(**cfg)).run(internal, processor, as_of=date(2026, 9, 30))


def statuses(result):
    return {r.record_id: g.status for g in result.groups for r in g.internal + g.processor}


def test_exact_match_across_reference_formats():
    res = run([bank("I1", 1000, ref="TXN-000123")], [proc("P1", 1000, ref="txn000123")])
    assert statuses(res) == {"I1": Status.MATCHED_EXACT, "P1": Status.MATCHED_EXACT}


def test_fee_within_tolerance():
    res = run([bank("I1", 10000, ref="TXN-1")], [proc("P1", 9850, ref="1")])
    g = res.by_status(Status.MATCHED_TOLERANCE)[0]
    assert g.difference_cents == 150


def test_large_difference_is_a_break_not_a_match():
    res = run([bank("I1", 10000, ref="TXN-1")], [proc("P1", 7000, ref="1")])
    assert statuses(res)["I1"] == Status.BREAK
    assert "amount differs" in res.by_status(Status.BREAK)[0].note


def test_settlement_outside_date_window_is_a_break():
    res = run([bank("I1", 5000, ref="TXN-1")], [proc("P1", 5000, ref="1", day=9)])
    assert statuses(res)["I1"] == Status.BREAK
    assert "days after booking" in res.by_status(Status.BREAK)[0].note


def test_fuzzy_match_when_reference_missing():
    res = run([bank("I1", 4321, ref="TXN-9", name="Bay Street Books")],
              [proc("P1", 4321, ref="", name="SQ *BAY STREET BOOKS")])
    assert statuses(res)["I1"] == Status.MATCHED_FUZZY


def test_fuzzy_refuses_different_merchant():
    res = run([bank("I1", 4321, ref="TXN-9", name="Bay Street Books")],
              [proc("P1", 4321, ref="", name="Greektown Taverna")])
    assert statuses(res)["I1"] == Status.UNMATCHED_INTERNAL
    assert statuses(res)["P1"] == Status.UNMATCHED_PROCESSOR


def test_fuzzy_flags_ambiguity():
    res = run([bank("I1", 500, name="Joe's Cafe"), bank("I2", 500, name="Joe's Cafe")],
              [proc("P1", 500, name="JOES CAFE")])
    fuzzy = res.by_status(Status.MATCHED_FUZZY)
    assert len(fuzzy) == 1 and "AMBIGUOUS" in fuzzy[0].note


def test_batch_many_to_one():
    internal = [bank("I1", 1000, ref="TXN-1"), bank("I2", 2500, ref="TXN-2"), bank("I3", 999, ref="TXN-3")]
    res = run(internal, [proc("P1", 3500, ref="BATCH-0001", name="JOES CAFE")])
    g = res.by_status(Status.MATCHED_MANY_TO_ONE)[0]
    assert {r.record_id for r in g.internal} == {"I1", "I2"}
    assert statuses(res)["I3"] == Status.UNMATCHED_INTERNAL


def test_prefers_exact_three_way_batch_over_near_two_way():
    # 1000 + 2400 = 3400 is within the $2.50 tolerance of 3500, but 1000 + 1500 + 1000 is exact.
    internal = [bank("I1", 1000), bank("I2", 2400), bank("I3", 1500), bank("I4", 1000)]
    res = run(internal, [proc("P1", 3500, ref="BATCH-1", name="JOES CAFE")], amount_tolerance_cents=250)
    g = res.by_status(Status.MATCHED_MANY_TO_ONE)[0]
    assert g.difference_cents == 0 and len(g.internal) == 3


def test_split_one_to_many_by_reference():
    res = run([bank("I1", 10000, ref="TXN-55")], [proc("P1", 4000, ref="55"), proc("P2", 6000, ref="TXN55")])
    g = res.by_status(Status.MATCHED_ONE_TO_MANY)[0]
    assert {r.record_id for r in g.processor} == {"P1", "P2"}


def test_duplicate_processor_line():
    res = run([bank("I1", 2000, ref="TXN-7")], [proc("P1", 2000, ref="7"), proc("P2", 2000, ref="7")])
    st = statuses(res)
    assert st["I1"] == Status.MATCHED_EXACT
    assert sorted([st["P1"], st["P2"]]) == sorted([Status.MATCHED_EXACT, Status.DUPLICATE])


def test_timing_vs_genuinely_missing():
    res = Reconciler(MatchConfig(settlement_lag_days=2)).run(
        [bank("OLD", 1000, ref="TXN-1"), rec("internal", "NEW", 1000, ref="TXN-2", day=19)], [],
        as_of=date(2026, 9, 30))
    notes = {g.internal[0].record_id: g.note for g in res.groups}
    assert notes["NEW"].startswith("TIMING")
    assert "chase processor" in notes["OLD"]


def test_currency_must_match():
    res = run([bank("I1", 1000, ref="TXN-1", currency="USD")], [proc("P1", 1000, ref="1", currency="CAD")])
    assert statuses(res)["I1"] == Status.UNMATCHED_INTERNAL


def test_every_record_lands_in_exactly_one_group():
    internal = [bank(f"I{i}", 1000 + i, ref=f"TXN-{i}") for i in range(50)]
    processor = [proc(f"P{i}", 1000 + i, ref=str(i)) for i in range(0, 50, 2)]
    res = run(internal, processor)
    ids = [r.record_id for g in res.groups for r in g.internal + g.processor]
    assert len(ids) == len(set(ids)) == 75


@pytest.mark.parametrize("seed", [3, 11])
def test_proof_always_ties_out(tmp_path, seed):
    from recon.cli import ROOT, reconcile
    from recon.generate import generate
    generate(tmp_path, n=400, seed=seed)
    res = reconcile(tmp_path / "internal_ledger.csv", tmp_path / "processor_settlement.csv", ROOT / "config.json")
    assert res.proof()[-1][1] == 0
