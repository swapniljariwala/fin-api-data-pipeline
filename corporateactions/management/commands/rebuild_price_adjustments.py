"""Rebuild the ``price_adjustment_segments`` table from stored corporate actions.

Safe to run any time and as often as you like: it computes the expected ranges
for every instrument from the actions and prices already in the database and
writes only the instruments whose stored ranges differ, so an up-to-date run
changes nothing. Run it after the daily ingests (it is chained after
``ingest_corporate_actions`` in cron) and after any price or corporate-action
backfill.

``--dry-run`` reports what would change without writing; ``--full`` rewrites
every instrument's ranges even if they match.
"""

from django.core.management.base import BaseCommand

from corporateactions import adjustments


class Command(BaseCommand):
    help = "Rebuild price-adjustment ranges (split/bonus/consolidation) from corporate actions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="rewrite every instrument's ranges, not just the ones that differ",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="show what would change without writing",
        )

    def handle(self, *args, **options):
        summary = adjustments.rebuild(full=options["full"], dry_run=options["dry_run"])
        self.stdout.write(
            "{verb}: {instruments_changed}/{instruments_examined} instrument(s) "
            "{changed_verb}, {rows_written} range row(s) written, {rows_deleted} replaced; "
            "{events_applied} event(s) applied, {events_waiting} waiting for prices".format(
                verb="dry run" if summary["dry_run"] else "rebuilt",
                changed_verb="would change" if summary["dry_run"] else "changed",
                **summary,
            )
        )
