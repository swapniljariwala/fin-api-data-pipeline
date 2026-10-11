# Index P/E (valuation) ingestion: plan

## Context

The daily NIFTY Indices bhavcopy that `ingest_index_bhavcopy` already downloads
(`ind_close_all_DDMMYYYY.csv`, via `jugaad-data`'s
`NSEIndicesArchives.bhavcopy_index_save`) carries three valuation columns that
the parser currently discards:

```
Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,
Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),
P/E,P/B,Div Yield
```

Real rows verified live (2013-01-02, 2016-10-06, 2020-08-20, 2026-10-09) carry
all three columns; the header is stable across those years. Files before
~2013 return HTTP 404, and the local DB starts at 2016-10-06 anyway.

```
S&P CNX Nifty,02-01-2013,...,4992.9,18.96,3.18,1.38
Nifty 50,06-10-2016,...,8114.93,23.52,3.3,1.27
Nifty 50,20-08-2020,...,27676.18,31.91,3.17,1.49
Nifty 50,09-10-2026,...,26129.26,19.27,2.77,1.52
```

There is also a **separate** niftyindices.com dataset — "P/E, P/B & Div.Yield
values" (`/BackPage/getpepbHistoricaldataDBtoString`, wrapped by `jugaad-data`
as `NSEIndexHistory.index_pe_raw(symbol, from, to)`) returning
`{'Index Name', 'pe', 'pb', 'divYield', 'DATE'}`. Its values are **identical**
to the bhavcopy's (Nifty 50, 2026-10-09: `19.27 / 2.77 / 1.52`). It is the same
underlying data exposed as a range download, so the bhavcopy is kept as the
single source and no second ingest path is added.

## Key decisions

1. **Source = existing index bhavcopy.** No new download path; the columns are
   already in the ingested file. The separate P/E/P/B/Div.Yield API is
   identical data and is not used.
2. **Storage = extend `index_price_history`.** One row per
   `(index, trade_date)` already exists; add three nullable `DecimalField`s.
   No new table, no join.
3. **Refreshes touch only valuation columns.** OHLC etc. are left untouched on
   backfill; the existing unique key `(index, trade_date)` is the upsert target.
4. **Reuse the existing command** with a new `--refresh-valuations` mode rather
   than a second command, so holiday detection, retries, throttling, and
   parsing stay single-sourced. New daily ingests populate the columns with no
   cron change.

## Schema change: `index_price_history`

Add three nullable columns:

| Field | Type |
|---|---|
| `pe_ratio` | `DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)` |
| `pb_ratio` | `DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)` |
| `div_yield` | `DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)` |

`Div Yield` is stored as the source percentage value (e.g. `1.52`).
`-`/`NA`/blank map to `NULL` via the existing `_dec()`/`_clean()` helpers.
Generate `indexpricehistory/migrations/0002_*.py` via
`makemigrations indexpricehistory`; no data migration (defaults NULL).

## Ingest changes (`ingest_index_bhavcopy.py`)

- `_parse_row` (~line 458): add `pe_ratio`, `pb_ratio`, `div_yield` reading
  `P/E`, `P/B`, `Div Yield` through `_dec()`. Missing/renamed columns degrade
  to `NULL` (parser uses `raw.get()`), so older files are safe.
- `_ingest_file` (~line 499): pass the three fields into each
  `IndexPriceHistory`.
- Normal daily cron is unchanged; re-runs stay no-ops via `ignore_conflicts`.

## New mode: `--refresh-valuations` (one-time backfill)

Files are deleted after ingest and the command skips already-ingested
filenames, so history can only be filled by re-downloading.

- Add a `--refresh-valuations` flag usable with `--from/--to` or `--days`.
- In `_run_dates`, when set and a date is already ingested: do **not** skip;
  download the file (bypass the `_already_ingested` short-circuit and
  `skip_if_present`), parse, and upsert only the valuation fields for
  `(index, trade_date)` via
  `bulk_create(..., update_conflicts=True, update_fields=["pe_ratio","pb_ratio","div_yield"], unique_fields=["index","trade_date"])`,
  or `bulk_update` on existing pks plus `ignore_conflicts` create for gaps.
- Reuse the existing `BhavcopyFile` row (unique, and PROTECTed by price rows);
  do **not** create a second one.
- Dates never ingested before fall back to the normal full-ingest path.
- Count inserted/updated via `count()` before/after (SQLite gives no per-row
  conflict feedback).

Backfill procedure:

```
python manage.py ingest_index_bhavcopy --from 2016-10-06 --to 2026-10-09 \
    --refresh-valuations --delay 2 --retries 3
```

~2,391 files at ~2s each ⇒ roughly 1–2 hours; run detached with
`nohup ... > logs/backfill_index_pe.log 2>&1`. Verify afterward with
`COUNT(*) WHERE pe_ratio IS NOT NULL` and a spot-check of NIFTY 50.

## Tests (`indexpricehistory/tests.py`)

- Extend `test_parse_rows_maps_real_header` to assert `pe_ratio=22.36`,
  `pb_ratio=3.49`, `div_yield=1.31`.
- Add parse test: missing `P/E`/`P/B`/`Div Yield` → `None`.
- Add refresh test: ingest a synthetic file, then refresh with changed
  valuations; assert values updated, row count unchanged, no duplicate
  `BhavcopyFile`.
- Add model test for the new fields (mirror existing model tests).

## Docs

- `spec/schema.md` §3.7: add the three columns to the DDL + mapping notes.
- `indexpricehistory/AGENTS.md`: parser maps P/E/P/B/Div Yield; document
  `--refresh-valuations`.
- Root `AGENTS.md`: index CLI note + command example.
- `docs/DEPLOYMENT.md`: new columns + backfill command.
- `spec/AGENTS.md`: add this plan to the file table.

## Risks / open items

- **Historical header drift:** older bhavcopies may name or omit columns;
  `.get()` → `NULL` degrades safely. Sample one pre-2018 file at backfill start.
- **Read API (Go):** adds nullable columns (additive); confirm no breaking
  `SELECT *` assumption and whether the API should expose the new fields.
- **Download reliability:** the backfill re-fetches ~10 years of files;
  retries/`--delay` handle rate limiting.

## Assumptions

- Source stays the bhavcopy (confirmed); no use of the separate P/E API.
- All three metrics are stored.
- Storage extends `index_price_history` (no new table).
- Full history 2016-10-06 → present is backfilled.
