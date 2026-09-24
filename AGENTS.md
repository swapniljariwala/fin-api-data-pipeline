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

- Django project `pipeline/` with three apps: `securityinfo` (instrument
  master: series, instruments, tickers), `dailypricehistory` (bhavcopy
  provenance + daily CM OHLC) and `fnopricehistory` (F&O contract identity +
  daily OHLC/OI).
- Working `ingest_bhavcopy` management command: downloads NSE CM bhavcopies and
  ingests them into `cm_price_history`. Idempotent and restart-safe; see
  [CLI reference](#cli-reference) below.
- Working `ingest_fno_bhavcopy` management command: same pattern for NSE F&O
  bhavcopies, into `fno_price_history` / `fno_contracts`.
- Project-level logging: console + `logs/pipeline.log` (rotated daily, 7 days kept).
- Object-store upload is a stub (`dailypricehistory/object_store.py`); to be
  implemented later.

## Folder index

| Path | What it is | Details |
|---|---|---|
| [`pipeline/`](pipeline/AGENTS.md) | Django project (settings, URLconf, ASGI/WSGI) | [AGENTS.md](pipeline/AGENTS.md) |
| [`dailypricehistory/`](dailypricehistory/AGENTS.md) | Django app: daily CM price/OHLC history | [AGENTS.md](dailypricehistory/AGENTS.md) |
| [`fnopricehistory/`](fnopricehistory/AGENTS.md) | Django app: F&O contract identity + daily OHLC/OI | [AGENTS.md](fnopricehistory/AGENTS.md) |
| [`securityinfo/`](securityinfo/AGENTS.md) | Django app: security/instrument master | [AGENTS.md](securityinfo/AGENTS.md) |
| [`spec/`](spec/AGENTS.md) | Design docs: DB schema + bhavcopy formats | [AGENTS.md](spec/AGENTS.md) |

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

## Root-level files

| File | Purpose |
|---|---|
| `manage.py` | Django CLI entry point (`DJANGO_SETTINGS_MODULE=pipeline.settings`). |
| `requirements.txt` | Dependencies: `django`, `jugaad-data`, `python-dotenv`. Edit manually; install from it. |
| `temp_bhavcopy.py` | Throwaway script: downloads one legacy-format NSE bhavcopy into `data/`. |
| `README.md` | Project intro, setup and CLI reference. |
| `.gitignore` | Ignore rules (`data/`, `db/`, `logs/`, `env/`, ...). |
