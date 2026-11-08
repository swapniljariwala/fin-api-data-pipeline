"""Turn one filing's XBRL into the objects that go in the bucket.

Pure functions: XML bytes in, named objects out. No network, no database.

Layout (``{basis}`` is ``standalone`` or ``consolidated``; every release - an
Original or a Revision - has its own ``seq_id`` folder, so nothing is overwritten)::

    raw/{symbol}/{fy}/{quarter}/{basis}/{seq_id}.xml                       original XML
    filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/{statement}.json      one per statement, all periods inside
    filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/canonical.json        ~12 headline numbers
    filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}/_manifest.json        which statements this release has

``fy`` is ``FY2027`` for April 2026 - March 2027; ``quarter`` is Q1 (Apr-Jun) to
Q4 (Jan-Mar). Both are derived from NSE's quarter-end date, not from the filer's
header facts (some filers mis-write those). "Latest release" of a symbol/quarter/basis
is a query over ``integrated_filings`` (newest ``done`` release by
``broadcast_at``, then ``seq_id``), not an overwrite.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import quote

QUARTER_BY_MONTH = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}


class ExtractionError(Exception):
    pass


def fy_and_quarter(period_end):
    """Indian financial year label and quarter for a quarter-end date."""
    quarter = QUARTER_BY_MONTH.get(period_end.month)
    if quarter is None:
        raise ExtractionError(
            f"period_end {period_end} is not a quarter end (Mar/Jun/Sep/Dec)"
        )
    fy = period_end.year + (1 if period_end.month > 3 else 0)
    return f"FY{fy}", quarter


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
class StatementObject:
    statement: str
    role_code: str
    description: str
    periods: list
    item_count: int
    row_count: int
    path: str
    data: bytes


@dataclass
class Extracted:
    base: str  # filings/{symbol}/{fy}/{quarter}/{basis}/{seq_id}
    raw_path: str
    statements: list = field(default_factory=list)  # list[StatementObject]
    canonical_path: str = ""
    canonical_data: bytes = b""
    manifest_path: str = ""
    manifest_data: bytes = b""
    warnings: list = field(default_factory=list)


def object_base(filing):
    """``(symbol, fy, quarter, basis)`` path parts for an ``IntegratedFiling``."""
    if filing.consolidated is None:
        raise ExtractionError("filing has no standalone/consolidated basis")
    symbol = (filing.raw or {}).get("symbol")
    if not symbol:
        raise ExtractionError("filing has no symbol")
    fy, quarter = fy_and_quarter(filing.period_end)
    basis = "consolidated" if filing.consolidated else "standalone"
    return quote(symbol, safe=""), fy, quarter, basis


def raw_path_for(filing):
    """Bucket path of the original XML of this release."""
    symbol, fy, quarter, basis = object_base(filing)
    return f"raw/{symbol}/{fy}/{quarter}/{basis}/{filing.seq_id}.xml"


def extract(filing, xml_bytes):
    """Parse ``xml_bytes`` (the XBRL of ``filing``) into bucket objects."""
    from jugaad_xbrl import parse

    symbol, fy, quarter, basis = object_base(filing)
    base = f"filings/{symbol}/{fy}/{quarter}/{basis}/{filing.seq_id}"  # one folder per release
    out = Extracted(base=base, raw_path=raw_path_for(filing))

    try:
        parsed = parse(xml_bytes)
        export = parsed.to_dict("all")
        sections = {s["name"]: s for s in parsed.sections()}
        canonical = parsed.canonical()
    except Exception as exc:
        raise ExtractionError(f"parse failed: {type(exc).__name__}: {exc}") from exc

    report = export.get("report", {})
    if report.get("quarter") not in (None, quarter) or report.get("financial_year") not in (
        None,
        int(fy[2:]),
    ):
        out.warnings.append(
            f"filing header says FY{report.get('financial_year')} {report.get('quarter')}, "
            f"NSE list says {fy} {quarter}; using the NSE list"
        )
    if export.get("report", {}).get("nature", "").lower() not in ("", basis):
        out.warnings.append(
            f"filing says {report['nature']}, NSE list says {basis}; using the NSE list"
        )

    entries_by_statement = {}
    for entry in export["statements"]:
        entries_by_statement.setdefault(entry["statement"], []).append(entry)
    if not entries_by_statement:
        raise ExtractionError("filing contains no statements")

    source = {
        "seq_id": filing.seq_id,
        "type_sub": filing.type_sub,
        "audited": filing.audited,
        "period_end": filing.period_end.isoformat(),
        "broadcast_at": filing.broadcast_at.isoformat(),
        "xbrl_url": filing.xbrl_url,
    }
    envelope = {
        "company": export.get("company", {}),
        "report": report,
        "units": export.get("units", {}),
        "source": source,
    }

    for name, entries in entries_by_statement.items():
        section = sections.get(name, {})
        payload = {**envelope, "statement": name, "role_code": section.get("code", ""),
                   "entries": entries}
        out.statements.append(
            StatementObject(
                statement=name,
                role_code=section.get("code", ""),
                description=section.get("description", ""),
                periods=section.get("periods", []),
                item_count=len(entries),
                row_count=max((len(e.get("rows") or []) for e in entries), default=0),
                path=f"{base}/{name}.json",
                data=dumps(payload),
            )
        )

    out.canonical_path = f"{base}/canonical.json"
    out.canonical_data = dumps(
        {**{k: envelope[k] for k in ("company", "report", "source")},
         "canonical": {k: v for k, v in vars(canonical).items()}}
    )

    out.manifest_path = f"{base}/_manifest.json"
    out.manifest_data = dumps(
        {
            "symbol": (filing.raw or {}).get("symbol"),
            "fy": fy,
            "quarter": quarter,
            "basis": basis,
            "format": parsed.format,
            "source": source,
            "raw_path": out.raw_path,
            "canonical_path": out.canonical_path,
            "warnings": out.warnings,
            "statements": [
                {
                    "statement": s.statement,
                    "role_code": s.role_code,
                    "periods": s.periods,
                    "rows": s.row_count,
                    "bytes": len(s.data),
                    "path": s.path,
                }
                for s in out.statements
            ],
        }
    )
    return out
