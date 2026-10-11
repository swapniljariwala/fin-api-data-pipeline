import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from indexpricehistory.management.commands.ingest_index_bhavcopy import Command
from indexpricehistory.models import Index, IndexPriceHistory
from dailypricehistory.models import BhavcopyFile

REAL_HEADER = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    "Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),"
    "P/E,P/B,Div Yield\n"
)
REAL_ROW = (
    "Nifty 50,09-01-2026,25840.4,25940.6,25623,25683.3,-193.55,-.75,"
    "348757262,27486.64,22.36,3.49,1.31\n"
)


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

    def test_index_price_history_valuation_fields(self):
        """Test the nullable valuation fields (P/E, P/B, Div Yield)."""
        price = IndexPriceHistory.objects.create(
            index=self.index,
            trade_date=date(2026, 9, 25),
            close=Decimal("19550.25"),
            pe_ratio=Decimal("22.3600"),
            pb_ratio=Decimal("3.4900"),
            div_yield=Decimal("1.3100"),
        )
        price.refresh_from_db()
        self.assertEqual(price.pe_ratio, Decimal("22.3600"))
        self.assertEqual(price.pb_ratio, Decimal("3.4900"))
        self.assertEqual(price.div_yield, Decimal("1.3100"))

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


class IndexBhavcopyParseTests(TestCase):
    """Regression tests for the real NIFTY indices CSV column names."""

    def test_parse_rows_maps_real_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind_close_all_09012026.csv"
            path.write_text(REAL_HEADER + REAL_ROW, encoding="utf-8")

            command = Command()
            rows, file_date = command._parse_rows(path, date(2026, 1, 9))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ticker"], "Nifty 50")
        self.assertEqual(row["trade_date"], date(2026, 1, 9))
        self.assertEqual(file_date, date(2026, 1, 9))
        self.assertEqual(row["open"], Decimal("25840.4"))
        self.assertEqual(row["high"], Decimal("25940.6"))
        self.assertEqual(row["low"], Decimal("25623"))
        self.assertEqual(row["close"], Decimal("25683.3"))
        self.assertEqual(row["volume"], 348757262)
        self.assertEqual(row["turnover"], Decimal("27486.64"))
        self.assertEqual(row["pe_ratio"], Decimal("22.36"))
        self.assertEqual(row["pb_ratio"], Decimal("3.49"))
        self.assertEqual(row["div_yield"], Decimal("1.31"))

    def test_parse_row_keeps_filename_date_when_index_date_unparseable(self):
        command = Command()
        raw = {
            "Index Name": "Nifty 50",
            "Index Date": "",
            "Open Index Value": "1",
            "High Index Value": "2",
            "Low Index Value": "3",
            "Closing Index Value": "4",
            "Volume": "5",
            "Turnover (Rs. Cr.)": "6",
        }
        row = command._parse_row(raw, date(2026, 1, 9))
        self.assertEqual(row["trade_date"], date(2026, 1, 9))

    def test_parse_row_missing_valuation_columns_is_none(self):
        command = Command()
        raw = {
            "Index Name": "Nifty 50",
            "Index Date": "09-01-2026",
            "Open Index Value": "1",
            "High Index Value": "2",
            "Low Index Value": "3",
            "Closing Index Value": "4",
            "Volume": "5",
            "Turnover (Rs. Cr.)": "6",
        }
        row = command._parse_row(raw, date(2026, 1, 9))
        self.assertIsNone(row["pe_ratio"])
        self.assertIsNone(row["pb_ratio"])
        self.assertIsNone(row["div_yield"])


class IndexValuationRefreshTests(TestCase):
    """Tests for the --refresh-valuations upsert path."""

    @staticmethod
    def _write(path, pe, pb, dy, close):
        row = (
            "Nifty 50,09-01-2026,25840.4,25940.6,25623,"
            f"{close},-193.55,-.75,348757262,27486.64,{pe},{pb},{dy}\n"
        )
        path.write_text(REAL_HEADER + row, encoding="utf-8")

    def test_refresh_updates_only_valuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind_close_all_09012026.csv"
            command = Command()
            command._index_cache = {}

            self._write(path, "22.36", "3.49", "1.31", "25683.3")
            command._ingest_file(path, date(2026, 1, 9))

            # Same file re-downloaded with changed valuations and a changed
            # close that must be left untouched.
            self._write(path, "25.00", "4.00", "1.50", "99999.0")
            command._refresh_valuations(path, date(2026, 1, 9))

        self.assertEqual(IndexPriceHistory.objects.count(), 1)
        self.assertEqual(BhavcopyFile.objects.count(), 1)
        row = IndexPriceHistory.objects.get()
        self.assertEqual(row.pe_ratio, Decimal("25.00"))
        self.assertEqual(row.pb_ratio, Decimal("4.00"))
        self.assertEqual(row.div_yield, Decimal("1.50"))
        self.assertEqual(row.close, Decimal("25683.3"))

