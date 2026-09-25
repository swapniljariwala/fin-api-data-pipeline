"""Download NIFTY index bhavcopy files and ingest them into the database.

Index bhavcopies are downloaded from NIFTY Indices website via the
``jugaad-data`` library (``NSEIndicesArchives``), parsed, and upserted into
``index_price_history`` per the index schema design.

The command is restart-safe:

* A file whose name already exists in ``bhavcopy_files`` is skipped entirely.
* Each file is ingested inside one transaction: the ``bhavcopy_files`` row is
  only committed together with its price rows, so an interrupted run never
  leaves a file marked as ingested without its data.
* Price rows are inserted with ``ignore_conflicts`` on the unique key
  ``(index, trade_date)``, so re-running never duplicates entries already
  present.

Mode selection (exactly one):

* ``--latest``: most recent index bhavcopy not yet ingested (walks back from
  today).
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive date range, fetched
  oldest-first.
* ``--days N``: last N calendar days ending today, fetched oldest-first.
* ``--delay SECONDS``: wait this long between successive API downloads to stay
  clear of the rate limit (default: 2.0; 0 disables the wait).
* ``--retries N``: retry a failed download up to N times total, backing off by
  ``--delay`` between attempts (default: 3).

Known NSE trading holidays (from the local calendar shipped with ``jugaad-data``
and NSE's live holiday API for the current year) are skipped before any
download is attempted.

The trade date is taken from the file content, not the filename.

The downloaded file is deleted from disk once it has been ingested, so nothing
is kept on disk after the run; restart-safety comes from the ``bhavcopy_files``
table, not from files.

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

from dailypricehistory.models import BhavcopyFile
from indexpricehistory.models import Index, IndexPriceHistory

SOURCE = "NSE"
SEGMENT = "IDX"

logger = logging.getLogger(__name__)


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
    help = "Download NIFTY index bhavcopy files and ingest them into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help="ingest the most recent index bhavcopy not yet ingested "
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
            help="wait this many seconds between API calls "
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

    def handle(self, *args, **options):
        self.data_dir = Path(options["data_dir"] or settings.BASE_DIR / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.no_download = options["no_download"]
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
        self._holiday_dates = None  # lazy set of NSE trading holidays
        self.archives = None
        if not self.no_download:
            from jugaad_data.nse.archives import NSEIndicesArchives

            self.archives = NSEIndicesArchives()

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
            "Index bhavcopy ingest job started (mode=%s, download=%s)",
            mode, not self.no_download,
        )
        started = time.monotonic()
        try:
            if mode == "latest":
                self._run_latest(options["lookback"])
            else:
                self._run_dates(targets)
        finally:
            logger.info(
                "Index bhavcopy ingest job finished in %.1fs",
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
            "Looking for the latest index bhavcopy not yet ingested "
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
            self._ingest_file(path, day)
            return
        logger.info(
            "Nothing to ingest: recent index bhavcopies are already ingested "
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
                            "%s: no index bhavcopy available (holiday?)", day
                        )
                    continue
                self._ingest_file(path, day)
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
        return f"ind_close_all_{day.strftime('%d%m%Y')}.csv"

    def _obtain_file(self, day):
        """Return the path of the index bhavcopy for ``day``, or None if unavailable."""
        path = self.data_dir / self._file_name(day)
        if self.no_download:
            return path if path.is_file() else None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                saved = self.archives.bhavcopy_index_save(
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
                        "%s: NSE trading holiday, no index bhavcopy expected", day
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
        if not self._looks_like_index_bhavcopy(path):
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
                    "%s: downloaded file is not a valid index bhavcopy, "
                    "discarding",
                    day,
                )
            return None
        return path

    def _is_nse_holiday(self, day):
        """Whether ``day`` is a declared NSE trading holiday."""
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
        """Sleep before API calls after the first one in a run."""
        if self._api_calls > 0 and self.delay > 0:
            logger.info(
                "Waiting %.1fs before next API call", self.delay
            )
            time.sleep(self.delay)
        self._api_calls += 1

    def _looks_like_index_bhavcopy(self, path):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                header = fh.readline()
        except (OSError, UnicodeDecodeError):
            return False
        # NIFTY indices CSV has columns like: Index Name, Close, Date, etc.
        # Check for date-like columns or index name pattern
        return "Close" in header or "Index Name" in header or "Closing" in header

    # ---------------------------------------------------------------- parsing

    def _file_date(self, raw, expected, name):
        """The trade date embedded in the file, not the filename.

        Index bhavcopies have a consistent date format embedded in them.
        """
        value = _clean(raw)
        try:
            # Try ISO format first
            parsed = date.fromisoformat(value)
        except (ValueError, TypeError):
            # Fallback to the filename date if parsing fails
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

    def _parse_rows(self, path, expected_date):
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, skipinitialspace=True)
            for raw in reader:
                row = self._parse_row(raw, expected_date)
                if row is not None:
                    rows.append(row)
        return rows, expected_date

    def _parse_row(self, raw, expected_date):
        """Parse one index row from the NIFTY indices CSV."""
        index_name = _clean(raw.get("Index Name"))
        if not index_name:
            return None

        # Extract date from the row (could be in different formats)
        date_str = _clean(raw.get("Date")) or _clean(raw.get("TradDt"))
        if date_str:
            try:
                trade_date = date.fromisoformat(date_str)
            except (ValueError, TypeError):
                trade_date = expected_date
        else:
            trade_date = expected_date

        return {
            "ticker": index_name,
            "trade_date": trade_date,
            "open": _dec(raw.get("Open")),
            "high": _dec(raw.get("High")),
            "low": _dec(raw.get("Low")),
            "close": _dec(raw.get("Close")),
            "volume": _int(raw.get("Volume")),
            "turnover": _dec(raw.get("Turnover")),
        }

    # --------------------------------------------------------------- ingesting

    def _ingest_file(self, path, trade_date):
        logger.info(
            "Ingesting %s (trade_date=%s)",
            path.name, trade_date,
        )
        with transaction.atomic():
            rows, file_date = self._parse_rows(path, trade_date)
            file_obj = BhavcopyFile.objects.create(
                file_name=path.name,
                source=SOURCE,
                segment=SEGMENT,
                format="udiff",  # Index bhavcopies use a single format
                trade_date=file_date,
            )
            self._index_cache = {}
            price_rows = []
            for row in rows:
                index = self._resolve_index(row["ticker"])
                price_rows.append(
                    IndexPriceHistory(
                        index=index,
                        trade_date=row["trade_date"],
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        volume=row["volume"],
                        turnover=row["turnover"],
                        file=file_obj,
                    )
                )
            created = IndexPriceHistory.objects.bulk_create(
                price_rows, ignore_conflicts=True, batch_size=1000
            )
            file_obj.row_count = len(created)
            file_obj.save(update_fields=["row_count"])
        logger.info(
            "%s: %d/%d rows ingested (%d duplicates skipped)",
            path.name, len(created), len(rows), len(rows) - len(created),
        )
        path.unlink(missing_ok=True)
        logger.info("%s: removed from disk", path.name)

    def _resolve_index(self, ticker):
        """Return or create the Index for a ticker."""
        if ticker not in self._index_cache:
            index, _ = Index.objects.get_or_create(
                source=SOURCE,
                ticker=ticker,
                defaults={"name": ticker},
            )
            self._index_cache[ticker] = index
        return self._index_cache[ticker]

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
