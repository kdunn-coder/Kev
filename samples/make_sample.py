"""Generate a synthetic deal-registration export for testing the pipeline.

THE NUMBERS THIS PRODUCES ARE FABRICATED. They exist only to exercise the
pipeline and show the shape of the output. Do not read any business conclusion
from a run against this file.

Deliberately messy, because a real Salesforce export is messy and the loader
needs to survive it:
  * Salesforce-style verbose headers, mixed date formats, currency symbols
  * picklist drift mid-window ("New Business" -> "New Logo" partway through)
  * territory values that need alias matching, plus some that will not resolve
  * reps joining and leaving mid-window
  * queue/integration-user owners that should be filtered out
  * blank ISRs, blank stages, a footer row, a duplicate header-ish row

It also bakes in a known allocation story so the analysis has something real to
find: DACH concentrates onto fewer reps over the window, UKI & Nordics spreads
out, Southern Europe stays flat while shifting toward renewals.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

random.seed(20260925)

END_MONTH = (2026, 8)
N_MONTHS = 24

POD_REPS = {
    "UKI & Nordics": [
        ("Aoife Brennan", "2024-09", "2026-08"),
        ("Tom Whitfield", "2024-09", "2026-08"),
        ("Sigrid Halvorsen", "2024-09", "2026-08"),
        ("Callum Reid", "2025-04", "2026-08"),   # joins mid-window
        ("Niamh O'Connor", "2024-09", "2025-07"), # leaves mid-window
        ("Erik Lindqvist", "2025-09", "2026-08"),
    ],
    "Southern Europe": [
        ("Marco Ferrari", "2024-09", "2026-08"),
        ("Elena Castells", "2024-09", "2026-08"),
        ("Julien Marchand", "2024-09", "2026-08"),
        ("Sofia Almeida", "2024-11", "2026-08"),
        ("Yannis Papadakis", "2024-09", "2026-03"),
    ],
    "DACH": [
        ("Lukas Brandt", "2024-09", "2026-08"),
        ("Katharina Vogel", "2024-09", "2026-08"),
        ("Stefan Huber", "2024-09", "2026-08"),
        ("Nadia Keller", "2024-09", "2026-08"),
        ("Jonas Richter", "2024-09", "2025-11"),
    ],
}

POD_ISRS = {
    "UKI & Nordics": ["Priya Raman", "Dan Eriksen", "Mollie Shaw"],
    "Southern Europe": ["Chiara Bruno", "Paulo Neves"],
    "DACH": ["Tobias Frank", "Lena Wirth"],
}

# Territory strings as they might really appear, including messy variants that
# exercise alias and substring matching.
POD_TERRITORY_VALUES = {
    "UKI & Nordics": ["UKI & Nordics", "UKI/Nordics", "EMEA - UKI & Nordics - Commercial", "Nordics"],
    "Southern Europe": ["Southern Europe", "SEMEA", "EMEA - Southern Europe", "Iberia"],
    "DACH": ["DACH", "EMEA - DACH - Enterprise", "dach pod"],
}

POD_COUNTRIES = {
    "UKI & Nordics": ["United Kingdom", "Ireland", "Sweden", "Denmark", "Norway", "Finland"],
    "Southern Europe": ["Italy", "Spain", "France", "Portugal", "Greece", "Israel"],
    "DACH": ["Germany", "Austria", "Switzerland"],
}

PARTNERS = [
    "Northbridge Data", "Softcat", "Bechtle AG", "Computacenter", "SHI EMEA",
    "Arrow ECS", "TD Synnex", "Ingram Micro", "Cancom", "Econocom",
]

OPEN_STAGES = [
    "1 - Submitted", "2 - Approved", "3 - Discovery", "4 - Technical Validation",
    "5 - Proposal", "6 - Negotiation",
]
CLOSED_STAGES = ["7 - Closed Won", "0 - Closed Lost", "Rejected - Duplicate", "Expired"]


def months_range() -> list[tuple[int, int]]:
    y, m = END_MONTH
    out: list[tuple[int, int]] = []
    for _ in range(N_MONTHS):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


MONTHS = months_range()


def month_key(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def rep_weight(pod: str, rep: str, idx: int) -> float:
    """Allocation weight for a rep in month `idx` (0..23).

    This is where the synthetic "story" lives.
    """
    progress = idx / (N_MONTHS - 1)
    if pod == "DACH":
        # Concentrate onto the first two reps over time.
        order = [r[0] for r in POD_REPS[pod]]
        rank = order.index(rep)
        return max(0.05, (3.0 - rank * 0.6) * (1 + progress * (1.4 if rank < 2 else -0.55)))
    if pod == "UKI & Nordics":
        # Start lopsided, flatten out.
        order = [r[0] for r in POD_REPS[pod]]
        rank = order.index(rep)
        start = 3.5 - rank * 0.55
        return max(0.05, start * (1 - progress) + 1.6 * progress)
    # Southern Europe: broadly stable.
    return max(0.05, 1.0 + random.random() * 0.35)


def pod_volume(pod: str, idx: int) -> int:
    """Registrations for a pod in month `idx`, with seasonality."""
    y, m = MONTHS[idx]
    base = {"UKI & Nordics": 26, "Southern Europe": 19, "DACH": 23}[pod]
    trend = {"UKI & Nordics": 0.30, "Southern Europe": -0.05, "DACH": 0.12}[pod]
    base *= 1 + trend * (idx / (N_MONTHS - 1))
    if m in (7, 8):       # European summer slowdown
        base *= 0.62
    elif m == 12:
        base *= 0.78
    elif m in (3, 6, 9):  # quarter-end push
        base *= 1.18
    return max(2, int(random.gauss(base, base * 0.16)))


def pick_deal_type(idx: int, pod: str) -> str:
    """Deal type, with picklist drift and a Southern Europe renewal shift."""
    progress = idx / (N_MONTHS - 1)
    renewal_w = 18 + (26 * progress if pod == "Southern Europe" else 6 * progress)
    roll = random.random() * 100
    if roll < renewal_w:
        return random.choice(["Renewal", "Existing Business - Renewal"])
    if roll < renewal_w + 22:
        return random.choice(["Add-On", "Existing Business - Add-On", "Upsell"])
    if roll < renewal_w + 27:
        return random.choice(["Competitive Migration", "Migration"])
    # The picklist was renamed partway through the window.
    return "New Logo" if idx >= 14 else "New Business"


def pick_stage(idx: int) -> str:
    """Stage as of export time: recent months are mostly still open."""
    age = (N_MONTHS - 1) - idx
    p_open = max(0.05, min(0.92, 0.10 + 0.82 * (1 - age / 14))) if age < 14 else 0.05
    if random.random() < p_open:
        return random.choice(OPEN_STAGES)
    return random.choices(CLOSED_STAGES, weights=[42, 34, 16, 8])[0]


def fmt_date(d: dt.date, idx: int) -> str:
    """Mixed date formats, as happens when exports come from different users."""
    if idx % 7 == 0:
        return d.strftime("%d/%m/%Y")   # dayfirst, unambiguous when day > 12
    if idx % 5 == 0:
        return d.strftime("%d %b %Y")
    return d.strftime("%Y-%m-%d")


HEADERS = [
    "Deal Registration: Deal Registration Name",
    "Deal Registration: Created Date",
    "Opportunity Owner",
    "Inside Sales Rep",
    "Sales Territory",
    "Account Name",
    "Billing Country",
    "Partner Account",
    "Opportunity Type",
    "Stage",
    "Amount (converted)",
    "Close Date",
]


def main() -> None:
    out = Path(__file__).resolve().parent / "sample_deal_registrations.csv"
    rows: list[list[str]] = []
    seq = 1000

    for idx, (y, m) in enumerate(MONTHS):
        mk = month_key(y, m)
        for pod, reps in POD_REPS.items():
            eligible = [r for r in reps if r[1] <= mk <= r[2]]
            if not eligible:
                continue
            weights = [rep_weight(pod, r[0], idx) for r in eligible]
            n = pod_volume(pod, idx)
            for _ in range(n):
                seq += 1
                rep = random.choices(eligible, weights=weights)[0][0]
                day = random.randint(1, 28)
                created = dt.date(y, m, day)
                close = created + dt.timedelta(days=random.randint(25, 200))

                isr = ""
                if random.random() < 0.72:
                    isr = random.choice(POD_ISRS[pod])

                territory = random.choice(POD_TERRITORY_VALUES[pod])
                if random.random() < 0.05:
                    territory = ""              # missing territory -> country fallback
                elif random.random() < 0.03:
                    territory = "EMEA - Other"  # will not resolve; lands in unassigned

                amount = random.choice([
                    f"${random.randint(8, 240) * 1000:,}",
                    f"{random.randint(8, 240) * 1000}",
                    f"EUR {random.randint(8, 240) * 1000:,}.00",
                ])
                if random.random() < 0.08:
                    amount = ""

                stage = pick_stage(idx)
                if random.random() < 0.02:
                    stage = ""

                rows.append([
                    f"DR-{seq}",
                    fmt_date(created, idx),
                    rep,
                    isr,
                    territory,
                    f"Account {seq % 400:03d} {random.choice(['Group','Holdings','GmbH','Ltd','SpA','AB','SAS'])}",
                    random.choice(POD_COUNTRIES[pod]),
                    random.choice(PARTNERS),
                    pick_deal_type(idx, pod),
                    stage,
                    amount,
                    close.strftime("%Y-%m-%d"),
                ])

    # Queue / integration-user owned rows that the config should filter out.
    for i in range(40):
        seq += 1
        y, m = MONTHS[random.randrange(N_MONTHS)]
        rows.append([
            f"DR-{seq}", dt.date(y, m, random.randint(1, 28)).strftime("%Y-%m-%d"),
            random.choice(["Deal Reg Queue", "Integration User", "Channel Queue"]),
            "", random.choice(["DACH", "UKI & Nordics", "Southern Europe"]),
            f"Queued Account {i:03d}", "Germany", random.choice(PARTNERS),
            "New Business", "1 - Submitted", "", "",
        ])

    # A few rows outside the 24-month window, to prove windowing works.
    for i in range(25):
        seq += 1
        rows.append([
            f"DR-{seq}", dt.date(2024, random.randint(1, 7), random.randint(1, 28)).strftime("%Y-%m-%d"),
            "Lukas Brandt", "Tobias Frank", "DACH", f"Old Account {i:03d}",
            "Germany", random.choice(PARTNERS), "New Business", "7 - Closed Won",
            "120000", "2024-09-30",
        ])

    # An unparseable date, which the loader should count as skipped.
    seq += 1
    rows.append([f"DR-{seq}", "", "Marco Ferrari", "", "Southern Europe",
                 "No Date Account", "Italy", "Arrow ECS", "New Business",
                 "1 - Submitted", "50000", ""])

    random.shuffle(rows)

    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        w.writerows(rows)
        # Salesforce report footer, which the loader should ignore.
        w.writerow([""] * len(HEADERS))
        w.writerow(["Confidential Information - Do Not Distribute"] + [""] * (len(HEADERS) - 1))

    print(f"Wrote {out} ({len(rows)} data rows)")


if __name__ == "__main__":
    main()
