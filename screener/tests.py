"""Tests for the screener snapshot: pure metrics and the DB builder."""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from corporateactions.models import (
    CorporateAction,
    CorporateActionLeg,
    PriceAdjustmentSegment,
)
from dailypricehistory.models import NSECmDelivery, NSECmPriceHistory
from securityinfo.models import Instrument, NSESymbolInstrumentMap, NSESymbols, Series

from . import metrics
from .models import ScreenerSnapshot

END = date(2026, 10, 22)  # a Thursday


def _weekdays(n, end=END):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _bar(day, close, high=None, low=None, volume=1000.0, deliv=None):
    return metrics.Bar(day, close, high or close, low or close, close, close, volume,
                       close * volume, 10, deliv)


class MetricsTests(SimpleTestCase):
    def test_empty(self):
        self.assertIsNone(metrics.compute([]))

    def test_ranges_returns_and_short_windows(self):
        days = _weekdays(300)
        bars = [_bar(d, 100.0 + i, high=110.0 + i, low=90.0 + i) for i, d in enumerate(days)]
        out = metrics.compute(bars)
        last = bars[-1]
        self.assertEqual(out["close"], last.close)
        self.assertEqual(out["trading_days_available"], 300)
        # 52w range covers only bars within 365 days of the last one
        in_year = [b for b in bars if b.trade_date > END - timedelta(days=365)]
        self.assertEqual(out["high_52w"], max(b.high for b in in_year))
        self.assertEqual(out["low_52w"], min(b.low for b in in_year))
        self.assertAlmostEqual(out["ret_1d"], 100 * (last.close / bars[-2].close - 1), 2)
        self.assertIsNotNone(out["ret_6m"])
        self.assertIsNone(out["ret_3y"])  # history shorter than the window
        self.assertIsNone(out["ret_5y"])
        self.assertIsNotNone(out["sma_200"])
        self.assertEqual(out["avg_vol_20d"], 1000.0)
        self.assertIsNone(out["avg_vol_250d"] if len(bars) < 250 else None)

    def test_short_history_gives_null_windows(self):
        out = metrics.compute([_bar(d, 50.0, volume=10.0) for d in _weekdays(15)])
        self.assertIsNone(out["sma_20"])
        self.assertIsNone(out["avg_vol_20d"])
        self.assertIsNone(out["ret_1y"])
        self.assertIsNotNone(out["avg_vol_10d"])
        self.assertIsNotNone(out["ret_1w"])
        self.assertEqual(out["high_52w"], 50.0)  # available data, new listing

    def test_return_gap_tolerance(self):
        # only an old bar and the latest: the 1m target is far from any bar
        bars = [_bar(END - timedelta(days=100), 80.0), _bar(END, 100.0)]
        out = metrics.compute(bars)
        self.assertIsNone(out["ret_1m"])
        self.assertIsNone(out["ret_1w"])

    def test_delivery_average_skips_nulls(self):
        bars = [_bar(d, 10.0, deliv=v) for d, v in zip(_weekdays(3), [40.0, None, 60.0])]
        out = metrics.compute(bars)
        self.assertEqual(out["deliv_per"], 60.0)
        self.assertEqual(out["avg_deliv_per_20d"], 50.0)

    def test_subtract_months_clamps_day(self):
        self.assertEqual(metrics.subtract_months(date(2026, 3, 31), 1), date(2026, 2, 28))
        self.assertEqual(metrics.subtract_months(date(2026, 1, 15), 12), date(2025, 1, 15))


