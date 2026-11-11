from django.contrib import admin

from .models import EventCalendar


@admin.register(EventCalendar)
class EventCalendarAdmin(admin.ModelAdmin):
    list_display = ["symbol", "event_date", "purpose", "company_name", "announced_at"]
    search_fields = ["instrument__isin", "instrument__name", "symbol", "company_name"]
    list_filter = ["segment", "purpose"]
    date_hierarchy = "event_date"
    list_per_page = 100
