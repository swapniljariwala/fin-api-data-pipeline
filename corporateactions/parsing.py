"""Fixed-logic parser for the free-text ``subject`` of NSE corporate actions.

NSE's ``corporate_actions`` feed carries no structured economics: everything
(ratios, amounts, face values) is in the ``subject`` string, e.g.
``"Bonus 5:1"`` or ``"Face Value Split (Sub-Division) - From Rs 10/- Per Share
To Rs 2/- Per Share"``. One subject can hold several actions
(``"Annual General Meeting/Dividend - Rs 2 Per Share/Special Dividend - Rs 5
Per Share"``), so :func:`parse_subject` returns a list of *legs*.

Parsing is deliberately strict: anything it cannot decode completely is
reported in ``problems`` instead of being guessed, and the caller puts the
action up for review.

``price_factor`` on a leg is the factor to multiply **earlier** (pre ex-date)
prices by so they are comparable with later ones: ``new_fv / old_fv`` for a
split, ``held / (new + held)`` for a bonus. It is only set for the actions
that rescale the share price (split, bonus on equity, consolidation).

Bump :data:`PARSER_VERSION` whenever the parsing rules change; the ingest
command re-parses stored actions whose version is older.
"""

import re
from decimal import Decimal

PARSER_VERSION = 2

KIND_DIVIDEND = "DIVIDEND"
KIND_MEETING = "MEETING"
KIND_INTEREST = "INTEREST"
KIND_DISTRIBUTION = "DISTRIBUTION"
KIND_SPLIT = "SPLIT"
KIND_BONUS = "BONUS"
KIND_RIGHTS = "RIGHTS"
KIND_CONSOLIDATION = "CONSOLIDATION"
KIND_BUYBACK = "BUYBACK"
KIND_DEMERGER = "DEMERGER"
KIND_CAPITAL_REDUCTION = "CAPITAL_REDUCTION"
KIND_REDEMPTION = "REDEMPTION"

# Kinds that rescale the share price on the ex-date (see ``price_factor``).
PRICE_ADJUSTING_KINDS = (KIND_SPLIT, KIND_BONUS, KIND_CONSOLIDATION)

DIV_REGULAR = "regular"
DIV_INTERIM = "interim"
DIV_SPECIAL = "special"
DIV_SPECIAL_INTERIM = "special_interim"

_NUM_RAW = r"\d[\d,]*(?:\.\d+)?|\.\d+"
_NUM = r"(" + _NUM_RAW + r")"
_CUR = r"(?:rs|re)\.?"

