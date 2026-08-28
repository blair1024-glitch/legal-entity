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


# 實測結果（2026-08-27）：
#     ALLBUT0999 → 1360 列   ← 全市場，正確
#     ALL        →    0 列   （這個端點不吃 ALL，和 T86 不一樣）
#     不帶參數    →    8 列   ← 危險：預設只回單一產業
#
# 「不帶參數」曾經在候選清單裡，那是個會安靜出錯的設計——它不會失敗，
# 只會回 8 檔水泥股，然後被當成全市場存進資料庫。寧可整個抓取失敗，
# 也不要拿一份看起來正常的殘缺資料去算外資持股比率。
SELECT_TYPES = ("ALLBUT0999", "ALL")

# 全市場查詢的合理下限。單一產業最多不到 100 檔（半導體 96），
# 低於這個數字就代表拿到的不是全市場，而是某個子集。
MIN_MARKET_ROWS = 100


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
            errors.append(f"selectType={select}: {exc}")
            continue

        # 拿到資料還不夠——要確認它真的是全市場，而不是某個產業的子集
        if len(rows) < MIN_MARKET_ROWS:
            errors.append(
                f"selectType={select}: 只有 {len(rows)} 列，不像全市場（疑似單一產業）"
            )
            continue

        for r in rows:
            r["trade_date"] = day.isoformat()
        return rows

    raise ParseError(
        SOURCE,
        "試過所有 selectType 取值都拿不到資料",
        observed=" | ".join(errors)[:500],
    )
