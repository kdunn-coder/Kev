"""Allocation-concentration metrics.

The question driving this analysis is not "how many deal regs did we get" but
"how evenly were they spread across the reps in a pod, and did that change".
Raw counts answer the first; these metrics answer the second.

Every metric here is computed over the per-rep registration counts within a
single pod-month, and every one is reported, because they fail in different
ways:

  effective_reps  The headline number. 1 / sum(share^2), the inverse-Simpson
                  index. Reads directly: "this pod's regs were effectively
                  spread across 4.2 reps". Comparable month to month even when
                  headcount changes, which is exactly the confound here.
  gini            Inequality on 0..1. Sensitive across the whole distribution,
                  so it catches a long tail of reps getting scraps, which
                  top-share numbers miss.
  hhi             0..10000. The regulator's concentration measure; included
                  because it is the one a CRO is most likely to already know.
  top1 / top3     Blunt but immediately legible: "the top rep took 38%".
  active_reps     Reps with >= 1 reg. The denominator behind everything else,
                  and a trend in its own right -- concentration rising while
                  active_reps falls is a headcount story, not an allocation one.

A caution that matters for reading the output: with few reps these metrics are
noisy, and `effective_reps` can never exceed `active_reps`. A pod-month with 3
active reps cannot look "well distributed" the way one with 12 can, so compare
a pod against itself over time, not against another pod.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Sequence


@dataclass
class Concentration:
    total: int
    active_reps: int
    effective_reps: float | None
    gini: float | None
    hhi: float | None
    top1_share: float | None
    top3_share: float | None
    mean_per_rep: float | None
    max_per_rep: int | None
    min_per_rep: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def gini(counts: Sequence[float]) -> float | None:
    """Gini coefficient of a distribution of counts.

    0.0 = every rep got the same number of regs; approaching 1.0 = one rep got
    everything. Returns None when there is nothing to measure (no reps, or no
    regs at all).
    """
    values = sorted(float(c) for c in counts)
    n = len(values)
    if n == 0:
        return None
    total = sum(values)
    if total <= 0:
        return None
    if n == 1:
        return 0.0
    # Standard mean-difference form: sum((2i - n - 1) * x_i) / (n * sum(x))
    weighted = sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(values))
    return weighted / (n * total)


def hhi(counts: Sequence[float]) -> float | None:
    """Herfindahl-Hirschman Index, scaled 0..10000."""
    total = sum(float(c) for c in counts)
    if total <= 0:
        return None
    return sum((float(c) / total) ** 2 for c in counts) * 10000.0


def effective_reps(counts: Sequence[float]) -> float | None:
    """Inverse-Simpson index: the effective number of reps sharing the regs.

    Equals the rep count when regs are split perfectly evenly, and drops toward
    1 as they pile onto one rep.
    """
    total = sum(float(c) for c in counts)
    if total <= 0:
        return None
    sum_sq = sum((float(c) / total) ** 2 for c in counts)
    return (1.0 / sum_sq) if sum_sq > 0 else None


def top_n_share(counts: Sequence[float], n: int) -> float | None:
    """Share of regs held by the top `n` reps, as a percentage."""
    total = sum(float(c) for c in counts)
    if total <= 0:
        return None
    top = sorted((float(c) for c in counts), reverse=True)[:n]
    return sum(top) / total * 100.0


def concentration(counts: Sequence[int]) -> Concentration:
    """Compute every concentration metric for one pod-month.

    `counts` must be the per-rep registration counts for reps considered part
    of the pod that month. Reps with zero are excluded from `active_reps` but
    should be passed in as zeros if you want them to drag the Gini down --
    `summarize` in analyze.py passes only non-zero reps, so metrics describe
    the distribution among reps who actually received work.
    """
    nonzero = [c for c in counts if c > 0]
    total = int(sum(nonzero))
    return Concentration(
        total=total,
        active_reps=len(nonzero),
        effective_reps=effective_reps(nonzero),
        gini=gini(nonzero),
        hhi=hhi(nonzero),
        top1_share=top_n_share(nonzero, 1),
        top3_share=top_n_share(nonzero, 3),
        mean_per_rep=(total / len(nonzero)) if nonzero else None,
        max_per_rep=max(nonzero) if nonzero else None,
        min_per_rep=min(nonzero) if nonzero else None,
    )


def linear_trend(values: Sequence[float | None]) -> tuple[float | None, float | None]:
    """Ordinary-least-squares slope and intercept over a series, skipping gaps.

    Returns (slope_per_month, intercept). Used to put a direction and a
    magnitude on a 24-point series rather than eyeballing the first and last
    points, which are the two noisiest observations in any short series.
    """
    pairs = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    n = len(pairs)
    if n < 2:
        return None, None
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    denom = sum((x - mean_x) ** 2 for x, _ in pairs)
    if denom == 0:
        return None, None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denom
    return slope, mean_y - slope * mean_x


def pct_change(before: float | None, after: float | None) -> float | None:
    """Percentage change from `before` to `after`, or None if undefined."""
    if before is None or after is None or before == 0:
        return None
    return (after - before) / abs(before) * 100.0


def safe_mean(values: Sequence[float | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return (sum(present) / len(present)) if present else None
