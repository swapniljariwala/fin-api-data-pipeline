import zoneinfo
from datetime import date
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from integratedfilings.models import IntegratedFiling
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# Shapes taken from live NSE responses (TCS, Q2 FY27).
CONSOLIDATED = {
    "attFileSize": "0 Bytes", "audited": "Audited",
    "broadcast_Date": "08-Oct-2026 22:07:33", "cmName": "Tata Consultancy Services Limited",
    "consolidated": "Consolidated", "creation_Date": "08-Oct-2026 22:08:10",
    "ixbrl": "https://nsearchives.nseindia.com/corporate/ixbrl/A_iXBRL_WEB.html",
    "ixbrlFileSize": "86.96 KB", "qe_Date": "30-SEP-2026", "revised_Date": None,
    "revision_Remark": None, "seq_Id": "200121",
    "smName": "Tata Consultancy Services Limited", "symbol": "TCS",
    "type": "Integrated Filing- Financials", "type_Sub": "Original",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/A_WEB.xml",
    "xbrlFileSize": "104.84 KB",
}
STANDALONE = {
    **CONSOLIDATED, "consolidated": "Standalone", "seq_Id": "200120",
    "broadcast_Date": "08-Oct-2026 22:07:30",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/B_WEB.xml",
}
REVISION = {
    **STANDALONE, "seq_Id": "201999", "type_Sub": "Revision",
    "revised_Date": "10-Oct-2026 11:00:00", "revision_Remark": "Corrected EPS",
    "broadcast_Date": "10-Oct-2026 11:00:00",
}


def _fake_nse(pages):
    """NSELive stand-in whose corporate_integrated_filing returns `pages` in order."""
    total = sum(len(p) for p in pages)
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        data = pages[kwargs["page"] - 1] if kwargs["page"] <= len(pages) else []
        return {"data": data, "page": kwargs["page"] - 1, "totalCount": total}

    nse = mock.Mock()
    nse.corporate_integrated_filing.side_effect = fn
    nse.calls = calls
    return nse


