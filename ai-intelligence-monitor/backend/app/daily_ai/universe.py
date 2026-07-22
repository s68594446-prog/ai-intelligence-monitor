import hashlib
from datetime import datetime, timezone

import xlrd

from app.daily_ai.contracts import AShareConstituent, AShareUniverse, UniverseSource, UniverseStatus


INDEX_URLS = {
    "931071": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/931071cons.xls",
    "950180": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/950180cons.xls",
}


def normalize_exchange(code: str, reported: str) -> str:
    value = reported.lower()
    if "上海" in reported or "shanghai" in value or code.startswith(("6", "5")):
        return "SSE"
    if "深圳" in reported or "shenzhen" in value or code.startswith(("0", "3")):
        return "SZSE"
    if "北京" in reported or "beijing" in value or code.startswith(("4", "8", "9")):
        return "BSE"
    raise ValueError("unknown exchange")


def parse_official_constituents(payloads: dict[str, bytes], *, retrieved_at: datetime | None = None) -> AShareUniverse:
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    merged: dict[str, dict] = {}
    sources = []
    for index_code in ("931071", "950180"):
        body = payloads[index_code]
        book = xlrd.open_workbook(file_contents=body)
        sheet = book.sheet_by_index(0)
        if sheet.nrows < 2 or sheet.ncols < 9:
            raise ValueError("official constituent workbook schema invalid")
        sources.append(UniverseSource(index_code=index_code, url=INDEX_URLS[index_code], retrieved_at=retrieved_at, sha256=hashlib.sha256(body).hexdigest()))
        for row in range(1, sheet.nrows):
            code = str(sheet.cell_value(row, 4)).split(".")[0].zfill(6)
            if not code.isdigit() or len(code) != 6:
                continue
            name = str(sheet.cell_value(row, 5)).strip()
            exchange = normalize_exchange(code, str(sheet.cell_value(row, 7)))
            effective = str(sheet.cell_value(row, 0)).split(".")[0]
            current = merged.setdefault(code, {"security_code": code, "company_name": name, "exchange": exchange, "index_codes": [], "effective_date": effective})
            current["index_codes"] = sorted(set(current["index_codes"] + [index_code]))
    return AShareUniverse(status=UniverseStatus.VERIFIED, sources=sources, constituents=[AShareConstituent(**item) for item in sorted(merged.values(), key=lambda x: x["security_code"])])
