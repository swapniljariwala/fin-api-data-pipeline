import logging
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from dailypricehistory.models import BhavcopyFile, CmPriceHistory
from securityinfo.models import Instrument, InstrumentTicker, Series

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data"


def _run(*args, **kwargs):
    kwargs.setdefault("verbosity", 0)
    call_command("ingest_bhavcopy", *args, **kwargs)


class IngestBhavcopyTests(TestCase):
    def _copy_sample(self, name):
        tmp = Path(tempfile.mkdtemp())
        shutil.copy(SAMPLE_DIR / name, tmp / name)
        return str(tmp)

    def test_ingest_legacy_file(self):
        data_dir = self._copy_sample("cm05Jul2024bhav.csv")
        _run(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.file_name, "cm05Jul2024bhav.csv")
        self.assertEqual(bf.source, "NSE")
        self.assertEqual(bf.format, "legacy")
        self.assertEqual(bf.row_count, CmPriceHistory.objects.count())
        self.assertGreater(bf.row_count, 0)

        # A known row: 20MICRONS, EQ series (primary series → no suffix).
        ticker = InstrumentTicker.objects.get(source="NSE", ticker="20MICRONS")
        self.assertEqual(ticker.instrument.isin, "INE144J01027")
        row = CmPriceHistory.objects.get(
            instrument_ticker=ticker, series__code="EQ"
        )
        self.assertEqual(row.open, Decimal("227.0000"))
        self.assertEqual(row.close, Decimal("224.7200"))
        # TOTTRDVAL is already in full rupees (not lakhs).
        self.assertEqual(row.turnover, Decimal("45223088.56"))
        self.assertEqual(row.volume, 201028)

    def test_ingest_udiff_file(self):
        data_dir = self._copy_sample("cm24Aug2026bhav.csv")
        _run(
            from_date="2026-08-24",
            to_date="2026-08-24",
            data_dir=data_dir,
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.format, "udiff")
        self.assertEqual(bf.row_count, CmPriceHistory.objects.count())
        self.assertGreater(bf.row_count, 0)

        # UDiFF populates ISIN, name and the exchange instrument id.
        # SGBJUN28 is GB series (non-primary) → ticker becomes SGBJUN28-GB.
        instrument = Instrument.objects.get(isin="IN0020200104")
        self.assertEqual(instrument.name, "2.5%GOLDBONDS2028SR-III")
        ticker = InstrumentTicker.objects.get(
            instrument=instrument, source="NSE", ticker="SGBJUN28-GB"
        )
        self.assertEqual(ticker.fin_instrm_id, 19078)
        # 20MICRONS is EQ series (primary) → no suffix.
        instrument = Instrument.objects.get(isin="INE144J01027")
        ticker = InstrumentTicker.objects.get(
            instrument=instrument, source="NSE", ticker="20MICRONS"
        )

    def test_rerun_is_a_noop(self):
        data_dir = self._copy_sample("cm05Jul2024bhav.csv")
        kwargs = dict(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )
        _run(**kwargs)
        first_count = CmPriceHistory.objects.count()
        _run(**kwargs)

        self.assertEqual(BhavcopyFile.objects.count(), 1)
        self.assertEqual(CmPriceHistory.objects.count(), first_count)

    def test_days_mode(self):
        # Sample file is cm24Aug2026bhav.csv with embedded trade_date 2026-08-24.
        data_dir = self._copy_sample("cm24Aug2026bhav.csv")
        _run(days=3, data_dir=data_dir, no_download=True)
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-24").exists()
        )

    def test_latest_no_download(self):
        data_dir = self._copy_sample("cm24Aug2026bhav.csv")
        _run(latest=True, data_dir=data_dir, no_download=True)
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-24").exists()
        )

    def test_missing_file_reports_no_data(self):
        with tempfile.TemporaryDirectory() as data_dir:
            _run(
                from_date="2024-07-06",
                to_date="2024-07-06",
                data_dir=data_dir,
                no_download=True,
            )
        self.assertEqual(BhavcopyFile.objects.count(), 0)
        self.assertEqual(CmPriceHistory.objects.count(), 0)

    def test_series_lookup_seeded(self):
        data_dir = self._copy_sample("cm05Jul2024bhav.csv")
        _run(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )
        self.assertTrue(Series.objects.filter(code="EQ").exists())
        self.assertTrue(Series.objects.filter(code="GS").exists())

    def test_file_deleted_after_ingest(self):
        data_dir = self._copy_sample("cm24Aug2026bhav.csv")
        _run(
            from_date="2026-08-24",
            to_date="2026-08-24",
            data_dir=data_dir,
            no_download=True,
        )
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-24").exists()
        )
        self.assertFalse(
            Path(data_dir, "cm24Aug2026bhav.csv").exists(),
            "bhavcopy file should be removed after ingestion",
        )

    def test_delay_throttles_nse_api_calls(self):
        base = Path(tempfile.mkdtemp())

        def fake_save(day, dest, skip_if_present=True):
            name = f"cm{day:%d%b%Y}bhav.csv"
            target = base / name
            if not target.is_file():
                shutil.copy(SAMPLE_DIR / "cm24Aug2026bhav.csv", target)
            return str(target)

        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ) as live_cls, mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ) as sleep:
            live_cls.return_value.holiday_list.return_value = {"CM": []}
            archives_cls.return_value.bhavcopy_save.side_effect = fake_save
            _run(
                from_date="2026-08-21",
                to_date="2026-08-24",
                data_dir=str(base),
                delay=1.5,
            )

        # One wait before each of the two downloads (the holiday check on
        # the first day is itself an API call but sleeps before nothing).
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(1.5)

    def test_download_retries_after_transient_failure(self):
        base = Path(tempfile.mkdtemp())
        calls = {"n": 0}

        def flaky_save(day, dest, skip_if_present=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    "cannot access local variable 'r' where it is not "
                    "associated with a value"
                )
            name = f"cm{day:%d%b%Y}bhav.csv"
            target = base / name
            if not target.is_file():
                shutil.copy(SAMPLE_DIR / "cm24Aug2026bhav.csv", target)
            return str(target)

        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ), mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ):
            archives_cls.return_value.bhavcopy_save.side_effect = flaky_save
            _run(
                from_date="2026-08-24",
                to_date="2026-08-24",
                data_dir=str(base),
                retries=3,
            )

        self.assertEqual(calls["n"], 2, "first attempt fails, second succeeds")
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-24").exists()
        )

    def test_download_gives_up_after_all_retries(self):
        base = Path(tempfile.mkdtemp())

        def always_fail(day, dest, skip_if_present=True):
            raise RuntimeError(
                "cannot access local variable 'r' where it is not "
                "associated with a value"
            )

        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ), mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ):
            archives_cls.return_value.bhavcopy_save.side_effect = always_fail
            _run(
                from_date="2026-08-20",
                to_date="2026-08-20",
                data_dir=str(base),
                retries=2,
            )

        self.assertEqual(
            archives_cls.return_value.bhavcopy_save.call_count, 2
        )
        self.assertFalse(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
        )

    def test_download_failure_classified_as_holiday(self):
        base = Path(tempfile.mkdtemp())
        holidays = {
            "CM": [
                {"tradingDate": "20-Aug-2026", "weekDay": "Thursday"},
                {"tradingDate": "21-Aug-2026", "weekDay": "Friday"},
            ]
        }
        logger = logging.getLogger(
            "dailypricehistory.management.commands.ingest_bhavcopy"
        )

        def always_fail(day, dest, skip_if_present=True):
            raise RuntimeError("boom")

        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ) as live_cls, mock.patch.object(logger, "info") as info:
            live_cls.return_value.holiday_list.return_value = holidays
            archives_cls.return_value.bhavcopy_save.side_effect = always_fail
            _run(
                from_date="2026-08-20",
                to_date="2026-08-20",
                data_dir=str(base),
                retries=1,
            )

        messages = [str(c) for c in info.call_args_list]
        self.assertTrue(
            any("NSE trading holiday" in m for m in messages),
            f"expected holiday classification, got {messages!r}",
        )

    def test_download_failure_classified_as_error(self):
        base = Path(tempfile.mkdtemp())

        def always_fail(day, dest, skip_if_present=True):
            raise RuntimeError("boom")

        logger = logging.getLogger(
            "dailypricehistory.management.commands.ingest_bhavcopy"
        )
        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ) as live_cls, mock.patch.object(logger, "warning") as warning:
            live_cls.return_value.holiday_list.return_value = {"CM": []}
            archives_cls.return_value.bhavcopy_save.side_effect = always_fail
            _run(
                from_date="2026-08-20",
                to_date="2026-08-20",
                data_dir=str(base),
                retries=1,
            )

        messages = [str(c) for c in warning.call_args_list]
        self.assertTrue(
            any("not an NSE holiday" in m for m in messages),
            f"expected error classification, got {messages!r}",
        )

    def test_known_holiday_not_downloaded(self):
        # A date the live holiday API reports must not be downloaded at all.
        base = Path(tempfile.mkdtemp())
        holidays = {"CM": [{"tradingDate": "20-Aug-2026"}]}
        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ) as live_cls, mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ):
            live_cls.return_value.holiday_list.return_value = holidays
            _run(
                from_date="2026-08-20",
                to_date="2026-08-20",
                data_dir=str(base),
            )
        archives_cls.return_value.bhavcopy_save.assert_not_called()
        self.assertFalse(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
        )

    def test_local_calendar_skips_historic_holiday(self):
        # 2025-10-02 is in jugaad's shipped calendar but not the live API
        # (which only covers the current year); it must still be skipped.
        base = Path(tempfile.mkdtemp())
        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "jugaad_data.nse.live.NSELive"
        ), mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ):
            _run(
                from_date="2025-10-02",
                to_date="2025-10-02",
                data_dir=str(base),
            )
        archives_cls.return_value.bhavcopy_save.assert_not_called()

    def test_date_taken_from_file_not_filename(self):
        # NSE can serve the previous day's file for a date with no bhavcopy;
        # rows must be stored under the embedded date, not the filename's.
        tmp = Path(tempfile.mkdtemp())
        shutil.copy(
            SAMPLE_DIR / "cm05Jul2024bhav.csv",
            tmp / "cm08Jul2024bhav.csv",
        )
        _run(
            from_date="2024-07-08",
            to_date="2024-07-08",
            data_dir=str(tmp),
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.file_name, "cm08Jul2024bhav.csv")
        self.assertEqual(bf.trade_date.isoformat(), "2024-07-05")
        self.assertEqual(
            list(
                CmPriceHistory.objects.values_list(
                    "trade_date", flat=True
                ).distinct()
            ),
            [bf.trade_date],
        )

    def test_purge_duplicate_bhavcopies(self):
        data_dir = self._copy_sample("cm05Jul2024bhav.csv")
        _run(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )
        real = BhavcopyFile.objects.get()
        real_rows = list(CmPriceHistory.objects.filter(file=real))
        bogus = BhavcopyFile.objects.create(
            file_name="cm06Jul2024bhav.csv",
            source="NSE",
            segment="CM",
            format="legacy",
            trade_date="2024-07-06",
            row_count=len(real_rows),
        )
        for r in real_rows:
            CmPriceHistory.objects.create(
                instrument_ticker=r.instrument_ticker,
                trade_date="2024-07-06",
                series=r.series,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                last_price=r.last_price,
                prev_close=r.prev_close,
                volume=r.volume,
                turnover=r.turnover,
                num_trades=r.num_trades,
                settlement_price=r.settlement_price,
                file=bogus,
            )

        # Dry run flags the bogus file without touching anything.
        call_command("purge_duplicate_bhavcopies", verbosity=0)
        self.assertTrue(BhavcopyFile.objects.filter(id=bogus.id).exists())

        call_command("purge_duplicate_bhavcopies", apply=True, verbosity=0)
        self.assertFalse(BhavcopyFile.objects.filter(id=bogus.id).exists())
        self.assertTrue(BhavcopyFile.objects.filter(id=real.id).exists())
        self.assertFalse(
            CmPriceHistory.objects.filter(trade_date="2024-07-06").exists()
        )
        self.assertEqual(
            CmPriceHistory.objects.filter(trade_date="2024-07-05").count(),
            len(real_rows),
        )
