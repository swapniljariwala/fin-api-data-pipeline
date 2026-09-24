"""Purge F&O contracts more than a year past expiry.

F&O contracts stop trading at expiry and are never revisited, so old
contracts (and their price history) are pure storage cost. Dry run by
default, same pattern as ``purge_duplicate_bhavcopies``.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand

from fnopricehistory.models import FnoContract, FnoPriceHistory

RETENTION_DAYS = 365


class Command(BaseCommand):
    help = (
        "Purge F&O contracts more than a year past expiry "
        "(dry run by default)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="delete the flagged contracts and their price rows "
            "(default: dry run)",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
        contracts = FnoContract.objects.filter(expiry_date__lt=cutoff)
        count = contracts.count()

        if not count:
            self.stdout.write("No expired F&O contracts found.")
            return

        row_count = FnoPriceHistory.objects.filter(contract__in=contracts).count()
        action = "Deleting" if apply else "Would delete"
        self.stdout.write(
            f"{action} {count} contract(s) expired before {cutoff} "
            f"and their {row_count} price row(s)."
        )
        if not apply:
            self.stdout.write(
                "Re-run with --apply to delete these contracts and rows."
            )
            return

        deleted, _ = contracts.delete()
        self.stdout.write(f"Deleted {deleted} row(s) total.")
