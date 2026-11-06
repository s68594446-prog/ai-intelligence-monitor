"""Bounded monetary normalization; preserves source spans and never rewrites prose."""
import hashlib
import re
from decimal import Decimal

NUM = r"(?:[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?)"
SCALE_TEXT = r"(?:trillion|billion|million|thousand|bn\b|mn\b|[bm]\b|万亿|亿|万|千)"
SCALE = {"": 1, "千": 1000, "万": 10_000, "亿": 10**8, "万亿": 10**12,
         "thousand": 1000, "million": 10**6, "billion": 10**9, "trillion": 10**12,
         "m": 10**6, "mn": 10**6, "b": 10**9, "bn": 10**9}
CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "美元": "USD",
            "cny": "CNY", "rmb": "CNY", "人民币": "CNY", "元人民币": "CNY",
            "元": "CNY", "yuan": "CNY", "eur": "EUR", "€": "EUR", "欧元": "EUR",
            "gbp": "GBP", "£": "GBP", "英镑": "GBP", "hkd": "HKD", "hk$": "HKD",
            "港元": "HKD", "港币": "HKD", "jpy": "JPY", "日元": "JPY",
            "twd": "TWD", "nt$": "TWD", "新台币": "TWD", "krw": "KRW", "韩元": "KRW"}
PREFIX = r"US\$|HK\$|NT\$|USD|CNY|RMB|EUR|GBP|HKD|JPY|TWD|KRW|人民币|新台币|港币|美元|欧元|英镑|港元|日元|韩元|\$|€|£"
SUFFIX = r"元人民币|人民币|新台币|美元|欧元|英镑|港元|港币|日元|韩元|USD|CNY|RMB|EUR|GBP|HKD|JPY|TWD|KRW|yuan\b|元"
MONEY = re.compile(
    rf"(?<![A-Za-z0-9])(?P<prefix>{PREFIX})\s*(?P<a>{NUM})\s*(?P<scale>{SCALE_TEXT})?"
    rf"|(?P<b>{NUM})\s*(?P<scale2>{SCALE_TEXT})?\s*(?P<suffix>{SUFFIX})", re.I)
CONNECTOR = r"\s*(?:to|[-–—至到])\s*"
SHARED_RANGE = re.compile(rf"(?P<n>{NUM})\s*(?P<s>{SCALE_TEXT})?{CONNECTOR}$", re.I)


def number(text: str) -> Decimal:
    if text[0].isascii() and text[0].isdigit():
        return Decimal(text.replace(",", ""))
    if "点" in text:
        left, right = text.split("点")
        digits = "".join(str("零一二三四五六七八九".index(c.replace("〇", "零").replace("两", "二"))) for c in right)
        return number(left) + Decimal("0." + digits)
    digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    total = section = current = 0
    for ch in text:
        if ch in digits:
            current = digits[ch]
        else:
            unit = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 10**8}[ch]
            if unit < 10_000:
                section += (current or 1) * unit
            elif unit == 10_000:
                total += (section + current) * unit
                section = 0
            else:
                total = (total + section + current) * unit
                section = 0
            current = 0
    return Decimal(total + section + current)


def period(text: str, start: int, end: int) -> str:
    markers = [(m.start(), m.end(), "annual") for m in re.finditer(
        r"每年|年度|annually|annual\b|each year|per year|a year", text, re.I)]
    for m in re.finditer(r"(?:\b(?:in|FY|year)\s*)?(20[0-9]{2})(?:年|\b)", text, re.I):
        markers.append((m.start(), m.end(), f"year:{m[1]}"))
    for m in re.finditer(r"([0-9]+|[一二三四五六七八九十]+|one|two|three|four|five|six|seven|eight|nine|ten)"
                         r"\s*[- ]?\s*(?:years?\b|年期|年)", text, re.I):
        word = m[1].lower()
        years = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11))).get(word)
        years = years if years is not None else int(number(word))
        markers.append((m.start(), m.end(), f"duration:{years}y" if years < 100 else f"year:{years}"))
    for m in re.finditer(r"(?:Q([1-4])\b|第([一二三四1-4])季度)", text, re.I):
        q = m[1] or str(int(number(m[2])))
        year = re.search(r"20[0-9]{2}", text)
        markers.append((m.start(), m.end(), f"quarter:{year[0] if year else '?'}Q{q}"))
    if not markers:
        return "unspecified"
    return min(markers, key=lambda m: max(start - m[1], m[0] - end, 0))[2]


