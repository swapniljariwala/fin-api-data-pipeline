# AGENTS.md — `pipeline/`

Django **project** package (the settings module is `pipeline.settings`).

## Files

| File | What it is |
|---|---|
| `settings.py` | Django settings. Notable: SQLite DB at `db/db.sqlite3` with WAL pragmas (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` for Litestream, `mmap_size`, `cache_size`, `foreign_keys=ON`); console email backend; `DEBUG=True`; both local apps (`dailypricehistory`, `securityinfo`) in `INSTALLED_APPS`; `LOGGING` dict with console + daily-rotated file handler (`logs/pipeline.log`, 7 backups kept) — app loggers at INFO, everything else WARNING+. |
| `urls.py` | URLconf; only the `admin/` route so far. |
| `asgi.py` | ASGI entry point. |
| `wsgi.py` | WSGI entry point. |
| `__init__.py` | Marks the folder as a Python package. |
