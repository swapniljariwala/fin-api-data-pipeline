# AGENTS.md — `integratedfilings/`

Django app tracking NSE **Integrated Filing** releases (SEBI's combined quarterly
filing; "Financials" is the XBRL-bearing one). Two jobs:

1. `ingest_integrated_filings` records the **list of releases** as NSE publishes it.
2. `extract_integrated_filings` downloads each listed filing's XBRL, splits it into
   statements (P&L, balance sheet, cash flow, segments, related-party transactions, ...)
   with `jugaad-xbrl`, uploads them to a GCS bucket and indexes them in
   `integrated_filing_statements`.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `IntegratedFiling` (`integrated_filings`): one row per NSE release, unique on NSE's `seq_id`. FK to `securityinfo.Instrument`; `filing_type`, `type_sub` (Original/Revision), `period_end`, `consolidated` (NULL when not applicable, e.g. governance), `audited`, `broadcast_at`/`revised_at` (IST-aware), `revision_remark`, `company_name`, `xbrl_url`/`ixbrl_url`/`xbrl_file_size`, `raw` (full source record), and extraction progress: `extraction_status` (`pending`/`done`/`failed`), `extraction_attempts`, `extraction_error`, `extracted_at`. Also `IntegratedFilingStatement` (`integrated_filing_statements`): one row per statement per filing (`statement`, `role_code`, `description`, `periods`, `item_count`, `row_count`, `object_path`, `size_bytes`); unique on (filing, role_code). |
| `extraction.py` | ready | Pure XML -> bucket-objects logic (no network, no DB): path layout, FY/quarter derivation, statement/canonical/manifest JSON. |
| `storage.py` | ready | `GCSStorage` and `LocalStorage` behind `put`/`get`; `get_storage()` picks one. |
| `management/commands/extract_integrated_filings.py` | ready | Download, parse, upload, index. See "Extraction" below. |
| `testdata/` | ready | A real DENTA Q2 FY27 standalone XBRL used by the extraction tests. |
| `tests_extract.py` | ready | Extraction tests (download mocked, local-folder storage). |
| `admin.py` | ready | `IntegratedFiling` registered. |
| `apps.py` | ready | `IntegratedfilingsConfig`. |
| `tests.py` | ready | Ingest command tests with `NSELive` mocked (record shapes copied from live responses). Run: `python manage.py test integratedfilings`. |
| `management/commands/ingest_integrated_filings.py` | ready | Fetches NSE's live integrated-filing list (`NSELive.corporate_integrated_filing`) and ingests it. See below. |
| `migrations/` | ready | `0001_initial`, `0002_extraction_status_and_statements`. Not yet applied on production. |

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

## Extraction

```
python manage.py extract_integrated_filings [--bucket NAME | --local-dir PATH]
                                            [--limit N] [--seq-id N] [--symbol SYM]
                                            [--max-attempts N] [--delay SECONDS] [--timeout SECONDS]
```

Destination: `--local-dir` (testing), else `--bucket`, else the environment variable
**`GCS_INTEGRATED_FILINGS_BUCKET`**; none of them set is an error. Credentials are Google
Application Default Credentials (`GOOGLE_APPLICATION_CREDENTIALS` key file, or the host's
service account).

Bucket layout (`fy` = `FY2027` for Apr 2026 - Mar 2027, `quarter` Q1..Q4, `basis` =
`standalone`|`consolidated`; symbols are URL-quoted, `M&M` -> `M%26M`):

```
raw/{symbol}/{fy}/{quarter}/{basis}/{seq_id}.xml           every release, kept as filed
filings/{symbol}/{fy}/{quarter}/{basis}/{statement}.json   one per statement, all periods inside
filings/{symbol}/{fy}/{quarter}/{basis}/canonical.json     ~12 headline numbers
filings/{symbol}/{fy}/{quarter}/{basis}/_manifest.json     statements present (written last)
```

- A statement object has `company`, `report`, `units`, `source` (seq_id, type, URL), `statement`,
  `role_code` and `entries`: one entry per period (P&L of a Q2 filing has Q2 and H1; Q4 has Q4 and
  FY; a balance sheet has an `as_of` date). Statement names come from `jugaad-xbrl`.
- **Status flow**: `pending` -> `done`, or `failed` (+1 attempt, error saved). `pending` and
  `failed` with attempts left are picked up newest-broadcast first (fresh filings are never stuck behind a backfill), `--limit` per run. After
  `--max-attempts` (3) a filing is left `failed`; `--seq-id N` re-runs one regardless. A crash
  leaves a filing `pending`; the re-run overwrites, so it is safe. The command carries on past a
  failed filing and exits 1 at the end. **HTTP 403/429 from NSE stops the run at once** (the filing stays `pending`, no attempt counted). Downloads are `--delay` (1.0 s) apart. Governance filings and rows without a URL are not processed.
- **Revisions**: same symbol/quarter/basis -> the `filings/` objects are overwritten by the latest
  release and the index rows of earlier releases are dropped, so `integrated_filing_statements`
  always describes what is in the bucket. `raw/` keeps every release. A release extracted after a
  newer one is already done only stores its raw XML (`extraction_error` says so). Turn on bucket
  object versioning if you want history of the parsed objects.
- `fy`/`quarter` come from NSE's quarter-end date, not the filing's header facts (a filer can
  mis-write those; `jugaad-xbrl` 0.4.0 `canonical()` is empty for such a file, `to_dict` is fine).
- Sizes: P&L ~6 KB, balance sheet ~3 KB, cash flow ~7 KB, but `related_party_transactions` has a
  median of ~280 KB and reaches 2.3 MB. Readers should page that one.

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
- No cron entry yet. Plan: every 15 minutes, ingest then extract in one `flock`-wrapped job, minutes `:12/:27/:42/:57`, skipping the 05:30-05:45 IST Litestream window.
