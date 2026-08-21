"""Download NSE CM bhavcopy files and ingest them into the database.

Bhavcopies are downloaded with the ``jugaad-data`` library (``NSEArchives``),
parsed in either the legacy (pre-8-Jul-2024) or UDiFF format, and upserted
into ``cm_price_history`` per ``spec/schema.md``.

The command is restart-safe:

* A file whose name already exists in ``bhavcopy_files`` is skipped entirely.
* Each file is ingested inside one transaction: the ``bhavcopy_files`` row is
  only committed together with its price rows, so an interrupted run never
  leaves a file marked as ingested without its data.
* Price rows are inserted with ``ignore_conflicts`` on the unique key
  ``(instrument_ticker, trade_date, series)``, so re-running never duplicates
  entries already present.

Mode selection (exactly one):

* ``--latest``: most recent bhavcopy not yet ingested (walks back from today).
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive date range, fetched
  newest-first.
* ``--days N``: last N calendar days ending today, fetched newest-first.
* ``--upload``: after each successful ingest, upload the bhavcopy to the
  object store (placeholder in ``dailypricehistory.object_store``; logs a
  warning until it is implemented).
* ``--delay SECONDS``: wait this long between successive NSE API downloads
  to stay clear of the rate limit (default: 2.0; 0 disables the wait).
* ``--retries N``: retry a failed download up to N times total, backing off
  by ``--delay`` between attempts (default: 3). NSE endpoints are flaky and
  ``jugaad-data`` can crash on a transient timeout, so a retry usually
  succeeds.

After a download exhausts its retries the command consults NSE's live
trading-holiday calendar (fetched once per run) to tell apart a market
holiday (no bhavcopy is expected, logged as INFO) from a genuine download
failure (logged as WARNING so it stands out). If the holiday API itself is
unreachable, the failure is reported as an error to be on the safe side.

The downloaded file is deleted from disk once it has been ingested (and
uploaded, when ``--upload`` is given), so nothing is kept on disk after the
run; restart-safety comes from the ``bhavcopy_files`` table, not from files.

Progress and errors are logged to console and to ``logs/pipeline.log`` via the
project-level ``LOGGING`` configuration.
"""

import csv
import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dailypricehistory.models import BhavcopyFile, CmPriceHistory
from dailypricehistory.object_store import upload_bhavcopy_to_object_store
from securityinfo.models import Instrument, InstrumentTicker, Series

SOURCE = "NSE"
SEGMENT = "CM"

logger = logging.getLogger(__name__)

SERIES_DESCRIPTIONS = {
    "EQ": "Fully paid equity shares / ETFs (rolling settlement)",
    "BE": "Trade-for-trade (surveillance)",
    "BZ": "Trade-for-trade Z category (listing non-compliance)",
    "SM": "Fully paid equity SME (rolling settlement)",
    "ST": "SME trade-for-trade",
    "SZ": "SME trade-for-trade Z category",
    "GB": "Gold Bonds",
    "GS": "Government Securities",
    "IV": "Units of InvITs",
    "BL": "Block deals (gross settlement, no netting)",
}


def _clean(value):
    """Normalize a raw CSV cell: whitespace, ``-``/``NA``/empty -> None."""
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan") else value


def _dec(value):
    value = _clean(value)
    return Decimal(value) if value is not None else None


def _int(value):
    value = _clean(value)
    return int(value) if value is not None else None


