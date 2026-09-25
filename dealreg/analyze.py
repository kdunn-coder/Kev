"""The analysis pipeline: normalized records in, trending tables out.

Produces, for each of the configured EMEA pods, over a 24-month window:

  * deal registrations per rep per month (the raw allocation matrix)
  * each rep's share of their pod's regs per month (the allocation question)
  * stage / outcome and deal-type splits per rep per month
  * pod-level concentration per month, plus a first-12 vs last-12 comparison
  * per-rep first-half vs second-half movement, with tenure flags

The tenure handling is the part that most affects how the output reads. A rep
who joined in month 18 will always look like they "gained share", and a rep who
left in month 6 will always look like they lost it. Both are noise, not
allocation policy, so every rep row carries `first_month`, `last_month`,
`months_active` and a `tenure_flag`, and the narrative findings only draw
conclusions from reps present in both halves of the window.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from . import metrics
from .loader import LoadResult, Record, iter_months, month_of, shift_month
from .taxonomy import (
    UNASSIGNED,
    DEAL_TYPES,
    OUTCOMES,
    PodResolver,
    normalize_deal_type,
    normalize_stage,
    stage_outcome,
    stage_sort_index,
)

# A rep needs at least this many regs across the window to appear in the
# per-rep narrative. Below it, share percentages swing wildly on single deals
# and say more about rounding than about allocation.
MIN_REGS_FOR_NARRATIVE = 5


@dataclass
class RepWindow:
    """One rep's activity across the analysis window."""

    rep: str
    pod: str
    monthly: dict[str, int] = field(default_factory=dict)
    monthly_amount: dict[str, float] = field(default_factory=dict)
    by_stage: Counter = field(default_factory=Counter)
    by_outcome: Counter = field(default_factory=Counter)
    by_deal_type: Counter = field(default_factory=Counter)
    # (month, deal_type) -> count and (month, outcome) -> count
    month_deal_type: Counter = field(default_factory=Counter)
    month_outcome: Counter = field(default_factory=Counter)
    isrs: Counter = field(default_factory=Counter)

    def count(self, month: str) -> int:
        return self.monthly.get(month, 0)

    @property
    def total(self) -> int:
        return sum(self.monthly.values())


@dataclass
class PodWindow:
    pod: str
    months: list[str]
    reps: dict[str, RepWindow] = field(default_factory=dict)
    monthly_total: dict[str, int] = field(default_factory=dict)
    monthly_amount: dict[str, float] = field(default_factory=dict)
    month_deal_type: Counter = field(default_factory=Counter)
    month_outcome: Counter = field(default_factory=Counter)
    month_stage: Counter = field(default_factory=Counter)
    concentration: dict[str, metrics.Concentration] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.monthly_total.values())


@dataclass
class Analysis:
    months: list[str]
    pods: list[PodWindow]
    #: Pods present in config but with no data in the window.
    empty_pods: list[str]
    #: Same structure as `pods`, for ISR-based allocation.
    isr_pods: list[PodWindow]
    stages_seen: list[str]
    deal_types_seen: list[str]
    outcomes_seen: list[str]
    data_quality: dict[str, object]
    window_label: str
    #: Records that fell outside the 24-month window.
    out_of_window: int
    generated_at: str

    def pod(self, name: str) -> PodWindow | None:
        return next((p for p in self.pods if p.pod == name), None)


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------


