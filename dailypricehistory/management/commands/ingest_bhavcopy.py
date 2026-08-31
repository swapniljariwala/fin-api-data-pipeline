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
  ``(instrument, trade_date, series)``, so re-running never duplicates
  entries already present.

Mode selection (exactly one):

* ``--latest``: most recent bhavcopy not yet ingested (walks back from today).
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive date range, fetched
  oldest-first.
* ``--days N``: last N calendar days ending today, fetched oldest-first.
* ``--upload``: after each successful ingest, upload the bhavcopy to the
  object store (placeholder in ``dailypricehistory.object_store``; logs a
  warning until it is implemented).
* ``--delay SECONDS``: wait this long between successive NSE API downloads
  to stay clear of the rate limit (default: 2.0; 0 disables the wait).
* ``--retries N``: retry a failed download up to N times total, backing off
  by ``--delay`` between attempts (default: 3). NSE endpoints are flaky and
  ``jugaad-data`` can crash on a transient timeout, so a retry usually
  succeeds.

Known NSE trading holidays (from the local calendar shipped with
``jugaad-data``, covering all years, plus NSE's live holiday API for the
current year) are skipped before any download is attempted. If a download
still yields no file - a closure the calendars do not know, or NSE serving
an error page - the day is logged as a holiday or a genuine download
failure respectively, not as an ingestion error.

The trade date is taken from the file content, not the filename: NSE's
archive server can serve the most recent available bhavcopy for a date
that has none, and storing those rows under the embedded date keeps the
data honest (any overlap with the real day is deduped by the uniqueness
key).

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

from dailypricehistory.models import BhavcopyFile, NSECmPriceHistory
from dailypricehistory.object_store import upload_bhavcopy_to_object_store
from securityinfo.models import (
    Instrument,
    NSESymbolInstrumentMap,
    NSESymbols,
    Series,
)

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


_LOCAL_HOLIDAYS = None


