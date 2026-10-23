"""Re-run the price sanity check on stored corporate actions.

``ingest_corporate_actions`` checks the actions it touches, but price data can
arrive later (bhavcopy backfills, new listings). Run this after a price
backfill to (re)check actions and move failures to ``needs_review``.

By default only actions with ``price_check=no_data`` are re-checked; use
``--all`` to re-check everything in the range.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from corporateactions import price_check
from corporateactions.models import CorporateAction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-run the price sanity check on stored corporate actions."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
        parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
        parser.add_argument("--symbol", metavar="SYM")
        parser.add_argument(
            "--all",
            action="store_true",
            help="re-check every action in range, not just those lacking price data",
        )

    def handle(self, *args, **options):
        actions = CorporateAction.objects.all()
        if options["from_date"]:
            actions = actions.filter(ex_date__gte=self._date(options["from_date"]))
        if options["to_date"]:
            actions = actions.filter(ex_date__lte=self._date(options["to_date"]))
        if options["symbol"]:
            actions = actions.filter(symbol=options["symbol"])
        if not options["all"]:
            actions = actions.filter(price_check=CorporateAction.PRICE_NO_DATA)

        groups = set(actions.values_list("instrument_id", "ex_date"))
        counts = price_check.check_groups(groups)
        logger.info("Price-checked %d group(s): %s", len(groups), counts)

        failed = CorporateAction.objects.filter(
            price_check=CorporateAction.PRICE_FAILED,
            status=CorporateAction.STATUS_NEEDS_REVIEW,
        )
        self.stdout.write(f"checked {len(groups)} group(s): {counts}")
        self.stdout.write(f"{failed.count()} action(s) currently failing the price check")

    @staticmethod
    def _date(raw):
        from datetime import date

        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise CommandError(f"expected YYYY-MM-DD, got {raw!r}")
