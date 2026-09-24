"""Download NSE F&O bhavcopy files and ingest them into the database.

Mirrors ``dailypricehistory.management.commands.ingest_bhavcopy`` almost
exactly (see that file for the full rationale on restart-safety, holiday
handling and the file-content-is-authoritative trade date); this command
swaps the CM-specific parsing/lookup for F&O:

* ``archives.bhavcopy_fo_save`` instead of ``bhavcopy_save``.
* Filename pattern ``fo{dd}{MMM}{yyyy}bhav.csv``.
* Legacy and UDiFF F&O column mappings (see ``spec/TODO-fno-storage.md``).
* ``FnoContract`` (5-tuple identity: underlying/type/expiry/strike/option)
  instead of ``Instrument``.
* ``segment="FO"`` on the (shared) ``BhavcopyFile`` row.

Restart-safety, dedup and logging guarantees are identical to
``ingest_bhavcopy``.
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
from dailypricehistory.object_store import upload_bhavcopy_to_object_store
from fnopricehistory.models import FnoContract, FnoPriceHistory

SOURCE = "NSE"
SEGMENT = "FO"

logger = logging.getLogger(__name__)

_LEGACY_TO_UDIFF_INSTRUMENT_TYPE = {
    "FUTIDX": "IDF",
    "OPTIDX": "IDO",
    "FUTSTK": "STF",
    "OPTSTK": "STO",
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
    """NSE trading holidays from the calendar shipped with ``jugaad-data``.

    Same calendar used for CM; NSE observes a single trading holiday
    calendar across segments (see ``ingest_bhavcopy._local_nse_holidays``).
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
    help = "Download NSE F&O bhavcopy files and ingest them into the database."

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
        self._holiday_dates = None  # lazy set of NSE FO trading holidays
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
            "F&O bhavcopy ingest job started (mode=%s, download=%s, upload=%s)",
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
                "F&O bhavcopy ingest job finished in %.1fs",
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
        return list(self._iter_weekdays(from_date, to_date))

    def _run_latest(self, lookback):
        logger.info(
            "Looking for the latest F&O bhavcopy not yet ingested "
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
            "Nothing to ingest: recent F&O bhavcopies are already ingested "
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
        return f"fo{day.strftime('%d%b%Y')}bhav.csv"

    def _obtain_file(self, day):
        """Return the path of the bhavcopy for ``day``, or None if unavailable."""
        path = self.data_dir / self._file_name(day)
        if self.no_download:
            return path if path.is_file() else None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                saved = self.archives.bhavcopy_fo_save(
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
        """Whether ``day`` is a declared NSE trading holiday (see CM version)."""
        if day in _local_nse_holidays():
            return True
        if self._holiday_dates is None:
            self._holiday_dates = set()
            self._throttle()
            try:
                from jugaad_data.nse.live import NSELive

                data = NSELive().holiday_list()
                for holiday in data.get("FO", []):
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
        return "TradDt" in header or "SYMBOL" in header or "INSTRUMENT" in header

    # ---------------------------------------------------------------- parsing

    def _file_date(self, raw, expected, fmt, name):
        """The trade date embedded in the file, not the filename (see CM version)."""
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
                date_col = "TIMESTAMP"
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
        instrument_type = _LEGACY_TO_UDIFF_INSTRUMENT_TYPE.get(
            (raw.get("INSTRUMENT") or "").strip(),
            (raw.get("INSTRUMENT") or "").strip(),
        )
        option_type = _clean(raw.get("OPTION_TYP"))
        if option_type in ("XX", None):
            option_type = None
        return {
            "underlying_symbol": (raw.get("SYMBOL") or "").strip(),
            "instrument_type": instrument_type,
            "expiry_date": self._parse_expiry(raw.get("EXPIRY_DT"), "%d-%b-%Y"),
            "strike_price": _dec(raw.get("STRIKE_PR")),
            "option_type": option_type,
            "fin_instrm_id": None,
            "trade_date": expected_date,
            "open": _dec(raw.get("OPEN")),
            "high": _dec(raw.get("HIGH")),
            "low": _dec(raw.get("LOW")),
            "close": _dec(raw.get("CLOSE")),
            "settlement_price": _dec(raw.get("SETTLE_PR")),
            "volume": _int(raw.get("CONTRACTS")),
            "turnover": self._legacy_turnover(raw.get("VAL_INLAKH")),
            "num_trades": None,
            "open_interest": _int(raw.get("OPEN_INT")),
            "change_in_oi": _int(raw.get("CHG_IN_OI")),
            "underlying_value": None,
        }

    def _legacy_turnover(self, raw):
        value = _dec(raw)
        if value is None:
            return None
        return value * Decimal("100000")

    def _parse_udiff_row(self, raw, expected_date):
        if (raw.get("Sgmt") or "FO") != "FO":
            return None  # guard: only the FO segment is ingested
        option_type = _clean(raw.get("OptnTp"))
        return {
            "underlying_symbol": (raw.get("TckrSymb") or "").strip(),
            "instrument_type": (raw.get("FinInstrmTp") or "").strip(),
            "expiry_date": self._parse_expiry(raw.get("XpryDt"), "iso"),
            "strike_price": _dec(raw.get("StrkPric")),
            "option_type": option_type,
            "fin_instrm_id": _int(raw.get("FinInstrmId")),
            "trade_date": expected_date,
            "open": _dec(raw.get("OpnPric")),
            "high": _dec(raw.get("HghPric")),
            "low": _dec(raw.get("LwPric")),
            "close": _dec(raw.get("ClsPric")),
            "settlement_price": _dec(raw.get("SttlmPric")),
            "volume": _int(raw.get("TtlTradgVol")),
            "turnover": _dec(raw.get("TtlTrfVal")),  # already full rupees
            "num_trades": _int(raw.get("TtlNbOfTxsExctd")),
            "open_interest": _int(raw.get("OpnIntrst")),
            "change_in_oi": _int(raw.get("ChngInOpnIntrst")),
            "underlying_value": _dec(raw.get("UndrlygPric")),
        }

    def _parse_expiry(self, raw, fmt):
        value = _clean(raw)
        if value is None:
            return None
        if fmt == "iso":
            return date.fromisoformat(value)
        return datetime.strptime(value, fmt).date()

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
            self._contract_cache = {}
            price_rows = []
            for row in rows:
                contract = self._resolve_contract(row)
                price_rows.append(
                    FnoPriceHistory(
                        contract=contract,
                        trade_date=row["trade_date"],
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        close=row["close"],
                        settlement_price=row["settlement_price"],
                        volume=row["volume"],
                        turnover=row["turnover"],
                        num_trades=row["num_trades"],
                        open_interest=row["open_interest"],
                        change_in_oi=row["change_in_oi"],
                        underlying_value=row["underlying_value"],
                        file=file_obj,
                    )
                )
            created = FnoPriceHistory.objects.bulk_create(
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
        if "INSTRUMENT" in header or "SYMBOL" in header:
            return "legacy"
        raise ValueError(f"unrecognized bhavcopy header: {header[:80]!r}")

    # ------------------------------------------------------ master data lookup

    def _resolve_contract(self, row):
        key = (
            row["underlying_symbol"],
            row["instrument_type"],
            row["expiry_date"],
            row["strike_price"],
            row["option_type"],
        )
        contract = self._contract_cache.get(key)
        if contract is not None:
            return contract
        contract, created = FnoContract.objects.get_or_create(
            underlying_symbol=row["underlying_symbol"],
            instrument_type=row["instrument_type"],
            expiry_date=row["expiry_date"],
            strike_price=row["strike_price"],
            option_type=row["option_type"],
            defaults={"fin_instrm_id": row["fin_instrm_id"]},
        )
        if (
            not created
            and row["fin_instrm_id"]
            and not contract.fin_instrm_id
        ):
            contract.fin_instrm_id = row["fin_instrm_id"]
            contract.save(update_fields=["fin_instrm_id"])
        self._contract_cache[key] = contract
        return contract

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
