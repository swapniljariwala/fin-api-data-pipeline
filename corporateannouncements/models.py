from django.db import models

from securityinfo.models import Instrument


class AnnouncementCategory(models.Model):
    """Lookup of NSE announcement categories (raw `desc` string)."""

    name = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "announcement_categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CorporateAnnouncement(models.Model):
    """One NSE corporate announcement record."""

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="announcements"
    )
    category = models.ForeignKey(
        AnnouncementCategory, on_delete=models.PROTECT, related_name="announcements"
    )
    seq_id = models.BigIntegerField(unique=True)
    announced_at = models.DateTimeField(db_index=True)
    exchange_received_at = models.DateTimeField(null=True, blank=True)
    attachment_text = models.TextField(blank=True)
    attachment_url = models.URLField(max_length=500, blank=True)
    attachment_file_size = models.CharField(max_length=20, blank=True)
    has_xbrl = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "corporate_announcements"
        ordering = ["-announced_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["seq_id"], name="uniq_announcement_seq_id"
            ),
        ]
        indexes = [
            models.Index(fields=["announced_at"], name="idx_announcement_date"),
            models.Index(fields=["instrument"], name="idx_announcement_instrument"),
            models.Index(fields=["category"], name="idx_announcement_category"),
            models.Index(
                fields=["instrument", "announced_at"],
                name="idx_annc_instr_date",
            ),
        ]

    def __str__(self):
        return f"#{self.seq_id} {self.announced_at}"
