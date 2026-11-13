# AGENTS.md — `screener/`

Django app holding the **screener snapshot**: one row per instrument with the latest
price/volume metrics, so the Go API can run Screener.in-style filters (e.g.
`100*((high_52w - close)/high_52w) < 10 AND volume > 100000`) as plain SQL. This repo only
computes and stores the base metrics; derived ratios (`% below 52w high`, `price / 52w low`,
CAGR from `ret_3y`/`ret_5y`) are expressions the API builds over these columns.
Design record: [`spec/TODO-screener-snapshot.md`](../spec/TODO-screener-snapshot.md).

## Files

| File | What it is |
|---|---|
| `models.py` | `ScreenerSnapshot` (table `screener_snapshot`, PK = `instrument_id`, 34 columns). |
| `metrics.py` | Pure `compute(bars)` → metrics dict for the latest bar. No DB access. |
| `builder.py` | Reads the last ~5 years of adjusted prices, calls `compute`, writes only changed rows (`rebuild(full, dry_run)`). |
| `management/commands/build_screener_snapshot.py` | `build_screener_snapshot [--dry-run] [--full]`. |
| `tests.py` | Metrics cases and builder cases (split in window, series fallback/change, new listing, delisted, `needs_review` flag, idempotency). |

## Rules

- **Adjusted prices.** `price * COALESCE(factor, 1)` via `price_adjustment_segments`
  (`corporateactions.models.PriceAdjustmentSegment`); volume is divided by the same factor.
  Turnover and trade counts are raw. Run after `rebuild_price_adjustments`.
- **Series.** One row per date: the EQ row when there is one, else the other series' row, so
  a stock moving EQ → BE/SM keeps one continuous history.
- **Active only.** Instruments whose newest price is more than 30 days older than the newest
  date in the database get no row (an existing row is deleted).
- **52-week range** = intraday high/low over the available part of the last 365 days
  (a new listing uses what it has; filter on `trading_days_available` if that matters).
- **Returns** = `100*(close/close_then - 1)`, point to point, dividends excluded.
  `close_then` is the last close on or before the calendar target (1w=7d, 1m…5y=months) and
  must be within 7 days of it, otherwise NULL (history too short or a data gap).
- **Averages / SMAs** are over the last N *rows*; NULL if fewer than N rows.
  `avg_deliv_per_20d` averages the non-null values in the last 20 rows (delivery only
  exists for the last 365 days).
- **`adjustment_pending`** = a `needs_review` action that may rescale the price (split,
  bonus, consolidation, or undecoded) sits in the history window, so ranges/returns may be
  distorted. Matched through the NSE symbol.
- Idempotent: a second run writes 0 rows. Local DB copies can have sparse old history, which
  just makes `ret_3y`/`ret_5y` NULL.

## Cron

Daily at 02:00 IST (installed 2026-11-13): after the 21:12 corporate-actions job, clear of the
evening price ingests, the Sun 03:30 log rotation and the 05:30 Litestream snapshot. A full run
reads ~3M rows and took ~31 min on prod with a cold cache (disk-bound); see `docs/DEPLOYMENT.md`.
