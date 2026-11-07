from django.contrib import admin

from .models import IntegratedFiling


@admin.register(IntegratedFiling)
class IntegratedFilingAdmin(admin.ModelAdmin):
    list_display = [
        "seq_id", "instrument", "period_end", "consolidated", "type_sub", "broadcast_at",
    ]
    search_fields = ["instrument__isin", "instrument__name", "company_name", "seq_id"]
    list_filter = ["filing_type", "type_sub", "consolidated"]
    date_hierarchy = "broadcast_at"
    list_per_page = 100
