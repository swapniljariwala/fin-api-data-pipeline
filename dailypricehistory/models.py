from django.db import models
from securityinfo.models import Instrument,  Series


class BhavcopyFile(models.Model):
    """Ingest provenance: one row per downloaded/imported bhavcopy file."""

    FORMAT_LEGACY = "legacy"
    FORMAT_UDIFF = "udiff"
    FORMAT_FULL = "full"
    FORMAT_CHOICES = [
        (FORMAT_LEGACY, "legacy"),
        (FORMAT_UDIFF, "udiff"),
        (FORMAT_FULL, "full"),
    ]

    file_name = models.CharField(max_length=255, unique=True)
    source = models.CharField(max_length=10)
    segment = models.CharField(max_length=10, default="CM")
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
    trade_date = models.DateField()
    row_count = models.IntegerField(null=True, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bhavcopy_files"
        ordering = ["-trade_date"]

    def __str__(self):
        return self.file_name


class NSECmPriceHistory(models.Model):
    """Daily OHLC row for an STK instrument on one exchange and series."""

    instrument = models.ForeignKey(
        Instrument,
        on_delete=models.CASCADE,
        related_name="price_history",
    )
    trade_date = models.DateField() 
    series = models.ForeignKey(
        Series,
        on_delete=models.PROTECT,
        related_name="price_history",
    )
    open = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    high = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    low = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    close = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    last_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    prev_close = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    turnover = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    num_trades = models.IntegerField(null=True, blank=True)
    settlement_price = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    file = models.ForeignKey(
        BhavcopyFile,
        on_delete=models.PROTECT,
        related_name="price_rows",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "nse_cm_price_history"
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "trade_date", "series"],
                name="uniq_price_instrument_date_series",
            ),
        ]
        indexes = [
            models.Index(fields=["trade_date"], name="idx_cm_price_date"),
            models.Index(fields=["instrument"], name="idx_cm_instrument"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.trade_date}"


class NSECmDelivery(models.Model):
    """Delivery data from the NSE full bhavcopy for one price row.

    The primary key is the price row's id, so joining to
    ``nse_cm_price_history`` is a primary-key lookup. Only the last year is
    kept (see ``purge_old_delivery``); price rows are never touched by it.
    ``trade_date`` is denormalised for the purge and date scans.
    """

    price = models.OneToOneField(
        NSECmPriceHistory,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="delivery",
    )
    trade_date = models.DateField()
    deliv_qty = models.BigIntegerField(null=True, blank=True)
    deliv_per = models.DecimalField(
        max_digits=7, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = "nse_cm_delivery"
        indexes = [
            models.Index(fields=["trade_date"], name="idx_cm_delivery_date"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.trade_date}"

