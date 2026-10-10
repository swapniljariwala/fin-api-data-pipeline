from django.contrib import admin

from .models import (
    MutualFundAmc,
    MutualFundCategory,
    MutualFundNavHistory,
    MutualFundScheme,
)


@admin.register(MutualFundAmc)
class MutualFundAmcAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(MutualFundCategory)
class MutualFundCategoryAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(MutualFundScheme)
class MutualFundSchemeAdmin(admin.ModelAdmin):
    list_display = ["scheme_code", "scheme_name", "scheme_type", "amc", "category"]
    search_fields = ["scheme_name", "scheme_code", "isin_growth", "isin_reinvest"]
    list_filter = ["scheme_type", "amc", "category"]
    list_per_page = 100


@admin.register(MutualFundNavHistory)
class MutualFundNavHistoryAdmin(admin.ModelAdmin):
    list_display = ["scheme", "nav_date", "nav"]
    search_fields = ["scheme__scheme_name", "scheme__scheme_code"]
    list_filter = ["nav_date"]
    date_hierarchy = "nav_date"
    list_per_page = 100
