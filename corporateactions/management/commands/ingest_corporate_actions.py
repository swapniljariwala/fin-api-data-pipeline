"""Fetch NSE corporate actions and ingest them into the database.

Like corporate announcements, corporate actions are a **live API, not a dated
archive file** (``NSELive.corporate_actions``); the API filters by *ex-date*.
Each record is a flat dict whose economics are only in the free-text
``subject``, which ``corporateactions.parsing`` decodes into legs
(dividend / split / bonus / rights / ...). Anything not decoded completely, or
whose split/bonus factor disagrees with the real price move
(``corporateactions.price_check``), is stored with ``status=needs_review``.

Mode selection (exactly one):

* ``--latest`` (default): ex-dates from ``--lookback`` days ago to ``--ahead``
  days from now (upcoming actions are announced in advance).
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive ex-date range.
* ``--days N``: last N calendar days ending today.

Long ranges are fetched in ``--chunk-days`` windows, oldest first.

Idempotency: an action is keyed on ``(instrument, ex_date, subject)``; a
re-run updates dates/raw when NSE amended them and re-parses rows written by an
older parser version, but never changes ``verified`` status.

Instruments are resolved via ISIN (falling back to symbol) like
``ingest_corporate_announcements``.
"""

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from requests.adapters import HTTPAdapter
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from corporateactions import price_check
from corporateactions.models import CorporateAction, CorporateActionLeg
from corporateactions.parsing import PARSER_VERSION, normalize_subject, parse_subject
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

logger = logging.getLogger(__name__)

# Actions without price data yet (ex-date just passed, bhavcopy not ingested)
# are re-checked on later runs for this many days after the ex-date.
RECHECK_DAYS = 45


