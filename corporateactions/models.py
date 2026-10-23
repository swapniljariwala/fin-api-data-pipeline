from django.db import models

from securityinfo.models import Instrument


class CorporateAction(models.Model):
    """One NSE corporate-action record (one row of the ``corporate_actions`` feed).

    The economics live in child :class:`CorporateActionLeg` rows, decoded from
    ``subject`` by ``corporateactions.parsing``. ``status`` is derived: an
    action is ``needs_review`` when the parser could not decode the subject
    completely or the price sanity check failed; ``verified`` is only ever set
    by a human and is never overwritten by re-parsing.
    """

    STATUS_AUTO = "auto"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_VERIFIED = "verified"
    STATUS_CHOICES = [
        (STATUS_AUTO, "auto"),
        (STATUS_NEEDS_REVIEW, "needs review"),
        (STATUS_VERIFIED, "verified"),
    ]

    PRICE_NOT_APPLICABLE = "not_applicable"
    PRICE_PASSED = "passed"
    PRICE_FAILED = "failed"
    PRICE_NO_DATA = "no_data"
    PRICE_INCONCLUSIVE = "inconclusive"
    PRICE_CHECK_CHOICES = [
        (PRICE_NOT_APPLICABLE, "not applicable"),
        (PRICE_PASSED, "passed"),
        (PRICE_FAILED, "failed"),
        (PRICE_NO_DATA, "no price data"),
        (PRICE_INCONCLUSIVE, "inconclusive"),
    ]

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="corporate_actions"
    )
    symbol = models.CharField(max_length=50)
    series = models.CharField(max_length=8, blank=True)
    company_name = models.CharField(max_length=255, blank=True)
    subject = models.TextField()
    face_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    ex_date = models.DateField()
    record_date = models.DateField(null=True, blank=True)
    book_closure_start = models.DateField(null=True, blank=True)
    book_closure_end = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_AUTO
    )
    review_reason = models.TextField(blank=True)
    parse_problems = models.JSONField(default=list, blank=True)
    parser_version = models.PositiveSmallIntegerField(default=0)

    # Price sanity check: product of the legs' price factors for this
    # instrument and ex-date vs the observed close-to-close move.
    expected_factor = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    observed_ratio = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    price_check = models.CharField(
        max_length=16, choices=PRICE_CHECK_CHOICES, default=PRICE_NOT_APPLICABLE
    )
    price_note = models.CharField(max_length=255, blank=True)
    price_checked_at = models.DateTimeField(null=True, blank=True)

    raw = models.JSONField(default=dict, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "corporate_actions"
        ordering = ["-ex_date", "symbol"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "ex_date", "subject"],
                name="uniq_corp_action_instr_exdate_subject",
            ),
        ]
        indexes = [
            models.Index(fields=["ex_date"], name="idx_corp_action_exdate"),
            models.Index(
                fields=["instrument", "ex_date"], name="idx_corp_action_instr_date"
            ),
            models.Index(fields=["status"], name="idx_corp_action_status"),
        ]

    def __str__(self):
        return f"{self.symbol} {self.ex_date} {self.subject[:60]}"

    def refresh_status(self):
        """Re-derive ``status``/``review_reason`` (caller saves). Verified is sticky."""
        if self.status == self.STATUS_VERIFIED:
            return
        reasons = list(self.parse_problems)
        if self.price_check == self.PRICE_FAILED:
            reasons.append(self.price_note or "price check failed")
        self.review_reason = "; ".join(reasons)
        self.status = self.STATUS_NEEDS_REVIEW if reasons else self.STATUS_AUTO


class CorporateActionLeg(models.Model):
    """One decoded component of a :class:`CorporateAction`.

    Which columns are filled depends on ``kind``: dividends use ``amount`` /
    ``dividend_type``; bonus and rights use ``ratio_new:ratio_held`` (new
    shares per held shares); splits and consolidations use the face values;
    ``price_factor`` is the multiplier for pre ex-date prices (split, bonus on
    equity, consolidation only).
    """

    KIND_CHOICES = [
        ("DIVIDEND", "dividend"),
        ("MEETING", "meeting"),
        ("INTEREST", "interest payment"),
        ("DISTRIBUTION", "distribution"),
        ("SPLIT", "split"),
        ("BONUS", "bonus"),
        ("RIGHTS", "rights"),
        ("CONSOLIDATION", "consolidation"),
        ("BUYBACK", "buyback"),
        ("DEMERGER", "demerger"),
        ("CAPITAL_REDUCTION", "capital reduction"),
        ("REDEMPTION", "redemption"),
    ]

    action = models.ForeignKey(
        CorporateAction, on_delete=models.CASCADE, related_name="legs"
    )
    ordinal = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    dividend_type = models.CharField(max_length=16, blank=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True
    )
    ratio_new = models.PositiveIntegerField(null=True, blank=True)
    ratio_held = models.PositiveIntegerField(null=True, blank=True)
    old_face_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    new_face_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    premium = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    price_factor = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "corporate_action_legs"
        ordering = ["action_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(
                fields=["action", "ordinal"], name="uniq_corp_action_leg_ordinal"
            ),
        ]
        indexes = [
            models.Index(fields=["kind"], name="idx_corp_action_leg_kind"),
        ]

    def __str__(self):
        return f"{self.action_id}#{self.ordinal} {self.kind}"


class PriceAdjustmentSegment(models.Model):
    """Materialized price-adjustment range for one instrument.

    Raw prices in ``nse_cm_price_history`` stay as traded. A price dated in
    ``[valid_from, valid_to)`` is multiplied by ``factor`` to put it on the
    share basis of the latest price (split / bonus / consolidation only for
    ``mode='split_bonus'``). Prices after the instrument's last qualifying
    event have no row, i.e. factor 1, so nothing needs updating day to day.
    The first range of an instrument starts at ``START_OF_TIME`` so the join
    needs no NULL handling.

    Rebuilt by ``rebuild_price_adjustments`` (see ``adjustments.py``); read
    with::

        SELECT p.trade_date, p.close * COALESCE(s.factor, 1)
        FROM nse_cm_price_history p
        LEFT JOIN price_adjustment_segments s
          ON s.instrument_id = p.instrument_id AND s.mode = 'split_bonus'
         AND p.trade_date >= s.valid_from AND p.trade_date < s.valid_to
    """

    MODE_SPLIT_BONUS = "split_bonus"

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="adjustment_segments"
    )
    mode = models.CharField(max_length=16, default=MODE_SPLIT_BONUS)
    valid_from = models.DateField()
    valid_to = models.DateField()
    factor = models.DecimalField(max_digits=18, decimal_places=8)
    built_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "price_adjustment_segments"
        ordering = ["instrument_id", "mode", "valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "mode", "valid_from"],
                name="uniq_adjustment_segment_start",
            ),
        ]

    def __str__(self):
        return f"{self.instrument_id} {self.valid_from}..{self.valid_to} x{self.factor}"
