from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from mfnavhistory.models import (
    MutualFundAmc,
    MutualFundCategory,
    MutualFundNavHistory,
    MutualFundScheme,
)

PATCH_TARGET = "jugaad_data.amfi.AMFI.nav_history_raw"


def _record(
    scheme_code="139619",
    scheme_name="Test Fund - Growth",
    plan="",
    option="Growth",
    isin_growth="INF000000001",
    isin_reinvest="",
    nav="10.0000",
    date="29-Sep-2026",
    scheme_type="Open Ended",
    category="Equity Scheme - Multi Cap Fund",
    amc="Test Mutual Fund",
):
    return {
        "scheme_code": scheme_code,
        "scheme_name": scheme_name,
        "plan": plan,
        "option": option,
        "isin_growth": isin_growth,
        "isin_reinvest": isin_reinvest,
        "nav": nav,
        "date": date,
        "scheme_type": scheme_type,
        "category": category,
        "amc": amc,
    }


def _run(*args, **kwargs):
    kwargs.setdefault("verbosity", 0)
    call_command("ingest_mf_nav", *args, **kwargs)


class IngestMfNavParsingTests(TestCase):
    @mock.patch(PATCH_TARGET)
    def test_parses_and_resolves_dimensions(self, fetch):
        fetch.return_value = [
            _record(),
            _record(scheme_code="139620", scheme_name="Another", nav="20.5"),
        ]

        _run(from_date="2026-09-29", to_date="2026-09-30")

        self.assertEqual(MutualFundNavHistory.objects.count(), 2)
        self.assertEqual(MutualFundScheme.objects.count(), 2)
        self.assertEqual(MutualFundAmc.objects.get().name, "Test Mutual Fund")
        self.assertEqual(
            MutualFundCategory.objects.get().name, "Equity Scheme - Multi Cap Fund"
        )

        scheme = MutualFundScheme.objects.get(scheme_code=139619)
        self.assertEqual(scheme.scheme_name, "Test Fund - Growth")
        self.assertEqual(scheme.option, "Growth")
        self.assertEqual(scheme.isin_growth, "INF000000001")
        self.assertEqual(scheme.scheme_type, "Open Ended")

        nav = MutualFundNavHistory.objects.get(scheme=scheme)
        self.assertEqual(nav.nav, Decimal("10.0000"))
        self.assertEqual(nav.nav_date, date(2026, 9, 29))

    @mock.patch(PATCH_TARGET)
    def test_blank_and_dash_nav_become_null(self, fetch):
        fetch.return_value = [
            _record(scheme_code="1", nav="-"),
            _record(scheme_code="2", nav=""),
            _record(scheme_code="3", nav="NA"),
            _record(scheme_code="4", nav="N.A."),
            _record(scheme_code="5", nav="n/a"),
        ]

        _run(from_date="2026-09-29", to_date="2026-09-30")

        self.assertEqual(MutualFundNavHistory.objects.count(), 5)
        self.assertEqual(
            MutualFundNavHistory.objects.filter(nav__isnull=True).count(), 5
        )

    @mock.patch(PATCH_TARGET)
    def test_unparseable_rows_are_skipped(self, fetch):
        fetch.return_value = [
            _record(),  # good
            _record(scheme_code=""),  # missing scheme_code
            _record(scheme_code="abc"),  # non-numeric
            _record(date="not-a-date"),  # bad date
            _record(nav="not-a-number"),  # bad nav
        ]

        _run(from_date="2026-09-29", to_date="2026-09-30")

        self.assertEqual(MutualFundNavHistory.objects.count(), 1)
        self.assertEqual(MutualFundScheme.objects.count(), 1)

    @mock.patch(PATCH_TARGET)
    def test_rerun_is_a_noop(self, fetch):
        fetch.return_value = [_record()]

        _run(from_date="2026-09-29", to_date="2026-09-30")
        first_count = MutualFundNavHistory.objects.count()
        _run(from_date="2026-09-29", to_date="2026-09-30")

        self.assertEqual(MutualFundNavHistory.objects.count(), first_count)
        self.assertEqual(MutualFundScheme.objects.count(), 1)


class IngestMfNavLatestWinsTests(TestCase):
    @mock.patch(PATCH_TARGET)
    def test_scheme_attributes_update_latest_wins(self, fetch):
        def side_effect(from_date, to_date, mf=""):
            return [_record(scheme_name=f"Fund {from_date.isoformat()}")]

        fetch.side_effect = side_effect

        _run(from_date="2026-09-01", to_date="2026-09-03", chunk_days=1)

        scheme = MutualFundScheme.objects.get(scheme_code=139619)
        self.assertEqual(scheme.scheme_name, "Fund 2026-09-03")


class IngestMfNavChunkingTests(TestCase):
    @mock.patch(PATCH_TARGET)
    def test_partitioned_oldest_first(self, fetch):
        calls = []

        def side_effect(from_date, to_date, mf=""):
            calls.append((from_date, to_date, mf))
            return []

        fetch.side_effect = side_effect

        _run(from_date="2026-09-01", to_date="2026-09-07", chunk_days=3)

        self.assertEqual(
            calls,
            [
                (date(2026, 9, 1), date(2026, 9, 3), ""),
                (date(2026, 9, 4), date(2026, 9, 6), ""),
                (date(2026, 9, 7), date(2026, 9, 7), ""),
            ],
        )

    @mock.patch(PATCH_TARGET)
    def test_amc_is_passed_through(self, fetch):
        fetch.return_value = []

        _run(from_date="2026-09-01", to_date="2026-09-02", amc="128")

        fetch.assert_called_once_with(date(2026, 9, 1), date(2026, 9, 2), mf="128")
