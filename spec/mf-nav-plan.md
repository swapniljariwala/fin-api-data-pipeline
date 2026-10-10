# Mutual Fund NAV History: data model + implementation plan

## Context

`jugaad-data`'s `amfi` module downloads the daily NAV history report from AMFI
(Association of Mutual Funds in India):

- Source: `https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx`
- API: `AMFI().nav_history_raw(from_date, to_date, mf="")` — a **range report**,
  cached on disk keyed by `(mf, from_date, to_date)` (override with
  `$J_CACHE_DIR`). Not a dated archive file, so the per-app `BhavcopyFile`
  provenance pattern does not apply (same situation as the live
  `corporateannouncements` feed).
- Report shape: a semicolon-separated file, one row per scheme per date, with
  the scheme type + category and AMC carried in section headers, flattened by
  the library into columns on every row.

Observed live sample (2026-09-29 → 2026-10-01):

- ~8,750 rows per trading day; 8,762 distinct scheme codes over 3 days.
- 55 AMCs, 96 categories, all three scheme types present.

Volume over the requested 10-year retention: ~2,500 trading days × ~8,750
rows/day ⇒ **~15–25M rows** (older years had fewer schemes), adding roughly
1.5–3 GB to the production SQLite DB. Accepted.

Example raw row:

```python
{
  "scheme_code": "139619",
  "scheme_name": "Taurus Investor Education Pool - Unclaimed Dividend - Growth",
  "plan": "", "option": "",
  "isin_growth": "", "isin_reinvest": "",
  "nav": "10.0000",
  "date": "29-Sep-2026",
  "scheme_type": "Open Ended",
  "category": "Money Market",
  "amc": "Taurus Mutual Fund",
}
```

## Key decisions

1. **Identity is AMFI `scheme_code`** (numeric). It is unique per *plan/option*
   variant and stable, while the human-readable attributes (`scheme_name`,
   `plan`, `option`, ISINs, `amc`, `category`, `scheme_type`) can drift over
   time (renames, AMC mergers, SEBI recategorization, ISIN add/change). Model a
   `MutualFundScheme` dimension keyed on `scheme_code`, updated **latest-wins**
   at ingest.
2. **No provenance table.** Unlike bhavcopy ingests there is no per-day file to
   track, and rolling/cron windows overlap, so a range-keyed provenance table
   would be counterproductive. Idempotency comes from the row-level unique key
   `(scheme, nav_date)` via `bulk_create(ignore_conflicts=True)` — the same
   live-endpoint precedent as `corporateannouncements`.
3. **AMC and category are lookup tables** (like `Series` / `AnnouncementCategory`),
   not `choices` or bare indexed strings: AMFI controls both vocabularies and
   adds values over time, so new values are picked up via `get_or_create`
   rather than needing a redeploy.
4. **No `raw` JSON.** Every published column is modeled; there is no
   undocumented field set worth preserving (unlike the announcements feed).
5. **No per-row `ingested_at`.** NAV rows are immutable; at tens of millions of
   rows a per-row timestamp buys nothing that the run logs don't already give.
6. **All three scheme types are stored.** Open-Ended publishes daily;
   Close-Ended and Interval Fund appear only on the dates AMFI declares a NAV
   (roughly monthly), so they simply have sparser rows. No filter is applied.
7. **No link to `securityinfo.Instrument`.** Mutual fund units are a distinct
   domain; `securityinfo` models instrument *identity* (ISIN/series/symbol) for
   the STK universe. Same separation `FnoContract`/`Index`/announcements keep.
   Linking MF schemes to the global instrument master via ISIN is deferred.

## New app: `mfnavhistory`

Follows the shape of `fnopricehistory`/`indexpricehistory`: lookup/dimension
tables + one fact table, no provenance table.

### `MutualFundAmc` (db_table = `mutual_fund_amcs`)

| Field | Type |
|---|---|
| `name` | `CharField(max_length=255, unique=True)` — e.g. `HDFC Mutual Fund` |

- `ordering = ["name"]`
- Resolved via `get_or_create(name=...)` at ingest time, with a per-run cache.

### `MutualFundCategory` (db_table = `mutual_fund_categories`)

| Field | Type |
|---|---|
| `name` | `CharField(max_length=255, unique=True)` — raw category text from the section header, e.g. `Equity Scheme - Multi Cap Fund` |

- `ordering = ["name"]`
- The category text is inconsistent in the source (`Equity Scheme` vs
  `Equity Schemes`, trailing `**`), kept as-is; `scheme_type` lives on the
  scheme row, not here, so a name collision across scheme types merges into one
  lookup row safely.

### `MutualFundScheme` (db_table = `mutual_fund_schemes`)

