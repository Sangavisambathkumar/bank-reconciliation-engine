import pytest

from recon.normalize import name_similarity, normalize_name, normalize_reference, to_cents


@pytest.mark.parametrize("raw,cents", [
    ("1234.56", 123456), ("$1,234.56", 123456), ("1,234.5", 123450), ("(12.00)", -1200),
    ("-12.00", -1200), ("12.00 CR", -1200), ("0.005", 1), ("7", 700),
])
def test_to_cents(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["", "abc", "12..50", None, "nan"])
def test_to_cents_rejects_garbage(raw):
    with pytest.raises(ValueError):
        to_cents(raw)


@pytest.mark.parametrize("raw", ["TXN-000123", "txn000123", "TXN123", "000123", "Ref: 123", " 123 "])
def test_reference_variants_collapse(raw):
    assert normalize_reference(raw) == "123"


def test_reference_keeps_non_numeric_ids():
    assert normalize_reference("BATCH-0007") == "BATCH0007"
    assert normalize_reference("-") == ""


def test_merchant_name_variants_are_similar():
    base = normalize_name("Joe's Cafe")
    for variant in ["JOES CAFE", "SQ *JOES CAFE", "JOES CAFE TORONTO ON", "Joe's Cafe Inc."]:
        assert name_similarity(base, normalize_name(variant)) >= 0.8, variant


def test_different_merchants_are_not_similar():
    assert name_similarity(normalize_name("Joe's Cafe"), normalize_name("Bay Street Books")) < 0.5
