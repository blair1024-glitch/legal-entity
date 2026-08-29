"""上櫃三大法人買賣超.

端點（OpenAPI，格式最穩定）::

    https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading

這個端點只提供**最新交易日**的資料，沒有日期參數。要抓歷史日期得改用
櫃買的網頁報表端點，格式較易變動，所以本模組以 OpenAPI 為主、
並在抓到的資料日期與請求日期不符時明確回報，而不是靜默存進錯的日期。

單位是**股**，與 TWSE T86 一致。
"""

from __future__ import annotations

import datetime as dt
import json

from ..errors import ParseError
from ..httpclient import Fetcher
from .twse_common import is_common_stock, normalize_code, to_float

URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
SOURCE = "tpex_insti"

CODE_KEYS = ("SecuritiesCompanyCode", "股票代號", "證券代號", "Code")
NAME_KEYS = ("CompanyName", "股票名稱", "證券名稱", "Name")
# 櫃買的英文鍵名在改版間變動過，中英文都涵蓋
FOREIGN_KEYS = (
    "ForeignInvestorsBuySell", "ForeignInvestorsNetBuySell",
    "外資及陸資買賣超股數", "外資買賣超股數",
)
TRUST_KEYS = (
    "SecuritiesInvestmentTrustBuySell", "InvestmentTrustNetBuySell",
    "投信買賣超股數",
)
DEALER_KEYS = (
    "DealersBuySell", "DealersNetBuySell", "自營商買賣超股數",
)
TOTAL_KEYS = (
    "TotalInstitutionalInvestorsBuySell", "TotalInstitutionalInvestorsNetBuySell",
    "三大法人買賣超股數",
)


def _pick(entry: dict, keys: tuple[str, ...]) -> object:
    for k in keys:
        if k in entry and entry[k] not in (None, ""):
            return entry[k]
    # 鍵名改版時的最後手段：用關鍵字模糊比對，避免整批資料歸零。
    for k, v in entry.items():
        flat = str(k).replace(" ", "")
        for want in keys:
            if want.replace(" ", "") in flat:
                return v
    return None


def parse(text: str) -> list[dict]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(SOURCE, f"回應不是合法 JSON: {exc}", observed=text[:200]) from exc

    if not isinstance(payload, list):
        raise ParseError(SOURCE, "預期 JSON 陣列", observed=type(payload).__name__)

    out: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        code = normalize_code(_pick(entry, CODE_KEYS))
        if not is_common_stock(code):
            continue

        foreign = to_float(_pick(entry, FOREIGN_KEYS))
        trust = to_float(_pick(entry, TRUST_KEYS))
        dealer = to_float(_pick(entry, DEALER_KEYS))
        total_raw = _pick(entry, TOTAL_KEYS)
        total = to_float(total_raw) if total_raw is not None else foreign + trust + dealer

        out.append(
            {
                "code": code,
                "name": str(_pick(entry, NAME_KEYS) or "").strip(),
                "market": "TPEX",
                "foreign_net": foreign,
                "trust_net": trust,
                "dealer_net": dealer,
                "total_net": total,
            }
        )

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何上櫃資料", observed=len(payload))
    return out


def fetch(fetcher: Fetcher, day: dt.date) -> list[dict]:
    """抓上櫃三大法人買賣超.

    注意這個 OpenAPI 端點只有最新交易日的資料，``day`` 只用來標記存入的
    日期。若需要回補歷史，得另外實作櫃買的網頁報表端點。
    """
    resp = fetcher.get(URL)
    rows = parse(resp.text)
    for r in rows:
        r["trade_date"] = day.isoformat()
    return rows
