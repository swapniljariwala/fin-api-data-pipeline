from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from corporateactions.models import CorporateAction
from corporateactions.parsing import normalize_subject, parse_subject
from dailypricehistory.models import NSECmPriceHistory
from securityinfo.models import (
    Instrument, NSESymbolInstrumentMap, NSESymbols, Series,
)


def _kinds(subject):
    legs, problems = parse_subject(subject)
    return [leg["kind"] for leg in legs], problems


class ParseSubjectTests(SimpleTestCase):
    def test_dividend_variants(self):
        legs, problems = parse_subject("Dividend - Rs 2.50 Per Share")
        self.assertEqual(problems, [])
        self.assertEqual(legs[0]["kind"], "DIVIDEND")
        self.assertEqual(legs[0]["amount"], Decimal("2.50"))
        self.assertEqual(legs[0]["dividend_type"], "regular")

        legs, _ = parse_subject("Interim Dividend - Re 1 Per Sh")
        self.assertEqual(legs[0]["dividend_type"], "interim")
        self.assertEqual(legs[0]["amount"], Decimal("1"))

        legs, _ = parse_subject("Special Interim Dividend - Rs 4 Per Share")
        self.assertEqual(legs[0]["dividend_type"], "special_interim")

        legs, problems = parse_subject("Dividend - RsN Per Share".replace("N", "5"))
        self.assertEqual((legs[0]["amount"], problems), (Decimal("5"), []))

        legs, problems = parse_subject("Interim Dividend - Rs Per 0.50 Share")
        self.assertEqual((legs[0]["amount"], problems), (Decimal("0.50"), []))

    def test_currency_marker_optional_only_before_per(self):
        legs, problems = parse_subject(
            "Dividend - Rs 4 Per Share/Special Dividend - 8 Per Share"
        )
        self.assertEqual(problems, [])
        self.assertEqual([l["amount"] for l in legs], [Decimal("4"), Decimal("8")])
        legs, problems = parse_subject(
            "Dividend - Rs 35 Per Share & Special Dividend Of Rs 100 Per Share"
            "/ Special Dividend Of Rs 30"
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [l["amount"] for l in legs], [Decimal("35"), Decimal("100"), Decimal("30")]
        )

    def test_combined_agm_and_dividends(self):
        legs, problems = parse_subject(
            "Annual General Meeting/Dividend - Rs 2 Per Share"
            "/Special Dividend - Rs 5 Per Share"
        )
        self.assertEqual(problems, [])
        self.assertEqual(
            [(l["kind"], l["dividend_type"], l["amount"]) for l in legs],
            [
                ("MEETING", "", None),
                ("DIVIDEND", "regular", Decimal("2")),
                ("DIVIDEND", "special", Decimal("5")),
            ],
        )

    def test_ampersand_and_missing_slash(self):
        legs, problems = parse_subject(
            "Dividend - Rs 3 Per Share & Special Dividend Rs 1 Per Share"
        )
        self.assertEqual(problems, [])
        self.assertEqual([l["amount"] for l in legs], [Decimal("3"), Decimal("1")])
        kinds, problems = _kinds("Annual General Meetingdividend - Rs 5 Per Share")
        self.assertEqual((kinds, problems), (["MEETING", "DIVIDEND"], []))

    def test_dividend_without_amount_is_a_problem(self):
        for subject in (
            "Interim Dividend",
            "Annual General Meeting/Dividend - Rs Per Share",
            "Dividend - 5%",
        ):
            with self.subTest(subject):
                self.assertTrue(parse_subject(subject)[1])

    def test_bonus_ratio_and_factor(self):
        legs, problems = parse_subject("Bonus 5:1")
        self.assertEqual(problems, [])
        self.assertEqual((legs[0]["ratio_new"], legs[0]["ratio_held"]), (5, 1))
        self.assertEqual(legs[0]["price_factor"], Decimal("0.16666667"))
        legs, _ = parse_subject("Bonus 1:1")
        self.assertEqual(legs[0]["price_factor"], Decimal("0.5"))

    def test_ncrps_bonus_does_not_rescale_equity_price(self):
        legs, problems = parse_subject("Scheme Of Arrangement - Bonus Ncrps 4:1")
        self.assertEqual(problems, [])
        self.assertEqual(legs[0]["kind"], "BONUS")
        self.assertIsNone(legs[0]["price_factor"])
        self.assertEqual(legs[0]["extra"], {"instrument": "NCRPS"})

    def test_split_face_values(self):
        legs, problems = parse_subject(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share "
            "To Re 1/- Per Share"
        )
        self.assertEqual(problems, [])
        leg = legs[0]
        self.assertEqual(leg["kind"], "SPLIT")
        self.assertEqual((leg["old_face_value"], leg["new_face_value"]),
                         (Decimal("10"), Decimal("1")))
        self.assertEqual(leg["price_factor"], Decimal("0.1"))

    def test_consolidation_factor_is_above_one(self):
        legs, problems = parse_subject(
            "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share"
        )
        self.assertEqual(problems, [])
        self.assertEqual(legs[0]["kind"], "CONSOLIDATION")
        self.assertEqual(legs[0]["price_factor"], Decimal("10"))

    def test_rights(self):
        legs, problems = parse_subject("Rights 1:15 @ Premium Rs 1247/-")
        self.assertEqual(problems, [])
        self.assertEqual(
            (legs[0]["ratio_new"], legs[0]["ratio_held"], legs[0]["premium"]),
            (1, 15, Decimal("1247")),
        )
        self.assertIsNone(legs[0]["price_factor"])

    def test_rights_premium_without_currency(self):
        legs, problems = parse_subject("Rights 1:9 @ Premium 91")
        self.assertEqual((legs[0]["premium"], problems), (Decimal("91"), []))

    def test_rights_without_premium_or_ratio_needs_review(self):
        self.assertTrue(parse_subject("Rights 1:1")[1])
        self.assertTrue(parse_subject("Rights - 7 Ccps And 7 Warrants:40")[1])

    def test_type_only_actions(self):
        for subject, kind in (
            ("Buy Back", "BUYBACK"),
            ("Buyback", "BUYBACK"),
            ("Demerger", "DEMERGER"),
            ("Capital Reduction", "CAPITAL_REDUCTION"),
            ("Redemption", "REDEMPTION"),
            ("Interest Payment", "INTEREST"),
            ("Extra- Ordinary General Meeting", "MEETING"),
        ):
            with self.subTest(subject):
                self.assertEqual(_kinds(subject), ([kind], []))

    def test_distribution_total_and_components(self):
        legs, problems = parse_subject(
            "Distribution - Rs 3 Per Unit Consists Of Interest- Rs 2 Per Unit"
            "/Dividend - Re 1 Per Unit"
        )
        self.assertEqual(problems, [])
        self.assertEqual(legs[0]["kind"], "DISTRIBUTION")
        self.assertEqual(legs[0]["amount"], Decimal("3"))
        self.assertEqual(
            [c["type"] for c in legs[0]["extra"]["components"]],
            ["interest", "dividend"],
        )

    def test_distribution_variants_without_per_unit_or_currency(self):
        legs, problems = parse_subject(
            "Distribution - Rs 1.556 Consists Of Rs 1.388 In The Form Of Dividend "
            "And Re 0.030 In The Form Of Interest"
        )
        self.assertEqual((legs[0]["kind"], legs[0]["amount"], problems),
                         ("DISTRIBUTION", Decimal("1.556"), []))
        legs, problems = parse_subject(
            "Distribution - 3.04316 Consisting Of Interest Rs 3.04013 Per Unit"
        )
        self.assertEqual((legs[0]["amount"], problems), (Decimal("3.04316"), []))

    def test_distribution_without_total_needs_review(self):
        _, problems = parse_subject("Interest Amount - Rs 2 Per Unit/Other - Re 1 Per Unit")
        self.assertTrue(problems)

    def test_unrecognised_subject_needs_review(self):
        self.assertEqual(parse_subject("Something Entirely New"), ([], ["unrecognised subject"]))

    def test_normalize_subject(self):
        self.assertEqual(normalize_subject("  Dividend -  Rs  1.10 \n Per Share"),
                         "Dividend - Rs 1.10 Per Share")


