from django.contrib import admin

from .models import (
    Instrument,
    InstrumentClassification,
    NSESymbolInstrumentMap,
    NSESymbols,
    Series,
)


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    list_display = ["code", "description"]
    search_fields = ["code", "description"]


class NSESymbolsInline(admin.TabularInline):
    model = NSESymbolInstrumentMap
    extra = 0
    fk_name = "instrument"
    fields = ["symbol"]


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ["isin", "name", "instrument_type", "created_at"]
    search_fields = ["isin", "name"]
    list_filter = ["instrument_type"]
    inlines = [NSESymbolsInline]


class InstrumentInline(admin.TabularInline):
    model = NSESymbolInstrumentMap
    extra = 0
    fk_name = "symbol"
    fields = ["instrument"]


@admin.register(NSESymbols)
class NSESymbolsAdmin(admin.ModelAdmin):
    list_display = ["symbol"]
    search_fields = ["symbol"]
    inlines = [InstrumentInline]


@admin.register(InstrumentClassification)
class InstrumentClassificationAdmin(admin.ModelAdmin):
    list_display = ["instrument", "symbol_used", "macro", "sector", "industry", "basic_industry", "status"]
    list_filter = ["status", "macro", "sector"]
    search_fields = ["instrument__isin", "instrument__name", "symbol_used"]
    raw_id_fields = ["instrument"]
