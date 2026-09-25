#!/usr/bin/env python3
"""Entry point for the EMEA deal-registration allocation analysis.

    python3 run.py --input path/to/export.csv

Add --describe to see how columns were detected without running the analysis --
do that first on a new export, then pin anything mis-detected in
config/columns.yml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dealreg import analyze, export, report  # noqa: E402
from dealreg.loader import load, resolve_headers, _read_rows  # noqa: E402
from dealreg.taxonomy import PodResolver  # noqa: E402


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            f"Reading {path.name} needs PyYAML. Run `pip install pyyaml`."
        )
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Trend deal-registration allocation per rep/ISR across EMEA pods.",
    )
    ap.add_argument("--input", "-i", required=True, type=Path,
                    help="CRM export of deal registrations (.csv or .xlsx)")
    ap.add_argument("--outdir", "-o", type=Path, default=ROOT / "out",
                    help="where to write CSVs, findings.md and the dashboard (default: ./out)")
    ap.add_argument("--pods-config", type=Path, default=ROOT / "config" / "pods.yml")
    ap.add_argument("--columns-config", type=Path, default=ROOT / "config" / "columns.yml")
    ap.add_argument("--months", type=int, default=24,
                    help="window length in months (default: 24)")
    ap.add_argument("--end-month", type=str, default=None,
                    help="last month of the window as YYYY-MM "
                         "(default: last complete month, or the last month in the data)")
    ap.add_argument("--describe", action="store_true",
                    help="print detected columns and sample values, then exit")
    args = ap.parse_args(argv)

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    columns_cfg = load_yaml(args.columns_config)
    pods_cfg = load_yaml(args.pods_config)
    if not pods_cfg.get("pods"):
        raise SystemExit(
            f"No pods defined in {args.pods_config}. Add a `pods:` list "
            f"(e.g. UKI & Nordics, Southern Europe, DACH)."
        )

    mapping = {k: v for k, v in (columns_cfg.get("mapping") or {}).items()}

    if args.describe:
        headers, rows = _read_rows(args.input)
        resolved, notes = resolve_headers(headers, mapping)
        print(f"\n{args.input.name}: {len(rows)} data rows, {len(headers)} columns\n")
        print("Detected column mapping")
        print("-" * 72)
        for fld in ("created_date", "rep", "isr", "pod", "stage", "deal_type",
                    "deal_reg_id", "country", "amount", "partner", "account",
                    "close_date"):
            header = resolved.get(fld)
            if header:
                samples = [str(r.get(header, "")).strip() for r in rows[:400]]
                samples = [s for s in samples if s][:3]
                print(f"  {fld:<14} <- {header!r}")
                if samples:
                    print(f"  {'':<14}    e.g. {', '.join(repr(s) for s in samples)}")
            else:
                required = fld in ("created_date", "rep")
                print(f"  {fld:<14} <- NOT FOUND{'  *** REQUIRED ***' if required else ''}")
        unused = [h for h in headers if h and h not in set(resolved.values())]
        if unused:
            print(f"\nUnused columns ({len(unused)}):")
            for h in unused:
                print(f"  - {h}")
        for n in notes:
            print(f"\nNote: {n}")
        print("\nPin any wrong mapping in config/columns.yml under `mapping:`.\n")
        return 0

    result = load(
        args.input,
        mapping_overrides=mapping,
        date_order=str(columns_cfg.get("date_order", "auto")),
        drop_rows_without_rep=bool(columns_cfg.get("drop_rows_without_rep", False)),
    )
    if not result.records:
        raise SystemExit(
            "No usable rows found. Run with --describe to check column detection."
        )

    resolver = PodResolver.from_config(pods_cfg)
    analysis = analyze.run(
        result, resolver, months=args.months, end_month=args.end_month
    )

    args.outdir.mkdir(parents=True, exist_ok=True)
    csvs = export.write_csvs(analysis, args.outdir)
    findings = export.write_findings(analysis, args.outdir)
    dashboard = report.write_dashboard(analysis, args.outdir)

    q = analysis.data_quality
    print(f"\nWindow: {analysis.window_label} ({len(analysis.months)} months)")
    print(f"Rows: {q['rows_in_file']} in file, {q['rows_in_window']} in window, "
          f"{q['rows_outside_window']} outside")
    if q["skipped"]:
        for reason, n in q["skipped"].items():
            print(f"  skipped {n}: {reason}")
    print("\nPods:")
    for pod in analysis.pods:
        t = analyze.pod_trend(pod)
        eff = (f"{t.h1_effective_reps:.1f} -> {t.h2_effective_reps:.1f}"
               if t.h1_effective_reps and t.h2_effective_reps else "n/a")
        print(f"  {pod.pod:<20} {pod.total:>5} regs   "
              f"{t.reps_ever_active:>2} reps   effective reps {eff}")
    if analysis.empty_pods:
        print(f"\n  No data for configured pods: {', '.join(analysis.empty_pods)}")
    if q["unresolved_pod_values"]:
        print(f"\n  {len(q['unresolved_pod_values'])} territory value(s) did not map "
              f"to a pod - see data_quality.json")

    print(f"\nWrote {len(csvs)} CSVs, findings.md and dashboard.html to {args.outdir}/")
    print(f"  Read first: {findings}")
    print(f"  Dashboard:  {dashboard}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
