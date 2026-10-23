"""Build the ``price_adjustment_segments`` table from stored corporate actions.

The table turns split / bonus / consolidation legs into per-instrument date
ranges so an API can adjust prices with one range join (see
``PriceAdjustmentSegment``). Rules:

* Only legs of actions with status ``auto`` or ``verified`` count, so anything
  in ``needs_review`` (parse problem or failed price check) is never applied.
* An event is applied only once the symbol has a price on or after its
  ex-date (``ex_date <= latest price date``), not by the calendar. Until the
  first post-event close is in our data, history stays as traded; announced
  future events are never applied early.
* A face-value split gives the shares a new ISIN, while NSE's action record
  carries the old one and prices continue under the new one. So events attach
  to a *lineage*: the instruments mapped to the NSE symbol whose price
  histories join up. A gap of more than ``LINEAGE_GAP_DAYS`` between price
  histories starts a new lineage, so a symbol reused by another company later
  does not inherit the old company's splits.
* Every instrument of a lineage gets the same ranges. Ranges are not clipped to
  the instrument's price dates, so a later price backfill is covered without a
  rebuild.

``rebuild`` computes the expected ranges for all instruments in memory (reads
only) and writes just the instruments that differ from what is stored, so a
normal day writes nothing.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Min

from dailypricehistory.models import NSECmPriceHistory
from securityinfo.models import NSESymbolInstrumentMap

from .models import CorporateAction, CorporateActionLeg, PriceAdjustmentSegment
from .parsing import PRICE_ADJUSTING_KINDS

logger = logging.getLogger(__name__)

MODE_SPLIT_BONUS = PriceAdjustmentSegment.MODE_SPLIT_BONUS
START_OF_TIME = date(1900, 1, 1)
LINEAGE_GAP_DAYS = 365
_QUANT = Decimal("0.00000001")
_ID_CHUNK = 500


def _qualifying_events():
    """``{symbol: [(ex_date, factor), ...]}`` for applicable price-adjusting legs."""
    events = defaultdict(list)
    legs = CorporateActionLeg.objects.filter(
        kind__in=PRICE_ADJUSTING_KINDS,
        price_factor__isnull=False,
        action__status__in=[CorporateAction.STATUS_AUTO, CorporateAction.STATUS_VERIFIED],
    ).values_list("action__symbol", "action__instrument_id", "action__ex_date", "price_factor")
    action_instruments = defaultdict(set)
    for symbol, instrument_id, ex_date, factor in legs:
        events[symbol].append((ex_date, factor))
        action_instruments[symbol].add(instrument_id)
    return events, action_instruments


def _price_ranges(instrument_ids):
    """``{instrument_id: (first_price_date, last_price_date)}``."""
    ranges = {}
    ids = sorted(instrument_ids)
    for start in range(0, len(ids), _ID_CHUNK):
        rows = (
            NSECmPriceHistory.objects.filter(instrument_id__in=ids[start:start + _ID_CHUNK])
            .values("instrument_id")
            .annotate(first=Min("trade_date"), last=Max("trade_date"))
        )
        for row in rows:
            ranges[row["instrument_id"]] = (row["first"], row["last"])
    return ranges


def _lineages(instrument_ranges):
    """Group ``[(instrument_id, first, last)]`` into lineages of joined-up prices."""
    lineages = []
    for instrument_id, first, last in sorted(instrument_ranges, key=lambda r: r[1]):
        if lineages and first - lineages[-1]["last"] <= timedelta(days=LINEAGE_GAP_DAYS):
            current = lineages[-1]
            current["instruments"].append(instrument_id)
            current["last"] = max(current["last"], last)
        else:
            lineages.append(
                {"instruments": [instrument_id], "first": first, "last": last}
            )
    return lineages


def _segments(events):
    """Ranges ``[(valid_from, valid_to, factor)]`` for ``[(ex_date, factor)]``."""
    by_date = defaultdict(lambda: Decimal(1))
    for ex_date, factor in events:
        by_date[ex_date] *= factor
    dates = sorted(by_date)
    segments = []
    for i, ex_date in enumerate(dates):
        valid_from = START_OF_TIME if i == 0 else dates[i - 1]
        factor = Decimal(1)
        for later in dates[i:]:
            factor *= by_date[later]
        factor = factor.quantize(_QUANT)
        if factor != 1:
            segments.append((valid_from, ex_date, factor))
    return segments


def compute_expected():
    """Expected ranges for every instrument: ``({instrument_id: [segments]}, stats)``."""
    events, action_instruments = _qualifying_events()
    symbol_instruments = defaultdict(set)
    for symbol, instrument_id in NSESymbolInstrumentMap.objects.filter(
        symbol__symbol__in=list(events)
    ).values_list("symbol__symbol", "instrument_id"):
        symbol_instruments[symbol].add(instrument_id)
    for symbol, ids in action_instruments.items():
        symbol_instruments[symbol] |= ids

    ranges = _price_ranges({i for ids in symbol_instruments.values() for i in ids})

    expected = {}
    stats = {"events_applied": 0, "events_waiting": 0}
    for symbol, symbol_events in events.items():
        lineages = _lineages(
            [(i, *ranges[i]) for i in symbol_instruments[symbol] if i in ranges]
        )
        per_lineage = defaultdict(list)
        for ex_date, factor in symbol_events:
            for index, lineage in enumerate(lineages):
                if lineage["first"] < ex_date <= lineage["last"]:
                    per_lineage[index].append((ex_date, factor))
                    stats["events_applied"] += 1
                    break
            else:
                stats["events_waiting"] += 1
        for index, lineage_events in per_lineage.items():
            segments = _segments(lineage_events)
            for instrument_id in lineages[index]["instruments"]:
                expected[instrument_id] = segments
    return expected, stats


def rebuild(mode=MODE_SPLIT_BONUS, full=False, dry_run=False):
    """Bring the table in line with the stored actions; returns a summary dict."""
    expected, stats = compute_expected()
    existing = defaultdict(list)
    for instrument_id, valid_from, valid_to, factor in PriceAdjustmentSegment.objects.filter(
        mode=mode
    ).order_by("instrument_id", "valid_from").values_list(
        "instrument_id", "valid_from", "valid_to", "factor"
    ):
        existing[instrument_id].append((valid_from, valid_to, factor))

    instruments = set(expected) | set(existing)
    changed = sorted(
        i for i in instruments if full or expected.get(i, []) != existing.get(i, [])
    )
    written = deleted = 0
    for instrument_id in changed:
        deleted += len(existing.get(instrument_id, []))
        written += len(expected.get(instrument_id, []))
        if dry_run:
            continue
        with transaction.atomic():
            PriceAdjustmentSegment.objects.filter(
                instrument_id=instrument_id, mode=mode
            ).delete()
            PriceAdjustmentSegment.objects.bulk_create(
                PriceAdjustmentSegment(
                    instrument_id=instrument_id, mode=mode,
                    valid_from=valid_from, valid_to=valid_to, factor=factor,
                )
                for valid_from, valid_to, factor in expected.get(instrument_id, [])
            )
    summary = dict(
        stats,
        instruments_examined=len(instruments),
        instruments_changed=len(changed),
        rows_deleted=deleted,
        rows_written=written,
        dry_run=dry_run,
    )
    logger.info("Price adjustment rebuild (%s): %s", mode, summary)
    return summary
