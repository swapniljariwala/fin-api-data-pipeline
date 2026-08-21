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

        # A known row: 20MICRONS, EQ series.
        ticker = InstrumentTicker.objects.get(source="NSE", ticker="20MICRONS")
        row = CmPriceHistory.objects.get(
            instrument_ticker=ticker, series__code="EQ"
        )
        self.assertEqual(row.open, Decimal("227.0000"))
        self.assertEqual(row.close, Decimal("224.7200"))
        # TURNOVER_LACS 452.23 -> full rupees.
        self.assertEqual(row.turnover, Decimal("45223000.00"))
        self.assertEqual(row.volume, 201028)
        # Legacy rows carry no ISIN.
        self.assertIsNone(ticker.instrument.isin)

    def test_ingest_udiff_file(self):
        data_dir = self._copy_sample("cm20Aug2026bhav.csv")
        _run(
            from_date="2026-08-20",
            to_date="2026-08-20",
            data_dir=data_dir,
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.format, "udiff")
        self.assertEqual(bf.row_count, CmPriceHistory.objects.count())
        self.assertGreater(bf.row_count, 0)

        # UDiFF populates ISIN, name and the exchange instrument id.
        instrument = Instrument.objects.get(isin="IN0020200104")
        self.assertEqual(instrument.name, "2.5%GOLDBONDS2028SR-III")
        ticker = InstrumentTicker.objects.get(
            instrument=instrument, source="NSE", ticker="SGBJUN28"
        )
        self.assertEqual(ticker.fin_instrm_id, 19078)

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
        # Sample file is from 2026-08-20; today in test env is 2026-08-21.
        data_dir = self._copy_sample("cm20Aug2026bhav.csv")
        _run(days=2, data_dir=data_dir, no_download=True)
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
        )

    def test_latest_no_download(self):
        data_dir = self._copy_sample("cm20Aug2026bhav.csv")
        _run(latest=True, data_dir=data_dir, no_download=True)
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
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
        data_dir = self._copy_sample("cm20Aug2026bhav.csv")
        _run(
            from_date="2026-08-20",
            to_date="2026-08-20",
            data_dir=data_dir,
            no_download=True,
        )
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
        )
        self.assertFalse(
            Path(data_dir, "cm20Aug2026bhav.csv").exists(),
            "bhavcopy file should be removed after ingestion",
        )

    def test_delay_throttles_nse_api_calls(self):
        base = Path(tempfile.mkdtemp())

        def fake_save(day, dest, skip_if_present=True):
            name = f"cm{day:%d%b%Y}bhav.csv"
            target = base / name
            if not target.is_file():
                shutil.copy(SAMPLE_DIR / "cm20Aug2026bhav.csv", target)
            return str(target)

        with mock.patch(
            "jugaad_data.nse.archives.NSEArchives"
        ) as archives_cls, mock.patch(
            "dailypricehistory.management.commands.ingest_bhavcopy.time.sleep"
        ) as sleep:
            archives_cls.return_value.bhavcopy_save.side_effect = fake_save
            _run(
                from_date="2026-08-19",
                to_date="2026-08-20",
                data_dir=str(base),
                delay=1.5,
            )

        # Two downloads, so exactly one wait between them.
        sleep.assert_called_once_with(1.5)

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
                shutil.copy(SAMPLE_DIR / "cm20Aug2026bhav.csv", target)
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
                from_date="2026-08-20",
                to_date="2026-08-20",
                data_dir=str(base),
                retries=3,
            )

        self.assertEqual(calls["n"], 2, "first attempt fails, second succeeds")
        self.assertTrue(
            BhavcopyFile.objects.filter(trade_date="2026-08-20").exists()
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
