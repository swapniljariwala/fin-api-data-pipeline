from django.db import models


class MutualFundAmc(models.Model):
    """Lookup of AMFI asset management companies (raw AMC name)."""

    name = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "mutual_fund_amcs"
        ordering = ["name"]

    def __str__(self):
        return self.name


class MutualFundCategory(models.Model):
    """Lookup of AMFI scheme categories (raw section-header text)."""

    name = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "mutual_fund_categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class MutualFundScheme(models.Model):
    """AMFI mutual fund scheme identity, keyed on the stable scheme code."""

    scheme_code = models.BigIntegerField(unique=True)
    scheme_name = models.CharField(max_length=255)
    plan = models.CharField(max_length=50, blank=True)
    option = models.CharField(max_length=50, blank=True)
    isin_growth = models.CharField(max_length=12, null=True, blank=True)
    isin_reinvest = models.CharField(max_length=12, null=True, blank=True)
    scheme_type = models.CharField(max_length=20)
    amc = models.ForeignKey(
        MutualFundAmc, on_delete=models.PROTECT, related_name="schemes"
    )
    category = models.ForeignKey(
        MutualFundCategory, on_delete=models.PROTECT, related_name="schemes"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mutual_fund_schemes"
        ordering = ["scheme_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["scheme_code"], name="uniq_mf_scheme_code"
            ),
        ]
        indexes = [
            models.Index(fields=["amc"], name="idx_mf_scheme_amc"),
            models.Index(fields=["category"], name="idx_mf_scheme_category"),
            models.Index(fields=["isin_growth"], name="idx_mf_scheme_isin_growth"),
            models.Index(fields=["isin_reinvest"], name="idx_mf_scheme_isin_reinvest"),
        ]

    def __str__(self):
        return f"{self.scheme_code} {self.scheme_name}"


class MutualFundNavHistory(models.Model):
    """One daily NAV observation for a mutual fund scheme."""

    scheme = models.ForeignKey(
        MutualFundScheme, on_delete=models.CASCADE, related_name="nav_history"
    )
    nav_date = models.DateField()
    nav = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )

    class Meta:
        db_table = "mutual_fund_nav_history"
        ordering = ["-nav_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["scheme", "nav_date"], name="uniq_nav_scheme_date"
            ),
        ]
        indexes = [
            models.Index(fields=["nav_date"], name="idx_nav_date"),
            models.Index(fields=["scheme"], name="idx_nav_scheme"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.nav_date}"
