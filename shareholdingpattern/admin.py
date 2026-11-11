from django.contrib import admin

from .models import ShareholdingPattern


@admin.register(ShareholdingPattern)
class ShareholdingPatternAdmin(admin.ModelAdmin):
    list_display = [
        "record_id", "instrument", "quarter_end", "broadcast_at", "revised",
        "extraction_status",
    ]
    search_fields = ["instrument__isin", "instrument__name", "symbol", "isin", "record_id"]
    list_filter = ["segment", "revised", "extraction_status"]
    date_hierarchy = "quarter_end"
    list_per_page = 100
