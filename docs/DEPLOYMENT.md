# Deployment guide

How the fin-api-data-pipeline is deployed and operated on the production host.
This is a **server-only** Django project: it exposes no web surface of its own
(no gunicorn/uwsgi, no public URLconf beyond the unused admin). Its sole job is
to run management commands (bhavcopy ingests, the corporate-announcements poll,
and the mutual-fund NAV fetch) on a schedule and write rows into a SQLite
database that a separate Go service reads.

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
| jugaad-data | 0.37.0 |
| jugaad-xbrl | 0.5.0 |
| google-cloud-storage | 3.17.1 |
| python-dotenv | 1.2.3 |

Dependencies are declared in `requirements.txt` and installed **into the venv**.
Per the repo's hard constraints, edit `requirements.txt` by hand and install
from it; never `pip install <pkg>` directly and never `pip freeze` into it:

```bash
cd /home/swapnil/apps/fin-api-data-pipeline
env/bin/pip install -r requirements.txt
```

## Database

- File: `/mnt/blockstorage/db/db.sqlite3`, set by `PIPELINE_DB_PATH` in the
  server's `.env` (`settings.py` falls back to `<repo>/db/db.sqlite3`, which does
  not exist on the host).
- Engine/config in `pipeline/settings.py`: SQLite with WAL, `synchronous=NORMAL`,
  `busy_timeout=5000`, `mmap_size`, `cache_size`, `foreign_keys=ON`. The
  `busy_timeout` matters because the Go API reads the same file concurrently.
- Size is large (≈4.5 GB); the WAL can hold hundreds of MB between checkpoints.
- Backup/snapshot uses SQLite's own tooling (never copy a live WAL DB by hand):

  ```bash
  sqlite3 /mnt/blockstorage/db/db.sqlite3 ".backup /mnt/blockstorage/db_snapshot.sqlite3"
  ```

  `.backup` is an online, consistent copy with no compaction. Use
  `VACUUM INTO '<path>'` instead if you want a compacted copy. Stage snapshots
  on `/mnt/blockstorage`, not in the home directory (the root volume is small).

## Scheduled jobs (cron)

Schedule lives in **user `swapnil`'s crontab** (`crontab -l`), not systemd. It
uses `CRON_TZ=Asia/Kolkata`. The four bhavcopy jobs run every 15 minutes during
the 04:00–08:59 IST window (after NSE publishes the previous session's files),
with a 5-day lookback so missed runs self-heal. They are **spaced 3 minutes
apart in priority order** (stock, index, F&O, delivery) so they do not all start
on the same minute and fight over the SQLite write lock on the slow disc;
delivery goes last because it needs that date's CM bhavcopy already ingested.
The corporate-announcements feed is **live** and can update at any second, so
it is polled **every minute**. The mutual-fund NAV report for a session is
published by AMFI the same evening, so it is fetched **once daily** at 09:30
IST, after the bhavcopy window. Stock industry classification runs daily at
**16:00 IST**, after market close; NSE publishes the day's bhavcopy later in the
evening, so this run uses the previous session's file (new listings are picked
up the next day). Every job is wrapped in `flock` and appends to
`logs/cron.log` (changed 2026-11-07; previously the bhavcopy jobs ran bare and
all fired on the same minute):

