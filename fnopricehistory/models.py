from django.db import models

from dailypricehistory.models import BhavcopyFile


class FnoContract(models.Model):
    """Identity of one F&O contract: underlying + type + expiry (+ strike/option)."""

    underlying_symbol = models.CharField(max_length=50)
    instrument_type = models.CharField(max_length=10)  # UDiFF: FUTIDX/OPTIDX/FUTSTK/OPTSTK
    expiry_date = models.DateField()
    strike_price = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    option_type = models.CharField(max_length=2, null=True, blank=True)  # CE/PE
    fin_instrm_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "fno_contracts"
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "underlying_symbol",
                    "instrument_type",
                    "expiry_date",
                    "strike_price",
                    "option_type",
                ],
                name="uniq_fno_contract",
            ),
        ]
        indexes = [models.Index(fields=["expiry_date"], name="idx_fno_expiry")]

    def __str__(self):
        if self.option_type:
            return (
                f"{self.underlying_symbol} {self.expiry_date} "
                f"{self.strike_price}{self.option_type}"
            )
        return f"{self.underlying_symbol} {self.expiry_date} FUT"


class FnoPriceHistory(models.Model):
    """Daily OHLC + OI row for one F&O contract (futures and options unified)."""

    contract = models.ForeignKey(
        FnoContract, on_delete=models.CASCADE, related_name="price_history"
    )
    trade_date = models.DateField()
    open = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    high = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    low = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    close = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    settlement_price = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    volume = models.BigIntegerField(null=True, blank=True)
    turnover = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    num_trades = models.IntegerField(null=True, blank=True)
    open_interest = models.BigIntegerField(null=True, blank=True)
    change_in_oi = models.BigIntegerField(null=True, blank=True)
    underlying_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    file = models.ForeignKey(
        BhavcopyFile,
        on_delete=models.PROTECT,
        related_name="fno_price_rows",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "fno_price_history"
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "trade_date"],
                name="uniq_fno_price_contract_date",
            ),
        ]
        indexes = [models.Index(fields=["trade_date"], name="idx_fno_price_date")]

    def __str__(self):
        return f"#{self.pk} {self.trade_date}"
