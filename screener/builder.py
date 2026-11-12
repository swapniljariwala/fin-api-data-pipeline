"""Build ``screener_snapshot`` from adjusted price history.

Reads the last ~5 years of ``nse_cm_price_history`` joined to
``price_adjustment_segments`` (see ``corporateactions.models``), computes the
metrics with :mod:`screener.metrics` and writes only the rows that changed.
"""

import logging
import time
from collections import defaultdict
from datetime import date, timedelta

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from corporateactions.models import CorporateAction
from corporateactions.parsing import PRICE_ADJUSTING_KINDS
from dailypricehistory.models import NSECmPriceHistory
from securityinfo.models import NSESymbolInstrumentMap

from . import metrics
from .models import ScreenerSnapshot

logger = logging.getLogger(__name__)

# Instruments whose last price is older than this (vs the newest date in the
# database) are delisted / suspended and get no snapshot row.
ACTIVE_WITHIN_DAYS = 30
PRIMARY_SERIES = "EQ"
READ_CHUNK = 300
WRITE_CHUNK = 500

_SQL = """
SELECT p.instrument_id, s.code, p.trade_date, p.open, p.high, p.low, p.close,
       p.prev_close, p.volume, p.turnover, p.num_trades,
       COALESCE(seg.factor, 1), d.deliv_per
FROM nse_cm_price_history p
JOIN series s ON s.id = p.series_id
LEFT JOIN price_adjustment_segments seg
  ON seg.instrument_id = p.instrument_id AND seg.mode = 'split_bonus'
 AND p.trade_date >= seg.valid_from AND p.trade_date < seg.valid_to
LEFT JOIN nse_cm_delivery d ON d.price_id = p.id
WHERE p.instrument_id IN ({marks}) AND p.trade_date >= %s
ORDER BY p.instrument_id, p.trade_date
"""

_FIELDS = [
    f.name
    for f in ScreenerSnapshot._meta.get_fields()
    if f.name not in ("instrument", "built_at")
]


def _f(value):
    return None if value is None else float(value)


def _bars_for(rows):
    """One row per date: the EQ row if there is one, else another series' row.

    Per date rather than per instrument, so a stock that moved from EQ to BE
    (or began trading in SM) keeps one continuous history.
    """
    by_date = {}
    for r in rows:  # ordered by date; EQ wins, otherwise the first row seen
        cur = by_date.get(r[2])
        if cur is None or (r[1] == PRIMARY_SERIES and cur[1] != PRIMARY_SERIES):
            by_date[r[2]] = r
    chosen = [by_date[d] for d in sorted(by_date)]
    bars = []
    for (_i, _c, day, o, h, l, c, pc, vol, turn, trades, factor, deliv) in chosen:
        if c is None or float(c) <= 0:
            continue
        k = float(factor)
        bars.append(
            metrics.Bar(
                trade_date=date.fromisoformat(str(day)[:10]),
                open=_f(o) and _f(o) * k,
                high=_f(h) and _f(h) * k,
                low=_f(l) and _f(l) * k,
                close=float(c) * k,
                prev_close=_f(pc) and _f(pc) * k,
                volume=None if vol is None else float(vol) / k,
                turnover=_f(turn),
                num_trades=trades,
                deliv_per=_f(deliv),
            )
        )
    return bars


def _pending_flags(instrument_ids, window_start):
    """Instruments with an unapplied (needs_review) split/bonus in the window."""
    symbols = defaultdict(set)
    for sym, inst in NSESymbolInstrumentMap.objects.filter(
        instrument_id__in=instrument_ids
    ).values_list("symbol__symbol", "instrument_id"):
        symbols[sym].add(inst)
    flagged = set()
    qs = CorporateAction.objects.filter(
        status=CorporateAction.STATUS_NEEDS_REVIEW, ex_date__gte=window_start
    )
    for action in qs.prefetch_related("legs"):
        kinds = {leg.kind for leg in action.legs.all()}
        if kinds and not (kinds & set(PRICE_ADJUSTING_KINDS)):
            continue  # decoded fine, and it does not rescale the price
        flagged |= symbols.get(action.symbol, set())
        flagged.add(action.instrument_id)
    return flagged & set(instrument_ids)