def _local_nse_holidays():
    """NSE CM holidays from the calendar shipped with ``jugaad-data``.

    Covers all years, so it is authoritative for historical backfills where
    NSE's live holiday API has no data. Parsed once per process.
    """
    global _LOCAL_HOLIDAYS
    if _LOCAL_HOLIDAYS is None:
        from jugaad_data.holidays import holidays_str

        parsed = set()
        for raw in holidays_str:
            try:
                parsed.add(datetime.strptime(raw, "%Y-%m-%d").date())
            except ValueError:
                continue
        _LOCAL_HOLIDAYS = parsed
    return _LOCAL_HOLIDAYS


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
        # Oldest first: chronological order matches the sequence in which
        # data is written to the DB.
        return list(self._iter_weekdays(from_date, to_date))

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
            if not self.no_download and self._is_nse_holiday(day):
                logger.info(
                    "%s: NSE trading holiday, skipping download", day
                )
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
            "Processing %d trading day(s), oldest first", len(targets)
        )
        for day in targets:
            if self._already_ingested(day):
                logger.info("%s: already ingested, skipping", day)
                summary["skipped"] += 1
                continue
            if not self.no_download and self._is_nse_holiday(day):
                logger.info(
                    "%s: NSE trading holiday, skipping download", day
                )
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
                    time.sleep(self.delay)
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
            path.unlink(missing_ok=True)
            self._download_ok = False
            if self._is_nse_holiday(day):
                logger.info(
                    "%s: discarded invalid download on an NSE holiday", day
                )
            else:
                logger.warning(
                    "%s: downloaded file is not a valid bhavcopy, "
                    "discarding",
                    day,
                )
            return None
        return path

    def _is_nse_holiday(self, day):
        """Whether ``day`` is a declared NSE CM trading holiday.

        Two sources, checked in order:
        * The local calendar shipped with ``jugaad-data``, which covers all
          years and is therefore authoritative for historical backfills.
        * NSE's live holiday-master API (fetched once per run), which covers
          the currently published year including one-off closures. If the API
          is unreachable or malformed we log a warning and fall through, so
          an unknown day is treated as a genuine trading day.
        """
        if day in _local_nse_holidays():
            return True
        if self._holiday_dates is None:
            self._holiday_dates = set()
            self._throttle()
            try:
                from jugaad_data.nse.live import NSELive

                data = NSELive().holiday_list()
                for holiday in data.get("CM", []):
                    raw = holiday.get("tradingDate")
                    try:
                        hday = datetime.strptime(
                            raw, "%d-%b-%Y"
                        ).date()
                    except (TypeError, ValueError):
                        continue
                    self._holiday_dates.add(hday)
            except Exception as exc:
                logger.warning(
                    "Could not fetch NSE holiday calendar (%s); treating "
                    "failed downloads as errors",
                    exc,
                )
                return False
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

    def _file_date(self, raw, expected, fmt, name):
        """The trade date embedded in the file, not the filename.

        NSE's archive server serves the most recent available bhavcopy for a
        date that has none (holidays, closures), so a file can hold the
        previous trading day's rows. The embedded date is authoritative: rows
        are stored under it, where the uniqueness key makes any duplicate of
        the real day a no-op.
        """
        value = _clean(raw)
        try:
            if fmt == "legacy":
                parsed = datetime.strptime(value, "%d-%b-%Y").date()
            else:
                parsed = date.fromisoformat(value)
        except (ValueError, TypeError):
            logger.warning(
                "%s: no trade date in file; assuming %s from the filename",
                name, expected,
            )
            return expected
        if parsed != expected:
            logger.warning(
                "%s: file holds %s data, not %s; storing under the "
                "file's date",
                name, parsed, expected,
            )
        return parsed

    def _parse_rows(self, path, fmt, expected_date):
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, skipinitialspace=True)
            first = next(reader, None)
            if first is None:
                return rows, expected_date
            if fmt == "legacy":
                date_col = "DATE1" if "DATE1" in reader.fieldnames else "TIMESTAMP"
            else:
                date_col = "TradDt"
            trade_date = self._file_date(
                first.get(date_col), expected_date, fmt, path.name
            )
            for raw in [first, *reader]:
                if fmt == "legacy":
                    row = self._parse_legacy_row(raw, trade_date)
                else:
                    row = self._parse_udiff_row(raw, trade_date)
                if row is not None:
                    rows.append(row)
        return rows, trade_date

    def _parse_legacy_row(self, raw, expected_date):
        series = (raw.get("SERIES") or "").strip()
        return {
            "ticker": (raw.get("SYMBOL") or "").strip(),
            "series": series,
            "trade_date": expected_date,
            "isin": _clean(raw.get("ISIN")),
            "name": _clean(raw.get("FinInstrmNm")),
            "fin_instrm_id": _int(raw.get("FinInstrmId")),
            "open": _dec(raw.get("OPEN_PRICE") or raw.get("OPEN")),
            "high": _dec(raw.get("HIGH_PRICE") or raw.get("HIGH")),
            "low": _dec(raw.get("LOW_PRICE") or raw.get("LOW")),
            "close": _dec(raw.get("CLOSE_PRICE") or raw.get("CLOSE")),
            "last_price": _dec(raw.get("LAST_PRICE") or raw.get("LAST")),
            "prev_close": _dec(raw.get("PREV_CLOSE") or raw.get("PREVCLOSE")),
            "volume": _int(raw.get("TTL_TRD_QNTY") or raw.get("TOTTRDQTY")),
            "turnover": self._legacy_turnover(
                raw.get("TOTTRDVAL") or raw.get("TURNOVER_LACS"),
                is_full_rupees="TOTTRDVAL" in raw,
            ),
            "num_trades": _int(raw.get("NO_OF_TRADES") or raw.get("TOTALTRADES")),
            "settlement_price": None,
        }

    def _legacy_turnover(self, raw, is_full_rupees=False):
        value = _dec(raw)
        if value is None:
            return None
        return value if is_full_rupees else value * Decimal("100000")

    def _parse_udiff_row(self, raw, expected_date):
        if (raw.get("FinInstrmTp") or "STK") != "STK":
            return None  # guard: only STK instruments are ingested
        series = (raw.get("SctySrs") or "").strip()
        return {
            "ticker": (raw.get("TckrSymb") or "").strip(),
            "series": series,
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
            rows, file_date = self._parse_rows(path, fmt, trade_date)
            file_obj = BhavcopyFile.objects.create(
                file_name=path.name,
                source=SOURCE,
                segment=SEGMENT,
                format=fmt,
                trade_date=file_date,
            )
            self._series_cache = {}
            self._instrument_cache = {}
            self._symbol_cache = {}
            self._symbol_map_cache = set()
            price_rows = []
            for row in rows:
                instrument = self._resolve_instrument(
                    isin=row["isin"],
                    name=row["name"],
                    ticker=row["ticker"],
                )
                self._ensure_symbol_map(row["ticker"], instrument)
                series = self._resolve_series(row["series"])
                price_rows.append(
                    NSECmPriceHistory(
                        instrument=instrument,
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
            created = NSECmPriceHistory.objects.bulk_create(
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

    def _resolve_instrument(self, isin, name, ticker):
        """Return the global ``Instrument`` for a bhavcopy row.

        Keyed by ISIN when present; otherwise matched or created against the
        ticker/name (legacy bhavcopies often omit ISIN).
        """
        cache_key = isin if isin else f"#{ticker or name}"
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
                name=ticker or name or "",
                defaults={"instrument_type": "STK"},
            )
        if name and not instrument.name:
            instrument.name = name
            instrument.save(update_fields=["name"])
        self._instrument_cache[cache_key] = instrument
        return instrument

    def _ensure_symbol_map(self, ticker, instrument):
        """Record that ``ticker`` (an NSE symbol) belongs to ``instrument``."""
        if not ticker:
            return
        symbol = self._symbol_cache.get(ticker)
        if symbol is None:
            symbol, _ = NSESymbols.objects.get_or_create(symbol=ticker)
            self._symbol_cache[ticker] = symbol
        key = (symbol.id, instrument.id)
        if key not in self._symbol_map_cache:
            NSESymbolInstrumentMap.objects.get_or_create(
                symbol=symbol, instrument=instrument
            )
            self._symbol_map_cache.add(key)

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
