"""Pure metric computation for the screener snapshot (no database access).

Input is one instrument's daily rows, oldest first, already on the adjusted
basis (prices multiplied by the split/bonus factor, volume divided by it).
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

# A return needs a close within this many days before its calendar target;
# otherwise (data gaps, or history starting later) the return is NULL.
RETURN_TOLERANCE_DAYS = 7

RETURN_LOOKBACKS = {
    "ret_1w": ("days", 7),
    "ret_1m": ("months", 1),
    "ret_3m": ("months", 3),
    "ret_6m": ("months", 6),
    "ret_1y": ("months", 12),
    "ret_3y": ("months", 36),
    "ret_5y": ("months", 60),
}
VOL_WINDOWS = {"avg_vol_10d": 10, "avg_vol_20d": 20, "avg_vol_50d": 50, "avg_vol_250d": 250}
SMA_WINDOWS = {"sma_20": 20, "sma_50": 50, "sma_100": 100, "sma_200": 200}
MAX_LOOKBACK_DAYS = 60 * 31 + 15  # enough calendar days for ret_5y


@dataclass
class Bar:
    trade_date: date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: float
    prev_close: Optional[float]
    volume: Optional[float]
    turnover: Optional[float]
    num_trades: Optional[int]
    deliv_per: Optional[float] = None


def subtract_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _round(value, digits=4):
    return None if value is None else round(value, digits)


def _mean(values):
    return sum(values) / len(values) if values else None


def _window_mean(bars, n, attr):
    """Mean of ``attr`` over the last ``n`` bars; None if fewer bars or any value missing."""
    if len(bars) < n:
        return None
    values = [getattr(b, attr) for b in bars[-n:]]
    if any(v is None for v in values):
        return None
    return _mean(values)


def compute(bars):
    """Metrics dict for the instrument's latest bar, or None when there are no bars."""
    if not bars:
        return None
    last = bars[-1]
    as_of = last.trade_date
    dates = [b.trade_date for b in bars]

    out = {
        "as_of_date": as_of,
        "trading_days_available": len(bars),
        "close": _round(last.close),
        "open": _round(last.open),
        "day_high": _round(last.high),
        "day_low": _round(last.low),
        "prev_close": _round(last.prev_close),
        "volume": None if last.volume is None else int(round(last.volume)),
        "turnover": _round(last.turnover, 2),
        "num_trades": last.num_trades,
    }

    # 52-week range over the available part of the last 365 days.
    in_year = [b for b in bars if b.trade_date > as_of - timedelta(days=365)]
    highs = [b.high if b.high is not None else b.close for b in in_year]
    lows = [b.low if b.low is not None else b.close for b in in_year]
    out["high_52w"] = _round(max(highs))
    out["low_52w"] = _round(min(lows))

    # Returns
    out["ret_1d"] = (
        _round(100 * (last.close / bars[-2].close - 1), 2) if len(bars) > 1 else None
    )
    for name, (unit, amount) in RETURN_LOOKBACKS.items():
        target = (
            as_of - timedelta(days=amount)
            if unit == "days"
            else subtract_months(as_of, amount)
        )
        idx = bisect_right(dates, target) - 1
        if idx < 0 or (target - dates[idx]).days > RETURN_TOLERANCE_DAYS:
            out[name] = None
        else:
            out[name] = _round(100 * (last.close / bars[idx].close - 1), 2)

    # Volume / turnover averages and SMAs
    for name, n in VOL_WINDOWS.items():
        out[name] = _round(_window_mean(bars, n, "volume"), 2)
    out["avg_turnover_20d"] = _round(_window_mean(bars, 20, "turnover"), 2)
    for name, n in SMA_WINDOWS.items():
        out[name] = _round(_window_mean(bars, n, "close"))

    # Delivery: average over the non-null values in the last 20 bars
    out["deliv_per"] = _round(last.deliv_per, 2)
    recent = [b.deliv_per for b in bars[-20:] if b.deliv_per is not None]
    out["avg_deliv_per_20d"] = _round(_mean(recent), 2)
    return out