def choose_window(
    records: Iterable[Record], months: int = 24,
    end_month: str | None = None, today: dt.date | None = None,
) -> list[str]:
    """Pick the `months`-long window ending at `end_month`.

    Default end is the last *complete* calendar month, so a part-month does not
    show up as a collapse in volume at the right edge of every chart. If the
    data itself ends earlier than that, the data wins -- there is no point
    reserving chart space for months the export does not cover.
    """
    today = today or dt.date.today()
    if end_month is None:
        last_complete = shift_month(month_of(today), -1)
        data_months = [month_of(r.created_date) for r in records if r.created_date]
        if data_months:
            end_month = min(last_complete, max(data_months))
        else:
            end_month = last_complete
    start_month = shift_month(end_month, -(months - 1))
    return list(iter_months(start_month, end_month))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _accumulate(
    records: list[Record], months: list[str], resolver: PodResolver,
    owner_attr: str, configured_pods: list[str],
) -> tuple[list[PodWindow], list[str]]:
    """Build PodWindows keyed on `owner_attr` ('rep' or 'isr')."""
    month_set = set(months)
    pods: dict[str, PodWindow] = {}

    for rec in records:
        if rec.month not in month_set:
            continue
        owner = getattr(rec, owner_attr) or UNASSIGNED
        if resolver.is_excluded_owner(owner):
            continue
        pod = pods.setdefault(rec.pod, PodWindow(pod=rec.pod, months=months))
        rw = pod.reps.setdefault(owner, RepWindow(rep=owner, pod=rec.pod))

        rw.monthly[rec.month] = rw.monthly.get(rec.month, 0) + 1
        rw.by_stage[rec.stage] += 1
        rw.by_outcome[rec.outcome] += 1
        rw.by_deal_type[rec.deal_type] += 1
        rw.month_deal_type[(rec.month, rec.deal_type)] += 1
        rw.month_outcome[(rec.month, rec.outcome)] += 1
        if owner_attr == "rep" and rec.isr:
            rw.isrs[rec.isr] += 1

        pod.monthly_total[rec.month] = pod.monthly_total.get(rec.month, 0) + 1
        pod.month_deal_type[(rec.month, rec.deal_type)] += 1
        pod.month_outcome[(rec.month, rec.outcome)] += 1
        pod.month_stage[(rec.month, rec.stage)] += 1

        if rec.amount is not None:
            rw.monthly_amount[rec.month] = rw.monthly_amount.get(rec.month, 0.0) + rec.amount
            pod.monthly_amount[rec.month] = pod.monthly_amount.get(rec.month, 0.0) + rec.amount

    for pod in pods.values():
        for month in months:
            counts = [rw.count(month) for rw in pod.reps.values()]
            pod.concentration[month] = metrics.concentration(counts)

    # Configured pods first and in config order, then anything else (including
    # UNASSIGNED) so unmapped data is visible rather than buried.
    ordered: list[PodWindow] = []
    for name in configured_pods:
        if name in pods:
            ordered.append(pods[name])
    for name in sorted(pods):
        if name not in configured_pods:
            ordered.append(pods[name])
    empty = [name for name in configured_pods if name not in pods]
    return ordered, empty


