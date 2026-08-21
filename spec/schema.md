# Database Schema: Stock Info + Price History (SQLite)

Applies to bhavcopy data for the **CM (Capital Market) segment, `instrument_type = STK`**
only. Other segments and instrument types (FO contracts, indices, debt) are deferred and
listed under [Future extensions](#7-future-extensions).

Companion doc: [`bhavcopy-format.md`](bhavcopy-format.md) (column-level spec of the two
bhavcopy formats). Store all dates as ISO `YYYY-MM-DD` TEXT and all money as DECIMAL
(Django `DecimalField` stores DECIMAL as TEXT in SQLite, preserving precision; never use
REAL for prices/turnover).

---

## 1. Design decisions (agreed)

1. **ISIN is the global instrument identity**, shared across NSE/BSE. `instruments` is
   keyed by ISIN; the per-exchange ticker lives in `instrument_tickers`.
   Legacy bhavcopy files have no ISIN, so ISIN is nullable and identity falls back to
   `(source, ticker)`; UDiFF rows upgrade the record with ISIN.
2. **Separate price table per segment.** `cm_price_history` now; FO/debt tables later.
   Keeps each table's columns honest (no expiry/strike/OI columns in CM).
3. **Series is a day-level attribute**, a FK to a `series` lookup table (seeded from the
   NSE Legend of Series). Same ISIN can appear in multiple series rows on the same day
   (e.g. `EQ` + `BL`), so the price key is `(instrument_ticker, trade_date, series)`.
4. **No delivery columns** — `DELIV_QTY`/`DELIV_PER` do not exist in the UDiFF format
   (delivery data left the bhavcopy in the 2024 transition), and `DELIV_PER` is derivable
   anyway.
5. **Ingest provenance**: every price row references `bhavcopy_files`, making ingestion
   idempotent (re-import by filename is a no-op).
6. **Units and dates normalized at ingest**: turnover always in full rupees (legacy
   `TURNOVER_LACS` × 1e5); dates `DD-MMM-YYYY` → ISO; `-` / empty → NULL.
7. **Columns with no UDiFF value are dropped** — legacy-only fields (`AVG_PRICE`,
   `DELIV_QTY`, `DELIV_PER`) have no UDiFF equivalent and are not stored; `avg_price`
   is derivable as turnover / volume when needed.
8. **`fin_instrm_id` (FinInstrmId) is exchange-specific** (NSE internal code; BSE has its
   own scrip code), so it lives on `instrument_tickers`, not `instruments`.
9. If a ticker changes (symbol change on a corporate action), **update the ticker in
   place** on `instrument_tickers`; price rows are unaffected because they reference the
   row id, and history continuity is preserved via ISIN.
10. **Corporate actions stored as events, not adjustment factors.** NSE publishes only the
    raw events (ex-date + free-text purpose like "Bonus 2:1"); adjustment factors are
    computed at query time using NSE's formulas (bonus `(A+B)/B`, split `A/B`, rights
    `(P−E)/P`). Deferred to future scope (table defined in §6).

---

## 2. Entity relationship

```
series 1 ──── * cm_price_history * ──── 1 instrument_tickers * ──── 1 instruments
                        │
                        └────── 1 bhavcopy_files
```

`cm_price_history` references `instrument_tickers` (not `instruments`) because a price
row is inherently from one exchange; the FK captures which exchange's price it is.

---

## 3. Tables

### 3.1 `series` — lookup dictionary (seeded from NSE Legend of Series)

```sql
CREATE TABLE series (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,      -- EQ, BE, BL, BZ, GB, GS, SM, ST, N1..NZ, ...
    description TEXT NOT NULL              -- e.g. 'Fully paid equity shares / ETFs'
);
```

Seed values (from `https://www.nseindia.com/static/market-data/legend-of-series`):

