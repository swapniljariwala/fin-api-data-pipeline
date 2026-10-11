# AGENTS.md — `spec/`

Design documentation. These are the source of truth for the schema and data formats;
implementation code should follow them.

## Files

| File | What it is |
|---|---|
| `schema.md` | SQLite database schema for stock info + price history: design decisions, entity relationship, DDL for `series`, `instruments`, `instrument_tickers`, `bhavcopy_files`, `cm_price_history`, example queries. Applies to CM segment, `STK` instruments only. |
| `bhavcopy-format.md` | NSE bhavcopy file formats: legacy (15 cols, pre-8-Jul-2024) vs UDiFF (34 cols, from 8-Jul-2024), column specs, data conventions, and the field mapping between the two. |
| `corporate-announcements-plan.md` | Proposed `corporateannouncements` app: `AnnouncementCategory`/`CorporateAnnouncement` models sourced from `jugaad-data`'s `NSELive.corporate_announcements()`, plus a 5-sprint implementation plan (models → ingest → CLI/logging → docs → verification). Not yet implemented. |
| `mf-nav-plan.md` | Implemented 2026-10-10: `mfnavhistory` app with `MutualFundAmc`/`MutualFundCategory`/`MutualFundScheme`/`MutualFundNavHistory` sourced from `jugaad-data`'s `amfi` `nav_history_raw()`, the `ingest_mf_nav` command, and a 10-year backfill. Implements `schema.md` §3.10–3.11. |
| `index-pe-plan.md` | Implemented 2026-10-11: captures `P/E`/`P/B`/`Div Yield` already present in the daily NIFTY indices bhavcopy. Extends `index_price_history` with three nullable columns (`pe_ratio`/`pb_ratio`/`div_yield`), adds a `--refresh-valuations` mode to `ingest_index_bhavcopy` for a 2016→present backfill, plus tests and docs. The separate niftyindices P/E/P/B/Div.Yield API is identical data and is not used. |
