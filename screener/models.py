from django.db import models

from securityinfo.models import Instrument


class ScreenerSnapshot(models.Model):
    """Latest price/volume metrics for one instrument (one row each).

    Everything is computed on split/bonus-adjusted prices (volume divided by
    the same factor) from EQ rows, falling back to the instrument's latest
    series when it has no EQ row. Only base metrics are stored; derived
    ratios (``% below 52w high``, ``price / 52w low``, CAGR) are expressions
    over these columns, built by the API. NULL in a window column means the
    history is shorter than the window.
    """

    instrument = models.OneToOneField(
        Instrument,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="screener_snapshot",
    )
    as_of_date = models.DateField()
    trading_days_available = models.IntegerField()
    built_at = models.DateTimeField()
    # An unapplied (needs_review) split/bonus sits in the history window, so
    # ranges and returns may be distorted.
    adjustment_pending = models.BooleanField(default=False)

    # Latest day
    close = models.FloatField()
    open = models.FloatField(null=True)
    day_high = models.FloatField(null=True)
    day_low = models.FloatField(null=True)
    prev_close = models.FloatField(null=True)
    volume = models.BigIntegerField(null=True)
    turnover = models.FloatField(null=True)
    num_trades = models.IntegerField(null=True)

    # 52-week range (intraday high / low, adjusted)
    high_52w = models.FloatField(null=True)
    low_52w = models.FloatField(null=True)

    # Absolute price returns in percent (adjusted, dividends excluded)
    ret_1d = models.FloatField(null=True)
    ret_1w = models.FloatField(null=True)
    ret_1m = models.FloatField(null=True)
    ret_3m = models.FloatField(null=True)
    ret_6m = models.FloatField(null=True)
    ret_1y = models.FloatField(null=True)
    ret_3y = models.FloatField(null=True)
    ret_5y = models.FloatField(null=True)

    # Volume
    avg_vol_10d = models.FloatField(null=True)
    avg_vol_20d = models.FloatField(null=True)
    avg_vol_50d = models.FloatField(null=True)
    avg_vol_250d = models.FloatField(null=True)
    avg_turnover_20d = models.FloatField(null=True)

    # Simple moving averages of close
    sma_20 = models.FloatField(null=True)
    sma_50 = models.FloatField(null=True)
    sma_100 = models.FloatField(null=True)
    sma_200 = models.FloatField(null=True)

    # Delivery (only the last 365 days exist)
    deliv_per = models.FloatField(null=True)
    avg_deliv_per_20d = models.FloatField(null=True)

    class Meta:
        db_table = "screener_snapshot"

    def __str__(self):
        return f"{self.instrument_id} {self.as_of_date} {self.close}"
