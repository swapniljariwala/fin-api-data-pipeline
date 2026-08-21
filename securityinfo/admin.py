from django.contrib import admin

from .models import Instrument, InstrumentTicker, Series


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    list_display = ["code", "description"]
    search_fields = ["code", "description"]


class InstrumentTickerInline(admin.TabularInline):
    model = InstrumentTicker
    extra = 0
    fields = ["source", "ticker", "fin_instrm_id"]


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ["isin", "name", "instrument_type", "created_at"]
    search_fields = ["isin", "name"]
    list_filter = ["instrument_type"]
    inlines = [InstrumentTickerInline]


@admin.register(InstrumentTicker)
class InstrumentTickerAdmin(admin.ModelAdmin):
    list_display = ["ticker", "source", "instrument", "fin_instrm_id"]
    search_fields = ["ticker", "instrument__isin", "instrument__name"]
    list_filter = ["source"]
    list_select_related = ["instrument"]
    autocomplete_fields = ["instrument"]