class PriceCheckAndIngestTests(TestCase):
    ISIN = "INE000A01010"

    @classmethod
    def setUpTestData(cls):
        cls.eq = Series.objects.create(code="EQ", description="Equity")
        cls.instrument = Instrument.objects.create(isin=cls.ISIN, name="Acme Ltd")

    def _prices(self, **closes):
        for day, close in closes.items():
            NSECmPriceHistory.objects.create(
                instrument=self.instrument,
                series=self.eq,
                trade_date=date.fromisoformat(day.replace("d", "").replace("_", "-")),
                close=Decimal(str(close)),
            )

    def _record(self, subject, ex="10-Mar-2025", **extra):
        record = {
            "symbol": "ACME", "isin": self.ISIN, "series": "EQ", "comp": "Acme Ltd",
            "faceVal": "10", "subject": subject, "exDate": ex, "recDate": ex,
            "bcStartDate": "-", "bcEndDate": "-", "ind": "-", "ndStartDate": "-",
            "ndEndDate": "-", "caBroadcastDate": None,
        }
        record.update(extra)
        return record

    def _ingest(self, records):
        with mock.patch(
            "jugaad_data.nse.live.NSELive.corporate_actions", return_value=records
        ):
            call_command(
                "ingest_corporate_actions",
                from_date="2025-03-01", to_date="2025-03-31",
                delay=0, verbosity=0,
            )

    def test_split_that_matches_the_price_move_passes(self):
        self._prices(d2025_03_07=100, d2025_03_10=51)
        self._ingest([self._record(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"
        )])
        action = CorporateAction.objects.get()
        self.assertEqual(action.price_check, "passed")
        self.assertEqual(action.status, "auto")
        self.assertEqual(action.expected_factor, Decimal("0.5"))
        self.assertEqual(action.observed_ratio, Decimal("0.51"))
        self.assertEqual(action.legs.get().kind, "SPLIT")

    def test_split_with_no_price_move_goes_to_review(self):
        self._prices(d2025_03_07=100, d2025_03_10=99)
        self._ingest([self._record(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
        )])
        action = CorporateAction.objects.get()
        self.assertEqual(action.price_check, "failed")
        self.assertEqual(action.status, "needs_review")
        self.assertIn("price check failed", action.review_reason)

    def test_split_and_bonus_on_the_same_day_are_checked_together(self):
        self._prices(d2025_03_07=1000, d2025_03_10=100)
        self._ingest([
            self._record("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share"),
            self._record("Bonus 4:1"),
        ])
        statuses = set(CorporateAction.objects.values_list("price_check", "status"))
        self.assertEqual(statuses, {("passed", "auto")})
        self.assertEqual(
            CorporateAction.objects.first().expected_factor, Decimal("0.1")
        )

    def test_prices_after_an_isin_change_are_found_via_the_symbol(self):
        # A split gives the shares a new ISIN: NSE's record carries the old one
        # while post-split prices sit under a different instrument.
        new_instrument = Instrument.objects.create(isin="INE000A01028", name="Acme Ltd")
        symbol = NSESymbols.objects.create(symbol="ACME")
        NSESymbolInstrumentMap.objects.create(symbol=symbol, instrument=self.instrument)
        NSESymbolInstrumentMap.objects.create(symbol=symbol, instrument=new_instrument)
        self._prices(d2025_03_07=100)
        NSECmPriceHistory.objects.create(
            instrument=new_instrument, series=self.eq,
            trade_date=date(2025, 3, 10), close=Decimal("20"))
        self._ingest([self._record(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
        )])
        action = CorporateAction.objects.get()
        self.assertEqual(action.price_check, "passed")
        self.assertEqual(action.observed_ratio, Decimal("0.2"))

    def test_no_prices_is_no_data_not_a_failure(self):
        self._ingest([self._record("Bonus 1:1")])
        action = CorporateAction.objects.get()
        self.assertEqual((action.price_check, action.status), ("no_data", "auto"))

    def test_no_data_is_rechecked_once_prices_arrive(self):
        self._ingest([self._record("Bonus 1:1", ex="10-Mar-2026")])
        self.assertEqual(CorporateAction.objects.get().price_check, "no_data")
        NSECmPriceHistory.objects.create(
            instrument=self.instrument, series=self.eq,
            trade_date=date(2026, 3, 9), close=Decimal("100"))
        NSECmPriceHistory.objects.create(
            instrument=self.instrument, series=self.eq,
            trade_date=date(2026, 3, 10), close=Decimal("51"))
        call_command("check_corporate_action_prices", verbosity=0)
        action = CorporateAction.objects.get()
        self.assertEqual(action.price_check, "passed")

    def test_small_factor_is_inconclusive(self):
        self._prices(d2025_03_07=100, d2025_03_10=100)
        self._ingest([self._record("Bonus 1:10")])
        self.assertEqual(CorporateAction.objects.get().price_check, "inconclusive")

    def test_dividend_has_no_price_check(self):
        self._ingest([self._record("Dividend - Rs 2 Per Share")])
        action = CorporateAction.objects.get()
        self.assertEqual((action.price_check, action.status), ("not_applicable", "auto"))
        leg = action.legs.get()
        self.assertEqual((leg.kind, leg.amount), ("DIVIDEND", Decimal("2")))

    def test_unparseable_subject_is_stored_for_review(self):
        self._ingest([self._record("Rights - 7 Ccps And 7 Warrants:40")])
        action = CorporateAction.objects.get()
        self.assertEqual(action.status, "needs_review")
        self.assertEqual(action.legs.count(), 0)
        self.assertTrue(action.review_reason)

    def test_rerun_is_idempotent_and_updates_amended_dates(self):
        record = self._record("Dividend - Rs 2 Per Share", recDate="-", bcStartDate="11-Mar-2025")
        self._ingest([record])
        self._ingest([record, dict(record)])  # same row twice in one response
        self.assertEqual(CorporateAction.objects.count(), 1)
        self.assertIsNone(CorporateAction.objects.get().record_date)

        self._ingest([self._record("Dividend - Rs 2 Per Share", recDate="12-Mar-2025")])
        self.assertEqual(CorporateAction.objects.count(), 1)
        self.assertEqual(CorporateAction.objects.get().record_date, date(2025, 3, 12))

    def test_verified_status_is_sticky_and_old_parser_versions_are_reparsed(self):
        self._prices(d2025_03_07=100, d2025_03_10=99)
        record = self._record(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share")
        self._ingest([record])
        CorporateAction.objects.update(status="verified", parser_version=0)
        self._ingest([record])
        action = CorporateAction.objects.get()
        self.assertEqual(action.status, "verified")
        self.assertEqual(action.price_check, "failed")  # still recorded
        self.assertEqual(action.legs.count(), 1)

    def test_failed_fetch_is_retried(self):
        record = self._record("Dividend - Rs 2 Per Share")
        with mock.patch(
            "jugaad_data.nse.live.NSELive.corporate_actions",
            side_effect=[RuntimeError("boom"), [record]],
        ):
            call_command(
                "ingest_corporate_actions",
                from_date="2025-03-01", to_date="2025-03-31",
                delay=0, retries=2, verbosity=0,
            )
        self.assertEqual(CorporateAction.objects.count(), 1)

    def test_run_fails_after_all_retries_but_other_windows_still_ingest(self):
        record = self._record("Dividend - Rs 2 Per Share")
        with mock.patch(
            "jugaad_data.nse.live.NSELive.corporate_actions",
            side_effect=[RuntimeError("a"), RuntimeError("b"), [record]],
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "ingest_corporate_actions",
                    from_date="2025-01-01", to_date="2025-06-30",
                    chunk_days=90, delay=0, retries=2, verbosity=0,
                )
        self.assertEqual(CorporateAction.objects.count(), 1)

    def test_empty_no_data_response_is_not_an_error(self):
        with mock.patch(
            "jugaad_data.nse.live.NSELive.corporate_actions",
            return_value={"data": [], "msg": "no data found"},
        ):
            call_command("ingest_corporate_actions", days=3, delay=0, verbosity=0)
        self.assertEqual(CorporateAction.objects.count(), 0)
