# AGENTS.md

Index of the repository. Each folder has its own `AGENTS.md` explaining the files
inside it; read the relevant one before working in that folder.

## Hard constraints (from CLAUDE.md)

- Never run Python/pip outside virtual environment `env/`.
- Never `pip install <pkg>` directly, and never `pip freeze > requirements.txt` 45 edit `requirements.txt` manually, install from it.

## Project overview

Downloads NSE/BSE datasets (via `jugaad-data`) into SQLite, managed with Django.
Current state: scaffold only, plus a temp bhavcopy downloader script and specs.

## Folder index

| Path | What it is | Details |
|---|---|---|
| [`pipeline/`](pipeline/AGENTS.md) | Django project (settings, URLconf, ASGI/WSGI) | [AGENTS.md](pipeline/AGENTS.md) |
| [`dailypricehistory/`](dailypricehistory/AGENTS.md) | Django app: daily price/OHLC history | [AGENTS.md](dailypricehistory/AGENTS.md) |
| [`securityinfo/`](securityinfo/AGENTS.md) | Django app: security/instrument master | [AGENTS.md](securityinfo/AGENTS.md) |
| [`spec/`](spec/AGENTS.md) | Design docs: DB schema + bhavcopy formats | [AGENTS.md](spec/AGENTS.md) |

## Root-level files

| File | Purpose |
|---|---|
| `manage.py` | Django CLI entry point (`DJANGO_SETTINGS_MODULE=pipeline.settings`). |
| `requirements.txt` | Dependencies: `django`, `jugaad-data`, `python-dotenv`. Edit manually; install from it. |
| `temp_bhavcopy.py` | Throwaway script: downloads one legacy-format NSE bhavcopy into `data/`. |
| `README.md` | Short project intro. |
| `.gitignore` | Ignore rules. |
