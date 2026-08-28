"""外資及陸資持股比率（MI_QFIIS）.

端點：``https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS``
時效：盤後。

自選股清單裡的「外資持股比率」就是這份資料。它和買賣超是互補的：
買賣超看的是「今天做了什麼」，持股比率看的是「累積下來站在哪個位置」。
"""

from __future__ import annotations

import datetime as dt

from ..errors import ParseError
from ..httpclient import Fetcher
from .twse_common import (
    cell,
    extract_table,
    find_column,
    load_json,
    normalize_code,
    to_float,
)

URL = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
SOURCE = "twse_qfiis"


def parse(text: str) -> list[dict]:
    payload = load_json(text, SOURCE)
    fields, rows = extract_table(payload, SOURCE)

    def col(pred, label, required=True):
        return find_column(fields, pred, source=SOURCE, label=label, required=required)

    i_code = col(lambda b, q: "證券代號" in b or "股票代號" in b, "證券代號")
    i_ratio = col(lambda b, q: "持股比率" in b or "持股比例" in b, "外資及陸資持股比率")
    i_issued = col(lambda b, q: "發行股數" in b, "發行股數", required=False)
    i_held = col(
        lambda b, q: "持有股數" in b and "外" in b, "全體外資及陸資持有股數", required=False
    )

    out: list[dict] = []
    for row in rows:
        code = normalize_code(cell(row, i_code))
        if len(code) != 4 or not code.isdigit():
            continue
        out.append(
            {
                "code": code,
                "foreign_ratio": to_float(cell(row, i_ratio)),
                "issued_shares": to_float(cell(row, i_issued)),
                "foreign_shares": to_float(cell(row, i_held)),
            }
        )

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何持股資料（可能是非交易日）", observed=len(rows))
    return out


def fetch(fetcher: Fetcher, day: dt.date) -> list[dict]:
    resp = fetcher.get(
        URL,
        params={"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
        headers={"Referer": "https://www.twse.com.tw/zh/trading/foreign/mi-qfiis.html"},
    )
    rows = parse(resp.text)
    for r in rows:
        r["trade_date"] = day.isoformat()
    return rows