```
CRON_TZ=Asia/Kolkata
0,15,30,45 4-8 * * * flock -n /tmp/ingest_bhavcopy.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_bhavcopy --latest --lookback 5 >> logs/cron.log 2>&1"
3,18,33,48 4-8 * * * flock -n /tmp/ingest_index_bhavcopy.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_index_bhavcopy --latest --lookback 5 >> logs/cron.log 2>&1"
6,21,36,51 4-8 * * * flock -n /tmp/ingest_fno_bhavcopy.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_fno_bhavcopy --latest --lookback 5 >> logs/cron.log 2>&1"
9,24,39,54 4-8 * * * flock -n /tmp/ingest_delivery_bhavcopy.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_delivery_bhavcopy --latest --lookback 5 >> logs/cron.log 2>&1"
# Stock industry classification: after market close; uses the latest published UDiFF bhavcopy (previous session)
0 16 * * * flock -n /tmp/ingest_stock_classification.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_stock_classification >> logs/cron.log 2>&1"
# Corporate announcements: live feed, poll every minute (lookback 1 = today only)
* * * * * flock -n /tmp/ingest_corporate_announcements.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_corporate_announcements --latest --lookback 1 >> logs/cron.log 2>&1"
# Mutual fund NAV: AMFI publishes the session's report the same evening; run after the bhavcopy window
30 9 * * * flock -n /tmp/ingest_mf_nav.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_mf_nav --latest --lookback 5 >> logs/cron.log 2>&1"
# Integrated filings: NSE release list, then XBRL -> statements -> GCS. Every 15 min at :12/:27/:42/:57,
# skipping the Litestream snapshot window (05:30-05:45). One lock so list and extraction never overlap.
12,27,42,57 0-4,6-23 * * * flock -n /tmp/integrated_filings.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_integrated_filings --latest --lookback 3 >> logs/cron.log 2>&1; env/bin/python manage.py extract_integrated_filings >> logs/cron.log 2>&1"
12,27,57 5 * * * flock -n /tmp/integrated_filings.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_integrated_filings --latest --lookback 3 >> logs/cron.log 2>&1; env/bin/python manage.py extract_integrated_filings >> logs/cron.log 2>&1"
# Integrated filings: weekly re-try of filings NSE did not serve (404 / empty file); waits for the lock instead of skipping
12 11 * * 0 flock -w 1800 /tmp/integrated_filings.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py extract_integrated_filings --retry-missing >> logs/cron.log 2>&1"
# Corporate actions: live NSE feed by ex-date, daily after the bhavcopy window so ex-date price checks see fresh closes
12 9 * * * flock -n /tmp/ingest_corporate_actions.lock bash -c "cd /home/swapnil/apps/fin-api-data-pipeline && env/bin/python manage.py ingest_corporate_actions --latest >> logs/cron.log 2>&1"
# Rotate logs/cron.log weekly (Sun 03:30 IST): rename, wait for in-flight appends, gzip, drop archives older than 28 days
30 3 * * 0 bash -c "cd /home/swapnil/apps/fin-api-data-pipeline/logs && d=\$(date +\%F) && [ -s cron.log ] && mv cron.log cron.log.\$d && sleep 75 && gzip -f cron.log.\$d && find . -maxdepth 1 -name 'cron.log.*.gz' -mtime +28 -delete"
```

The Litestream daily snapshot of the 5.4 GB DB starts at 05:30 IST (00:00 UTC)
and runs to about 05:41; the service is throttled (`Nice=15`,
`IOSchedulingClass=idle` in its systemd drop-in) so ingests and the API stay
responsive. Run migrations and other heavy DB work outside 05:30–05:45.

