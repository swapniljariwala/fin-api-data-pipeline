from django.db import models

from securityinfo.models import Instrument


class EventCalendar(models.Model):
    """One NSE board-meeting / event calendar entry.

    NSE gives these records no ID, so ``record_key`` is a deterministic hash of
    the stable fields and is the idempotency key for re-runs.
    """

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="event_calendar"
    )
    record_key = models.CharField(max_length=64, unique=True)
    segment = models.CharField(max_length=20, default="equities")
    symbol = models.CharField(max_length=50)
    company_name = models.CharField(max_length=255, blank=True)
    event_date = models.DateField(db_index=True)
    purpose = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    announced_at = models.DateTimeField(null=True, blank=True)
    attachment_url = models.URLField(max_length=500, blank=True)
    ixbrl_url = models.URLField(max_length=500, blank=True)
    attachment_file_size = models.CharField(max_length=20, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "event_calendar"
        ordering = ["-event_date", "-announced_at"]
        indexes = [
            models.Index(
                fields=["instrument", "-event_date"], name="idx_event_instr_date"
            ),
            models.Index(fields=["purpose"], name="idx_event_purpose"),
        ]

    def __str__(self):
        return f"{self.symbol} {self.event_date}: {self.purpose}"
