"""Fetch the NSE integrated-filing list and ingest it into ``integrated_filings``.

Like corporate announcements, this is a **live API, not a dated archive file**
(``NSELive.corporate_integrated_filing``), so there is no file to download and
no ``BhavcopyFile``-style provenance table. Each record is one filing release,
upserted keyed on NSE's own ``seq_Id``. This command only records the *list*
of releases; it does not download or parse the XBRL.

Mode selection (exactly one):

* ``--latest`` (default): filings broadcast in the last ``--lookback`` days.
* ``--from YYYY-MM-DD [--to YYYY-MM-DD]``: inclusive date range.
* ``--days N``: last N calendar days ending today.

Optional filters passed through to NSE: ``--symbol``, ``--segment``
(``equities`` or ``sme``), ``--period-ended`` (e.g. ``30-Sep-2026`` or ``all``)
and ``--type`` (``financials`` default, or ``governance``).

The API is paginated (request ``page`` starts at 1, response ``page`` at 0), so
pages are fetched until ``totalCount`` is reached, with ``--delay`` between
pages and ``--retries`` per page.

Idempotency: ``bulk_create(ignore_conflicts=True)`` on the ``seq_id`` unique
constraint, one transaction per run. A revision has its own ``seq_Id`` and is
stored as a separate row.

The feed carries no ISIN, only ``symbol``. The instrument is looked up through
``NSESymbolInstrumentMap`` first; if the symbol is unknown, an ISIN-less
``Instrument`` named after the symbol is created, as the announcements ingest
does for records without an ISIN.
"""

import logging
import time
import zoneinfo
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integratedfilings.models import IntegratedFiling
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

FILING_TYPES = {
    "financials": "Integrated Filing- Financials",
    "governance": "Integrated Filing- Governance",
}


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan", "null") else value


def _parse_dt(raw):
    """Parse NSE's ``DD-Mon-YYYY HH:MM:SS`` (IST) into an aware datetime."""
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return timezone.make_aware(datetime.strptime(raw, fmt), IST)
        except ValueError:
            continue
    return None


