"""Command line entry point.

  python -m recon generate                 # create sample files in data/
  python -m recon run                      # reconcile them, write output/
  python -m recon evaluate                 # run + score against the answer key
  python -m recon dashboard --out docs/index.html --truth data/ground_truth.json
                                           # write only the HTML dashboard
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from .config import ReconConfig
from .dashboard import write_dashboard
from .evaluate import evaluate, format_evaluation
from .generate import generate
from .loader import load_records
from .matcher import Reconciler, ReconResult
from .report import console_summary, write_csv, write_excel

ROOT = Path(__file__).resolve().parent.parent


def _display_path(path: Path) -> str:
    """Show paths relative to the current folder when possible, so output doesn't expose machine paths."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def reconcile(internal_path, processor_path, config_path) -> ReconResult:
    cfg = ReconConfig.load(config_path)
    return _reconcile(internal_path, processor_path, cfg)


def _reconcile(internal_path, processor_path, cfg: ReconConfig) -> ReconResult:
    internal, rej_i = load_records(internal_path, "internal", cfg.internal)
    processor, rej_p = load_records(processor_path, "processor", cfg.processor)
    as_of = date.fromisoformat(cfg.as_of_date) if cfg.as_of_date else None
    return Reconciler(cfg.matching).run(internal, processor, as_of=as_of, rejected=rej_i + rej_p)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recon", description="Bank vs. processor reconciliation engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="create sample input files with an answer key")
    g.add_argument("--out", default=str(ROOT / "data"))
    g.add_argument("--n", type=int, default=1000, help="number of internal transactions")
    g.add_argument("--seed", type=int, default=42)

    for name in ("run", "evaluate", "dashboard"):
        helps = {"run": "reconcile two files", "evaluate": "run and score vs. ground truth",
                 "dashboard": "reconcile and write only the HTML dashboard"}
        r = sub.add_parser(name, help=helps[name])
        r.add_argument("--internal", default=str(ROOT / "data" / "internal_ledger.csv"))
        r.add_argument("--processor", default=str(ROOT / "data" / "processor_settlement.csv"))
        r.add_argument("--config", default=str(ROOT / "config.json"))
        if name == "dashboard":
            r.add_argument("--out", default=str(ROOT / "output" / "dashboard.html"), help="HTML file to write")
            r.add_argument("--truth", default=None, help="optional answer key; adds the accuracy panel")
        else:
            r.add_argument("--out", default=str(ROOT / "output"))
        if name == "evaluate":
            r.add_argument("--truth", default=str(ROOT / "data" / "ground_truth.json"))

    args = p.parse_args(argv)

    if args.cmd == "generate":
        info = generate(args.out, n=args.n, seed=args.seed)
        print(f"Wrote {info['internal_rows']} internal and {info['processor_rows']} processor rows to {args.out}")
        print(json.dumps(info["scenarios"], indent=2))
        return 0

    cfg = ReconConfig.load(args.config)
    t0 = time.perf_counter()
    result = _reconcile(args.internal, args.processor, cfg)
    elapsed = time.perf_counter() - t0
    inputs = {"internal": Path(args.internal).name, "processor": Path(args.processor).name}
    ev = evaluate(result, args.truth) if getattr(args, "truth", None) else None

    if args.cmd == "dashboard":
        html = write_dashboard(result, args.out, cfg.matching, ev, inputs)
        print(f"Dashboard: {_display_path(html)}")
    else:
        out = Path(args.out)
        xlsx = write_excel(result, out / "recon_report.xlsx")
        csv_path = write_csv(result, out / "recon_results.csv")
        html = write_dashboard(result, out / "dashboard.html", cfg.matching, ev, inputs)
        print(console_summary(result))
        print(f"\n  Reconciled in {elapsed:.2f}s")
        print(f"  Report   : {_display_path(xlsx)}\n  Detail   : {_display_path(csv_path)}"
              f"\n  Dashboard: {_display_path(html)}")
        if ev:
            print()
            print(format_evaluation(ev))

    unexplained = result.proof()[-1][1]
    if unexplained != 0:
        print(f"\nERROR: proof does not tie out (unexplained {unexplained/100:,.2f})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
