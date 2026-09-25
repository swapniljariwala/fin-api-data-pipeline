from django.contrib import admin
from .models import Index, IndexPriceHistory


@admin.register(Index)
class IndexAdmin(admin.ModelAdmin):
    list_display = ["source", "ticker", "name"]
    search_fields = ["ticker", "name"]
    list_filter = ["source"]


@admin.register(IndexPriceHistory)
class IndexPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ["index", "trade_date", "close", "volume"]
    search_fields = ["index__ticker"]
    list_filter = ["index", "trade_date"]
    date_hierarchy = "trade_date"
    list_per_page = 100
