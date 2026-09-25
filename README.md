# EMEA deal-registration allocation trending

Month-by-month analysis of how deal registrations are allocated across sales
reps and ISRs in the three EMEA pods — **UKI & Nordics**, **Southern Europe**,
**DACH** — over a 24-month window, broken out by opportunity stage and deal
type.

The question this answers is not "how many deal regs did we get" but **"how
evenly were they spread across the reps in a pod, and did that change over 24
months"** — so the output leads with concentration measures, not volume.

> **No CRM data is included in this repository.** It contains the analysis
> pipeline. Export the deal registrations from Salesforce (spec below), run one
> command, and it produces the tables, the write-up and the dashboard.

---

## Quick start

```bash
# 1. Check that your input's columns were detected correctly.
python3 run.py --input path/to/data.csv --describe

# 2. Run the analysis.
python3 run.py --input path/to/data.csv

# Outputs land in ./out/ :
#   findings.md     <- read this first: what changed in each pod, in words
#   dashboard.html  <- interactive; open in a browser
#   *.csv           <- tidy tables for Excel / BI
```

Input can be a **CSV/XLSX export** or **JSON query results** from a warehouse —
see [Data sources](#data-sources). Several inputs may be passed at once and are
merged, so a large pull can be fetched in chunks:

```bash
python3 run.py --input q1.json q2.json q3.json q4.json
```

Requires Python 3.10+ and PyYAML (`pip install pyyaml`). `openpyxl` is needed
only if you feed it `.xlsx` instead of `.csv`.

To see the output shape before you have real data:

```bash
python3 samples/make_sample.py           # writes a synthetic export
python3 run.py --input samples/sample_deal_registrations.csv
```

**The sample numbers are fabricated.** They exist to exercise the pipeline and
demonstrate the report. Do not read any business conclusion from them.

To verify the pipeline itself:

```bash
python3 tests/test_core.py    # 128 checks, no test framework needed
```

---

## Data sources

Two ways in. Both are normalized by the same code and produce the same
outputs — that equivalence is asserted in the test suite, not assumed.

### A. Warehouse query (no file handling)

`sql/deal_reg_allocation.sql` is a template for the pull. Save the results as
JSON and run against them directly:

```bash
python3 run.py --input result.json
```

Accepted JSON shapes: a list of row objects, `{"rows": [...]}` / `{"data": ...}`
/ `{"records": ...}`, `{"columns": [...], "rows": [[...]]}`, or newline-delimited
objects.

**The query is aggregated on purpose.** It groups in SQL and returns a
`DEAL_REGS` count per group; the pipeline multiplies by it. The reason is that
query results usually have to travel through a conversation or an API response,
where a raw pull of every registration is slow and may not fit. Aggregating
caps the row count at months × reps × deal types × stages, so ten times the
deal volume still returns roughly the same number of rows.

Any column named `count`, `deal_regs`, `n`, `cnt` (and similar) is picked up
automatically. Rows with a count of zero or less are skipped and reported
rather than silently distorting every share.

If one response can't hold the result, add a date predicate, save each slice,
and pass them all — chunks are merged and de-duplicated by registration id.

> **Grain matters.** Group by month, owner, ISR, territory, country, deal type
> and stage — and *nothing else*. Adding account, partner or registration id
> makes every row unique and defeats the aggregation entirely.

### B. CSV / XLSX export

The column spec is below. Headers do not need renaming.

---

## The Salesforce export you need

Build a report on **Deal Registrations** (or on Opportunities filtered to the
deal-reg record type, if that is how your org models them), covering the last
**25 months** — one month more than the window, so the earliest month is
complete. No date grouping, no summaries: **one row per deal registration**.

| Column | Needed | Notes |
|---|---|---|
| Deal Registration Name / ID | recommended | For traceability; a row number is used if absent. |
| **Created Date** (or Date Submitted) | **required** | Drives the month bucket. See "which date" below. |
| **Opportunity Owner** / Assigned AE | **required** | The rep the registration landed on. |
| Inside Sales Rep / ISR | recommended | Enables the whole ISR view. |
| Sales Territory / Pod / Team | recommended | Primary pod signal. |
| Stage (or Approval Status) | recommended | Needed for the stage breakdown. |
| Opportunity Type / Deal Type | recommended | Needed for new business / add-on / renewal. |
| Billing Country | optional | Fallback pod signal when territory is blank. |
| Amount (converted) | optional | Adds value alongside counts. |
| Partner Account | optional | Carried through for reference. |
| Account Name | optional | Carried through for reference. |
| Close Date | optional | Read but not used for bucketing. |
| Count / Deal Regs | optional | Only for pre-aggregated input — how many registrations the row stands for. |

Export as CSV. Headers do not need renaming — the loader matches them against
a large alias list, so `Deal Registration: Created Date`, `Date Submitted` and
`Created Date` all resolve. Run `--describe` to confirm, and pin anything it
gets wrong in `config/columns.yml`.

### Which date defines "allocation"

**Created / submitted date**, not close date. Allocation happens when a
registration lands on a rep; close date is an outcome months later and would
smear each rep's monthly allocation across quarters. Set
`month_basis: created_date` in `config/columns.yml` (the default).

---

## Configuration

### `config/pods.yml` — who is in which pod

Resolution order per registration, first match wins:

1. `rep_overrides` (rep name → pod) — **fill this in and it beats everything else**
2. the mapped territory/team column
3. `pod_aliases` applied to that value
4. `country_to_pod` applied to the country column
5. otherwise `(unassigned)`, reported separately and never distributed

Two settings are worth checking before you trust the first run:

- **France** defaults into Southern Europe. If your org runs France separately,
  move it to `country_to_pod_excluded`.
- **`excluded_owner_patterns`** drops queues and integration users. Leaving a
  deal-reg queue in the data makes concentration look far worse than it is,
  because the queue absorbs a large share of registrations.

The most reliable setup is to paste the EMEA org chart into `rep_overrides`.
Territory picklists drift over 24 months and reps change pods; an explicit
rep→pod map does not.

### `config/columns.yml` — column mapping and date format

Only needed when auto-detection picks the wrong column, or when your export
uses ambiguous D/M/Y dates. The loader infers day-first vs month-first from
the column, and tells you in `findings.md` when it could not — an unresolved
ambiguity shifts registrations between months, so check that note.

---

## What the analysis produces

### `findings.md`

Per pod: volume, headcount, the concentration measures, the biggest share
gainers and losers, reps with partial windows called out separately, and the
deal-type and stage-outcome mix for the first 12 months vs the last 12. Then a
data-quality section.

### `dashboard.html`

Self-contained, no network calls. Filters for rep-vs-ISR basis, pod, and which
metric the overview charts. Per pod: a KPI row, a rep × month allocation-share
heatmap, a first-12-vs-last-12 dumbbell, and deal-type and stage-outcome mix
columns. Every chart has a table view, and the whole page works in light and
dark mode.

### CSVs

| File | Grain |
|---|---|
| `monthly_regs_by_rep.csv` | pod × rep × month — counts, pod total, share |
| `monthly_regs_by_rep_pivot.csv` | one row per rep, one column per month |
| `monthly_regs_by_rep_dealtype.csv` | pod × rep × month × deal type |
| `monthly_regs_by_rep_outcome.csv` | pod × rep × month × stage outcome |
| `regs_by_rep_stage.csv` | pod × rep × detailed CRM stage |
| `pod_monthly_concentration.csv` | pod × month — every concentration metric |
| `rep_halfyear_trend.csv` | per rep — first 12 vs last 12, with tenure flags |
| `pod_summary.csv` | one row per pod — the headline comparison |
| `pod_monthly_dealtype.csv` | pod × month × deal type |
| `pod_monthly_outcome.csv` | pod × month × stage outcome |
| `monthly_regs_by_isr.csv` | the ISR-basis equivalent |
| `data_quality.json` | detected columns, skipped rows, unresolved values |

---

## How to read the numbers

Three numbers have to be read **together**. Volume alone cannot tell you
whether allocation changed.

1. **Total regs** — how much came in.
2. **Active reps** — how many people it was split between.
3. **Concentration** — how evenly it was split.

A pod can hold volume flat, lose two reps, and hand every remaining rep more
work without any allocation *policy* changing. Concentration rising while
headcount is stable is a real allocation shift. The per-pod sections separate
those cases.

| Metric | Reads as |
|---|---|
| **Effective reps** | "regs were effectively spread across N reps". `1 / Σ(share²)`. The headline: comparable month to month even as headcount changes. |
| **Gini** | 0 = every rep got the same number; higher = more uneven. Catches a long tail of reps getting scraps. |
| **HHI** | 0–10000 concentration index, for readers who already know it. |
| **Top-1 / top-3 share** | Blunt but immediately legible. |
| **Active reps** | The denominator behind all of the above. |

### Four things that will otherwise mislead you

- **Rep tenure.** A rep who joined in month 18 always looks like they "gained
  share"; one who left in month 6 always looks like they lost it. Neither is an
  allocation decision. Every rep row carries `first_month`, `last_month`,
  `months_active` and a `tenure_flag`, the narrative compares only reps present
  in both halves, and partial-window reps are italicised in the dashboard.
- **Stage is point-in-time.** Stage reflects each registration's status *as of
  the export*, not when it was created. Recent months skew toward Open because
  those deals have had less time to resolve — that is deal age, not
  deteriorating quality. What *is* meaningful is a persistent
  Rejected/Duplicate band.
- **Small pods are noisy.** With 3–5 reps these metrics swing on single deals,
  and `effective_reps` can never exceed `active_reps`. Compare a pod against
  itself over time, not against another pod.
- **Deal type changes what a reg is worth.** A rep holding 20 renewals has not
  been allocated the same opportunity as one holding 20 new-logo regs. That is
  why the mix is broken out per rep rather than only per pod.

---

## Layout

```
run.py                        CLI entry point
tests/test_core.py            test suite: python3 tests/test_core.py
config/pods.yml               pod membership and exclusions  <- edit this
config/columns.yml            column mapping overrides
dealreg/loader.py             CSV/XLSX reading, column aliasing, date parsing
dealreg/taxonomy.py           pod / stage / deal-type normalization
dealreg/metrics.py            concentration metrics
dealreg/analyze.py            the pipeline; windowing and trend computation
dealreg/export.py             CSV outputs and the findings write-up
dealreg/report.py             dashboard data payload
dealreg/templates/            dashboard HTML/CSS/JS
sql/deal_reg_allocation.sql   warehouse query template (placeholders to fill)
samples/make_sample.py        synthetic export generator (fabricated data)
```

### Options

```
--input, -i      one or more inputs: .csv, .xlsx, .json, .jsonl   [required]
                 several are merged and de-duplicated by reg id
--outdir, -o     output directory                                [./out]
--months         window length in months                         [24]
--end-month      last month of the window, YYYY-MM   [last complete month]
--describe       print detected columns and sample values, then exit
--pods-config    path to pods.yml
--columns-config path to columns.yml
```

By default the window ends at the **last complete calendar month**, so a
part-month never shows up as a collapse in volume at the right edge of every
chart. If the data ends earlier, the data wins.
