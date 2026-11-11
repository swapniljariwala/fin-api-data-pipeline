# AGENTS.md — `eventcalendar/`

Django app for NSE's board-meeting / event calendar. It is a live feed like
corporate announcements, so there is no dated archive file and no provenance
table.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `EventCalendar` (`event_calendar`): FK to `securityinfo.Instrument`, `record_key` (unique), segment, symbol, company name, event date, purpose, free-text description, announcement timestamp (SME), attachment/iXBRL URLs and sizes, `raw`, `ingested_at`. |
| `management/commands/ingest_event_calendar.py` | ready | Fetches `NSELive.event_calendar` and ingests it. See below. |
| `tests.py` | ready | Ingest command tests with `NSELive` mocked (equity and SME record shapes copied from live responses). |
| `admin.py` / `apps.py` | ready | Model registered; `EventcalendarConfig`. |
| `migrations/` | ready | `0001_initial`. |

## Command

```
python manage.py ingest_event_calendar [--latest | --from YYYY-MM-DD | --days N]
                                       [--to YYYY-MM-DD] [--lookback N] [--ahead N]
                                       [--symbol SYM] [--segment equities|sme]
                                       [--delay SECONDS] [--retries N]
```

| Option | Meaning |
|---|---|
| `--latest` | Event dates from `--lookback` days ago to `--ahead` days from today. Default mode. |
| `--from YYYY-MM-DD [--to YYYY-MM-DD]` | Inclusive **event-date** range. `--to` defaults to today. |
| `--days N` | Last `N` calendar days (ending today). |
| `--lookback N` / `--ahead N` | Window for `--latest`. Defaults 3 / 30 (upcoming events are announced in advance). |
| `--symbol SYM` | Restrict to one NSE symbol, applied locally (NSE does not accept a symbol filter here). |
| `--segment` | `equities` (default) or `sme`. The two return different schemas. |
| `--delay` / `--retries` | Retry back-off and total attempts for the API call. |

Examples:

```bash
python manage.py ingest_event_calendar                          # last 3 + next 30 days
python manage.py ingest_event_calendar --days 90
python manage.py ingest_event_calendar --symbol TCS --from 2026-01-01 --to 2026-12-31
```

Guarantees:

- Idempotent: NSE provides no ID, so `record_key` is a SHA-256 of
  `(segment, symbol, event_date, purpose, description)` and is unique. Rows are inserted
  with `bulk_create(ignore_conflicts=True)` in one transaction per run.
- Equity records use `symbol`/`company`/`purpose`/`bm_desc`/`date`; SME records use
  `bm_symbol`/`sm_name`/`bm_purpose`/`bm_desc`/`bm_date` and carry `bm_timestamp`,
  `attachment`, `ixbrl`, `attFileSize`. Both are normalized into the same columns.
- `bm_timestamp` (SME only) is parsed as IST; equity records leave `announced_at` NULL.
- Records missing symbol or date are logged and skipped.
- Instrument resolved through `NSESymbolInstrumentMap`; an unknown symbol gets an
  ISIN-less `Instrument` named after the symbol (same fallback as announcements and
  integrated filings).

## TODO

- No cron entry yet. A daily/weekly run is enough; the feed changes slowly compared
  with announcements.
- If NSE ever adds an ID, key on it and keep `record_key` as a fallback.
