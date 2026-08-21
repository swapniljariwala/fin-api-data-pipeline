# AGENTS.md — `securityinfo/`

Django app for security/instrument master data (global security identity, per-exchange
tickers). Schema is designed in [`spec/schema.md`](../spec/schema.md) (`instruments`,
`instrument_tickers`, `series` tables); the Django models have not been written yet.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | stub | Empty; will hold `instruments`/`instrument_tickers`/`series` models per `spec/schema.md`. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | stub | Empty; no models registered yet. |
| `apps.py` | ready | `SecurityinfoConfig` (app name `securityinfo`). |
| `tests.py` | stub | Empty; no tests yet. |
| `migrations/` | fresh | Contains only `__init__.py`; no migrations generated yet. |
| `__init__.py` | ready | Marks the folder as a Python package. |
