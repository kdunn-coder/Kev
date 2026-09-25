#!/usr/bin/env python3
"""Tests for the deal-registration allocation pipeline.

Plain stdlib, no pytest needed:

    python3 tests/test_core.py

Covers the parts where a silent error would corrupt the analysis without
raising: the concentration maths, the picklist normalization, currency and
date parsing, and an end-to-end run whose per-rep shares must reconcile to
the pod totals.
"""

from __future__ import annotations

import csv
import datetime as dt
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dealreg import analyze, export, metrics  # noqa: E402
from dealreg.loader import DateParser, load, parse_amount, shift_month  # noqa: E402
from dealreg.taxonomy import PodResolver, normalize_deal_type, normalize_stage, stage_outcome  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want, tol: float = 1e-9) -> None:
    if isinstance(want, float) or isinstance(got, float):
        good = (got is None and want is None) or (
            got is not None and want is not None and abs(got - want) <= tol
        )
    else:
        good = got == want
    print(("  PASS  " if good else "  FAIL  ") + name + (
        "" if good else f"   got {got!r}, want {want!r}"))
    if not good:
        FAILED.append(name)


def section(title: str) -> None:
    print("\n" + title)


# ---------------------------------------------------------------------------


def test_metrics() -> None:
    section("Concentration metrics")
    # A perfectly even split is the fixed point every measure must agree on.
    check("gini, even split", metrics.gini([5, 5, 5, 5]), 0.0)
    check("effective reps, even split", metrics.effective_reps([5, 5, 5, 5]), 4.0)
    check("hhi, even split of 4", metrics.hhi([5, 5, 5, 5]), 2500.0)

    # Total concentration is the other fixed point.
    check("effective reps, one rep holds all", metrics.effective_reps([10, 0, 0, 0]), 1.0)
    check("hhi, one rep holds all", metrics.hhi([10]), 10000.0)
    check("gini, single rep", metrics.gini([10]), 0.0)

    # Closed-form Gini for [1,2,3,4] is 0.25.
    check("gini, [1,2,3,4]", metrics.gini([1, 2, 3, 4]), 0.25)
    check("top1 share", metrics.top_n_share([5, 3, 2], 1), 50.0)
    check("top3 share", metrics.top_n_share([5, 3, 2, 1, 1], 3), 10 / 12 * 100)

    # Degenerate input must yield None, never 0.0 -- a pod-month with no regs
    # is "not measurable", and reporting 0.0 would read as "perfectly even".
    check("gini, no reps", metrics.gini([]), None)
    check("gini, all zero", metrics.gini([0, 0]), None)
    check("effective reps, all zero", metrics.effective_reps([0, 0]), None)
    check("hhi, all zero", metrics.hhi([0, 0]), None)

    c = metrics.concentration([4, 0, 2, 0, 0])
    check("active_reps excludes zeros", c.active_reps, 2)
    check("total counts only nonzero", c.total, 6)
    check("max_per_rep", c.max_per_rep, 4)
    check("min_per_rep ignores zeros", c.min_per_rep, 2)

    section("Trend fitting")
    slope, intercept = metrics.linear_trend([1, 2, 3, 4, 5])
    check("slope of a perfect line", slope, 1.0)
    check("intercept of a perfect line", intercept, 1.0)
    check("slope skips gaps", metrics.linear_trend([5, None, 3, None, 1])[0], -1.0)
    check("slope needs 2 points", metrics.linear_trend([3])[0], None)
    check("slope of a flat series", metrics.linear_trend([4, 4, 4])[0], 0.0)
    check("pct_change guards zero base", metrics.pct_change(0, 5), None)
    check("pct_change", metrics.pct_change(200, 250), 25.0)


