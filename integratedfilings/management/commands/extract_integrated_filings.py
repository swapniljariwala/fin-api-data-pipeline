"""Download each listed filing's XBRL, split it into statements and upload them.

Works through ``integrated_filings`` rows whose ``extraction_status`` is
``pending`` (or ``failed`` with attempts left), **newest broadcast first** (so fresh
filings are never stuck behind a backfill). For each filing it:

1. downloads the XBRL XML from NSE,
2. parses it with ``jugaad-xbrl`` and splits it into one JSON object per
   statement (all periods of a statement inside the one object),
3. uploads the raw XML, every statement, ``canonical.json`` and finally
   ``_manifest.json`` (last, so a manifest means the set is complete) into a
   folder of its own, ``.../{basis}/{seq_id}/``,
4. records the statements in ``integrated_filing_statements`` and marks the
   filing ``done``.

Every release (Original or Revision) keeps its own folder and its own statement
rows, so nothing is overwritten; "latest" is decided by a query (see AGENTS.md).

If NSE answers HTTP 403/429 the run stops at once (filing stays ``pending``, no attempt
counted). A filing that fails otherwise is marked ``failed`` with the error and an attempt counted;
it is retried by later runs until ``--max-attempts``. A crash mid-filing leaves
it ``pending`` and the next run redoes it (uploads overwrite, so that is safe).
The run continues past a failed filing and exits non-zero at the end.

``--retry-missing`` re-tries ``failed`` filings whose problem is on NSE's side (HTTP 404, or an empty /
non-XML download) whatever their attempt count, once per run; schedule it weekly in case NSE republishes
the file. Other failures (e.g. an unsupported taxonomy) are not touched.

``--rebuild`` re-creates the parsed objects of filings that are already ``done`` but have
no objects in the per-release layout (older runs wrote one shared folder per quarter, or
stored only the raw XML for a superseded release). It reads the raw XML from the bucket, so
NSE is not contacted and no attempt is counted. A filing that fails to rebuild keeps
``done`` and gets ``extraction_error = "rebuild failed: ..."`` so it is not retried forever.

Destination: ``--local-dir`` (testing), else the bucket named by ``--bucket`` or
the ``GCS_INTEGRATED_FILINGS_BUCKET`` environment variable (Google Application
Default Credentials). Only "Integrated Filing- Financials" rows are processed.
"""

import logging
import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from integratedfilings.extraction import ExtractionError, extract, raw_path_for
from integratedfilings.models import IntegratedFiling, IntegratedFilingStatement
from integratedfilings.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

