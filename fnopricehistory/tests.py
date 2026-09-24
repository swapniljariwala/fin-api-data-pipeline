import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from dailypricehistory.models import BhavcopyFile
from fnopricehistory.models import FnoContract, FnoPriceHistory

LEGACY_HEADER = (
    "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
    "SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"
)
LEGACY_FUT_ROW = (
    "FUTSTK,RELIANCE,25-JUL-2024,0,XX,2900,2950,2890,2940,2940,1000,"
    "2000,50000,500,05-JUL-2024\n"
)
LEGACY_OPT_ROW = (
    "OPTSTK,RELIANCE,25-JUL-2024,3000,CE,50,60,45,55,55,500,800,20000,"
    "-200,05-JUL-2024\n"
)

UDIFF_HEADER = (
    "Sgmt,TradDt,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,FinInstrmId,"
    "OpnPric,HghPric,LwPric,ClsPric,SttlmPric,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,OpnIntrst,ChngInOpnIntrst,UndrlygPric\n"
)
UDIFF_FUT_ROW = (
    "FO,2026-08-28,STF,RELIANCE,2026-08-28,,,12345,2900,2950,2890,2940,2940,"
    "1000,294000000,50,50000,500,2935.5\n"
)
UDIFF_OPT_ROW = (
    "FO,2026-08-28,STO,RELIANCE,2026-08-28,3000,CE,12346,50,60,45,55,55,500,"
    "2750000,30,20000,-200,2935.5\n"
)
UDIFF_NON_FO_ROW = (
    "CM,2026-08-28,STK,RELIANCE,2026-08-28,,,99999,2900,2950,2890,2940,,"
    "1000,294000000,50,,,\n"
)


def _write(dir_path, name, content):
    path = Path(dir_path) / name
    path.write_text(content, encoding="utf-8")
    return str(dir_path)


def _run(*args, **kwargs):
    kwargs.setdefault("verbosity", 0)
    call_command("ingest_fno_bhavcopy", *args, **kwargs)


class IngestFnoBhavcopyTests(TestCase):
    def test_ingest_legacy_file(self):
        data_dir = tempfile.mkdtemp()
        _write(
            data_dir,
            "fo05Jul2024bhav.csv",
            LEGACY_HEADER + LEGACY_FUT_ROW + LEGACY_OPT_ROW,
        )
        _run(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.file_name, "fo05Jul2024bhav.csv")
        self.assertEqual(bf.segment, "FO")
        self.assertEqual(bf.format, "legacy")
        self.assertEqual(bf.row_count, 2)

        # Legacy INSTRUMENT values are normalized to their UDiFF equivalents
        # so both formats land in the same column (FUTSTK -> STF, OPTSTK -> STO).
        fut = FnoContract.objects.get(instrument_type="STF")
        self.assertIsNone(fut.option_type)
        fut_price = FnoPriceHistory.objects.get(contract=fut)
        self.assertEqual(fut_price.open, Decimal("2900"))
        self.assertEqual(fut_price.turnover, Decimal("200000000"))
        self.assertEqual(fut_price.open_interest, 50000)

        opt = FnoContract.objects.get(instrument_type="STO")
        self.assertEqual(opt.option_type, "CE")
        self.assertEqual(opt.strike_price, Decimal("3000"))

    def test_ingest_udiff_file(self):
        data_dir = tempfile.mkdtemp()
        _write(
            data_dir,
            "fo28Aug2026bhav.csv",
            UDIFF_HEADER + UDIFF_FUT_ROW + UDIFF_OPT_ROW + UDIFF_NON_FO_ROW,
        )
        _run(
            from_date="2026-08-28",
            to_date="2026-08-28",
            data_dir=data_dir,
            no_download=True,
        )

        bf = BhavcopyFile.objects.get()
        self.assertEqual(bf.format, "udiff")
        # Only the two FO rows are ingested; the CM row is filtered out.
        self.assertEqual(bf.row_count, 2)
        self.assertEqual(FnoPriceHistory.objects.count(), 2)

        fut = FnoContract.objects.get(instrument_type="STF")
        self.assertEqual(fut.fin_instrm_id, 12345)
        self.assertIsNone(fut.strike_price)

        opt = FnoContract.objects.get(instrument_type="STO")
        self.assertEqual(opt.option_type, "CE")
        self.assertEqual(opt.strike_price, Decimal("3000"))
        opt_price = FnoPriceHistory.objects.get(contract=opt)
        self.assertEqual(opt_price.underlying_value, Decimal("2935.5"))

    def test_rerun_is_a_noop(self):
        data_dir = tempfile.mkdtemp()
        _write(
            data_dir,
            "fo05Jul2024bhav.csv",
            LEGACY_HEADER + LEGACY_FUT_ROW + LEGACY_OPT_ROW,
        )
        kwargs = dict(
            from_date="2024-07-05",
            to_date="2024-07-05",
            data_dir=data_dir,
            no_download=True,
        )
        _run(**kwargs)
        first_count = FnoPriceHistory.objects.count()
        _run(**kwargs)

        self.assertEqual(BhavcopyFile.objects.count(), 1)
        self.assertEqual(FnoPriceHistory.objects.count(), first_count)

    def test_missing_file_reports_no_data(self):
        with tempfile.TemporaryDirectory() as data_dir:
            _run(
                from_date="2024-07-06",
                to_date="2024-07-06",
                data_dir=data_dir,
                no_download=True,
            )
        self.assertEqual(BhavcopyFile.objects.count(), 0)
        self.assertEqual(FnoPriceHistory.objects.count(), 0)


class PurgeExpiredFnoContractsTests(TestCase):
    def test_purge_expired_contracts(self):
        expired = FnoContract.objects.create(
            underlying_symbol="OLDSYM",
            instrument_type="FUTSTK",
            expiry_date="2020-01-30",
        )
        FnoPriceHistory.objects.create(
            contract=expired, trade_date="2020-01-30", close=Decimal("100")
        )
        current = FnoContract.objects.create(
            underlying_symbol="RELIANCE",
            instrument_type="FUTSTK",
            expiry_date="2026-12-31",
        )

        call_command("purge_expired_fno_contracts", verbosity=0)
        self.assertTrue(FnoContract.objects.filter(id=expired.id).exists())

        call_command("purge_expired_fno_contracts", apply=True, verbosity=0)
        self.assertFalse(FnoContract.objects.filter(id=expired.id).exists())
        self.assertFalse(
            FnoPriceHistory.objects.filter(contract_id=expired.id).exists()
        )
        self.assertTrue(FnoContract.objects.filter(id=current.id).exists())
