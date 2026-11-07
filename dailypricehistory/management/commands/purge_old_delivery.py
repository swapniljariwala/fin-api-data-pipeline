"""Purge delivery data older than a year.

Deletes ``nse_cm_delivery`` rows (and the ``bhavcopy_files`` provenance rows
of the full bhavcopies they came from) older than ``RETENTION_DAYS``. Price
rows are never touched. Dry run by default, same pattern as
``purge_expired_fno_contracts``.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from dailypricehistory.models import BhavcopyFile, NSECmDelivery

RETENTION_DAYS = 365


class Command(BaseCommand):
    help = "Purge delivery data older than a year (dry run by default)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="delete the old delivery rows and their file records "
            "(default: dry run)",
        )

    def handle(self, *args, **options):
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
        rows = NSECmDelivery.objects.filter(trade_date__lt=cutoff)
        files = BhavcopyFile.objects.filter(
            format=BhavcopyFile.FORMAT_FULL, trade_date__lt=cutoff
        )
        row_count = rows.count()
        file_count = files.count()

        if not row_count and not file_count:
            self.stdout.write("No delivery data older than the cutoff.")
            return

        action = "Deleting" if options["apply"] else "Would delete"
        self.stdout.write(
            f"{action} {row_count} delivery row(s) and {file_count} file "
            f"record(s) before {cutoff}."
        )
        if not options["apply"]:
            self.stdout.write("Re-run with --apply to delete them.")
            return

        with transaction.atomic():
            rows.delete()
            files.delete()
        self.stdout.write("Done.")