| code | description |
|---|---|
| `EQ` | Fully paid equity shares / ETFs (rolling settlement) |
| `BE` | Trade-for-trade (surveillance); also Rights Entitlement |
| `BZ` | Trade-for-trade Z category (listing non-compliance) |
| `SM` | Fully paid equity SME (rolling settlement) |
| `ST` / `SZ` | SME trade-for-trade / Z category |
| `E1–E9, EA–EZ` (`E@`) | Partly paid equity shares |
| `MF` / `ME` | Mutual fund units (close-ended, listed after 11-Dec-2008) / trade-for-trade |
| `P@` / `O@` | Non-convertible preference shares / trade-for-trade |
| `Q@` / `F@` | Fully convertible preference shares / trade-for-trade |
| `N@, Y@, Z@, A@, B@` | Non-convertible debt instruments (rolling) |
| `@@, U@, M@` | Non-convertible debt (trade-for-trade) |
| `D@` / `S@` | Fully convertible debt instruments |
| `W@` / `K@` | Convertible warrants |
| `IV` / `ID` | Units of InvITs |
| `GB` | Gold Bonds |
| `GS` | Government Securities |
| `RR` / `RT` | Units of REITs |
| `SG` | State Development Loans |
| `TB` | Treasury Bills |
| `BO` | Buyback of equity shares through exchange route |
| `BL` | Block deals (gross settlement, no netting) |

(`@` expands over `0-9, A-Z`.) Only the codes actually observed in ingested files need
to be present; the full legend is the reference.

### 3.2 `instruments` — global security identity

