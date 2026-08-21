from django.contrib import admin

from .models import BhavcopyFile, CmPriceHistory


@admin.register(BhavcopyFile)
class BhavcopyFileAdmin(admin.ModelAdmin):
    list_display = [
        "file_name",
        "source",
        "segment",
        "format",
        "trade_date",
        "row_count",
        "ingested_at",
    ]
    search_fields = ["file_name"]
    list_filter = ["source", "segment", "format"]
    date_hierarchy = "trade_date"
    readonly_fields = ["ingested_at"]


@admin.register(CmPriceHistory)
class CmPriceHistoryAdmin(admin.ModelAdmin):
    list_display = [
        "instrument_ticker",
        "trade_date",
        "series",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    ]
    search_fields = ["instrument_ticker__ticker", "instrument_ticker__instrument__isin"]
    list_filter = ["series", "trade_date"]
    date_hierarchy = "trade_date"
    list_select_related = ["instrument_ticker", "series"]
    ordering = ["-trade_date"]
    list_per_page = 100
    autocomplete_fields = ["instrument_ticker", "series", "file"]
