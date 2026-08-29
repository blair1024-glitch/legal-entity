"""上市三大法人買賣超日報（T86）—— 校準推估值的真實基準.

端點：``https://www.twse.com.tw/rwd/zh/fund/T86``
參數：``date=YYYYMMDD``、``selectType=ALL``、``response=json``
時效：**收盤後**（約 15:00–16:00）才會有當日資料。

單位是**股**，不是張。這是很常見的誤算來源，所以本模組一律保留原始的
股數，換算成張的動作留給呼叫端明確處理。
"""

from __future__ import annotations

import datetime as dt

from ..errors import ParseError
from ..httpclient import Fetcher
from .twse_common import (
    is_common_stock,
    cell,
    extract_table,
    find_column,
    load_json,
    normalize_code,
    to_float,
)

URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
SOURCE = "twse_t86"


def parse(text: str) -> list[dict]:
    """解析 T86 回應成逐檔三大法人買賣超（單位：股）.

    欄位以標題關鍵字定位。T86 的欄位曾經改版過（外資拆成「外陸資」與
    「外資自營商」、自營商拆成「自行買賣」與「避險」），所以：

    * 外資 = 外陸資買賣超 + 外資自營商買賣超（若後者存在）
    * 自營商 = 自營商買賣超（總計欄若存在就用它，否則自行買賣 + 避險）
    """
    payload = load_json(text, SOURCE)
    fields, rows = extract_table(payload, SOURCE)

    def col(pred, label, required=True):
        return find_column(fields, pred, source=SOURCE, label=label, required=required)

    i_code = col(lambda b, q: "證券代號" in b or "股票代號" in b, "證券代號")
    i_name = col(lambda b, q: "證券名稱" in b or "股票名稱" in b, "證券名稱", required=False)

    # 外資：主欄位是「外陸資買賣超股數」或舊版的「外資買賣超股數」。
    # 注意它的括號註記「(不含外資自營商)」裡也有「自營商」，所以判斷只看主體。
    i_foreign = col(
        lambda b, q: "買賣超" in b and b.startswith("外") and not b.startswith("外資自營商"),
        "外資買賣超股數",
    )
    # 外資自營商是改版後才拆出來的獨立欄位
    i_foreign_dealer = col(
        lambda b, q: "買賣超" in b and b.startswith("外資自營商"),
        "外資自營商買賣超股數",
        required=False,
    )

    i_trust = col(lambda b, q: "投信" in b and "買賣超" in b, "投信買賣超股數")

    # 自營商：總計欄沒有括號註記；自行買賣與避險則由註記區分
    i_dealer_total = col(
        lambda b, q: b.startswith("自營商") and "買賣超" in b and not q,
        "自營商買賣超股數（總計）",
        required=False,
    )
    i_dealer_self = col(
        lambda b, q: b.startswith("自營商") and "買賣超" in b and "自行買賣" in q,
        "自營商買賣超股數（自行買賣）",
        required=False,
    )
    i_dealer_hedge = col(
        lambda b, q: b.startswith("自營商") and "買賣超" in b and "避險" in q,
        "自營商買賣超股數（避險）",
        required=False,
    )
    if i_dealer_total is None and i_dealer_self is None and i_dealer_hedge is None:
        raise ParseError(SOURCE, "找不到任何自營商買賣超欄位", observed=fields)

    i_total = col(
        lambda b, q: "三大法人" in b and "買賣超" in b, "三大法人買賣超股數", required=False
    )

    out: list[dict] = []
    for row in rows:
        code = normalize_code(cell(row, i_code))
        # 只保留普通股——ETF、權證、受益證券沒有產業歸屬，
        # 放進板塊統計只會製造雜訊（見 is_common_stock）。
        if not is_common_stock(code):
            continue

        foreign = to_float(cell(row, i_foreign))
        if i_foreign_dealer is not None:
            foreign += to_float(cell(row, i_foreign_dealer))

        trust = to_float(cell(row, i_trust))

        if i_dealer_total is not None:
            dealer = to_float(cell(row, i_dealer_total))
        else:
            dealer = to_float(cell(row, i_dealer_self)) + to_float(cell(row, i_dealer_hedge))

        total = (
            to_float(cell(row, i_total))
            if i_total is not None
            else foreign + trust + dealer
        )

        out.append(
            {
                "code": code,
                "name": str(cell(row, i_name, "")).strip(),
                "market": "TWSE",
                "foreign_net": foreign,
                "trust_net": trust,
                "dealer_net": dealer,
                "total_net": total,
            }
        )

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何普通股資料（可能是非交易日）", observed=len(rows))
    return out


def fetch(fetcher: Fetcher, day: dt.date) -> list[dict]:
    resp = fetcher.get(
        URL,
        params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
        headers={"Referer": "https://www.twse.com.tw/zh/trading/foreign/t86.html"},
    )
    rows = parse(resp.text)
    for r in rows:
        r["trade_date"] = day.isoformat()
    return rows
