import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
import zoneinfo

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from integratedfilings.extraction import ExtractionError, fy_and_quarter
from integratedfilings.models import IntegratedFiling, IntegratedFilingStatement
from securityinfo.models import Instrument

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
XML = (Path(__file__).parent / "testdata" / "denta_q2_fy27_standalone.xml").read_bytes()
COMMAND = "integratedfilings.management.commands.extract_integrated_filings"
BASE = "filings/DENTA/FY2027/Q2/standalone"


class FyQuarterTests(SimpleTestCase):
    def test_indian_financial_year(self):
        self.assertEqual(fy_and_quarter(date(2026, 6, 30)), ("FY2027", "Q1"))
        self.assertEqual(fy_and_quarter(date(2026, 9, 30)), ("FY2027", "Q2"))
        self.assertEqual(fy_and_quarter(date(2026, 12, 31)), ("FY2027", "Q3"))
        self.assertEqual(fy_and_quarter(date(2027, 3, 31)), ("FY2027", "Q4"))

    def test_non_quarter_end_is_rejected(self):
        with self.assertRaises(ExtractionError):
            fy_and_quarter(date(2026, 8, 31))


def make_filing(instrument, seq_id, hours=0, **kw):
    fields = dict(
        instrument=instrument, seq_id=seq_id, filing_type="Integrated Filing- Financials",
        type_sub="Original", period_end=date(2026, 9, 30), consolidated=False,
        audited="Un-Audited", xbrl_url=f"https://nsearchives.example/{seq_id}.xml",
        broadcast_at=datetime(2026, 11, 6, 21, 46, tzinfo=IST) + timedelta(hours=hours),
        raw={"symbol": "DENTA"},
    )
    fields.update(kw)
    return IntegratedFiling.objects.create(**fields)


class ExtractCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inst = Instrument.objects.create(isin=None, name="DENTA")

    def _run(self, body=XML, error=None, **opts):
        session = mock.MagicMock()
        if error:
            session.get.side_effect = error
        else:
            session.get.return_value = mock.Mock(content=body, raise_for_status=lambda: None)
        opts.setdefault("delay", 0)
        opts.setdefault("local_dir", str(self.root))
        with mock.patch(f"{COMMAND}.requests.Session", return_value=session):
            call_command("extract_integrated_filings", **opts)
        return session

    def _json(self, rel):
        return json.loads((self.root / rel).read_text())

    def test_extracts_statements_and_marks_done(self):
        f = make_filing(self.inst, 211600)
        self._run()

        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "done")
        self.assertEqual(f.extraction_attempts, 1)
        self.assertIsNotNone(f.extracted_at)
        self.assertTrue((self.root / "raw/DENTA/FY2027/Q2/standalone/211600.xml").exists())

        pnl = IntegratedFilingStatement.objects.get(filing=f, statement="profit_and_loss")
        self.assertEqual(pnl.role_code, "100200")
        self.assertEqual(pnl.periods, ["Q2", "H1"])
        self.assertEqual(pnl.object_path, f"{BASE}/profit_and_loss.json")
        self.assertEqual(pnl.item_count, 2)
        self.assertEqual(pnl.size_bytes, (self.root / pnl.object_path).stat().st_size)
        self.assertTrue(f.statements.filter(statement="balance_sheet").exists())

        # every indexed statement exists in the bucket, and the manifest lists them all
        manifest = self._json(f"{BASE}/_manifest.json")
        self.assertEqual({s["statement"] for s in manifest["statements"]},
                         set(f.statements.values_list("statement", flat=True)))
        for s in manifest["statements"]:
            self.assertTrue((self.root / s["path"]).exists())
        self.assertEqual((manifest["fy"], manifest["quarter"], manifest["basis"]),
                         ("FY2027", "Q2", "standalone"))

    def test_statement_file_holds_all_periods(self):
        make_filing(self.inst, 211600)
        self._run()
        doc = self._json(f"{BASE}/profit_and_loss.json")
        self.assertEqual([e["period"]["label"] for e in doc["entries"]], ["Q2", "H1"])
        self.assertEqual(doc["source"]["seq_id"], 211600)
        self.assertEqual(doc["company"]["symbol"], "DENTA")
        self.assertEqual(doc["role_code"], "100200")

    def test_canonical_file_written(self):
        make_filing(self.inst, 211600)
        self._run()
        canon = self._json(f"{BASE}/canonical.json")["canonical"]
        self.assertIsInstance(canon["net_profit"], (int, float))
        self.assertIn("eps_basic", canon)

    def test_done_filing_is_not_picked_up_again(self):
        make_filing(self.inst, 211600)
        self._run()
        session = self._run()
        session.get.assert_not_called()

    def test_download_failure_marks_failed_and_counts_attempts(self):
        f = make_filing(self.inst, 211600)
        with self.assertRaises(CommandError):
            self._run(error=requests.ConnectionError("boom"))
        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "failed")
        self.assertEqual(f.extraction_attempts, 1)
        self.assertIn("boom", f.extraction_error)
        self.assertFalse(f.statements.exists())

    def test_failed_is_retried_then_given_up_after_max_attempts(self):
        f = make_filing(self.inst, 211600)
        for _ in range(3):
            with self.assertRaises(CommandError):
                self._run(error=requests.ConnectionError("boom"))
        f.refresh_from_db()
        self.assertEqual(f.extraction_attempts, 3)
        session = self._run()  # attempts exhausted: not selected, nothing to do
        session.get.assert_not_called()
        # --seq-id forces it regardless
        self._run(seq_id=211600)
        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "done")

    def test_failed_filing_recovers_on_next_run(self):
        f = make_filing(self.inst, 211600)
        with self.assertRaises(CommandError):
            self._run(error=requests.ConnectionError("boom"))
        self._run()
        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "done")
        self.assertEqual(f.extraction_attempts, 2)

    def test_non_xml_download_fails(self):
        f = make_filing(self.inst, 211600)
        with self.assertRaises(CommandError):
            self._run(body=b"Access Denied")
        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "failed")
        self.assertIn("not XML", f.extraction_error)

    def test_unparseable_xml_fails_without_uploading_statements(self):
        f = make_filing(self.inst, 211600)
        with self.assertRaises(CommandError):
            self._run(body=b"<xbrl>not a filing</xbrl>")
        f.refresh_from_db()
        self.assertEqual(f.extraction_status, "failed")
        self.assertFalse((self.root / "filings").exists())

    def test_one_failure_does_not_stop_the_others(self):
        bad = make_filing(self.inst, 1, hours=0, xbrl_url="https://nsearchives.example/bad.xml")
        good = make_filing(self.inst, 2, hours=1, consolidated=True)

        def get(url, timeout=None):
            if "bad" in url:
                raise requests.ConnectionError("boom")
            return mock.Mock(content=XML, raise_for_status=lambda: None)

        session = mock.MagicMock()
        session.get.side_effect = get
        with mock.patch(f"{COMMAND}.requests.Session", return_value=session):
            with self.assertRaises(CommandError):
                call_command("extract_integrated_filings", local_dir=str(self.root), delay=0)
        bad.refresh_from_db(); good.refresh_from_db()
        self.assertEqual(bad.extraction_status, "failed")
        self.assertEqual(good.extraction_status, "done")
        self.assertTrue((self.root / "filings/DENTA/FY2027/Q2/consolidated/_manifest.json").exists())

    def test_governance_and_basisless_filings_are_ignored(self):
        make_filing(self.inst, 1, filing_type="Integrated Filing- Governance", consolidated=None)
        make_filing(self.inst, 2, xbrl_url="")
        session = self._run()
        session.get.assert_not_called()

    def test_revision_replaces_index_rows_of_the_earlier_release(self):
        original = make_filing(self.inst, 211600)
        self._run()
        revision = make_filing(self.inst, 211700, hours=48, type_sub="Revision")
        self._run()
        self.assertFalse(original.statements.exists())
        self.assertTrue(revision.statements.exists())
        # both raw releases are kept; one set of statement objects
        raws = sorted(p.name for p in (self.root / "raw/DENTA/FY2027/Q2/standalone").iterdir())
        self.assertEqual(raws, ["211600.xml", "211700.xml"])

    def test_older_release_extracted_late_does_not_overwrite_newer(self):
        make_filing(self.inst, 211600)  # original, still pending
        revision = make_filing(self.inst, 211700, hours=48, type_sub="Revision")
        self._run(seq_id=211700)
        before = (self.root / BASE / "profit_and_loss.json").read_bytes()

        self._run()  # now the original is processed
        original = IntegratedFiling.objects.get(seq_id=211600)
        self.assertEqual(original.extraction_status, "done")
        self.assertIn("superseded by seq_id 211700", original.extraction_error)
        self.assertFalse(original.statements.exists())
        self.assertTrue(revision.statements.exists())
        self.assertEqual((self.root / BASE / "profit_and_loss.json").read_bytes(), before)
        self.assertTrue((self.root / "raw/DENTA/FY2027/Q2/standalone/211600.xml").exists())

    def test_symbol_with_special_characters_is_path_safe(self):
        make_filing(self.inst, 5, raw={"symbol": "M&M"})
        self._run()
        self.assertTrue((self.root / "filings/M%26M/FY2027/Q2/standalone/_manifest.json").exists())

    def test_no_destination_is_an_error(self):
        make_filing(self.inst, 211600)
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GCS_INTEGRATED_FILINGS_BUCKET", None)
            with self.assertRaises(CommandError) as ctx:
                call_command("extract_integrated_filings", delay=0)
        self.assertIn("GCS_INTEGRATED_FILINGS_BUCKET", str(ctx.exception))

    def test_bucket_comes_from_environment(self):
        make_filing(self.inst, 211600)
        fake = mock.Mock()
        with mock.patch.dict("os.environ", {"GCS_INTEGRATED_FILINGS_BUCKET": "my-bucket"}):
            with mock.patch("integratedfilings.storage.GCSStorage", return_value=fake) as gcs:
                session = mock.MagicMock()
                session.get.return_value = mock.Mock(content=XML, raise_for_status=lambda: None)
                with mock.patch(f"{COMMAND}.requests.Session", return_value=session):
                    call_command("extract_integrated_filings", delay=0)
        gcs.assert_called_once_with("my-bucket")
        paths = [c.args[0] for c in fake.put.call_args_list]
        self.assertIn("raw/DENTA/FY2027/Q2/standalone/211600.xml", paths)
        self.assertEqual(paths[-1], f"{BASE}/_manifest.json")  # manifest is written last

    def test_newest_filings_are_extracted_first(self):
        old = make_filing(self.inst, 1, hours=0, consolidated=True)
        new = make_filing(self.inst, 2, hours=500, consolidated=False)
        self._run(limit=1)
        old.refresh_from_db(); new.refresh_from_db()
        self.assertEqual((new.extraction_status, old.extraction_status), ("done", "pending"))

    def test_limit_and_argument_validation(self):
        for n in (1, 2, 3):
            make_filing(self.inst, n, hours=n, consolidated=bool(n % 2))
        self._run(limit=2)
        self.assertEqual(IntegratedFiling.objects.filter(extraction_status="done").count(), 2)
        with self.assertRaises(CommandError):
            call_command("extract_integrated_filings", limit=0, local_dir=str(self.root))
