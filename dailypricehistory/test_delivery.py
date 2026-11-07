"""Tests for delivery ingest (full bhavcopy) and its retention purge."""

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from dailypricehistory.models import BhavcopyFile, NSECmDelivery, NSECmPriceHistory
from securityinfo.models import (
    Instrument,
    NSESymbolInstrumentMap,
    NSESymbols,
    Series,
)

HEADER = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
    "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
    "NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
)
DAY = date.today() - timedelta(days=3)
while DAY.weekday() >= 5:
    DAY -= timedelta(days=1)


def _line(symbol, series, day, qty, per):
    return (
        f"{symbol}, {series}, {day.strftime('%d-%b-%Y')}, 1.00, 1.00, 1.00, "
        f"1.00, 1.00, 1.00, 1.00, 100, 1.00, 5, {qty}, {per}\n"
    )


def _make_price(symbol, series_code, day, isin):
    instrument, _ = Instrument.objects.get_or_create(
        isin=isin, defaults={"name": symbol, "instrument_type": "STK"}
    )
    sym, _ = NSESymbols.objects.get_or_create(symbol=symbol)
    NSESymbolInstrumentMap.objects.get_or_create(symbol=sym, instrument=instrument)
    series, _ = Series.objects.get_or_create(
        code=series_code, defaults={"description": series_code}
    )
    return NSECmPriceHistory.objects.create(
        instrument=instrument, trade_date=day, series=series
    )


class IngestDeliveryTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def _write(self, day, *lines):
        name = f"sec_bhavdata_full_{day.strftime('%d%b%Y')}bhav.csv"
        (self.data_dir / name).write_text(HEADER + "".join(lines))
        return name

    def _run(self, day):
        call_command(
            "ingest_delivery_bhavcopy",
            "--from", day.isoformat(), "--to", day.isoformat(),
            "--data-dir", str(self.data_dir), "--no-download", "--delay", "0",
        )

    def test_ingest_joins_to_price_row_and_nulls_dash(self):
        eq = _make_price("AAA", "EQ", DAY, "INE000000001")
        be = _make_price("BBB", "BE", DAY, "INE000000002")
        self._write(
            DAY,
            _line("AAA", "EQ", DAY, 29072, "45.34"),
            _line("BBB", "BE", DAY, "-", "-"),
            _line("ZZZ", "EQ", DAY, 5, "1.00"),  # no price row
        )
        self._run(DAY)

        d = NSECmDelivery.objects.get(pk=eq.pk)
        self.assertEqual(d.deliv_qty, 29072)
        self.assertEqual(d.deliv_per, Decimal("45.34"))
        self.assertEqual(d.trade_date, DAY)
        dash = NSECmDelivery.objects.get(pk=be.pk)
        self.assertIsNone(dash.deliv_qty)
        self.assertIsNone(dash.deliv_per)
        self.assertEqual(NSECmDelivery.objects.count(), 2)
        self.assertEqual(eq.delivery.deliv_qty, 29072)
        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.format, "full")
        self.assertEqual(bf.row_count, 2)

    def test_rerun_is_noop(self):
        _make_price("AAA", "EQ", DAY, "INE000000001")
        self._write(DAY, _line("AAA", "EQ", DAY, 10, "50.00"))
        self._run(DAY)
        self._write(DAY, _line("AAA", "EQ", DAY, 99, "99.00"))
        self._run(DAY)
        self.assertEqual(NSECmDelivery.objects.get().deliv_qty, 10)
        self.assertEqual(BhavcopyFile.objects.count(), 1)

    def test_no_price_rows_fails_without_recording_file(self):
        self._write(DAY, _line("AAA", "EQ", DAY, 10, "50.00"))
        with self.assertRaises(CommandError):
            self._run(DAY)
        self.assertEqual(BhavcopyFile.objects.count(), 0)
        self.assertEqual(NSECmDelivery.objects.count(), 0)
        # Once prices exist, a re-run succeeds.
        _make_price("AAA", "EQ", DAY, "INE000000001")
        self._run(DAY)
        self.assertEqual(NSECmDelivery.objects.count(), 1)

    def test_ambiguous_symbol_is_skipped(self):
        _make_price("AAA", "EQ", DAY, "INE000000001")
        other = _make_price("OTH", "EQ", DAY, "INE000000002")
        sym = NSESymbols.objects.get(symbol="AAA")
        NSESymbolInstrumentMap.objects.create(
            symbol=sym, instrument=other.instrument
        )
        self._write(DAY, _line("AAA", "EQ", DAY, 10, "50.00"))
        self._run(DAY)
        self.assertEqual(NSECmDelivery.objects.count(), 0)

    def test_dates_older_than_retention_are_not_fetched(self):
        old = date.today() - timedelta(days=400)
        while old.weekday() >= 5:
            old -= timedelta(days=1)
        with mock.patch(
            "dailypricehistory.management.commands.ingest_delivery_bhavcopy."
            "Command._obtain_file"
        ) as obtain:
            call_command(
                "ingest_delivery_bhavcopy",
                "--from", old.isoformat(), "--to", old.isoformat(),
                "--data-dir", str(self.data_dir), "--no-download", "--delay", "0",
            )
        obtain.assert_not_called()


class PurgeOldDeliveryTests(TestCase):
    def test_purge_deletes_only_old_delivery(self):
        old_day = date.today() - timedelta(days=400)
        old_price = _make_price("AAA", "EQ", old_day, "INE000000001")
        new_price = _make_price("BBB", "EQ", DAY, "INE000000002")
        NSECmDelivery.objects.create(price=old_price, trade_date=old_day, deliv_qty=1)
        NSECmDelivery.objects.create(price=new_price, trade_date=DAY, deliv_qty=2)
        BhavcopyFile.objects.create(
            file_name="old.csv", source="NSE", format="full", trade_date=old_day
        )

        call_command("purge_old_delivery", stdout=StringIO())  # dry run
        self.assertEqual(NSECmDelivery.objects.count(), 2)

        call_command("purge_old_delivery", "--apply", stdout=StringIO())
        self.assertEqual(
            list(NSECmDelivery.objects.values_list("pk", flat=True)),
            [new_price.pk],
        )
        self.assertEqual(NSECmPriceHistory.objects.count(), 2)
        self.assertEqual(BhavcopyFile.objects.count(), 0)
