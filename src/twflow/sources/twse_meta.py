"""上市／上櫃公司基本資料 —— 提供官方產業別，作為板塊分類的第一層.

上市：``https://openapi.twse.com.tw/v1/opendata/t187ap03_L``
上櫃：``https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O``

兩者都回傳 JSON 陣列，欄位名稱是中文。``產業別`` 有時是代號（``"24"``）、
有時是名稱（``"半導體業"``），所以兩種都要能處理。
"""

from __future__ import annotations

import json

from ..errors import ParseError
from ..httpclient import Fetcher
from .twse_common import is_common_stock, normalize_code

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
SOURCE = "twse_meta"

# 證交所產業別代號 → 名稱。回應給的是代號時用這張表還原。
INDUSTRY_CODES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷",
    "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業",
    "13": "電子工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅",
    "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
    "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒",
    "38": "居家生活", "80": "管理股票", "91": "存託憑證",
}

# 這些鍵名在上市/上櫃的資料集之間略有差異，都要涵蓋。
CODE_KEYS = ("公司代號", "SecuritiesCompanyCode", "股票代號", "Code")
NAME_KEYS = ("公司簡稱", "CompanyAbbreviation", "公司名稱", "CompanyName", "Name")
INDUSTRY_KEYS = ("產業別", "IndustryName", "SecuritiesIndustryCode", "industry")


def _pick(entry: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        val = entry.get(k)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def normalize_industry(raw: str) -> str:
    """把產業別正規化成中文名稱（代號會被還原）."""
    raw = (raw or "").strip()
    if not raw:
        return "未分類"
    key = raw.zfill(2)
    if key in INDUSTRY_CODES:
        return INDUSTRY_CODES[key]
    return raw


def parse(text: str, market: str = "TWSE") -> list[dict]:
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
        out.append(
            {
                "code": code,
                "name": _pick(entry, NAME_KEYS),
                "market": market,
                "industry": normalize_industry(_pick(entry, INDUSTRY_KEYS)),
            }
        )

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何公司資料", observed=len(payload))
    return out


def fetch(fetcher: Fetcher, market: str = "TWSE") -> list[dict]:
    url = TWSE_URL if market.upper() == "TWSE" else TPEX_URL
    resp = fetcher.get(url)
    return parse(resp.text, market=market.upper())
