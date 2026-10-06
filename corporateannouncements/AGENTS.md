# AGENTS.md — `corporateannouncements/`

Django app for NSE corporate announcements (board meetings, general updates, etc.).
Schema is designed in [`spec/schema.md`](../spec/schema.md); the models in `models.py`
implement §3.8 (`announcement_categories`) and §3.9 (`corporate_announcements`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `AnnouncementCategory`, `CorporateAnnouncement` per `spec/schema.md` §3.8–3.9. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `CorporateannouncementsConfig` (app name `corporateannouncements`). |
| `tests.py` | pending | Tests for the ingest command. |
| `management/commands/ingest_corporate_announcements.py` | ready | Fetches NSE's live `corporate_announcements` feed (via `jugaad-data`'s `NSELive`) and ingests them. Modes: `--latest` (default, last `--lookback` days), `--from/--to`, `--days N`. Optional `--symbol SYM` and `--segment` (default `equities`) filters, passed through to the API call. Idempotent via `bulk_create(ignore_conflicts=True)` keyed on `seq_id`'s unique constraint, inside one transaction per run. Records missing/unparseable `seq_id` or `sort_date` are logged and skipped rather than failing the run. `sort_date`/`exchdisstime` are parsed and made timezone-aware as IST before storage. Instrument resolved via `sm_isin`, falling back to `symbol`, also recording the symbol-at-filing-time in `NSESymbolInstrumentMap`. Progress and errors logged to console + `logs/pipeline.log` (INFO), failures raise `CommandError` (exit 1). |
| `migrations/` | ready | `0001_initial` (both tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |
