# Integrated filings — handoff for the fin-api (Go) service

What the pipeline publishes and how to read it. Written 2026-11-08. Read-only for fin-api.

## Terms
- **Filing** = one NSE submission = one row in `integrated_filings`, identified by NSE's `seq_id`.
  `type_sub` is `Original` or `Revision`. A company can have several filings for the same quarter
  (an Original and Revisions); **all of them are kept**.
- **Group** = `(instrument_id, period_end, consolidated)` = one company, one quarter, standalone or consolidated.
- **Latest** = the newest filing of a group that is `done` and has statements (defined below).

## Tables (SQLite, read-only)
- `integrated_filings`: `id`, `instrument_id` (FK `instruments`), `seq_id` (unique), `filing_type`
  (only `Integrated Filing- Financials` has statements), `type_sub`, `period_end` (quarter end),
  `consolidated` (1 consolidated / 0 standalone), `audited`, `broadcast_at` (IST-aware; for a Revision it
  comes from `revised_at` because NSE sends no broadcast date), `revised_at`, `revision_remark`,
  `company_name`, `xbrl_url`, `extraction_status` (`pending` / `done` / `failed`), `extraction_error`,
  `extracted_at`, `raw` (NSE record as JSON; `raw->>'symbol'` is the symbol at filing time).
- `integrated_filing_statements`: one row per statement per filing: `filing_id`, `statement` (e.g.
  `profit_and_loss`, `balance_sheet`, `cash_flow`, `segment_information`, `related_party_transactions`),
  `role_code`, `description`, `periods` (JSON list such as `["Q2","H1"]` or `["as_of 2026-09-30"]`),
  `item_count`, `row_count`, `object_path`, `size_bytes`. Unique on `(filing_id, role_code)`.
- Data starts **April 2025** (NSE has no Integrated Filing before that). A historical backfill is still
  running, so older quarters fill in over time.

## Rules for the tools
1. **Listing returns every filing** (Originals, Revisions, and ones that are `pending` / `failed`).
   Show `seq_id`, `type_sub`, `broadcast_at`, `extraction_status` and an `is_latest` flag. Only `done`
   filings have statements.
2. **Get a statement.**
   - **With `seq_id`**: that exact filing.
     `select s.object_path from integrated_filing_statements s join integrated_filings f on f.id=s.filing_id where f.seq_id=? and s.statement=?`
   - **Without `seq_id`**: the latest filing of the group:
     ```sql
     select f.* from integrated_filings f
     where f.instrument_id=? and f.period_end=? and f.consolidated=?
       and f.filing_type='Integrated Filing- Financials' and f.extraction_status='done'
       and exists (select 1 from integrated_filing_statements s where s.filing_id=f.id)
     order by f.broadcast_at desc, f.seq_id desc limit 1;
     ```
     A newer filing that failed (for example NSE returns 404 for its file) is skipped, so "latest" is
     the newest filing that has data. For the `is_latest` flag over all groups use the query in
     `integratedfilings/AGENTS.md`.
3. **Use `object_path` exactly as stored.** Never rebuild it from the symbol. Target layout is
   `filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/{statement}.json`. A re-key of older filings is in
   progress, so some `object_path` values may still be the old shared-folder form
   (`filings/{symbol}/{fy}/{quarter}/{basis}/{statement}.json`); both work if you read what the table says.
4. **Join on `instrument_id`**, not on symbol. In the master tables 509 symbols map to more than one
   instrument and 210 instruments have more than one symbol; filings always point at one instrument.
   To find a company by its current symbol go through `nse_symbols` / `nse_symbols_instrument_map` to
   `instrument_id`. One instrument (NAHARENTP) has no ISIN yet.
5. **Quarter to `period_end`** (Indian April-March convention, what the paths use): FY2027 Q1 = 2026-06-30,
   Q2 = 2026-09-30, Q3 = 2026-12-31, Q4 = 2027-03-31.
6. **Discover statements per filing** from `integrated_filing_statements`. Do not assume a balance sheet
   or cash flow exists for Mar/Sep and not for Jun/Dec: they follow each company's own half-years, so
   companies with a calendar or Jul-Jun year file them at other quarter-ends. Most Q1/Q3 filings have none.
   For those companies the path `fy`/`quarter` (Indian convention) differs from their own fiscal quarter.
7. **Null means "not reported", never zero.**

## Statement JSON
Top level: `company`, `report`, `units` (a plain-English legend), `source` (seq_id, type, URL),
`statement`, `role_code`, `entries`. Each entry is one period: `period` (`label` Q1..Q4 / H1 / 9M / FY with
`start`/`end`, or `as_of` for a balance) and `data` (a nested tree: each line is `{"value": n}` plus
`breakup` / `less` / `formula`), and where they apply `rows` (segments, related-party transactions...),
`opening_balance`, `closing_balance` (cash flow).
- Return the tree **as-is**; money is in absolute rupees, EPS in rupees per share, ratios are fractions.
- Balance sheet and cash flow have a stable top-level key set per format
  (Ind AS / NBFC / other_than_bank: `Assets`, `EquityAndLiabilities`; banks: `CapitalAndLiabilities`, `Assets`;
  insurers: `SourcesOfFunds`, `ApplicationOfFunds`; cash flow: the same 6 keys everywhere). **P&L varies**
  (Ind AS has 13 distinct key sets), so do not hard-code its keys.
- Insurer P&L has extra `as_of` entries (investor complaints, balances): select the entries that have
  `period.label`.
- A Q2 P&L has Q2 and H1, Q4 has Q4 and FY: both periods are in the one object; trim to one if asked.

## Size
Core statements are small (P&L ~6 KB, balance sheet ~3 KB, cash flow ~8 KB; the largest non-related-party
object seen is 134 KB). **`related_party_transactions` is large** (median ~90 KB, max 2.8 MB on prod):
page it by rows (50-100) and offer a counterparty filter.

## `canonical.json`
Next to the statements: ~12 headline numbers (net profit, EPS, total assets...), same names in every format.
Nulls are mostly legitimate: no balance sheet in Q1/Q3, no owners/minority split in standalone filings,
banks and insurers file no single equity or revenue-from-operations line. Use the statements when you
need those.

## Known gaps (not bugs on your side)
- 8 filings are HTTP 404 and 1 is empty on NSE's own archive, so they stay `failed`.
- 7 insurer filings use a new taxonomy (`insurance_indas`) the parser does not support yet (GODIGIT, NIVABUPA,
  STARHEALTH). They stay `failed` until the library is updated.
- Bucket: `gs://bhav-marketsetup-db-replication`, prefixes `filings/` and `raw/`. The bucket also holds
  `secrets/`, `bhavdb/` etc.; grant read on the two prefixes only if you can.