def test_taxonomy() -> None:
    section("Deal-type normalization (picklists drift over 24 months)")
    for raw, want in [
        ("New Business", "New Business"),
        ("New Logo", "New Business"),          # renamed mid-window
        ("Net New", "New Business"),
        ("Existing Business - Add-On", "Add-On / Expansion"),
        ("Upsell", "Add-On / Expansion"),
        ("Renewal", "Renewal"),
        ("Existing Business - Renewal", "Renewal"),
        ("renewal add on", "Renewal"),         # renewal wins over add-on
        ("Competitive Migration", "Migration"),
        ("Rip and Replace", "Migration"),
        ("", "(unassigned)"),
        ("Something Unmapped", "Other"),
    ]:
        check(f"deal type {raw!r}", normalize_deal_type(raw), want)

    section("Stage outcome bucketing")
    for raw, want in [
        ("7 - Closed Won", "Won"),
        ("Closed Won", "Won"),
        ("0 - Closed Lost", "Lost"),
        ("Closed Lost - No Decision", "Lost"),
        ("Rejected - Duplicate", "Rejected / Duplicate"),
        ("Duplicate Registration", "Rejected / Duplicate"),
        ("Expired", "Expired"),
        ("1 - Submitted", "Open"),
        ("Pending Approval", "Open"),
        ("A Stage Nobody Mapped", "Open"),     # unknown but live
        ("", "(unassigned)"),
    ]:
        check(f"outcome {raw!r}", stage_outcome(raw), want)

    section("Stage display cleanup")
    check("strips numeric prefix", normalize_stage("3 - Technical Validation"), "Technical Validation")
    check("strips zero-padded prefix", normalize_stage("05. Negotiation"), "Negotiation")
    check("strips 'Stage n'", normalize_stage("Stage 2 - Discovery"), "Discovery")
    check("title-cases lowercase", normalize_stage("closed won"), "Closed Won")
    check("leaves mixed case alone", normalize_stage("POC"), "Poc")
    check("blank becomes unassigned", normalize_stage("  "), "(unassigned)")


def test_pod_resolution() -> None:
    section("Pod resolution")
    cfg = {
        "pods": ["UKI & Nordics", "Southern Europe", "DACH"],
        "pod_aliases": {
            "UKI & Nordics": ["uki nordics", "nordics"],
            "DACH": ["dach"],
            "Southern Europe": ["semea"],
        },
        "country_to_pod": {
            "DACH": ["germany"],
            "UKI & Nordics": ["united kingdom"],
        },
        "rep_overrides": {"Moved Rep": "DACH"},
        "excluded_owner_patterns": ["deal reg queue", "integration user"],
    }
    r = PodResolver.from_config(cfg)
    check("exact pod name", r.resolve(pod_value="DACH"), "DACH")
    check("alias", r.resolve(pod_value="UKI/Nordics"), "UKI & Nordics")
    check("alias, punctuation-insensitive", r.resolve(pod_value="uki  -  nordics"), "UKI & Nordics")
    check("substring inside a path", r.resolve(pod_value="EMEA - DACH - Enterprise"), "DACH")
    check("country fallback", r.resolve(country="Germany"), "DACH")
    check("territory beats country", r.resolve(pod_value="SEMEA", country="Germany"), "Southern Europe")
    check("rep override beats territory",
          r.resolve(rep="Moved Rep", pod_value="SEMEA"), "DACH")
    check("unresolvable is unassigned", r.resolve(pod_value="EMEA - Other"), "(unassigned)")
    check("unresolved value recorded", r.unresolved_pod_values.get("EMEA - Other"), 1)
    check("queue owner excluded", r.is_excluded_owner("Deal Reg Queue"), True)
    check("queue match is case-insensitive", r.is_excluded_owner("INTEGRATION USER"), True)
    check("real rep not excluded", r.is_excluded_owner("Aoife Brennan"), False)


