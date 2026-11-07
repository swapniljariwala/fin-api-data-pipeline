from unittest import mock

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from corporateannouncements.management.commands.ingest_corporate_announcements import (
    Command,
)
from corporateannouncements.models import AnnouncementCategory, CorporateAnnouncement
from securityinfo.models import Instrument


def _stat_tables():
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT tbl FROM sqlite_stat1")
        return {r[0] for r in cur.fetchall()}


class OptimizeStatsTests(TestCase):
    """The stats hook must touch only corporate_announcements, and only if stale."""

    def setUp(self):
        self.cat = AnnouncementCategory.objects.create(name="General")
        self.inst = Instrument.objects.create(name="X")

    def _add(self, n, start=0):
        now = timezone.now()
        CorporateAnnouncement.objects.bulk_create(
            CorporateAnnouncement(
                instrument=self.inst, category=self.cat, seq_id=start + i,
                announced_at=now,
            )
            for i in range(n)
        )

    def test_analyzes_only_this_table_when_stats_missing(self):
        self._add(20)
        Command._optimize()
        self.assertEqual(_stat_tables(), {"corporate_announcements"})

    def test_skips_when_stats_fresh(self):
        self._add(20)
        Command._optimize()
        with mock.patch.object(Command, "_optimize", wraps=Command._optimize):
            with connection.cursor() as cur:
                cur.execute("SELECT stat FROM sqlite_stat1 WHERE tbl=%s LIMIT 1",
                            ["corporate_announcements"])
                before = cur.fetchone()[0]
            self._add(1, start=100)  # +5%, under the 25% threshold
            Command._optimize()
            with connection.cursor() as cur:
                cur.execute("SELECT stat FROM sqlite_stat1 WHERE tbl=%s LIMIT 1",
                            ["corporate_announcements"])
                self.assertEqual(cur.fetchone()[0], before)

    def test_sparse_ids_do_not_trigger_reanalyze(self):
        """ignore_conflicts inserts burn autoincrement ids; MAX(id) != row count."""
        self._add(20)
        Command._optimize()
        CorporateAnnouncement.objects.filter(seq_id=0).update(id=1_000_000)
        with mock.patch(
            "corporateannouncements.management.commands."
            "ingest_corporate_announcements.logger"
        ) as log:
            Command._optimize()
        log.info.assert_not_called()

    def test_reanalyzes_after_growth(self):
        self._add(20)
        Command._optimize()
        self._add(20, start=100)  # doubled
        Command._optimize()
        with connection.cursor() as cur:
            cur.execute("SELECT stat FROM sqlite_stat1 WHERE tbl=%s LIMIT 1",
                        ["corporate_announcements"])
            self.assertTrue(cur.fetchone()[0].startswith("40"))

    def test_skipped_inside_snapshot_window(self):
        self._add(20)
        fake_now = timezone.now().astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
        ).replace(hour=5, minute=36)
        with mock.patch(
            "corporateannouncements.management.commands."
            "ingest_corporate_announcements.timezone.now",
            return_value=fake_now,
        ):
            Command._optimize()
        self.assertEqual(_stat_tables(), set())
