"""Fetch NSE's board-meeting / event calendar and ingest it into ``event_calendar``.

This is a live API (``NSELive.event_calendar``), not a dated archive file. NSE
gives the records no ID, so each row gets a deterministic ``record_key`` (hash of
segment, symbol, date, purpose and description) and is inserted with
``ignore_conflicts`` for idempotent re-runs.

Mode selection (exactly one):

* ``--latest`` (default): events from ``--lookback`` days ago to ``--ahead`` days
  from today (upcoming events are announced in advance).
* ``--from YYYY-MM-DD [--to YYYY-MM-DD]``: inclusive event-date range.
* ``--days N``: last N calendar days ending today.

``--symbol`` is applied locally because NSE's event-calendar endpoint does not
accept a symbol filter. The instrument is resolved from that symbol. ``--segment``
is ``equities`` (default) or ``sme``.
"""

import hashlib
import logging
import time
import zoneinfo
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from eventcalendar.models import EventCalendar
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan", "null", "******") else value


def _parse_date(raw):
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_dt(raw):
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return timezone.make_aware(datetime.strptime(raw, fmt), IST)
        except ValueError:
            continue
    return None


def record_key(segment, symbol, event_date, purpose, description):
    basis = "\x1f".join(
        [
            segment or "",
            symbol or "",
            event_date.isoformat() if event_date else "",
            purpose or "",
            description or "",
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class Command(BaseCommand):
    help = "Fetch NSE's event calendar and ingest it into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="events from --lookback days ago to --ahead days ahead (default)",
        )
        mode.add_argument(
            "--from",
            dest="from_date",
            metavar="YYYY-MM-DD",
            help="start of inclusive event-date range",
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
            help="end of inclusive event-date range (default: today)",
        )
        parser.add_argument(
            "--lookback",
            type=int,
            default=3,
            help="days before today for --latest (default: 3)",
        )
        parser.add_argument(
            "--ahead",
            type=int,
            default=30,
            help="days after today for --latest (default: 30)",
        )
        parser.add_argument(
            "--symbol",
            metavar="SYM",
            help="restrict to events for this NSE symbol",
        )
        parser.add_argument(
            "--segment",
            default="equities",
            help="equities or sme (default: equities)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="seconds before a retry (default: 1.0, 0 disables)",
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=3,
            help="total attempts for the API call (default: 3)",
        )

    def handle(self, *args, **options):
        if options["days"] is not None:
            if options["days"] <= 0:
                raise CommandError("--days must be a positive integer")
            from_date = date.today() - timedelta(days=options["days"] - 1)
            to_date = date.today()
        elif options["from_date"]:
            from_date = self._parse_range_date(options["from_date"], "--from")
            to_date = self._parse_range_date(
                options["to_date"] or date.today().isoformat(), "--to"
            )
            if from_date > to_date:
                raise CommandError("--from must be on or before --to")
        else:
            if options["to_date"]:
                raise CommandError("--to requires --from")
            if options["lookback"] < 0 or options["ahead"] < 0:
                raise CommandError("--lookback and --ahead must not be negative")
            from_date = date.today() - timedelta(days=options["lookback"])
            to_date = date.today() + timedelta(days=options["ahead"])
        if options["retries"] <= 0:
            raise CommandError("--retries must be a positive integer")

        logger.info(
            "Event calendar ingest job started "
            "(from=%s, to=%s, segment=%s, symbol=%s)",
            from_date, to_date, options["segment"], options["symbol"],
        )
        started = time.monotonic()
        try:
            self._run(from_date, to_date, options)
        finally:
            logger.info(
                "Event calendar ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    def _run(self, from_date, to_date, options):
        records = self._fetch(from_date, to_date, options)
        symbol_filter = (options["symbol"] or "").upper()

        self._instrument_cache = {}
        self._symbol_cache = {}
        self._symbol_map_cache = set()

        with transaction.atomic():
            rows, skipped = [], 0
            for raw in records:
                symbol = _clean(raw.get("symbol")) or _clean(raw.get("bm_symbol"))
                if symbol_filter and (symbol or "").upper() != symbol_filter:
                    continue
                row = self._parse_record(raw, options["segment"])
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
            before = EventCalendar.objects.count()
            EventCalendar.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
            inserted = EventCalendar.objects.count() - before

        logger.info(
            "%d/%d record(s) ingested (%d duplicates skipped, %d unparseable)",
            inserted, len(records), len(rows) - inserted, skipped,
        )

    def _fetch(self, from_date, to_date, options):
        from jugaad_data.nse.live import NSELive

        retries, delay = options["retries"], options["delay"]
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return NSELive().event_calendar(
                    segment=options["segment"],
                    from_date=from_date,
                    to_date=to_date,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Fetching event calendar failed (attempt %d/%d): %s",
                    attempt, retries, exc,
                )
                if attempt < retries and delay:
                    time.sleep(delay * attempt)
        raise CommandError(f"fetching event calendar failed: {last_exc}")

    def _parse_record(self, raw, segment):
        symbol = _clean(raw.get("symbol")) or _clean(raw.get("bm_symbol"))
        event_date = _parse_date(_clean(raw.get("date")) or _clean(raw.get("bm_date")))
        if symbol is None or event_date is None:
            logger.warning("skipping record missing symbol/date: %r", raw)
            return None

        purpose = _clean(raw.get("purpose")) or _clean(raw.get("bm_purpose")) or ""
        description = _clean(raw.get("bm_desc")) or ""
        name = _clean(raw.get("company")) or _clean(raw.get("sm_name"))
        instrument = self._resolve_instrument(symbol, name)

        return EventCalendar(
            instrument=instrument,
            record_key=record_key(segment, symbol, event_date, purpose, description),
            segment=segment,
            symbol=symbol,
            company_name=name or "",
            event_date=event_date,
            purpose=purpose[:255],
            description=description,
            announced_at=_parse_dt(raw.get("bm_timestamp")),
            attachment_url=(
                _clean(raw.get("attachment"))
                or _clean(raw.get("attchmntFile"))
                or ""
            ),
            ixbrl_url=_clean(raw.get("ixbrl")) or "",
            attachment_file_size=(
                _clean(raw.get("attFileSize"))
                or _clean(raw.get("attachmentFileSize"))
                or ""
            ),
            raw=raw,
        )

    def _resolve_instrument(self, symbol, name):
        instrument = self._instrument_cache.get(symbol)
        if instrument is not None:
            return instrument

        mapped = [
            m.instrument
            for m in NSESymbolInstrumentMap.objects.filter(symbol__symbol=symbol)
            .select_related("instrument")
            .order_by("-id")
        ]
        instrument = next((i for i in mapped if i.isin), None) or (
            mapped[0] if mapped else None
        )
        if instrument is None:
            instrument, _ = Instrument.objects.get_or_create(
                isin=None,
                name=symbol,
                defaults={"instrument_type": "STK"},
            )
            logger.info(
                "No instrument known for symbol %s; created ISIN-less %r",
                symbol,
                name,
            )
        if name and not instrument.name:
            instrument.name = name
            instrument.save(update_fields=["name"])
        self._instrument_cache[symbol] = instrument
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

    @staticmethod
    def _parse_range_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
