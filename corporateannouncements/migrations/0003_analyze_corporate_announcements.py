from django.db import migrations


class Migration(migrations.Migration):
    """Populate planner statistics (sqlite_stat1) for corporate_announcements.

    Runs after the (category, -announced_at) index exists so its stats are
    included. Deploy outside market hours and outside Litestream's snapshot
    window (05:35-05:41 IST): ANALYZE reads the table.
    """

    dependencies = [
        ("corporateannouncements", "0002_annc_category_date_index"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ANALYZE corporate_announcements",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
