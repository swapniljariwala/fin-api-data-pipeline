# TODO — price/volume screener snapshot (`screener` app)

Status: lean v1 implemented 2026-11-12 (tasks 1-5, 8 done; 6 partly: dry-run + sample screen on a local copy, no Screener.in/Bhav comparison yet; 7 documented, not in live crontab; 9 open). Design agreed in conversation 2026-11-12; open items at the bottom.

## Goal

Support Screener.in-style filters on price/volume only, e.g.

```
100*((High - Price)/High) < 10 AND 100*((High - Price)/High) > 0 AND
100*(Price/Low - 1) > 100 AND Price > 10 AND Volume > 100000
```

"High"/"Low" = 52-week high/low, "Price" = latest close, "Volume" = latest day's volume.
The Go API (separate repo) turns a filter into SQL; this repo only computes and stores the metrics.

## Design decisions

- **One wide table, one row per instrument** (`screener_snapshot`). Not key/value rows,
  not one row per date. ~2.5k rows, so a full scan per query is sub-millisecond; no
  per-column indexes needed.
- **Only base metrics are columns. Derived ratios are not stored.** `% below 52w high` and
  `price / 52w low` are computed in the API from `close`, `high_52w`, `low_52w` (named
  "virtual metrics" expanded to SQL expressions). Store a value only when it needs a window
  computation (ranges, returns, averages, moving averages).
- **Adjusted prices.** `nse_cm_price_history` holds raw prices, so everything is computed on
  `price * COALESCE(factor, 1)` via `price_adjustment_segments` (join documented in
  `corporateactions/models.py` `PriceAdjustmentSegment`); volume is divided by the same
  factor. Otherwise a split looks like a crash in the 52-week high.
- **Series:** EQ only; fall back to the instrument's primary series (BE/SM...) when it has
  no EQ row.
- **Latest snapshot only** (no history by date) for now.
- **No date columns** (52w high/low dates dropped), **no all-time high/low** (history is
  too short). Recency filters, if wanted later, get one `days_since_high_52w` column.
- **Returns are absolute price returns** (point-to-point `100*(close/close_N_ago - 1)`,
  adjusted, dividends excluded). CAGR for 3y/5y is derived in the API:
  `100*((1+ret/100)^(1/years)-1)`. SQLite `pow()` needs a math-enabled build; if the Go
  driver lacks it, compute in the API or store `cagr_3y`/`cagr_5y` columns.
- NULL in any window column (history shorter than the window) means the stock fails that
  filter.

## Columns (44 full, 34 lean)

| Group | Columns | # |
|---|---|---|
| Key / meta | `instrument` (PK, FK), `as_of_date`, `trading_days_available`, `built_at`, `adjustment_pending` | 5 |
| Latest day | `close`, `open`, `day_high`, `day_low`, `prev_close`, `volume`, `turnover`, `num_trades` | 8 |
| 52-week range | `high_52w`, `low_52w` | 2 |
| Returns | `ret_1d`, `ret_1w`, `ret_1m`, `ret_3m`, `ret_6m`, `ret_1y`, `ret_3y`, `ret_5y` | 8 |
| Volume | `avg_vol_10d`, `avg_vol_20d`, `avg_vol_50d`, `avg_vol_250d`, `avg_turnover_20d` | 5 |
| SMA | `sma_20`, `sma_50`, `sma_100`, `sma_200` | 4 |
| Delivery | `deliv_per`, `avg_deliv_per_20d` (only the last 365 days exist) | 2 |
| *Optional: short ranges* | `high_1m/3m/6m`, `low_1m/3m/6m` | 6 |
| *Optional: indicators* | `ema_20`, `ema_50`, `rsi_14`, `atr_14` | 4 |

Lean v1 = everything except the two optional groups. Adding a metric later is one nullable
column plus one line in the compute code.

`adjustment_pending` = the instrument has a `needs_review` split/bonus (not applied), so its
ranges may be distorted.

## Tasks

1. [ ] Check `nse_cm_price_history` depth on a local DB copy (earliest `trade_date`);
       drop `ret_5y`/`ret_3y` if the history is too short.
2. [ ] Create app `screener`: `models.py` (`ScreenerSnapshot`, table `screener_snapshot`),
       migration, register in `INSTALLED_APPS` and the logging config in `pipeline/settings.py`.
3. [ ] `screener/metrics.py`: pure compute module. Read the last ~260 trading days (more if
       `ret_3y`/`ret_5y` kept) of EQ rows joined to `price_adjustment_segments`; compute the
       columns above per instrument.
4. [ ] `build_screener_snapshot [--dry-run] [--full]` management command, thin like
       `rebuild_price_adjustments`: write only changed rows, in chunks of 500 inside
       `transaction.atomic`; idempotent (a second run writes 0 rows).
5. [ ] Tests (pattern: `dailypricehistory/test_delivery.py`; `dailypricehistory/tests.py` is
       stale): split inside the window, <52 weeks of history, missing days, series fallback,
       `needs_review` flag.
6. [ ] Verify: `--dry-run` on a local copy, spot-check ~5 symbols (one recent split, one new
       listing) against the Bhav MCP `stock_price_history`; run the sample screen as SQL and
       compare a few results with Screener.in.
7. [ ] Cron (`docs/DEPLOYMENT.md`): `flock`-wrapped, captured output. Run nightly after the
       evening ingest window (16:00–20:59 IST), e.g. ~21:05, clear of the 21:30 shareholding
       job and the 05:30–05:45 Litestream snapshot. Also re-run after
       `rebuild_price_adjustments` (corporate actions run 09:12 IST), since a new split
       changes the adjusted history. Confirm the live crontab chains it.
8. [ ] Docs: `screener/AGENTS.md`, root `AGENTS.md` (folder index + CLI reference),
       `spec/AGENTS.md`.
9. [ ] Hand the Go API owner the column list and the virtual-metric expressions.

## Open decisions

- Keep history by date (add `as_of_date` to the PK, ~2.5k rows/day) or latest only?
- Include the optional short ranges and indicators in v1, or start lean?
- CAGR: derive in the API, or store `cagr_3y`/`cagr_5y`?
