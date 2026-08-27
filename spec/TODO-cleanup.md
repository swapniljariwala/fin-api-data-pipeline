# Cleanup — data repair after fixes

## Cleanup 1: orphaned duplicate `Instrument` rows

After Fix 1 is applied, `_resolve_instrument_by_isin` may still `get_or_create` a
second `Instrument` row (via `isin=`) when the old instrument had its ISIN updated
in-place.  The newly-created row is an orphan — its ISIN now references the (older)
updated row, but the duplicate itself has no `InstrumentTicker` links (unless one
was created before the ISIN collision happened).

**Action:** Write a data-repair command that finds `Instrument` rows whose ISIN is
shared with another `Instrument`, merges any tickers and price-history rows to the
surviving instrument, and deletes the duplicate.

## Cleanup 2: blank-ISIN placeholder instruments

Before Fix 1, many tickers with no ISIN in the bhavcopy created placeholder
`Instrument` rows with `isin=None`.  After Fix 1 is deployed and the backfill
re-runs, these placeholders will gradually get their ISINs filled in.  Any that
remain (genuine missing-ISIN entries) could be cleaned up or flagged for manual
review.

**Action:** After the backfill completes, run a query to find `Instrument` rows
with `isin IS NULL` that have no price history — these are true orphans and can be
deleted.

## Cleanup 3: merge instruments with duplicate ISINs

The `20MICRONS` case (`INE144J01019` vs `INE144J01027`) and similar warnings in
the log show the same ticker mapped to two different `Instrument` rows, each with a
valid ISIN.  After Fix 1, one of them will be updated to the other's ISIN, but the
duplicate row remains.

**Action:** A general-purpose duplicate-instrument merger that:
1. Finds `Instrument` rows sharing the same ISIN.
2. Picks the oldest (or the one with more tickers) as the survivor.
3. Re-parents all `InstrumentTicker` rows to the survivor.
4. Deletes the duplicate.