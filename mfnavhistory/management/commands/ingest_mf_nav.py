"""Fetch AMFI mutual fund NAV history and ingest it into the database.

``jugaad-data``'s ``AMFI`` module downloads the daily NAV history report from
AMFI's ``DownloadNAVHistoryReport_Po.aspx`` endpoint. That is a **range report**
(``nav_history_raw(from_date, to_date, mf="")``), not a dated archive file, so
there is no per-day ``BhavcopyFile``-style provenance table — the same
live-endpoint situation as ``ingest_corporate_announcements``.

The report is a semicolon-separated file, one row per scheme per date, with the
scheme type + category and the AMC carried in section headers that the library
flattens into columns on every returned row. All values come back as strings; we
parse ``scheme_code`` (int), ``nav`` (``Decimal`` or ``None``) and ``date``
(``%d-%b-%Y``) ourselves.

Mode selection (exactly one):

* ``--latest`` (default): fetch ``[today - lookback + 1, today]`` in one call and
  ingest every date present.
* ``--from YYYY-MM-DD --to YYYY-MM-DD``: inclusive range, partitioned into
  ``--chunk-days`` chunks and processed **oldest-first**.
* ``--days N``: last N calendar days ending today.

Idempotency comes from the row-level unique key ``(scheme, nav_date)`` via
``bulk_create(ignore_conflicts=True)`` inside one transaction per chunk, so
re-running an overlapping window is a no-op for rows already present. Scheme
dimension attributes (name, plan, option, ISINs, scheme type, AMC, category) are
updated **latest-wins** as newer rows are seen.

Progress and errors go to console and ``logs/pipeline.log`` via the
project-level ``LOGGING`` configuration.
"""

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mfnavhistory.models import (
    MutualFundAmc,
    MutualFundCategory,
    MutualFundNavHistory,
    MutualFundScheme,
)

logger = logging.getLogger(__name__)

UNSPECIFIED = "Unspecified"


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "-", "NA", "nan") else value


