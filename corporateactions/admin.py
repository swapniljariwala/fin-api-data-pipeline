from django.contrib import admin

from .models import CorporateAction, CorporateActionLeg


class CorporateActionLegInline(admin.TabularInline):
    model = CorporateActionLeg
    extra = 0


@admin.register(CorporateAction)
class CorporateActionAdmin(admin.ModelAdmin):
    list_display = [
        "symbol", "ex_date", "subject", "status", "price_check",
        "expected_factor", "observed_ratio",
    ]
    list_filter = ["status", "price_check", "legs__kind"]
    search_fields = ["symbol", "company_name", "instrument__isin", "subject"]
    date_hierarchy = "ex_date"
    list_per_page = 100
    inlines = [CorporateActionLegInline]
    actions = ["mark_verified"]

    @admin.action(description="Mark selected actions as verified")
    def mark_verified(self, request, queryset):
        queryset.update(
            status=CorporateAction.STATUS_VERIFIED, review_reason=""
        )
