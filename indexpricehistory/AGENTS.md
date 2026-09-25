# AGENTS.md — `indexpricehistory/`

Django app for daily price history (OHLC) of NIFTY indices. Schema is designed in
[`spec/schema.md`](../spec/schema.md); the models in `models.py` implement
§3.6 (`indices`) and §3.7 (`index_price_history`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `Index`, `IndexPriceHistory` per `spec/schema.md` §3.6–3.7. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `IndexpricehistoryConfig` (app name `indexpricehistory`). |
| `tests.py` | pending | Tests for the bhavcopy ingestion command. |
| `management/commands/ingest_index_bhavcopy.py` | ready | Downloads NIFTY index bhavcopies from NIFTY Indices website (via `jugaad-data`) and ingests them. Modes: `--latest`, `--from/--to`, `--days` (oldest first). Skips already-ingested files; per-file atomic ingest; `--no-download` to re-ingest files already on disk; `--delay SECONDS` spaces out API downloads to respect the rate limit (default 2.0, 0 disables); `--retries N` retries failed downloads (default 3, backing off by `--delay`). Known NSE holidays are skipped before any download (local all-year calendar from `jugaad-data` + NSE's live holiday API for the current year). The trade date is taken from the file content, not the filename. Each file is deleted from disk after it is ingested. Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `migrations/` | ready | `0001_initial` (both tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |
