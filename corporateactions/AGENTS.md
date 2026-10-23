# AGENTS.md — `corporateactions/`

Django app for NSE corporate actions (dividends, splits, bonus, rights, buyback,
demerger, ...), from NSE's live `corporate_actions` feed (via `jugaad-data`'s
`NSELive`, filtered by **ex-date**). NSE gives no structured economics: ratios,
amounts and face values are only in the free-text `subject`, so this app decodes
it with **fixed (regex) logic** and sanity-checks split/bonus factors against
real prices. Anything not decoded completely, or failing the price check, is
stored with `status=needs_review` for a human. No LLM is involved.

## Data model

* `corporate_actions` (`CorporateAction`) — one row per NSE record, keyed
  `(instrument, ex_date, subject)`. Holds dates, the raw record, `status`
  (`auto` / `needs_review` / `verified`), `review_reason`, `parse_problems`,
  `parser_version` and the price-check result (`expected_factor`,
  `observed_ratio`, `price_check`).
* `corporate_action_legs` (`CorporateActionLeg`) — the decoded components of an
  action. One subject can hold several (`AGM/Dividend/Special Dividend`), so an
  action has 0..n legs. `kind` selects which columns apply: `amount` /
  `dividend_type` (dividend, distribution), `ratio_new:ratio_held` (bonus,
  rights; *new shares per held shares*), `old_face_value` / `new_face_value`
  (split, consolidation), `premium` (rights), and `price_factor` — the
  multiplier for pre ex-date prices (split `new_fv/old_fv`, bonus
  `held/(new+held)`, consolidation `new_fv/old_fv`; null for everything else).

`status` is **derived** (`refresh_status`): `needs_review` if the parser left
problems or the price check failed, else `auto`. `verified` is only set by a
human (admin action) and is never overwritten.

## Price sanity check

For an instrument and ex-date, close on the first trading day on/after the
ex-date ÷ close on the last trading day before it should be ≈ the product of
the legs' `price_factor`s (a split and a bonus on the same ex-date arrive as
separate NSE records, so the check is per instrument+ex-date). Tolerance is a
deliberately coarse ±25% (daily moves up to the 20% circuit limit are normal):
it catches gross errors (inverted/mis-parsed ratio, missing event) but cannot
confirm an exact ratio; the parsed text stays the source of truth.

Results: `passed`, `failed` (→ `needs_review`), `inconclusive` (factor so close
to 1 that "no adjustment" would pass too, e.g. bonus 1:10), `no_data` (no
close on both sides; re-checked on later runs), `not_applicable` (dividends
etc.).

**ISIN changes:** a face-value split gives the shares a *new ISIN*. NSE's action
record carries the old ISIN while prices from the ex-date sit under the new
one, so prices are looked up through the NSE **symbol**
(`NSESymbolInstrumentMap`), not the action's instrument. Remember this for any
future price-adjustment query.

## Files

| File | Status | What it is |
|---|---|---|
| `models.py` | ready | `CorporateAction`, `CorporateActionLeg`. |
| `parsing.py` | ready | Pure `parse_subject(subject) -> (legs, problems)`; `PARSER_VERSION` (bump on rule changes; ingest re-parses older rows except `verified`). |
| `price_check.py` | ready | Per-(instrument, ex-date) sanity check; `check_groups()`. |
| `admin.py` | ready | Both models; filter by status/price_check/kind; "Mark selected actions as verified". |
| `management/commands/ingest_corporate_actions.py` | ready | Fetch + parse + store + price-check. See root `AGENTS.md` CLI reference. |
| `management/commands/check_corporate_action_prices.py` | ready | Re-run the price check (default: rows with `no_data`; `--all` for everything). Run after a price backfill. |
| `tests.py` | ready | Parser cases from real subjects, price check, ingest flow with a mocked NSE API. |
| `migrations/` | ready | `0001_initial`. |

## Known gaps

* **Demerger / buyback / capital reduction / redemption** are recorded as
  type-only legs: the feed carries no ratio, price or resulting entity for them.
* **Rights** store ratio and premium but produce no `price_factor` (needs the
  cum-date price); they are excluded from the price check.
* Distribution (InvIT/REIT) component breakdown is best effort (`extra`); the
  per-unit total is what is reliable.
* Price adjustment itself (applying factors to history) is not implemented yet.
