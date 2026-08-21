# AGENTS.md — `spec/`

Design documentation. These are the source of truth for the schema and data formats;
implementation code should follow them.

## Files

| File | What it is |
|---|---|
| `schema.md` | SQLite database schema for stock info + price history: design decisions, entity relationship, DDL for `series`, `instruments`, `instrument_tickers`, `bhavcopy_files`, `cm_price_history`, example queries. Applies to CM segment, `STK` instruments only. |
| `bhavcopy-format.md` | NSE bhavcopy file formats: legacy (15 cols, pre-8-Jul-2024) vs UDiFF (34 cols, from 8-Jul-2024), column specs, data conventions, and the field mapping between the two. |
