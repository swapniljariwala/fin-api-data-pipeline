from django.contrib import admin

from .models import FnoContract, FnoPriceHistory


@admin.register(FnoContract)
class FnoContractAdmin(admin.ModelAdmin):
    list_display = [
        "underlying_symbol",
        "instrument_type",
        "expiry_date",
        "strike_price",
        "option_type",
    ]
    search_fields = ["underlying_symbol"]
    list_filter = ["instrument_type", "option_type"]
    date_hierarchy = "expiry_date"


@admin.register(FnoPriceHistory)
class FnoPriceHistoryAdmin(admin.ModelAdmin):
    list_display = [
        "contract",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    search_fields = ["contract__underlying_symbol"]
    list_filter = ["trade_date"]
    date_hierarchy = "trade_date"
    list_select_related = ["contract"]
    ordering = ["-trade_date"]
    list_per_page = 100
    autocomplete_fields = ["contract", "file"]
