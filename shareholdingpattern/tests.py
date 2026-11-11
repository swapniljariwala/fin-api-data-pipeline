import zoneinfo
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols
from shareholdingpattern.models import ShareholdingPattern

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

EQUITY = {
    "broadcastDate": "31-OCT-2026 11:13:25",
    "date": "30-SEP-2026",
    "employeeTrusts": "0",
    "isin": "INE786W01010",
    "name": "RKEC Projects Limited",
    "pr_and_prgrp": "57.74",
    "public_val": "42.26",
    "recordId": "216349",
    "revisedData": "N",
    "revisedDate": None,
    "revisedRemark": None,
    "revisedStatus": "-",
    "submissionDate": "31-OCT-2026",
    "symbol": "RKEC",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1750613_WEB.xml",
    "xbrlFileSize": "177.16 KB",
}
REVISED = {
    **EQUITY,
    "recordId": "216400",
    "revisedData": "Y",
    "revisedDate": "02-NOV-2026 09:10:00",
    "revisedRemark": "Corrected promoter holding",
    "revisedStatus": "Revised",
}
SME = {
    "broadcastDate": "29-OCT-2026 13:01:00",
    "date": "30-SEP-2026",
    "isin": "INE0P7201019",
    "name": "Drone Destination Limited",
    "pr_and_prgrp": "61.03",
    "public_val": "38.97",
    "recordId": "216344",
    "revisedData": None,
    "revisedStatus": "-",
    "submissionDate": "29-OCT-2026",
    "symbol": "DRONE",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_1748563_WEB.xml",
    "xbrlFileSize": "168.63 KB",
}


def _fake_nse(records):
    nse = mock.Mock()
    nse.shareholding_pattern.return_value = records
    return nse


class IngestShareholdingPatternTests(TestCase):
    def _run(self, records, **opts):
        nse = _fake_nse(records)
        opts.setdefault("days", 7)
        opts.setdefault("delay", 0)
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            call_command("ingest_shareholding_pattern", **opts)
        return nse

    def test_ingests_and_parses_fields(self):
        self._run([EQUITY])
        row = ShareholdingPattern.objects.get(record_id=216349)
        self.assertEqual(row.symbol, "RKEC")
        self.assertEqual(row.isin, "INE786W01010")
        self.assertEqual(row.company_name, "RKEC Projects Limited")
        self.assertEqual(row.quarter_end, date(2026, 9, 30))
        self.assertEqual(row.submitted_on, date(2026, 10, 31))
        self.assertEqual(row.promoter_percent, Decimal("57.74"))
        self.assertEqual(row.public_percent, Decimal("42.26"))
        self.assertFalse(row.revised)
        self.assertEqual(row.revised_status, "")
        self.assertIsNone(row.revised_at)
        self.assertEqual(row.extraction_status, "pending")
        broadcast = row.broadcast_at.astimezone(IST)
        self.assertEqual((broadcast.day, broadcast.hour, broadcast.minute), (31, 11, 13))
        self.assertEqual(row.raw["recordId"], "216349")

    def test_rerun_is_a_noop(self):
        self._run([EQUITY])
        self._run([EQUITY])
        self.assertEqual(ShareholdingPattern.objects.count(), 1)

    def test_revision_is_a_separate_row_with_flag(self):
        self._run([EQUITY, REVISED])
        rev = ShareholdingPattern.objects.get(record_id=216400)
        self.assertTrue(rev.revised)
        self.assertEqual(rev.revision_remark, "Corrected promoter holding")
        revised_ist = rev.revised_at.astimezone(IST)
        self.assertEqual((revised_ist.day, revised_ist.hour), (2, 9))
        self.assertEqual(ShareholdingPattern.objects.count(), 2)

    def test_sme_record_uses_isin(self):
        self._run([SME], segment="sme")
        row = ShareholdingPattern.objects.get(record_id=216344)
        self.assertEqual(row.segment, "sme")
        self.assertEqual(row.instrument.isin, "INE0P7201019")

    def test_rerun_after_symbol_map_exists_reuses_instrument(self):
        inst = Instrument.objects.create(isin="INE786W01010", name="RKEC Projects")
        nse_symbol = NSESymbols.objects.create(symbol="RKEC")
        NSESymbolInstrumentMap.objects.create(symbol=nse_symbol, instrument=inst)
        self._run([EQUITY])
        self.assertEqual(Instrument.objects.count(), 1)
        self.assertEqual(ShareholdingPattern.objects.get().instrument_id, inst.id)

    def test_missing_fields_are_skipped_not_fatal(self):
        bad = {**EQUITY, "recordId": None}
        bad_date = {**EQUITY, "recordId": "1", "date": "not-a-date"}
        self._run([bad, bad_date, EQUITY])
        self.assertEqual(ShareholdingPattern.objects.count(), 1)

    def test_symbol_and_segment_are_passed_to_nse(self):
        nse = self._run([EQUITY], segment="sme", symbol="RKEC")
        kwargs = nse.shareholding_pattern.call_args.kwargs
        self.assertEqual(kwargs["segment"], "sme")
        self.assertEqual(kwargs["symbol"], "RKEC")

    def test_fetch_failure_raises_command_error(self):
        nse = mock.Mock()
        nse.shareholding_pattern.side_effect = RuntimeError("boom")
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            with self.assertRaises(CommandError):
                call_command(
                    "ingest_shareholding_pattern", days=1, delay=0, retries=2
                )
