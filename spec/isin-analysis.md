# ISIN Structure Analysis — findings from `db/isin.db` (`isin_components`, 8,083 rows)

Analysis done in support of designing corporate-action tracking (ticker changes, splits,
mergers) for the price-history pipeline. See also the BURNPUR ingestion crash
(`instrument_tickers` unique constraint) that prompted this investigation.

## ISIN structure

```
IN  E        817H         01              01           4
country  issuer_type  issuer_code   instrument_type  security_serial  check_digit
```

- `issuer_type` — legal nature of the issuer. Observed values: `E` (company — equities,
  and NBFC debt issuers), `F` (mutual fund / AMC), `0`/`1`/`2`/`3`/`4`/`9` (govt securities,
  T-Bills, and other non-corporate issuers).
- `issuer_code` — 4-char code identifying the issuing *legal entity*, assigned once by
  NSDL/CDSL.
- `instrument_type` — NOT a stable semantic "kind of security" flag. It behaves as a
  rolling registration-batch counter per issuer: numeric batches `01`-`99`, then
  alphanumeric batches `A1`, `B1`, `C1`, ... once an issuer (typically a prolific AMC)
  exhausts the numeric range. The same AMC issuer_code can have instruments spread across
  `01`, `A1`, `B1`, `C1` with no consistent category meaning attached to any one code.
  Known values seen: `01` equity/plain, `03` preference shares, `04` partly-paid equity
  (fully-paid ISINs instead use the `IN9` prefix), `07`/`08`/`09`/`10`/`11`/`A7` NCDs/bonds,
  `13` warrants, `20` rights entitlements, `23` InvIT units, `25` REIT units.
- `IN9` vs `INE` prefix — distinguishes partly-paid (`IN9`) from fully-paid (`INE`) equity
  share classes of the same company; issuer_code is shared between the two.

## Key relationships tested (no filters unless noted)

### 1. Same ISIN → multiple symbols
**0 occurrences**, across the entire table, unfiltered. ISIN is a fully reliable 1:1 key
to a symbol at any point in time, for every instrument class.

### 2. Same symbol → multiple ISINs
472 groups unfiltered (327 when restricted to `series='EQ'`). Two distinct causes:
- **Equity face-value splits / re-issuance**: same company, symbol constant, new ISIN
  with same `issuer_code`+`instrument_type` prefix and only the security_serial changing
  (e.g. `SBIN`: `INE062A01012` → `INE062A01020`). Almost always only 2-3 ISINs per symbol.
- **NCD/bond issuance**: one company legitimately issues many bond ISINs under one
  symbol-ish debt program tag (e.g. `HUDCO`, `IIFLFIN` — dozens of tranches). Not a
  corporate-action-tracking case; expected behavior for debt issuance.

### 3. Same `(issuer_code, instrument_type)` → multiple symbols
366 groups unfiltered (89 restricted to `series='EQ'`). Two very different causes:
- **AMC/fund-house issuer_codes** (`issuer_type='F'`, e.g. `109K`=ICICI Pru AMC,
  `179K`=HDFC AMC, `204K`=Nippon India MF, `732E`=Reliance MF, `769K`, `754K`, `846K`=Axis
  MF, etc.): one issuer_code + instrument_type spans **10-34+ completely unrelated fund
  schemes** (different underlying indices/asset classes). issuer_code here identifies the
  *sponsor*, not an individual product. The actual per-scheme uniqueness lives in the
  `security_serial` (e.g. under `109K/C1`: `ICICI500`→serial `CZ`, `ICICI5GSEC`→serial
  `4A`, `ICICIAUTO`→serial `Y4`).
- **Serial NCD/bond issuers** (`issuer_type='E'`, instrument_type `07`/`08`): one company
  issues many bond ISINs under distinct series codes (e.g. one issuer with instrument_type
  `07` spanning 24 symbols / 218 ISINs) — again, expected debt-issuance behavior, not
  ambiguity.

**Conclusion**: `issuer_code + instrument_type` is a safe "same security across a
corporate action" key **only** when restricted to plain listed equity
(`instrument_type='01'`, `issuer_type='E'`) **and** low symbol-cardinality (2-3 distinct
symbols/ISINs, not 10+). High cardinality is itself the tell that the issuer is an AMC or
a serial bond issuer, not a single company undergoing a corporate action.

