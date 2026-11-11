"""Fetch NSE shareholding-pattern filings and ingest them into ``shareholding_patterns``.

This is a live API (``NSELive.shareholding_pattern``), not a dated archive file,
so there is no per-day file to download and no provenance table. The command
records the list of filings only; ``extract_shareholding_pattern`` downloads and
parses each filing's XBRL.

Mode selection (exactly one):

* ``--latest`` (default): filings from the last ``--lookback`` days.
* ``--from YYYY-MM-DD [--to YYYY-MM-DD]``: inclusive date range.
* ``--days N``: last N calendar days ending today.

Optional filters passed to NSE: ``--symbol`` and ``--segment`` (default
``equities``).

Idempotency: ``bulk_create(ignore_conflicts=True)`` on NSE's ``recordId``
unique constraint, inside one transaction per run.
"""

import logging
import time
import zoneinfo
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols
from shareholdingpattern.models import ShareholdingPattern

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan", "null", "******") else value


def _parse_dt(raw):
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return timezone.make_aware(datetime.strptime(raw, fmt), IST)
        except ValueError:
            continue
    return None


def _parse_decimal(raw):
    raw = _clean(raw)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _valid_isin(raw):
    return raw if raw and len(raw) == 12 and raw.isalnum() else None


class Command(BaseCommand):
    help = "Fetch NSE shareholding-pattern filings and ingest them into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="filings from the last --lookback days (default)",
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
        parser.add_argument("--symbol", metavar="SYM", help="restrict to one NSE symbol")
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
        if options["retries"] <= 0:
            raise CommandError("--retries must be a positive integer")

        logger.info(
            "Shareholding pattern ingest job started "
            "(from=%s, to=%s, segment=%s, symbol=%s)",
            from_date, to_date, options["segment"], options["symbol"],
        )
        started = time.monotonic()
        try:
            self._run(from_date, to_date, options)
        finally:
            logger.info(
                "Shareholding pattern ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    def _run(self, from_date, to_date, options):
        records = self._fetch(from_date, to_date, options)
        if not records:
            logger.info("No shareholding filings returned for %s to %s", from_date, to_date)
            return

        self._instrument_cache = {}
        self._symbol_cache = {}
        self._symbol_map_cache = set()

        with transaction.atomic():
            rows, skipped = [], 0
            for raw in records:
                row = self._parse_record(raw, options["segment"])
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
            before = ShareholdingPattern.objects.count()
            ShareholdingPattern.objects.bulk_create(
                rows, ignore_conflicts=True, batch_size=500
            )
            inserted = ShareholdingPattern.objects.count() - before

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
                return NSELive().shareholding_pattern(
                    segment=options["segment"],
                    from_date=from_date,
                    to_date=to_date,
                    symbol=options["symbol"],
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Fetching shareholding filings failed (attempt %d/%d): %s",
                    attempt, retries, exc,
                )
                if attempt < retries and delay:
                    time.sleep(delay * attempt)
        raise CommandError(f"fetching shareholding filings failed: {last_exc}")

    def _parse_record(self, raw, segment):
        record_id_raw = _clean(raw.get("recordId"))
        symbol = _clean(raw.get("symbol"))
        quarter_raw = _clean(raw.get("date"))
        if record_id_raw is None or symbol is None or quarter_raw is None:
            logger.warning("skipping record missing recordId/symbol/date: %r", raw)
            return None
        try:
            record_id = int(record_id_raw)
        except ValueError:
            logger.warning("skipping record with non-numeric recordId: %r", raw)
            return None
        quarter_end = _parse_dt(quarter_raw)
        if quarter_end is None:
            logger.warning(
                "skipping record %s with unparseable date %r", record_id, quarter_raw
            )
            return None

        isin = _valid_isin(_clean(raw.get("isin")))
        name = _clean(raw.get("name"))
        instrument = self._resolve_instrument(isin=isin, symbol=symbol, name=name)
        revised_at = _parse_dt(raw.get("revisedDate")) or _parse_dt(
            raw.get("revisionDate")
        )
        submitted_at = _parse_dt(raw.get("submissionDate"))

        return ShareholdingPattern(
            instrument=instrument,
            record_id=record_id,
            isin=isin or "",
            symbol=symbol,
            company_name=name or "",
            segment=segment,
            quarter_end=quarter_end.date(),
            broadcast_at=_parse_dt(raw.get("broadcastDate")),
            submitted_on=submitted_at.date() if submitted_at else None,
            promoter_percent=_parse_decimal(raw.get("pr_and_prgrp")),
            public_percent=_parse_decimal(raw.get("public_val")),
            revised=(_clean(raw.get("revisedData")) or "").upper() == "Y",
            revised_status=_clean(raw.get("revisedStatus")) or "",
            revised_at=revised_at,
            revision_remark=(
                _clean(raw.get("revisedRemark"))
                or _clean(raw.get("revisionRemark"))
                or ""
            ),
            xbrl_url=_clean(raw.get("xbrl")) or "",
            xbrl_file_size=_clean(raw.get("xbrlFileSize")) or "",
            raw=raw,
        )

    def _resolve_instrument(self, isin, symbol, name):
        cache_key = isin or f"#{symbol}"
        instrument = self._instrument_cache.get(cache_key)
        if instrument is not None:
            return instrument

        if isin:
            instrument, _ = Instrument.objects.get_or_create(
                isin=isin, defaults={"name": name or "", "instrument_type": "STK"}
            )
        else:
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

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
