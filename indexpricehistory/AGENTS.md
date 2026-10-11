# AGENTS.md — `indexpricehistory/`

Django app for daily price history (OHLC) of NIFTY indices. Schema is designed in
[`spec/schema.md`](../spec/schema.md); the models in `models.py` implement
§3.6 (`indices`) and §3.7 (`index_price_history`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `Index`, `IndexPriceHistory` per `spec/schema.md` §3.6–3.7, including the nullable valuation columns `pe_ratio`/`pb_ratio`/`div_yield`. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `IndexpricehistoryConfig` (app name `indexpricehistory`). |
| `tests.py` | ready | Model tests plus regression tests for the bhavcopy parse (real header names + `DD-MM-YYYY` date), the valuation columns, and the `--refresh-valuations` upsert path. |
| `management/commands/ingest_index_bhavcopy.py` | ready | Downloads NIFTY index bhavcopies from NIFTY Indices website (via `jugaad-data`) and ingests them. Modes: `--latest`, `--from/--to`, `--days` (oldest first). Skips already-ingested files; per-file atomic ingest; `--no-download` to re-ingest files already on disk; `--delay SECONDS` spaces out API downloads to respect the rate limit (default 2.0, 0 disables); `--retries N` retries failed downloads (default 3, backing off by `--delay`); `--refresh-valuations` (with `--from/--to` or `--days`) re-downloads already-ingested files and upserts only the `P/E`/`P/B`/`Div Yield` columns (one-time backfill). Known NSE holidays are skipped before any download (local all-year calendar from `jugaad-data` + NSE's live holiday API for the current year). The trade date is taken from the file content, not the filename. Each file is deleted from disk after it is ingested. Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `migrations/` | ready | `0001_initial` (both tables); `0002` adds the three valuation columns; applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |

## Index bhavcopy CSV format

`ind_close_all_DDMMYYYY.csv` (from NIFTY Indices, via `jugaad-data`'s
`NSEIndicesArchives.bhavcopy_index_save`) has this exact header:

```
Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,
Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield
```

The parser in `ingest_index_bhavcopy.py` maps these names verbatim — note there
is a space before the parenthesis in `Turnover (Rs. Cr.)`. A real row looks like:

```
Nifty 50,09-01-2026,25840.4,25940.6,25623,25683.3,-193.55,-.75,348757262,27486.64,22.36,3.49,1.31
```

- Date (`Index Date`) is **`DD-MM-YYYY`**, not ISO; parsed with `%d-%m-%Y`,
  falling back to ISO and then to the expected/filename date.
- `turnover` is in **Rs. Crore**, stored as-is (matches the read API contract).
- The valuation columns `P/E`, `P/B` and `Div Yield` are parsed into the
  `pe_ratio`, `pb_ratio` and `div_yield` fields; `Div Yield` is stored as the
  source percentage value.
- `-`/`NA`/blank cells map to `NULL`; the columns are intentionally nullable.
  Missing/renamed valuation columns also degrade to `NULL`, so older files are safe.
