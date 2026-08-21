# AGENTS.md — `dailypricehistory/`

Django app for daily price history (OHLC) of securities. Schema is designed in
[`spec/schema.md`](../spec/schema.md) (`cm_price_history` table); the Django models
have not been written yet.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | stub | Empty; will hold `cm_price_history`-related models per `spec/schema.md`. |
| `views.py` | stub | Empty; no HTTP views yet. |
| `admin.py` | stub | Empty; no models registered yet. |
| `apps.py` | ready | `DailypricehistoryConfig` (app name `dailypricehistory`). |
| `tests.py` | stub | Empty; no tests yet. |
| `migrations/` | fresh | Contains only `__init__.py`; no migrations generated yet. |
| `__init__.py` | ready | Marks the folder as a Python package. |