class BuilderTests(TestCase):
    def _instrument(self, symbol, isin):
        inst = Instrument.objects.create(isin=isin, name=symbol, instrument_type="STK")
        sym, _ = NSESymbols.objects.get_or_create(symbol=symbol)
        NSESymbolInstrumentMap.objects.create(symbol=sym, instrument=inst)
        return inst

    def _prices(self, inst, closes, series="EQ", end=END, volume=1000):
        s, _ = Series.objects.get_or_create(code=series, defaults={"description": series})
        rows = []
        for d, c in zip(_weekdays(len(closes), end), closes):
            rows.append(NSECmPriceHistory.objects.create(
                instrument=inst, trade_date=d, series=s, open=c, high=c * 1.1, low=c * 0.9,
                close=c, prev_close=c, volume=volume, turnover=c * volume, num_trades=5))
        return rows

    def test_split_inside_window_is_adjusted(self):
        inst = self._instrument("SPLIT", "INE000000001")
        # 2:1 split: raw price halves from day 100 on; volume doubles
        closes = [200.0] * 100 + [100.0] * 100
        rows = self._prices(inst, closes)
        for r in rows[:100]:
            NSECmPriceHistory.objects.filter(pk=r.pk).update(volume=1000)
        for r in rows[100:]:
            NSECmPriceHistory.objects.filter(pk=r.pk).update(volume=2000)
        PriceAdjustmentSegment.objects.create(
            instrument=inst, valid_from=date(1900, 1, 1), valid_to=rows[100].trade_date,
            factor=Decimal("0.5"))
        call_command("build_screener_snapshot", stdout=StringIO())
        snap = ScreenerSnapshot.objects.get(pk=inst.pk)
        self.assertEqual(snap.close, 100.0)
        self.assertAlmostEqual(snap.high_52w, 110.0)  # not 220 from unadjusted history
        self.assertAlmostEqual(snap.ret_1m, 0.0, places=2)  # no fake crash
        self.assertEqual(snap.avg_vol_20d, 2000.0)
        self.assertFalse(snap.adjustment_pending)

    def test_series_fallback_and_eq_preference(self):
        be_only = self._instrument("BEONLY", "INE000000002")
        self._prices(be_only, [10.0] * 5, series="BE")
        both = self._instrument("BOTH", "INE000000003")
        self._prices(both, [20.0] * 5, series="EQ")
        self._prices(both, [99.0] * 5, series="BE")
        call_command("build_screener_snapshot", stdout=StringIO())
        self.assertEqual(ScreenerSnapshot.objects.get(pk=be_only.pk).close, 10.0)
        self.assertEqual(ScreenerSnapshot.objects.get(pk=both.pk).close, 20.0)

    def test_series_change_keeps_continuous_history(self):
        inst = self._instrument("MOVED", "INE000000010")
        self._prices(inst, [30.0] * 10, series="EQ", end=END - timedelta(days=20))
        self._prices(inst, [40.0] * 5, series="BE", end=END)
        call_command("build_screener_snapshot", stdout=StringIO())
        snap = ScreenerSnapshot.objects.get(pk=inst.pk)
        self.assertEqual(snap.as_of_date, END)
        self.assertEqual(snap.close, 40.0)
        self.assertEqual(snap.trading_days_available, 15)

    def test_new_listing_and_delisted(self):
        new = self._instrument("NEWCO", "INE000000004")
        self._prices(new, [50.0] * 10)
        dead = self._instrument("DEADCO", "INE000000005")
        self._prices(dead, [5.0] * 10, end=END - timedelta(days=90))
        call_command("build_screener_snapshot", stdout=StringIO())
        snap = ScreenerSnapshot.objects.get(pk=new.pk)
        self.assertEqual(snap.trading_days_available, 10)
        self.assertIsNone(snap.ret_1y)
        self.assertIsNone(snap.sma_20)
        self.assertFalse(ScreenerSnapshot.objects.filter(pk=dead.pk).exists())

    def test_needs_review_split_sets_flag(self):
        flagged = self._instrument("FLAG", "INE000000006")
        self._prices(flagged, [10.0] * 30)
        clean = self._instrument("CLEAN", "INE000000007")
        self._prices(clean, [10.0] * 30)
        action = CorporateAction.objects.create(
            instrument=flagged, symbol="FLAG", subject="Split", ex_date=END - timedelta(days=20),
            status=CorporateAction.STATUS_NEEDS_REVIEW)
        CorporateActionLeg.objects.create(action=action, ordinal=0, kind="SPLIT")
        # a needs_review dividend does not rescale prices, so it must not flag
        div = CorporateAction.objects.create(
            instrument=clean, symbol="CLEAN", subject="Div", ex_date=END - timedelta(days=20),
            status=CorporateAction.STATUS_NEEDS_REVIEW)
        CorporateActionLeg.objects.create(action=div, ordinal=0, kind="DIVIDEND")
        call_command("build_screener_snapshot", stdout=StringIO())
        self.assertTrue(ScreenerSnapshot.objects.get(pk=flagged.pk).adjustment_pending)
        self.assertFalse(ScreenerSnapshot.objects.get(pk=clean.pk).adjustment_pending)

    def test_delivery_and_idempotent_second_run(self):
        inst = self._instrument("DELIV", "INE000000008")
        rows = self._prices(inst, [10.0] * 3)
        NSECmDelivery.objects.create(price=rows[-1], trade_date=rows[-1].trade_date,
                                     deliv_qty=500, deliv_per=Decimal("45.50"))
        call_command("build_screener_snapshot", stdout=StringIO())
        snap = ScreenerSnapshot.objects.get(pk=inst.pk)
        self.assertEqual(snap.deliv_per, 45.5)
        built_at = snap.built_at
        out = StringIO()
        call_command("build_screener_snapshot", stdout=out)
        self.assertIn("0 created, 0 updated, 0 deleted", out.getvalue())
        self.assertEqual(ScreenerSnapshot.objects.get(pk=inst.pk).built_at, built_at)

    def test_dry_run_writes_nothing(self):
        inst = self._instrument("DRY", "INE000000009")
        self._prices(inst, [10.0] * 3)
        out = StringIO()
        call_command("build_screener_snapshot", "--dry-run", stdout=out)
        self.assertIn("1 to create", out.getvalue())
        self.assertEqual(ScreenerSnapshot.objects.count(), 0)