| Job | Command | Writes to |
|---|---|---|
| CM daily prices | `ingest_bhavcopy --latest --lookback 5` | `nse_cm_price_history` |
| CM delivery data | `ingest_delivery_bhavcopy --latest --lookback 5` (:09/:24/:39/:54, after `ingest_bhavcopy`, see [Delivery data](#delivery-data)) | `nse_cm_delivery` |
| F&O daily prices | `ingest_fno_bhavcopy --latest --lookback 5` | `fno_price_history` |
| Index prices | `ingest_index_bhavcopy --latest --lookback 5` | `index_price_history` |
| Stock industry classification | `ingest_stock_classification` (daily 16:00 IST, after market close, see [Stock industry classification](#stock-industry-classification)) | `instrument_classification` |
| Corporate announcements | `ingest_corporate_announcements --latest --lookback 1` (every minute) | `corporate_announcements` |
| Mutual fund NAV | `ingest_mf_nav --latest --lookback 5` (daily 09:30 IST) | `mutual_fund_nav_history` |
| Integrated filings (list) | `ingest_integrated_filings --latest --lookback 3` (every 15 min, :12/:27/:42/:57, see [Integrated filings](#integrated-filings)) | `integrated_filings` |
| Integrated filings (extraction) | `extract_integrated_filings` (same job, right after the list ingest) | `integrated_filing_statements`, GCS bucket |
| Integrated filings (weekly retry) | `extract_integrated_filings --retry-missing` (Sun 11:12 IST): re-tries filings NSE did not serve | `integrated_filings`, GCS bucket |
| Corporate actions | `ingest_corporate_actions --latest` (daily 09:12 IST, scheduled 2026-11-08, see [Corporate actions](#corporate-actions)) | `corporate_actions`, `corporate_action_legs` |

Notes:

- Commands are idempotent and restart-safe, so the repeats are cheap:
  already-ingested files/rows are skipped and each run is a no-op when up to date.
- The commands log to `logs/pipeline.log`, and all cron entries now also append
  stdout/stderr to `logs/cron.log` (no MTA is installed on the host, so
  uncaptured cron failures used to be silent). Each is wrapped in `flock` so a
  slow run cannot overlap the next one.
- Corporate announcements: `--lookback 1` is the command's minimum window
  (it rejects `<= 0`) and fetches **today only**; because the API takes day
  ranges, an announcement that lands just after local midnight is not picked up
  by the next day's window, so re-run a short range manually if that matters
  (e.g. `--from <yesterday> --to <today>`; safe/idempotent).
- **There are no cron entries for the maintenance commands** `purge_expired_fno_contracts`
  or `purge_duplicate_bhavcopies`. They are currently run manually. See
  [Recommended hardening](#recommended-hardening).

### Delivery data

`ingest_delivery_bhavcopy` loads `DELIV_QTY`/`DELIV_PER` from the NSE full
bhavcopy into `nse_cm_delivery` (1 year kept). It needs migration
`dailypricehistory.0007` and the regular bhavcopy for the date already ingested.
It is scheduled in the crontab above (same window as the price ingests, at
:09/:24/:39/:54 so the CM bhavcopy from :00 is already in).

Backfill (at most 365 days; older dates are skipped):
`manage.py ingest_delivery_bhavcopy --from <date> --delay 2 --retries 3`.
Retention is manual like the other purges:
`manage.py purge_old_delivery` (dry run), then `--apply`.

### Corporate announcements

NSE's corporate-announcement feed is a **live API** (not a dated archive), so
there is no downloaded file to keep and no `BhavcopyFile`-style provenance row.
The command upserts rows keyed on NSE's `seq_id`, so re-running any window is a
no-op for rows already present.

A one-off **12-month backfill** is run on the host as a detached (`nohup`) shell
loop that walks month by month and logs to
`logs/backfill_corporate_announcements.log`. Script:
`logs/backfill_corporate_announcements.sh` (git-ignored, operational).

```bash
# backfill ~12 months in monthly chunks, detached (already done 2026-10-10)
ssh finapi 'cd /home/swapnil/apps/fin-api-data-pipeline && \
  nohup bash logs/backfill_corporate_announcements.sh \
  > logs/backfill_corporate_announcements.nohup 2>&1 &'

# progress
ssh finapi 'tail -f /home/swapnil/apps/fin-api-data-pipeline/logs/backfill_corporate_announcements.log'
```

Ad-hoc/manual fetches use the same command:

```bash
env/bin/python manage.py ingest_corporate_announcements --from 2026-01-01 --to 2026-01-31
env/bin/python manage.py ingest_corporate_announcements --days 30
env/bin/python manage.py ingest_corporate_announcements --symbol RELIANCE --days 30
```

### Integrated filings

Two commands in one `flock`-wrapped cron job (see the crontab above): the list ingest
records NSE's Integrated Filing releases, then the extraction downloads each pending
filing's XBRL, splits it into one JSON per statement and uploads to GCS. Details, bucket
layout and the pending/done/failed status flow:
[`integratedfilings/AGENTS.md`](../integratedfilings/AGENTS.md).

Configuration lives in the server's `.env` (loaded by `settings.py`, git-ignored):

| Variable | Value |
|---|---|
| `GCS_INTEGRATED_FILINGS_BUCKET` | `bhav-marketsetup-db-replication` (the Litestream bucket; our objects use the `raw/` and `filings/` prefixes, which its lifecycle rule - it only deletes under `announcements/` - does not touch) |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/home/swapnil/service-account.json`, the same key Litestream and fin-api use (all run as `swapnil`; file mode `600` since 2026-11-08) |

Operations:

```bash
# status of extraction
sqlite3 /mnt/blockstorage/db/db.sqlite3 \
  "SELECT extraction_status, count(*) FROM integrated_filings GROUP BY 1;"
# what failed, and why
sqlite3 /mnt/blockstorage/db/db.sqlite3 \
  "SELECT seq_id, extraction_attempts, substr(extraction_error,1,120) FROM integrated_filings WHERE extraction_status='failed';"
# re-run one filing, or raise the retry limit for stuck ones
env/bin/python manage.py extract_integrated_filings --seq-id 211600
env/bin/python manage.py extract_integrated_filings --max-attempts 6
# backfill the list for a period (then the cron drains the extraction, 200 filings per 15 min)
env/bin/python manage.py ingest_integrated_filings --from 2026-10-01 --to 2026-10-31
```

The bucket has no object versioning (soft delete keeps removed or overwritten objects 7
days). Do not run migrations or heavy backfills during 05:30-05:45 IST.

### Corporate actions

NSE's corporate-actions feed is a **live API filtered by ex-date** (not a dated
archive), so there is no downloaded file or provenance row. Each record is
parsed with fixed regex logic into `corporate_actions` + `corporate_action_legs`
and sanity-checked against real prices; anything not fully decoded, or a
split/bonus whose factor disagrees with the real ex-date price move, is stored
with `status='needs_review'`. Details: [`corporateactions/AGENTS.md`](../corporateactions/AGENTS.md).

Deployed 2026-10-23 (requires `jugaad-data>=0.37.0`; `NSELive.corporate_actions`
does not exist in 0.35.x). Backfilled from 2025-01-01 (the start of our price
history) through 30 days ahead, in ~51s:

```bash
# one-off backfill, detached (done 2026-10-23); idempotent, safe to re-run
ssh finapi 'cd /home/swapnil/apps/fin-api-data-pipeline && \
  nohup env/bin/python manage.py ingest_corporate_actions \
  --from 2025-01-01 --to "$(date -d "+30 days" +%F)" \
  > logs/backfill_corporate_actions.log 2>&1 < /dev/null &'
```

Result on prod: 3,804 actions; 3,803 `auto`, 1 `needs_review` (QUINT, a
`Rights - 7 Ccps And 7 Warrants:40` issue the parser cannot decode). Price check:
165 passed, 4 inconclusive (bonus factor too close to 1), 6 `no_data` (no close on
both sides, e.g. a just-listed stock or an ex-date not yet traded; re-checked on
later runs), 0 failed.

**On cron since 2026-11-08**: daily at 09:12 IST (after the 04:00-08:59 bhavcopy window, before the 09:30
mutual-fund NAV job), `flock`-wrapped, output to `logs/cron.log`. A first manual run on that day fetched
103 records in 7 s. It had not been scheduled before, so ex-dates from 2 Nov were caught up by that run.

Review queue and re-checks:

```bash
sqlite3 /mnt/blockstorage/db/db.sqlite3 \
  "SELECT symbol, ex_date, subject, review_reason FROM corporate_actions WHERE status='needs_review';"
env/bin/python manage.py check_corporate_action_prices      # re-check rows lacking price data
```

Gotchas learned while building this:

- A face-value split gives the shares a **new ISIN**: NSE's record carries the old
  one while post-ex-date prices sit under the new one. Prices are therefore looked
  up through the NSE symbol (`nse_symbols_instrument_map`), not the action's
  instrument.
- `jugaad-data`'s requests session has **no timeout**; one stalled NSE response hung
  a local run for many minutes. The command sets its own per-request timeout
  (`--timeout`, default 60s). The older ingest commands do not, so a stalled NSE
  response can still hold their `flock`.

### Mutual fund NAV

AMFI's NAV history is a **live range report** (`AMFI.nav_history_raw()`, cached
by `jugaad-data` under `$J_CACHE_DIR`), not a dated archive, so there is no
downloaded file to keep and no provenance row. The command upserts rows keyed on
`(scheme, nav_date)`, so re-running any overlapping window is a no-op for rows
already present.

A one-off **10-year backfill** is run on the host, internally chunked monthly
(`--chunk-days 30`) and detached with `nohup`, logging to
`logs/backfill_mf_nav.log`:

```bash
# backfill the last 10 years, detached
ssh finapi 'cd /home/swapnil/apps/fin-api-data-pipeline && \
  nohup env/bin/python manage.py ingest_mf_nav \
  --from "$(date -d "10 years ago" +%F)" --to "$(date +%F)" \
  > logs/backfill_mf_nav.log 2>&1 &'

# progress
ssh finapi 'tail -f /home/swapnil/apps/fin-api-data-pipeline/logs/backfill_mf_nav.log'
```

Sanity check after the backfill:

```bash
sqlite3 db/db.sqlite3 \
  "SELECT max(nav_date), count(*), count(DISTINCT scheme_id) FROM mutual_fund_nav_history;"
```

Ad-hoc/manual fetches use the same command:

```bash
env/bin/python manage.py ingest_mf_nav --days 30
env/bin/python manage.py ingest_mf_nav --amc 128 --from 2026-09-01 --to 2026-09-30
```

### Index valuations (P/E, P/B, Div Yield)

`index_price_history` has three nullable valuation columns (`pe_ratio`,
`pb_ratio`, `div_yield`) populated from the `P/E`/`P/B`/`Div Yield` columns that
the daily NIFTY indices bhavcopy already carries. New daily ingests fill them
with no cron change; already-ingested history is filled by a one-off backfill
that re-downloads each file and upserts **only** the three columns (OHLC is left
untouched):

```bash
# one-time backfill 2016-10-06 -> present, detached (migration 0002 first)
env/bin/python manage.py migrate
ssh finapi 'cd /home/swapnil/apps/fin-api-data-pipeline && \
  nohup env/bin/python manage.py ingest_index_bhavcopy \
  --from 2016-10-06 --to "$(date +%F)" \
  --refresh-valuations --delay 2 --retries 3 \
  > logs/backfill_index_pe.log 2>&1 &'

# progress
ssh finapi 'tail -f /home/swapnil/apps/fin-api-data-pipeline/logs/backfill_index_pe.log'
```

~2,391 files at ~2s each ⇒ roughly 1–2 hours. Verify afterward:

```bash
sqlite3 db/db.sqlite3 \
  "SELECT count(*) FROM index_price_history WHERE pe_ratio IS NOT NULL;"
```


### Stock industry classification

`instrument_classification` is filled from the NSE live quote's `secInfo`
(`macro`/`sector`/`industryInfo`/`basicIndustry`); NSE publishes no bulk file.
`ingest_stock_classification` takes the ISIN/symbol/series triples from a recent
UDiFF CM bhavcopy and calls `stock_quote` only for instruments without a row,
`--delay` 3 s apart. It runs daily at **16:00 IST**, after market close. NSE
publishes a session's bhavcopy later in the evening (~18:00), so the 16:00 run
uses the **previous** session's file; new listings from the current session are
classified the next day. That is fine for classification (it changes slowly) and
keeps the run out of market hours. The 04:00 CM ingest has already created that
file's `Instrument` rows hours earlier. The **first full pass is ~3–4 h**
(~3,700 ISINs at 3 s); later runs only look up new listings and are quick. NSE
answers with no `secInfo` are stored as `not_found` and not retried (unless
`--retry-not-found`); transient errors and ISIN mismatches leave no row, make the
run exit 1, and are retried by the next run.

### Historical note: F&O cron typo (fixed 2026-10-10)

The F&O cron previously invoked `ingest_fo_bhavcopy`, which is **not a valid
command** — the implemented command is `ingest_fno_bhavcopy` (extra `n`). Django
exits with `Unknown command: 'ingest_fo_bhavcopy'. Did you mean
ingest_fno_bhavcopy?`, and because cron has nowhere to deliver stderr, the job
failed silently from ~2026-09-24 until it was noticed. The crontab was corrected
and the gap backfilled:

```bash
# what was run (one-off, already done)
env/bin/python manage.py ingest_fno_bhavcopy --from 2026-09-25 --to 2026-10-10
```

Lesson: because no MTA is installed, a scheduled command that fails *before*
Django's logging is set up leaves no trace at all. Capture `/bin/sh` output in
the cron line (see [Recommended hardening](#recommended-hardening)).

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

   **Warning:** on Django 6.1 every migration (DDL *or* data-only) ends with a
   bare `PRAGMA foreign_key_check`, i.e. a full scan of this 5.3 GB DB. See
   [Known drift / gotchas](#known-drift--gotchas) before running this on prod.

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
# 1. On the server: consistent single-file snapshot (no compaction), on the
#    block-storage volume (home is on a small root volume)
ssh finapi 'sqlite3 /mnt/blockstorage/db/db.sqlite3 ".backup /mnt/blockstorage/db_snapshot.sqlite3"'

# 2. On your machine: drop stale WAL files, then pull into the local db/ (git-ignored) directory
rm -f db/db.sqlite3-wal db/db.sqlite3-shm
rsync -avz --progress --partial \
  finapi:/mnt/blockstorage/db_snapshot.sqlite3 \
  db/db.sqlite3

# 3. On the server: remove the snapshot
ssh finapi 'rm -f /mnt/blockstorage/db_snapshot.sqlite3'
```

The local file is at `db/db.sqlite3`, exactly where `pipeline/settings.py`
expects it, so local management commands/test runs use the real data. The
snapshot is ~4.5 GB. The copy is in WAL mode, so `sqlite3 -readonly` cannot open
it (no `-shm`); open it normally.

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
- Cron-captured log: `logs/cron.log` — stdout/stderr of the corporate-announcements
  and mutual-fund NAV jobs (the cron entries that redirect their output). Rotated weekly
  (Sun 03:30 IST) into `cron.log.YYYY-MM-DD.gz`, archives older than 28 days deleted.
- Backfill log: `logs/backfill_corporate_announcements.log` — one-off 12-month
  announcements backfill (see [Corporate announcements](#corporate-announcements));
  `logs/backfill_mf_nav.log` — one-off 10-year mutual-fund NAV backfill (see
  [Mutual fund NAV](#mutual-fund-nav)).
- App loggers (`dailypricehistory`, `fnopricehistory`, `indexpricehistory`,
  `securityinfo`, `corporateannouncements`, `mfnavhistory`, `integratedfilings`) log at INFO;
  everything else at WARNING+. Failures are logged at ERROR with a traceback and
  make the command exit non-zero.
- The Go API service logs separately under `/home/swapnil/apps/fin-api/logs`.

Because cron discards stdout/stderr, the only durable record of a scheduled run
is `pipeline.log` (or `cron.log` for the announcements and mutual-fund NAV jobs,
the entries that redirect their output). A failed job that never reaches Django's
logging (e.g. the `ingest_fo_bhavcopy` typo) produces **no log line at all**.

```bash
tail -f /home/swapnil/apps/fin-api-data-pipeline/logs/pipeline.log
```

## Recommended hardening

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

- **Migration drift (resolved 2026-10-11).** `dailypricehistory` `0005`
  (`AutoField → BigAutoField`, generated by Django 6.1.1) was originally
  untracked on production. It is now committed, so the `0001`–`0005` history is
  shared. Root cause: `requirements.txt` was unpinned, so the dev venv (Django
  4.2/5.2) and prod (Django 6.1) disagreed on the default auto field. In SQLite
  both `AutoField` and `BigAutoField` are `INTEGER PRIMARY KEY`, so the migration
  is a schema no-op. Still run `manage.py makemigrations --check --dry-run`
  before/after `migrate` on deploy.
- **`requirements.txt` is pinned** (`django==6.1.1`, `jugaad-data==0.35.10`,
  `python-dotenv==1.2.3`). Production runs Python 3.14; keep the dev venv on the
  same Python and pinned versions so migrations stay in sync.
- **TODO: full-DB FK check on every `migrate` (Django 6.1 SQLite).**
  `DatabaseSchemaEditor.__exit__` calls `check_constraints()`, which runs a bare
  `PRAGMA foreign_key_check` over the whole database for *every* migration, even
  a data-only `RunPython` one. On 2026-11-11 the `securityinfo.0005` deploy sat
  in this scan at ~200 KB/s (hours). Worked around by applying the `sqlmigrate`
  SQL by hand, then `migrate securityinfo 0005 --fake`. TODO: add an env-gated
  override of `DatabaseWrapper.check_constraints` (or a small migrate wrapper)
  so a normal `migrate` does not scan every row; keep the full check as an
  explicit maintenance-window command.
- **`settings.py` has `DEBUG=True`** and a hard-coded `SECRET_KEY`/`ALLOWED_HOSTS`.
  Acceptable only because no web surface of this project is exposed; do not
  expose the admin without hardening.
- **The Go API reads the same DB.** Any long write/backfill locks it briefly;
  the configured `busy_timeout=5000` is what keeps concurrent reads from
  erroring. Avoid interactive `VACUUM` (full rewrite) during market hours.