# The currency marker is optional only when the number is followed by "Per"
# (``Special Dividend - 8 Per Share``); a bare ``Dividend - 5%`` stays unread.
_DIVIDEND = re.compile(
    r"(?:(special interim|special|interim)\s+)?dividend\b\s*-?\s*(?:of\s+)?"
    r"(?:(?:" + _CUR + r"\s*-?\s*(?:per\s+)?|per\s+)(?P<a>" + _NUM_RAW + r")"
    r"|(?P<b>" + _NUM_RAW + r")(?=\s*per\b))",
    re.I,
)
_BONUS = re.compile(r"bonus(\s+ncrps)?\s*(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS = re.compile(r"rights\s*(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS_PREMIUM = re.compile(r"@\s*premium\s*(?:" + _CUR + r")?\s*" + _NUM, re.I)
_FV_CHANGE = re.compile(
    r"from\s+" + _CUR + r"\s*" + _NUM + r"\s*/?-?\s*per\s+share\s+to\s+"
    + _CUR + r"\s*" + _NUM,
    re.I,
)
_DISTRIBUTION_TOTAL = re.compile(
    r"^(?:total\s+)?distribution\s*-?\s*(?:" + _CUR + r")?\s*" + _NUM, re.I
)
_COMPONENT = re.compile(
    r"([a-z][a-z ]{2,40}?)\s*-?\s*" + _CUR + r"\s*" + _NUM, re.I
)
_COMPONENT_TYPES = (
    "interest",
    "dividend",
    "return of capital",
    "repayment",
    "principal",
    "other income",
    "treasury",
)
_TYPE_ONLY = (
    (KIND_BUYBACK, re.compile(r"\bbuy ?back\b", re.I)),
    (KIND_DEMERGER, re.compile(r"\bdemerger\b", re.I)),
    (KIND_CAPITAL_REDUCTION, re.compile(r"\bcapital reduction\b", re.I)),
    (KIND_REDEMPTION, re.compile(r"\bredemption\b", re.I)),
)

_FACTOR_QUANT = Decimal("0.00000001")


def normalize_subject(subject):
    """Collapse runs of whitespace; NSE text has stray double spaces."""
    return " ".join((subject or "").split())


def _dec(text):
    return Decimal(text.replace(",", ""))


def _leg(kind, **fields):
    leg = {
        "kind": kind,
        "dividend_type": "",
        "amount": None,
        "ratio_new": None,
        "ratio_held": None,
        "old_face_value": None,
        "new_face_value": None,
        "premium": None,
        "price_factor": None,
        "extra": {},
    }
    leg.update(fields)
    return leg


def parse_subject(subject):
    """Decode a subject into ``(legs, problems)``.

    ``legs`` is a list of dicts whose keys match ``CorporateActionLeg``
    fields (minus ``action``/``ordinal``). ``problems`` is a list of strings;
    non-empty means the parse is incomplete and a human should look.
    """
    text = normalize_subject(subject)
    low = text.lower()
    legs = []
    problems = []

    if "per unit" in low or low.startswith("distribution"):
        return _parse_distribution(text), _distribution_problems(text)

    if "general meeting" in low:
        legs.append(_leg(KIND_MEETING))

    dividend_matches = list(_DIVIDEND.finditer(text))
    for m in dividend_matches:
        legs.append(
            _leg(
                KIND_DIVIDEND,
                dividend_type=_dividend_type(m.group(1)),
                amount=_dec(m.group("a") or m.group("b")),
            )
        )
    if low.count("dividend") > len(dividend_matches):
        problems.append("dividend without a readable amount")

    if low.startswith("interest payment"):
        legs.append(_leg(KIND_INTEREST))

    m = _BONUS.search(text)
    if m:
        new, held = int(m.group(2)), int(m.group(3))
        is_ncrps = bool(m.group(1))
        if new <= 0 or held <= 0:
            problems.append("bonus ratio must be positive")
        else:
            legs.append(
                _leg(
                    KIND_BONUS,
                    ratio_new=new,
                    ratio_held=held,
                    price_factor=(
                        None if is_ncrps else _factor(Decimal(held) / (new + held))
                    ),
                    extra={"instrument": "NCRPS"} if is_ncrps else {},
                )
            )
    elif "bonus" in low:
        problems.append("bonus without a readable ratio")

    m = _RIGHTS.search(text)
    if m:
        new, held = int(m.group(1)), int(m.group(2))
        premium = _RIGHTS_PREMIUM.search(text)
        if new <= 0 or held <= 0:
            problems.append("rights ratio must be positive")
        else:
            legs.append(
                _leg(
                    KIND_RIGHTS,
                    ratio_new=new,
                    ratio_held=held,
                    premium=_dec(premium.group(1)) if premium else None,
                )
            )
            if premium is None:
                problems.append("rights without a stated premium")
    elif "rights" in low:
        problems.append("rights without a readable ratio")

    is_split = bool(re.search(r"split|sub-?\s?division", low))
    is_consolidation = "consolidation" in low
    m = _FV_CHANGE.search(text)
    if (is_split or is_consolidation) and m:
        old_fv, new_fv = _dec(m.group(1)), _dec(m.group(2))
        if old_fv <= 0 or new_fv <= 0:
            problems.append("face values must be positive")
        else:
            legs.append(
                _leg(
                    KIND_CONSOLIDATION if is_consolidation else KIND_SPLIT,
                    old_face_value=old_fv,
                    new_face_value=new_fv,
                    price_factor=_factor(new_fv / old_fv),
                )
            )
    elif is_split:
        problems.append("split without readable face values")
    elif is_consolidation:
        problems.append("consolidation without readable face values")

    for kind, pattern in _TYPE_ONLY:
        if pattern.search(text):
            legs.append(_leg(kind))

    if not legs and not problems:
        problems.append("unrecognised subject")
    return legs, problems


def _factor(value):
    return value.quantize(_FACTOR_QUANT)


def _dividend_type(prefix):
    return {
        None: DIV_REGULAR,
        "interim": DIV_INTERIM,
        "special": DIV_SPECIAL,
        "special interim": DIV_SPECIAL_INTERIM,
    }[prefix.lower() if prefix else None]


def _parse_distribution(text):
    """InvIT/REIT distribution: total per unit plus a best-effort breakdown."""
    total = _DISTRIBUTION_TOTAL.search(text)
    components = []
    for m in _COMPONENT.finditer(text):
        label = m.group(1).lower()
        for component_type in _COMPONENT_TYPES:
            if component_type in label:
                components.append(
                    {"type": component_type, "amount": str(_dec(m.group(2)))}
                )
                break
    extra = {"components": components} if components else {}
    return [
        _leg(
            KIND_DISTRIBUTION,
            amount=_dec(total.group(1)) if total else None,
            extra=extra,
        )
    ]


def _distribution_problems(text):
    if _DISTRIBUTION_TOTAL.search(text):
        return []
    return ["distribution without a readable total per unit"]
