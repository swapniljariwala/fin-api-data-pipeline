from django.db import models

from securityinfo.models import Instrument


class ShareholdingPattern(models.Model):
    """One NSE shareholding-pattern filing, keyed on NSE's ``recordId``."""

    class ExtractionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="shareholding_patterns"
    )
    record_id = models.BigIntegerField(unique=True)
    isin = models.CharField(max_length=12, blank=True)
    symbol = models.CharField(max_length=50)
    company_name = models.CharField(max_length=255, blank=True)
    segment = models.CharField(max_length=20, default="equities")
    quarter_end = models.DateField()
    broadcast_at = models.DateTimeField(null=True, blank=True)
    submitted_on = models.DateField(null=True, blank=True)
    promoter_percent = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True
    )
    public_percent = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True
    )
    revised = models.BooleanField(default=False)
    revised_status = models.CharField(max_length=20, blank=True)
    revised_at = models.DateTimeField(null=True, blank=True)
    revision_remark = models.TextField(blank=True)
    xbrl_url = models.URLField(max_length=500, blank=True)
    xbrl_file_size = models.CharField(max_length=20, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    extraction_status = models.CharField(
        max_length=10,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extraction_attempts = models.PositiveSmallIntegerField(default=0)
    extraction_error = models.TextField(blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    raw_object_path = models.CharField(max_length=300, blank=True)
    parsed_object_path = models.CharField(max_length=300, blank=True)
    parsed_size_bytes = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "shareholding_patterns"
        ordering = ["-broadcast_at", "-record_id"]
        indexes = [
            models.Index(
                fields=["instrument", "-quarter_end"], name="idx_shp_instr_quarter"
            ),
            models.Index(fields=["quarter_end"], name="idx_shp_quarter"),
            models.Index(fields=["extraction_status"], name="idx_shp_extraction"),
        ]

    def __str__(self):
        return f"#{self.record_id} {self.symbol} {self.quarter_end}"