FINANCIALS = "Integrated Filing- Financials"
BLOCKED = object()  # _process result: NSE refused the download (HTTP 403/429)
BLOCK_STATUSES = (403, 429)
REBUILD_FAILED = "rebuild failed"
# failures caused by NSE not serving the file (not by our parser): worth retrying later
SOURCE_MISSING_ERRORS = ("HTTPError: 404", "ExtractionError: download is not XML")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class Command(BaseCommand):
    help = "Download pending integrated filings, split them into statements and upload."

    def add_arguments(self, parser):
        parser.add_argument("--bucket", help="GCS bucket (default: $GCS_INTEGRATED_FILINGS_BUCKET)")
        parser.add_argument("--local-dir", metavar="PATH",
                            help="write to this folder instead of a bucket (testing)")
        parser.add_argument("--limit", type=int, default=200,
                            help="max filings per run (default: 200)")
        parser.add_argument("--seq-id", type=int, metavar="N",
                            help="extract this one filing, whatever its status")
        parser.add_argument("--symbol", metavar="SYM", help="only this NSE symbol")
        parser.add_argument("--max-attempts", type=int, default=3,
                            help="give up on a failing filing after N attempts (default: 3)")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="seconds between downloads (default: 1.0, 0 disables)")
        parser.add_argument("--timeout", type=float, default=60,
                            help="download timeout in seconds (default: 60)")
        parser.add_argument("--retry-missing", action="store_true",
                            help="re-try failed filings that NSE did not serve (404 / empty file), "
                                 "regardless of attempts; for a weekly job")
        parser.add_argument("--rebuild", action="store_true",
                            help="re-create parsed objects of done filings from the raw XML "
                                 "already in the bucket (no NSE access)")

    def handle(self, *args, **options):
        if options["limit"] <= 0 or options["max_attempts"] <= 0:
            raise CommandError("--limit and --max-attempts must be positive integers")
        try:
            storage = get_storage(options["bucket"], options["local_dir"])
        except StorageError as exc:
            raise CommandError(str(exc))

        rebuild = options["rebuild"]
        todo = self._select(options)
        logger.info("Integrated filings %s started (%d filing(s), dest=%s)",
                    "rebuild" if rebuild else "extraction", len(todo), storage)
        started = time.monotonic()
        done = failed = 0
        blocked = False
        delay = 0 if rebuild else options["delay"]  # rebuild reads our own bucket
        try:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            for i, filing in enumerate(todo):
                if i and delay:
                    time.sleep(delay)
                outcome = self._process(filing, storage, session, options["timeout"], rebuild)
                if outcome is BLOCKED:
                    blocked = True
                    break
                if outcome:
                    done += 1
                else:
                    failed += 1
        finally:
            logger.info("Integrated filings %s finished in %.1fs: %d done, %d failed%s",
                        "rebuild" if rebuild else "extraction",
                        time.monotonic() - started, done, failed,
                        ", STOPPED: NSE refused a download" if blocked else "")
        if blocked:
            raise CommandError(
                "NSE refused a download (HTTP 403/429); stopped early. The remaining filings "
                "stay pending and no attempt was counted; try again later."
            )
        if failed:
            raise CommandError(f"{failed} filing(s) failed; see the log (they are retried next run)")

    # ----------------------------------------------------------------- select

    @staticmethod
    def _select(options):
        qs = IntegratedFiling.objects.filter(filing_type=FINANCIALS).exclude(xbrl_url="")
        if options["symbol"]:
            qs = qs.filter(raw__symbol=options["symbol"])
        if options["seq_id"]:
            return list(qs.filter(seq_id=options["seq_id"]))
        if options["retry_missing"]:
            missing = Q()
            for prefix in SOURCE_MISSING_ERRORS:
                missing |= Q(extraction_error__startswith=prefix)
            qs = qs.filter(extraction_status=IntegratedFiling.ExtractionStatus.FAILED).filter(missing)
            return list(qs.order_by("-broadcast_at")[: options["limit"]])
        if options["rebuild"]:
            in_new_layout = IntegratedFilingStatement.objects.filter(
                filing=OuterRef("pk"),
                object_path__contains=Concat(
                    Value("/"), Cast(OuterRef("seq_id"), CharField()), Value("/")
                ),
            )
            qs = (
                qs.filter(extraction_status=IntegratedFiling.ExtractionStatus.DONE)
                .exclude(extraction_error__startswith=REBUILD_FAILED)
                .exclude(Exists(in_new_layout))
            )
            return list(qs.order_by("-broadcast_at")[: options["limit"]])
        qs = qs.filter(
            extraction_status__in=[
                IntegratedFiling.ExtractionStatus.PENDING,
                IntegratedFiling.ExtractionStatus.FAILED,
            ],
            extraction_attempts__lt=options["max_attempts"],
        )
        return list(qs.order_by("-broadcast_at")[: options["limit"]])

    # ---------------------------------------------------------------- process

    def _process(self, filing, storage, session, timeout, rebuild=False):
        """Extract one filing. Returns True on success, False on failure."""
        try:
            if rebuild:
                xml = storage.get(raw_path_for(filing))
                result = extract(filing, xml)
            else:
                xml = self._download(session, filing.xbrl_url, timeout)
                result = extract(filing, xml)
                storage.put(result.raw_path, xml, "application/xml")
            for s in result.statements:
                storage.put(s.path, s.data)
            storage.put(result.canonical_path, result.canonical_data)
            storage.put(result.manifest_path, result.manifest_data)  # last
            self._record(filing, result, rebuild)
        except Exception as exc:  # one bad filing must not stop the run
            response = getattr(exc, "response", None)
            if not rebuild and isinstance(exc, requests.HTTPError) and getattr(
                response, "status_code", None
            ) in BLOCK_STATUSES:
                # we are being throttled or blocked: do not hammer on, and do not
                # burn the filing's attempts; it stays pending for a later run
                logger.warning("seq_id %s: HTTP %s from NSE - stopping this run",
                               filing.seq_id, response.status_code)
                return BLOCKED
            if isinstance(exc, (ExtractionError, StorageError, requests.RequestException)):
                logger.warning("seq_id %s (%s) failed: %s", filing.seq_id,
                               (filing.raw or {}).get("symbol"), exc)
            else:
                logger.exception("seq_id %s failed unexpectedly", filing.seq_id)
            if rebuild:
                # stays done; the note keeps it out of the next rebuild batch
                IntegratedFiling.objects.filter(pk=filing.pk).update(
                    extraction_error=f"{REBUILD_FAILED}: {type(exc).__name__}: {exc}"[:2000]
                )
            else:
                IntegratedFiling.objects.filter(pk=filing.pk).update(
                    extraction_status=IntegratedFiling.ExtractionStatus.FAILED,
                    extraction_attempts=filing.extraction_attempts + 1,
                    extraction_error=f"{type(exc).__name__}: {exc}"[:2000],
                )
            return False
        logger.info("seq_id %s (%s %s): %d statement(s) %s", filing.seq_id,
                    (filing.raw or {}).get("symbol"), filing.period_end,
                    len(result.statements), "rebuilt" if rebuild else "uploaded")
        return True

    @staticmethod
    def _download(session, url, timeout):
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        body = response.content
        if not body.lstrip().startswith(b"<"):
            raise ExtractionError(f"download is not XML ({len(body)} bytes)")
        return body

    @staticmethod
    def _record(filing, result, rebuild=False):
        with transaction.atomic():
            # only this release's own rows: other releases keep theirs
            filing.statements.all().delete()
            IntegratedFilingStatement.objects.bulk_create(
                IntegratedFilingStatement(
                    filing=filing,
                    statement=s.statement,
                    role_code=s.role_code,
                    description=s.description,
                    periods=s.periods,
                    item_count=s.item_count,
                    row_count=s.row_count,
                    object_path=s.path,
                    size_bytes=len(s.data),
                )
                for s in result.statements
            )
            IntegratedFiling.objects.filter(pk=filing.pk).update(
                extraction_status=IntegratedFiling.ExtractionStatus.DONE,
                extraction_attempts=filing.extraction_attempts + (0 if rebuild else 1),
                extraction_error="; ".join(result.warnings)[:2000],
                extracted_at=timezone.now(),
            )
