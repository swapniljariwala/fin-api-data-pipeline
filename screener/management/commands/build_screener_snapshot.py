"""Rebuild the ``screener_snapshot`` table (latest price/volume metrics per stock).

Idempotent: writes only the instruments whose metrics changed, so a second run
writes nothing. ``--dry-run`` reports without writing; ``--full`` rewrites every
row. Run nightly after the evening ingest and after
``rebuild_price_adjustments``.
"""

from django.core.management.base import BaseCommand

from screener import builder


class Command(BaseCommand):
    help = "Rebuild the screener snapshot (adjusted price/volume metrics per instrument)."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true", help="rewrite every row")
        parser.add_argument(
            "--dry-run", action="store_true", help="show what would change without writing"
        )

    def handle(self, *args, **options):
        s = builder.rebuild(full=options["full"], dry_run=options["dry_run"])
        self.stdout.write(
            "{verb}: {examined} instrument(s) examined, {created} {c}, "
            "{updated} {u}, {deleted} {d}".format(
                verb="dry run" if s["dry_run"] else "rebuilt",
                c="to create" if s["dry_run"] else "created",
                u="to update" if s["dry_run"] else "updated",
                d="to delete" if s["dry_run"] else "deleted",
                **s,
            )
        )
