"""Score the engine against the generator's answer key.

Two measures:
  * Link precision / recall: did we pair the right internal records with the
    right processor records? (A wrong pairing is worse than no pairing: it
    hides a real exception.)
  * Status accuracy: did each item land in the right bucket?
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .matcher import ReconResult


def _links(internal_ids, processor_ids) -> set[tuple[str, str]]:
    return {(i, p) for i in internal_ids for p in processor_ids}


def evaluate(result: ReconResult, truth_path: str | Path) -> dict:
    truth = json.loads(Path(truth_path).read_text())["groups"]

    pred_links: set[tuple[str, str]] = set()
    status_of: dict[str, str] = {}
    for g in result.groups:
        pred_links |= _links([r.record_id for r in g.internal], [r.record_id for r in g.processor])
        for r in g.internal + g.processor:
            status_of[r.record_id] = g.status.value

    # For a duplicated settlement line, either copy may legitimately be kept as the
    # original, so the true link is whichever copy the engine chose (if it chose one).
    true_links: set[tuple[str, str]] = set()
    for t in truth:
        if t["expected"] == "MATCHED_EXACT+DUPLICATE":
            iid = t["internal"][0]
            chosen = [p for p in t["processor"] if (iid, p) in pred_links]
            true_links.add((iid, chosen[0] if chosen else t["processor"][0]))
        else:
            true_links |= _links(t["internal"], t["processor"])

    tp = len(true_links & pred_links)
    precision = tp / len(pred_links) if pred_links else 1.0
    recall = tp / len(true_links) if true_links else 1.0

    per_scenario: dict[str, dict] = defaultdict(lambda: {"items": 0, "correct": 0, "misses": []})
    for t in truth:
        ids = t["internal"] + t["processor"]
        if t["expected"] == "MATCHED_EXACT+DUPLICATE":
            ok = status_of.get(ids[0]) == "MATCHED_EXACT" and \
                sorted(status_of.get(x) for x in t["processor"]) == ["DUPLICATE", "MATCHED_EXACT"]
        else:
            ok = all(status_of.get(x) == t["expected"] for x in ids)
        s = per_scenario[t["scenario"]]
        s["items"] += 1
        s["correct"] += ok
        if not ok and len(s["misses"]) < 5:
            s["misses"].append({"ids": ids, "expected": t["expected"],
                                "got": sorted({status_of.get(x, "MISSING") for x in ids})})

    total = sum(s["items"] for s in per_scenario.values())
    correct = sum(s["correct"] for s in per_scenario.values())
    return {
        "link_precision": precision,
        "link_recall": recall,
        "status_accuracy": correct / total if total else 1.0,
        "per_scenario": dict(per_scenario),
    }


def format_evaluation(ev: dict) -> str:
    lines = [
        "Accuracy against ground truth",
        f"  Link precision : {ev['link_precision']:.2%}   (pairings we made that are correct)",
        f"  Link recall    : {ev['link_recall']:.2%}   (true pairings we found)",
        f"  Status accuracy: {ev['status_accuracy']:.2%}",
        "",
        f"  {'Scenario':<18}{'Items':>7}{'Correct':>9}{'Rate':>9}",
    ]
    for name, s in sorted(ev["per_scenario"].items()):
        lines.append(f"  {name:<18}{s['items']:>7}{s['correct']:>9}{s['correct']/s['items']:>9.1%}")
    for name, s in sorted(ev["per_scenario"].items()):
        for m in s["misses"]:
            lines.append(f"  miss [{name}] {m['ids']} expected {m['expected']} got {m['got']}")
    return "\n".join(lines)
