"""外資及陸資持股比率（MI_QFIIS）.

端點：``https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS``
時效：盤後。

自選股清單裡的「外資持股比率」就是這份資料。它和買賣超是互補的：
買賣超看的是「今天做了什麼」，持股比率看的是「累積下來站在哪個位置」。
"""

from __future__ import annotations

import datetime as dt

from ..errors import ParseError, TwflowError
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
        if not rows:
            raise ParseError(
                SOURCE,
                "回應裡沒有任何資料列——該日尚未發布，或 selectType 參數不適用",
                observed=f"fields={fields[:6]}",
            )
        raise ParseError(
            SOURCE,
            f"有 {len(rows)} 列資料，但沒有一列是 4 碼普通股代號",
            observed=rows[0][:4] if rows else None,
        )
    return out


# 這個端點的 selectType 取值無從查證（證交所沒有公開 API 文件），實測
# "ALL" 會回空表。與其押一個值，不如依序試候選值、用第一個真的有資料的。
# 這不是亂試——每個候選都是證交所其他端點實際在用的取值。
SELECT_TYPES = ("ALLBUT0999", "ALL", None)


def fetch(fetcher: Fetcher, day: dt.date) -> list[dict]:
    headers = {"Referer": "https://www.twse.com.tw/zh/trading/foreign/mi-qfiis.html"}
    errors: list[str] = []

    for select in SELECT_TYPES:
        params = {"date": day.strftime("%Y%m%d"), "response": "json"}
        if select is not None:
            params["selectType"] = select
        try:
            rows = parse(fetcher.get(URL, params=params, headers=headers).text)
        except TwflowError as exc:
            errors.append(f"selectType={select or '（不帶）'}: {exc}")
            continue
        for r in rows:
            r["trade_date"] = day.isoformat()
        return rows

    raise ParseError(
        SOURCE,
        "試過所有 selectType 取值都拿不到資料",
        observed=" | ".join(errors)[:500],
    )
