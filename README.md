# Fin Data Pipeline

Downloads NSE/BSE datasets (via `jugaad-data`) into an SQLite database, managed
with Django for its ORM and management commands.

## Setup

```bash
# create the virtual environment (once)
python -m venv env

# activate it (Windows)
env\Scripts\activate

# install dependencies, then migrate
pip install -r requirements.txt
python manage.py migrate
```

The SQLite database lives at `db/db.sqlite3` (WAL mode, Litestream-friendly
pragmas configured in `pipeline/settings.py`).

## Ingest NSE bhavcopy

The `ingest_bhavcopy` command downloads NSE CM (Capital Market) bhavcopy files
and ingests them into `cm_price_history`:

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
| `--data-dir PATH` | Download directory. Default: `data/`. |
| `--no-download` | Ingest only files already present on disk; no network. Use to retry after a partial failure. |
| `--lookback N` | How far back `--latest` searches. Default 30. |
| `--upload` | Call the object-store upload hook after each successful ingest (stub; logs a warning until implemented). |

Examples:

```bash
# Latest available bhavcopy
python manage.py ingest_bhavcopy

# Last 5 calendar days
python manage.py ingest_bhavcopy --days 5

# A specific date range
python manage.py ingest_bhavcopy --from 2024-07-01 --to 2024-07-31

# Retry an earlier failed run without re-downloading
python manage.py ingest_bhavcopy --from 2024-07-01 --to 2024-07-31 --no-download
```

### Behaviour and guarantees

- Files already recorded in `bhavcopy_files` are skipped; re-running is a no-op.
- Each file's rows are written inside one transaction, and the `bhavcopy_files`
  row is committed only together with its price rows. An interrupted run leaves
  no partial state and can simply be re-run.
- Price rows dedupe on `(instrument_ticker, trade_date, series)` via
  `ignore_conflicts` on insert.
- Both bhavcopy formats are supported: legacy (pre-8-Jul-2024) and UDiFF. The
  format is sniffed from the file header, not assumed from the date.
- Only the CM segment and `STK` instruments are ingested. Turnover is normalized
  to full rupees; `DELIV_*` / `AVG_PRICE` are not stored. See
  [`spec/schema.md`](spec/schema.md) for the full design.
- Ingesting a bhavcopy also upserts master data: `series`, `instruments` (keyed
  on ISIN) and `instrument_tickers` (keyed on `(source, ticker)`, upgraded with
  ISIN when a UDiFF row provides it).

## Logging

- App loggers (INFO) write to both the console and `logs/pipeline.log`.
- The file handler rotates daily and keeps 7 days of history.
- Failures are logged at ERROR with a traceback and make the command exit
  non-zero, so a scheduler can alert on it.

## Docs

- [`spec/schema.md`](spec/schema.md) - SQLite schema and design decisions.
- [`spec/bhavcopy-format.md`](spec/bhavcopy-format.md) - legacy vs UDiFF column
  specifications and the field mapping between them.
