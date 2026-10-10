# Deployment guide

How the fin-api-data-pipeline is deployed and operated on the production host.
This is a **server-only** Django project: it exposes no web surface of its own
(no gunicorn/uwsgi, no public URLconf beyond the unused admin). Its sole job is
to run management commands (bhavcopy ingests) on a schedule and write rows into
a SQLite database that a separate Go service reads.

## Topology at a glance

```
NSE / NIFTY websites
        │  (jugaad-data)
        ▼
fin-api-data-pipeline  ── writes ──▶  db/db.sqlite3
  venv: env/                           │
  cron: management commands            │  read-only
                                       ▼
                       fin-api (Go)  ──▶  https://bhav.marketsetup.in
                       systemd: finapi.service
```

- `fin-api-data-pipeline` (this repo) ingests data on a cron schedule.
- `fin-api` (separate Go repo at `/home/swapnil/apps/fin-api`) serves the data
  as an HTTP API from the **same** SQLite file. It is *not* part of this repo;
  see its own deployment notes if you need to change it.

## Server access

| Item | Value |
|---|---|
| SSH alias | `ssh finapi` |
| Hostname | `finapi` |
| OS | Ubuntu LTS |
| Login user | `swapnil` |
| App directory | `/home/swapnil/apps/fin-api-data-pipeline` |
| Git branch | `main` (deploys are fast-forward pulls from `origin/main`) |

## Filesystem layout

