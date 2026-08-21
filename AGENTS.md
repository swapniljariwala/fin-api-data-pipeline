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

- Django project `pipeline/` with two apps: `securityinfo` (instrument master:
  series, instruments, tickers) and `dailypricehistory` (bhavcopy provenance +
  daily OHLC).
- Working `ingest_bhavcopy` management command: downloads NSE CM bhavcopies and
  ingests them into `cm_price_history`. Idempotent and restart-safe; see
  [CLI reference](#cli-reference) below.
- Project-level logging: console + `logs/pipeline.log` (rotated daily, 7 days kept).
- Object-store upload is a stub (`dailypricehistory/object_store.py`); to be
  implemented later.

## Folder index

| Path | What it is | Details |
|---|---|---|
| [`pipeline/`](pipeline/AGENTS.md) | Django project (settings, URLconf, ASGI/WSGI) | [AGENTS.md](pipeline/AGENTS.md) |
| [`dailypricehistory/`](dailypricehistory/AGENTS.md) | Django app: daily price/OHLC history | [AGENTS.md](dailypricehistory/AGENTS.md) |
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
- Progress (INFO) and failures (ERROR with traceback) go to console and
  `logs/pipeline.log`; failures exit non-zero.

## Root-level files

| File | Purpose |
|---|---|
| `manage.py` | Django CLI entry point (`DJANGO_SETTINGS_MODULE=pipeline.settings`). |
| `requirements.txt` | Dependencies: `django`, `jugaad-data`, `python-dotenv`. Edit manually; install from it. |
| `temp_bhavcopy.py` | Throwaway script: downloads one legacy-format NSE bhavcopy into `data/`. |
| `README.md` | Project intro, setup and CLI reference. |
| `.gitignore` | Ignore rules (`data/`, `db/`, `logs/`, `env/`, ...). |