class _TimeoutAdapter(HTTPAdapter):
    """Give every request a timeout; jugaad-data's session has none."""

    def __init__(self, timeout, *args, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def _set_request_timeout(nse, seconds):
    """Without this a stalled NSE response hangs the run (and its cron flock) forever."""
    adapter = _TimeoutAdapter((10, seconds))
    nse.s.mount("https://", adapter)
    nse.s.mount("http://", adapter)


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan") else value


class Command(BaseCommand):
    help = "Fetch NSE corporate actions and ingest them into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="ex-dates from --lookback days ago to --ahead days ahead (default)",
        )
        mode.add_argument(
            "--from",
            dest="from_date",
            metavar="YYYY-MM-DD",
            help="start of inclusive ex-date range",
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
            help="end of inclusive ex-date range (default: today)",
        )
        parser.add_argument(
            "--lookback",
            type=int,
            default=7,
            help="calendar days back for --latest (default: 7)",
        )
        parser.add_argument(
            "--ahead",
            type=int,
            default=30,
            help="calendar days ahead for --latest (default: 30)",
        )
        parser.add_argument(
            "--symbol",
            metavar="SYM",
            help="restrict to actions for this NSE symbol",
        )
        parser.add_argument(
            "--segment",
            default="equities",
            help="NSE segment to query (default: equities)",
        )
        parser.add_argument(
            "--chunk-days",
            type=int,
            default=90,
            help="split long ranges into windows of this many days (default: 90)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="seconds to wait between chunks; 0 disables (default: 1.0)",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=60.0,
            help="seconds to wait for NSE to answer a request (default: 60)",
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=3,
            help="total attempts per chunk (default: 3)",
        )

    def handle(self, *args, **options):
        today = date.today()
        if options["days"] is not None:
            if options["days"] <= 0:
                raise CommandError("--days must be a positive integer")
            from_date = today - timedelta(days=options["days"] - 1)
            to_date = today
        elif options["from_date"]:
            from_date = self._parse_date(options["from_date"], "--from")
            to_date = self._parse_date(options["to_date"] or today.isoformat(), "--to")
            if from_date > to_date:
                raise CommandError("--from must be on or before --to")
        else:
            if options["to_date"]:
                raise CommandError("--to requires --from")
            if options["lookback"] <= 0:
                raise CommandError("--lookback must be a positive integer")
            if options["ahead"] < 0:
                raise CommandError("--ahead must not be negative")
            from_date = today - timedelta(days=options["lookback"] - 1)
            to_date = today + timedelta(days=options["ahead"])
        if options["chunk_days"] <= 0:
            raise CommandError("--chunk-days must be a positive integer")
        if options["retries"] <= 0:
            raise CommandError("--retries must be a positive integer")
        if options["timeout"] <= 0:
            raise CommandError("--timeout must be positive")

        logger.info(
            "Corporate actions ingest job started "
            "(from=%s, to=%s, segment=%s, symbol=%s)",
            from_date, to_date, options["segment"], options["symbol"],
        )
        started = time.monotonic()
        try:
            self._run(from_date, to_date, options)
        finally:
            logger.info(
                "Corporate actions ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    def _run(self, from_date, to_date, options):
        from jugaad_data.nse.live import NSELive

        run_started = timezone.now()
        self._instrument_cache = {}
        self._symbol_cache = {}
        self._symbol_map_cache = set()
        stats ={"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        touched = set()
        failed_windows = []

        windows = list(self._windows(from_date, to_date, options["chunk_days"]))
        nse = NSELive()
        _set_request_timeout(nse, options["timeout"])
        for index, (start, end) in enumerate(windows):
            if index and options["delay"] > 0:
                time.sleep(options["delay"])
            try:
                records = self._fetch(nse, start, end, options)
                with transaction.atomic():
                    self._ingest_records(records, stats, touched)
            except Exception:
                logger.exception("window %s to %s failed", start, end)
                failed_windows.append((start, end))

        counts = self._run_price_checks(touched)
        review = CorporateAction.objects.filter(
            status=CorporateAction.STATUS_NEEDS_REVIEW, updated_at__gte=run_started
        ).count()
        logger.info(
            "Corporate actions: %d created, %d updated, %d unchanged, %d skipped; "
            "price checks %s; %d need review",
            stats["created"], stats["updated"], stats["unchanged"],
            stats["skipped"], counts, review,
        )
        if failed_windows:
            spans = ", ".join(f"{s}..{e}" for s, e in failed_windows)
            raise CommandError(f"{len(failed_windows)} window(s) failed: {spans}")

    # ------------------------------------------------------------------ fetch

    @staticmethod
    def _windows(from_date, to_date, chunk_days):
        start = from_date
        while start <= to_date:
            end = min(start + timedelta(days=chunk_days - 1), to_date)
            yield start, end
            start = end + timedelta(days=1)

    def _fetch(self, nse, start, end, options):
        last_error = None
        for attempt in range(1, options["retries"] + 1):
            try:
                result = nse.corporate_actions(
                    segment=options["segment"],
                    from_date=start,
                    to_date=end,
                    symbol=options["symbol"],
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "fetch %s..%s attempt %d/%d failed: %s",
                    start, end, attempt, options["retries"], exc,
                )
                if attempt < options["retries"] and options["delay"] > 0:
                    time.sleep(options["delay"] * attempt)
        else:
            raise CommandError(f"fetching corporate actions failed: {last_error}")

        # NSE answers "nothing here" with {"data": [], "msg": "no data found"}.
        records = result if isinstance(result, list) else result.get("data") or []
        logger.info("Fetched %d record(s) for %s..%s", len(records), start, end)
        return records

    # ----------------------------------------------------------------- ingest

    def _ingest_records(self, records, stats, touched):
        parsed = []
        for raw in records:
            row = self._parse_record(raw)
            if row is None:
                stats["skipped"] += 1
                continue
            parsed.append(row)
        if not parsed:
            return

        existing = {
            (a.instrument_id, a.ex_date, a.subject): a
            for a in CorporateAction.objects.filter(
                ex_date__gte=min(r["ex_date"] for r in parsed),
                ex_date__lte=max(r["ex_date"] for r in parsed),
            )
        }
        for row in parsed:
            instrument = self._resolve_instrument(
                row.pop("isin"), row["company_name"], row["symbol"]
            )
            key = (instrument.id, row["ex_date"], row["subject"])
            action = existing.get(key)
            if action is None:
                action = CorporateAction(instrument=instrument, **row)
                self._apply_parse(action)
                action.refresh_status()
                action.save()
                self._write_legs(action)
                existing[key] = action
                stats["created"] += 1
            elif self._update_existing(action, row):
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
            touched.add((instrument.id, row["ex_date"]))

    def _update_existing(self, action, row):
        changed = []
        for field, value in row.items():
            if getattr(action, field) != value:
                setattr(action, field, value)
                changed.append(field)
        reparse = (
            action.parser_version != PARSER_VERSION
            and action.status != CorporateAction.STATUS_VERIFIED
        )
        if reparse:
            self._apply_parse(action)
            changed += ["parse_problems", "parser_version"]
        if not changed:
            return False
        action.refresh_status()
        action.save()
        if reparse:
            action.legs.all().delete()
            self._write_legs(action)
        return True

    @staticmethod
    def _apply_parse(action):
        legs, problems = parse_subject(action.subject)
        action._parsed_legs = legs
        action.parse_problems = problems
        action.parser_version = PARSER_VERSION

    @staticmethod
    def _write_legs(action):
        CorporateActionLeg.objects.bulk_create(
            CorporateActionLeg(action=action, ordinal=i, **leg)
            for i, leg in enumerate(action._parsed_legs)
        )

    def _parse_record(self, raw):
        subject = normalize_subject(raw.get("subject"))
        symbol = _clean(raw.get("symbol"))
        ex_date = self._parse_nse_date(raw.get("exDate"))
        if not subject or not symbol or ex_date is None:
            logger.warning("skipping record missing subject/symbol/exDate: %r", raw)
            return None
        return {
            "isin": _clean(raw.get("isin")),
            "symbol": symbol,
            "series": _clean(raw.get("series")) or "",
            "company_name": _clean(raw.get("comp")) or "",
            "subject": subject,
            "face_value": self._parse_decimal(raw.get("faceVal")),
            "ex_date": ex_date,
            "record_date": self._parse_nse_date(raw.get("recDate")),
            "book_closure_start": self._parse_nse_date(raw.get("bcStartDate")),
            "book_closure_end": self._parse_nse_date(raw.get("bcEndDate")),
            "raw": raw,
        }

    def _run_price_checks(self, touched):
        """Check this run's ex-date groups plus recent ones still lacking prices."""
        groups = set(touched)
        cutoff = date.today() - timedelta(days=RECHECK_DAYS)
        groups.update(
            CorporateAction.objects.filter(
                price_check=CorporateAction.PRICE_NO_DATA,
                ex_date__gte=cutoff,
                ex_date__lte=date.today(),
            ).values_list("instrument_id", "ex_date")
        )
        return price_check.check_groups(groups)

    # ------------------------------------------------------ master data lookup

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
    def _parse_nse_date(raw):
        value = _clean(raw)
        if value is None:
            return None
        try:
            return datetime.strptime(value, "%d-%b-%Y").date()
        except ValueError:
            logger.warning("unparseable NSE date %r, treating as missing", value)
            return None

    @staticmethod
    def _parse_decimal(raw):
        value = _clean(raw)
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
