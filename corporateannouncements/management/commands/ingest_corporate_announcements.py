"""Fetch NSE corporate announcements and ingest them into the database.

Unlike bhavcopies, corporate announcements are a **live feed, not a dated
archive file** (``NSELive.corporate_announcements``), so there is no
per-day file to download or a ``BhavcopyFile``-style provenance table.
Each API response is a flat list of announcement records, upserted into
``corporate_announcements`` keyed on NSE's own ``seq_id``.

Mode selection (exactly one):

* ``--latest`` (default): announcements from the last ``--lookback`` days.
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive date range.
* ``--days N``: last N calendar days ending today.

Optional filters, passed straight through to ``corporate_announcements``:
``--symbol SYM`` restricts to one NSE symbol, ``--segment`` selects the NSE
segment (default ``equities``).

Idempotency: rows are inserted with ``bulk_create(ignore_conflicts=True)``
keyed on the ``seq_id`` unique constraint, inside one transaction per run,
so re-running the same range is a no-op for rows already ingested.

Each instrument is resolved via ``sm_isin`` (falling back to ``symbol``),
reusing the same ISIN-first / symbol-fallback pattern as
``ingest_bhavcopy.py``, and the symbol-at-filing-time is recorded in
``NSESymbolInstrumentMap`` so a later symbol rename or ISIN change does not
orphan historical announcements.

Progress and errors are logged to console and to ``logs/pipeline.log`` via
the project-level ``LOGGING`` configuration.
"""

import logging
import time
import zoneinfo
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from corporateannouncements.models import AnnouncementCategory, CorporateAnnouncement
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan") else value


