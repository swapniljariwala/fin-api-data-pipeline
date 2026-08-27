# Fixes — `ingest_bhavcopy`

## Fix 1: `_maybe_upgrade_link` — update instrument ISIN in-place

**Problem:** When a ticker row exists pointing to an ISIN-less placeholder
`Instrument`, and a later bhavcopy supplies the ISIN, the code re-points
`link.instrument` to a different `Instrument` row (found/created by
`_resolve_instrument_by_isin`). If that target instrument already has its own
`InstrumentTicker` row for the same source — due to a prior symbol change or a
placeholder-instrument merge already consumed the `(instrument, source)` slot — the
UPDATE violates `uniq_instrument_source` and the entire file's transaction rolls
back.

**Fix:** Instead of re-pointing `link.instrument`, update the existing instrument's
ISIN and name in-place.  This works for both the blank-ISIN case (placeholder gets
real ISIN) and the corporate-action case (old ISIN is replaced by new one, e.g.
stock-split generates a fresh ISIN in Indian depositories).  Also update the
`_instruments_by_isin` cache so future lookups find the same row.

**Location:** `dailypricehistory/management/commands/ingest_bhavcopy.py`,
`_maybe_upgrade_link` (line 742).

## Fix 2: delay between retry attempts

**Problem:** The download retry loop at line 357 logs the failure and
`continue`s immediately — no sleep between attempts.  This can hammer NSE's API
on transient failures.

**Fix:** Add `time.sleep(self.delay)` before `continue` in the retry loop.

**Location:** Same file, download retry loop (line 365–370).

## Fix 3: scope `update_fields` to only what changed

**Problem:** `link.save(update_fields=["fin_instrm_id", "instrument"])` always
includes both fields even when only one changed.

**Fix:** Track changed fields as a set and save only those.

**Location:** Same file, `_maybe_upgrade_link` (line 762–763).