def test_parsing() -> None:
    section("Currency parsing (exports carry mixed locales)")
    for raw, want in [
        ("$174,000", 174000.0),
        ("1,234.56", 1234.56),      # US
        ("1.234,56", 1234.56),      # European
        ("EUR 8,000.00", 8000.0),
        ("1234", 1234.0),
        ("(500)", -500.0),          # accounting negative
        ("", None),
        ("n/a", None),
    ]:
        check(f"amount {raw!r}", parse_amount(raw), want)

    section("Date parsing")
    p = DateParser("auto")
    p.sniff(["25/12/2025", "01/02/2025"])       # a day > 12 proves dayfirst
    check("infers dayfirst", p.parse("03/04/2025"), dt.date(2025, 4, 3))
    p2 = DateParser("auto")
    p2.sniff(["12/25/2025"])                    # a month > 12 proves monthfirst
    check("infers monthfirst", p2.parse("03/04/2025"), dt.date(2025, 3, 4))
    p3 = DateParser("auto")
    p3.sniff(["01/02/2025"])
    check("flags a genuinely ambiguous column", p3.was_ambiguous, True)
    check("explicit dayfirst is honoured",
          DateParser("dayfirst").parse("03/04/2025"), dt.date(2025, 4, 3))
    d = DateParser()
    check("ISO", d.parse("2025-06-07"), dt.date(2025, 6, 7))
    check("ISO with time", d.parse("2025-06-07 14:32:00"), dt.date(2025, 6, 7))
    check("text month", d.parse("7 Jun 2025"), dt.date(2025, 6, 7))
    check("text month, long form", d.parse("June 7, 2025"), dt.date(2025, 6, 7))
    check("2-digit year", DateParser("dayfirst").parse("07/06/25"), dt.date(2025, 6, 7))
    check("blank", d.parse(""), None)
    check("garbage", d.parse("not a date"), None)
    check("impossible date", d.parse("2025-02-30"), None)

    section("Month arithmetic")
    check("forward across a year", shift_month("2024-11", 3), "2025-02")
    check("back across a year", shift_month("2025-01", -1), "2024-12")
    check("24-month window start", shift_month("2026-08", -23), "2024-09")
    check("zero shift", shift_month("2025-05", 0), "2025-05")


