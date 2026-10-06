from django.contrib import admin

from .models import AnnouncementCategory, CorporateAnnouncement


@admin.register(AnnouncementCategory)
class AnnouncementCategoryAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(CorporateAnnouncement)
class CorporateAnnouncementAdmin(admin.ModelAdmin):
    list_display = ["instrument", "category", "announced_at", "has_xbrl"]
    search_fields = ["instrument__isin", "instrument__name"]
    list_filter = ["category", "has_xbrl"]
    date_hierarchy = "announced_at"
    list_per_page = 100
