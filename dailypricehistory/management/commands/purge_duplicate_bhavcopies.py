"""Purge bhavcopy files whose rows duplicate the previous trading day's.

NSE's archive server can serve the most recent available bhavcopy for a
holiday or closure date, and older ingest runs trusted the filename and
stored that data under the wrong date. This command flags (and, with
``--apply``, deletes) files whose stored price rows are near-identical to
the previous trading day's file.

Safe by default: without ``--apply`` it only lists candidates.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from dailypricehistory.models import BhavcopyFile, CmPriceHistory

SIMILARITY = 0.98


class Command(BaseCommand):
    help = (
        "Purge bhavcopy files that duplicate the previous trading day "
        "(dry run by default)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="delete the flagged files and their price rows "
            "(default: dry run)",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        files = list(BhavcopyFile.objects.order_by("trade_date", "id"))
        flagged = []
        for i, f in enumerate(files):
            prev = None
            for j in range(i - 1, -1, -1):
                if files[j].trade_date < f.trade_date:
                    prev = files[j]
                    break
            if prev is None:
                continue
            n, same = self._overlap(f, prev)
            if n and same / n >= SIMILARITY:
                flagged.append((f, prev, n, same))

        if not flagged:
            self.stdout.write("No duplicate bhavcopy files found.")
            return

        action = "delete" if apply else "would delete"
        self.stdout.write(
            f"Found {len(flagged)} bhavcopy file(s) duplicating the "
            "previous trading day:"
        )
        for f, prev, n, same in flagged:
            self.stdout.write(
                f"  {f.file_name} (trade_date {f.trade_date}, "
                f"{f.row_count} rows) {action}; {same}/{n} rows match "
                f"{prev.file_name}"
            )
        if not apply:
            self.stdout.write(
                "Re-run with --apply to delete these files and rows."
            )
            return

        for f, prev, n, same in flagged:
            with transaction.atomic():
                deleted = CmPriceHistory.objects.filter(file=f).delete()[0]
                f.delete()
            self.stdout.write(
                f"  deleted {f.file_name} and {deleted} price row(s)"
            )

    def _overlap(self, f, prev):
        rows = list(
            CmPriceHistory.objects.filter(file=f).values_list(
                "instrument_ticker_id", "close"
            )
        )
        if not rows:
            return 0, 0
        prev_rows = dict(
            CmPriceHistory.objects.filter(file=prev).values_list(
                "instrument_ticker_id", "close"
            )
        )
        same = sum(1 for t, c in rows if prev_rows.get(t) == c)
        return len(rows), same
