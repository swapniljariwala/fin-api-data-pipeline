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
| `management/commands/ingest_bhavcopy.py` | ready | Downloads NSE CM bhavcopies (via `jugaad-data`) and ingests them. Modes: `--latest`, `--from/--to`, `--days` (newest first). Skips already-ingested files; per-file atomic ingest; `--no-download` to re-ingest files already on disk; `--upload` calls the object-store stub after each ingest. Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `migrations/` | ready | `0001_initial` (both tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |
