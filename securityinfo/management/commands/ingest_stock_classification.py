"""Fill ``instrument_classification`` from the NSE live quote.

NSE publishes no bulk file with the Macro / Sector / Industry / Basic Industry
hierarchy; it is only in ``secInfo`` of the per-symbol live quote
(``NSELive.stock_quote``). So this command makes one throttled call per
instrument that has no classification yet.

Which symbol to ask for is taken from a UDiFF CM bhavcopy (it carries both
``ISIN`` and ``TckrSymb``), not from the database: ``nse_cm_price_history`` has
no symbol and ``nse_symbols_instrument_map`` has no "current" flag, so an
instrument that was renamed has several candidate symbols. The bhavcopy of a
recent trading day gives the live ``(ISIN, symbol, series)`` triples.

Flow:

1. Obtain the bhavcopy (``--file``, or download the latest one, walking back up
   to ``--lookback`` days; the legacy format has no ISIN and is rejected).
2. One ``(ISIN, symbol, series)`` per ISIN (``EQ`` preferred).
3. Drop ISINs with no ``Instrument`` row or that already have a classification
   (``not_found`` rows too, unless ``--retry-not-found``).
4. For each remaining one, ``stock_quote(symbol, series)``, ``--delay`` seconds
   apart. The ISIN in the answer must equal the bhavcopy ISIN, otherwise the
   result is logged and skipped (a reused symbol must not take another
   company's sector).

Outcomes per instrument: ``ok`` row (labels stored), ``not_found`` row (NSE has
no classification: bond, ETF, SME, ...), or nothing (transient error or ISIN
mismatch, so the next run retries). Each instrument is written in its own
transaction, so an interrupted run loses nothing. A run with failures finishes
the remaining instruments and then raises ``CommandError`` (exit 1).
"""

import csv
import logging
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, transaction
from django.utils import timezone

from corporateactions.management.commands.ingest_corporate_actions import (
    _set_request_timeout,
)
from securityinfo.models import Instrument, InstrumentClassification

logger = logging.getLogger(__name__)

# NSE needs fresh cookies now and then; recreate the session this often.
SESSION_REFRESH_EVERY = 300


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan", "null", "None") else value


def read_bhavcopy_pairs(path):
    """``{isin: (symbol, series)}`` from a UDiFF CM bhavcopy, ``EQ`` preferred."""
    pairs = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        if "TradDt" not in (reader.fieldnames or []):
            raise CommandError(
                f"{path}: not a UDiFF bhavcopy (legacy files carry no ISIN)"
            )
        for raw in reader:
            if (raw.get("FinInstrmTp") or "STK") != "STK":
                continue
            isin = _clean(raw.get("ISIN"))
            symbol = _clean(raw.get("TckrSymb"))
            series = _clean(raw.get("SctySrs")) or "EQ"
            if not isin or not symbol:
                continue
            if isin not in pairs or (series == "EQ" and pairs[isin][1] != "EQ"):
                pairs[isin] = (symbol, series)
    return pairs


def extract_classification(quote):
    """``(isin, labels)`` from a ``stock_quote`` answer; labels may be all None."""
    sec = quote.get("secInfo") or {}
    meta = quote.get("metaData") or {}
    labels = {
        "macro": _clean(sec.get("macro")),
        "sector": _clean(sec.get("sector")),
        "industry": _clean(sec.get("industryInfo")),
        "basic_industry": _clean(sec.get("basicIndustry")),
    }
    return _clean(meta.get("isinCode")), labels


class Command(BaseCommand):
    help = "Fetch NSE industry classification for instruments that have none yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", metavar="PATH",
            help="use this UDiFF bhavcopy instead of downloading one",
        )
        parser.add_argument(
            "--date", dest="on_date", metavar="YYYY-MM-DD",
            help="download this day's bhavcopy (default: latest available)",
        )
        parser.add_argument(
            "--lookback", type=int, default=7,
            help="days to walk back looking for a bhavcopy (default: 7)",
        )
        parser.add_argument("--symbol", metavar="SYM", help="only this NSE symbol")
        parser.add_argument(
            "--limit", type=int, metavar="N", help="look up at most N instruments"
        )
        parser.add_argument(
            "--retry-not-found", action="store_true",
            help="also retry instruments previously recorded as not_found",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="list what would be looked up; no quote calls, no writes",
        )
        parser.add_argument(
            "--delay", type=float, default=3.0,
            help="seconds between quote calls; also retry back-off (default: 3.0)",
        )
        parser.add_argument(
            "--retries", type=int, default=3,
            help="total attempts per instrument (default: 3)",
        )
        parser.add_argument(
            "--timeout", type=float, default=60.0,
            help="per-request timeout in seconds (default: 60)",
        )

    def handle(self, *args, **options):
        if options["delay"] < 0:
            raise CommandError("--delay must be zero or positive")
        if options["retries"] < 1:
            raise CommandError("--retries must be at least 1")
        if options["timeout"] <= 0:
            raise CommandError("--timeout must be positive")
        self.delay = options["delay"]
        self.retries = options["retries"]
        self.timeout = options["timeout"]

        with tempfile.TemporaryDirectory() as tmp:
            if options["file"]:
                path = Path(options["file"])
                if not path.is_file():
                    raise CommandError(f"{path} does not exist")
            else:
                path = self._download_bhavcopy(
                    Path(tmp), options["on_date"], options["lookback"]
                )
            pairs = read_bhavcopy_pairs(path)
        logger.info("Bhavcopy %s: %d distinct ISINs", path.name, len(pairs))

        todo = self._select(pairs, options)
        logger.info("%d instruments need classification", len(todo))
        if options["dry_run"]:
            for instrument, symbol, series in todo:
                self.stdout.write(f"{instrument.isin}\t{symbol}\t{series}")
            return
        if todo:
            self._lookup(todo)

    # ------------------------------------------------------------- bhavcopy

    def _download_bhavcopy(self, directory, on_date, lookback):
        from jugaad_data.nse.archives import NSEArchives

        if on_date:
            try:
                days = [datetime.strptime(on_date, "%Y-%m-%d").date()]
            except ValueError:
                raise CommandError("--date must be YYYY-MM-DD")
        else:
            days = [date.today() - timedelta(days=i) for i in range(lookback)]
        archives = NSEArchives()
        for day in days:
            if day.weekday() >= 5:
                continue
            try:
                saved = Path(archives.bhavcopy_save(day, str(directory)))
                header = saved.read_text(encoding="utf-8", errors="replace")[:300]
            except Exception as exc:
                logger.info("%s: no bhavcopy (%s)", day, exc)
                continue
            if "TradDt" in header:
                logger.info("Using bhavcopy of %s", day)
                return saved
            saved.unlink(missing_ok=True)
            logger.info("%s: not a UDiFF bhavcopy, skipping", day)
        raise CommandError("could not obtain a UDiFF bhavcopy")

    # ------------------------------------------------------------ selection

    def _select(self, pairs, options):
        skip_statuses = [InstrumentClassification.STATUS_OK]
        if not options["retry_not_found"]:
            skip_statuses.append(InstrumentClassification.STATUS_NOT_FOUND)
        done = set(
            InstrumentClassification.objects.filter(status__in=skip_statuses)
            .values_list("instrument__isin", flat=True)
        )
        instruments = {
            i.isin: i for i in Instrument.objects.filter(isin__in=list(pairs))
        }
        todo = []
        for isin, (symbol, series) in sorted(pairs.items(), key=lambda kv: kv[1][0]):
            instrument = instruments.get(isin)
            if instrument is None or isin in done:
                continue
            if options["symbol"] and symbol != options["symbol"].upper():
                continue
            todo.append((instrument, symbol, series))
        if options["limit"] is not None:
            todo = todo[: options["limit"]]
        return todo

    # --------------------------------------------------------------- lookup

    def _new_session(self):
        from jugaad_data.nse.live import NSELive

        nse = NSELive()
        _set_request_timeout(nse, self.timeout)
        return nse

    def _quote(self, nse, symbol, series):
        """The quote, or ``None`` when NSE has no data. Raises on transient errors."""
        try:
            return nse.stock_quote(symbol, series=series)
        except (IndexError, KeyError, TypeError):
            return None  # empty equityResponse: unknown / delisted / not equity

    def _lookup(self, todo):
        nse = self._new_session()
        counts = {"ok": 0, "not_found": 0, "mismatch": 0, "failed": 0}
        for n, (instrument, symbol, series) in enumerate(todo):
            if n:
                if self.delay:
                    time.sleep(self.delay)
                if n % SESSION_REFRESH_EVERY == 0:
                    nse = self._new_session()
            outcome = self._lookup_one(nse, instrument, symbol, series)
            counts[outcome] += 1
            if (n + 1) % 100 == 0:
                logger.info("Progress %d/%d %s", n + 1, len(todo), counts)
        logger.info(
            "Classification done: %d ok, %d not_found, %d ISIN mismatch, %d failed",
            counts["ok"], counts["not_found"], counts["mismatch"], counts["failed"],
        )
        if counts["failed"] or counts["mismatch"]:
            raise CommandError(
                f"{counts['failed']} lookups failed, {counts['mismatch']} ISIN "
                "mismatches; a re-run retries them"
            )

    def _lookup_one(self, nse, instrument, symbol, series):
        quote, error = None, None
        for attempt in range(1, self.retries + 1):
            try:
                quote = self._quote(nse, symbol, series)
                error = None
                break
            except Exception as exc:
                error = exc
                if attempt < self.retries:
                    logger.warning(
                        "%s: attempt %d/%d failed (%s), retrying",
                        symbol, attempt, self.retries, exc,
                    )
                    time.sleep(self.delay)
        if error is not None:
            logger.error("%s: lookup failed after %d attempts (%s)", symbol, self.retries, error)
            return "failed"

        labels = {"macro": None, "sector": None, "industry": None, "basic_industry": None}
        status = InstrumentClassification.STATUS_NOT_FOUND
        if quote is not None:
            isin, found = extract_classification(quote)
            if isin and isin != instrument.isin:
                logger.error(
                    "%s: NSE answered ISIN %s but bhavcopy says %s; skipped",
                    symbol, isin, instrument.isin,
                )
                return "mismatch"
            if any(found.values()):
                labels, status = found, InstrumentClassification.STATUS_OK
        if not self._save(instrument, symbol, labels, status):
            logger.error(
                "%s: database still locked after %d attempts",
                symbol, self.retries,
            )
            return "failed"
        logger.info("%s (%s): %s %s", symbol, instrument.isin, status, labels["basic_industry"] or "")
        return "ok" if status == InstrumentClassification.STATUS_OK else "not_found"

    def _save(self, instrument, symbol, labels, status):
        """Write one row, retrying transient SQLite lock errors.

        The corporate-announcements job writes every minute, and a read-then-
        write upsert inside ``atomic`` can hit ``SQLITE_BUSY_SNAPSHOT`` (which
        bypasses ``busy_timeout``). Back off and retry; each instrument is
        independent, so the caller skips it after the last attempt and a re-run
        picks it up.
        """
        for attempt in range(1, self.retries + 1):
            try:
                with transaction.atomic():
                    InstrumentClassification.objects.update_or_create(
                        instrument=instrument,
                        defaults={
                            **labels,
                            "symbol_used": symbol,
                            "status": status,
                            "fetched_at": timezone.now(),
                        },
                    )
                return True
            except OperationalError:
                logger.warning(
                    "%s: database locked (attempt %d/%d)",
                    symbol,
                    attempt,
                    self.retries,
                )
                if attempt < self.retries:
                    time.sleep(max(1.0, self.delay) * attempt)
        return False
