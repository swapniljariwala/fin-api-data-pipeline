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
        constraints = [
                models.UniqueConstraint(fields=['symbol', 'instrument'], name='unique_symbol_instrument')
                ]
    
    def __str__(self):
        return f"{self.symbol}-{self.instrument}"