def _monies(sentence: str) -> list[dict]:
    urls = [m.span() for m in re.finditer(r"https?://\S+", sentence)]
    tokens = []
    for m in MONEY.finditer(sentence):
        if any(a <= m.start() < b for a, b in urls):
            continue
        scale = (m["scale"] or m["scale2"] or "").lower()
        token = dict(start=m.start(), end=m.end(), value_text=m[0],
                     n=m["a"] or m["b"], scale=scale,
                     currency=CURRENCY[(m["prefix"] or m["suffix"]).lower()], bound=None)
        # A range may share a currency and multiplier: 2.5–3亿美元.
        prefix = sentence[tokens[-1]["end"] if tokens else 0:m.start()]
        omitted = SHARED_RANGE.search(prefix)
        if omitted:
            offset = m.start() - len(prefix)
            tokens.append(dict(start=offset + omitted.start(), end=offset + omitted.end(),
                               value_text=omitted[0], n=omitted["n"],
                               scale=(omitted["s"] or scale).lower(), currency=token["currency"],
                               bound="range_lower"))
            token["bound"] = "range_upper"
        elif tokens and tokens[-1]["currency"] == token["currency"] and re.fullmatch(CONNECTOR, prefix, re.I):
            previous = tokens[-1]
            previous["bound"], token["bound"] = "range_lower", "range_upper"
            previous["scale"] = previous["scale"] or scale
            token["scale"] = scale or previous["scale"]
        tokens.append(token)
    return tokens


METRICS = {
    "revenue": r"营收|营业收入|\brevenue\b",
    "profit": r"净利润|净收益|net (?:profit|income)",
    "capex": r"资本开支|资本支出|\bcapex\b|capital expenditure",
    "fundraising": r"筹集|筹资|融资|\braise\b|fundraising|\bIPO\b",
}
SCOPES = {"china": r"中国|\bChina\b", "us": r"美国|United States|\bU\.S\.",
          "uk": r"英国|United Kingdom|\bU\.K\.", "global": r"全球|\bglobal\b|worldwide"}


def local_context(sentence: str, start: int, end: int) -> str:
    # Distinct clauses can describe different payments/metrics in one sentence.
    # Keep thousands separators and explanatory parentheticals intact.
    breaks = list(re.finditer(r"，|,(?![0-9]{3}\b)|\b(?:but|whereas|while|and)\b", sentence, re.I))
    left = max((m.end() for m in breaks if m.end() <= start), default=0)
    right = min((m.start() for m in breaks if m.start() >= end), default=len(sentence))
    return sentence[left:right]


def sentence_parts(text: str):
    start = 0
    for stop in re.finditer(r"\.(?=\s|$)", text):
        # Approx. and geographical abbreviations must not lose the qualifier
        # attached to the following amount. Decimal points are not boundaries.
        if re.search(r"(?:approx|U\.S|U\.K|vs|Mr|Dr|Inc|Ltd)\.$", text[:stop.end()], re.I):
            continue
        yield start, text[start:stop.end()]
        start = stop.end()
    if start < len(text):
        yield start, text[start:]


def facts(text: str, source_url: str, *, field: str = "source") -> list[dict]:
    records = []
    # Sentence spans retain offsets into the exact input. Decimal points stay intact.
    for part in re.finditer(r"[^。！？;；\n]+", text):
        for piece_start, piece_text in sentence_parts(part[0]):
            sentence = piece_text.strip()
            if not sentence:
                continue
            sentence_start = part.start() + piece_start + len(piece_text) - len(piece_text.lstrip())
            for token in _monies(sentence):
                start, end = token["start"], token["end"]
                before = sentence[max(0, start - 28):start]
                bound = token["bound"] or (
                    "upper" if re.search(r"至多|不超过|最高|up to|at most", before, re.I)
                    else "lower" if re.search(r"至少|超过|逾|at least|more than|over\s*$", before, re.I)
                    else "approx" if re.search(r"约|大约|about|approx\.?|approximately|around|nearly", before, re.I)
                    else "exact")
                # Do not let a negated cash/payment word change the monetary basis.
                clause = local_context(sentence, start, end)
                context = re.sub(r"(?:并非|不是|不代表|不等于|尚未)[^，,。；;]*|\b(?:not|rather than) (?:a )?(?:cash|paid)[^,.;]*", "", clause, flags=re.I)
                state = ("estimate" if re.search(r"估计|预计|估算|estimate|forecast", context, re.I)
                         else "paid" if re.search(r"已支付|已支出|已拨付|has paid|paid out|disbursed|\bspent\b", context, re.I)
                         else "commitment" if re.search(r"承诺|计划|拟|将|commit|pledge|will\b|plan", context, re.I)
                         else "unspecified")
                kind = ("cash" if re.search(r"现金|cash", context, re.I)
                        else "compute_access" if re.search(r"算力|compute|computing", context, re.I) else "unspecified")
                scopes = sorted(k for k, pattern in SCOPES.items() if re.search(pattern, clause, re.I))
                metrics = sorted(k for k, pattern in METRICS.items() if re.search(pattern, clause, re.I))
                records.append({
                    "claim_id": hashlib.sha256(f"{source_url}|{field}|{sentence_start + start}|{sentence}".encode()).hexdigest()[:24],
                    "field": field, "source_url": source_url, "original_text": sentence,
                    "amount": str((number(token["n"]) * SCALE[token["scale"]]).normalize()),
                    "currency": token["currency"], "unit": kind, "period": period(sentence, start, end),
                    "fact_status": state, "bound": bound, "scope": sentence,
                    "scope_tags": scopes, "metric_tags": metrics, "value_text": token["value_text"],
                    "source_span": [sentence_start + start, sentence_start + end],
                })
    return records
