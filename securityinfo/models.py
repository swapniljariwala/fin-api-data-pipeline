from django.db import models


class Series(models.Model):
    """Lookup dictionary of NSE series codes (EQ, BE, BL, ...)."""

    code = models.CharField(max_length=2, unique=True)
    description = models.CharField(max_length=200)

    class Meta:
        db_table = "series"
        verbose_name_plural = "series"
        ordering = ["code"]

    def __str__(self):
        return self.code


class Instrument(models.Model):
    """Global security identity, keyed by ISIN across NSE/BSE."""

    isin = models.CharField(max_length=12, unique=True, null=True, blank=True)
    name = models.CharField(max_length=255, blank=True)
    instrument_type = models.CharField(max_length=20, default="STK")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "instruments"
        ordering = ["name"]

    def __str__(self):
        return self.isin or self.name or f"Instrument #{self.pk}"


class InstrumentTicker(models.Model):
    """Per-exchange ticker and exchange-specific codes for an instrument."""

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="tickers"
    )
    source = models.CharField(max_length=10)
    ticker = models.CharField(max_length=50)
    fin_instrm_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "instrument_tickers"
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "source"], name="uniq_instrument_source"
            ),
            models.UniqueConstraint(
                fields=["source", "ticker"], name="uniq_source_ticker"
            ),
        ]
        ordering = ["ticker", "source"]

    def __str__(self):
        return f"{self.ticker} ({self.source})"
