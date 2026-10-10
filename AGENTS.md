# AGENTS.md

Index of the repository. Each folder has its own `AGENTS.md` explaining the files
inside it; read the relevant one before working in that folder.

## Hard constraints (from CLAUDE.md)

- Never run Python/pip outside virtual environment `env/`.
- Never `pip install <pkg>` directly, and never `pip freeze > requirements.txt`;
  edit `requirements.txt` manually, install from it.

## Project overview

Downloads NSE/BSE datasets (via `jugaad-data`) into SQLite, managed with Django.

Current state:

- Django project `pipeline/` with five apps: `securityinfo` (instrument
  master: series, instruments, tickers), `dailypricehistory` (CM bhavcopy
  provenance + daily OHLC), `fnopricehistory` (F&O contract identity +
  daily OHLC/OI), `indexpricehistory` (index daily OHLC), and
  `corporateannouncements` (NSE corporate announcements feed) — plus the
  `mfnavhistory` app (AMFI mutual fund NAV history).
- Working `ingest_bhavcopy` management command: downloads NSE CM bhavcopies and
  ingests them into `cm_price_history`. Idempotent and restart-safe; see
  [CLI reference](#cli-reference) below.
- Working `ingest_fno_bhavcopy` management command: same pattern for NSE F&O
  bhavcopies, into `fno_price_history` / `fno_contracts`.
- Working `ingest_index_bhavcopy` management command: downloads NIFTY index
  bhavcopies and ingests them into `index_price_history` / `indices`.
- Working `ingest_corporate_announcements` management command (live NSE feed)
  and `ingest_mf_nav` management command (live AMFI range report).
- Project-level logging: console + `logs/pipeline.log` (rotated daily, 7 days kept).
- Object-store upload is a stub (`dailypricehistory/object_store.py`); to be
  implemented later.

## Deployment & operations

The pipeline runs on the `finapi` host as scheduled management commands writing
to a shared SQLite DB that a separate Go API service reads. The full deployment
guide — server layout, virtualenv, DB, cron schedule, deploy/backfill
procedures, local DB sync for testing, logs, and known issues — is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Read it before touching production.

Cron schedule (prod): the three bhavcopy ingests every 15 min in the 04:00–08:00
IST window, `ingest_corporate_announcements --latest --lookback 1` (a live
feed) **every minute**, and `ingest_mf_nav --latest --lookback 5` **once daily**
at 06:30 IST, all wrapped in `flock` with captured output.

Two production gotchas are called out there:

- The F&O cron once ran `ingest_fo_bhavcopy` (typo); the real command is
  `ingest_fno_bhavcopy`. It silently failed for weeks before being fixed on
  2026-10-10 and backfilled. There is no MTA on the host, so capture cron output.
- Production carries an untracked `dailypricehistory` migration `0005` not in
  the repo; reconcile with `makemigrations --check` before deploying.

## Folder index

| Path | What it is | Details |
|---|---|---|
| [`pipeline/`](pipeline/AGENTS.md) | Django project (settings, URLconf, ASGI/WSGI) | [AGENTS.md](pipeline/AGENTS.md) |
| [`dailypricehistory/`](dailypricehistory/AGENTS.md) | Django app: daily CM price/OHLC history | [AGENTS.md](dailypricehistory/AGENTS.md) |
| [`fnopricehistory/`](fnopricehistory/AGENTS.md) | Django app: F&O contract identity + daily OHLC/OI | [AGENTS.md](fnopricehistory/AGENTS.md) |
| [`indexpricehistory/`](indexpricehistory/AGENTS.md) | Django app: index daily OHLC history | [AGENTS.md](indexpricehistory/AGENTS.md) |
| [`corporateannouncements/`](corporateannouncements/AGENTS.md) | Django app: NSE corporate announcements feed | [AGENTS.md](corporateannouncements/AGENTS.md) |
| [`mfnavhistory/`](mfnavhistory/AGENTS.md) | Django app: AMFI mutual fund NAV history | [AGENTS.md](mfnavhistory/AGENTS.md) |
| [`securityinfo/`](securityinfo/AGENTS.md) | Django app: security/instrument master | [AGENTS.md](securityinfo/AGENTS.md) |
| [`spec/`](spec/AGENTS.md) | Design docs: DB schema + bhavcopy formats | [AGENTS.md](spec/AGENTS.md) |
| [`docs/`](docs/AGENTS.md) | Deployment & operation docs | [AGENTS.md](docs/AGENTS.md) |

## CLI reference

Run from the repo root with the venv activated (`env\Scripts\python.exe` on
Windows, or just `python` after `env\Scripts\activate`):

```
python manage.py ingest_bhavcopy [--latest | --from YYYY-MM-DD | --days N]
                                 [--to YYYY-MM-DD] [--data-dir PATH]
                                 [--no-download] [--lookback N] [--upload]
```

| Option | Meaning |
|---|---|
| `--latest` | Most recent bhavcopy not yet ingested. Default mode. Walks back from today up to `--lookback` days. |
| `--from YYYY-MM-DD [--to YYYY-MM-DD]` | Inclusive date range, processed newest-first. `--to` defaults to today. |
| `--days N` | Last `N` calendar days (ending today), processed newest-first. |
| `--data-dir PATH` | Download directory. Default: `<repo>/data`. |
| `--no-download` | Ingest only files already present on disk; no network. Use to retry after a partial failure. |
| `--lookback N` | How far back `--latest` searches. Default 30. |
| `--upload` | Call the object-store upload hook after each successful ingest (stub; logs a warning until implemented). |

Data repair (safe; dry run unless `--apply`):

```
python manage.py purge_duplicate_bhavcopies [--apply]
```

Flags bhavcopy files whose rows duplicate the previous trading day (the
signature of NSE serving the previous day's bhavcopy for a holiday date
under older code) and, with `--apply`, deletes them and their price rows.
Keeps real data such as Diwali muhurat sessions.

Examples:

```bash
python manage.py ingest_bhavcopy                            # latest
python manage.py ingest_bhavcopy --days 5                   # last 5 calendar days
python manage.py ingest_bhavcopy --from 2024-07-01 --to 2024-07-31
python manage.py ingest_bhavcopy --latest --upload          # ingest + future upload
```

Guarantees:

- Already-ingested files (present in `bhavcopy_files`) are skipped; re-running is
  a no-op.
- Each file ingests inside one transaction; the `bhavcopy_files` row is committed
  only together with its price rows, so an interrupted run leaves no partial
  state and can be restarted.
- Price rows dedupe on `(instrument_ticker, trade_date, series)` via
  `ignore_conflicts` on insert.
- Both bhavcopy formats supported (legacy pre-8-Jul-2024 and UDiFF); the format
  is sniffed from the file header, not assumed from the date.
- Known NSE holidays are skipped before any download (local all-year calendar
  from `jugaad-data` plus NSE's live holiday API for the current year), so
  holiday dates are never downloaded in the first place.
- The trade date is taken from the file content (`DATE1`/`TradDt`), not the
  filename: if NSE serves the previous day's file for a date with no bhavcopy,
  the rows land under their real date and dedupe against the actual day.
- Progress (INFO) and failures (ERROR with traceback) go to console and
  `logs/pipeline.log`; failures exit non-zero.

### F&O bhavcopy ingest

```
python manage.py ingest_fno_bhavcopy [--latest | --from YYYY-MM-DD | --days N]
                                     [--to YYYY-MM-DD] [--data-dir PATH]
                                     [--no-download] [--lookback N] [--upload]
```

Same options and guarantees as `ingest_bhavcopy` above (restart-safe,
holiday-aware, file-date-authoritative), applied to the F&O segment:
downloads via `bhavcopy_fo_save`, files named `fo{dd}{MMM}{yyyy}bhav.csv`,
rows land in `fno_price_history` keyed by `(contract, trade_date)` where
`contract` identifies an `FnoContract` by
`(underlying_symbol, instrument_type, expiry_date, strike_price, option_type)`.
Legacy `INSTRUMENT` codes (`FUTSTK`/`OPTSTK`/`FUTIDX`/`OPTIDX`) are normalized
to their UDiFF equivalents (`STF`/`STO`/`IDF`/`IDO`) so both formats share one
`instrument_type` column.

Retention (safe; dry run unless `--apply`):

```
python manage.py purge_expired_fno_contracts [--apply]
```

Deletes `FnoContract` rows (and their cascaded `FnoPriceHistory` rows) more
than 365 calendar days past `expiry_date`. Run on a schedule alongside
ingest, not inline in it.

### Index bhavcopy ingest

```
python manage.py ingest_index_bhavcopy [--latest | --from YYYY-MM-DD | --days N]
                                       [--to YYYY-MM-DD] [--data-dir PATH]
                                       [--no-download] [--lookback N]
```

Same options and guarantees as `ingest_bhavcopy` above (restart-safe,
holiday-aware, file-date-authoritative), applied to NIFTY indices:
downloads via NIFTY Indices website (https://www.niftyindices.com) using
`bhavcopy_index_save`, files named `ind_close_all_DDMMYYYY.csv`, rows land
in `index_price_history` keyed by `(index, trade_date)` where `index` is
identified by `(source, ticker)`, e.g., `('NSE', 'NIFTY 50')`.

Examples:

```bash
python manage.py ingest_index_bhavcopy                    # latest
python manage.py ingest_index_bhavcopy --days 5           # last 5 calendar days
python manage.py ingest_index_bhavcopy --from 2024-08-01 --to 2024-08-31
```

### Corporate announcements ingest

```
python manage.py ingest_corporate_announcements [--latest | --from YYYY-MM-DD | --days N]
                                                [--to YYYY-MM-DD] [--lookback N]
                                                [--symbol SYM] [--segment SEGMENT]
```

| Option | Meaning |
|---|---|
| `--latest` | Announcements from the last `--lookback` days. Default mode. |
| `--from YYYY-MM-DD [--to YYYY-MM-DD]` | Inclusive date range. `--to` defaults to today. |
| `--days N` | Last `N` calendar days (ending today). |
| `--lookback N` | How far back `--latest` looks. Default 3. |
| `--symbol SYM` | Restrict to announcements for one NSE symbol. |
| `--segment SEGMENT` | NSE segment to query. Default `equities`. |

Unlike the bhavcopy commands, this hits NSE's **live** `corporate_announcements` API
(via `jugaad-data`'s `NSELive`) rather than downloading a dated archive file, so there
is no `--data-dir`/`--no-download` and the whole date range is fetched in one API call.
Rows land in `corporate_announcements` keyed by `seq_id` (NSE's own sequential ID);
`category` is a FK into `announcement_categories`, resolved via `get_or_create` on the
raw `desc` string.

Examples:

```bash
python manage.py ingest_corporate_announcements                        # latest
python manage.py ingest_corporate_announcements --days 30
python manage.py ingest_corporate_announcements --symbol RELIANCE --days 30
python manage.py ingest_corporate_announcements --from 2024-07-01 --to 2024-07-31
```

Guarantees:

- Idempotent: rows are inserted via `bulk_create(ignore_conflicts=True)` keyed on
  `seq_id`'s unique constraint, inside one transaction per run; re-running the same
  range is a no-op.
- Instrument resolved via `sm_isin`, falling back to `symbol`, reusing the ISIN-first
  pattern from `ingest_bhavcopy.py`; the symbol-at-filing-time is recorded in
  `NSESymbolInstrumentMap` so a later symbol rename or ISIN change doesn't orphan
  historical announcements.
- A record missing/unparseable `seq_id` or `sort_date` is logged and skipped rather
  than failing the run.
- `sort_date`/`exchdisstime` are parsed and stored as timezone-aware IST
  (`Asia/Kolkata`) datetimes.
- Progress (INFO) and failures go to console and `logs/pipeline.log`; failures raise
  `CommandError` (exit 1).

### Mutual fund NAV ingest

```
python manage.py ingest_mf_nav [--latest | --from YYYY-MM-DD | --days N]
                              [--to YYYY-MM-DD] [--lookback N] [--amc CODE]
                              [--chunk-days N] [--delay SECONDS] [--retries N]
```

| Option | Meaning |
|---|---|
| `--latest` | Fetch `[today - lookback + 1, today]` in one call. Default mode. |
| `--from YYYY-MM-DD [--to YYYY-MM-DD]` | Inclusive range, chunked and processed **oldest-first**. `--to` defaults to today. |
| `--days N` | Last `N` calendar days (ending today), oldest-first. |
| `--lookback N` | How far back `--latest` looks. Default 5. |
| `--amc CODE` | Restrict the download to one AMFI AMC code (`mf=CODE`). |
| `--chunk-days N` | Chunk long ranges into windows of this many days. Default 30. |
| `--delay SECONDS` | Throttle between chunks (0 disables). Default 1.0. |
| `--retries N` | Total attempts per chunk, backing off by `--delay`. Default 3. |

Like the corporate-announcements command, this hits a **live** AMFI range endpoint
(`AMFI().nav_history_raw()`, cached by `jugaad-data` under `$J_CACHE_DIR`) rather than a
dated archive file, so there is no `--data-dir`/`--no-download`. Rows land in
`mutual_fund_nav_history` keyed by `(scheme, nav_date)`; `scheme` is a `MutualFundScheme`
identified by AMFI's `scheme_code`, with `amc`/`category` as growable lookup tables.
There is no provenance table (rolling windows overlap).

Examples:

```bash
python manage.py ingest_mf_nav                          # latest (last 5 days)
python manage.py ingest_mf_nav --days 30
python manage.py ingest_mf_nav --amc 128 --days 30
python manage.py ingest_mf_nav --from 2026-09-01 --to 2026-09-30
```

Guarantees:

- Idempotent: rows are inserted via `bulk_create(ignore_conflicts=True)` keyed on
  `(scheme, nav_date)`, one transaction per chunk; re-running an overlapping window is a
  no-op for rows already present.
- Scheme dimension attributes are updated **latest-wins** (ranges processed oldest-first).
- A row missing/unparseable `scheme_code`, `date`, or `nav` is logged and skipped rather
  than failing the chunk; blank/`-`/`N.A.` NAV is stored as NULL.
- A failed chunk is retried up to `--retries`; if it still fails the run continues with
  the remaining chunks and raises `CommandError` at the end (exit 1), so a re-run retries
  the gap.
- Progress (INFO) and failures go to console and `logs/pipeline.log`; failures raise
  `CommandError` (exit 1).

## Root-level files

| File | Purpose |
|---|---|
| `manage.py` | Django CLI entry point (`DJANGO_SETTINGS_MODULE=pipeline.settings`). |
| `requirements.txt` | Dependencies: `django`, `jugaad-data`, `python-dotenv`. Edit manually; install from it. |
| `temp_bhavcopy.py` | Throwaway script: downloads one legacy-format NSE bhavcopy into `data/`. |
| `README.md` | Project intro, setup and CLI reference. |
| `.gitignore` | Ignore rules (`data/`, `db/`, `logs/`, `env/`, ...). |
