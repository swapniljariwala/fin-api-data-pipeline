# AGENTS.md — `securityinfo/`

Django app for security/instrument master data (global security identity, per-exchange
tickers). Schema is designed in [`spec/schema.md`](../spec/schema.md); the models in
`models.py` implement §3.1 (`series`), §3.2 (`instruments`) and §3.3
(`instrument_tickers`).

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `Series`, `Instrument`, `InstrumentTicker` per `spec/schema.md` §3.1–3.3. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | ready | All three models registered. |
| `apps.py` | ready | `SecurityinfoConfig` (app name `securityinfo`). |
| `tests.py` | stub | Empty; no tests yet. |
| `migrations/` | ready | `0001_initial` (all three tables); applied. |
| `__init__.py` | ready | Marks the folder as a Python package. |