class IngestIntegratedFilingsTests(TestCase):
    def _run(self, pages, **opts):
        nse = _fake_nse(pages)
        opts.setdefault("delay", 0)
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            call_command("ingest_integrated_filings", **opts)
        return nse

    def test_ingests_all_pages_and_parses_fields(self):
        nse = self._run([[CONSOLIDATED], [STANDALONE]], page_size=1, days=7)

        self.assertEqual(IntegratedFiling.objects.count(), 2)
        self.assertEqual([c["page"] for c in nse.calls], [1, 2])
        f = IntegratedFiling.objects.get(seq_id=200121)
        self.assertTrue(f.consolidated)
        self.assertFalse(IntegratedFiling.objects.get(seq_id=200120).consolidated)
        self.assertEqual(f.period_end, date(2026, 9, 30))
        self.assertEqual(f.type_sub, "Original")
        self.assertEqual(f.audited, "Audited")
        self.assertEqual(f.xbrl_url, CONSOLIDATED["xbrl"])
        self.assertEqual(f.xbrl_file_size, "104.84 KB")
        self.assertIsNone(f.revised_at)
        self.assertEqual(f.revision_remark, "")
        # stored timezone-aware; the DB hands back UTC, so compare in IST
        broadcast_ist = f.broadcast_at.astimezone(IST)
        self.assertEqual(
            (broadcast_ist.day, broadcast_ist.hour, broadcast_ist.minute), (8, 22, 7)
        )
        self.assertEqual(f.raw["symbol"], "TCS")

    def test_rerun_is_a_noop(self):
        self._run([[CONSOLIDATED, STANDALONE]], days=7)
        self._run([[CONSOLIDATED, STANDALONE]], days=7)
        self.assertEqual(IntegratedFiling.objects.count(), 2)

    def test_revision_is_a_separate_row(self):
        self._run([[STANDALONE, REVISION]], days=7)
        rev = IntegratedFiling.objects.get(seq_id=201999)
        self.assertEqual(rev.type_sub, "Revision")
        self.assertEqual(rev.revision_remark, "Corrected EPS")
        revised_ist = rev.revised_at.astimezone(IST)
        self.assertEqual((revised_ist.day, revised_ist.hour), (10, 11))
        self.assertEqual(IntegratedFiling.objects.count(), 2)

    def test_live_revision_has_null_broadcast_date(self):
        # Real NSE revisions: broadcast_Date is null, the time is in revised_Date.
        live_rev = {**STANDALONE, "seq_Id": "210047", "type_Sub": "Revision",
                    "broadcast_Date": None, "revised_Date": "05-NOV-2026 10:38:25",
                    "creation_Date": "05-Nov-2026 10:39:34"}
        no_dates = {**STANDALONE, "seq_Id": "210048", "broadcast_Date": None,
                    "revised_Date": None, "creation_Date": "05-Nov-2026 10:39:34"}
        self._run([[live_rev, no_dates]], days=7)
        a = IntegratedFiling.objects.get(seq_id=210047).broadcast_at.astimezone(IST)
        b = IntegratedFiling.objects.get(seq_id=210048).broadcast_at.astimezone(IST)
        self.assertEqual((a.day, a.hour, a.minute), (5, 10, 38))   # from revised_Date
        self.assertEqual((b.day, b.hour, b.minute), (5, 10, 39))   # from creation_Date

    def test_governance_consolidated_is_null(self):
        gov = {**STANDALONE, "seq_Id": "300001", "consolidated": None,
               "type": "Integrated Filing- Governance"}
        self._run([[gov]], days=7)
        self.assertIsNone(IntegratedFiling.objects.get(seq_id=300001).consolidated)

    def test_uses_existing_symbol_map_before_creating(self):
        inst = Instrument.objects.create(isin="INE467B01029", name="TCS LTD")
        NSESymbolInstrumentMap.objects.create(
            symbol=NSESymbols.objects.create(symbol="TCS"), instrument=inst
        )
        self._run([[CONSOLIDATED]], days=7)
        self.assertEqual(IntegratedFiling.objects.get().instrument, inst)
        self.assertEqual(Instrument.objects.count(), 1)

    def test_creates_isinless_instrument_for_unknown_symbol(self):
        self._run([[CONSOLIDATED, STANDALONE]], days=7)
        inst = Instrument.objects.get()
        self.assertIsNone(inst.isin)
        self.assertEqual(inst.name, "TCS")
        self.assertTrue(
            NSESymbolInstrumentMap.objects.filter(
                symbol__symbol="TCS", instrument=inst
            ).exists()
        )

    def test_bad_records_are_skipped_not_fatal(self):
        bad = [{**CONSOLIDATED, "seq_Id": None}, {**CONSOLIDATED, "seq_Id": "abc"},
               {**CONSOLIDATED, "seq_Id": "5", "qe_Date": "garbage"}]
        self._run([bad + [STANDALONE]], days=7)
        self.assertEqual(list(IntegratedFiling.objects.values_list("seq_id", flat=True)), [200120])

    def test_filters_are_passed_to_nse(self):
        from datetime import timedelta
        nse = self._run([[CONSOLIDATED]], symbol="TCS", segment="sme",
                        period_ended="30-Sep-2026", filing_type="governance", days=3)
        call = nse.calls[0]
        self.assertEqual(call["symbol"], "TCS")
        self.assertEqual(call["index"], "sme")
        self.assertEqual(call["period_ended"], "30-Sep-2026")
        self.assertEqual(call["filing_type"], "Integrated Filing- Governance")
        self.assertEqual(call["to_date"] - call["from_date"], timedelta(days=2))

    def test_failed_page_is_retried_then_raises(self):
        nse = mock.Mock()
        nse.corporate_integrated_filing.side_effect = RuntimeError("boom")
        with mock.patch("jugaad_data.nse.live.NSELive", return_value=nse):
            with self.assertRaises(CommandError):
                call_command("ingest_integrated_filings", days=1, delay=0, retries=3)
        self.assertEqual(nse.corporate_integrated_filing.call_count, 3)

    def test_empty_result_is_fine(self):
        self._run([[]], days=1)
        self.assertEqual(IntegratedFiling.objects.count(), 0)

    def test_argument_validation(self):
        for opts in ({"days": 0}, {"to_date": "2026-10-01"}, {"from_date": "2026-10-05", "to_date": "2026-10-01"}):
            with self.assertRaises(CommandError):
                call_command("ingest_integrated_filings", **opts)