```sql
CREATE TABLE instruments (
    id              INTEGER PRIMARY KEY,
    isin            TEXT UNIQUE,           -- global key across NSE/BSE; NULL if unknown
    name            TEXT,                  -- FinInstrmNm (company name), UDiFF only
    instrument_type TEXT NOT NULL DEFAULT 'STK',  -- FinInstrmTp; only 'STK' ingested now
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

- Dedupe rule: if `isin` present, upsert on ISIN; else upsert on `(source, ticker)` via
  `instrument_tickers` and leave `isin` NULL (upgrade when a later UDiFF row provides it).

### 3.3 `instrument_tickers` — per-exchange ticker and codes

```sql
CREATE TABLE instrument_tickers (
    id            INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    source        TEXT NOT NULL,           -- 'NSE' | 'BSE'
    ticker        TEXT NOT NULL,           -- SYMBOL / TckrSymb
    fin_instrm_id INTEGER,                 -- NSE FinInstrmId (exchange-specific)
    UNIQUE (instrument_id, source),
    UNIQUE (source, ticker)
);
```

This is the lookup entry point for "price history of ticker X on exchange Y".

### 3.4 `bhavcopy_files` — ingest provenance

```sql
CREATE TABLE bhavcopy_files (
    id          INTEGER PRIMARY KEY,
    file_name   TEXT NOT NULL UNIQUE,      -- e.g. cm20Aug2026bhav.csv
    source      TEXT NOT NULL,             -- 'NSE'
    segment     TEXT NOT NULL DEFAULT 'CM',
    format      TEXT NOT NULL,             -- 'legacy' | 'udiff'
    trade_date  TEXT NOT NULL,             -- ISO YYYY-MM-DD
    row_count   INTEGER,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 3.5 `cm_price_history` — daily OHLC for STK instruments

```sql
CREATE TABLE cm_price_history (
    id                    INTEGER PRIMARY KEY,
    instrument_ticker_id  INTEGER NOT NULL REFERENCES instrument_tickers(id),
    trade_date            TEXT NOT NULL,            -- ISO YYYY-MM-DD
    series_id             INTEGER NOT NULL REFERENCES series(id),
    open                  DECIMAL(18,4),
    high                  DECIMAL(18,4),
    low                   DECIMAL(18,4),
    close                 DECIMAL(18,4),
    last_price            DECIMAL(18,4),
    prev_close            DECIMAL(18,4),
    volume                INTEGER,                  -- TTL_TRD_QNTY / TtlTradgVol
    turnover              DECIMAL(22,2),            -- normalized to full rupees
    num_trades            INTEGER,                  -- NO_OF_TRADES / TtlNbOfTxsExctd
    settlement_price      DECIMAL(18,4),            -- SttlmPric, UDiFF only
    file_id               INTEGER REFERENCES bhavcopy_files(id),
    UNIQUE (instrument_ticker_id, trade_date, series_id)
);

CREATE INDEX idx_cm_price_date   ON cm_price_history(trade_date);
CREATE INDEX idx_cm_price_ticker ON cm_price_history(instrument_ticker_id);
```

Source field mapping: legacy `OPEN_PRICE`→`open`, ... `TURNOVER_LACS`×1e5→`turnover`,
`AVG_PRICE`, `DELIV_*` dropped; UDiFF `OpnPric`→`open`, ... `TtlTrfVal`→`turnover`
(already rupees).

---

## 4. Example queries

Price history for `RELIANCE` on NSE (EQ series):

```sql
SELECT p.trade_date, p.open, p.high, p.low, p.close, p.volume, p.turnover
FROM cm_price_history p
JOIN instrument_tickers t ON p.instrument_ticker_id = t.id
JOIN series s ON p.series_id = s.id
WHERE t.source = 'NSE' AND t.ticker = 'RELIANCE' AND s.code = 'EQ'
ORDER BY p.trade_date;
```

Omit the `s.code` filter to get all series (incl. `BL`, odd-lot) for that ticker.

Latest close per instrument:

```sql
SELECT t.ticker, p.trade_date, p.close
FROM cm_price_history p
JOIN instrument_tickers t ON p.instrument_ticker_id = t.id
WHERE p.trade_date = (SELECT MAX(trade_date) FROM cm_price_history);
```

---

## 5. Ingestion notes

- Only rows with `FinInstrmTp = STK` (CM files are entirely STK today; the filter is a
  guard for future mixed files) and only the CM segment are ingested.
- Parse both formats with whitespace-tolerant CSV (`skipinitialspace=True` for legacy).
- Normalize: dates → ISO; turnover → rupees; `-`/blank → NULL; `DELIV_*` ignored.
- Upsert via the unique keys: `instruments` on ISIN, `instrument_tickers` on
  `(source, ticker)`, `cm_price_history` on `(instrument_ticker_id, trade_date, series_id)`.
- Skip re-ingest when `bhavcopy_files.file_name` already exists.

---

## 6. Deferred: `corporate_actions` (events, not factors)

NSE publishes raw corporate-action events (no adjustment factors, no back-adjusted
series): `https://www.nseindia.com/api/corporates-corporateActions?index=equities`.

```sql
CREATE TABLE corporate_actions (
    id                   INTEGER PRIMARY KEY,
    isin                 TEXT,              -- when available
    ticker               TEXT NOT NULL,
    company_name         TEXT,
    series               TEXT,              -- 'EQ'
    purpose              TEXT NOT NULL,     -- free text: 'Bonus 2:1', 'Dividend - Rs 2 Per Share', ...
    face_value           DECIMAL(10,2),
    ex_date              TEXT,              -- ISO
    record_date          TEXT,
    book_closure_start   TEXT,
    book_closure_end     TEXT,
    UNIQUE (isin, purpose, ex_date)
);
```

Adjustment factors are computed at query time per NSE methodology (bonus `(A+B)/B`,
split `A/B`, rights `(P−E)/P`) when back-adjusted series are needed.

---

## 7. Future extensions

| Item | When | Notes |
|---|---|---|
| `fo_price_history` + `derivative_contracts` | Later | FO "instruments" are contracts (underlying × expiry × strike × CE/PE); separate identity model, `FUT`/`OPT` types |
| `index_prices` | Later | Indices have no ISIN; key `(source, ticker)`, e.g. `NIFTY 50` |
| Debt segment | Later | Own bhavcopy files, own table |
| `corporate_actions` ingestion + back-adjustment | Later | §6 |
| BSE sources | Later | Same schema; `source = 'BSE'`, add BSE scrip code to `instrument_tickers` |