| Field | Type | Notes |
|---|---|---|
| `scheme_code` | `BigIntegerField(unique=True)` | AMFI identity |
| `scheme_name` | `CharField(max_length=255)` | full scheme name |
| `plan` | `CharField(max_length=50, blank=True)` | `Direct Plan` / `Regular Plan` (often blank in source) |
| `option` | `CharField(max_length=50, blank=True)` | `Growth` / `IDCW`, etc. |
| `isin_growth` | `CharField(max_length=12, null=True, blank=True)` | ISIN (Div Payout / Growth) |
| `isin_reinvest` | `CharField(max_length=12, null=True, blank=True)` | ISIN (Div Reinvestment) |
| `scheme_type` | `CharField(max_length=20)` | `Open Ended` / `Close Ended` / `Interval Fund` |
| `amc` | `FK(MutualFundAmc, on_delete=PROTECT, related_name="schemes")` | |
| `category` | `FK(MutualFundCategory, on_delete=PROTECT, related_name="schemes")` | |
| `created_at` | `DateTimeField(auto_now_add=True)` | |
| `updated_at` | `DateTimeField(auto_now=True)` | |

Constraints/indexes (named, following repo convention):
- `UniqueConstraint(fields=["scheme_code"], name="uniq_mf_scheme_code")`
- `Index(fields=["amc"], name="idx_mf_scheme_amc")`
- `Index(fields=["category"], name="idx_mf_scheme_category")`
- `Index(fields=["isin_growth"], name="idx_mf_scheme_isin_growth")`
- `Index(fields=["isin_reinvest"], name="idx_mf_scheme_isin_reinvest")`
- `ordering = ["scheme_code"]`

`isin_growth`/`isin_reinvest` are **not** unique (blank for many rows; the same
ISIN can transiently appear on more than one variant).

### `MutualFundNavHistory` (db_table = `mutual_fund_nav_history`)

| Field | Type | Notes |
|---|---|---|
| `scheme` | `FK(MutualFundScheme, on_delete=CASCADE, related_name="nav_history")` | |
| `nav_date` | `DateField` | parsed from `date` (`%d-%b-%Y`) |
| `nav` | `DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)` | `-`/blank → NULL |

Constraints/indexes:
- `UniqueConstraint(fields=["scheme", "nav_date"], name="uniq_nav_scheme_date")` — primary upsert key
- `Index(fields=["nav_date"], name="idx_nav_date")` — date-range queries
- `Index(fields=["scheme"], name="idx_nav_scheme")` — per-scheme history
- `ordering = ["-nav_date"]`

This supports the query patterns:
- NAV history for a scheme → `filter(scheme__scheme_code=...)` over `uniq_nav_scheme_date` / `idx_nav_scheme`
- latest NAV per scheme → `nav_date` index
- all schemes of an AMC / category → `idx_mf_scheme_amc` / `idx_mf_scheme_category`
- by ISIN → `idx_mf_scheme_isin_growth` / `idx_mf_scheme_isin_reinvest`

## Ingest command: `ingest_mf_nav`

Mirrors `ingest_corporate_announcements` (live range endpoint), **not** the
file-based bhavcopy commands.

Mode selection (exactly one):
- `--latest` (default): fetch `[today - lookback + 1, today]` in one call and
  ingest **every date present** in the response. `--lookback` default 5 so
  weekends/holidays self-heal; re-runs are no-ops via the unique key.
- `--from YYYY-MM-DD [--to YYYY-MM-DD]`: inclusive range, partitioned into
  chunks and processed **oldest-first** (so latest attribute wins on the
  dimension ends up correct). `--to` defaults to today.
- `--days N`: last N calendar days ending today.

Options:
| Option | Meaning |
|---|---|
| `--amc CODE` | passed through as `mf=CODE` to restrict the download to one AMC (much smaller/faster) |
| `--chunk-days N` | chunk long ranges to bound memory; default 30 (monthly), per the guide's "prefer narrow ranges" advice |
| `--delay SECONDS` | throttle between chunks (default 1.0; 0 disables) |
| `--retries N` | retry a failed chunk up to N times total, backing off by `--delay` (default 3) |

There is no `--data-dir`/`--no-download`: the endpoint is live and the raw
report is cached by `jugaad-data` on disk under `$J_CACHE_DIR`.

Per-chunk processing:
1. `AMFI().nav_history_raw(chunk_from, chunk_to, mf=amc or "")`.
2. For each row: parse `scheme_code` (int), `nav` (`Decimal` or `None`), and
   `nav_date` (`datetime.strptime(date, "%d-%b-%Y").date()`); `get_or_create`
   AMC/category/scheme through **per-run caches shared across all chunks**;
   update the scheme's mutable attributes when changed (latest-wins).
