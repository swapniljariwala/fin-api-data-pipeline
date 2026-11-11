import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from securityinfo.management.commands import ingest_stock_classification as cmd
from securityinfo.models import Instrument, InstrumentClassification

HEADER = "TradDt,FinInstrmTp,ISIN,TckrSymb,SctySrs\n"


def _quote(isin, sector="Information Technology"):
    return {
        "metaData": {"isinCode": isin},
        "secInfo": {
            "macro": sector, "sector": sector,
            "industryInfo": "IT - Software",
            "basicIndustry": "Computers - Software & Consulting",
        },
    }


class StockClassificationTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "bhav.csv"
        self.path.write_text(
            HEADER
            + "2026-11-06,STK,INE001,AAA,BE\n"
            + "2026-11-06,STK,INE001,AAA,EQ\n"
            + "2026-11-06,STK,INE002,BBB,EQ\n"
            + "2026-11-06,STK,INE003,CCC,EQ\n"
            + "2026-11-06,STK,INE999,NOINST,EQ\n"
        )
        for isin in ("INE001", "INE002", "INE003"):
            Instrument.objects.create(isin=isin, name=isin)

    def run_cmd(self, quotes, **kw):
        calls = []

        def fake(symbol, series="EQ"):
            calls.append((symbol, series))
            q = quotes[symbol]
            if isinstance(q, Exception):
                raise q
            return q

        nse = mock.Mock()
        nse.stock_quote.side_effect = fake
        with mock.patch.object(cmd.Command, "_new_session", return_value=nse):
            call_command("ingest_stock_classification", file=str(self.path),
                         delay=0, **kw)
        return calls

    def test_pairs_prefer_eq_and_skip_unknown(self):
        pairs = cmd.read_bhavcopy_pairs(self.path)
        self.assertEqual(pairs["INE001"], ("AAA", "EQ"))

    def test_ok_not_found_and_idempotent(self):
        quotes = {
            "AAA": _quote("INE001"),
            "BBB": IndexError(),  # empty equityResponse
            "CCC": _quote("INE003"),
        }
        calls = self.run_cmd(quotes)
        self.assertEqual(len(calls), 3)  # INE999 has no Instrument
        c = InstrumentClassification.objects.get(instrument__isin="INE001")
        self.assertEqual((c.status, c.sector, c.basic_industry),
                         ("ok", "Information Technology",
                          "Computers - Software & Consulting"))
        self.assertEqual(
            InstrumentClassification.objects.get(instrument__isin="INE002").status,
            "not_found")
        self.assertEqual(self.run_cmd(quotes), [])  # nothing left to do
        self.assertEqual(len(self.run_cmd(quotes, retry_not_found=True)), 1)

    def test_isin_mismatch_is_skipped_and_fails_run(self):
        quotes = {"AAA": _quote("INEOTHER"), "BBB": _quote("INE002"),
                  "CCC": _quote("INE003")}
        with self.assertRaises(CommandError):
            self.run_cmd(quotes)
        self.assertFalse(
            InstrumentClassification.objects.filter(instrument__isin="INE001").exists())
        self.assertEqual(InstrumentClassification.objects.count(), 2)

    def test_transient_error_leaves_no_row(self):
        quotes = {"AAA": RuntimeError("boom"), "BBB": _quote("INE002"),
                  "CCC": _quote("INE003")}
        with self.assertRaises(CommandError):
            self.run_cmd(quotes, retries=2)
        self.assertEqual(InstrumentClassification.objects.count(), 2)

    def test_limit_and_symbol_and_dry_run(self):
        quotes = {"AAA": _quote("INE001"), "BBB": _quote("INE002"),
                  "CCC": _quote("INE003")}
        self.assertEqual(len(self.run_cmd(quotes, dry_run=True)), 0)
        self.assertEqual(self.run_cmd(quotes, symbol="bbb"), [("BBB", "EQ")])
        self.assertEqual(len(self.run_cmd(quotes, limit=1)), 1)
