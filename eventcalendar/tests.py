import zoneinfo
from datetime import date
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from eventcalendar.models import EventCalendar
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

EQUITY = {
    "symbol": "POWERGRID",
    "company": "Power Grid Corporation of India Limited",
    "purpose": "Financial Results/Dividend",
    "bm_desc": "To consider and approve the Unaudited Financial Results for Q2.",
    "date": "01-Nov-2026",
}
SME = {
    "bm_symbol": "TAURIAN",
    "bm_date": "02-Nov-2026",
    "bm_purpose": "Board Meeting Intimation",
    "bm_desc": "Taurian MPS Limited has informed the Exchange about its board meeting.",
    "bm_timestamp": "28-Oct-2026 23:42:49",
    "sm_name": "Taurian MPS Limited",
    "sm_isin": "77366",
    "attFileSize": "14.48 KB",
    "attachment": "https://nsearchives.nseindia.com/corporate/PRIOR_INTIMATION.xml",
    "ixbrl": "https://nsearchives.nseindia.com/corporate/ixbrl/PRIOR_INTIMATION.html",
}


def _fake_nse(records):
    nse = mock.Mock()
    nse.event_calendar.return_value = records
    return nse


class IngestEventCalendarTests(TestCase):
    def _run(self, records, **opts):
        nse = _fake_nse(records)
        opts.setdefault("days", 7)
        opts.setdefault("delay", 0)
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            call_command("ingest_event_calendar", **opts)
        return nse

    def test_ingests_and_parses_equity_fields(self):
        self._run([EQUITY])
        row = EventCalendar.objects.get()
        self.assertEqual(row.symbol, "POWERGRID")
        self.assertEqual(row.company_name, "Power Grid Corporation of India Limited")
        self.assertEqual(row.event_date, date(2026, 11, 1))
        self.assertEqual(row.purpose, "Financial Results/Dividend")
        self.assertIn("Unaudited Financial Results", row.description)
        self.assertIsNone(row.announced_at)
        self.assertEqual(row.segment, "equities")

    def test_ingests_and_parses_sme_fields(self):
        self._run([SME], segment="sme")
        row = EventCalendar.objects.get()
        self.assertEqual(row.symbol, "TAURIAN")
        self.assertEqual(row.company_name, "Taurian MPS Limited")
        self.assertEqual(row.event_date, date(2026, 11, 2))
        self.assertEqual(row.purpose, "Board Meeting Intimation")
        self.assertEqual(row.attachment_file_size, "14.48 KB")
        self.assertTrue(row.attachment_url.endswith("PRIOR_INTIMATION.xml"))
        announced = row.announced_at.astimezone(IST)
        self.assertEqual((announced.day, announced.hour, announced.minute), (28, 23, 42))
        self.assertEqual(row.segment, "sme")

    def test_rerun_is_a_noop(self):
        self._run([EQUITY])
        self._run([EQUITY])
        self.assertEqual(EventCalendar.objects.count(), 1)

    def test_same_event_on_two_dates_is_not_a_duplicate(self):
        other = {**EQUITY, "date": "02-Nov-2026"}
        self._run([EQUITY, other])
        self.assertEqual(EventCalendar.objects.count(), 2)

    def test_missing_symbol_or_date_is_skipped(self):
        no_symbol = {**EQUITY, "symbol": None}
        no_date = {**EQUITY, "date": "not-a-date"}
        self._run([no_symbol, no_date, EQUITY])
        self.assertEqual(EventCalendar.objects.count(), 1)

    def test_symbol_filter_is_applied_locally(self):
        other = {
            **EQUITY,
            "symbol": "TCS",
            "company": "Tata Consultancy Services Limited",
        }
        self._run([EQUITY, other], symbol="tcs")
        self.assertEqual(EventCalendar.objects.count(), 1)
        self.assertEqual(EventCalendar.objects.get().symbol, "TCS")

    def test_latest_window_includes_upcoming_events(self):
        nse = self._run([EQUITY], days=None)
        kwargs = nse.event_calendar.call_args.kwargs
        self.assertLess(kwargs["from_date"], date.today())
        self.assertGreater(kwargs["to_date"], date.today())

    def test_existing_symbol_map_is_reused(self):
        inst = Instrument.objects.create(isin="INE752E01010", name="Power Grid")
        nse_symbol = NSESymbols.objects.create(symbol="POWERGRID")
        NSESymbolInstrumentMap.objects.create(symbol=nse_symbol, instrument=inst)
        self._run([EQUITY])
        self.assertEqual(Instrument.objects.count(), 1)
        self.assertEqual(EventCalendar.objects.get().instrument_id, inst.id)

    def test_fetch_failure_raises_command_error(self):
        nse = mock.Mock()
        nse.event_calendar.side_effect = RuntimeError("boom")
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            with self.assertRaises(CommandError):
                call_command("ingest_event_calendar", days=1, delay=0, retries=2)