class Command(BaseCommand):
    help = "Fetch the NSE integrated-filing list and ingest it into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest", action="store_true",
            help="filings from the last --lookback days (default)",
        )
        mode.add_argument(
            "--from", dest="from_date", metavar="YYYY-MM-DD",
            help="start of inclusive date range",
        )
        mode.add_argument(
            "--days", type=int, metavar="N", help="last N calendar days ending today"
        )
        parser.add_argument(
            "--to", dest="to_date", metavar="YYYY-MM-DD",
            help="end of inclusive date range (default: today)",
        )
        parser.add_argument(
            "--lookback", type=int, default=3,
            help="calendar days to fetch for --latest (default: 3)",
        )
        parser.add_argument("--symbol", metavar="SYM", help="restrict to one NSE symbol")
        parser.add_argument(
            "--segment", default="equities", help="equities or sme (default: equities)"
        )
        parser.add_argument(
            "--period-ended", metavar="DD-Mon-YYYY|all",
            help="restrict to a quarter end, e.g. 30-Sep-2026",
        )
        parser.add_argument(
            "--type", dest="filing_type", choices=sorted(FILING_TYPES),
            default="financials", help="filing type (default: financials)",
        )
        parser.add_argument(
            "--page-size", type=int, default=100, help="records per API page (default: 100)"
        )
        parser.add_argument(
            "--delay", type=float, default=1.0,
            help="seconds between API pages; also the retry back-off (default: 1.0, 0 disables)",
        )
        parser.add_argument(
            "--retries", type=int, default=3,
            help="total attempts per page (default: 3)",
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
        if options["page_size"] <= 0:
            raise CommandError("--page-size must be a positive integer")
        if options["retries"] <= 0:
            raise CommandError("--retries must be a positive integer")

        logger.info(
            "Integrated filings ingest job started "
            "(from=%s, to=%s, type=%s, segment=%s, symbol=%s, period_ended=%s)",
            from_date, to_date, options["filing_type"], options["segment"],
            options["symbol"], options["period_ended"],
        )
        started = time.monotonic()
        try:
            self._run(from_date, to_date, options)
        finally:
            logger.info(
                "Integrated filings ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    # ------------------------------------------------------------------ fetch

    def _fetch_all(self, from_date, to_date, options):
        from jugaad_data.nse.live import NSELive

        nse = NSELive()
        filing_type = FILING_TYPES[options["filing_type"]]
        size, delay, retries = options["page_size"], options["delay"], options["retries"]

        records, page = [], 1
        while True:
            response = self._fetch_page(
                nse, page, size, from_date, to_date, filing_type, options, retries, delay
            )
            data = response.get("data") or []
            records.extend(data)
            total = response.get("totalCount") or 0
            logger.info(
                "Fetched page %d: %d record(s) (%d/%d)", page, len(data), len(records), total
            )
            if not data or len(records) >= total:
                return records
            page += 1
            if delay:
                time.sleep(delay)

    @staticmethod
    def _fetch_page(nse, page, size, from_date, to_date, filing_type, options, retries, delay):
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return nse.corporate_integrated_filing(
                    index=options["segment"],
                    symbol=options["symbol"],
                    period_ended=options["period_ended"],
                    from_date=from_date,
                    to_date=to_date,
                    filing_type=filing_type,
                    page=page,
                    size=size,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Fetching page %d failed (attempt %d/%d): %s", page, attempt, retries, exc
                )
                if attempt < retries and delay:
                    time.sleep(delay * attempt)
        raise CommandError(f"fetching integrated filings failed on page {page}: {last_exc}")

    # ----------------------------------------------------------------- ingest

    def _run(self, from_date, to_date, options):
        records = self._fetch_all(from_date, to_date, options)
        if not records:
            logger.info("No integrated filings returned for %s to %s", from_date, to_date)
            return

        self._instrument_cache = {}
        self._symbol_cache = {}

        with transaction.atomic():
            rows, skipped = [], 0
            for raw in records:
                row = self._parse_record(raw)
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
            before = IntegratedFiling.objects.count()
            IntegratedFiling.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
            inserted = IntegratedFiling.objects.count() - before

        logger.info(
            "%d/%d record(s) ingested (%d duplicates skipped, %d unparseable)",
            inserted, len(records), len(rows) - inserted, skipped,
        )

    def _parse_record(self, raw):
        seq_id_raw = _clean(raw.get("seq_Id"))
        symbol = _clean(raw.get("symbol"))
        # Revisions come with broadcast_Date = null (their time is in
        # revised_Date); creation_Date is the last resort.
        broadcast_at = (
            _parse_dt(raw.get("broadcast_Date"))
            or _parse_dt(raw.get("revised_Date"))
            or _parse_dt(raw.get("creation_Date"))
        )
        qe_raw = _clean(raw.get("qe_Date"))
        if seq_id_raw is None or symbol is None or broadcast_at is None or qe_raw is None:
            logger.warning("skipping record missing seq_Id/symbol/date/qe_Date: %r", raw)
            return None
        try:
            seq_id = int(seq_id_raw)
        except ValueError:
            logger.warning("skipping record with non-numeric seq_Id: %r", raw)
            return None
        qe_dt = _parse_dt(qe_raw)
        if qe_dt is None:
            logger.warning("skipping record %s with unparseable qe_Date %r", seq_id, qe_raw)
            return None

        scope = (_clean(raw.get("consolidated")) or "").lower()
        consolidated = {"consolidated": True, "standalone": False}.get(scope)

        instrument = self._resolve_instrument(
            symbol, _clean(raw.get("cmName")) or _clean(raw.get("smName"))
        )
        return IntegratedFiling(
            instrument=instrument,
            seq_id=seq_id,
            filing_type=_clean(raw.get("type")) or "",
            type_sub=_clean(raw.get("type_Sub")) or "",
            period_end=qe_dt.date(),
            consolidated=consolidated,
            audited=_clean(raw.get("audited")) or "",
            broadcast_at=broadcast_at,
            revised_at=_parse_dt(raw.get("revised_Date")),
            revision_remark=_clean(raw.get("revision_Remark")) or "",
            company_name=_clean(raw.get("cmName")) or _clean(raw.get("smName")) or "",
            xbrl_url=_clean(raw.get("xbrl")) or "",
            ixbrl_url=_clean(raw.get("ixbrl")) or "",
            xbrl_file_size=_clean(raw.get("xbrlFileSize")) or "",
            raw=raw,
        )

    # ------------------------------------------------------ master data lookup

    def _resolve_instrument(self, symbol, name):
        """Symbol map first (newest mapping with an ISIN wins), else create."""
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
                isin=None, name=symbol, defaults={"instrument_type": "STK"}
            )
            nse_symbol = self._symbol_cache.get(symbol)
            if nse_symbol is None:
                nse_symbol, _ = NSESymbols.objects.get_or_create(symbol=symbol)
                self._symbol_cache[symbol] = nse_symbol
            NSESymbolInstrumentMap.objects.get_or_create(
                symbol=nse_symbol, instrument=instrument
            )
            logger.info("No instrument known for symbol %s; created ISIN-less %r", symbol, name)
        self._instrument_cache[symbol] = instrument
        return instrument

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