| Path | What it is |
|---|---|
| `/home/swapnil/apps/fin-api-data-pipeline` | The app checkout (this repo). |
| `…/env` | Python virtual environment (Linux layout: `env/bin/python`, `env/bin/pip`). |
| `…/db/db.sqlite3` | The production SQLite database (WAL mode). |
| `…/db/db.sqlite3-wal`, `…-shm` | SQLite WAL sidecar files; normal while the DB is in use. |
| `…/data` | Downloaded bhavcopy files (can be emptied; re-downloadable). |
| `…/logs/pipeline.log` | Rotating app log (see [Logs](#logs-and-monitoring)). |

`db/`, `data/`, `logs/` and `env/` are git-ignored and live **only** on the
server (and any local testing copy). Never commit them.

## Virtual environment

The venv lives at `env/` in the repo root. There is **no** activation needed for
cron or one-off commands — invoke the interpreter by absolute path:

```bash
/home/swapnil/apps/fin-api-data-pipeline/env/bin/python \
    /home/swapnil/apps/fin-api-data-pipeline/manage.py <command> [options]
```

Observed versions (prod):

| Package | Version |
|---|---|
| Python | 3.14.4 |
| Django | 6.1.1 |
| jugaad-data | 0.35.9 |
| python-dotenv | 1.2.3 |

Dependencies are declared in `requirements.txt` and installed **into the venv**.
Per the repo's hard constraints, edit `requirements.txt` by hand and install
from it; never `pip install <pkg>` directly and never `pip freeze` into it:

```bash
cd /home/swapnil/apps/fin-api-data-pipeline
env/bin/pip install -r requirements.txt
```

## Database

- File: `/home/swapnil/apps/fin-api-data-pipeline/db/db.sqlite3`.
- Engine/config in `pipeline/settings.py`: SQLite with WAL, `synchronous=NORMAL`,
  `busy_timeout=5000`, `mmap_size`, `cache_size`, `foreign_keys=ON`. The
  `busy_timeout` matters because the Go API reads the same file concurrently.
- Size is large (≈2 GB); the WAL can hold hundreds of MB between checkpoints.
- Backup/snapshot uses SQLite's own tooling (never copy a live WAL DB by hand):

  ```bash
  sqlite3 db/db.sqlite3 "VACUUM INTO '/home/swapnil/db_snapshot.sqlite3'"
  ```

## Scheduled jobs (cron)

Schedule lives in **user `swapnil`'s crontab** (`crontab -l`), not systemd. It
uses `CRON_TZ=Asia/Kolkata` and runs every 15 minutes during the 04:00–08:00 IST
window (after NSE publishes the previous session's files), with a 5-day
lookback so missed runs self-heal:

```
CRON_TZ=Asia/Kolkata
*/15 4-8 * * * /home/swapnil/apps/fin-api-data-pipeline/env/bin/python /home/swapnil/apps/fin-api-data-pipeline/manage.py  ingest_bhavcopy --latest --lookback 5
*/15 4-8 * * * /home/swapnil/apps/fin-api-data-pipeline/env/bin/python /home/swapnil/apps/fin-api-data-pipeline/manage.py  ingest_fo_bhavcopy --latest --lookback 5
*/15 4-8 * * * /home/swapnil/apps/fin-api-data-pipeline/env/bin/python /home/swapnil/apps/fin-api-data-pipeline/manage.py  ingest_index_bhavcopy --latest --lookback 5
```

| Job | Command | Writes to |
|---|---|---|
| CM daily prices | `ingest_bhavcopy --latest --lookback 5` | `nse_cm_price_history` |
| F&O daily prices | `ingest_fo_bhavcopy …` (**broken, see below**) | `fno_price_history` |
| Index prices | `ingest_index_bhavcopy --latest --lookback 5` | `index_price_history` |

Notes:

- Commands are idempotent and restart-safe, so the 15-minute repeat is cheap:
  already-ingested files are skipped and each run is a no-op when up to date.
- The commands log to `logs/pipeline.log`; cron output/errors are not captured
  anywhere (no MTA is installed on the host), which is why failures are silent.
- **There are no cron entries for the maintenance commands** `purge_expired_fno_contracts`
  or `purge_duplicate_bhavcopies`. They are currently run manually. See
  [Recommended hardening](#recommended-hardening).

### Known bug: F&O cron uses the wrong command name

`ingest_fo_bhavcopy` is **not a valid command** — the implemented command is
`ingest_fno_bhavcopy` (extra `n`). Django exits with
`Unknown command: 'ingest_fo_bhavcopy'. Did you mean ingest_fno_bhavcopy?`, and
because cron has nowhere to deliver stderr, the job has been failing silently.
Effect: as of this writing, F&O data is stale (last row 2026-09-24) while CM and
index data are current. Fix:

```bash
crontab -e   # change ingest_fo_bhavcopy -> ingest_fno_bhavcopy
```

and backfill the gap:

```bash
cd /home/swapnil/apps/fin-api-data-pipeline
env/bin/python manage.py ingest_fno_bhavcopy --from 2026-09-25 --to 2026-10-10
```

## Deploying a new version

1. SSH in and enter the app directory:

   ```bash
   ssh finapi
   cd /home/swapnil/apps/fin-api-data-pipeline
   ```

2. Inspect before pulling (the working tree should be clean):

   ```bash
   git status
   git fetch origin
   git log --oneline -5 origin/main
   ```

3. Fast-forward to the release:

   ```bash
   git pull --ff-only origin main
   ```

4. Install/refresh dependencies (edit `requirements.txt` first if it changed):

   ```bash
   env/bin/pip install -r requirements.txt
   ```

5. Apply migrations:

   ```bash
   env/bin/python manage.py migrate
   ```

6. Smoke-test the commands (read-only where possible):

   ```bash
   env/bin/python manage.py ingest_bhavcopy --latest --lookback 5
   env/bin/python manage.py ingest_fno_bhavcopy --latest --lookback 5
   env/bin/python manage.py ingest_index_bhavcopy --latest --lookback 5
   tail -n 20 logs/pipeline.log
   ```

There is nothing to restart: cron picks up the new code on the next tick. If you
changed something the Go API reads (schema), coordinate with `fin-api`; the
`finapi.service` may need `sudo systemctl restart finapi.service`.

## Running commands manually

Use the absolute interpreter path shown above. The full option set for each
command is documented in the top-level [`AGENTS.md`](../AGENTS.md#cli-reference)
and [`README.md`](../README.md). Common backfills:

```bash
# Re-ingest a specific window (newest-first; idempotent)
env/bin/python manage.py ingest_bhavcopy --from 2026-09-01 --to 2026-09-30
env/bin/python manage.py ingest_fno_bhavcopy --from 2026-09-25 --to 2026-10-10
env/bin/python manage.py ingest_index_bhavcopy --from 2026-09-01 --to 2026-09-30
```

## Syncing the production DB locally for testing

Testing against real data means pulling the prod SQLite file down. Always take a
consistent snapshot on the server; do not rsync a live WAL database.

```bash
# 1. On the server: compact + consistent single-file snapshot
ssh finapi 'cd /home/swapnil/apps/fin-api-data-pipeline && \
  sqlite3 db/db.sqlite3 "VACUUM INTO '\''/home/swapnil/db_snapshot.sqlite3'\''"'

# 2. On your machine: pull it into the local db/ (git-ignored) directory
rsync -avz --progress --partial \
  finapi:/home/swapnil/db_snapshot.sqlite3 \
  db/db.sqlite3

# 3. On the server: remove the snapshot
ssh finapi 'rm -f /home/swapnil/db_snapshot.sqlite3'
```

The local file is at `db/db.sqlite3`, exactly where `pipeline/settings.py`
expects it, so local management commands/test runs use the real data. The
snapshot is ~1.6 GB; a `VACUUM INTO` takes under a minute.

Quick sanity check after syncing:

```bash
sqlite3 db/db.sqlite3 \
  "SELECT 'cm', max(trade_date) FROM nse_cm_price_history
   UNION ALL SELECT 'fno', max(trade_date) FROM fno_price_history
   UNION ALL SELECT 'index', max(trade_date) FROM index_price_history;"
```

## Logs and monitoring

- App log: `/home/swapnil/apps/fin-api-data-pipeline/logs/pipeline.log`,
  rotated daily at midnight, 7 days retained (`pipeline/settings.py` `LOGGING`).
- App loggers (`dailypricehistory`, `fnopricehistory`, `indexpricehistory`,
  `securityinfo`, `corporateannouncements`) log at INFO; everything else at
  WARNING+. Failures are logged at ERROR with a traceback and make the command
  exit non-zero.
- The Go API service logs separately under `/home/swapnil/apps/fin-api/logs`.

Because cron discards stdout/stderr, the only durable record of a scheduled run
is `pipeline.log`. A failed job that never reaches Django's logging (e.g. the
`ingest_fo_bhavcopy` typo) produces **no log line at all**.

```bash
tail -f /home/swapnil/apps/fin-api-data-pipeline/logs/pipeline.log
```

## Recommended hardening

- Fix the F&O cron command name (see above); backfill the gap.
- Wrap each cron entry so output and exit status are captured, e.g.

  ```
  */15 4-8 * * * cd /home/swapnil/apps/fin-api-data-pipeline && \
    env/bin/python manage.py ingest_fno_bhavcopy --latest --lookback 5 \
    >> logs/cron.log 2>&1
  ```

  and alert on non-zero exit.
- Add periodic maintenance jobs that are currently manual:
  `purge_expired_fno_contracts --apply` (retention) and
  `purge_duplicate_bhavcopies --apply` (repair) at a low-traffic hour.

## Known drift / gotchas

- **Migration drift.** Production has an untracked
  `dailypricehistory/migrations/0005_alter_bhavcopyfile_id_alter_nsecmpricehistory_id.py`
  (`AutoField → BigAutoField`, generated by Django 6.1.1) that is not in the
  repo. Local/repo migrations stop at `0004` (generated by Django 5.2). When
  deploying, run `manage.py makemigrations --check --dry-run` and reconcile
  before/after `migrate` so the two histories don't diverge further.
- **`requirements.txt` is unpinned** for Django and `python-dotenv`; a fresh
  install can bump Django. Pin versions when reproducibility matters.
- **`settings.py` has `DEBUG=True`** and a hard-coded `SECRET_KEY`/`ALLOWED_HOSTS`.
  Acceptable only because no web surface of this project is exposed; do not
  expose the admin without hardening.
- **The Go API reads the same DB.** Any long write/backfill locks it briefly;
  the configured `busy_timeout=5000` is what keeps concurrent reads from
  erroring. Avoid interactive `VACUUM` (full rewrite) during market hours.