class Command(BaseCommand):
    help = "Download NSE CM bhavcopy files and ingest them into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="ingest the most recent bhavcopy not yet ingested "
            "(default; walks back from today up to --lookback days)",
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
            "--data-dir",
            dest="data_dir",
            help="directory for downloaded bhavcopy files "
            f"(default: {settings.BASE_DIR / 'data'})",
        )
        parser.add_argument(
            "--no-download",
            action="store_true",
            help="do not download; ingest only files already present in --data-dir",
        )
        parser.add_argument(
            "--lookback",
            type=int,
            default=30,
            help="calendar days to walk back for --latest (default: 30)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=2.0,
            metavar="SECONDS",
            help="wait this many seconds between NSE API calls "
            "(default: 2.0; 0 disables)",
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=3,
            metavar="N",
            help="retry a failed download up to N times total, "
            "backing off by --delay between attempts (default: 3)",
        )
        parser.add_argument(
            "--upload",
            action="store_true",
            help="upload each ingested bhavcopy to the object store after "
            "ingestion (not implemented yet)",
        )

    def handle(self, *args, **options):
        self.data_dir = Path(options["data_dir"] or settings.BASE_DIR / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.no_download = options["no_download"]
        self.upload = options["upload"]
        self.delay = options["delay"]
        if self.delay < 0:
            raise CommandError(
                "--delay must be zero or a positive number of seconds"
            )
        self.retries = options["retries"]
        if self.retries < 1:
            raise CommandError("--retries must be at least 1")
        self._api_calls = 0
        self._download_ok = True
        self._holiday_dates = None  # lazy set of NSE CM trading holidays
        self.archives = None
        if not self.no_download:
            from jugaad_data.nse.archives import NSEArchives

            self.archives = NSEArchives()

        if options["days"] is not None:
            mode = "days"
            targets = self._resolve_days(options["days"])
        elif options["from_date"]:
            mode = "range"
            targets = self._resolve_range(
                options["from_date"], options["to_date"]
            )
        else:
            if options["to_date"]:
                raise CommandError("--to requires --from")
            mode = "latest"

        logger.info(
            "Bhavcopy ingest job started (mode=%s, download=%s, upload=%s)",
            mode, not self.no_download, self.upload,
        )
        started = time.monotonic()
        try:
            if mode == "latest":
                self._run_latest(options["lookback"])
            else:
                self._run_dates(targets)
        finally:
            logger.info(
                "Bhavcopy ingest job finished in %.1fs",
                time.monotonic() - started,
            )

    # ------------------------------------------------------------------ modes

    def _resolve_days(self, days):
        if days <= 0:
            raise CommandError("--days must be a positive integer")
        start = date.today() - timedelta(days=days - 1)
        return self._target_dates(start, date.today())

    def _resolve_range(self, from_raw, to_raw):
        from_date = self._parse_date(from_raw, "--from")
        to_date = self._parse_date(to_raw or date.today().isoformat(), "--to")
        if from_date > to_date:
            raise CommandError("--from must be on or before --to")
        return self._target_dates(from_date, to_date)

    def _target_dates(self, from_date, to_date):
        # Newest first: most recent data lands in the DB first, and if the
        # run is interrupted the remaining dates are the older ones.
        return list(self._iter_weekdays(from_date, to_date))[::-1]

    def _run_latest(self, lookback):
        logger.info(
            "Looking for the latest bhavcopy not yet ingested "
            "(walking back up to %d days)",
            lookback,
        )
        for i in range(lookback):
            day = date.today() - timedelta(days=i)
            if day.weekday() >= 5:  # weekend, NSE is closed
                continue
            if self._already_ingested(day):
                continue
            self._download_ok = True
            path = self._obtain_file(day)
            if path is None:
                continue
            self._ingest_and_upload(path, day)
            return
        logger.info(
            "Nothing to ingest: recent bhavcopies are already ingested "
            "or unavailable"
        )

    def _run_dates(self, targets):
        summary = {"ingested": 0, "skipped": 0, "failed": 0}
        logger.info(
            "Processing %d trading day(s), newest first", len(targets)
        )
        for day in targets:
            if self._already_ingested(day):
                logger.info("%s: already ingested, skipping", day)
                summary["skipped"] += 1
                continue
            try:
                self._download_ok = True
                path = self._obtain_file(day)
                if path is None:
                    if self._download_ok:
                        logger.info(
                            "%s: no bhavcopy available (holiday?)", day
                        )
                    continue
                self._ingest_and_upload(path, day)
                summary["ingested"] += 1
            except Exception:  # keep the run going; restart re-tries
                summary["failed"] += 1
                logger.exception("%s: ingestion failed", day)
        if summary["failed"]:
            logger.error(
                "Job finished with failures: %d ingested, %d skipped, %d failed",
                summary["ingested"], summary["skipped"], summary["failed"],
            )
            raise CommandError(
                f"{summary['failed']} file(s) failed; re-run to retry"
            )
        logger.info(
            "Job completed successfully: %d ingested, %d skipped",
            summary["ingested"], summary["skipped"],
        )

    # ------------------------------------------------------------- downloading

    def _already_ingested(self, day):
        return BhavcopyFile.objects.filter(
            file_name=self._file_name(day)
        ).exists()

    def _file_name(self, day):
        return f"cm{day.strftime('%d%b%Y')}bhav.csv"

    def _obtain_file(self, day):
        """Return the path of the bhavcopy for ``day``, or None if unavailable."""
        path = self.data_dir / self._file_name(day)
        if self.no_download:
            return path if path.is_file() else None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                saved = self.archives.bhavcopy_save(
                    day, str(self.data_dir), skip_if_present=True
                )
                break
            except Exception as exc:
                if attempt < self.retries:
                    logger.warning(
                        "%s: download attempt %d/%d failed (%s), retrying",
                        day, attempt, self.retries, exc,
                    )
                    continue
                self._download_ok = False
                if self._is_nse_holiday(day):
                    logger.info(
                        "%s: NSE trading holiday, no bhavcopy expected", day
                    )
                    return None
                logger.warning(
                    "%s: download failed after %d attempt(s) (%s); "
                    "not an NSE holiday",
                    day, attempt, exc,
                )
                return None
        path = Path(saved)
        if not path.is_file():
            self._download_ok = False
            return None
        if not self._looks_like_bhavcopy(path):
            # Likely an error page saved by a failed download; drop it so the
            # next run re-downloads instead of ingesting garbage.
            logger.warning(
                "%s: downloaded file is not a valid bhavcopy, discarding", day
            )
            path.unlink(missing_ok=True)
            self._download_ok = False
            return None
        return path

    def _is_nse_holiday(self, day):
        """Whether ``day`` is a declared NSE CM trading holiday.

        The holiday calendar is fetched once per run, lazily, from NSE's live
        holiday-master API. If the API is unreachable we log a warning and
        return False so the failure is treated as a genuine download error.
        """
        if self._holiday_dates is None:
            self._holiday_dates = set()
            self._throttle()
            try:
                from jugaad_data.nse.live import NSELive

                data = NSELive().holiday_list()
            except Exception as exc:
                logger.warning(
                    "Could not fetch NSE holiday calendar (%s); treating "
                    "failed downloads as errors",
                    exc,
                )
                return False
            for holiday in data.get("CM", []):
                raw = holiday.get("tradingDate")
                try:
                    hday = datetime.strptime(
                        raw, "%d-%b-%Y"
                    ).date()
                except (TypeError, ValueError):
                    continue
                self._holiday_dates.add(hday)
        return day in self._holiday_dates

    def _throttle(self):
        """Sleep before NSE API calls after the first one in a run."""
        if self._api_calls > 0 and self.delay > 0:
            logger.info(
                "Waiting %.1fs before next NSE API call", self.delay
            )
            time.sleep(self.delay)
        self._api_calls += 1

    def _looks_like_bhavcopy(self, path):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                header = fh.readline()
        except (OSError, UnicodeDecodeError):
            return False
        return "TradDt" in header or "SYMBOL" in header

    # ---------------------------------------------------------------- parsing

    def _validate_csv_date(self, raw, expected, fmt):
        value = _clean(raw)
        try:
            if fmt == "legacy":
                parsed = datetime.strptime(value, "%d-%b-%Y").date()
            else:
                parsed = date.fromisoformat(value)
        except (ValueError, TypeError):
            parsed = expected
        if parsed != expected:
            logger.warning(
                "CSV trade date %s != expected %s", parsed, expected
            )

    def _parse_rows(self, path, fmt, expected_date):
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, skipinitialspace=True)
            first = next(reader, None)
            if first is None:
                return rows
            date_col = "DATE1" if fmt == "legacy" else "TradDt"
            self._validate_csv_date(
                first.get(date_col), expected_date, fmt
            )
            for raw in [first, *reader]:
                if fmt == "legacy":
                    row = self._parse_legacy_row(raw, expected_date)
                else:
                    row = self._parse_udiff_row(raw, expected_date)
                if row is not None:
                    rows.append(row)
        return rows

    def _parse_legacy_row(self, raw, expected_date):
        return {
            "ticker": (raw.get("SYMBOL") or "").strip(),
            "series": (raw.get("SERIES") or "").strip(),
            "trade_date": expected_date,
            "isin": None,
            "name": None,
            "fin_instrm_id": None,
            "open": _dec(raw.get("OPEN_PRICE")),
            "high": _dec(raw.get("HIGH_PRICE")),
            "low": _dec(raw.get("LOW_PRICE")),
            "close": _dec(raw.get("CLOSE_PRICE")),
            "last_price": _dec(raw.get("LAST_PRICE")),
            "prev_close": _dec(raw.get("PREV_CLOSE")),
            "volume": _int(raw.get("TTL_TRD_QNTY")),
            "turnover": self._legacy_turnover(raw.get("TURNOVER_LACS")),
            "num_trades": _int(raw.get("NO_OF_TRADES")),
            "settlement_price": None,
        }

    def _legacy_turnover(self, raw):
        value = _dec(raw)  # lakhs
        return value * Decimal("100000") if value is not None else None

    def _parse_udiff_row(self, raw, expected_date):
        if (raw.get("FinInstrmTp") or "STK") != "STK":
            return None  # guard: only STK instruments are ingested
        return {
            "ticker": (raw.get("TckrSymb") or "").strip(),
            "series": (raw.get("SctySrs") or "").strip(),
            "trade_date": expected_date,
            "isin": _clean(raw.get("ISIN")),
            "name": _clean(raw.get("FinInstrmNm")),
            "fin_instrm_id": _int(raw.get("FinInstrmId")),
            "open": _dec(raw.get("OpnPric")),
            "high": _dec(raw.get("HghPric")),
            "low": _dec(raw.get("LwPric")),
            "close": _dec(raw.get("ClsPric")),
            "last_price": _dec(raw.get("LastPric")),
            "prev_close": _dec(raw.get("PrvsClsgPric")),
            "volume": _int(raw.get("TtlTradgVol")),
            "turnover": _dec(raw.get("TtlTrfVal")),  # already full rupees
            "num_trades": _int(raw.get("TtlNbOfTxsExctd")),
            "settlement_price": _dec(raw.get("SttlmPric")),
        }

    # --------------------------------------------------------------- ingesting

    def _ingest_and_upload(self, path, day):
        """Ingest one file, optionally upload it, then delete it from disk."""
        self._ingest_file(path, day)
        if self.upload:
            try:
                upload_bhavcopy_to_object_store(str(path))
            except NotImplementedError as exc:
                logger.warning(
                    "%s: object store upload not implemented yet, skipped (%s)",
                    path.name, exc,
                )
            else:
                logger.info("%s: uploaded to object store", path.name)
        path.unlink(missing_ok=True)
        logger.info("%s: removed from disk", path.name)

    def _ingest_file(self, path, trade_date):
        fmt = self._detect_format(path)
        logger.info(
            "Ingesting %s (format=%s, trade_date=%s)",
            path.name, fmt, trade_date,
        )
        with transaction.atomic():
            file_obj = BhavcopyFile.objects.create(
                file_name=path.name,
                source=SOURCE,
                segment=SEGMENT,
                format=fmt,
                trade_date=trade_date,
            )
            rows = self._parse_rows(path, fmt, trade_date)
            self._series_cache = {}
            self._instruments_by_isin = {}
            self._tickers = {}
            price_rows = []
            for row in rows:
                ticker = self._resolve_ticker(
                    row["ticker"],
                    isin=row["isin"],
                    name=row["name"],
                    fin_id=row["fin_instrm_id"],
                )
                series = self._resolve_series(row["series"])
                price_rows.append(
                    CmPriceHistory(
                        instrument_ticker=ticker,
                        trade_date=row["trade_date"],
                        series=series,
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        last_price=row["last_price"],
                        prev_close=row["prev_close"],
                        volume=row["volume"],
                        turnover=row["turnover"],
                        num_trades=row["num_trades"],
                        settlement_price=row["settlement_price"],
                        file=file_obj,
                    )
                )
            created = CmPriceHistory.objects.bulk_create(
                price_rows, ignore_conflicts=True, batch_size=1000
            )
            file_obj.row_count = len(created)
            file_obj.save(update_fields=["row_count"])
        logger.info(
            "%s: %d/%d rows ingested (%d duplicates skipped)",
            path.name, len(created), len(rows), len(rows) - len(created),
        )

    def _detect_format(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            header = fh.readline()
        if "TradDt" in header:
            return "udiff"
        if "SYMBOL" in header:
            return "legacy"
        raise ValueError(f"unrecognized bhavcopy header: {header[:80]!r}")

    # ------------------------------------------------------ master data lookup

    def _resolve_series(self, code):
        code = (code or "").strip().upper()
        if code not in self._series_cache:
            self._series_cache[code] = Series.objects.get_or_create(
                code=code,
                defaults={
                    "description": SERIES_DESCRIPTIONS.get(
                        code, f"NSE series {code}"
                    )
                },
            )[0]
        return self._series_cache[code]

    def _resolve_instrument_by_isin(self, isin, name):
        instrument = self._instruments_by_isin.get(isin)
        if instrument is None:
            instrument, _ = Instrument.objects.get_or_create(
                isin=isin,
                defaults={"name": name or "", "instrument_type": "STK"},
            )
            self._instruments_by_isin[isin] = instrument
        if name and not instrument.name:
            instrument.name = name
            instrument.save(update_fields=["name"])
        return instrument

    def _resolve_ticker(self, ticker, isin=None, name=None, fin_id=None):
        key = (SOURCE, ticker)
        cached = self._tickers.get(key)
        if cached is not None:
            return cached

        try:
            obj = InstrumentTicker.objects.get(source=SOURCE, ticker=ticker)
        except InstrumentTicker.DoesNotExist:
            obj = None

        if obj is None:
            if isin:
                instrument = self._resolve_instrument_by_isin(isin, name)
            else:
                # Legacy rows have no ISIN: identity falls back to
                # (source, ticker); the instrument stays ISIN-less until a
                # later UDiFF row upgrades it.
                instrument, _ = Instrument.objects.get_or_create(
                    isin=None,
                    name=ticker or "",
                    defaults={"instrument_type": "STK"},
                )
            obj = InstrumentTicker.objects.create(
                instrument=instrument,
                source=SOURCE,
                ticker=ticker,
                fin_instrm_id=fin_id,
            )
        else:
            # A UDiFF row upgrades an ISIN-less instrument created earlier.
            if isin:
                instrument = self._resolve_instrument_by_isin(isin, name)
                if obj.instrument_id != instrument.id:
                    obj.instrument = instrument
                    obj.save(update_fields=["instrument"])
            if fin_id and obj.fin_instrm_id != fin_id:
                obj.fin_instrm_id = fin_id
                obj.save(update_fields=["fin_instrm_id"])

        self._tickers[key] = obj
        return obj

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")

    @staticmethod
    def _iter_weekdays(from_date, to_date):
        day = from_date
        while day <= to_date:
            if day.weekday() < 5:
                yield day
            day += timedelta(days=1)