3. `bulk_create(..., ignore_conflicts=True, batch_size=1000)` inside
   `transaction.atomic()`.
4. Count inserted rows as `count()` before/after the `bulk_create`, **not** from
   its return value (SQLite gives Django no per-row conflict feedback — the bug
   found in the announcements verification pass).

Robustness:
- A row missing/unparseable `scheme_code`, `date`, or `nav` is logged and
  skipped, never failing the chunk.
- A failed chunk is retried up to `--retries`; if it still fails the run
  continues with the remaining chunks and raises `CommandError` at the end
  (non-zero exit), so a re-run retries the gap.
- Progress (INFO) and failures (ERROR with traceback) go to console and
  `logs/pipeline.log` via a dedicated `mfnavhistory` logger entry.

## Sprints

Split for incremental review — each sprint is a reviewable, independently
mergeable unit, in order.

### Sprint 1 — Models only

- New `mfnavhistory` app (`python manage.py startapp mfnavhistory`), registered
  in `INSTALLED_APPS`.
- `MutualFundAmc` + `MutualFundCategory` + `MutualFundScheme` +
  `MutualFundNavHistory` as specified above.
- `admin.py`: register all four (mirrors `indexpricehistory/admin.py`).
- `apps.py`: `MfnavhistoryConfig`.
- Add the `mfnavhistory` logger to `pipeline/settings.py` `LOGGING`.
- `makemigrations mfnavhistory && migrate`.
- **No ingest command yet.** Reviewable purely as a schema addition — verify
  the migration applies cleanly and the admin page loads.

### Sprint 2 — Basic ingest command

- `ingest_mf_nav` with `--latest` / `--from`/`--to` / `--days`, chunking,
  parsing, per-run caches, latest-wins attribute update, and
  `bulk_create(ignore_conflicts=True)` inside `transaction.atomic()`.
- `--amc`, `--chunk-days`, `--delay`, `--retries` optional filters/controls.
- **Verification:** run `ingest_mf_nav --days 5` against the live API and
  confirm rows land; re-run and confirm a no-op (`0 inserted, N duplicates`).

### Sprint 3 — Tests

- `tests.py` with inline synthetic semicolon-report fixtures (no network, no
  checked-in real files, following `fnopricehistory/tests.py`):
  parse correctness (scheme/AMC/category resolution, date + NAV parsing,
  blank/`-` handling), rerun no-op via the unique key, attribute latest-wins,
  chunk partitioning, and `--amc` pass-through.

### Sprint 4 — Docs

- New `mfnavhistory/AGENTS.md`, mirroring `corporateannouncements/AGENTS.md`'s
  `## Files` table.
- `spec/schema.md`: new `§3.10`/`§3.11` sections documenting the four tables
  (DDL + design notes), plus a "Mutual fund NAV" row in §8 Future extensions.
- `spec/AGENTS.md`: add this plan to the file table.
- Root `AGENTS.md`: new row in "Folder index"; new `###` subsection in "CLI
  reference" for `ingest_mf_nav` (options table + guarantees + examples);
  note the new production schedule in "Deployment & operations".
- `README.md`: command in the CLI list.
- `docs/DEPLOYMENT.md`: cron entry + 10-year backfill procedure + the
  `mfnavhistory` app logger in the loggers list.

### Sprint 5 — Verification + 10-year backfill

- Live spot-check: `ingest_mf_nav --days 5`, then `--amc <code>` on the same
  range; confirm row counts and that `logs/pipeline.log` captured
  started/fetched/ingested/finished phases.
- Backfill the last 10 years: `ingest_mf_nav --from <today-10y> --to <today>`
  (internally chunked monthly), run detached with
  `nohup ... > logs/backfill_mf_nav.log 2>&1` like the announcements backfill.
- Sanity: `MAX(nav_date)`, total row count, distinct scheme count, and a
  targeted per-scheme history query.

## Production schedule (to add in Sprint 4)

The report for a session is published by AMFI the same evening. Add one daily
cron entry in the 04:00–08:00 IST window (after publication), `flock`-wrapped
with output captured, alongside the bhavcopy jobs:

```
30 6 * * * flock -n /tmp/ingest_mf_nav.lock bash -c 'cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_mf_nav --latest --lookback 5 >> logs/cron.log 2>&1'
```

## Assumptions

- Command name is `ingest_mf_nav`; app is `mfnavhistory`.
- All-scheme (Open/Close/Interval) ingestion; sparse rows for the non-daily
  types are expected and fine.
- Full 10-year retention is intended; the DB growth (1.5–3 GB) is accepted.
- Application name matches the request; no `securityinfo` coupling in v1.
