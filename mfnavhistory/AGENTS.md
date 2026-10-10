# AGENTS.md — `mfnavhistory/`

Django app for AMFI mutual fund NAV history. Schema is designed in
[`spec/schema.md`](../spec/schema.md); the models in `models.py` implement
§3.10 (`mutual_fund_amcs`, `mutual_fund_categories`, `mutual_fund_schemes`) and
§3.11 (`mutual_fund_nav_history`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `MutualFundAmc`, `MutualFundCategory`, `MutualFundScheme`, `MutualFundNavHistory` per `spec/schema.md` §3.10–3.11. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | All four models registered. |
| `apps.py` | ready | `MfnavhistoryConfig` (app name `mfnavhistory`). |
| `tests.py` | ready | Tests for the ingest command with synthetic report fixtures (network-free; `AMFI.nav_history_raw` is patched). Covers parsing/resolution, blank/`-`/`N.A.` NAV → NULL, unparseable-row skipping, rerun no-op, attribute latest-wins, chunk partitioning, and `--amc` pass-through. |
| `management/commands/ingest_mf_nav.py` | ready | Fetches AMFI's live NAV history range report (via `jugaad-data`'s `AMFI.nav_history_raw()`) and ingests it. Modes: `--latest` (default, last `--lookback` days in one call), `--from/--to` (oldest-first, chunked by `--chunk-days`), `--days N`. Optional `--amc CODE`, `--delay SECONDS`, `--retries N`. Idempotent via `bulk_create(ignore_conflicts=True)` keyed on `(scheme, nav_date)`, one transaction per chunk. Scheme dimension attributes updated latest-wins. Rows missing/unparseable `scheme_code` or `date`, or with a genuinely malformed `nav`, are logged and skipped; blank/`-`/`N.A.` NAV is stored as NULL. Failed chunks are retried, then the run continues and raises `CommandError` at the end. Progress/errors logged to console + `logs/pipeline.log` (INFO). |
| `migrations/` | ready | `0001_initial` (all four tables). |
| `__init__.py` | ready | Marks the folder as a Python package. |

## Production schedule

On `finapi` this command is run by cron **once daily** in the 04:00–08:00 IST
window (the session's report is published by AMFI the same evening), `flock`-wrapped
with output captured to `logs/cron.log`, using `--latest --lookback 5`. A one-off
10-year backfill is run with `--from <today-10y> --to <today>`. See
[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#mutual-fund-nav).