def compute_all():
    """``{instrument_id: metrics dict}`` for every active instrument."""
    t0 = time.monotonic()
    newest = NSECmPriceHistory.objects.aggregate(m=Max("trade_date"))["m"]
    if newest is None:
        return {}
    window_start = newest - timedelta(days=metrics.MAX_LOOKBACK_DAYS)
    active = sorted(
        NSECmPriceHistory.objects.filter(
            trade_date__gte=newest - timedelta(days=ACTIVE_WITHIN_DAYS)
        )
        .values_list("instrument_id", flat=True)
        .distinct()
    )
    logger.info(
        "stage 1/4 active instruments: %d (newest %s, window from %s) [%.2fs]",
        len(active), newest, window_start, time.monotonic() - t0,
    )
    result = {}
    totals = {"rows": 0, "sql": 0.0, "bars": 0.0, "compute": 0.0}
    n_chunks = (len(active) + READ_CHUNK - 1) // READ_CHUNK
    for start in range(0, len(active), READ_CHUNK):
        ids = active[start:start + READ_CHUNK]
        t = time.monotonic()
        grouped = defaultdict(list)
        with connection.cursor() as cur:
            cur.execute(
                _SQL.format(marks=",".join(["%s"] * len(ids))),
                [*ids, window_start.isoformat()],
            )
            for row in cur:
                grouped[row[0]].append(row)
        t_sql = time.monotonic() - t
        t = time.monotonic()
        all_bars = {i: _bars_for(rows) for i, rows in grouped.items()}
        t_bars = time.monotonic() - t
        t = time.monotonic()
        for inst_id, bars in all_bars.items():
            values = metrics.compute(bars)
            if values is not None:
                result[inst_id] = values
        t_comp = time.monotonic() - t
        n_rows = sum(len(r) for r in grouped.values())
        totals["rows"] += n_rows
        totals["sql"] += t_sql
        totals["bars"] += t_bars
        totals["compute"] += t_comp
        logger.info(
            "stage 2/4 chunk %d/%d: %d stocks, %d rows | sql %.2fs, bars %.2fs, compute %.2fs",
            start // READ_CHUNK + 1, n_chunks, len(grouped), n_rows, t_sql, t_bars, t_comp,
        )
    logger.info(
        "stage 2/4 done: %d rows | sql %.1fs, bars %.1fs, compute %.1fs",
        totals["rows"], totals["sql"], totals["bars"], totals["compute"],
    )
    t = time.monotonic()
    flagged = _pending_flags(list(result), window_start)
    logger.info(
        "stage 3/4 adjustment_pending flags: %d flagged [%.2fs]", len(flagged), time.monotonic() - t
    )
    for inst_id, values in result.items():
        values["adjustment_pending"] = inst_id in flagged
    return result


def rebuild(full=False, dry_run=False):
    t_all = time.monotonic()
    computed = compute_all()
    t = time.monotonic()
    existing = {row.pk: row for row in ScreenerSnapshot.objects.all()}
    now = timezone.now()

    to_create, to_update = [], []
    for inst_id, values in computed.items():
        old = existing.get(inst_id)
        if old is None:
            to_create.append(
                ScreenerSnapshot(instrument_id=inst_id, built_at=now, **values)
            )
        elif full or any(getattr(old, f) != values.get(f) for f in _FIELDS):
            for f in _FIELDS:
                setattr(old, f, values.get(f))
            old.built_at = now
            to_update.append(old)
    to_delete = [pk for pk in existing if pk not in computed]

    logger.info(
        "stage 4/4 diff: %d new, %d changed, %d to delete (%d stored) [%.2fs]",
        len(to_create), len(to_update), len(to_delete), len(existing), time.monotonic() - t,
    )
    summary = {
        "dry_run": dry_run,
        "examined": len(computed),
        "created": len(to_create),
        "updated": len(to_update),
        "deleted": len(to_delete),
    }
    if dry_run:
        logger.info("dry run: nothing written [total %.1fs]", time.monotonic() - t_all)
        return summary

    t = time.monotonic()
    with transaction.atomic():
        for i in range(0, len(to_create), WRITE_CHUNK):
            ScreenerSnapshot.objects.bulk_create(to_create[i:i + WRITE_CHUNK])
        for i in range(0, len(to_update), WRITE_CHUNK):
            ScreenerSnapshot.objects.bulk_update(
                to_update[i:i + WRITE_CHUNK], _FIELDS + ["built_at"]
            )
        for i in range(0, len(to_delete), WRITE_CHUNK):
            ScreenerSnapshot.objects.filter(pk__in=to_delete[i:i + WRITE_CHUNK]).delete()
    logger.info("write done [%.2fs]; total %.1fs: %s", time.monotonic() - t, time.monotonic() - t_all, summary)
    return summary
