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
`standalone`|`consolidated`; symbols are URL-quoted, `M&M` -> `M%26M`). **Every release
(Original or Revision) has its own `seq_id` folder; nothing is overwritten:**

```
raw/{symbol}/{fy}/{quarter}/{basis}/{seq_id}.xml                       original XML, as filed
filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/{statement}.json      one per statement, all periods inside
filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/canonical.json        ~12 headline numbers
filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/_manifest.json        statements present (written last)
```

- A statement object has `company`, `report`, `units`, `source` (seq_id, type, URL), `statement`,
  `role_code` and `entries`: one entry per period (P&L of a Q2 filing has Q2 and H1; Q4 has Q4 and
  FY; a balance sheet has an `as_of` date). Statement names come from `jugaad-xbrl`.
- **Status flow**: `pending` -> `done`, or `failed` (+1 attempt, error saved). `pending` and
  `failed` with attempts left are picked up newest-broadcast first (fresh filings are never stuck
  behind a backfill), `--limit` per run. After `--max-attempts` (3) a filing is left `failed`;
  `--seq-id N` re-runs one regardless. A crash leaves a filing `pending`; the re-run overwrites its
  own folder, so it is safe. The command carries on past a failed filing and exits 1 at the end.
  **HTTP 403/429 from NSE stops the run at once** (the filing stays `pending`, no attempt counted).
  Downloads are `--delay` (1.0 s) apart.
- **Releases**: each release has its own statement rows in `integrated_filing_statements` and its own
  folder, so an Original and its Revisions all stay readable. **Latest** is a query, not an overwrite:
  the newest `done` release with statements, by `broadcast_at` then `seq_id`:
  ```sql
  select f.* from integrated_filings f
  where f.extraction_status='done' and exists (select 1 from integrated_filing_statements s where s.filing_id=f.id)
    and not exists (select 1 from integrated_filings g
        where g.instrument_id=f.instrument_id and g.period_end=f.period_end and g.consolidated=f.consolidated
          and g.filing_type=f.filing_type and g.extraction_status='done'
          and exists (select 1 from integrated_filing_statements s2 where s2.filing_id=g.id)
          and (g.broadcast_at>f.broadcast_at or (g.broadcast_at=f.broadcast_at and g.seq_id>f.seq_id)));
  ```
  A newer release that failed (e.g. NSE 404) is skipped, so "latest" is the newest one that has data.
  Always use `object_path` from the table; never rebuild a path from the current symbol.
- **`--rebuild`**: re-creates the parsed objects of `done` filings that have none in this layout
  (older runs shared one folder per quarter, or kept only the raw XML of a superseded release).
  It reads the raw XML from the bucket (NSE is not contacted, no attempt counted). A filing that
  fails keeps `done` with `extraction_error = "rebuild failed: ..."`. The old shared-folder
  objects are left in place (orphans) until someone deletes them.
- `fy`/`quarter` come from NSE's quarter-end date with the Indian April-March convention, not
  from the filing (a filer can mis-write the header, and companies with a calendar or Jul-Jun
  year have a different own fiscal quarter: balance sheet and cash flow follow the company's own
  half-years). `jugaad-xbrl` 0.4.0 `canonical()` is empty for a file with a mis-written period.
- Sizes: P&L ~6 KB, balance sheet ~3 KB, cash flow ~7 KB, `related_party_transactions` has a
  median of ~90 KB (max 2.8 MB on prod). Readers should page that one.

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

## TODO

- **`jugaad-xbrl`: map `total_equity` and `paid_up_capital` for the `other_than_bank` format** (library
  change, owner: the library; nothing to do in this repo until a release). For this older format
  (e.g. L&T FY25 Q4, seq 89929 / 89932) `canonical.json` has both fields null although the filing carries
  `ShareholdersFunds` (total equity) and `PaidUpValueOfEquityShareCapital`. The mapping file
  `specs/canonical/other_than_bank.yaml` simply has no entry; add:
  ```yaml
  total_equity: [ShareholdersFunds]
  paid_up_capital: [PaidUpValueOfEquityShareCapital]
  ```
  with a test on the L&T file. After the release: bump the pin in `requirements.txt`, deploy, then refresh the
  affected filings from their stored raw XML with `extract_integrated_filings --rebuild --seq-id N`
  (one per `other_than_bank` filing; no NSE access). Scope: only `canonical.json` of that format.
  It does not cover banks / insurers (they file no single equity line, so `total_equity` stays null by
  design) or the HDFC Life Dec-2025 file whose reporting period the filer mis-wrote.
- **`jugaad-xbrl`: support the new insurer Ind AS taxonomy** (`format = insurance_indas`, header
  `IFInsuranceIndAs V1.0 (30-09-2026)`, schema `in-capmkt-ent-2026-08-31`; file names `IF_INS_COM_IND_AS_*`).
  `jugaad-xbrl` 0.4.0 raises `cannot identify the filing format`; the unreleased 0.4.1 identifies the format
  and reads the header and facts but has no statement layout yet (`sections()` is empty, `canonical()` raises
  `NotImplementedError`). Until the library supports it these filings stay `failed` (3 attempts used). Known
  on 2026-11-08: GODIGIT seq 210652, NIVABUPA seq 209588, STARHEALTH seq 205171-205174 (Sep-2026) and 202542
  (Jun-2026); expect more insurers on this taxonomy. After the library release: bump the pin in
  `requirements.txt`, deploy, then re-run the failed ones with `extract_integrated_filings --max-attempts 6`
  (their attempt count is 3, so they are picked up again; downloads from NSE, 1 s apart).
  *Decision (2026-11-08):* no raw-XML-only fallback for now, wait for the library. If the library is slow, the
  option is to mark such filings `done` with a note "no taxonomy: raw XML only" so they stop counting as failures.
- **Company's own fiscal year and quarter (decided later; today paths use Indian April-March labels for
  everyone).** For companies with a calendar or Jul-Jun year the path `fy`/`quarter` differs from their own
  fiscal quarter (31 filings, 16 companies on 2026-11-08; e.g. Schaeffler India, calendar year: its Jun-2026
  filing is labelled FY2027 Q1 here but is its own half-year end, with balance sheet and cash flow). The
  mismatch is already recorded in the manifest `warnings` / `extraction_error` ("filing header says FY2026 Q3,
  NSE list says FY2027 Q2"). Option when needed: store the company's own `fy`/`quarter` (from the filing's
  `report`) in the manifest and as columns on `integrated_filings`, without changing the paths. Readers must
  discover statements per filing and not assume Mar/Sep (see `FIN_API_HANDOFF.md`).

