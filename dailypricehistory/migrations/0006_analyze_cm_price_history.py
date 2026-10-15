from django.db import migrations


class Migration(migrations.Migration):
    """Populate planner statistics so SQLite stops choosing the series_id index."""

    dependencies = [
        ("dailypricehistory", "0005_alter_bhavcopyfile_id_alter_nsecmpricehistory_id"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ANALYZE nse_cm_price_history",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
