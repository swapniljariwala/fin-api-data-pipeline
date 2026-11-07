# AGENTS.md — `integratedfilings/`

Django app tracking NSE **Integrated Filing** releases (SEBI's combined quarterly
filing; "Financials" is the XBRL-bearing one). This app records only the **list of
releases** as NSE publishes it. It does not download or parse the XBRL itself;
statement extraction (P&L, balance sheet, ... via `jugaad-xbrl`) is a later step
and will hang off `integrated_filings` through its own table.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `IntegratedFiling` (`integrated_filings`): one row per NSE release, unique on NSE's `seq_id`. FK to `securityinfo.Instrument`; `filing_type`, `type_sub` (Original/Revision), `period_end`, `consolidated` (NULL when not applicable, e.g. governance), `audited`, `broadcast_at`/`revised_at` (IST-aware), `revision_remark`, `company_name`, `xbrl_url`/`ixbrl_url`/`xbrl_file_size`, `raw` (full source record). |
| `admin.py` | ready | `IntegratedFiling` registered. |
| `apps.py` | ready | `IntegratedfilingsConfig`. |
| `tests.py` | ready | Command tests with `NSELive` mocked (record shapes copied from live responses). Run: `python manage.py test integratedfilings`. |
| `management/commands/ingest_integrated_filings.py` | ready | Fetches NSE's live integrated-filing list (`NSELive.corporate_integrated_filing`) and ingests it. See below. |
| `migrations/` | ready | `0001_initial`. Not yet applied on production. |

## Command

```
python manage.py ingest_integrated_filings [--latest | --from YYYY-MM-DD | --days N]
                                           [--to YYYY-MM-DD] [--lookback N]
                                           [--symbol SYM] [--segment equities|sme]
                                           [--period-ended DD-Mon-YYYY|all]
                                           [--type financials|governance]
                                           [--page-size N] [--delay SECONDS] [--retries N]
```

- Modes as in the other live-feed commands: `--latest` (default, `--lookback` 3 days),
  `--from/--to`, `--days N`. The date range filters NSE's side; `--period-ended`
  filters by quarter end (e.g. `30-Sep-2026`).
- The API is paginated (request `page` starts at 1, response `page` at 0); pages are
  fetched until `totalCount`, `--delay` apart, each page retried up to `--retries`.
  A page that still fails raises `CommandError` (exit 1).
- Idempotent: `bulk_create(ignore_conflicts=True)` on `seq_id`, one transaction per run.

## Things worth knowing

- **A revision is a separate release** with its own `seq_id` and `type_sub = Revision`.
  "Latest filing for a company/quarter" is a query over
  `(instrument, period_end, consolidated)` ordered by `broadcast_at`, not an update.
- **Revisions have `broadcast_Date = null` in NSE's feed**; their time is in
  `revised_Date`. `broadcast_at` is therefore `broadcast_Date`, else `revised_Date`,
  else `creation_Date`. (Found on the first live run: 58 of 750 records were
  otherwise dropped.)
- **The feed has no ISIN**, only `symbol`. The instrument is looked up through
  `NSESymbolInstrumentMap`; an unknown symbol gets an ISIN-less `Instrument` named
  after the symbol (same fallback as the announcements ingest). On an empty
  instrument master (fresh DB) this means every company is created that way.
- Records missing `seq_Id`, `symbol`, `qe_Date` or every date field are logged and
  skipped, not fatal.
- `ixbrl` is only a rendering of the same data; `xbrl_url` is the machine-readable file
  (on `nsearchives.nseindia.com`, no cookies needed).
- No cron entry yet.
