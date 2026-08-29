"""證交所 / 櫃買 ``{fields, data}`` 風格回應的共用解析工具.

證交所多數 JSON 端點長這樣::

    {"stat": "OK", "date": "20260827",
     "fields": ["證券代號", "證券名稱", "外陸資買賣超股數", ...],
     "data": [["1101", "台泥", "1,234,000", ...], ...]}

欄位順序與名稱都可能隨改版變動，所以這裡提供**依標題比對**的欄位定位，
並在找不到必要欄位時丟出帶有實際標題清單的 :class:`ParseError`，
讓 ``twflow doctor`` 能印出可讀的診斷。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..errors import ParseError

# 千分位、全形空白、括號註記等雜訊
_NUM_CLEAN = re.compile(r"[,\s　　]")


def to_float(value: Any, default: float = 0.0) -> float:
    """把證交所回應裡的數字字串轉成 float.

    需要處理：千分位逗號、``"-"``（無資料）、``"--"``、空字串、全形空白，
    以及偶爾出現的括號負數 ``(1,234)``。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = _NUM_CLEAN.sub("", str(value))
    if not s or s in {"-", "--", "---", "N/A"}:
        return default
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("+"):
        s = s[1:]
    try:
        out = float(s)
    except ValueError:
        return default
    return -out if neg else out


def load_json(text: str, source: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:200].replace("\n", " ")
        raise ParseError(source, f"回應不是合法 JSON: {exc}", observed=preview) from exc
    if not isinstance(payload, dict):
        raise ParseError(source, f"預期 JSON object，實際得到 {type(payload).__name__}", payload)
    return payload


def extract_table(payload: dict, source: str) -> tuple[list[str], list[list]]:
    """從證交所回應取出 (欄位標題, 資料列).

    證交所不同端點會把表格放在 ``data`` / ``aaData`` / ``data1`` 等不同鍵底下，
    標題則在 ``fields`` / ``fields1``。這裡一併涵蓋。
    """
    stat = payload.get("stat")
    if isinstance(stat, str) and stat.upper() not in {"OK", ""}:
        # 非交易日或日期太舊時證交所會用 stat 說明原因，這不是程式錯誤。
        raise ParseError(source, f"證交所回報: {stat}", observed=stat)

    fields: list[str] = []
    for key in ("fields", "fields1", "fields0"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            fields = [str(f).strip() for f in val]
            break

    rows: list[list] = []
    for key in ("data", "aaData", "data1", "data0"):
        val = payload.get(key)
        if isinstance(val, list):
            rows = [r for r in val if isinstance(r, list)]
            if rows:
                break

    if not fields:
        raise ParseError(
            source, "找不到欄位標題（fields/fields1）", observed=sorted(payload.keys())
        )
    return fields, rows


def split_header(name: str) -> tuple[str, str]:
    """把欄位標題拆成「主體」與「括號註記」.

    證交所用括號註記來區分同一類法人的細項，例如::

        外陸資買賣超股數(不含外資自營商)  ->  ("外陸資買賣超股數", "不含外資自營商")
        自營商買賣超股數                  ->  ("自營商買賣超股數", "")
        自營商買賣超股數(自行買賣)        ->  ("自營商買賣超股數", "自行買賣")

    這個拆分很關鍵：「外陸資」那欄的註記裡也含有「自營商」三個字，若只用
    整串標題做子字串比對，會誤把它當成外資自營商欄而漏算外資買賣超。
    """
    flat = _NUM_CLEAN.sub("", str(name))
    m = re.search(r"[（(]([^）)]*)[）)]", flat)
    if not m:
        return flat, ""
    base = (flat[: m.start()] + flat[m.end():]).strip()
    return base, m.group(1).strip()


def find_column(
    fields: list[str],
    predicate,
    *,
    source: str = "",
    label: str = "",
    required: bool = True,
) -> int | None:
    """依 ``predicate(base, qualifier)`` 定位欄位索引.

    用 predicate 而非關鍵字清單，是因為證交所的欄位語意需要同時看主體與
    括號註記才能區分（見 :func:`split_header`）。找不到必要欄位時丟出帶有
    實際標題清單的 :class:`ParseError`，讓 ``twflow doctor`` 印得出診斷。
    """
    for idx, name in enumerate(fields):
        base, qual = split_header(name)
        if predicate(base, qual):
            return idx
    if required:
        raise ParseError(source, f"找不到欄位: {label or predicate}", observed=fields)
    return None


def cell(row: list, idx: int | None, default: Any = "") -> Any:
    """安全取值：欄位不存在或該列比標題短時回傳預設值."""
    if idx is None or idx < 0 or idx >= len(row):
        return default
    return row[idx]


def is_common_stock(code: str) -> bool:
    """判斷是否為普通股（相對於 ETF、ETN、權證、受益證券）.

    台股上市櫃普通股代號一律是 4 碼數字且首碼為 1–9（台泥 1101、台積電 2330）。
    ETF 則一律以 0 開頭：0050、0056、00878、00631L、00407A…

    為什麼要排除 ETF：本工具是看「板塊資金流向」，而 ETF 沒有產業歸屬，
    收下來只會全部堆進「未分類」。0050 這種權值 ETF 的法人買賣超動輒
    數千萬股，足以讓「未分類」變成圖上最大的泡泡——那是個沒有意義、
    卻又看起來很重要的板塊。
    """
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


def normalize_code(value: Any) -> str:
    """清理證券代號.

    證交所有時會回傳帶空白或全形字元的代號；權證、ETN 等非普通股的代號
    長度不是 4 碼，呼叫端可自行過濾。
    """
    return _NUM_CLEAN.sub("", str(value or "")).strip()
