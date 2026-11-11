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

    isin = models.CharField(max_length=12, unique=True, null=True)
    name = models.CharField(max_length=255, blank=True)
    instrument_type = models.CharField(max_length=20, default="STK")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "instruments"
        ordering = ["name"]

    def __str__(self):
        return self.isin or self.name or f"Instrument #{self.pk}"


class NSESymbols(models.Model):
    symbol = models.CharField(max_length=50)

    class Meta:
        db_table = "nse_symbols"
        constraints = [
            models.UniqueConstraint(fields=['symbol'], name='unique_symbol')
                ]
    def __str__(self):
        return self.symbol 

class NSESymbolInstrumentMap(models.Model):
    symbol = models.ForeignKey(NSESymbols, on_delete=models.CASCADE, related_name='instruments')
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name='symbols')

    class Meta:
        db_table = "nse_symbols_instrument_map"
        constraints = [
                models.UniqueConstraint(fields=['symbol', 'instrument'], name='unique_symbol_instrument')
                ]
    
    def __str__(self):
        return f"{self.symbol}-{self.instrument}"



class InstrumentClassification(models.Model):
    """NSE four-level industry classification of an instrument.

    Source: ``secInfo`` of the NSE live quote (``macro``, ``sector``,
    ``industryInfo``, ``basicIndustry``). A row exists once a lookup was
    *attempted* with a definitive answer: ``ok`` carries the labels,
    ``not_found`` means NSE has no classification for the symbol (bond, ETF,
    SME, delisted, ...) and is not retried unless asked to.
    """

    STATUS_OK = "ok"
    STATUS_NOT_FOUND = "not_found"
    STATUS_CHOICES = [(STATUS_OK, "ok"), (STATUS_NOT_FOUND, "not found")]

    instrument = models.OneToOneField(
        Instrument, on_delete=models.CASCADE, related_name="classification"
    )
    macro = models.CharField(max_length=100, null=True, blank=True)
    sector = models.CharField(max_length=100, null=True, blank=True)
    industry = models.CharField(max_length=100, null=True, blank=True)
    basic_industry = models.CharField(max_length=150, null=True, blank=True)
    symbol_used = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "instrument_classification"
        indexes = [
            models.Index(fields=["macro"], name="idx_classif_macro"),
            models.Index(fields=["sector"], name="idx_classif_sector"),
            models.Index(fields=["industry"], name="idx_classif_industry"),
            models.Index(fields=["basic_industry"], name="idx_classif_basic"),
        ]

    def __str__(self):
        return f"{self.instrument_id}: {self.basic_industry or self.status}"
