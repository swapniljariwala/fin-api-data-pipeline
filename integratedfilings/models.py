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
        ]

    def __str__(self):
        scope = {True: "consolidated", False: "standalone"}.get(self.consolidated, "")
        return f"#{self.seq_id} {self.instrument} {self.period_end} {scope}".strip()
