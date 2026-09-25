from django.test import TestCase
from datetime import date
from decimal import Decimal

from indexpricehistory.models import Index, IndexPriceHistory
from dailypricehistory.models import BhavcopyFile


class IndexModelTests(TestCase):
    """Tests for Index and IndexPriceHistory models."""

    def setUp(self):
        self.index = Index.objects.create(
            source="NSE",
            ticker="NIFTY 50",
            name="NIFTY 50 Index",
        )
        self.file = BhavcopyFile.objects.create(
            file_name="ind_close_all_25092026.csv",
            source="NSE",
            segment="IDX",
            format="udiff",
            trade_date=date(2026, 9, 25),
            row_count=1,
        )

    def test_index_creation(self):
        """Test creating an index."""
        self.assertEqual(self.index.source, "NSE")
        self.assertEqual(self.index.ticker, "NIFTY 50")

    def test_index_unique_constraint(self):
        """Test that (source, ticker) is unique."""
        with self.assertRaises(Exception):
            Index.objects.create(
                source="NSE",
                ticker="NIFTY 50",
                name="Duplicate",
            )

    def test_index_price_history_creation(self):
        """Test creating an index price history row."""
        price = IndexPriceHistory.objects.create(
            index=self.index,
            trade_date=date(2026, 9, 25),
            open=Decimal("19500.50"),
            high=Decimal("19600.00"),
            low=Decimal("19450.00"),
            close=Decimal("19550.25"),
            volume=1000000,
            turnover=Decimal("100000000.00"),
            file=self.file,
        )
        self.assertEqual(price.index, self.index)
        self.assertEqual(price.close, Decimal("19550.25"))

    def test_index_price_history_unique_constraint(self):
        """Test that (index, trade_date) is unique."""
        IndexPriceHistory.objects.create(
            index=self.index,
            trade_date=date(2026, 9, 25),
            close=Decimal("19550.25"),
            file=self.file,
        )
        with self.assertRaises(Exception):
            IndexPriceHistory.objects.create(
                index=self.index,
                trade_date=date(2026, 9, 25),
                close=Decimal("19555.00"),
            )
