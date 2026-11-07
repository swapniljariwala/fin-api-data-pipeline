# AGENTS.md — `dailypricehistory/`

Django app for daily price history (OHLC) of securities. Schema is designed in
[`spec/schema.md`](../spec/schema.md); the models in `models.py` implement
§3.4 (`bhavcopy_files`) and §3.5 (`cm_price_history`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `BhavcopyFile`, `NSECmPriceHistory` per `spec/schema.md` §3.4–3.5, and `NSECmDelivery` (`nse_cm_delivery`: `price` one-to-one **primary key** to the price row, `trade_date`, `deliv_qty`, `deliv_per`; 1-year retention). |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `DailypricehistoryConfig` (app name `dailypricehistory`). |
| `tests.py` | ready | Tests for the bhavcopy ingestion command. |
| `object_store.py` | stub | Placeholder `upload_bhavcopy_to_object_store()` for S3/GCS/Azure; not implemented yet. |
| `management/commands/ingest_bhavcopy.py` | ready | Downloads NSE CM bhavcopies (via `jugaad-data`) and ingests them. Modes: `--latest`, `--from/--to`, `--days` (newest first). Skips already-ingested files; per-file atomic ingest; `--no-download` to re-ingest files already on disk; `--upload` calls the object-store stub after each ingest; `--delay SECONDS` spaces out NSE API downloads to respect the rate limit (default 2.0, 0 disables); `--retries N` retries failed downloads (default 3, backing off by `--delay`). Known NSE holidays are skipped before any download (local all-year calendar from `jugaad-data` + NSE's live holiday API for the current year); if a download still fails or yields a non-CSV response, the day is classified as a trading holiday (INFO) or a genuine download failure (WARNING). The trade date is taken from the file content (`DATE1`/`TradDt`), not the filename, so a stale file served for a holiday cannot land under a wrong date. Each file is deleted from disk after it is ingested (and uploaded, if `--upload` is set). Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `management/commands/ingest_delivery_bhavcopy.py` | ready | Subclass of `ingest_bhavcopy` (same options, retry/holiday/restart-safety) that downloads the NSE **full** bhavcopy (`sec_bhavdata_full_*.csv`) and stores only `DELIV_QTY`/`DELIV_PER` in `nse_cm_delivery`. The full file has no ISIN, so `(symbol, series)` is resolved onto that date's `nse_cm_price_history` row via `nse_symbols_instrument_map`; unmatched/ambiguous rows are logged and skipped. A date with no price rows fails before `bhavcopy_files` is written (re-run after the regular bhavcopy is ingested). Dates older than 365 days are not fetched. |
| `management/commands/purge_old_delivery.py` | ready | Retention: deletes `nse_cm_delivery` rows (and their `format="full"` `bhavcopy_files` rows) older than 365 days. Dry run unless `--apply`; price rows are never touched. |
| `test_delivery.py` | ready | Tests for the delivery ingest and purge. (`tests.py` is stale: it imports the removed `CmPriceHistory` and fails to load.) |
| `management/commands/purge_duplicate_bhavcopies.py` | ready | One-off data repair: flags bhavcopy files whose stored rows are near-identical to the previous trading day's file (the signature of NSE serving the previous day's bhavcopy for a holiday date under older code). Dry run by default; `--apply` deletes the flagged files and their price rows. Keeps real data such as Diwali muhurat sessions (which differ from the prior day). |
| `migrations/` | ready | `0001_initial` … `0006_analyze_cm_price_history`, `0007_nsecmdelivery` (delivery table + `full` file format). Prod also carries an untracked `0005`; see `docs/DEPLOYMENT.md`. |
| `__init__.py` | ready | Marks the folder as a Python package. |