class Command(BaseCommand):
    help = "Fetch AMFI mutual fund NAV history and ingest it into the database."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--latest",
            action="store_true",
            help=(
                "Fetch the last --lookback days in one call (default mode). "
                "Every date present in the response is ingested."
            ),
        )
        mode.add_argument(
            "--from",
            dest="from_date",
            metavar="YYYY-MM-DD",
            help="Inclusive start date; processed oldest-first.",
        )
        mode.add_argument(
            "--days",
            type=int,
            metavar="N",
            help="Last N calendar days ending today.",
        )
        parser.add_argument(
            "--to",
            dest="to_date",
            metavar="YYYY-MM-DD",
            help="Inclusive end date (with --from); defaults to today.",
        )
        parser.add_argument(
            "--lookback",
            type=int,
            default=5,
            help="How far back --latest looks. Default 5.",
        )
        parser.add_argument(
            "--amc",
            metavar="CODE",
            help="Restrict the download to a single AMFI AMC code (mf=CODE).",
        )
        parser.add_argument(
            "--chunk-days",
            type=int,
            default=30,
            help="Chunk long ranges into windows of this many days. Default 30.",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Seconds to wait between chunks (0 disables). Default 1.0.",
        )
        parser.add_argument(
            "--retries",
            type=int,
            default=3,
            help="Total attempts per chunk, backing off by --delay. Default 3.",
        )

    def handle(self, *args, **options):
        from_date, to_date, single_call = self._resolve_range(options)

        if options["chunk_days"] <= 0:
            raise CommandError("--chunk-days must be a positive integer")
        if options["delay"] < 0:
            raise CommandError("--delay must be non-negative")
        if options["retries"] < 1:
            raise CommandError("--retries must be at least 1")

        amc = _clean(options["amc"])

        if single_call:
            chunks = [(from_date, to_date)]
        else:
            chunks = self._partition(from_date, to_date, options["chunk_days"])

        logger.info(
            "MF NAV ingest job started (from=%s, to=%s, chunks=%d, amc=%s, "
            "chunk_days=%d, delay=%s, retries=%d)",
            from_date,
            to_date,
            len(chunks),
            amc,
            options["chunk_days"],
            options["delay"],
            options["retries"],
        )
        started = time.monotonic()
        try:
            self._run(chunks, amc, options["delay"], options["retries"])
        finally:
            logger.info(
                "MF NAV ingest job finished in %.1fs", time.monotonic() - started
            )

    # ------------------------------------------------------------------- modes

    def _resolve_range(self, options):
        """Return (from_date, to_date, single_call)."""
        if options["days"] is not None:
            if options["days"] <= 0:
                raise CommandError("--days must be a positive integer")
            if options["to_date"]:
                raise CommandError("--to cannot be combined with --days")
            return (
                date.today() - timedelta(days=options["days"] - 1),
                date.today(),
                False,
            )
        if options["from_date"]:
            from_date = self._parse_date(options["from_date"], "--from")
            to_date = self._parse_date(
                options["to_date"] or date.today().isoformat(), "--to"
            )
            if from_date > to_date:
                raise CommandError("--from must be on or before --to")
            return from_date, to_date, False
        if options["to_date"]:
            raise CommandError("--to requires --from")
        if options["lookback"] <= 0:
            raise CommandError("--lookback must be a positive integer")
        return (
            date.today() - timedelta(days=options["lookback"] - 1),
            date.today(),
            True,
        )

    @staticmethod
    def _partition(from_date, to_date, chunk_days):
        """Split an inclusive range into oldest-first chunks of chunk_days."""
        chunks = []
        start = from_date
        while start <= to_date:
            end = min(start + timedelta(days=chunk_days - 1), to_date)
            chunks.append((start, end))
            start = end + timedelta(days=1)
        return chunks

    # -------------------------------------------------------------------- run

    def _run(self, chunks, amc, delay, retries):
        # Per-run caches shared across all chunks.
        self._amc_cache = {}
        self._category_cache = {}
        self._scheme_cache = {}

        total_inserted = 0
        total_rows = 0
        total_duplicates = 0
        total_skipped = 0
        failed = []

        for index, (chunk_from, chunk_to) in enumerate(chunks, start=1):
            if index > 1 and delay:
                time.sleep(delay)
            logger.info(
                "Chunk %d/%d: fetching %s to %s", index, len(chunks), chunk_from, chunk_to
            )
            records = self._fetch_with_retries(
                chunk_from, chunk_to, amc, delay, retries
            )
            if records is None:
                failed.append((chunk_from, chunk_to))
                continue
            logger.info("Chunk %d/%d: fetched %d row(s)", index, len(chunks), len(records))

            inserted, duplicates, skipped = self._ingest_records(records)
            total_inserted += inserted
            total_rows += len(records)
            total_duplicates += duplicates
            total_skipped += skipped
            logger.info(
                "Chunk %d/%d: %d inserted, %d duplicates, %d unparseable",
                index,
                len(chunks),
                inserted,
                duplicates,
                skipped,
            )

        logger.info(
            "MF NAV ingest complete: %d/%d row(s) inserted (%d duplicates, "
            "%d unparseable, %d failed chunk(s))",
            total_inserted,
            total_rows,
            total_duplicates,
            total_skipped,
            len(failed),
        )
        if failed:
            detail = ", ".join(f"{a}..{b}" for a, b in failed)
            raise CommandError(f"{len(failed)} chunk(s) failed after retries: {detail}")

    def _fetch_with_retries(self, chunk_from, chunk_to, amc, delay, retries):
        from jugaad_data.amfi import AMFI

        for attempt in range(1, retries + 1):
            try:
                return AMFI().nav_history_raw(chunk_from, chunk_to, mf=amc or "")
            except Exception:
                logger.warning(
                    "chunk %s..%s failed (attempt %d/%d)",
                    chunk_from,
                    chunk_to,
                    attempt,
                    retries,
                    exc_info=True,
                )
                if attempt < retries and delay:
                    time.sleep(delay * attempt)
        logger.error(
            "chunk %s..%s failed after %d attempt(s)", chunk_from, chunk_to, retries
        )
        return None

    # ---------------------------------------------------------------- ingest

    def _ingest_records(self, records):
        rows = []
        skipped = 0
        for record in records:
            row = self._parse_record(record)
            if row is None:
                skipped += 1
                continue
            rows.append(row)

        with transaction.atomic():
            before = MutualFundNavHistory.objects.count()
            MutualFundNavHistory.objects.bulk_create(
                rows, ignore_conflicts=True, batch_size=1000
            )
            inserted = MutualFundNavHistory.objects.count() - before
        return inserted, len(rows) - inserted, skipped

    def _parse_record(self, record):
        scheme_code = _clean(record.get("scheme_code"))
        date_raw = _clean(record.get("date"))
        if scheme_code is None or date_raw is None:
            logger.warning("skipping row missing scheme_code/date: %r", record)
            return None
        try:
            scheme_code = int(scheme_code)
        except ValueError:
            logger.warning("skipping row with non-numeric scheme_code: %r", record)
            return None
        try:
            nav_date = datetime.strptime(date_raw, "%d-%b-%Y").date()
        except ValueError:
            logger.warning(
                "skipping scheme %s with unparseable date %r", scheme_code, date_raw
            )
            return None

        nav_raw = _clean(record.get("nav"))
        if nav_raw is None:
            nav = None
        else:
            try:
                nav = Decimal(nav_raw)
            except InvalidOperation:
                logger.warning(
                    "skipping scheme %s on %s with unparseable nav %r",
                    scheme_code,
                    nav_date,
                    nav_raw,
                )
                return None

        scheme_type = _clean(record.get("scheme_type")) or UNSPECIFIED
        scheme = self._resolve_scheme(record, scheme_code, scheme_type)

        return MutualFundNavHistory(scheme=scheme, nav_date=nav_date, nav=nav)

    # ---------------------------------------------------------- master data

    def _resolve_scheme(self, record, scheme_code, scheme_type):
        amc_name = _clean(record.get("amc")) or UNSPECIFIED
        category_name = _clean(record.get("category")) or UNSPECIFIED
        amc = self._resolve_amc(amc_name)
        category = self._resolve_category(category_name)

        scheme_name = _clean(record.get("scheme_name")) or ""
        attrs = {
            "scheme_name": scheme_name,
            "plan": _clean(record.get("plan")) or "",
            "option": _clean(record.get("option")) or "",
            "isin_growth": _clean(record.get("isin_growth")),
            "isin_reinvest": _clean(record.get("isin_reinvest")),
            "scheme_type": scheme_type,
            "amc": amc,
            "category": category,
        }

        scheme = self._scheme_cache.get(scheme_code)
        if scheme is None:
            scheme, _ = MutualFundScheme.objects.get_or_create(
                scheme_code=scheme_code, defaults=attrs
            )
            self._scheme_cache[scheme_code] = scheme
            return scheme

        changed = [field for field, value in attrs.items() if getattr(scheme, field) != value]
        if changed:
            for field in changed:
                setattr(scheme, field, attrs[field])
            scheme.save(update_fields=changed)
        return scheme

    def _resolve_amc(self, name):
        amc = self._amc_cache.get(name)
        if amc is None:
            amc, _ = MutualFundAmc.objects.get_or_create(name=name)
            self._amc_cache[name] = amc
        return amc

    def _resolve_category(self, name):
        category = self._category_cache.get(name)
        if category is None:
            category, _ = MutualFundCategory.objects.get_or_create(name=name)
            self._category_cache[name] = category
        return category

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _parse_date(raw, label):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"{label} must be YYYY-MM-DD, got {raw!r}")