def test_end_to_end() -> None:
    """Run the pipeline on a fixture and reconcile the outputs.

    The invariant that matters: every rep's monthly counts must sum to the pod
    month total, and each month's shares must sum to 100%. If windowing,
    exclusion or pod resolution ever double-counts or drops a record, this is
    what catches it.
    """
    section("End-to-end reconciliation")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "export.csv"
        rows = []
        # 3 reps in DACH, 2 in Southern Europe, over 3 months, plus rows that
        # must be excluded: a queue owner and an out-of-window record.
        for month in ("2026-06", "2026-07", "2026-08"):
            for rep, n in (("Rep A", 5), ("Rep B", 3), ("Rep C", 1)):
                for i in range(n):
                    rows.append([f"{month}-10", rep, "DACH", "New Business", "2 - Approved"])
            for rep, n in (("Rep D", 4), ("Rep E", 4)):
                for i in range(n):
                    rows.append([f"{month}-10", rep, "SEMEA", "Renewal", "7 - Closed Won"])
        rows.append(["2026-07-10", "Deal Reg Queue", "DACH", "New Business", "2 - Approved"])
        rows.append(["2019-01-10", "Rep A", "DACH", "New Business", "2 - Approved"])
        with src.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Created Date", "Opportunity Owner", "Sales Territory",
                        "Opportunity Type", "Stage"])
            w.writerows(rows)

        result = load(src)
        resolver = PodResolver.from_config({
            "pods": ["UKI & Nordics", "Southern Europe", "DACH"],
            "pod_aliases": {"DACH": ["dach"], "Southern Europe": ["semea"]},
            "excluded_owner_patterns": ["deal reg queue"],
        })
        a = analyze.run(result, resolver, months=3, end_month="2026-08")

        check("window length", len(a.months), 3)
        check("window start", a.months[0], "2026-06")
        check("out-of-window row excluded", a.out_of_window, 1)

        dach = a.pod("DACH")
        semea = a.pod("Southern Europe")
        check("DACH total (queue row excluded)", dach.total, 27)
        check("Southern Europe total", semea.total, 24)
        check("DACH reps", len(dach.reps), 3)
        check("queue owner not a rep", "Deal Reg Queue" in dach.reps, False)

        for pod in (dach, semea):
            for m in a.months:
                rep_sum = sum(rw.count(m) for rw in pod.reps.values())
                check(f"{pod.pod} {m}: rep counts sum to pod total",
                      rep_sum, pod.monthly_total[m])
                shares = analyze.share_matrix(pod, list(pod.reps))
                total_share = sum(v for row in shares.values() for v in [row[a.months.index(m)]] if v)
                check(f"{pod.pod} {m}: shares sum to 100%", total_share, 100.0, tol=1e-6)

        # 5/3/1 out of 9 -> effective reps = 1/(0.5556^2+0.3333^2+0.1111^2)
        exp_eff = 1.0 / ((5 / 9) ** 2 + (3 / 9) ** 2 + (1 / 9) ** 2)
        check("DACH effective reps matches closed form",
              dach.concentration["2026-06"].effective_reps, exp_eff, tol=1e-9)
        check("Southern Europe is perfectly even (gini 0)",
              semea.concentration["2026-06"].gini, 0.0)
        check("Southern Europe effective reps = 2",
              semea.concentration["2026-06"].effective_reps, 2.0)

        # Deal types and outcomes must land in the right buckets.
        check("DACH is all new business",
              dach.month_deal_type[("2026-06", "New Business")], 9)
        check("Southern Europe is all renewal",
              semea.month_deal_type[("2026-06", "Renewal")], 8)
        check("Southern Europe outcome is Won",
              semea.month_outcome[("2026-06", "Won")], 8)

        # Writers must produce every file and the pivot must reconcile.
        outdir = tmpdir / "out"
        written = export.write_csvs(a, outdir)
        export.write_findings(a, outdir)
        check("all CSVs written", all(p.exists() for p in written), True)
        check("findings written", (outdir / "findings.md").exists(), True)

        with (outdir / "monthly_regs_by_rep_pivot.csv").open(encoding="utf-8") as fh:
            pivot = list(csv.DictReader(fh))
        dach_rows = [r for r in pivot if r["pod"] == "DACH" and r["rep"] != "TOTAL"]
        check("pivot rep rows reconcile to pod total",
              sum(int(r["total"]) for r in dach_rows), 27)
        dach_total_row = next(r for r in pivot if r["pod"] == "DACH" and r["rep"] == "TOTAL")
        check("pivot TOTAL row matches", int(dach_total_row["total"]), 27)


def test_cli_runs() -> None:
    section("CLI smoke test")
    sample = ROOT / "samples" / "sample_deal_registrations.csv"
    if not sample.exists():
        subprocess.run([sys.executable, str(ROOT / "samples" / "make_sample.py")],
                       check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "-i", str(sample), "-o", tmp],
            capture_output=True, text=True,
        )
        check("run.py exits 0", r.returncode, 0)
        if r.returncode != 0:
            print(r.stderr[-2000:])
        for name in ("findings.md", "dashboard.html", "pod_summary.csv",
                     "monthly_regs_by_rep.csv", "data_quality.json"):
            check(f"produced {name}", (Path(tmp) / name).exists(), True)
        html = (Path(tmp) / "dashboard.html").read_text(encoding="utf-8")
        check("dashboard placeholder replaced", "__DASHBOARD_DATA__" not in html, True)
        check("dashboard carries data", '"pods"' in html, True)

        d = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "-i", str(sample), "--describe"],
            capture_output=True, text=True,
        )
        check("--describe exits 0", d.returncode, 0)
        check("--describe reports no missing required field",
              "*** REQUIRED ***" not in d.stdout, True)


def main() -> int:
    test_metrics()
    test_taxonomy()
    test_pod_resolution()
    test_parsing()
    test_end_to_end()
    test_cli_runs()
    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} FAILED:")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
