# AGENTS.md — `dailypricehistory/`

Django app for daily price history (OHLC) of securities. Schema is designed in
[`spec/schema.md`](../spec/schema.md); the models in `models.py` implement
§3.4 (`bhavcopy_files`) and §3.5 (`cm_price_history`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `BhavcopyFile`, `CmPriceHistory` per `spec/schema.md` §3.4–3.5. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `DailypricehistoryConfig` (app name `dailypricehistory`). |
| `tests.py` | ready | Tests for the bhavcopy ingestion command. |
| `object_store.py` | stub | Placeholder `upload_bhavcopy_to_object_store()` for S3/GCS/Azure; not implemented yet. |
| `management/commands/ingest_bhavcopy.py` | ready | Downloads NSE CM bhavcopies (via `jugaad-data`) and ingests them. Modes: `--latest`, `--from/--to`, `--days` (newest first). Skips already-ingested files; per-file atomic ingest; `--no-download` to re-ingest files already on disk; `--upload` calls the object-store stub after each ingest; `--delay SECONDS` spaces out NSE API downloads to respect the rate limit (default 2.0, 0 disables); `--retries N` retries failed downloads (default 3, backing off by `--delay`). Known NSE holidays are skipped before any download (local all-year calendar from `jugaad-data` + NSE's live holiday API for the current year); if a download still fails or yields a non-CSV response, the day is classified as a trading holiday (INFO) or a genuine download failure (WARNING). The trade date is taken from the file content (`DATE1`/`TradDt`), not the filename, so a stale file served for a holiday cannot land under a wrong date. Each file is deleted from disk after it is ingested (and uploaded, if `--upload` is set). Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `management/commands/purge_duplicate_bhavcopies.py` | ready | One-off data repair: flags bhavcopy files whose stored rows are near-identical to the previous trading day's file (the signature of NSE serving the previous day's bhavcopy for a holiday date under older code). Dry run by default; `--apply` deletes the flagged files and their price rows. Keeps real data such as Diwali muhurat sessions (which differ from the prior day). |
| `migrations/` | ready | `0001_initial` (both tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |
