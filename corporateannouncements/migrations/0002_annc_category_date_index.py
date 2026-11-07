from django.db import migrations, models


class Migration(migrations.Migration):
    """Composite index so `WHERE category_id=? ORDER BY announced_at DESC LIMIT n`
    needs no temp b-tree sort. Builds on a ~5.4 GB WAL DB replicated by
    Litestream: apply outside market hours and outside 05:35-05:41 IST."""

    dependencies = [
        ("corporateannouncements", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="corporateannouncement",
            index=models.Index(
                fields=["category", "-announced_at"], name="idx_annc_cat_date"
            ),
        ),
    ]
