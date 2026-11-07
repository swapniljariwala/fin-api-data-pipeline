"""Download the NSE full bhavcopy and ingest its delivery data.

The full bhavcopy (``sec_bhavdata_full_*.csv``) matches the regular bhavcopy
on every price/volume column; its only extra data are ``DELIV_QTY`` and
``DELIV_PER``. Those are stored in ``nse_cm_delivery``, keyed by the id of
the matching ``nse_cm_price_history`` row, so the regular bhavcopy for a date
must be ingested first.

Same options, download/retry/holiday handling and restart-safety as
``ingest_bhavcopy`` (this command subclasses it). Differences:

* The full file has no ISIN, so each ``(symbol, series)`` is resolved onto the
  price row of that date through ``nse_symbols_instrument_map``. Rows with no
  price row, or whose symbol maps to more than one price row, are logged and
  skipped.
* A date with no price rows at all fails the file *before* it is recorded in
  ``bhavcopy_files``, so a re-run picks it up once prices are ingested.
* Only the last ``RETENTION_DAYS`` are kept (see ``purge_old_delivery``);
  older dates are not fetched, so a backfill never recreates purged data.
* ``-`` delivery values (BE/BZ series) are stored as NULL.
"""

import csv
import logging
from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction

from dailypricehistory.management.commands.ingest_bhavcopy import (
    Command as BhavcopyCommand,
    SEGMENT,
    SOURCE,
    _clean,
    _dec,
    _int,
)
from dailypricehistory.models import BhavcopyFile, NSECmDelivery, NSECmPriceHistory

logger = logging.getLogger(__name__)

RETENTION_DAYS = 365


class Command(BhavcopyCommand):
    help = (
        "Download NSE full bhavcopy files and ingest their delivery data "
        "(requires the regular bhavcopy for the date to be ingested first)."
    )

    download_method = "full_bhavcopy_save"

    def _target_dates(self, from_date, to_date):
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
        if from_date < cutoff:
            logger.warning(
                "Delivery data is kept for %d days; starting from %s "
                "instead of %s",
                RETENTION_DAYS, cutoff, from_date,
            )
            from_date = cutoff
        return super()._target_dates(from_date, to_date)

    def _file_name(self, day):
        return f"sec_bhavdata_full_{day.strftime('%d%b%Y')}bhav.csv"

    def _looks_like_bhavcopy(self, path):
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                header = fh.readline()
        except (OSError, UnicodeDecodeError):
            return False
        return "DELIV_QTY" in header

    def _detect_format(self, path):
        return BhavcopyFile.FORMAT_FULL

    def _parse_rows(self, path, fmt, expected_date):
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            # The header has spaces after the commas (" SERIES"), hence
            # skipinitialspace.
            reader = csv.DictReader(fh, skipinitialspace=True)
            first = next(reader, None)
            if first is None:
                return rows, expected_date
            trade_date = self._file_date(
                first.get("DATE1"), expected_date, "legacy", path.name
            )
            for raw in [first, *reader]:
                rows.append(
                    {
                        "symbol": (raw.get("SYMBOL") or "").strip(),
                        "series": (raw.get("SERIES") or "").strip().upper(),
                        "deliv_qty": _int(raw.get("DELIV_QTY")),
                        "deliv_per": _dec(raw.get("DELIV_PER")),
                    }
                )
        return rows, trade_date

    def _price_ids(self, trade_date):
        """Map ``(symbol, series code)`` to the set of price row ids."""
        ids = defaultdict(set)
        qs = NSECmPriceHistory.objects.filter(trade_date=trade_date).values_list(
            "id", "series__code", "instrument__symbols__symbol__symbol"
        )
        for price_id, series, symbol in qs.iterator():
            if symbol:
                ids[(symbol, series)].add(price_id)
        return ids

    def _ingest_file(self, path, trade_date):
        logger.info(
            "Ingesting %s (format=full, trade_date=%s)", path.name, trade_date
        )
        rows, file_date = self._parse_rows(path, BhavcopyFile.FORMAT_FULL, trade_date)
        with transaction.atomic():
            price_ids = self._price_ids(file_date)
            if not price_ids:
                raise ValueError(
                    f"{path.name}: no price rows for {file_date}; ingest "
                    "the regular bhavcopy first"
                )
            file_obj = BhavcopyFile.objects.create(
                file_name=path.name,
                source=SOURCE,
                segment=SEGMENT,
                format=BhavcopyFile.FORMAT_FULL,
                trade_date=file_date,
            )
            delivery_rows = []
            unmatched = ambiguous = 0
            for row in rows:
                ids = price_ids.get((row["symbol"], row["series"]))
                if not ids:
                    unmatched += 1
                    continue
                if len(ids) > 1:
                    ambiguous += 1
                    logger.warning(
                        "%s: %s/%s maps to %d price rows, skipping",
                        path.name, row["symbol"], row["series"], len(ids),
                    )
                    continue
                delivery_rows.append(
                    NSECmDelivery(
                        price_id=next(iter(ids)),
                        trade_date=file_date,
                        deliv_qty=row["deliv_qty"],
                        deliv_per=row["deliv_per"],
                    )
                )
            created = NSECmDelivery.objects.bulk_create(
                delivery_rows, ignore_conflicts=True, batch_size=1000
            )
            file_obj.row_count = len(created)
            file_obj.save(update_fields=["row_count"])
        logger.info(
            "%s: %d/%d rows ingested (%d without a price row, %d ambiguous)",
            path.name, len(created), len(rows), unmatched, ambiguous,
        )
