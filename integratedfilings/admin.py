from django.contrib import admin

from .models import IntegratedFiling, IntegratedFilingStatement


@admin.register(IntegratedFiling)
class IntegratedFilingAdmin(admin.ModelAdmin):
    list_display = [
        "seq_id", "instrument", "period_end", "consolidated", "type_sub", "broadcast_at",
        "extraction_status",
    ]
    search_fields = ["instrument__isin", "instrument__name", "company_name", "seq_id"]
    list_filter = ["filing_type", "type_sub", "consolidated", "extraction_status"]
    date_hierarchy = "broadcast_at"
    list_per_page = 100


@admin.register(IntegratedFilingStatement)
class IntegratedFilingStatementAdmin(admin.ModelAdmin):
    list_display = ["filing", "statement", "role_code", "periods", "row_count", "size_bytes"]
    search_fields = ["statement", "filing__seq_id"]
    list_filter = ["statement"]
    raw_id_fields = ["filing"]
    list_per_page = 100
