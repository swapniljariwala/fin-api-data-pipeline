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
- New live-feed apps: `shareholdingpattern` (NSE shareholding-pattern filings,
  list + XBRL parsing to GCS) and `eventcalendar` (NSE board-meeting / event
  calendar).
- Working `ingest_bhavcopy` management command: downloads NSE CM bhavcopies and
  ingests them into `cm_price_history`. Idempotent and restart-safe; see
  [CLI reference](#cli-reference) below.
- Working `ingest_fno_bhavcopy` management command: same pattern for NSE F&O
  bhavcopies, into `fno_price_history` / `fno_contracts`.
- Working `ingest_index_bhavcopy` management command: downloads NIFTY index
  bhavcopies and ingests them into `index_price_history` / `indices`.
- Working `ingest_corporate_announcements` management command (live NSE feed)
  and `ingest_mf_nav` management command (live AMFI range report).
- Working `ingest_corporate_actions` management command (live NSE feed, by
  ex-date) into the `corporateactions` app: fixed-logic parsing of dividends,
  splits, bonus, rights etc., plus a price sanity check that sends anything
  doubtful to `needs_review`.
- Working `ingest_integrated_filings` management command (live NSE feed) into
  the `integratedfilings` app: records the list of NSE Integrated Filing
  releases (one row per release, revisions included). Working
  `extract_integrated_filings` command downloads each release's XBRL, splits it into
  statements with `jugaad-xbrl` and uploads them to a GCS bucket
  (`GCS_INTEGRATED_FILINGS_BUCKET`), indexed in `integrated_filing_statements`.
- Project-level logging: console + `logs/pipeline.log` (rotated daily, 7 days kept).
- Object-store upload is a stub (`dailypricehistory/object_store.py`); to be
  implemented later.

## Deployment & operations

The pipeline runs on the `finapi` host as scheduled management commands writing
to a shared SQLite DB that a separate Go API service reads. The full deployment
guide — server layout, virtualenv, DB, cron schedule, deploy/backfill
procedures, local DB sync for testing, logs, and known issues — is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Read it before touching production.

Cron schedule (prod): the four bhavcopy ingests (CM, index, F&O, delivery) every
15 min in the 04:00–08:59 IST window, spaced 3 min apart in that priority order
(:00/:03/:06/:09 of each quarter hour),
`ingest_corporate_announcements --latest --lookback 1` (a live feed) **every
minute**, `ingest_mf_nav --latest --lookback 5` **once daily** at 09:30 IST,
shareholding pattern list+extraction **once daily** at 21:30 IST, and the event
calendar **hourly at :17**, all wrapped in `flock` with captured output.
Litestream's daily snapshot runs 05:30–05:45 IST; avoid heavy DB work then.

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
| [`corporateactions/`](corporateactions/AGENTS.md) | Django app: NSE corporate actions (dividend/split/bonus/...) | [AGENTS.md](corporateactions/AGENTS.md) |
| [`integratedfilings/`](integratedfilings/AGENTS.md) | Django app: NSE Integrated Filing releases (list) + XBRL statement extraction to GCS | [AGENTS.md](integratedfilings/AGENTS.md) |
| [`shareholdingpattern/`](shareholdingpattern/AGENTS.md) | Django app: NSE shareholding-pattern filings (list) + XBRL parsing to GCS | [AGENTS.md](shareholdingpattern/AGENTS.md) |
| [`eventcalendar/`](eventcalendar/AGENTS.md) | Django app: NSE board-meeting / event calendar feed | [AGENTS.md](eventcalendar/AGENTS.md) |
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

### Delivery (full bhavcopy) ingest

```
python manage.py ingest_delivery_bhavcopy [same options as ingest_bhavcopy]
python manage.py purge_old_delivery [--apply]
```

The NSE full bhavcopy (`sec_bhavdata_full_*.csv`, via `full_bhavcopy_save`) equals
the regular bhavcopy on every price/volume column; its only extra data are
`DELIV_QTY` and `DELIV_PER`. `ingest_delivery_bhavcopy` stores just those in
`nse_cm_delivery`, whose primary key is the matching `nse_cm_price_history` id
(`JOIN nse_cm_delivery d ON d.price_id = p.id`). Same guarantees as
`ingest_bhavcopy` (restart-safe, holiday-aware, file-date-authoritative), plus:

- The regular bhavcopy for a date must be ingested first; the full file has no
  ISIN, so `(symbol, series)` is resolved onto that date's price rows. A date with
  no price rows fails and is retried by the next run; unmatched or ambiguous
  symbols are logged and skipped.
- `-` delivery values (BE/BZ series) are stored as NULL.
- Only the last 365 days are kept: older dates are not fetched, and
  `purge_old_delivery --apply` (dry run by default) deletes older rows. Price rows
  are never touched; deleting a price row cascades to its delivery row.

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
                                       [--refresh-valuations]
```

Same options and guarantees as `ingest_bhavcopy` above (restart-safe,
holiday-aware, file-date-authoritative), applied to NIFTY indices:
downloads via NIFTY Indices website (https://www.niftyindices.com) using
`bhavcopy_index_save`, files named `ind_close_all_DDMMYYYY.csv`, rows land
in `index_price_history` keyed by `(index, trade_date)` where `index` is
identified by `(source, ticker)`, e.g., `('NSE', 'NIFTY 50')`. The bhavcopy's
`P/E`, `P/B` and `Div Yield` columns are stored in the nullable
`pe_ratio`/`pb_ratio`/`div_yield` fields.

`--refresh-valuations` (requires `--from/--to` or `--days`) re-downloads
already-ingested files and upserts **only** the three valuation columns, for
the one-time history backfill; OHLC and the rest are left untouched.

Examples:

```bash
python manage.py ingest_index_bhavcopy                    # latest
python manage.py ingest_index_bhavcopy --days 5           # last 5 calendar days
python manage.py ingest_index_bhavcopy --from 2024-08-01 --to 2024-08-31
python manage.py ingest_index_bhavcopy --from 2016-10-06 --to 2026-10-09 \
    --refresh-valuations --delay 2 --retries 3            # valuation backfill
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

### Corporate actions ingest

```
python manage.py ingest_corporate_actions [--latest | --from YYYY-MM-DD | --days N]
                                          [--to YYYY-MM-DD] [--lookback N] [--ahead N]
                                          [--symbol SYM] [--segment SEGMENT]
                                          [--chunk-days N] [--delay SECONDS]
                                          [--timeout SECONDS] [--retries N]
python manage.py check_corporate_action_prices [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                                               [--symbol SYM] [--all]
python manage.py rebuild_price_adjustments [--dry-run] [--full]
```

| Option | Meaning |
|---|---|
| `--latest` | Ex-dates from `--lookback` days ago to `--ahead` days from now. Default mode. |
| `--from YYYY-MM-DD [--to YYYY-MM-DD]` | Inclusive **ex-date** range, chunked and processed oldest-first. `--to` defaults to today. |
| `--days N` | Last `N` calendar days (ending today). |
| `--lookback N` / `--ahead N` | Window for `--latest`. Defaults 7 / 30 (upcoming actions are announced in advance). |
| `--symbol SYM` / `--segment SEGMENT` | Filters passed to NSE. Segment default `equities`. |
| `--chunk-days N` | Window size for long ranges. Default 90. |
| `--delay` / `--retries` / `--timeout` | Throttle between chunks (1.0s), attempts per chunk (3), per-request timeout (60s; `jugaad-data`'s session has none, so a stalled NSE response would otherwise hang forever). |

Live-API command like the announcements one (no `--data-dir`). Each NSE record
becomes a `corporate_actions` row plus decoded `corporate_action_legs`
(dividend / split / bonus / rights / ...); see
[`corporateactions/AGENTS.md`](corporateactions/AGENTS.md).

Guarantees:

- Idempotent: keyed on `(instrument, ex_date, subject)`; re-running updates
  amended dates, re-parses rows from an older `PARSER_VERSION`, never touches
  `verified` status.
- Fixed regex parsing only. A subject not decoded completely, or a split/bonus
  whose factor disagrees with the real price move (±25%), is stored with
  `status=needs_review` and a `review_reason`; nothing is guessed.
- Price checks run for the actions touched by the run plus recent ones still
  lacking price data; `check_corporate_action_prices` re-runs them (e.g. after a
  price backfill).
- A failed window is retried, the run continues with the other windows and
  raises `CommandError` (exit 1) at the end; a re-run fills the gap.

`rebuild_price_adjustments` rebuilds `price_adjustment_segments` (split/bonus/
consolidation ranges an API joins to adjust `nse_cm_price_history`; raw prices are
never modified). An event is applied once the symbol has a price on or after its
ex-date, never earlier; only `auto`/`verified` actions count. It writes only the
instruments whose ranges changed, so it is safe to run any time (`--dry-run` to
preview, `--full` to rewrite everything). See
[`corporateactions/AGENTS.md`](corporateactions/AGENTS.md#price-adjustment).

Examples:

```bash
python manage.py ingest_corporate_actions                       # latest (+30 days ahead)
python manage.py ingest_corporate_actions --from 2025-01-01     # backfill
python manage.py ingest_corporate_actions --symbol RELIANCE --from 2025-01-01
python manage.py check_corporate_action_prices --all
python manage.py rebuild_price_adjustments --dry-run
```

### Integrated filings ingest

```
python manage.py ingest_integrated_filings [--latest | --from YYYY-MM-DD | --days N]
                                           [--to YYYY-MM-DD] [--lookback N]
                                           [--symbol SYM] [--segment equities|sme]
                                           [--period-ended DD-Mon-YYYY|all]
                                           [--type financials|governance]
                                           [--page-size N] [--delay SECONDS] [--retries N]
```

Live NSE API (`NSELive.corporate_integrated_filing`), paginated; no `--data-dir`.
Rows land in `integrated_filings` keyed by NSE's `seq_id`; a revision is its own
row (`type_sub = Revision`). Only the list is recorded, not the XBRL. Idempotent
(`ignore_conflicts`), a failed page is retried then raises `CommandError`. The feed
has no ISIN, so the instrument is resolved through `NSESymbolInstrumentMap` by
symbol. Details and gotchas (null `broadcast_Date` on revisions) in
[`integratedfilings/AGENTS.md`](integratedfilings/AGENTS.md).

```bash
python manage.py ingest_integrated_filings                       # last 3 days
python manage.py ingest_integrated_filings --period-ended 30-Sep-2026 --from 2026-10-01
python manage.py ingest_integrated_filings --symbol TCS --days 60
```

### Integrated filings extraction

```
python manage.py extract_integrated_filings [--bucket NAME | --local-dir PATH]
                                            [--limit N] [--seq-id N] [--symbol SYM]
                                            [--max-attempts N] [--delay SECONDS] [--timeout SECONDS]
                                            [--retry-missing] [--rebuild]
```

Takes `integrated_filings` rows that are `pending` (or `failed` with attempts left), downloads
the XBRL, splits it into one JSON per statement (all periods inside), uploads to the bucket
named by `$GCS_INTEGRATED_FILINGS_BUCKET` (or `--bucket`; `--local-dir` for testing) and records
them in `integrated_filing_statements`, one folder per release (`.../{basis}/{seq_id}/`). Bucket layout, status flow, the "latest release" query and `--rebuild` are in
[`integratedfilings/AGENTS.md`](integratedfilings/AGENTS.md#extraction).

```bash
python manage.py extract_integrated_filings --local-dir /tmp/out --limit 5     # try locally
python manage.py extract_integrated_filings                                    # to the bucket
python manage.py extract_integrated_filings --seq-id 211600                    # redo one
```

### Shareholding pattern ingest

```
python manage.py ingest_shareholding_pattern [--latest | --from YYYY-MM-DD | --days N]
                                             [--to YYYY-MM-DD] [--lookback N]
                                             [--symbol SYM] [--segment equities|sme]
                                             [--delay SECONDS] [--retries N]
```

Live NSE API (`NSELive.shareholding_pattern`), no `--data-dir`. Rows land in
`shareholding_patterns` keyed on NSE's `recordId`; revisions are separate rows.
Idempotent (`ignore_conflicts`), a failed API call is retried then raises
`CommandError`. Details in [`shareholdingpattern/AGENTS.md`](shareholdingpattern/AGENTS.md).

```bash
python manage.py ingest_shareholding_pattern                     # last 3 days
python manage.py ingest_shareholding_pattern --days 30
python manage.py ingest_shareholding_pattern --symbol TCS --from 2026-10-01
```

### Shareholding pattern extraction

```
python manage.py extract_shareholding_pattern [--bucket NAME | --local-dir PATH]
                                             [--limit N] [--record-id N] [--symbol SYM]
                                             [--max-attempts N] [--delay SECONDS] [--timeout SECONDS]
                                             [--retry-missing] [--rebuild]
```

Downloads each pending filing's XBRL, parses it with `jugaad-xbrl` 0.6.0's
`parse_shareholding()`, and uploads the raw XML plus one JSON document to the bucket
named by `$GCS_SHAREHOLDING_PATTERN_BUCKET` (or `--bucket`; `--local-dir` for testing):

```
shareholding/{symbol}/{YYYY-MM-DD}/{record_id}.xml
shareholding/{symbol}/{YYYY-MM-DD}/{record_id}.json
```

Status flow and `--rebuild`/`--retry-missing` work like integrated filings. See
[`shareholdingpattern/AGENTS.md`](shareholdingpattern/AGENTS.md#extraction).

```bash
python manage.py extract_shareholding_pattern --local-dir /tmp/out --limit 5
python manage.py extract_shareholding_pattern --record-id 216349
```

### Event calendar ingest

```
python manage.py ingest_event_calendar [--latest | --from YYYY-MM-DD | --days N]
                                       [--to YYYY-MM-DD] [--lookback N] [--ahead N]
                                       [--symbol SYM] [--segment equities|sme]
                                       [--delay SECONDS] [--retries N]
```

Live NSE API (`NSELive.event_calendar`); no `--data-dir`. `--latest` (default) covers
`--lookback` days back through `--ahead` days forward, so upcoming board meetings are
recorded. NSE gives no ID, so rows are keyed on a deterministic hash of
`(segment, symbol, event_date, purpose, description)` and inserted idempotently.
Equity and SME records are normalized into `event_calendar`; unknown symbols get an
ISIN-less `Instrument`. Details in [`eventcalendar/AGENTS.md`](eventcalendar/AGENTS.md).

```bash
python manage.py ingest_event_calendar                      # last 3 + next 30 days
python manage.py ingest_event_calendar --days 90
python manage.py ingest_event_calendar --symbol TCS --from 2026-01-01 --to 2026-12-31
```

### Stock industry classification

```
python manage.py ingest_stock_classification [--file PATH | --date YYYY-MM-DD] [--lookback N]
                                             [--symbol SYM] [--limit N] [--retry-not-found]
                                             [--dry-run] [--delay SECONDS] [--retries N]
                                             [--timeout SECONDS]
```

Downloads a recent UDiFF CM bhavcopy (to a temp dir) for the `(ISIN, symbol, series)` pairs,
then calls the live NSE quote (`--delay` 3 s apart) only for instruments with no row in
`instrument_classification`, storing macro/sector/industry/basic industry. Run after the
bhavcopy ingest (daily or weekly; after the first full run each run is only new listings).
Outcomes `ok` / `not_found` are stored; transient errors and ISIN mismatches leave no row and
make the run exit 1. Details in [`securityinfo/AGENTS.md`](securityinfo/AGENTS.md#industry-classification).

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
- A failed chunk is retried up to `--retries` (including transient
  `database is locked` errors from the Go API's concurrent reads); if it still
  fails the run continues with the remaining chunks and raises `CommandError`
  at the end (exit 1), so a re-run retries the gap.
- Progress (INFO) and failures go to console and `logs/pipeline.log`; failures raise
  `CommandError` (exit 1).

## Root-level files

| File | Purpose |
|---|---|
| `manage.py` | Django CLI entry point (`DJANGO_SETTINGS_MODULE=pipeline.settings`). |
| `requirements.txt` | Dependencies: `django`, `google-cloud-storage`, `jugaad-data`, `jugaad-xbrl`, `python-dotenv`. Edit manually; install from it. |
| `temp_bhavcopy.py` | Throwaway script: downloads one legacy-format NSE bhavcopy into `data/`. |
| `README.md` | Project intro, setup and CLI reference. |
| `.gitignore` | Ignore rules (`data/`, `db/`, `logs/`, `env/`, ...). |
