# TODO — F&O historical data storage

Design for storing NSE F&O (futures & options) bhavcopy data, following the
same patterns already used for CM (see `schema.md` §1-5 and
`dailypricehistory/management/commands/ingest_bhavcopy.py`).

**Implemented** in the `fnopricehistory` app — see its
[AGENTS.md](../fnopricehistory/AGENTS.md) for the current file layout. The
design below is kept as the record of the decisions made; the app follows it
as written.

## Why a separate identity model

CM instruments have a stable identity (ISIN/ticker per `Instrument`). F&O
contracts have no ISIN — identity is the tuple
`(underlying_symbol, instrument_type, expiry_date, strike_price, option_type)`,
and a contract only trades for its own lifetime (listing → expiry). This needs
its own model, not `securityinfo.Instrument`.

## Proposed models (new app, e.g. `fnopricehistory`, mirroring `dailypricehistory`)

### `FnoContract` (identity, like `Instrument`)

```python
underlying_symbol = models.CharField(max_length=50)  # e.g. NIFTY, RELIANCE — plain
                                                      # string, no FK to NSESymbols
                                                      # (index underlyings aren't
                                                      # stock symbols; keep it simple)
instrument_type = models.CharField(max_length=10)    # UDiFF codes: FUTIDX/OPTIDX/
                                                      # FUTSTK/OPTSTK — normalize
                                                      # legacy INSTRUMENT values to
                                                      # these using jugaad_data's
                                                      # _LEGACY_TO_UDIFF_INSTRUMENT_TYPE
                                                      # so both formats land in one
                                                      # column
expiry_date = models.DateField()
strike_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
option_type = models.CharField(max_length=2, null=True, blank=True)  # CE/PE, null for futures
fin_instrm_id = models.IntegerField(null=True, blank=True)

class Meta:
    constraints = [
        models.UniqueConstraint(
            fields=["underlying_symbol", "instrument_type", "expiry_date",
                    "strike_price", "option_type"],
            name="uniq_fno_contract",
        ),
    ]
    indexes = [models.Index(fields=["expiry_date"], name="idx_fno_expiry")]
```

### `FnoPriceHistory` (single unified table for futures + options — see rationale below)

```python
contract = models.ForeignKey(FnoContract, on_delete=models.CASCADE, related_name="price_history")
trade_date = models.DateField()
open, high, low, close, settlement_price  # DecimalField(18,4), same convention as NSECmPriceHistory
volume = models.BigIntegerField(null=True, blank=True)
turnover = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
num_trades = models.IntegerField(null=True, blank=True)
open_interest = models.BigIntegerField(null=True, blank=True)
change_in_oi = models.BigIntegerField(null=True, blank=True)
underlying_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
file = models.ForeignKey(BhavcopyFile, on_delete=models.PROTECT, related_name="fno_price_rows",
                          null=True, blank=True)  # reuse BhavcopyFile with segment="FO";
                                                   # no schema change needed there

class Meta:
    constraints = [
        models.UniqueConstraint(fields=["contract", "trade_date"], name="uniq_fno_price_contract_date"),
    ]
    indexes = [models.Index(fields=["trade_date"], name="idx_fno_price_date")]
```

**Unified vs split (futures/options in separate tables) — decided: unified.**
Rationale: the UDiFF bhavcopy already unifies both in one row schema, so
parsing stays a 1:1 mapping instead of fighting the source data; most useful
queries (PCR, OI walls, underlying-level rollups) need futures and options
together anyway; and it mirrors the existing `NSECmPriceHistory` pattern (one
table regardless of series). Tradeoff accepted: `strike_price`/`option_type`
are null on future rows, enforced only by application logic, not a DB
constraint.

## Ingest command

New command `ingest_fno_bhavcopy`, mirroring `ingest_bhavcopy.py` almost
exactly:

- Same holiday/retry/throttle/date-walking scaffolding (reuse as-is — it's
  segment-agnostic).
- Swap `archives.bhavcopy_save` → `archives.bhavcopy_fo_save` (already
  implemented in `jugaad_data.nse.archives.NSEArchives`, see
  `env/Lib/site-packages/jugaad_data/nse/archives.py:461`).
- Filename pattern `fo{dd}{MMM}{yyyy}bhav.csv` instead of `cm{...}bhav.csv`.
- `_parse_legacy_row`/`_parse_udiff_row` replaced with F&O column mappings:
  - legacy: `INSTRUMENT, SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP, OPEN, HIGH,
    LOW, CLOSE, SETTLE_PR, CONTRACTS, VAL_INLAKH, OPEN_INT, CHG_IN_OI,
    TIMESTAMP`
  - UDiFF: `FinInstrmTp, TckrSymb, XpryDt, StrkPric, OptnTp, OpnPric, HghPric,
    LwPric, ClsPric, SttlmPric, TtlTradgVol, TtlTrfVal, OpnIntrst,
    ChngInOpnIntrst, UndrlygPric` (filter on `Sgmt == "FO"` instead of the CM
    guard on `FinInstrmTp == "STK"`)
- `_resolve_instrument` becomes `_resolve_contract`: `get_or_create` on the
  5-tuple unique key instead of ISIN, with the same per-file cache pattern.
- `segment="FO"` on the `BhavcopyFile` row (field already exists, no schema
  change).

## Retention: purge contracts >1 year past expiry

New command `purge_expired_fno_contracts [--apply]`, same dry-run-by-default
pattern as `purge_duplicate_bhavcopies`:

- Query: `FnoContract.objects.filter(expiry_date__lt=today - timedelta(days=365))`.
- Retention window is **calendar days** from `expiry_date` (matches the
  `--days N` convention already used elsewhere in this repo; not trading
  days).
- Dry run (default): log/report contract count and associated
  `FnoPriceHistory` row count that would be deleted.
- `--apply`: delete the `FnoContract` rows; `FnoPriceHistory.contract` is
  `on_delete=CASCADE`, so their price rows are removed automatically — no
  separate price-row query needed.
- Run on a schedule (cron/Task Scheduler) alongside ingest, not inline in
  `ingest_fno_bhavcopy` — keeps ingest idempotent/side-effect-free and keeps
  the destructive path opt-in and reviewable, same reasoning as
  `purge_duplicate_bhavcopies`.
- `idx_fno_expiry` (above) makes the filter a range scan, not a table scan.

## Open item

`underlying_symbol` has no FK (decided: keep simple, plain string) — so
there's no referential link back to `securityinfo.Instrument`/`NSESymbols`
for stock-underlying contracts. If cross-segment queries ("F&O activity for
this equity instrument") are ever needed, revisit this.
