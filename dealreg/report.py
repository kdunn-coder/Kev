"""Builds the self-contained HTML dashboard.

This module only shapes the data payload; all layout, color and chart drawing
lives in `templates/dashboard.html`, which is written against the validated
dataviz palette and rendered client-side from the injected JSON. Keeping the
split here means the chart code can be edited without touching the analysis.
"""

from __future__ import annotations

import json
from pathlib import Path

from .analyze import Analysis, PodWindow, mix_series, pod_trend, rep_trends, share_matrix
from .taxonomy import UNASSIGNED, stage_sort_index

TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"

#: Reps shown individually in the per-pod charts. Beyond this the tail is
#: folded into an "Other reps" row rather than generating more rows nobody can
#: read; the CSVs always carry every rep.
MAX_REPS_CHARTED = 14


def _pod_payload(pod: PodWindow, months: list[str]) -> dict:
    trends = rep_trends(pod)
    charted = trends[:MAX_REPS_CHARTED]
    tail = trends[MAX_REPS_CHARTED:]

    reps_payload = []
    shares = share_matrix(pod, [t.rep for t in charted])
    for t in charted:
        rw = pod.reps[t.rep]
        reps_payload.append({
            "rep": t.rep,
            "total": t.total,
            "monthly": [rw.count(m) for m in months],
            "share": shares[t.rep],
            "h1_share": t.h1_share,
            "h2_share": t.h2_share,
            "share_delta_pp": t.share_delta_pp,
            "h1_count": t.h1_count,
            "h2_count": t.h2_count,
            "count_pct_change": t.count_pct_change,
            "first_month": t.first_month,
            "last_month": t.last_month,
            "months_active": t.months_active,
            "tenure_flag": t.tenure_flag,
            "full_window": t.tenure_flag == "full window",
            "top_deal_type": t.top_deal_type,
            "new_business_pct": t.new_business_pct,
            "renewal_pct": t.renewal_pct,
            "open_pct": t.open_pct,
            "won_pct": t.won_pct,
            "rejected_pct": t.rejected_pct,
            "isrs": [f"{n} ({c})" for n, c in rw.isrs.most_common(4)],
        })

    if tail:
        monthly = [sum(pod.reps[t.rep].count(m) for t in tail) for m in months]
        share = [
            (c / pod.monthly_total[m] * 100) if pod.monthly_total.get(m) else None
            for c, m in zip(monthly, months)
        ]
        reps_payload.append({
            "rep": f"Other reps ({len(tail)})",
            "total": sum(t.total for t in tail),
            "monthly": monthly, "share": share,
            "h1_share": None, "h2_share": None, "share_delta_pp": None,
            "h1_count": sum(t.h1_count for t in tail),
            "h2_count": sum(t.h2_count for t in tail),
            "count_pct_change": None,
            "first_month": "", "last_month": "", "months_active": 0,
            "tenure_flag": "aggregate of the long tail", "full_window": False,
            "top_deal_type": "", "new_business_pct": None, "renewal_pct": None,
            "open_pct": None, "won_pct": None, "rejected_pct": None,
            "isrs": [], "is_aggregate": True,
        })

    dt_cats, dt_series = mix_series(pod, "deal_type")
    oc_cats, oc_series = mix_series(pod, "outcome")
    t = pod_trend(pod)

    return {
        "pod": pod.pod,
        "total": pod.total,
        "monthly": [pod.monthly_total.get(m, 0) for m in months],
        "amount": [pod.monthly_amount.get(m, 0.0) for m in months],
        "concentration": {
            k: [getattr(pod.concentration[m], k) for m in months]
            for k in ("active_reps", "effective_reps", "gini", "top1_share", "mean_per_rep")
        },
        "trend": {
            "total": t.total, "h1_total": t.h1_total, "h2_total": t.h2_total,
            "volume_pct_change": t.volume_pct_change,
            "h1_active_reps": t.h1_active_reps, "h2_active_reps": t.h2_active_reps,
            "h1_regs_per_rep": t.h1_regs_per_rep, "h2_regs_per_rep": t.h2_regs_per_rep,
            "regs_per_rep_pct_change": t.regs_per_rep_pct_change,
            "h1_effective_reps": t.h1_effective_reps,
            "h2_effective_reps": t.h2_effective_reps,
            "h1_gini": t.h1_gini, "h2_gini": t.h2_gini, "gini_delta": t.gini_delta,
            "h1_top1": t.h1_top1, "h2_top1": t.h2_top1,
            "h1_top3": t.h1_top3, "h2_top3": t.h2_top3,
            "reps_ever_active": t.reps_ever_active,
            "reps_full_window": t.reps_full_window,
            "effective_reps_slope": t.effective_reps_slope,
            "gini_slope": t.gini_slope, "volume_slope": t.volume_slope,
        },
        "reps": reps_payload,
        "reps_total_count": len(trends),
        "deal_type": {"categories": dt_cats, "series": dt_series},
        "outcome": {"categories": oc_cats, "series": oc_series},
        "stages": sorted(
            ((s, sum(rw.by_stage[s] for rw in pod.reps.values()))
             for s in {s for rw in pod.reps.values() for s in rw.by_stage}),
            key=lambda kv: stage_sort_index(kv[0]),
        ),
    }


def build_payload(analysis: Analysis, banner: str | None = None) -> dict:
    return {
        "meta": {
            "synthetic_warning": banner,
            "window_label": analysis.window_label,
            "months": analysis.months,
            "n_months": len(analysis.months),
            "generated_at": analysis.generated_at,
            "empty_pods": analysis.empty_pods,
            "out_of_window": analysis.out_of_window,
            "max_reps_charted": MAX_REPS_CHARTED,
            "unassigned_label": UNASSIGNED,
        },
        "quality": analysis.data_quality,
        "pods": [_pod_payload(p, analysis.months) for p in analysis.pods],
        "isr_pods": [_pod_payload(p, analysis.months) for p in analysis.isr_pods],
    }


def write_dashboard(analysis: Analysis, outdir: Path, banner: str | None = None) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    payload = build_payload(analysis, banner=banner)
    html = TEMPLATE.read_text(encoding="utf-8")

    # `</script>` inside JSON would terminate the host script tag early, and a
    # lone U+2028/U+2029 is a literal line break in JS string context.
    blob = (
        json.dumps(payload, default=str, allow_nan=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    if "__DASHBOARD_DATA__" not in html:
        raise RuntimeError(f"{TEMPLATE} is missing the __DASHBOARD_DATA__ placeholder.")
    html = html.replace("__DASHBOARD_DATA__", blob)

    path = outdir / "dashboard.html"
    path.write_text(html, encoding="utf-8")
    return path
