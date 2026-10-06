# Corporate Announcements: data model proposal

## Context

`jugaad-data`'s `NSELive.corporate_announcements(segment, from_date, to_date, symbol)`
hits NSE's live API and returns JSON records like:

```json
{
  "symbol": "MINDACORP", "sm_name": "Minda Corporation Limited",
  "sm_isin": "INE842C01013", "smIndustry": "Auto Ancillaries",
  "desc": "General Updates",
  "attchmntText": "Minda Corporation Limited has informed the Exchange about General Updates",
  "attchmntFile": "https://nsearchives.nseindia.com/corporate/....pdf",
  "fileSize": "329.92 KB", "attFileSize": "329.92 KB",
  "an_dt": "05-Oct-2026 22:04:51", "exchdisstime": "05-Oct-2026 22:04:52",
  "dt": "05102026220451", "sort_date": "2026-10-05 22:04:51",
  "seq_id": "106807553", "hasXbrl": true,
  "bflag": null, "csvName": null, "old_new": null, "orgid": null
}
```

This is a **live feed, not a dated archive file** — unlike bhavcopies, there's no
per-day downloadable file, so the per-app `BhavcopyFile` provenance pattern
doesn't apply here. The query pattern to support is: search by **symbol**,
**date range**, and **type** (announcement category).

Design decisions:
- Announcement category (`desc`) is normalized into its own lookup table
  (mirrors the `Series` pattern in `securityinfo`), not Django `choices` or a
  bare indexed string — NSE controls this vocabulary and adds new values over
  time, so a growable dimension table avoids rejecting/needing redeploys for
  new categories. It stays in the new app rather than `securityinfo`, since
  `securityinfo` models instrument *identity* (ISIN, series, symbol), not
  announcement-domain concepts — same separation `FnoContract`/`Index` already
  keep from `securityinfo`.
- The fact table FKs to `securityinfo.Instrument` only (resolved via ISIN,
  falling back to symbol, reusing `_resolve_instrument`) — **no denormalized
  `symbol` column**. At ~95k rows/year (775 announcements per 3-day sample),
  a join has no measurable cost, so denormalizing bought nothing. More
  importantly, `symbol` on the instrument can drift two ways — same ISIN gets
  a new symbol, or a symbol survives an ISIN change — and
  `NSESymbolInstrumentMap` already accumulates every `(symbol, instrument)`
  pair ever observed (`_ensure_symbol_map` in
  `dailypricehistory/management/commands/ingest_bhavcopy.py:696` does
  `get_or_create` on the pair, never overwrites), so
  `Instrument.objects.filter(symbols__symbol=X)` already returns every
  instrument ever associated with that symbol in both drift directions. A
  bare `symbol` string on the announcement would actually handle the
  ISIN-change case *worse* (it only matches the ISIN active at filing time),
  not better, so the FK + existing mapping table is strictly the right source
  of truth.

## New app: `corporateannouncements`

Follows the same shape as `fnopricehistory`/`indexpricehistory`: one
identity/dimension table + one fact table, no `BhavcopyFile`-style provenance
table (not applicable to a live API).

### `AnnouncementCategory` (db_table = `announcement_categories`)

| Field | Type |
|---|---|
| `name` | `CharField(max_length=255, unique=True)` — raw `desc` string from NSE |

- `ordering = ["name"]`
- Resolved via `get_or_create(name=...)` at ingest time, same caching pattern
  as `_resolve_series`/`_resolve_instrument` in `ingest_bhavcopy.py`.

### `CorporateAnnouncement` (db_table = `corporate_announcements`)

| Field | Type | Notes |
|---|---|---|
| `instrument` | `FK(Instrument, on_delete=CASCADE, related_name="announcements")` | resolved via `sm_isin`, fallback `symbol`, reusing `_resolve_instrument` logic from `ingest_bhavcopy.py`; also calls `_ensure_symbol_map` so the symbol-at-filing-time is captured in `NSESymbolInstrumentMap`, not duplicated on this row |
| `category` | `FK(AnnouncementCategory, on_delete=PROTECT, related_name="announcements")` | from `desc` |
| `seq_id` | `BigIntegerField(unique=True)` | NSE's own sequential ID — natural idempotency/upsert key, analogous to `file_name` on `BhavcopyFile` |
| `announced_at` | `DateTimeField(db_index=True)` | parsed from `sort_date` (unambiguous `YYYY-MM-DD HH:MM:SS`), the authoritative timestamp for date-range queries |
| `exchange_received_at` | `DateTimeField(null=True, blank=True)` | parsed from `exchdisstime` |
| `attachment_text` | `TextField(blank=True)` | from `attchmntText` |
| `attachment_url` | `URLField(max_length=500, blank=True)` | from `attchmntFile` |
| `attachment_file_size` | `CharField(max_length=20, blank=True)` | from `fileSize`/`attFileSize` (kept as given string, e.g. "329.92 KB") |
| `has_xbrl` | `BooleanField(default=False)` | from `hasXbrl` |
| `raw` | `JSONField(default=dict, blank=True)` | full source record, for fields not worth modeling individually (`bflag`, `csvName`, `old_new`, `orgid` — all null in observed samples, purpose unconfirmed) and as a forward-compat safety net if NSE adds fields |
| `ingested_at` | `DateTimeField(auto_now_add=True)` | |

Constraints/indexes (named, following repo convention):
- `UniqueConstraint(fields=["seq_id"], name="uniq_announcement_seq_id")` — primary upsert key
- `Index(fields=["announced_at"], name="idx_announcement_date")`
- `Index(fields=["instrument"], name="idx_announcement_instrument")`
- `Index(fields=["category"], name="idx_announcement_category")`
- `Index(fields=["instrument", "announced_at"], name="idx_announcement_instrument_date")` — composite for the common "company + date range" query
- `ordering = ["-announced_at"]`

