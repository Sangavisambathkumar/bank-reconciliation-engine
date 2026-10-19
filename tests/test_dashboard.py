import json
import re

from recon.cli import ROOT, main, reconcile
from recon.config import ReconConfig
from recon.dashboard import build_payload, write_dashboard
from recon.evaluate import evaluate
from recon.generate import generate


def _result(tmp_path, n=400, seed=4):
    generate(tmp_path, n=n, seed=seed)
    return reconcile(tmp_path / "internal_ledger.csv", tmp_path / "processor_settlement.csv", ROOT / "config.json")


def _embedded(html: str) -> dict:
    m = re.search(r'<script id="recon-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_payload_ties_out_and_bridge_sums_to_gap(tmp_path):
    res = _result(tmp_path)
    data = build_payload(res, ReconConfig.load(ROOT / "config.json").matching)
    t = data["totals"]
    assert t["unexplained_cents"] == 0
    assert sum(b["cents"] for b in data["bridge"]) == t["gap_cents"]
    assert sum(s["internal_cents"] for s in data["statuses"]) == t["internal_cents"]
    assert sum(a["count"] for a in data["aging"]) == next(
        s["items"] for s in data["statuses"] if s["key"] == "UNMATCHED_INTERNAL")


def test_exceptions_cover_every_unresolved_item(tmp_path):
    res = _result(tmp_path)
    data = build_payload(res)
    unresolved = sum(1 for g in res.groups if not g.status.is_match)
    assert len([e for e in data["exceptions"] if e["cat"] != "review"]) == unresolved


def test_page_is_self_contained_and_escapes_data(tmp_path):
    res = _result(tmp_path)
    res.groups[0].note = "</script><script>alert(1)</script>"  # hostile text in the data
    path = write_dashboard(res, tmp_path / "d.html")
    html = path.read_text()
    assert "alert(1)</script>" not in html            # cannot break out of the data block
    assert _embedded(html)["totals"]["internal_records"] == len(res.internal_records)
    assert not re.search(r'<(script|link)[^>]+(src|href)=["\']https?:', html)  # no external files


def test_dashboard_command_with_accuracy_panel(tmp_path):
    data_dir = tmp_path / "data"
    generate(data_dir, n=300, seed=9)
    out = tmp_path / "site" / "index.html"
    code = main(["dashboard", "--internal", str(data_dir / "internal_ledger.csv"),
                 "--processor", str(data_dir / "processor_settlement.csv"),
                 "--truth", str(data_dir / "ground_truth.json"), "--out", str(out)])
    assert code == 0
    payload = _embedded(out.read_text())
    assert payload["evaluation"]["precision"] >= 0.99
    assert payload["inputs"] == {"internal": "internal_ledger.csv", "processor": "processor_settlement.csv"}


def test_accuracy_panel_is_optional(tmp_path):
    res = _result(tmp_path)
    assert "evaluation" not in build_payload(res)
    assert "evaluation" in build_payload(res, evaluation=evaluate(res, tmp_path / "ground_truth.json"))