def run(
    load_result: LoadResult, resolver: PodResolver, *,
    months: int = 24, end_month: str | None = None,
    today: dt.date | None = None,
) -> Analysis:
    records = load_result.records

    # Normalize every record before windowing, so the data-quality report
    # covers the whole export rather than just the window.
    for rec in records:
        rec.pod = resolver.resolve(
            rep=rec.rep, isr=rec.isr, pod_value=rec.pod_raw, country=rec.country
        )
        rec.stage = normalize_stage(rec.stage_raw)
        rec.outcome = stage_outcome(rec.stage_raw)
        rec.deal_type = normalize_deal_type(rec.deal_type_raw)
        rec.month = month_of(rec.created_date) if rec.created_date else ""

    window = choose_window(records, months=months, end_month=end_month, today=today)
    window_set = set(window)
    out_of_window = sum(1 for r in records if r.month and r.month not in window_set)

    configured = list(resolver.pods)
    rep_pods, empty_pods = _accumulate(records, window, resolver, "rep", configured)
    isr_pods, _ = _accumulate(
        [r for r in records if r.isr], window, resolver, "isr", configured
    )

    in_window = [r for r in records if r.month in window_set]
    stages_seen = sorted({r.stage for r in in_window}, key=stage_sort_index)
    deal_types_seen = [t for t in DEAL_TYPES if any(r.deal_type == t for r in in_window)]
    deal_types_seen += sorted(
        {r.deal_type for r in in_window} - set(DEAL_TYPES)
    )
    outcomes_seen = [o for o in OUTCOMES if any(r.outcome == o for r in in_window)]

    quality = _data_quality(load_result, resolver, records, window, out_of_window)

    return Analysis(
        months=window,
        pods=rep_pods,
        empty_pods=empty_pods,
        isr_pods=isr_pods,
        stages_seen=stages_seen,
        deal_types_seen=deal_types_seen,
        outcomes_seen=outcomes_seen,
        data_quality=quality,
        window_label=f"{window[0]} to {window[-1]}",
        out_of_window=out_of_window,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def _data_quality(
    load_result: LoadResult, resolver: PodResolver,
    records: list[Record], window: list[str], out_of_window: int,
) -> dict[str, object]:
    window_set = set(window)
    in_window = [r for r in records if r.month in window_set]
    n = len(in_window) or 1
    return {
        "rows_in_file": load_result.total_rows,
        "rows_loaded": len(records),
        "rows_in_window": len(in_window),
        "rows_outside_window": out_of_window,
        "skipped": dict(load_result.skipped),
        "resolved_headers": dict(load_result.resolved_headers),
        "unresolved_fields": [
            f for f in ("isr", "pod", "stage", "deal_type", "country", "amount", "partner")
            if f not in load_result.resolved_headers
        ],
        "notes": list(load_result.notes),
        "pod_provenance": dict(resolver.provenance),
        "unresolved_pod_values": dict(
            sorted(resolver.unresolved_pod_values.items(), key=lambda kv: -kv[1])[:25]
        ),
        "unresolved_countries": dict(
            sorted(resolver.unresolved_countries.items(), key=lambda kv: -kv[1])[:25]
        ),
        "pct_missing_rep": sum(1 for r in in_window if not r.rep) / n * 100,
        "pct_missing_isr": sum(1 for r in in_window if not r.isr) / n * 100,
        "pct_missing_stage": sum(1 for r in in_window if r.stage == UNASSIGNED) / n * 100,
        "pct_missing_deal_type": sum(1 for r in in_window if r.deal_type == UNASSIGNED) / n * 100,
        "pct_unassigned_pod": sum(1 for r in in_window if r.pod == UNASSIGNED) / n * 100,
        "pct_missing_amount": sum(1 for r in in_window if r.amount is None) / n * 100,
    }


# ---------------------------------------------------------------------------
# Derived views used by the reporters
# ---------------------------------------------------------------------------


@dataclass
class RepTrend:
    """First-half vs second-half movement for one rep."""

    rep: str
    pod: str
    total: int
    h1_count: int
    h2_count: int
    count_delta: int
    count_pct_change: float | None
    h1_share: float | None
    h2_share: float | None
    share_delta_pp: float | None
    first_month: str
    last_month: str
    months_active: int
    monthly_slope: float | None
    tenure_flag: str
    top_deal_type: str
    new_business_pct: float | None
    renewal_pct: float | None
    open_pct: float | None
    won_pct: float | None
    rejected_pct: float | None


def rep_trends(pod: PodWindow) -> list[RepTrend]:
    """Per-rep first-12 vs last-12 comparison for a pod."""
    months = pod.months
    half = len(months) // 2
    h1_months, h2_months = months[:half], months[half:]
    h1_pod_total = sum(pod.monthly_total.get(m, 0) for m in h1_months)
    h2_pod_total = sum(pod.monthly_total.get(m, 0) for m in h2_months)

    out: list[RepTrend] = []
    for rep, rw in pod.reps.items():
        active = [m for m in months if rw.count(m) > 0]
        if not active:
            continue
        h1 = sum(rw.count(m) for m in h1_months)
        h2 = sum(rw.count(m) for m in h2_months)
        h1_share = (h1 / h1_pod_total * 100) if h1_pod_total else None
        h2_share = (h2 / h2_pod_total * 100) if h2_pod_total else None

        # Tenure flags. `partial` covers reps whose activity does not span the
        # window; their share movement is mostly an artefact of arrival or
        # departure, and the narrative excludes them.
        first, last = active[0], active[-1]
        joined_late = first != months[0] and months.index(first) >= 3
        left_early = last != months[-1] and (len(months) - 1 - months.index(last)) >= 3
        if joined_late and left_early:
            flag = "partial (joined late, stopped early)"
        elif joined_late:
            flag = "partial (no regs until " + first + ")"
        elif left_early:
            flag = "partial (no regs after " + last + ")"
        else:
            flag = "full window"

        slope, _ = metrics.linear_trend([rw.count(m) for m in months])
        total = rw.total
        dt_counts = rw.by_deal_type
        oc = rw.by_outcome

        out.append(
            RepTrend(
                rep=rep, pod=pod.pod, total=total,
                h1_count=h1, h2_count=h2, count_delta=h2 - h1,
                count_pct_change=metrics.pct_change(h1, h2),
                h1_share=h1_share, h2_share=h2_share,
                share_delta_pp=(
                    (h2_share - h1_share) if (h1_share is not None and h2_share is not None) else None
                ),
                first_month=first, last_month=last, months_active=len(active),
                monthly_slope=slope, tenure_flag=flag,
                top_deal_type=(dt_counts.most_common(1)[0][0] if dt_counts else UNASSIGNED),
                new_business_pct=(dt_counts["New Business"] / total * 100) if total else None,
                renewal_pct=(dt_counts["Renewal"] / total * 100) if total else None,
                open_pct=(oc["Open"] / total * 100) if total else None,
                won_pct=(oc["Won"] / total * 100) if total else None,
                rejected_pct=(oc["Rejected / Duplicate"] / total * 100) if total else None,
            )
        )
    out.sort(key=lambda r: -r.total)
    return out


@dataclass
class PodTrend:
    """Pod-level first-half vs second-half comparison."""

    pod: str
    total: int
    h1_total: int
    h2_total: int
    volume_pct_change: float | None
    h1_active_reps: float | None
    h2_active_reps: float | None
    h1_regs_per_rep: float | None
    h2_regs_per_rep: float | None
    regs_per_rep_pct_change: float | None
    h1_effective_reps: float | None
    h2_effective_reps: float | None
    h1_gini: float | None
    h2_gini: float | None
    gini_delta: float | None
    h1_top1: float | None
    h2_top1: float | None
    h1_top3: float | None
    h2_top3: float | None
    gini_slope: float | None
    volume_slope: float | None
    effective_reps_slope: float | None
    reps_ever_active: int
    reps_full_window: int


def pod_trend(pod: PodWindow) -> PodTrend:
    months = pod.months
    half = len(months) // 2
    h1_months, h2_months = months[:half], months[half:]

    def agg(ms: list[str], attr: str) -> float | None:
        return metrics.safe_mean([getattr(pod.concentration[m], attr) for m in ms])

    def total_of(ms: list[str]) -> int:
        return sum(pod.monthly_total.get(m, 0) for m in ms)

    h1_total, h2_total = total_of(h1_months), total_of(h2_months)
    h1_reps, h2_reps = agg(h1_months, "active_reps"), agg(h2_months, "active_reps")
    h1_per_rep = (h1_total / len(h1_months) / h1_reps) if h1_reps else None
    h2_per_rep = (h2_total / len(h2_months) / h2_reps) if h2_reps else None
    h1_gini, h2_gini = agg(h1_months, "gini"), agg(h2_months, "gini")

    trends = rep_trends(pod)
    gini_slope, _ = metrics.linear_trend([pod.concentration[m].gini for m in months])
    vol_slope, _ = metrics.linear_trend([float(pod.monthly_total.get(m, 0)) for m in months])
    eff_slope, _ = metrics.linear_trend([pod.concentration[m].effective_reps for m in months])

    return PodTrend(
        pod=pod.pod, total=pod.total, h1_total=h1_total, h2_total=h2_total,
        volume_pct_change=metrics.pct_change(h1_total, h2_total),
        h1_active_reps=h1_reps, h2_active_reps=h2_reps,
        h1_regs_per_rep=h1_per_rep, h2_regs_per_rep=h2_per_rep,
        regs_per_rep_pct_change=metrics.pct_change(h1_per_rep, h2_per_rep),
        h1_effective_reps=agg(h1_months, "effective_reps"),
        h2_effective_reps=agg(h2_months, "effective_reps"),
        h1_gini=h1_gini, h2_gini=h2_gini,
        gini_delta=((h2_gini - h1_gini) if (h1_gini is not None and h2_gini is not None) else None),
        h1_top1=agg(h1_months, "top1_share"), h2_top1=agg(h2_months, "top1_share"),
        h1_top3=agg(h1_months, "top3_share"), h2_top3=agg(h2_months, "top3_share"),
        gini_slope=gini_slope, volume_slope=vol_slope, effective_reps_slope=eff_slope,
        reps_ever_active=len(trends),
        reps_full_window=sum(1 for t in trends if t.tenure_flag == "full window"),
    )


def mix_series(pod: PodWindow, dimension: str) -> tuple[list[str], dict[str, list[int]]]:
    """Per-month counts by deal type or outcome for a pod.

    Returns (category order, category -> per-month counts).
    """
    source = pod.month_deal_type if dimension == "deal_type" else pod.month_outcome
    order_ref = DEAL_TYPES if dimension == "deal_type" else OUTCOMES
    present = {cat for (_m, cat) in source}
    categories = [c for c in order_ref if c in present]
    categories += sorted(present - set(order_ref))
    return categories, {
        cat: [source.get((m, cat), 0) for m in pod.months] for cat in categories
    }


def share_matrix(pod: PodWindow, reps: list[str]) -> dict[str, list[float | None]]:
    """Per-rep share of pod regs per month, as percentages.

    None (rather than 0) for a month where the pod itself had no regs, so the
    heatmap can distinguish "rep got nothing" from "nothing to get".
    """
    out: dict[str, list[float | None]] = {}
    for rep in reps:
        rw = pod.reps[rep]
        row: list[float | None] = []
        for m in pod.months:
            pod_total = pod.monthly_total.get(m, 0)
            row.append((rw.count(m) / pod_total * 100) if pod_total else None)
        out[rep] = row
    return out
