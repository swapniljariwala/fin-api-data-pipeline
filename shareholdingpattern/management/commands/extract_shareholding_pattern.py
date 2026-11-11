"""Download each listed shareholding filing's XBRL, parse it and upload the result.

Works through ``shareholding_patterns`` rows whose ``extraction_status`` is
``pending`` (or ``failed`` with attempts left), newest broadcast first. For each
filing it downloads the XBRL, parses it with ``jugaad-xbrl``'s
``parse_shareholding()``, uploads the raw XML and one JSON document, then marks
the row ``done``.

Destination: ``--local-dir`` (testing), else the bucket named by ``--bucket`` or
the ``GCS_SHAREHOLDING_PATTERN_BUCKET`` environment variable (Google Application
Default Credentials).

``--retry-missing`` re-tries failed rows whose problem is on NSE's side (HTTP 404
or a non-XML download). ``--rebuild`` re-creates the parsed JSON of ``done`` rows
that have none, reading the raw XML already in the bucket (no NSE access).
"""

import logging
import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from shareholdingpattern.extraction import ExtractionError, extract, object_paths
from shareholdingpattern.models import ShareholdingPattern
from securityinfo.models import NSESymbolInstrumentMap
from shareholdingpattern.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

BLOCKED = object()
BLOCK_STATUSES = (403, 429)
REBUILD_FAILED = "rebuild failed"
SOURCE_MISSING_ERRORS = ("HTTPError: 404", "ExtractionError: download is not XML")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class Command(BaseCommand):
    help = "Download pending shareholding-pattern filings, parse them and upload the result."

    def add_arguments(self, parser):
        parser.add_argument(
            "--bucket", help="GCS bucket (default: $GCS_SHAREHOLDING_PATTERN_BUCKET)"
        )
        parser.add_argument(
            "--local-dir",
            metavar="PATH",
            help="write to this folder instead of a bucket (testing)",
        )
        parser.add_argument(
            "--limit", type=int, default=200, help="max filings per run (default: 200)"
        )
        parser.add_argument(
            "--record-id",
            type=int,
            metavar="N",
            help="extract this one filing, whatever its status",
        )
        parser.add_argument(
            "--symbol",
            metavar="SYM",
            help="only filings for this NSE symbol (resolved to its instrument)",
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=3,
            help="give up on a failing filing after N attempts (default: 3)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="seconds between downloads (default: 1.0, 0 disables)",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=60,
            help="download timeout in seconds (default: 60)",
        )
        parser.add_argument(
            "--retry-missing",
            action="store_true",
            help="re-try failed filings that NSE did not serve (404 / empty file), "
            "regardless of attempts; for a weekly job",
        )
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="re-create parsed JSON of done filings from the raw XML already "
            "in the bucket (no NSE access)",
        )

    def handle(self, *args, **options):
        if options["limit"] <= 0 or options["max_attempts"] <= 0:
            raise CommandError("--limit and --max-attempts must be positive integers")
        try:
            storage = get_storage(options["bucket"], options["local_dir"])
        except StorageError as exc:
            raise CommandError(str(exc))

        rebuild = options["rebuild"]
        todo = self._select(options)
        logger.info(
            "Shareholding pattern %s started (%d filing(s), dest=%s)",
            "rebuild" if rebuild else "extraction",
            len(todo),
            storage,
        )
        started = time.monotonic()
        done = failed = 0
        blocked = False
        delay = 0 if rebuild else options["delay"]
        try:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            for i, filing in enumerate(todo):
                if i and delay:
                    time.sleep(delay)
                outcome = self._process(
                    filing, storage, session, options["timeout"], rebuild
                )
                if outcome is BLOCKED:
                    blocked = True
                    break
                if outcome:
                    done += 1
                else:
                    failed += 1
        finally:
            logger.info(
                "Shareholding pattern %s finished in %.1fs: %d done, %d failed%s",
                "rebuild" if rebuild else "extraction",
                time.monotonic() - started,
                done,
                failed,
                ", STOPPED: NSE refused a download" if blocked else "",
            )
        if blocked:
            raise CommandError(
                "NSE refused a download (HTTP 403/429); stopped early. The remaining "
                "filings stay pending and no attempt was counted; try again later."
            )
        if failed:
            raise CommandError(
                f"{failed} filing(s) failed; see the log (they are retried next run)"
            )

    @staticmethod
    def _instrument_id_for_symbol(symbol):
        mapped = [
            m.instrument
            for m in NSESymbolInstrumentMap.objects.filter(symbol__symbol=symbol)
            .select_related("instrument")
            .order_by("-id")
        ]
        instrument = next((i for i in mapped if i.isin), None) or (
            mapped[0] if mapped else None
        )
        return instrument.id if instrument else None

    def _select(self, options):
        qs = ShareholdingPattern.objects.exclude(xbrl_url="")
        if options["symbol"]:
            instrument_id = self._instrument_id_for_symbol(options["symbol"])
            if instrument_id is None:
                logger.warning(
                    "No instrument mapped to symbol %s; nothing to do", options["symbol"]
                )
                return []
            qs = qs.filter(instrument_id=instrument_id)
        if options["record_id"]:
            return list(qs.filter(record_id=options["record_id"]))
        if options["retry_missing"]:
            missing = Q()
            for prefix in SOURCE_MISSING_ERRORS:
                missing |= Q(extraction_error__startswith=prefix)
            qs = qs.filter(
                extraction_status=ShareholdingPattern.ExtractionStatus.FAILED
            ).filter(missing)
            return list(qs.order_by("-broadcast_at", "-record_id")[: options["limit"]])
        if options["rebuild"]:
            qs = (
                qs.filter(extraction_status=ShareholdingPattern.ExtractionStatus.DONE)
                .exclude(extraction_error__startswith=REBUILD_FAILED)
                .filter(parsed_object_path="")
            )
            return list(qs.order_by("-broadcast_at", "-record_id")[: options["limit"]])
        qs = qs.filter(
            extraction_status__in=[
                ShareholdingPattern.ExtractionStatus.PENDING,
                ShareholdingPattern.ExtractionStatus.FAILED,
            ],
            extraction_attempts__lt=options["max_attempts"],
        )
        return list(qs.order_by("-broadcast_at", "-record_id")[: options["limit"]])

    def _process(self, filing, storage, session, timeout, rebuild=False):
        try:
            raw_path, _ = object_paths(filing)
            if rebuild:
                xml = storage.get(filing.raw_object_path or raw_path)
            else:
                xml = self._download(session, filing.xbrl_url, timeout)
            result = extract(filing, xml)
            if not rebuild:
                storage.put(raw_path, xml, "application/xml")
            storage.put(result.parsed_path, result.parsed_data)
            self._record(filing, result, rebuild)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if not rebuild and isinstance(exc, requests.HTTPError) and getattr(
                response, "status_code", None
            ) in BLOCK_STATUSES:
                logger.warning(
                    "record_id %s: HTTP %s from NSE - stopping this run",
                    filing.record_id,
                    response.status_code,
                )
                return BLOCKED
            if isinstance(exc, (ExtractionError, StorageError, requests.RequestException)):
                logger.warning(
                    "record_id %s (%s) failed: %s", filing.record_id, filing.symbol, exc
                )
            else:
                logger.exception("record_id %s failed unexpectedly", filing.record_id)
            if rebuild:
                ShareholdingPattern.objects.filter(pk=filing.pk).update(
                    extraction_error=f"{REBUILD_FAILED}: {type(exc).__name__}: {exc}"[:2000]
                )
            else:
                ShareholdingPattern.objects.filter(pk=filing.pk).update(
                    extraction_status=ShareholdingPattern.ExtractionStatus.FAILED,
                    extraction_attempts=filing.extraction_attempts + 1,
                    extraction_error=f"{type(exc).__name__}: {exc}"[:2000],
                )
            return False
        logger.info(
            "record_id %s (%s %s) %s",
            filing.record_id,
            filing.symbol,
            filing.quarter_end,
            "rebuilt" if rebuild else "uploaded",
        )
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
        ShareholdingPattern.objects.filter(pk=filing.pk).update(
            extraction_status=ShareholdingPattern.ExtractionStatus.DONE,
            extraction_attempts=filing.extraction_attempts + (0 if rebuild else 1),
            extraction_error="",
            extracted_at=timezone.now(),
            raw_object_path=filing.raw_object_path or result.raw_path,
            parsed_object_path=result.parsed_path,
            parsed_size_bytes=len(result.parsed_data),
        )
