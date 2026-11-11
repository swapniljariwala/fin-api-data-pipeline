"""Turn a shareholding-pattern filing into the objects that go in the bucket.

Pure functions: XML bytes in, named objects out. No network, no database.

Layout (``symbol`` is URL-quoted; ``date`` is the filing's ``date`` field)::

    shareholding/{symbol}/{date}/{record_id}.xml     the original XML, as filed
    shareholding/{symbol}/{date}/{record_id}.json    parse_shareholding(...).to_dict()

``jugaad-xbrl`` 0.6.0 parses BSE shareholding-pattern documents through its
separate ``parse_shareholding()`` entry point (not ``parse()``, which is for
NSE integrated financial results).
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote


class ExtractionError(Exception):
    pass


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def dumps(obj):
    """Compact UTF-8 JSON; Decimals become numbers."""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), default=_json_default
    ).encode("utf-8")


@dataclass
class Extracted:
    raw_path: str
    parsed_path: str
    parsed_data: bytes
    payload: dict


def object_paths(filing):
    symbol = quote(filing.symbol or "", safe="")
    day = filing.quarter_end.isoformat()
    base = f"shareholding/{symbol}/{day}/{filing.record_id}"
    return f"{base}.xml", f"{base}.json"


def extract(filing, xml_bytes):
    """Parse ``xml_bytes`` (the XBRL of ``filing``) into bucket objects."""
    from jugaad_xbrl import parse_shareholding

    try:
        parsed = parse_shareholding(xml_bytes)
        payload = parsed.to_dict()
    except Exception as exc:
        raise ExtractionError(f"parse failed: {type(exc).__name__}: {exc}") from exc

    raw_path, parsed_path = object_paths(filing)
    source = payload.setdefault("source", {})
    source.update(
        {
            "record_id": filing.record_id,
            "symbol": filing.symbol,
            "isin": filing.isin,
            "segment": filing.segment,
            "quarter_end": filing.quarter_end.isoformat(),
            "broadcast_at": filing.broadcast_at.isoformat() if filing.broadcast_at else None,
            "submitted_on": filing.submitted_on.isoformat() if filing.submitted_on else None,
            "revised": filing.revised,
            "xbrl_url": filing.xbrl_url,
            "report_start": parsed.report_start.isoformat() if parsed.report_start else None,
            "report_end": parsed.report_end.isoformat() if parsed.report_end else None,
        }
    )
    return Extracted(
        raw_path=raw_path,
        parsed_path=parsed_path,
        parsed_data=dumps(payload),
        payload=payload,
    )
