from django.db import models

from securityinfo.models import Instrument


class IntegratedFiling(models.Model):
    """One NSE "Integrated Filing" release, as listed by NSE's filings API.

    A revision is a separate release with its own ``seq_id`` (``type_sub`` =
    ``Revision``), so every row here is immutable history; "latest" is a query
    over ``(instrument, period_end, consolidated)`` by ``broadcast_at``.
    """

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="integrated_filings"
    )
    seq_id = models.BigIntegerField(unique=True)  # NSE's own filing ID
    filing_type = models.CharField(max_length=60)  # e.g. "Integrated Filing- Financials"
    type_sub = models.CharField(max_length=20, blank=True)  # Original / Revision
    period_end = models.DateField()  # quarter end, from `qe_Date`
    consolidated = models.BooleanField(null=True, blank=True)  # NULL: not applicable
    audited = models.CharField(max_length=20, blank=True)  # NSE's text, e.g. "Un-Audited"
    broadcast_at = models.DateTimeField()  # IST
    revised_at = models.DateTimeField(null=True, blank=True)
    revision_remark = models.TextField(blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    xbrl_url = models.URLField(max_length=500, blank=True)
    ixbrl_url = models.URLField(max_length=500, blank=True)
    xbrl_file_size = models.CharField(max_length=20, blank=True)  # as given, e.g. '104.84 KB'
    raw = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    # --- extraction progress (written by `extract_integrated_filings`) ---
    class ExtractionStatus(models.TextChoices):
        PENDING = "pending", "Pending"  # listed, not yet downloaded/parsed/uploaded
        DONE = "done", "Done"  # raw XML and every statement uploaded
        FAILED = "failed", "Failed"  # last attempt failed; see extraction_error

    extraction_status = models.CharField(
        max_length=10, choices=ExtractionStatus.choices, default=ExtractionStatus.PENDING
    )
    extraction_attempts = models.PositiveSmallIntegerField(default=0)
    extraction_error = models.TextField(blank=True)  # last error, or a note on a done row
    extracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "integrated_filings"
        ordering = ["-broadcast_at"]
        indexes = [
            models.Index(fields=["broadcast_at"], name="idx_intfiling_broadcast"),
            models.Index(
                fields=["instrument", "-period_end"], name="idx_intfiling_instr_period"
            ),
            models.Index(
                fields=["period_end", "filing_type"], name="idx_intfiling_period_type"
            ),
            models.Index(fields=["extraction_status"], name="idx_intfiling_extraction"),
        ]

    def __str__(self):
        scope = {True: "consolidated", False: "standalone"}.get(self.consolidated, "")
        return f"#{self.seq_id} {self.instrument} {self.period_end} {scope}".strip()


class IntegratedFilingStatement(models.Model):
    """One statement (P&L, balance sheet, ...) extracted from a filing.

    The JSON lives in the bucket at ``object_path``; this row is the index of
    what exists, so a reader can discover statements without listing the
    bucket. All periods of the statement (e.g. Q2 and H1) are inside the one
    object; ``periods`` lists their labels.
    """

    filing = models.ForeignKey(
        IntegratedFiling, on_delete=models.CASCADE, related_name="statements"
    )
    statement = models.CharField(max_length=60)  # jugaad-xbrl name, e.g. "profit_and_loss"
    role_code = models.CharField(max_length=10)  # XBRL role code, e.g. "100200"
    description = models.TextField(blank=True)  # what the section is, from jugaad-xbrl
    periods = models.JSONField(default=list)  # e.g. ["Q2", "H1"] or ["as_of 2026-09-30"]
    item_count = models.PositiveIntegerField(default=0)  # entries (one per period)
    row_count = models.PositiveIntegerField(default=0)  # most dimensional rows in a period
    object_path = models.CharField(max_length=300)
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integrated_filing_statements"
        constraints = [
            models.UniqueConstraint(
                fields=["filing", "role_code"], name="uq_intfiling_stmt_filing_role"
            ),
        ]
        indexes = [models.Index(fields=["statement"], name="idx_intfiling_stmt_name")]

    def __str__(self):
        return f"{self.filing_id} {self.statement}"
