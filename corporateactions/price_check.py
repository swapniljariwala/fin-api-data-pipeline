"""Sanity-check parsed split/bonus/consolidation factors against real prices.

For an instrument and ex-date, the close on the first trading day on or after
the ex-date divided by the close on the last trading day before it should be
roughly the product of the parsed price factors (a 1:2 split => ~0.5). The
check is intentionally coarse (``TOLERANCE``): daily moves of up to the 20%
circuit limit are normal, so it catches gross errors (inverted or
mis-parsed ratios, a split read as a bonus, a missing event) but cannot pin
an exact ratio. The parsed text stays the source of truth.

Results (``CorporateAction.price_check``):

* ``not_applicable`` - no price-adjusting leg on this instrument/ex-date.
* ``no_data`` - no close before and/or after the ex-date in our price history
  (future ex-date, newly listed, SME); re-checked on later runs.
* ``inconclusive`` - the expected factor is so close to 1 that "no
  adjustment happened" would pass too (e.g. a 1:5 bonus).
* ``passed`` / ``failed`` - observed move within / outside ``TOLERANCE``.

Actions are checked per (instrument, ex-date) because a split and a bonus
often share an ex-date and arrive as separate NSE records.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from dailypricehistory.models import NSECmPriceHistory

from securityinfo.models import NSESymbolInstrumentMap

from .models import CorporateAction, CorporateActionLeg
from .parsing import PRICE_ADJUSTING_KINDS

logger = logging.getLogger(__name__)

TOLERANCE = Decimal("0.25")
WINDOW_DAYS = 14
_PREFERRED_SERIES = "EQ"
_QUANT = Decimal("0.00000001")


def expected_factor(instrument_id, ex_date):
    """Product of price factors of all legs on this instrument/ex-date, or None."""
    factors = CorporateActionLeg.objects.filter(
        action__instrument_id=instrument_id,
        action__ex_date=ex_date,
        kind__in=PRICE_ADJUSTING_KINDS,
        price_factor__isnull=False,
    ).values_list("price_factor", flat=True)
    result = None
    for factor in factors:
        result = factor if result is None else result * factor
    return result


def price_instrument_ids(instrument_id, symbols):
    """Instruments whose prices continue this one across an ISIN change.

    A face-value split or consolidation gives the shares a new ISIN: NSE's
    action record carries the old one while prices from the ex-date on sit
    under the new one. The NSE symbol is the stable identity, so follow it
    through ``NSESymbolInstrumentMap``.
    """
    ids = {instrument_id}
    ids.update(
        NSESymbolInstrumentMap.objects.filter(
            symbol__symbol__in=symbols
        ).values_list("instrument_id", flat=True)
    )
    return ids


def observed_ratio(instrument_ids, ex_date):
    """``(ratio, note)``: first close on/after ex-date over last close before it.

    ``ratio`` is None when either side is missing (``note`` says why). Uses a
    single series: EQ if it has data on both sides, else the first one that does.
    """
    rows = (
        NSECmPriceHistory.objects.filter(
            instrument_id__in=instrument_ids,
            trade_date__gte=ex_date - timedelta(days=WINDOW_DAYS),
            trade_date__lte=ex_date + timedelta(days=WINDOW_DAYS),
            close__isnull=False,
        )
        .order_by("trade_date")
        .values_list("trade_date", "close", "series__code")
    )
    by_series = {}
    for trade_date, close, code in rows:
        by_series.setdefault(code, []).append((trade_date, close))

    codes = sorted(by_series, key=lambda c: (c != _PREFERRED_SERIES, c))
    for code in codes:
        series_rows = by_series[code]
        before = [r for r in series_rows if r[0] < ex_date]
        after = [r for r in series_rows if r[0] >= ex_date]
        if before and after and before[-1][1] > 0:
            return (after[0][1] / before[-1][1]), ""
    return None, "no close before and after the ex-date"


def classify(expected, observed):
    """Map an expected factor and observed ratio to a ``price_check`` result."""
    if abs(1 / expected - 1) <= TOLERANCE:
        return CorporateAction.PRICE_INCONCLUSIVE
    if abs(observed / expected - 1) <= TOLERANCE:
        return CorporateAction.PRICE_PASSED
    return CorporateAction.PRICE_FAILED


def check_group(instrument_id, ex_date):
    """Run the check for one instrument/ex-date and update its actions."""
    actions = list(
        CorporateAction.objects.filter(instrument_id=instrument_id, ex_date=ex_date)
    )
    if not actions:
        return None

    expected = expected_factor(instrument_id, ex_date)
    observed = None
    note = ""
    if expected is None:
        result = CorporateAction.PRICE_NOT_APPLICABLE
    else:
        symbols = {a.symbol for a in actions}
        observed, note = observed_ratio(
            price_instrument_ids(instrument_id, symbols), ex_date
        )
        if observed is None:
            result = CorporateAction.PRICE_NO_DATA
        else:
            observed = observed.quantize(_QUANT)
            result = classify(expected, observed)
            if result == CorporateAction.PRICE_FAILED:
                note = "price check failed: observed %s vs expected %s" % (
                    observed.normalize(), expected.normalize(),
                )

    now = timezone.now()
    for action in actions:
        action.expected_factor = expected
        action.observed_ratio = observed
        action.price_check = result
        action.price_note = note
        action.price_checked_at = now
        action.refresh_status()
        action.save(
            update_fields=[
                "expected_factor", "observed_ratio", "price_check", "price_note",
                "price_checked_at", "status", "review_reason", "updated_at",
            ]
        )
    return result


def check_groups(groups):
    """Check an iterable of ``(instrument_id, ex_date)``; returns result counts."""
    counts = {}
    for instrument_id, ex_date in sorted(set(groups), key=lambda g: (g[1], g[0])):
        result = check_group(instrument_id, ex_date)
        if result is not None:
            counts[result] = counts.get(result, 0) + 1
    return counts
