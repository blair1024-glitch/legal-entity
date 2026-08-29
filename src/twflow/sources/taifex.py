"""期交所三大法人未平倉（台指期等）.

端點：``https://www.taifex.com.tw/cht/3/futContractsDateDown``（POST，回 CSV）

用 CSV 下載端點而非網頁報表，因為 CSV 的欄位穩定得多、也不必解 HTML。
單位是**口**；契約金額欄位單位是**千元**，本模組換算成元後存入。

期貨法人未平倉是判斷法人整體多空方向最直接的公開資料——它不像現貨那樣
只能推估，是實打實的官方數字，只是同樣要收盤後才發布。
"""

from __future__ import annotations

import csv
import datetime as dt
import io

from ..errors import ParseError, TwflowError
from ..httpclient import Fetcher
from ..tradingcal import roc_to_date
from .twse_common import to_float

URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
SOURCE = "taifex"

# 預設只抓台指期與小台——這兩個最能代表法人對大盤的方向判斷。
#
# 契約代號實測以 TXF / MXF 為準（2026-08-27 驗證通過）。保留舊寫法
# TX / MTX 當備援，因為期交所不同端點的命名並不一致。
#
# 回應是 MS950（Big5）編碼，Python 認得這個名稱，requests 會依
# Content-Type 的 charset 正確解碼，不需要額外處理。
CONTRACT_CANDIDATES = {
    "臺股期貨": ("TXF", "TX"),
    "小型臺指期貨": ("MXF", "MTX"),
}
DEFAULT_CONTRACTS = tuple(CONTRACT_CANDIDATES)

PARTY_ALIASES = {
    "自營商": "自營商",
    "自營商(避險)": "自營商",
    "投信": "投信",
    "外資": "外資",
    "外資及陸資": "外資",
}


def _find(header: list[str], *keywords: str) -> int | None:
    """找出同時包含所有關鍵字的欄位索引."""
    for idx, name in enumerate(header):
        flat = str(name).replace(" ", "").replace("　", "")
        if all(k in flat for k in keywords):
            return idx
    return None


def parse(text: str) -> list[dict]:
    """解析期交所三大法人 CSV.

    欄位很多且名稱冗長，一律以關鍵字定位。重點在「未平倉」而非「交易」——
    未平倉才代表法人手上實際的部位方向。
    """
    text = text.lstrip("﻿").lstrip()

    # 期交所在參數不對、或改版之後，會回傳一個 HTML 網頁而不是 CSV。
    # 這時候說「CSV 缺少欄位」會把人引導到錯的方向——真正的問題是
    # 根本沒拿到 CSV。
    head = text[:400].lower()
    if head.startswith("<!doctype") or head.startswith("<html") or "<body" in head:
        raise ParseError(
            SOURCE,
            "伺服器回傳的是 HTML 網頁，不是 CSV——通常代表 POST 參數"
            "（commodityId 或日期欄位）與現行介面不符",
            observed=text[:300].replace("\n", " "),
        )

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ParseError(SOURCE, "CSV 沒有資料列（可能是非交易日）", observed=len(rows))

    header = rows[0]
    i_date = _find(header, "日期")
    i_name = _find(header, "商品名稱")
    i_party = _find(header, "身份別")
    i_long = _find(header, "多方未平倉口數")
    i_short = _find(header, "空方未平倉口數")
    i_net = _find(header, "多空未平倉口數淨額")
    i_net_val = _find(header, "多空未平倉契約金額淨額")

    missing = [
        label
        for label, idx in [
            ("日期", i_date), ("商品名稱", i_name), ("身份別", i_party),
            ("多方未平倉口數", i_long), ("空方未平倉口數", i_short),
        ]
        if idx is None
    ]
    if missing:
        raise ParseError(SOURCE, f"CSV 缺少必要欄位: {missing}", observed=header)

    out: list[dict] = []
    for row in rows[1:]:
        if max(i_date, i_name, i_party, i_long, i_short) >= len(row):
            continue
        party_raw = row[i_party].strip()
        party = PARTY_ALIASES.get(party_raw, party_raw)
        if not party:
            continue

        try:
            trade_date = roc_to_date(row[i_date])
        except ValueError:
            continue

        long_oi = to_float(row[i_long])
        short_oi = to_float(row[i_short])
        net_oi = to_float(row[i_net]) if i_net is not None and i_net < len(row) else long_oi - short_oi
        # 契約金額單位是千元
        net_value = (
            to_float(row[i_net_val]) * 1000.0
            if i_net_val is not None and i_net_val < len(row)
            else 0.0
        )

        out.append(
            {
                "trade_date": trade_date.isoformat(),
                "contract": row[i_name].strip(),
                "party": party,
                "long_oi": long_oi,
                "short_oi": short_oi,
                "net_oi": net_oi,
                "net_value": net_value,
            }
        )

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何法人未平倉資料", observed=len(rows))
    return out


def fetch(fetcher: Fetcher, day: dt.date, contracts=DEFAULT_CONTRACTS) -> list[dict]:
    stamp = day.strftime("%Y/%m/%d")
    headers = {"Referer": "https://www.taifex.com.tw/cht/3/futContractsDate"}
    out: list[dict] = []
    errors: list[str] = []

    for label in contracts:
        candidates = CONTRACT_CANDIDATES.get(label, (label,))
        got = False
        for code in candidates:
            try:
                # 只送這三個參數。實測（2026-08-27）多送 firstDate/lastDate
                # 會讓期交所改回一個 616 位元組的 HTML 錯誤頁而不是 CSV——
                # 多給參數反而被拒絕，是這個端點最反直覺的地方。
                resp = fetcher.get(
                    URL,
                    method="POST",
                    data={
                        "queryStartDate": stamp,
                        "queryEndDate": stamp,
                        "commodityId": code,
                    },
                    headers=headers,
                )
                rows = parse(resp.text)
            except TwflowError as exc:
                errors.append(f"{label}/{code}: {exc}")
                continue
            out.extend(rows)
            got = True
            break
        if not got:
            continue

    if not out:
        raise ParseError(
            SOURCE,
            "所有契約代號都拿不到 CSV",
            observed=" | ".join(errors)[:500],
        )
    return out
