import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock
import zoneinfo

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from securityinfo.models import Instrument
from shareholdingpattern.extraction import ExtractionError, object_paths
from shareholdingpattern.models import ShareholdingPattern

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
XML = (Path(__file__).parent / "testdata" / "sample_shp.xml").read_bytes()
COMMAND = "shareholdingpattern.management.commands.extract_shareholding_pattern"
BASE = "shareholding/RKEC/2026-09-30/216349"


def make_filing(instrument, record_id=216349, **kw):
    fields = dict(
        instrument=instrument,
        record_id=record_id,
        isin="INE786W01010",
        symbol="RKEC",
        company_name="RKEC Projects Limited",
        segment="equities",
        quarter_end=date(2026, 9, 30),
        submitted_on=date(2026, 10, 31),
        broadcast_at=datetime(2026, 10, 31, 11, 13, tzinfo=IST),
        xbrl_url=f"https://nsearchives.example/{record_id}.xml",
        raw={"symbol": "RKEC"},
    )
    fields.update(kw)
    return ShareholdingPattern.objects.create(**fields)


class ObjectPathTests(TestCase):
    def test_paths_are_symbol_date_and_record_id(self):
        filing = make_filing(Instrument.objects.create(name="RKEC"))
        raw, parsed = object_paths(filing)
        self.assertEqual(raw, f"{BASE}.xml")
        self.assertEqual(parsed, f"{BASE}.json")


class ExtractShareholdingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inst = Instrument.objects.create(isin="INE786W01010", name="RKEC")

    def _run(self, body=XML, error=None, **opts):
        session = mock.MagicMock()
        if error:
            session.get.side_effect = error
        else:
            session.get.return_value = mock.Mock(
                content=body, raise_for_status=lambda: None
            )
        opts.setdefault("delay", 0)
        opts.setdefault("local_dir", str(self.root))
        with mock.patch(f"{COMMAND}.requests.Session", return_value=session):
            call_command("extract_shareholding_pattern", **opts)
        return session

    def _json(self, rel):
        return json.loads((self.root / rel).read_text())

    def test_extracts_raw_and_parsed_objects_and_marks_done(self):
        filing = make_filing(self.inst)
        self._run()

        filing.refresh_from_db()
        self.assertEqual(filing.extraction_status, "done")
        self.assertEqual(filing.extraction_attempts, 1)
        self.assertIsNotNone(filing.extracted_at)
        self.assertEqual(filing.raw_object_path, f"{BASE}.xml")
        self.assertEqual(filing.parsed_object_path, f"{BASE}.json")
        self.assertTrue((self.root / f"{BASE}.xml").exists())
        parsed = self._json(f"{BASE}.json")
        self.assertEqual(parsed["company"]["symbol"], "RKEC")
        self.assertEqual(parsed["company"]["isin"], "INE786W01010")
        self.assertEqual(parsed["source"]["record_id"], 216349)
        self.assertEqual(parsed["source"]["quarter_end"], "2026-09-30")
        self.assertEqual(parsed["source"]["report_end"], "2026-09-30")
        self.assertIn("shareholding", parsed)
        self.assertEqual(
            filing.parsed_size_bytes, (self.root / f"{BASE}.json").stat().st_size
        )

    def test_done_filing_is_not_picked_up_again(self):
        make_filing(self.inst)
        self._run()
        session = self._run()
        session.get.assert_not_called()

    def test_download_failure_marks_failed_and_counts_attempts(self):
        filing = make_filing(self.inst)
        with self.assertRaises(CommandError):
            self._run(error=requests.ConnectionError("boom"))
        filing.refresh_from_db()
        self.assertEqual(filing.extraction_status, "failed")
        self.assertEqual(filing.extraction_attempts, 1)
        self.assertIn("boom", filing.extraction_error)

    def test_non_xml_download_fails(self):
        filing = make_filing(self.inst)
        with self.assertRaises(CommandError):
            self._run(body=b"Access Denied")
        filing.refresh_from_db()
        self.assertEqual(filing.extraction_status, "failed")
        self.assertIn("not XML", filing.extraction_error)

    def test_unparseable_xml_fails_without_uploading(self):
        filing = make_filing(self.inst)
        with self.assertRaises(CommandError):
            self._run(body=b"<xbrl>not a shareholding filing</xbrl>")
        filing.refresh_from_db()
        self.assertEqual(filing.extraction_status, "failed")
        self.assertFalse((self.root / f"{BASE}.json").exists())

    def test_record_id_forces_done_filing(self):
        make_filing(self.inst)
        self._run()
        session = self._run(record_id=216349)
        session.get.assert_called_once()

    def test_one_failure_does_not_stop_the_others(self):
        bad = make_filing(
            self.inst, record_id=1, xbrl_url="https://nsearchives.example/bad.xml"
        )
        good = make_filing(self.inst, record_id=2, symbol="RKEC")

        def get(url, timeout=None):
            if "bad" in url:
                raise requests.ConnectionError("boom")
            return mock.Mock(content=XML, raise_for_status=lambda: None)

        session = mock.MagicMock()
        session.get.side_effect = get
        with mock.patch(f"{COMMAND}.requests.Session", return_value=session):
            with self.assertRaises(CommandError):
                call_command(
                    "extract_shareholding_pattern",
                    local_dir=str(self.root),
                    delay=0,
                )
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.extraction_status, "failed")
        self.assertEqual(good.extraction_status, "done")

    def test_rebuild_reads_raw_from_storage_without_nse(self):
        filing = make_filing(self.inst)
        self._run()
        (self.root / f"{BASE}.json").unlink()
        ShareholdingPattern.objects.filter(pk=filing.pk).update(parsed_object_path="")

        session = self._run(rebuild=True)
        session.get.assert_not_called()
        filing.refresh_from_db()
        self.assertEqual(filing.extraction_status, "done")
        self.assertEqual(filing.parsed_object_path, f"{BASE}.json")
        self.assertTrue((self.root / f"{BASE}.json").exists())