class Command(BaseCommand):
    help = "Fetch NSE corporate announcements and ingest them into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="fetch announcements from the last --lookback days (default)",
        )
        mode.add_argument(
            "--from",
            dest="from_date",
            metavar="YYYY-MM-DD",
            help="start of inclusive date range",
        )
        mode.add_argument(
            "--days",
            type=int,
            metavar="N",
            help="last N calendar days ending today",
        )
        parser.add_argument(
            "--to",
            dest="to_date",
            metavar="YYYY-MM-DD",
            help="end of inclusive date range (default: today)",
        )
        parser.add_argument(
            "--lookback",
            type=int,
            default=3,
            help="calendar days to fetch for --latest (default: 3)",
        )
        parser.add_argument(
            "--symbol",
            metavar="SYM",
            help="restrict to announcements for this NSE symbol",
        )
        parser.add_argument(
            "--segment",
            default="equities",
            help="NSE segment to query (default: equities)",
        )

    def handle(self, *args, **options):
        if options["days"] is not None:
            if options["days"] <= 0:
                raise CommandError("--days must be a positive integer")
            from_date = date.today() - timedelta(days=options["days"] - 1)
            to_date = date.today()
        elif options["from_date"]:
            from_date = self._parse_date(options["from_date"], "--from")
            to_date = self._parse_date(
                options["to_date"] or date.today().isoformat(), "--to"
            )
            if from_date > to_date:
                raise CommandError("--from must be on or before --to")
        else:
            if options["to_date"]:
                raise CommandError("--to requires --from")
            if options["lookback"] <= 0:
                raise CommandError("--lookback must be a positive integer")
            from_date = date.today() - timedelta(days=options["lookback"] - 1)
            to_date = date.today()

        symbol = options["symbol"]
        segment = options["segment"]

        logger.info(
            "Corporate announcements ingest job started "
            "(from=%s, to=%s, segment=%s, symbol=%s)",
            from_date, to_date, segment, symbol,
        )
        started = time.monotonic()
        try:
            self._run(from_date, to_date, segment, symbol)
        finally:
            logger.info(
                "Corporate announcements ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    def _run(self, from_date, to_date, segment, symbol):
        from jugaad_data.nse.live import NSELive

        try:
            records = NSELive().corporate_announcements(
                segment=segment, from_date=from_date, to_date=to_date, symbol=symbol
            )
        except Exception as exc:
            raise CommandError(f"fetching corporate announcements failed: {exc}")

        if not records:
            logger.info("No announcements returned for %s to %s", from_date, to_date)
            return

        logger.info("Fetched %d announcement record(s)", len(records))
        self._category_cache = {}
        self._instrument_cache = {}
        self._symbol_cache = {}
        self._symbol_map_cache = set()

        with transaction.atomic():
            rows = []
            skipped = 0
            for raw in records:
                row = self._parse_record(raw)
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
            before = CorporateAnnouncement.objects.count()
            CorporateAnnouncement.objects.bulk_create(
                rows, ignore_conflicts=True, batch_size=1000
            )
            inserted = CorporateAnnouncement.objects.count() - before

        logger.info(
            "%d/%d record(s) ingested (%d duplicates skipped, %d unparseable)",
            inserted, len(records), len(rows) - inserted, skipped,
        )

    def _parse_record(self, raw):
        seq_id_raw = _clean(raw.get("seq_id"))
        sort_date_raw = _clean(raw.get("sort_date"))
        if seq_id_raw is None or sort_date_raw is None:
            logger.warning("skipping record missing seq_id/sort_date: %r", raw)
            return None
        try:
            seq_id = int(seq_id_raw)
        except ValueError:
            logger.warning("skipping record with non-numeric seq_id: %r", raw)
            return None
        try:
            announced_at = timezone.make_aware(
                datetime.strptime(sort_date_raw, "%Y-%m-%d %H:%M:%S"), IST
            )
        except ValueError:
            logger.warning(
                "skipping record %s with unparseable sort_date %r",
                seq_id, sort_date_raw,
            )
            return None

        exchange_received_at = None
        exch_raw = _clean(raw.get("exchdisstime"))
        if exch_raw:
            try:
                exchange_received_at = timezone.make_aware(
                    datetime.strptime(exch_raw, "%d-%b-%Y %H:%M:%S"), IST
                )
            except ValueError:
                logger.warning(
                    "%s: unparseable exchdisstime %r, leaving null",
                    seq_id, exch_raw,
                )

        instrument = self._resolve_instrument(
            isin=_clean(raw.get("sm_isin")),
            name=_clean(raw.get("sm_name")),
            symbol=_clean(raw.get("symbol")),
        )
        category = self._resolve_category(_clean(raw.get("desc")) or "Unspecified")

        return CorporateAnnouncement(
            instrument=instrument,
            category=category,
            seq_id=seq_id,
            announced_at=announced_at,
            exchange_received_at=exchange_received_at,
            attachment_text=_clean(raw.get("attchmntText")) or "",
            attachment_url=_clean(raw.get("attchmntFile")) or "",
            attachment_file_size=(
                _clean(raw.get("fileSize")) or _clean(raw.get("attFileSize")) or ""
            ),
            has_xbrl=bool(raw.get("hasXbrl")),
            raw=raw,
        )

    # ------------------------------------------------------ master data lookup

    def _resolve_category(self, name):
        if name not in self._category_cache:
            self._category_cache[name] = AnnouncementCategory.objects.get_or_create(
                name=name
            )[0]
        return self._category_cache[name]

    def _resolve_instrument(self, isin, name, symbol):
        cache_key = isin if isin else f"#{symbol or name}"
        instrument = self._instrument_cache.get(cache_key)
        if instrument is not None:
            return instrument
        if isin:
            instrument, _ = Instrument.objects.get_or_create(
                isin=isin,
                defaults={"name": name or "", "instrument_type": "STK"},
            )
        else:
            instrument, _ = Instrument.objects.get_or_create(
                isin=None,
                name=symbol or name or "",
                defaults={"instrument_type": "STK"},
            )
        if name and not instrument.name:
            instrument.name = name
            instrument.save(update_fields=["name"])
        self._instrument_cache[cache_key] = instrument
        self._ensure_symbol_map(symbol, instrument)
        return instrument

    def _ensure_symbol_map(self, symbol, instrument):
        if not symbol:
            return
        key = (symbol, instrument.id)
        if key in self._symbol_map_cache:
            return
        nse_symbol = self._symbol_cache.get(symbol)
        if nse_symbol is None:
            nse_symbol, _ = NSESymbols.objects.get_or_create(symbol=symbol)
            self._symbol_cache[symbol] = nse_symbol
        NSESymbolInstrumentMap.objects.get_or_create(
            symbol=nse_symbol, instrument=instrument
        )
        self._symbol_map_cache.add(key)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
