# AGENTS.md — `fnopricehistory/`

Django app for NSE F&O (futures & options) price history. Design is in
[`spec/TODO-fno-storage.md`](../spec/TODO-fno-storage.md); mirrors the
patterns in [`dailypricehistory/`](../dailypricehistory/AGENTS.md) (CM
segment) with its own identity model, since F&O contracts have no ISIN and
only trade for their own lifetime (listing → expiry).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `FnoContract` (identity: `underlying_symbol`, `instrument_type`, `expiry_date`, `strike_price`, `option_type`) and `FnoPriceHistory` (unified futures+options OHLC/OI row, FK to `FnoContract` and to `dailypricehistory.BhavcopyFile`). |
| `admin.py` | ready | Both models registered. |
| `apps.py` | ready | `FnopricehistoryConfig` (app name `fnopricehistory`). |
| `tests.py` | ready | Tests for the F&O ingest command (legacy + UDiFF, rerun no-op, missing file) and the expired-contract purge command, using inline synthetic bhavcopy fixtures (no real F&O sample files are checked in). |
| `views.py` | stub | Empty; no HTTP views yet. |
| `management/commands/ingest_fno_bhavcopy.py` | ready | Downloads NSE F&O bhavcopies (via `jugaad-data`'s `bhavcopy_fo_save`) and ingests them. Same CLI shape, restart-safety, holiday handling and file-date-is-authoritative behavior as `dailypricehistory`'s `ingest_bhavcopy` — see its docstring for the shared rationale. Legacy `INSTRUMENT` values (`FUTSTK`/`OPTSTK`/`FUTIDX`/`OPTIDX`) are normalized to their UDiFF equivalents (`STF`/`STO`/`IDF`/`IDO`) via `jugaad_data`'s `_LEGACY_TO_UDIFF_INSTRUMENT_TYPE` so both formats land in one `instrument_type` column. UDiFF rows are filtered on `Sgmt == "FO"`. `BhavcopyFile.segment="FO"` (same shared table as CM). |
| `management/commands/purge_expired_fno_contracts.py` | ready | Data retention: dry-run by default; `--apply` deletes `FnoContract` rows expired more than 365 calendar days ago. `FnoPriceHistory` rows cascade-delete with their contract. |
| `migrations/` | ready | `0001_initial` (both tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |

## Not yet implemented

- Object-store upload: `--upload` calls `dailypricehistory.object_store` (the
  same stub used by CM ingest); it logs a warning until that stub is
  implemented.
- No cron/scheduler wiring for `purge_expired_fno_contracts` — see
  `spec/TODO-fno-storage.md` for why it's meant to run standalone, not
  inline in ingest.
