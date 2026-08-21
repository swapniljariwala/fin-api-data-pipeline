# NSE Bhavcopy File Format Specification

This document describes the two bhavcopy (end-of-day price) file formats found in `data/`.
The two formats correspond to a regulatory-mandated format change that took effect on
**8-Jul-2024** (SEBI's **UDiFF** standard, "Unified Distilled File Formats").

| File | Date | Format |
|---|---|---|
| `data/cm05Jul2024bhav.csv` | 05-Jul-2024 | Legacy ("standardized") bhavcopy, **15 columns** |
| `data/cm20Aug2026bhav.csv` | 20-Aug-2026 | UDiFF bhavcopy, **34 columns** |

Both files cover the **CM (Capital Market / cash equities)** segment only.
Reference circulars: NSE/MSD/59694 (11-Dec-2023), NSE/MSD/62142 (22-May-2024);
NSE UDiFF guidance doc (Ver 1.0); official spec spreadsheet
"Proposed UDiFF Bhavcopy file formats.xlsx" (via https://www.nseindia.com/static/resources/forms-formats-members).

---

## 1. Legacy format (`cm05Jul2024bhav.csv`)

Produced by NSE until 5-Jul-2024 (in parallel with UDiFF from Dec-2023). One row per
security per day. Header row is present.

### 1.1 Columns

| # | Column | Type | Meaning |
|---|---|---|---|
| 1 | `SYMBOL` | str | Ticker symbol (e.g. `RELIANCE`, `1018GS2026`) |
| 2 | `SERIES` | str | Trading series: `EQ` (rolling settlement), `BE` (trade-to-trade), `SM` (SME), `ST` (SLB), `GB` (Sovereign Gold Bonds), `GS` (Government Securities), `BZ`, etc. |
| 3 | `DATE1` | date `DD-MMM-YYYY` | Trade date |
| 4 | `PREV_CLOSE` | float | Previous session close (restated on ex-corporate-action dates) |
| 5 | `OPEN_PRICE` | float | First traded price (pre-open call auction) |
| 6 | `HIGH_PRICE` | float | Session high |
| 7 | `LOW_PRICE` | float | Session low |
| 8 | `LAST_PRICE` | float | Last traded price (not necessarily the official close) |
| 9 | `CLOSE_PRICE` | float | Official closing price (VWAP of closing-window trades; LTP if none) |
| 10 | `AVG_PRICE` | float | Session VWAP = turnover / quantity |
| 11 | `TTL_TRD_QNTY` | int | Total shares traded |
| 12 | `TURNOVER_LACS` | float | Turnover in **₹ lakhs** |
| 13 | `NO_OF_TRADES` | int | Number of trades executed |
| 14 | `DELIV_QTY` | int or `-` | Deliverable quantity (shares moved to demat delivery) |
| 15 | `DELIV_PER` | float or `-` | Delivery percentage = DELIV_QTY / TTL_TRD_QNTY × 100 |

### 1.2 Data conventions observed in `cm05Jul2024bhav.csv`

- Header and every field carry a **leading space** (` SYMBOL, SERIES, ...`); parse with
  whitespace-tolerant CSV handling (e.g. `csv.reader` with `skipinitialspace=True`).
- Prices are 2-decimal floats; quantities/trades are integers (unquoted).
- Missing delivery data is encoded as `-` in `DELIV_QTY` and `DELIV_PER`
  (e.g. series `BE`, `GS` rows).
- Dates use `DD-MMM-YYYY` (e.g. `05-Jul-2024`).
- No ISIN, no sector/name fields, no segment column (file is inherently CM-only).

### 1.3 Example row

```
1018GS2026, GS, 05-Jul-2024, 115.00, 115.00, 115.00, 115.00, 115.00, 115.00, 115.00, 5002, 5.75, 2, 5002, 100.00
```

---

## 2. UDiFF format (`cm20Aug2026bhav.csv`)

SEBI-standardized format produced by all exchanges since **8-Jul-2024**. Single format
covers CM, FO, CD and COM segments; the CM file is one row per security per day.
Header row is present. Download URL pattern:
`https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`.

### 2.1 Columns (34)

| # | Column | Type | Meaning |
|---|---|---|---|
| 1 | `TradDt` | date `YYYY-MM-DD` | Trade date |
| 2 | `BizDt` | date `YYYY-MM-DD` | Business date covered (normally = TradDt) |
| 3 | `Sgmt` | str | Segment: `CM`, `FO`, `CD`, `COM`, ... |
| 4 | `Src` | str | Data source, e.g. `NSE` |
| 5 | `FinInstrmTp` | str | Instrument type: `STK`, `FUT`, `OPT`, `IDX`, `MF`, `BOND`, ... |
| 6 | `FinInstrmId` | int | Exchange-internal instrument/security code |
| 7 | `ISIN` | str | ISIN of the security (blank for indices) |
| 8 | `TckrSymb` | str | Ticker symbol (e.g. `ZYDUSWELL`, `SGBJUN28`) |
| 9 | `SctySrs` | str | Security series: `EQ`, `BE`, `BZ`, `GB`, `SM`, `ST`, etc. |
| 10 | `XpryDt` | date or blank | Derivative contract expiry (blank in CM) |
| 11 | `FininstrmActlXpryDt` | date or blank | Actual instrument expiry (blank in CM) |
| 12 | `StrkPric` | float or blank | Strike price, options only (blank in CM) |
| 13 | `OptnTp` | str or blank | Option type `CE`/`PE` (blank for non-options) |
| 14 | `FinInstrmNm` | str | Instrument name; for CM, the company name (e.g. `ZYDUS WELLNESS LIMITED`) |
| 15 | `OpnPric` | float | Opening price |
| 16 | `HghPric` | float | Session high |
| 17 | `LwPric` | float | Session low |
| 18 | `ClsPric` | float | Official closing price |
| 19 | `LastPric` | float | Last traded price |
| 20 | `PrvsClsgPric` | float | Previous closing price |
| 21 | `UndrlygPric` | float or blank | Underlying price (derivatives; blank in CM) |
| 22 | `SttlmPric` | float or blank | Settlement price (populated in CM) |
| 23 | `OpnIntrst` | int or blank | Open interest (derivatives; blank in CM) |
| 24 | `ChngInOpnIntrst` | int or blank | Change in open interest (blank in CM) |
| 25 | `TtlTradgVol` | int | Total traded volume (shares/contracts) |
| 26 | `TtlTrfVal` | float | Total traded value in ₹ (full rupees, not lakhs) |
| 27 | `TtlNbOfTxsExctd` | int | Number of trades executed |
| 28 | `SsnId` | str | Session ID, `F1` = normal full session |
| 29 | `NewBrdLotQty` | int | New board lot quantity (1 in CM equity rows) |
| 30 | `Rmks` | str or blank | Remarks (blank in CM) |
| 31–34 | `Rsvd1`–`Rsvd4` | blank | Reserved for future use |

### 2.2 Data conventions observed in `cm20Aug2026bhav.csv`

- Clean CSV; header row present with no leading spaces.
- Dates in ISO `YYYY-MM-DD` (`2026-08-20`).
- Missing/not-applicable fields are **empty strings**, never `-` or `NA`.
- `TtlTrfVal` is in full rupees (e.g. `71015872.85`), unlike legacy `TURNOVER_LACS`.
- `SsnId` is always `F1` in this file; `FinInstrmTp` is always `STK`; segment is `CM`,
  source is `NSE`.
- `SctySrs` in this file spans many series (`EQ`, `BE`, `BZ`, `GB`, `GS`, `SM`, `ST`,
  `MF`, plus dozens of odd-lot series `N0`–`NZ`, `Y*`, `Z*`, `P1`, `RR`, `TB`, `IV`,
  `BL`, `BS`, `E1`, `SG`, `SZ`, `SA`... ) — do not assume only `EQ`.
- Same security can appear in multiple series rows for the same day (e.g. normal + odd-lot).

### 2.3 Example row

```
2026-08-20,2026-08-20,CM,NSE,STK,17635,INE768C01028,ZYDUSWELL,EQ,,,,,ZYDUS WELLNESS LIMITED,493.00,505.70,493.00,501.30,505.00,492.70,,501.35,,,141793,71015872.85,11917,F1,1,,,,,
```

---

## 3. Field mapping legacy → UDiFF (for a unified DB schema)

| Legacy | UDiFF | Notes |
|---|---|---|
| `SYMBOL` | `TckrSymb` | |
| `SERIES` | `SctySrs` | |
| `DATE1` | `TradDt` / `BizDt` | parse `DD-MMM-YYYY` → ISO |
| `PREV_CLOSE` | `PrvsClsgPric` | |
| `OPEN_PRICE` | `OpnPric` | |
| `HIGH_PRICE` | `HghPric` | |
| `LOW_PRICE` | `LwPric` | |
| `LAST_PRICE` | `LastPric` | |
| `CLOSE_PRICE` | `ClsPric` | |
| `AVG_PRICE` | — (derivable: TtlTrfVal / TtlTradgVol) | no direct UDiFF equivalent |
| `TTL_TRD_QNTY` | `TtlTradgVol` | |
| `TURNOVER_LACS` | `TtlTrfVal` | × 1e5 to convert legacy lakhs → rupees |
| `NO_OF_TRADES` | `TtlNbOfTxsExctd` | |
| `DELIV_QTY` | — | not in UDiFF bhavcopy; delivery data moved to separate files |
| `DELIV_PER` | — | not in UDiFF bhavcopy |

UDiFF-only columns with no legacy equivalent: `Sgmt`, `Src`, `FinInstrmTp`,
`FinInstrmId`, `ISIN`, `XpryDt`, `FininstrmActlXpryDt`, `StrkPric`, `OptnTp`,
`FinInstrmNm`, `UndrlygPric`, `SttlmPric`, `OpnIntrst`, `ChngInOpnIntrst`, `SsnId`,
`NewBrdLotQty`, `Rmks`, `Rsvd1`–`Rsvd4`.

---

## 4. Sources

- NSE Forms & Formats / UDiFF page: https://www.nseindia.com/static/resources/forms-formats-members
- UDiFF bhavcopy format spec (xlsx): `Proposed UDiFF Bhavcopy file formats.xlsx` on the above page
- UDiFF guidance document (Ver 1.0, Dec-2023) and UDiFF Catalogue (xlsx)
- SEBI press release PR No. 37/2024 (26-Sep-2024) on UDiFF
- BSE notice 20240610-33 (old formats discontinued 8-Jul-2024)
- Sample NSE UDiFF CM bhavcopy files (e.g. `nse-cm-bhavcopy-2024-07-25.csv`)
