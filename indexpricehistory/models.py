from django.db import models
from dailypricehistory.models import BhavcopyFile


class Index(models.Model):
    """Global index identity."""

    source = models.CharField(max_length=10)  # 'NSE'
    ticker = models.CharField(max_length=100)  # 'NIFTY 50', 'SENSEX', etc.
    name = models.CharField(max_length=255, null=True, blank=True)  # Full name if available

    class Meta:
        db_table = "indices"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "ticker"],
                name="uniq_index_source_ticker",
            ),
        ]

    def __str__(self):
        return f"{self.source}:{self.ticker}"


class IndexPriceHistory(models.Model):
    """Daily OHLC data for indices."""

    index = models.ForeignKey(
        Index,
        on_delete=models.CASCADE,
        related_name="price_history",
    )
    trade_date = models.DateField()
    open = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    high = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    low = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    close = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    turnover = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    file = models.ForeignKey(
        BhavcopyFile,
        on_delete=models.PROTECT,
        related_name="index_price_rows",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "index_price_history"
        constraints = [
            models.UniqueConstraint(
                fields=["index", "trade_date"],
                name="uniq_price_index_date",
            ),
        ]
        indexes = [
            models.Index(fields=["trade_date"], name="idx_index_price_date"),
            models.Index(fields=["index"], name="idx_index_price_index"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.trade_date}"