### 4. Same symbol → multiple issuer_codes
11 groups unfiltered. All fall into two categories, distinguished by `issuer_type`:

- **`issuer_type='F'` (fund/AMC sponsor transfer)** — symbol kept constant, issuer_code
  changes because a *new legal AMC entity* took over sponsorship:
  - `BANKBEES`, `GOLDBEES`, `HNGSNGBEES`, `NIFTYBEES`, `PSUBNKBEES`: `732E` (Reliance
    Capital AMC) → `204K` (Nippon India AMC), following the 2019 Reliance→Nippon
    ownership transfer. `instrument_type` also changed `01`→`B1` — this is *not* a
    category change, just a different registration-batch slot at the new issuer_code
    (see instrument_type note above).
  - `KOTAKGOLD`: `373I` → `174K`, same fund-sponsor-transfer pattern under Kotak Mahindra
    AMC.
- **`issuer_type='E'` (NBFC/corporate merger, debt program only)** — symbol is a bond
  program nickname carried across a legal-entity merger:
  - `TATACAP`: `306N` (Tata Capital Financial Services) / `976I` (Tata Capital Housing
    Finance)
  - `L&TFINANCE`: `523E` (L&T Infra Credit) / `027E` (L&T Finance Ltd)
  - `IIFL`: `866I` (IIFL Finance) / `530B` (older IIFL group entity)
  - `SREIBNPNCD`: `881J` / `872A` (SREI Equipment Finance / SREI Infrastructure Finance)
- **`SPARC`** (Sun Pharma Advanced Research Co) initially looked like a third case but is
  **not** a real dual-issuer situation:
  - `INE232I01014` (series `EQ`, fully-paid) and `IN9232I01012` (series `E1`, partly-paid,
    `IN9` prefix) both correctly share issuer_code `232I` — this is the expected
    fully-paid/partly-paid share-class pairing, not an anomaly.
  - `IN9232101012` (issuer_code parsed as `2321`) is a transcription duplicate of
    `IN9232I01012` (`I` misread as `1`) — a data-entry/scrape artifact, not a genuine
    second ISIN.

**Filtered to `instrument_type='01'` only, the SPARC artifact is the sole remaining
group.** Excluding it: **zero genuine cases of a listed equity symbol having two
different issuer_codes.** issuer_code is stable for the life of an equity symbol,
including across its fully-paid/partly-paid share classes.

## Overall conclusions for corporate-action-chain design

1. **ISIN** is always 1:1 with a symbol at a point in time — safe to use as the current
   identity key, never ambiguous in the "one ISIN → many symbols" direction.
2. For **plain listed equity** (`issuer_type='E'`, `instrument_type='01'`),
   **`issuer_code`** is a stable, safe key for chaining a company's ISINs across
   corporate actions (splits/reissuance) over its lifetime — no genuine counterexamples
   found in this dataset. Symbol renames (e.g. `MMFSL`→`CGCL`, `IIFLWAM`→`360ONE`,
   `CASTROL`→`CASTROLIND`) occur under a stable issuer_code and are exactly the kind of
   corporate-action event the chain should capture.
3. Do **not** apply issuer_code-based chaining to non-equity instrument classes without
   guardrails:
   - AMC/fund products (`issuer_type='F'`) reuse one issuer_code across many unrelated
     schemes — chain by ISIN/serial instead, and treat an issuer_code change as a
     sponsor-transfer event only when accompanied by `issuer_type='F'` and low
     symbol-cardinality per ISIN pair.
   - Debt instruments (`07`/`08`/`09`/`10`/`11`/`A7`) legitimately have many ISINs per
     issuer/symbol; this is normal bond-tranche issuance, not something the
     `CorporateAction` model needs to represent.
   - A practical guard: only trust `issuer_code`+`instrument_type` as a chain key when
     the resulting symbol/ISIN cardinality is small (2-3); higher cardinality indicates
     an AMC or serial bond issuer, not a single evolving security.