This directly supports the three query patterns:
- by symbol → resolve symbol to instrument(s) via `Instrument.objects.filter(symbols__symbol=X)`
  (uses `idx_announcement_instrument`/composite index), correct across both
  symbol-rename and ISIN-change drift since it reads the same
  `NSESymbolInstrumentMap` the ingest command writes to
- by date range → `idx_announcement_date` / composite index
- by type → FK join to `AnnouncementCategory` + `idx_announcement_category`

## Sprints

Split for incremental review — each sprint is a reviewable, independently
mergeable unit, in order.

### Sprint 1 — Models only ✅ done (`88b979d`)

- New `corporateannouncements` app (`python manage.py startapp corporateannouncements`,
  registered in `INSTALLED_APPS`).
- `AnnouncementCategory` + `CorporateAnnouncement` models as specified above.
- `admin.py`: register both models (mirrors `securityinfo/admin.py`,
  `indexpricehistory/admin.py` pattern).
- `apps.py`: `CorporateannouncementsConfig`.
- `python manage.py makemigrations corporateannouncements && python manage.py migrate`.
- **No ingest command yet.** Reviewable purely as a schema addition —
  verify migration applies cleanly and `Instrument`/`NSESymbols` FKs resolve
  as expected via Django shell (`CorporateAnnouncement.objects.none()`, admin
  page loads).

### Sprint 2 — Basic ingest command ✅ done (`b21d8bc`)

- `ingest_corporate_announcements` management command, minimal surface:
  `--latest` (default, last `--lookback` days, default 3) /
  `--from YYYY-MM-DD --to YYYY-MM-DD` / `--days N` — same
  mutually-exclusive mode group as `ingest_bhavcopy.py`.
- Calls `NSELive().corporate_announcements(from_date=..., to_date=...)`
  directly (no `--symbol`/`--segment` filters yet — that's Sprint 3).
- Reuses `_resolve_instrument`-equivalent logic (ISIN-first via `sm_isin`,
  symbol fallback) + `_ensure_symbol_map`, and `get_or_create` for
  `AnnouncementCategory`, each with a per-run cache dict as in
  `ingest_bhavcopy.py`.
- `sort_date`/`exchdisstime` are parsed and made timezone-aware as IST
  (`Asia/Kolkata`, via `zoneinfo` + `django.utils.timezone.make_aware`)
  before being stored — NSE's feed has no explicit offset, and
  `USE_TZ = True` requires aware datetimes; Django stores them as UTC.
- Idempotency: `bulk_create(..., ignore_conflicts=True)` keyed on `seq_id`'s
  unique constraint, inside `transaction.atomic()` for the whole run (one
  API response is small enough not to need per-batch transactions).
- A record missing `seq_id`/`sort_date`, or with an unparseable `sort_date`,
  is logged and skipped rather than failing the run.
- **Verification:** ran `ingest_corporate_announcements --days 3` against the
  live API — 761 rows landed in `corporate_announcements`, 54 in
  `announcement_categories`; re-running the same command was a confirmed
  no-op (dedup via `seq_id`).
- No logger entry yet (uses `logging.getLogger(__name__)`, falls through to
  the root logger) — wiring a dedicated `corporateannouncements` entry into
  `LOGGING` is Sprint 3.

### Sprint 3 — CLI polish + logging ✅ done

- Added `--symbol SYM` and `--segment` (default `equities`) filters, passed
  through to `corporate_announcements(...)`.
- Wired up a `corporateannouncements` logger entry in `pipeline/settings.py`
  `LOGGING` config, mirroring the `indexpricehistory` entry added in commit
  `d4f818f`.
- Error handling already matched the collect-and-continue convention from
  Sprint 2 (unparseable/missing `seq_id`/`sort_date` records are logged and
  skipped, never fail the run) — this command fetches the whole date range
  in one API call rather than per-day like `ingest_bhavcopy.py`, so there is
  no multi-day batch loop to wrap in a try/except + trailing `CommandError`.
- **Verification:** ran `ingest_corporate_announcements --symbol RELIANCE
  --days 30` — API filter applied (16 targeted rows vs. hundreds
  unfiltered); confirmed `logs/pipeline.log` captured the new
  `corporateannouncements` logger's entries.

### Sprint 4 — Docs ✅ done

- New `corporateannouncements/AGENTS.md`, mirroring `indexpricehistory/AGENTS.md`'s
  `## Files` table, referencing a new `spec/schema.md` section.
- `spec/schema.md`: new `§3.x` section documenting `announcement_categories`
  and `corporate_announcements` (DDL + design notes, same style as existing
  sections).
- Root `AGENTS.md`: new row in "Folder index" table; new `###` subsection in
  "CLI reference" for `ingest_corporate_announcements`, modeled on the
  "Index bhavcopy ingest" subsection (options table + guarantees + examples).

### Sprint 5 — Verification pass

- Full end-to-end run against the live API for a realistic range (e.g.
  `--days 30`).
- Re-run idempotency check at scale.
- Spot-check all three query patterns in Django shell:
  - `CorporateAnnouncement.objects.filter(instrument__symbols__symbol="RELIANCE")`
  - `CorporateAnnouncement.objects.filter(announced_at__date__range=(d1, d2))`
  - `CorporateAnnouncement.objects.filter(category__name="Board Meeting")`
- Confirm `logs/pipeline.log` output end-to-end, and that `spec/schema.md`/`AGENTS.md`
  docs match the final implementation.
