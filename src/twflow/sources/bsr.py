"""券商分點進出（選配）與官股動向.

## 為什麼是「選配」

證交所的分點資料在 ``https://bsr.twse.com.tw/bshtm/`` 需要通過**圖形驗證碼**，
而且只有盤後資料。自動破解驗證碼既不穩定也不妥當，所以本模組採手動匯入：

1. 使用者到 BSR 網站查詢個股、下載 CSV
2. 把檔案放進 ``data/bsr/``（檔名格式 ``YYYY-MM-DD_2330.csv``）
3. ``twflow eod`` 會自動讀取並存進資料庫

任何一步失敗都只會跳過分點資料，**不影響盤中推估與三大法人主流程**。

## 官股動向

「官股」指公股行庫體系的券商分點。這裡以**券商名稱**比對而非代號，
因為名稱較穩定且可讀；清單可在 ``sectors.yaml`` 的 ``state_brokers``
覆寫，因為不同資料來源對「官股」的認定範圍略有差異。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from pathlib import Path

from ..errors import ParseError
from .twse_common import normalize_code, to_float

SOURCE = "bsr"

# 公股行庫體系券商。以名稱關鍵字比對，可由設定覆寫。
DEFAULT_STATE_BROKERS = (
    "合作金庫", "合庫", "土地銀行", "土銀", "臺灣銀行", "台灣銀行", "臺銀", "台銀",
    "華南永昌", "永昌", "第一金", "一銀", "兆豐", "彰銀", "彰化銀行",
)

FILENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[_-](\d{4})")


def is_state_broker(name: str, keywords=DEFAULT_STATE_BROKERS) -> bool:
    flat = str(name or "").replace(" ", "")
    return any(k in flat for k in keywords)


def _split_broker(cell: str) -> tuple[str, str]:
    """把 ``"1020 合庫"`` 拆成 (代號, 名稱)；沒有代號時代號留空."""
    s = str(cell or "").strip()
    if not s:
        return "", ""
    parts = s.split(None, 1)
    if len(parts) == 2 and re.fullmatch(r"[0-9A-Za-z]{3,5}", parts[0]):
        return parts[0], parts[1].strip()
    return "", s


def parse(text: str, code: str) -> list[dict]:
    """解析 BSR 分點 CSV.

    BSR 的下載檔把資料橫向排成兩組（每組是 序號/券商/價格/買進股數/賣出股數），
    而且前面有數行說明。這裡先找出含有「券商」的標題列，再依該列推出每組的
    欄位位移，因此不論一列放幾組都能處理。
    """
    text = text.lstrip("﻿")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(str(c).strip() for c in r)]
    if not rows:
        raise ParseError(SOURCE, "CSV 是空的", observed=0)

    header_idx = None
    for idx, row in enumerate(rows):
        if any("券商" in str(c) for c in row):
            header_idx = idx
            break
    if header_idx is None:
        raise ParseError(SOURCE, "找不到含「券商」的標題列", observed=rows[0][:10])

    header = [str(c).strip() for c in rows[header_idx]]
    # 每組的起點：出現「券商」的欄位索引
    group_starts = [i for i, c in enumerate(header) if "券商" in c]
    if not group_starts:
        raise ParseError(SOURCE, "標題列裡沒有券商欄", observed=header)

    def offset(start: int, *keywords: str) -> int | None:
        stop = len(header)
        for nxt in group_starts:
            if nxt > start:
                stop = nxt
                break
        for i in range(start, stop):
            flat = header[i].replace(" ", "")
            if all(k in flat for k in keywords):
                return i
        return None

    layout = []
    for start in group_starts:
        layout.append(
            {
                "broker": start,
                "buy": offset(start, "買進"),
                "sell": offset(start, "賣出"),
            }
        )

    agg: dict[str, dict] = {}
    for row in rows[header_idx + 1 :]:
        for group in layout:
            b_idx, buy_idx, sell_idx = group["broker"], group["buy"], group["sell"]
            if b_idx >= len(row):
                continue
            broker_id, broker_name = _split_broker(row[b_idx])
            if not broker_name:
                continue
            buy = to_float(row[buy_idx]) if buy_idx is not None and buy_idx < len(row) else 0.0
            sell = to_float(row[sell_idx]) if sell_idx is not None and sell_idx < len(row) else 0.0
            if buy == 0 and sell == 0:
                continue
            key = broker_id or broker_name
            rec = agg.setdefault(
                key,
                {
                    "code": code,
                    "broker_id": key,
                    "broker_name": broker_name,
                    "buy_shares": 0.0,
                    "sell_shares": 0.0,
                },
            )
            # BSR 是「分價量表」，同一分點會有多列不同價位，必須累加
            rec["buy_shares"] += buy
            rec["sell_shares"] += sell

    out = []
    for rec in agg.values():
        rec["net_shares"] = rec["buy_shares"] - rec["sell_shares"]
        out.append(rec)

    if not out:
        raise ParseError(SOURCE, "解析後沒有任何分點資料", observed=len(rows))
    return out


def load_directory(directory: str | Path = "data/bsr") -> list[dict]:
    """讀取 ``data/bsr/`` 底下所有分點 CSV.

    檔名需含日期與股票代號，例如 ``2026-08-27_2330.csv``。
    單一檔案解析失敗只會跳過該檔並回報，不會中斷整批匯入。
    """
    d = Path(directory)
    if not d.exists():
        return []

    out: list[dict] = []
    for path in sorted(d.glob("*.csv")):
        m = FILENAME_RE.search(path.name)
        if not m:
            continue
        day, code = m.group(1), normalize_code(m.group(2))
        try:
            rows = parse(path.read_text("utf-8", errors="replace"), code)
        except (ParseError, OSError):
            # 壞掉的單一檔案不該讓整批匯入失敗
            continue
        for r in rows:
            r["trade_date"] = day
        out.extend(rows)
    return out


def state_broker_summary(rows: list[dict], keywords=DEFAULT_STATE_BROKERS) -> list[dict]:
    """把分點資料彙總成每檔的官股買賣超（單位：股）."""
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        if not is_state_broker(r.get("broker_name", ""), keywords):
            continue
        key = (r["trade_date"], r["code"])
        rec = agg.setdefault(
            key,
            {"trade_date": key[0], "code": key[1], "state_net_shares": 0.0, "brokers": 0},
        )
        rec["state_net_shares"] += r.get("net_shares", 0.0)
        rec["brokers"] += 1
    return sorted(agg.values(), key=lambda r: -abs(r["state_net_shares"]))
