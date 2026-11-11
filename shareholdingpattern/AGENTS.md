# AGENTS.md — `shareholdingpattern/`

Django app tracking NSE **shareholding pattern** filings. Two jobs:

1. `ingest_shareholding_pattern` records the list of filings NSE publishes.
2. `extract_shareholding_pattern` downloads each filing's XBRL, parses it with
   `jugaad-xbrl`'s `parse_shareholding()` and uploads the raw XML plus one JSON
   document per filing.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `ShareholdingPattern` (`shareholding_patterns`): one row per NSE filing, unique on NSE's `recordId`; FK to `securityinfo.Instrument`; ISIN, symbol, company, segment, `quarter_end`, `broadcast_at`, `submitted_on`, promoter/public percentages, revision fields, `xbrl_url`/`xbrl_file_size`, `raw`, and extraction progress (`extraction_status`, `extraction_attempts`, `extraction_error`, `extracted_at`, `raw_object_path`, `parsed_object_path`, `parsed_size_bytes`). |
| `extraction.py` | ready | Pure XML -> bucket objects logic: path layout and `parse_shareholding(...).to_dict()` envelope. No network, no DB. |
| `storage.py` | ready | `GCSStorage` and `LocalStorage` behind `put`/`get`; `get_storage()` picks one. Bucket comes from `GCS_SHAREHOLDING_PATTERN_BUCKET`. |
| `management/commands/ingest_shareholding_pattern.py` | ready | Fetches `NSELive.shareholding_pattern` and ingests it. See below. |
| `management/commands/extract_shareholding_pattern.py` | ready | Download, parse, upload, mark done. See below. |
| `testdata/sample_shp.xml` | ready | A real RKEC Sep-2026 shareholding-pattern XBRL used by the extraction tests. |
| `tests.py` | ready | Ingest command tests with `NSELive` mocked (record shapes copied from live responses). |
| `tests_extract.py` | ready | Extraction tests (download mocked, local-folder storage). |
| `admin.py` / `apps.py` | ready | Model registered; `ShareholdingpatternConfig`. |
| `migrations/` | ready | `0001_initial`. |

## Ingest command

```
python manage.py ingest_shareholding_pattern [--latest | --from YYYY-MM-DD | --days N]
                                             [--to YYYY-MM-DD] [--lookback N]
                                             [--symbol SYM] [--segment equities|sme]
                                             [--delay SECONDS] [--retries N]
```

- Modes as in the other live-feed commands: `--latest` (default, `--lookback` 3 days),
  `--from/--to`, `--days N`. NSE filters on broadcast/submission date.
- Idempotent: `bulk_create(ignore_conflicts=True)` on `record_id`, one transaction per run.
- The feed has a proper `isin`, so the instrument is resolved ISIN-first (falling back to
  the `NSESymbolInstrumentMap` symbol lookup only when it is absent). The symbol-at-filing-time
  is recorded in `NSESymbolInstrumentMap`.
- A revision is its own `recordId`; `revised`/`revised_at`/`revision_remark` hold its metadata.
- Records missing `recordId`, `symbol` or `date` are logged and skipped.
- `--delay`/`--retries` apply to the one API call.

## Extraction

```
python manage.py extract_shareholding_pattern [--bucket NAME | --local-dir PATH]
                                             [--limit N] [--record-id N] [--symbol SYM]
                                             [--max-attempts N] [--delay SECONDS] [--timeout SECONDS]
                                             [--retry-missing] [--rebuild]
```

Destination: `--local-dir` (testing), else `--bucket`, else
**`GCS_SHAREHOLDING_PATTERN_BUCKET`**; none of them set is an error. Credentials are
Google Application Default Credentials.

Layout (`symbol` URL-quoted; `date` is NSE's `date` field, usually the quarter end but
sometimes an event date, so it is **not** converted to an FY/quarter label):

```
shareholding/{symbol}/{YYYY-MM-DD}/{record_id}.xml     original XML, as filed
shareholding/{symbol}/{YYYY-MM-DD}/{record_id}.json    parse_shareholding(...).to_dict()
```

- The JSON carries the parser's `company`, `report`, `flags`, `shareholding`, and optional
  `details`/`significant_beneficial_owners`/`unclaimed_shares`/`limits`, plus a `source`
  block with `record_id`, symbol, ISIN, segment, quarter end, broadcast/submission times,
  revision flag, `xbrl_url` and the parser's `report_start`/`report_end`.
- Status flow: `pending` -> `done`, or `failed` (+1 attempt, error saved). `--max-attempts`
  defaults to 3; `--record-id N` forces one row. `--retry-missing` re-tries failed rows
  whose error is HTTP 404 or a non-XML download. `--rebuild` recreates the JSON from the
  stored raw XML without contacting NSE.
- `--symbol` is resolved to its instrument through `NSESymbolInstrumentMap`; selection
  and ordering then use `instrument_id`, never the symbol column.
- HTTP 403/429 stops the run at once (row stays `pending`, no attempt counted).
- `jugaad-xbrl >= 0.6.0` is required for `parse_shareholding()`; `parse()` is for NSE
  integrated financial results and deliberately rejects SHP documents.

## Schedule

Cron on `finapi` runs daily at **21:30 IST** (`flock`-wrapped): list ingest with
`--latest --lookback 7`, then `extract_shareholding_pattern --limit 1000`, both
appending to `logs/cron.log`. See
[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#scheduled-jobs-cron).

## TODO

- Decide whether to also upload `to_yaml()` for human readers; JSON is the only parsed
  object today.
