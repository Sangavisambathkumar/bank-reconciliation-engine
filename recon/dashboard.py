"""Turn a reconciliation run into a single self-contained HTML dashboard.

The page has no external dependencies: data is embedded as JSON and the charts
are drawn with a small amount of plain JavaScript, so the file works when
opened locally, attached to an email, or hosted on GitHub Pages.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import MatchConfig
from .matcher import ReconResult
from .models import MatchGroup, Status

STATUS_LABELS = {
    Status.MATCHED_EXACT: "Exact match",
    Status.MATCHED_TOLERANCE: "Matched, fee deducted",
    Status.MATCHED_FUZZY: "Matched without reference",
    Status.MATCHED_MANY_TO_ONE: "Batched settlement",
    Status.MATCHED_ONE_TO_MANY: "Split settlement",
    Status.BREAK: "Break (amount or date)",
    Status.UNMATCHED_INTERNAL: "Booked, not settled",
    Status.UNMATCHED_PROCESSOR: "Settled, not booked",
    Status.DUPLICATE: "Duplicate line",
}

PROOF_LABELS = {
    "Fees / small differences on tolerance matches": "Processor fees",
    "Differences on fuzzy matches": "Fuzzy-match differences",
    "Differences on batched settlements": "Batch rounding",
    "Differences on split settlements": "Split rounding",
    "Breaks: amount disagreements to investigate": "Breaks to investigate",
    "Booked internally, not yet settled": "Booked, not yet settled",
    "Settled by processor, not booked internally": "Settled, not booked",
    "Duplicate lines": "Duplicate lines",
}

AGE_BUCKETS = [(0, 1, "0–1 days"), (2, 7, "2–7 days"), (8, 14, "8–14 days"), (15, 30, "15–30 days"),
               (31, 10**6, "Over 30 days")]


def _category(g: MatchGroup) -> str | None:
    """Which work queue an item belongs to (None = nothing to do)."""
    if g.status == Status.BREAK:
        return "break"
    if g.status == Status.UNMATCHED_INTERNAL:
        return "timing" if g.note.startswith("TIMING") else "chase"
    if g.status == Status.UNMATCHED_PROCESSOR:
        return "not_booked"
    if g.status == Status.DUPLICATE:
        return "duplicate"
    if g.status == Status.MATCHED_FUZZY and "AMBIGUOUS" in g.note:
        return "review"
    return None


def build_payload(result: ReconResult, cfg: MatchConfig | None = None, evaluation: dict | None = None,
                  inputs: dict | None = None) -> dict:
    s = result.summary()
    statuses = []
    for status in Status:
        groups = result.by_status(status)
        statuses.append({
            "key": status.value, "label": STATUS_LABELS[status], "matched": status.is_match,
            "items": len(groups),
            "internal_records": sum(len(g.internal) for g in groups),
            "processor_records": sum(len(g.processor) for g in groups),
            "internal_cents": sum(g.internal_cents for g in groups),
            "processor_cents": sum(g.processor_cents for g in groups),
        })

    proof = result.proof()
    bridge = [{"label": PROOF_LABELS[label], "cents": cents} for label, cents in proof[3:-1]
              if label in PROOF_LABELS and cents != 0]

    aging = [{"label": label, "lo": lo, "hi": hi, "count": 0, "cents": 0} for lo, hi, label in AGE_BUCKETS]
    for g in result.by_status(Status.UNMATCHED_INTERNAL):
        age = (result.as_of - g.internal[0].txn_date).days
        for b in aging:
            if b["lo"] <= age <= b["hi"]:
                b["count"] += 1
                b["cents"] += g.internal_cents
                break
    lag = cfg.settlement_lag_days if cfg else 2

    exceptions = []
    for g in result.groups:
        cat = _category(g)
        if not cat:
            continue
        recs = g.internal + g.processor
        exceptions.append({
            "id": g.match_id, "cat": cat, "status": STATUS_LABELS[g.status],
            "date": min(r.txn_date for r in recs).isoformat(),
            "age": (result.as_of - min(r.txn_date for r in recs)).days,
            "internal": [r.record_id for r in g.internal],
            "processor": [r.record_id for r in g.processor],
            "ref": next((r.reference for r in recs if r.reference.strip()), ""),
            "counterparty": (g.internal or g.processor)[0].counterparty,
            "internal_cents": g.internal_cents if g.internal else None,
            "processor_cents": g.processor_cents if g.processor else None,
            "diff_cents": g.difference_cents,
            "note": g.note,
        })

    payload = {
        "as_of": s["as_of"],
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "inputs": inputs or {},
        "totals": {
            "internal_records": s["internal_records"], "processor_records": s["processor_records"],
            "internal_cents": s["internal_total_cents"], "processor_cents": s["processor_total_cents"],
            "gap_cents": proof[2][1], "unexplained_cents": proof[-1][1],
            "internal_match_rate": s["internal_match_rate"], "processor_match_rate": s["processor_match_rate"],
            "rejected": s["rejected_rows"],
        },
        "statuses": statuses,
        "bridge": bridge,
        "aging": aging,
        "timing_lag_days": lag,
        "exceptions": exceptions,
        "rejected": [{"source": r.source, "row": r.row_number, "reason": r.reason} for r in result.rejected],
        "settings": {
            "date_window_days": cfg.date_window_days, "tolerance_cents": cfg.amount_tolerance_cents,
            "fuzzy_threshold": cfg.fuzzy_threshold,
        } if cfg else None,
    }
    if evaluation:
        payload["evaluation"] = {
            "precision": evaluation["link_precision"], "recall": evaluation["link_recall"],
            "status_accuracy": evaluation["status_accuracy"],
            "scenarios": [{"name": k, "items": v["items"], "correct": v["correct"]}
                          for k, v in sorted(evaluation["per_scenario"].items())],
        }
    return payload


def write_dashboard(result: ReconResult, path: str | Path, cfg: MatchConfig | None = None,
                    evaluation: dict | None = None, inputs: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(build_payload(result, cfg, evaluation, inputs), separators=(",", ":"))
    data = data.replace("</", "<\\/")  # never let embedded text close the <script> tag
    path.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reconciliation Dashboard</title>
<meta name="description" content="Bank ledger vs payment processor reconciliation results">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10); --wash: rgba(11,11,11,0.04);
  --series-1: #2a78d6; --series-2: #eb6834; --total: #52514e;
  --good: #0ca30c; --good-text: #006300; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --accent: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.05);
    --series-1: #3987e5; --series-2: #d95926; --total: #c3c2b7; --good-text: #0ca30c; --accent: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.05);
  --series-1: #3987e5; --series-2: #d95926; --total: #c3c2b7; --good-text: #0ca30c; --accent: #3987e5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1160px; margin: 0 auto; padding: 28px 16px 48px; }
header { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: flex-end; justify-content: space-between; margin-bottom: 20px; }
h1 { font-size: 24px; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--ink-2); margin: 0; }
.head-right { display: flex; align-items: center; gap: 10px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  font-weight: 600; font-size: 13px; border: 1px solid var(--ring); background: var(--surface); white-space: nowrap; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex: none; }
button.theme { font: inherit; font-size: 13px; color: var(--ink-2); background: var(--surface);
  border: 1px solid var(--ring); border-radius: 8px; padding: 5px 10px; cursor: pointer; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 18px 18px 16px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 12px; }
.kpi .label { color: var(--ink-2); font-size: 13px; }
.kpi .value { font-size: 26px; font-weight: 650; letter-spacing: -0.01em; margin: 2px 0; }
.kpi .note { color: var(--muted); font-size: 12px; }
.kpi .value.good { color: var(--good-text); }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }
@media (max-width: 820px) { .grid2 { grid-template-columns: 1fr; } }
h2 { font-size: 15px; margin: 0 0 2px; }
.card .desc { color: var(--ink-2); margin: 0 0 12px; font-size: 13px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--ink-2); margin: 0 0 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.chart { width: 100%; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { font-family: inherit; }
details.tbl { margin-top: 10px; font-size: 13px; }
details.tbl summary { color: var(--ink-2); cursor: pointer; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mini td, .mini th { padding: 4px 6px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font: inherit; font-size: 13px; display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px;
  border-radius: 999px; border: 1px solid var(--ring); background: transparent; color: var(--ink); cursor: pointer; }
.chip:hover { background: var(--wash); }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.chip .n { color: inherit; opacity: .7; font-variant-numeric: tabular-nums; }
input[type=search] { font: inherit; font-size: 13px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--ring);
  background: var(--page); color: var(--ink); min-width: 220px; flex: 1 1 220px; max-width: 340px; margin-left: auto; }
.scroll { overflow-x: auto; margin: 0 -4px; padding: 0 4px; }
#ex th[data-sort] { cursor: pointer; user-select: none; }
#ex th[data-sort]:hover { color: var(--ink); }
#ex th .arr { color: var(--muted); font-size: 11px; margin-left: 3px; }
#ex td.note { color: var(--ink-2); min-width: 260px; }
#ex td.ids { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; white-space: nowrap; }
.tag { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; font-weight: 550; }
.icon { width: 16px; height: 16px; border-radius: 50%; display: inline-grid; place-items: center; font-size: 10px;
  font-weight: 800; color: #0b0b0b; flex: none; line-height: 1; }
.more { margin-top: 10px; }
.more button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--ring);
  background: transparent; color: var(--ink); cursor: pointer; }
.empty { color: var(--muted); padding: 18px 8px; text-align: center; }
#tip { position: fixed; pointer-events: none; z-index: 10; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.5;
  box-shadow: 0 4px 16px rgba(0,0,0,.12); opacity: 0; transition: opacity .08s; max-width: 280px; }
#tip b { font-weight: 650; }
#tip .k { color: var(--ink-2); }
footer { color: var(--muted); font-size: 12px; margin-top: 18px; display: flex; flex-wrap: wrap; gap: 6px 18px; }
footer code { font-size: 12px; }
.acc { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px; margin-bottom: 12px; }
.acc .v { font-size: 22px; font-weight: 650; }
.acc .l { color: var(--ink-2); font-size: 12px; }
@media (max-width: 520px) { .kpi .value { font-size: 22px; } input[type=search] { margin-left: 0; max-width: none; } }
@media print { button.theme, .toolbar, .more { display: none; } .card { break-inside: avoid; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Reconciliation dashboard</h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <div class="head-right">
      <span class="pill" id="tie"></span>
      <button class="theme" id="theme" type="button" aria-label="Toggle light or dark theme">Theme</button>
    </div>
  </header>

  <section class="kpis" id="kpis" aria-label="Headline figures"></section>

  <div class="grid2">
    <section class="card">
      <h2>Where every item landed</h2>
      <p class="desc">Each settlement or booking is placed in exactly one outcome.</p>
      <div class="legend" id="lg-status"></div>
      <div class="chart" id="ch-status" role="img" aria-label="Bar chart of items by outcome"></div>
      <details class="tbl"><summary>View as table</summary><div class="scroll" id="tb-status"></div></details>
    </section>
    <section class="card">
      <h2>Why the two files differ</h2>
      <p class="desc" id="bridge-desc"></p>
      <div class="legend" id="lg-bridge"></div>
      <div class="chart" id="ch-bridge" role="img" aria-label="Bridge chart explaining the gap between the files"></div>
      <details class="tbl"><summary>View as table</summary><div class="scroll" id="tb-bridge"></div></details>
    </section>
  </div>

  <div class="grid2">
    <section class="card">
      <h2>Unsettled bookings by age</h2>
      <p class="desc" id="aging-desc"></p>
      <div class="legend" id="lg-aging"></div>
      <div class="chart" id="ch-aging" role="img" aria-label="Bar chart of unsettled bookings by age"></div>
      <details class="tbl"><summary>View as table</summary><div class="scroll" id="tb-aging"></div></details>
    </section>
    <section class="card" id="acc-card">
      <h2>Engine accuracy</h2>
      <p class="desc">Scored against the answer key written by the test-data generator.</p>
      <div class="acc" id="acc"></div>
      <div class="scroll" id="tb-acc"></div>
    </section>
  </div>

  <section class="card" aria-labelledby="ex-h">
    <h2 id="ex-h">Exceptions to work</h2>
    <p class="desc">Everything that needs a person, with the engine's reason. Click a column to sort.</p>
    <div class="toolbar">
      <div class="chips" id="chips" role="group" aria-label="Filter by queue"></div>
      <input type="search" id="q" placeholder="Search merchant, ID, reference, note" aria-label="Search exceptions">
    </div>
    <div class="scroll"><table id="ex"></table></div>
    <div class="more" id="more"></div>
  </section>

  <footer id="foot"></footer>
</div>
<div id="tip" role="tooltip"></div>

<script id="recon-data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("recon-data").textContent);
  var NS = "http://www.w3.org/2000/svg";

  // ---------- helpers ----------
  function money(c, signed) {
    if (c === null || c === undefined) return "";
    var v = Math.abs(c) / 100;
    var s = v.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var sign = c < 0 ? "−" : (signed && c > 0 ? "+" : "");
    return sign + "$" + s;
  }
  function moneyShort(c) {
    var v = Math.abs(c) / 100, sign = c < 0 ? "−" : "";
    if (v >= 1e6) return sign + "$" + (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return sign + "$" + (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + "k";
    return sign + "$" + v.toFixed(0);
  }
  function pct(x) { return (x * 100).toFixed(1) + "%"; }
  function num(n) { return n.toLocaleString("en-CA"); }
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function svg(tag, attrs, text) {
    var e = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function niceTicks(lo, hi, count) {
    var span = hi - lo || 1, step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = (span / count) / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var t = [], v = Math.ceil(lo / step) * step;
    for (; v <= hi + step * 1e-9; v += step) t.push(Math.round(v / step) * step);
    return { ticks: t, lo: Math.min(lo, Math.floor(lo / step) * step), hi: Math.max(hi, Math.ceil(hi / step) * step) };
  }
  // Bar whose data end (right or left) is rounded and whose baseline end is square.
  function barPath(x0, x1, y, h, r) {
    var left = Math.min(x0, x1), right = Math.max(x0, x1), w = right - left;
    r = Math.min(r, w / 2, h / 2);
    if (w <= 0) return "";
    if (x1 >= x0) return "M" + left + "," + y + "H" + (right - r) + "Q" + right + "," + y + " " + right + "," + (y + r) +
      "V" + (y + h - r) + "Q" + right + "," + (y + h) + " " + (right - r) + "," + (y + h) + "H" + left + "Z";
    return "M" + right + "," + y + "H" + (left + r) + "Q" + left + "," + y + " " + left + "," + (y + r) +
      "V" + (y + h - r) + "Q" + left + "," + (y + h) + " " + (left + r) + "," + (y + h) + "H" + right + "Z";
  }
  // Measure text so label columns fit their content instead of a guessed width.
  var measureCtx = document.createElement("canvas").getContext("2d");
  function textW(t, size, weight) {
    measureCtx.font = (weight || 400) + " " + size + "px " + getComputedStyle(document.body).fontFamily;
    return measureCtx.measureText(t).width;
  }
  function labelCol(labels, W) {
    var widest = Math.max.apply(null, labels.map(function (l) { return textW(l, 12, 650); }));
    return Math.min(Math.ceil(widest) + 2, Math.floor(W * 0.45));
  }
  function fit(t, maxW) {
    if (textW(t, 12) <= maxW) return t;
    while (t.length > 1 && textW(t + "…", 12) > maxW) t = t.slice(0, -1);
    return t + "…";
  }
  // Drop ticks that would sit closer than minGap pixels, so axis labels never collide.
  function thin(ticks, sx, minGap) {
    var kept = [], last = -Infinity;
    ticks.forEach(function (t) { var x = sx(t); if (x - last >= minGap) { kept.push(t); last = x; } });
    return kept;
  }
  function legend(id, items) {
    var box = document.getElementById(id); box.textContent = "";
    items.forEach(function (it) {
      var s = el("span"); var sw = el("i", { "class": "sw" }); sw.style.background = "var(" + it.color + ")";
      s.appendChild(sw); s.appendChild(document.createTextNode(it.label)); box.appendChild(s);
    });
  }
  function table(id, head, rows, numCols) {
    var t = el("table", { "class": "mini" }), tr = el("tr");
    head.forEach(function (h, i) { tr.appendChild(el("th", numCols.indexOf(i) >= 0 ? { "class": "num" } : null, h)); });
    var th = el("thead"); th.appendChild(tr); t.appendChild(th);
    var tb = el("tbody");
    rows.forEach(function (r) {
      var row = el("tr");
      r.forEach(function (c, i) { row.appendChild(el("td", numCols.indexOf(i) >= 0 ? { "class": "num" } : null, c)); });
      tb.appendChild(row);
    });
    t.appendChild(tb);
    var box = document.getElementById(id); box.textContent = ""; box.appendChild(t);
  }

  // ---------- tooltip ----------
  var tip = document.getElementById("tip");
  function showTip(evt, lines) {
    tip.textContent = "";
    lines.forEach(function (l, i) {
      var d = el("div");
      if (i === 0) d.appendChild(el("b", null, l));
      else { var parts = l.split("|"); d.appendChild(el("span", { "class": "k" }, parts[0] + (parts.length > 1 ? ": " : "")));
             if (parts.length > 1) d.appendChild(document.createTextNode(parts[1])); }
      tip.appendChild(d);
    });
    tip.style.opacity = 1;
    var x = evt.clientX + 14, y = evt.clientY + 14, r = tip.getBoundingClientRect();
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hideTip() { tip.style.opacity = 0; }
  function hover(target, lines, mark) {
    target.addEventListener("mousemove", function (e) { showTip(e, lines); if (mark) mark.style.opacity = 0.85; });
    target.addEventListener("mouseleave", function () { hideTip(); if (mark) mark.style.opacity = 1; });
  }

  // ---------- horizontal bar chart ----------
  function hbar(id, rows, opts) {
    var box = document.getElementById(id), W = Math.max(box.clientWidth, 280);
    var labelW = labelCol(rows.map(function (r) { return r.label; }), W), valueW = 52, rowH = 28, barH = 14, top = 4, axisH = 22;
    var plotX = labelW + 10, plotW = W - plotX - valueW;
    var H = top + rows.length * rowH + axisH;
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; })) || 1;
    var nt = niceTicks(0, max, Math.max(2, Math.floor(plotW / 90)));
    var sx = function (v) { return plotX + (v / nt.hi) * plotW; };
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H });
    thin(nt.ticks, sx, 48).forEach(function (t) {
      var x = sx(t);
      s.appendChild(svg("line", { x1: x, x2: x, y1: top, y2: top + rows.length * rowH, stroke: css("--grid"), "stroke-width": 1 }));
      s.appendChild(svg("text", { x: x, y: H - 6, "text-anchor": "middle", "font-size": 11, fill: css("--muted") },
        opts.tick ? opts.tick(t) : num(t)));
    });
    rows.forEach(function (r, i) {
      var y = top + i * rowH, by = y + (rowH - barH) / 2;
      s.appendChild(svg("text", { x: labelW, y: y + rowH / 2 + 4, "text-anchor": "end", "font-size": 12, fill: css("--ink-2") }, fit(r.label, labelW)));
      var p = svg("path", { d: barPath(plotX, sx(r.value), by, barH, 4), fill: css(r.color) });
      s.appendChild(p);
      s.appendChild(svg("text", { x: sx(r.value) + 6, y: y + rowH / 2 + 4, "font-size": 12, fill: css("--ink"),
        "font-variant-numeric": "tabular-nums" }, r.valueLabel));
      var hit = svg("rect", { x: 0, y: y, width: W, height: rowH, fill: "transparent" });
      hover(hit, r.tip, p); s.appendChild(hit);
    });
    s.appendChild(svg("line", { x1: plotX, x2: plotX, y1: top, y2: top + rows.length * rowH, stroke: css("--axis"), "stroke-width": 1 }));
    box.textContent = ""; box.appendChild(s);
  }

  // ---------- bridge (waterfall) chart ----------
  function bridge(id, steps, totalCents) {
    var box = document.getElementById(id), W = Math.max(box.clientWidth, 280);
    var rows = [], run = 0;
    steps.forEach(function (st) { rows.push({ label: st.label, from: run, to: run + st.cents, cents: st.cents }); run += st.cents; });
    rows.push({ label: "Gap between files", from: 0, to: totalCents, cents: totalCents, total: true });
    var valueW = Math.ceil(Math.max.apply(null, rows.map(function (r) { return textW(money(r.cents, !r.total), 12, 650); }))) + 14;
    var labelW = labelCol(rows.map(function (r) { return r.label; }), W - valueW), rowH = 30, barH = 16, top = 4, axisH = 22;
    var plotX = labelW + 10, plotW = W - plotX - valueW;
    var vals = [0]; rows.forEach(function (r) { vals.push(r.from, r.to); });
    var nt = niceTicks(Math.min.apply(null, vals), Math.max.apply(null, vals), Math.max(2, Math.floor(plotW / 90)));
    var sx = function (v) { return plotX + ((v - nt.lo) / (nt.hi - nt.lo)) * plotW; };
    var H = top + rows.length * rowH + axisH;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H });
    thin(nt.ticks, sx, 52).forEach(function (t) {
      var x = sx(t);
      s.appendChild(svg("line", { x1: x, x2: x, y1: top, y2: top + rows.length * rowH,
        stroke: t === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }));
      s.appendChild(svg("text", { x: x, y: H - 6, "text-anchor": "middle", "font-size": 11, fill: css("--muted") }, moneyShort(t)));
    });
    rows.forEach(function (r, i) {
      var y = top + i * rowH, by = y + (rowH - barH) / 2;
      var color = r.total ? "--total" : (r.cents >= 0 ? "--series-1" : "--series-2");
      s.appendChild(svg("text", { x: labelW, y: y + rowH / 2 + 4, "text-anchor": "end", "font-size": 12,
        fill: r.total ? css("--ink") : css("--ink-2"), "font-weight": r.total ? 650 : 400 }, fit(r.label, labelW)));
      var x0 = sx(r.from), x1 = sx(r.to);
      if (Math.abs(x1 - x0) < 2) x1 = x0 + (r.cents >= 0 ? 2 : -2);   // keep tiny steps visible
      var p = svg("path", { d: barPath(x0, x1, by, barH, 4), fill: css(color) });
      s.appendChild(p);
      if (i < rows.length - 1) {
        var cx = sx(r.to);
        s.appendChild(svg("line", { x1: cx, x2: cx, y1: by + barH, y2: by + rowH, stroke: css("--muted"),
          "stroke-width": 1, "stroke-dasharray": "2 2" }));
      }
      s.appendChild(svg("text", { x: W - 2, y: y + rowH / 2 + 4, "text-anchor": "end", "font-size": 12,
        fill: css("--ink"), "font-weight": r.total ? 650 : 400 }, money(r.cents, !r.total)));
      var hit = svg("rect", { x: 0, y: y, width: W, height: rowH, fill: "transparent" });
      hover(hit, r.total
        ? ["Gap between files", "Internal total|" + money(D.totals.internal_cents), "Processor total|" + money(D.totals.processor_cents),
           "Difference|" + money(totalCents)]
        : [r.label, "Effect on gap|" + money(r.cents, true), "Running total|" + money(r.to)], p);
      s.appendChild(hit);
    });
    box.textContent = ""; box.appendChild(s);
  }

  // ---------- header + KPIs ----------
  var T = D.totals;
  var inp = D.inputs || {};
  document.getElementById("subtitle").textContent =
    "Cutoff " + D.as_of + (inp.internal ? " · " + inp.internal + " vs " + inp.processor : "");
  var tie = document.getElementById("tie"), ok = T.unexplained_cents === 0;
  var dot = el("i", { "class": "dot" }); dot.style.background = ok ? "var(--good)" : "var(--critical)";
  tie.appendChild(dot);
  tie.appendChild(document.createTextNode(ok ? "Ties out: $0.00 unexplained" : "Does not tie: " + money(T.unexplained_cents) + " unexplained"));

  var open = D.exceptions.filter(function (e) { return e.cat !== "timing"; }).length;
  var kpis = [
    { label: "Internal ledger", value: money(T.internal_cents), note: num(T.internal_records) + " bookings" },
    { label: "Processor settlements", value: money(T.processor_cents), note: num(T.processor_records) + " lines" },
    { label: "Gap to explain", value: money(T.gap_cents), note: ok ? "Fully explained below" : "Not fully explained", cls: "" },
    { label: "Bookings matched", value: pct(T.internal_match_rate), note: pct(T.processor_match_rate) + " of settlement lines" },
    { label: "Exceptions to work", value: num(open), note: (D.exceptions.length - open) + " timing items expected to clear" },
    { label: "Rows rejected", value: num(T.rejected), note: "Unparseable dates or amounts" }
  ];
  var kbox = document.getElementById("kpis");
  kpis.forEach(function (k) {
    var c = el("div", { "class": "card kpi" });
    c.appendChild(el("div", { "class": "label" }, k.label));
    c.appendChild(el("div", { "class": "value" }, k.value));
    c.appendChild(el("div", { "class": "note" }, k.note));
    kbox.appendChild(c);
  });

  // ---------- static text, legends, tables ----------
  document.getElementById("bridge-desc").textContent =
    "Internal total minus processor total = " + money(T.gap_cents) + ". Each bar is one reason; together they explain all of it.";
  var timingCount = D.aging[0].count;
  document.getElementById("aging-desc").textContent =
    "Bookings with no settlement yet. Items under " + D.timing_lag_days + " days old are timing and should clear next cycle; older ones need chasing.";
  legend("lg-status", [{ label: "Matched", color: "--series-1" }, { label: "Needs review", color: "--series-2" }]);
  legend("lg-bridge", [{ label: "Widens gap", color: "--series-1" }, { label: "Narrows gap", color: "--series-2" }, { label: "Total", color: "--total" }]);
  legend("lg-aging", [{ label: "Timing (expected)", color: "--series-1" }, { label: "Chase processor", color: "--series-2" }]);

  table("tb-status", ["Outcome", "Items", "Internal $", "Processor $"],
    D.statuses.map(function (s) { return [s.label, num(s.items), money(s.internal_cents), money(s.processor_cents)]; }), [1, 2, 3]);
  table("tb-bridge", ["Reason", "Effect on gap"],
    D.bridge.map(function (b) { return [b.label, money(b.cents, true)]; }).concat([["Gap between files", money(T.gap_cents)]]), [1]);
  table("tb-aging", ["Age", "Bookings", "Amount"],
    D.aging.map(function (a) { return [a.label, num(a.count), money(a.cents)]; }), [1, 2]);

  if (D.evaluation) {
    var E = D.evaluation, acc = document.getElementById("acc");
    [["Link precision", E.precision, "pairings made that are correct"], ["Link recall", E.recall, "true pairings found"],
     ["Status accuracy", E.status_accuracy, "items in the right bucket"]].forEach(function (m) {
      var c = el("div"); c.appendChild(el("div", { "class": "v" }, (m[1] * 100).toFixed(2) + "%"));
      c.appendChild(el("div", { "class": "l" }, m[0] + ": " + m[2])); acc.appendChild(c);
    });
    table("tb-acc", ["Scenario", "Items", "Correct", "Rate"],
      E.scenarios.map(function (s) { return [s.name, num(s.items), num(s.correct), pct(s.correct / s.items)]; }), [1, 2, 3]);
  } else {
    document.getElementById("acc-card").style.display = "none";
  }

  // ---------- exceptions table ----------
  var CATS = [
    { key: "all", label: "All" },
    { key: "break", label: "Breaks", status: "critical", icon: "!" },
    { key: "chase", label: "Not settled", status: "serious", icon: "?" },
    { key: "not_booked", label: "Not booked", status: "serious", icon: "?" },
    { key: "duplicate", label: "Duplicates", status: "warning", icon: "2" },
    { key: "review", label: "Ambiguous", status: "warning", icon: "~" },
    { key: "timing", label: "Timing", status: "good", icon: "✓" }
  ];
  var CAT = {}; CATS.forEach(function (c) { CAT[c.key] = c; });
  var PAGE = 15;
  var state = { cat: "all", q: "", sort: "age", dir: -1, limit: PAGE };
  var chips = document.getElementById("chips");
  CATS.forEach(function (c) {
    var n = c.key === "all" ? D.exceptions.length : D.exceptions.filter(function (e) { return e.cat === c.key; }).length;
    if (c.key !== "all" && n === 0) return;
    var b = el("button", { "class": "chip", type: "button", "aria-pressed": c.key === state.cat ? "true" : "false", "data-cat": c.key });
    b.appendChild(document.createTextNode(c.label)); b.appendChild(el("span", { "class": "n" }, num(n)));
    b.addEventListener("click", function () {
      state.cat = c.key; state.limit = PAGE;
      chips.querySelectorAll(".chip").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      renderTable();
    });
    chips.appendChild(b);
  });
  document.getElementById("q").addEventListener("input", function (e) { state.q = e.target.value.toLowerCase(); state.limit = PAGE; renderTable(); });

  var COLS = [
    { key: "cat", label: "Queue" }, { key: "id", label: "Item" }, { key: "date", label: "Date" },
    { key: "age", label: "Age", num: true }, { key: "counterparty", label: "Counterparty" },
    { key: "ids", label: "Records" }, { key: "internal_cents", label: "Internal", num: true },
    { key: "processor_cents", label: "Processor", num: true }, { key: "diff_cents", label: "Difference", num: true },
    { key: "note", label: "Reason" }
  ];
  function renderTable() {
    var q = state.q;
    var rows = D.exceptions.filter(function (e) {
      if (state.cat !== "all" && e.cat !== state.cat) return false;
      if (!q) return true;
      return [e.id, e.counterparty, e.ref, e.note, e.internal.join(" "), e.processor.join(" ")].join(" ").toLowerCase().indexOf(q) >= 0;
    });
    var k = state.sort, dir = state.dir;
    rows.sort(function (a, b) {
      var x = a[k], y = b[k];
      if (k === "cat") { x = CAT[a.cat].label; y = CAT[b.cat].label; }
      if (x === null || x === undefined) x = -Infinity; if (y === null || y === undefined) y = -Infinity;
      return (x < y ? -1 : x > y ? 1 : 0) * dir || (a.id < b.id ? -1 : 1);
    });
    var t = document.getElementById("ex"); t.textContent = "";
    var head = el("thead"), hr = el("tr");
    COLS.forEach(function (c) {
      var th = el("th", c.num ? { "class": "num" } : {}, c.label);
      if (c.key !== "ids") {
        th.setAttribute("data-sort", c.key);
        th.setAttribute("aria-sort", state.sort === c.key ? (dir > 0 ? "ascending" : "descending") : "none");
        if (state.sort === c.key) th.appendChild(el("span", { "class": "arr" }, dir > 0 ? "▲" : "▼"));
        th.addEventListener("click", function () {
          if (state.sort === c.key) state.dir = -state.dir; else { state.sort = c.key; state.dir = c.num ? -1 : 1; }
          renderTable();
        });
      }
      hr.appendChild(th);
    });
    head.appendChild(hr); t.appendChild(head);
    var body = el("tbody");
    rows.slice(0, state.limit).forEach(function (e) {
      var tr = el("tr"), c = CAT[e.cat];
      var tdq = el("td"), tag = el("span", { "class": "tag" }), ic = el("span", { "class": "icon", "aria-hidden": "true" }, c.icon);
      ic.style.background = "var(--" + c.status + ")"; tag.appendChild(ic); tag.appendChild(document.createTextNode(c.label));
      tdq.appendChild(tag); tr.appendChild(tdq);
      tr.appendChild(el("td", { "class": "ids" }, e.id));
      tr.appendChild(el("td", { "class": "num" }, e.date));
      tr.appendChild(el("td", { "class": "num" }, e.age + "d"));
      tr.appendChild(el("td", null, e.counterparty));
      tr.appendChild(el("td", { "class": "ids" }, e.internal.concat(e.processor).join(", ")));
      tr.appendChild(el("td", { "class": "num" }, money(e.internal_cents)));
      tr.appendChild(el("td", { "class": "num" }, money(e.processor_cents)));
      tr.appendChild(el("td", { "class": "num" }, money(e.diff_cents, true)));
      tr.appendChild(el("td", { "class": "note" }, e.note));
      body.appendChild(tr);
    });
    if (!rows.length) { var tr0 = el("tr"), td0 = el("td", { colspan: COLS.length, "class": "empty" }, "No exceptions match this filter."); tr0.appendChild(td0); body.appendChild(tr0); }
    t.appendChild(body);
    var more = document.getElementById("more"); more.textContent = "";
    if (rows.length > state.limit) {
      var btn = el("button", { type: "button" }, "Show " + Math.min(PAGE, rows.length - state.limit) + " more of " + (rows.length - state.limit) + " remaining");
      btn.addEventListener("click", function () { state.limit += PAGE; renderTable(); });
      more.appendChild(btn);
    } else if (rows.length) {
      more.appendChild(el("span", { "class": "sub" }, "Showing all " + rows.length + " item" + (rows.length === 1 ? "" : "s")));
    }
  }

  // ---------- charts ----------
  function drawCharts() {
    hbar("ch-status", D.statuses.filter(function (s) { return s.items > 0; }).map(function (s) {
      return { label: s.label, value: s.items, valueLabel: num(s.items), color: s.matched ? "--series-1" : "--series-2",
        tip: [s.label, "Items|" + num(s.items), "Bookings|" + num(s.internal_records) + " · " + money(s.internal_cents),
              "Settlement lines|" + num(s.processor_records) + " · " + money(s.processor_cents)] };
    }), {});
    bridge("ch-bridge", D.bridge, T.gap_cents);
    hbar("ch-aging", D.aging.map(function (a, i) {
      var timing = a.hi < D.timing_lag_days;
      return { label: a.label, value: a.count, valueLabel: num(a.count), color: timing ? "--series-1" : "--series-2",
        tip: [a.label, "Bookings|" + num(a.count), "Amount|" + money(a.cents), "Action|" + (timing ? "Expected next cycle" : "Chase processor")] };
    }), {});
  }

  // ---------- footer ----------
  var foot = document.getElementById("foot");
  foot.appendChild(el("span", null, "Generated " + D.generated + " by "));
  foot.lastChild.appendChild(el("code", null, "python -m recon dashboard"));
  if (D.settings) foot.appendChild(el("span", null, "Date window " + D.settings.date_window_days + " days · fee tolerance " +
    money(D.settings.tolerance_cents) + " · fuzzy threshold " + D.settings.fuzzy_threshold));
  if (D.rejected.length) foot.appendChild(el("span", null, D.rejected.length + " rejected rows: " +
    D.rejected.map(function (r) { return r.source + " row " + r.row + " (" + r.reason + ")"; }).join("; ")));

  // ---------- theme toggle ----------
  var root = document.documentElement;
  try { var saved = localStorage.getItem("recon-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  document.getElementById("theme").addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") ? root.getAttribute("data-theme") === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("recon-theme", next); } catch (e) {}
    drawCharts();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawCharts);

  renderTable();
  drawCharts();
  var rt; addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(drawCharts, 120); });
})();
</script>
</body>
</html>
"""
