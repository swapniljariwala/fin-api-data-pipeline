# AGENTS.md — `securityinfo/`

Django app for security/instrument master data (global security identity, per-exchange
tickers). Schema is designed in [`spec/schema.md`](../spec/schema.md); the models in
`models.py` implement §3.1 (`series`), §3.2 (`instruments`) and §3.3
(`instrument_tickers`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `Series`, `Instrument`, `InstrumentTicker` per `spec/schema.md` §3.1–3.3. |
| `management/commands/ingest_stock_classification.py` | ready | Fills `instrument_classification` (NSE macro/sector/industry/basic industry) from the live quote, one throttled call per instrument lacking a row; symbol taken from a UDiFF bhavcopy. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | All three models registered. |
| `apps.py` | ready | `SecurityinfoConfig` (app name `securityinfo`). |
| `tests.py` | stub | Empty; no tests yet. |
| `migrations/` | ready | `0001_initial` (all three tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |

## Industry classification

`InstrumentClassification` (`instrument_classification`, one row per instrument): `macro`,
`sector`, `industry`, `basic_industry` from `secInfo` of `NSELive.stock_quote`, plus
`symbol_used`, `status` (`ok` / `not_found`) and `fetched_at`. NSE has no bulk file for this.
Bhavcopy carries no industry either, so it is used only to learn the live `(ISIN, symbol,
series)` (the DB cannot: prices store no symbol and the symbol map has no "current" flag).
The ISIN in NSE's answer must equal the bhavcopy ISIN, else the row is skipped. `not_found`
rows are not retried unless `--retry-not-found`. Bhavcopy includes bonds/debt, whose issuer
industry NSE also returns, so ~3,700 ISINs are looked up on the first run (~3 h at 3 s).
In production it is scheduled daily at 16:00 IST, after market close (it uses the most
recently published UDiFF bhavcopy, so the previous session's file); see
[`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md#stock-industry-classification).
