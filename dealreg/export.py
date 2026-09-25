"""CSV outputs and the narrative findings write-up.

The CSVs are the working artefacts -- long/tidy format so they pivot cleanly in
Excel or load straight into a BI tool. `findings.md` is the read-it-first
summary that states what actually changed in each pod over the window.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .analyze import (
    Analysis,
    MIN_REGS_FOR_NARRATIVE,
    PodWindow,
    RepTrend,
    mix_series,
    pod_trend,
    rep_trends,
)
from .taxonomy import UNASSIGNED, stage_sort_index


def _w(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _r(v: float | None, places: int = 2) -> str:
    return "" if v is None else f"{v:.{places}f}"


def write_csvs(analysis: Analysis, outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # 1. The core allocation matrix, long format.
    rows = []
    for pod in analysis.pods:
        for month in analysis.months:
            pod_total = pod.monthly_total.get(month, 0)
            for rep, rw in sorted(pod.reps.items()):
                count = rw.count(month)
                rows.append([
                    pod.pod, rep, month, count, pod_total,
                    _r((count / pod_total * 100) if pod_total else None),
                    _r(rw.monthly_amount.get(month), 0),
                ])
    p = outdir / "monthly_regs_by_rep.csv"
    _w(p, ["pod", "rep", "month", "deal_regs", "pod_month_total",
           "share_of_pod_pct", "amount"], rows)
    written.append(p)

    # 2. Wide pivot: one row per rep, one column per month. This is the table
    #    people actually want to eyeball, so it ships as its own file.
    rows = []
    for pod in analysis.pods:
        for trend in rep_trends(pod):
            rw = pod.reps[trend.rep]
            rows.append(
                [pod.pod, trend.rep, trend.total]
                + [rw.count(m) for m in analysis.months]
            )
        rows.append([pod.pod, "TOTAL", pod.total]
                    + [pod.monthly_total.get(m, 0) for m in analysis.months])
    p = outdir / "monthly_regs_by_rep_pivot.csv"
    _w(p, ["pod", "rep", "total"] + analysis.months, rows)
    written.append(p)

    # 3. Rep x month x deal type.
    rows = []
    for pod in analysis.pods:
        for rep, rw in sorted(pod.reps.items()):
            for (month, deal_type), count in sorted(rw.month_deal_type.items()):
                rows.append([pod.pod, rep, month, deal_type, count])
    p = outdir / "monthly_regs_by_rep_dealtype.csv"
    _w(p, ["pod", "rep", "month", "deal_type", "deal_regs"], rows)
    written.append(p)

    # 4. Rep x month x stage outcome.
    rows = []
    for pod in analysis.pods:
        for rep, rw in sorted(pod.reps.items()):
            for (month, outcome), count in sorted(rw.month_outcome.items()):
                rows.append([pod.pod, rep, month, outcome, count])
    p = outdir / "monthly_regs_by_rep_outcome.csv"
    _w(p, ["pod", "rep", "month", "stage_outcome", "deal_regs"], rows)
    written.append(p)

    # 5. Rep x detailed CRM stage (window totals; stage is a current-state
    #    field, so a per-month cut of it would imply history the export lacks).
    rows = []
    for pod in analysis.pods:
        for rep, rw in sorted(pod.reps.items()):
            for stage, count in sorted(rw.by_stage.items(), key=lambda kv: stage_sort_index(kv[0])):
                rows.append([pod.pod, rep, stage, count])
    p = outdir / "regs_by_rep_stage.csv"
    _w(p, ["pod", "rep", "crm_stage", "deal_regs"], rows)
    written.append(p)

    # 6. Pod-month concentration.
    rows = []
    for pod in analysis.pods:
        for month in analysis.months:
            c = pod.concentration[month]
            rows.append([
                pod.pod, month, c.total, c.active_reps,
                _r(c.mean_per_rep), c.max_per_rep or "", c.min_per_rep or "",
                _r(c.effective_reps), _r(c.gini, 4), _r(c.hhi, 1),
                _r(c.top1_share, 1), _r(c.top3_share, 1),
                _r(pod.monthly_amount.get(month), 0),
            ])
    p = outdir / "pod_monthly_concentration.csv"
    _w(p, ["pod", "month", "deal_regs", "active_reps", "mean_per_rep",
           "max_per_rep", "min_per_rep", "effective_reps", "gini", "hhi",
           "top1_share_pct", "top3_share_pct", "amount"], rows)
    written.append(p)

    # 7. Per-rep first-half vs second-half movement.
    rows = []
    for pod in analysis.pods:
        for t in rep_trends(pod):
            rows.append([
                pod.pod, t.rep, t.total, t.h1_count, t.h2_count, t.count_delta,
                _r(t.count_pct_change, 1), _r(t.h1_share, 2), _r(t.h2_share, 2),
                _r(t.share_delta_pp, 2), _r(t.monthly_slope, 3),
                t.first_month, t.last_month, t.months_active, t.tenure_flag,
                t.top_deal_type, _r(t.new_business_pct, 1), _r(t.renewal_pct, 1),
                _r(t.open_pct, 1), _r(t.won_pct, 1), _r(t.rejected_pct, 1),
            ])
    p = outdir / "rep_halfyear_trend.csv"
    _w(p, ["pod", "rep", "total_regs", "first_12mo", "last_12mo", "count_delta",
           "count_pct_change", "first_12mo_share_pct", "last_12mo_share_pct",
           "share_delta_pp", "regs_per_month_slope", "first_active_month",
           "last_active_month", "months_active", "tenure_flag",
           "top_deal_type", "new_business_pct", "renewal_pct",
           "open_pct", "won_pct", "rejected_pct"], rows)
    written.append(p)

    # 8. Pod-level summary.
    rows = []
    for pod in analysis.pods:
        t = pod_trend(pod)
        rows.append([
            t.pod, t.total, t.h1_total, t.h2_total, _r(t.volume_pct_change, 1),
            _r(t.h1_active_reps, 1), _r(t.h2_active_reps, 1),
            _r(t.h1_regs_per_rep), _r(t.h2_regs_per_rep),
            _r(t.regs_per_rep_pct_change, 1),
            _r(t.h1_effective_reps), _r(t.h2_effective_reps),
            _r(t.h1_gini, 4), _r(t.h2_gini, 4), _r(t.gini_delta, 4),
            _r(t.h1_top1, 1), _r(t.h2_top1, 1), _r(t.h1_top3, 1), _r(t.h2_top3, 1),
            _r(t.volume_slope, 3), _r(t.gini_slope, 5), _r(t.effective_reps_slope, 4),
            t.reps_ever_active, t.reps_full_window,
        ])
    p = outdir / "pod_summary.csv"
    _w(p, ["pod", "total_regs", "first_12mo", "last_12mo", "volume_pct_change",
           "first_12mo_avg_active_reps", "last_12mo_avg_active_reps",
           "first_12mo_regs_per_rep_per_month", "last_12mo_regs_per_rep_per_month",
           "regs_per_rep_pct_change", "first_12mo_effective_reps",
           "last_12mo_effective_reps", "first_12mo_gini", "last_12mo_gini",
           "gini_delta", "first_12mo_top1_pct", "last_12mo_top1_pct",
           "first_12mo_top3_pct", "last_12mo_top3_pct",
           "volume_slope_per_month", "gini_slope_per_month",
           "effective_reps_slope_per_month",
           "reps_ever_active", "reps_active_full_window"], rows)
    written.append(p)

    # 9. Pod x month x deal type and x outcome, for mix trending.
    for dim, name in (("deal_type", "pod_monthly_dealtype.csv"),
                      ("outcome", "pod_monthly_outcome.csv")):
        rows = []
        for pod in analysis.pods:
            cats, series = mix_series(pod, dim)
            for cat in cats:
                for month, count in zip(analysis.months, series[cat]):
                    total = pod.monthly_total.get(month, 0)
                    rows.append([pod.pod, month, cat, count,
                                 _r((count / total * 100) if total else None, 1)])
        p = outdir / name
        _w(p, ["pod", "month", dim, "deal_regs", "pct_of_pod_month"], rows)
        written.append(p)

    # 10. ISR view, same shape as the rep view.
    rows = []
    for pod in analysis.isr_pods:
        for month in analysis.months:
            pod_total = pod.monthly_total.get(month, 0)
            for isr, rw in sorted(pod.reps.items()):
                count = rw.count(month)
                rows.append([pod.pod, isr, month, count, pod_total,
                             _r((count / pod_total * 100) if pod_total else None)])
    p = outdir / "monthly_regs_by_isr.csv"
    _w(p, ["pod", "isr", "month", "deal_regs", "pod_month_total",
           "share_of_pod_pct"], rows)
    written.append(p)

    # 11. Data quality, as JSON since it is nested.
    p = outdir / "data_quality.json"
    p.write_text(json.dumps(analysis.data_quality, indent=2, default=str), encoding="utf-8")
    written.append(p)

    return written


# ---------------------------------------------------------------------------
# Narrative findings
# ---------------------------------------------------------------------------


def _describe_direction(delta: float | None, up: str, down: str, flat: str,
                        threshold: float) -> str:
    if delta is None:
        return flat
    if delta > threshold:
        return up
    if delta < -threshold:
        return down
    return flat


def _pod_narrative(pod: PodWindow, months: list[str]) -> list[str]:
    t = pod_trend(pod)
    trends = rep_trends(pod)
    half = len(months) // 2
    lines: list[str] = [f"### {pod.pod}", ""]

    lines.append(
        f"**{t.total} deal registrations** across the window, "
        f"{t.h1_total} in the first 12 months ({months[0]}–{months[half - 1]}) "
        f"and {t.h2_total} in the last 12 ({months[half]}–{months[-1]})"
        + (f", a {t.volume_pct_change:+.0f}% change in volume." if t.volume_pct_change is not None else ".")
    )
    lines.append("")

    # Headcount vs volume: the confound that has to be separated before any
    # statement about "allocation per rep" means anything.
    if t.h1_active_reps and t.h2_active_reps:
        lines.append(
            f"- **Reps receiving regs:** {t.h1_active_reps:.1f}/month on average in the "
            f"first half, {t.h2_active_reps:.1f}/month in the second "
            f"({t.reps_ever_active} reps appear at some point; {t.reps_full_window} "
            f"are active across the whole window)."
        )
    if t.h1_regs_per_rep and t.h2_regs_per_rep:
        lines.append(
            f"- **Regs per active rep per month:** {t.h1_regs_per_rep:.1f} → "
            f"{t.h2_regs_per_rep:.1f}"
            + (f" ({t.regs_per_rep_pct_change:+.0f}%)." if t.regs_per_rep_pct_change is not None else ".")
        )

    if t.h1_effective_reps and t.h2_effective_reps:
        direction = _describe_direction(
            t.h2_effective_reps - t.h1_effective_reps,
            "spread across more reps", "concentrated onto fewer reps",
            "held roughly steady", 0.3,
        )
        lines.append(
            f"- **Effective number of reps sharing the regs:** "
            f"{t.h1_effective_reps:.1f} → {t.h2_effective_reps:.1f} — allocation {direction}."
        )
    if t.h1_gini is not None and t.h2_gini is not None:
        direction = _describe_direction(
            t.gini_delta, "became more uneven", "became more even",
            "was essentially unchanged", 0.03,
        )
        lines.append(
            f"- **Gini (0 = perfectly even):** {t.h1_gini:.3f} → {t.h2_gini:.3f}, "
            f"i.e. the distribution {direction}."
        )
    if t.h1_top1 is not None and t.h2_top1 is not None:
        lines.append(
            f"- **Top rep's monthly share:** {t.h1_top1:.0f}% → {t.h2_top1:.0f}%; "
            f"**top 3:** {t.h1_top3:.0f}% → {t.h2_top3:.0f}%."
        )
    lines.append("")

    # Movers, restricted to reps present in both halves so arrivals and
    # departures do not masquerade as allocation shifts.
    comparable = [
        x for x in trends
        if x.tenure_flag == "full window" and x.total >= MIN_REGS_FOR_NARRATIVE
        and x.share_delta_pp is not None
    ]
    if comparable:
        gainers = [x for x in sorted(comparable, key=lambda x: -(x.share_delta_pp or 0))
                   if (x.share_delta_pp or 0) > 0][:3]
        losers = [x for x in sorted(comparable, key=lambda x: (x.share_delta_pp or 0))
                  if (x.share_delta_pp or 0) < 0][:3]

        def movers(label: str, group: list[RepTrend], empty_note: str) -> None:
            lines.append(f"**{label}**")
            lines.append("")
            if not group:
                lines.append(f"- {empty_note}")
            for x in group:
                lines.append(
                    f"- {x.rep}: {x.h1_share:.1f}% → {x.h2_share:.1f}% "
                    f"({x.share_delta_pp:+.1f} pp; {x.h1_count} → {x.h2_count} regs)"
                )
            lines.append("")

        movers(
            "Biggest share gains (reps active across the full window):", gainers,
            "None — every rep present for the whole window lost share, which means the "
            "gains went to reps who joined mid-window (listed below).",
        )
        movers(
            "Biggest share losses:", losers,
            "None — no rep present for the whole window lost share.",
        )

    partial = [x for x in trends if x.tenure_flag != "full window" and x.total >= MIN_REGS_FOR_NARRATIVE]
    if partial:
        lines.append(
            "**Reps with partial windows** — excluded from the share-movement "
            "comparison above, because arriving or leaving mid-window shows up as "
            "a share change that is not an allocation decision:"
        )
        lines.append("")
        for x in sorted(partial, key=lambda x: -x.total)[:10]:
            lines.append(
                f"- {x.rep}: {x.total} regs, active {x.first_month} → {x.last_month} "
                f"({x.months_active} of {len(months)} months) — {x.tenure_flag}"
            )
        lines.append("")

    # Mix: a rep loaded with renewals is not being allocated new opportunity.
    cats, series = mix_series(pod, "deal_type")
    if cats:
        lines.append("**Deal-type mix, first 12 months vs last 12:**")
        lines.append("")
        lines.append("| Deal type | First 12mo | Last 12mo | Share then | Share now |")
        lines.append("|---|---:|---:|---:|---:|")
        for cat in cats:
            vals = series[cat]
            h1, h2 = sum(vals[:half]), sum(vals[half:])
            s1 = (h1 / t.h1_total * 100) if t.h1_total else 0
            s2 = (h2 / t.h2_total * 100) if t.h2_total else 0
            lines.append(f"| {cat} | {h1} | {h2} | {s1:.0f}% | {s2:.0f}% |")
        lines.append("")

    cats, series = mix_series(pod, "outcome")
    if cats:
        lines.append("**Stage outcome mix, first 12 months vs last 12:**")
        lines.append("")
        lines.append("| Outcome | First 12mo | Last 12mo | Share then | Share now |")
        lines.append("|---|---:|---:|---:|---:|")
        for cat in cats:
            vals = series[cat]
            h1, h2 = sum(vals[:half]), sum(vals[half:])
            s1 = (h1 / t.h1_total * 100) if t.h1_total else 0
            s2 = (h2 / t.h2_total * 100) if t.h2_total else 0
            lines.append(f"| {cat} | {h1} | {h2} | {s1:.0f}% | {s2:.0f}% |")
        lines.append("")
        lines.append(
            "> Outcome reflects each registration's stage *as of the export*, not "
            "its stage at the time it was created. Recent months therefore skew "
            "toward Open simply because those deals have had less time to resolve — "
            "do not read that as deteriorating quality."
        )
        lines.append("")

    return lines


def write_findings(analysis: Analysis, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    q = analysis.data_quality
    lines: list[str] = [
        "# EMEA deal-registration allocation trending",
        "",
        f"Window: **{analysis.window_label}** ({len(analysis.months)} months). "
        f"Generated {analysis.generated_at}.",
        "",
        "Allocation is measured by **registration creation month** — the month a "
        "reg landed on a rep — not close date. Each registration counts once, "
        "against the rep who owns it.",
        "",
        "## How to read this",
        "",
        "Three numbers have to be read together, because volume alone cannot tell "
        "you whether allocation changed:",
        "",
        "1. **Total regs** — how much came in.",
        "2. **Active reps** — how many people it was split between.",
        "3. **Effective reps / Gini / top-rep share** — how evenly it was split.",
        "",
        "A pod can hold volume flat, lose two reps, and hand every remaining rep "
        "more work without any allocation *policy* changing. Conversely, rising "
        "concentration with stable headcount is a real allocation shift. The "
        "per-pod sections below separate those two cases.",
        "",
        "## Pods",
        "",
    ]

    for pod in analysis.pods:
        if pod.pod == UNASSIGNED:
            continue
        lines += _pod_narrative(pod, analysis.months)

    unassigned = analysis.pod(UNASSIGNED)
    if unassigned and unassigned.total:
        lines += [
            f"### {UNASSIGNED}",
            "",
            f"**{unassigned.total} registrations ({q['pct_unassigned_pod']:.1f}% of the "
            f"window) could not be assigned to a pod.** They are reported here rather "
            f"than distributed, so pod totals stay honest. Fix this by filling in "
            f"`rep_overrides` in `config/pods.yml` — see `data_quality.json` for the "
            f"raw territory and country values that did not resolve.",
            "",
        ]

    if analysis.empty_pods:
        lines += [
            "### Pods with no data",
            "",
            "These pods are configured but had zero registrations in the window, "
            "which usually means the territory values in the export do not match "
            "the aliases in `config/pods.yml`:",
            "",
        ] + [f"- {p}" for p in analysis.empty_pods] + [""]

    # Data quality, last but deliberately not optional reading.
    lines += ["## Data quality", "", "| Check | Value |", "|---|---:|"]
    lines += [
        f"| Rows in file | {q['rows_in_file']} |",
        f"| Rows loaded | {q['rows_loaded']} |",
        f"| Rows inside the {len(analysis.months)}-month window | {q['rows_in_window']} |",
        f"| Rows outside the window | {q['rows_outside_window']} |",
        f"| Missing rep/owner | {q['pct_missing_rep']:.1f}% |",
        f"| Missing ISR | {q['pct_missing_isr']:.1f}% |",
        f"| Missing stage | {q['pct_missing_stage']:.1f}% |",
        f"| Missing deal type | {q['pct_missing_deal_type']:.1f}% |",
        f"| Unassigned pod | {q['pct_unassigned_pod']:.1f}% |",
        f"| Missing amount | {q['pct_missing_amount']:.1f}% |",
        "",
    ]

    if q["skipped"]:
        lines += ["**Rows skipped:**", ""]
        lines += [f"- {reason}: {n}" for reason, n in q["skipped"].items()]
        lines.append("")

    if q["unresolved_fields"]:
        lines += [
            "**Fields with no matching column in the export** — the analysis runs "
            "without them, but these dimensions will be blank:",
            "",
        ]
        lines += [f"- `{f}`" for f in q["unresolved_fields"]]
        lines.append("")

    lines += ["**Columns used:**", "", "| Field | Header in your export |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(q["resolved_headers"].items())]
    lines.append("")

    if q["notes"]:
        lines += ["**Notes:**", ""] + [f"- {n}" for n in q["notes"]] + [""]

    if q["unresolved_pod_values"]:
        lines += [
            "**Territory values that did not map to a pod** (add these to "
            "`pod_aliases` in `config/pods.yml`):",
            "",
        ]
        lines += [f"- `{v}` ({n} rows)" for v, n in q["unresolved_pod_values"].items()]
        lines.append("")

    if q["unresolved_countries"]:
        lines += ["**Country values that did not map to a pod:**", ""]
        lines += [f"- `{v}` ({n} rows)" for v, n in q["unresolved_countries"].items()]
        lines.append("")

    path = outdir / "findings.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
