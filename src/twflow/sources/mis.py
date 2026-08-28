"""證交所 MIS 盤中即時報價.

端點：``https://mis.twse.com.tw/stock/api/getStockInfo.jsp``

參數 ``ex_ch`` 以 ``|`` 串接多檔，格式為 ``tse_2330.tw``（上市）或
``otc_6488.tw``（上櫃）。實測速率限制約 **3 requests / 5 秒**，
所以 :class:`~twflow.httpclient.Fetcher` 對這個 host 有專屬限流。

回應的欄位是單字母代號（證交所沒有官方文件，以下為社群長期觀察的共識）::

    c    證券代號              z   最近成交價
    n    公司簡稱              v   當日累積成交量（張）
    o    開盤價                y   昨收價
    h/l  最高/最低價           t   最近成交時刻
    a    揭示賣價（由低到高，底線分隔）
    b    揭示買價（由高到低，底線分隔）
    f/g  對應的賣量/買量
    tlong  epoch 毫秒

沒有成交時多數欄位會是 ``"-"``，所有解析都必須容忍這件事。
"""

from __future__ import annotations

import datetime as dt

from ..errors import ParseError
from ..flow import Quote
from ..httpclient import Fetcher
from ..tradingcal import TAIPEI
from .twse_common import load_json, normalize_code, to_float

URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
SOURCE = "mis"

MARKET_PREFIX = {"TWSE": "tse", "TPEX": "otc"}


def channel(code: str, market: str = "TWSE") -> str:
    prefix = MARKET_PREFIX.get(market.upper(), "tse")
    return f"{prefix}_{code}.tw"


def _first_level(packed: object) -> float:
    """取五檔字串的第一檔（最佳價/量）.

    格式是 ``"1001.0000_1002.0000_..."``，未揭示時是 ``"-"`` 或空字串。
    """
    if packed is None:
        return 0.0
    s = str(packed).strip()
    if not s or s == "-":
        return 0.0
    return to_float(s.split("_", 1)[0])


def _timestamp(entry: dict) -> dt.datetime:
    """從回應取出時間；``tlong`` 是 epoch 毫秒.

    抓不到就退回本機當下時間——時間戳只用來對齊分鐘桶，稍有偏差不影響
    資金流的正負判斷。
    """
    raw = entry.get("tlong")
    if raw:
        try:
            return dt.datetime.fromtimestamp(int(raw) / 1000, tz=TAIPEI)
        except (ValueError, TypeError, OSError):
            pass
    return dt.datetime.now(TAIPEI)


def parse(text: str) -> list[Quote]:
    """把 MIS 回應解析成 :class:`~twflow.flow.Quote` 清單.

    尚未成交的股票（``z`` 為 ``"-"``）會被跳過——沒有成交價就無法計算
    資金流，硬塞 0 進去只會污染統計。
    """
    payload = load_json(text, SOURCE)

    rtcode = str(payload.get("rtcode", "")).strip()
    if rtcode and rtcode != "0000":
        raise ParseError(
            SOURCE,
            f"MIS 回報錯誤 rtcode={rtcode}: {payload.get('rtmessage', '')}",
            observed=rtcode,
        )

    arr = payload.get("msgArray")
    if not isinstance(arr, list):
        raise ParseError(SOURCE, "回應缺少 msgArray", observed=sorted(payload.keys()))

    out: list[Quote] = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        code = normalize_code(entry.get("c"))
        if not code:
            continue
        price = to_float(entry.get("z"))
        if price <= 0:
            # 尚無成交（open 前、或整天沒交易的冷門股）
            continue
        out.append(
            Quote(
                code=code,
                ts=_timestamp(entry),
                price=price,
                cum_volume=to_float(entry.get("v")),
                bid1=_first_level(entry.get("b")),
                ask1=_first_level(entry.get("a")),
            )
        )
    return out


def parse_names(text: str) -> dict[str, str]:
    """順便從報價回應取出股票簡稱，省一次額外請求."""
    payload = load_json(text, SOURCE)
    arr = payload.get("msgArray") or []
    out = {}
    for entry in arr:
        if isinstance(entry, dict):
            code = normalize_code(entry.get("c"))
            name = str(entry.get("n") or "").strip()
            if code and name:
                out[code] = name
    return out


def fetch_batch(fetcher: Fetcher, codes: list[tuple[str, str]]) -> list[Quote]:
    """抓一批報價.

    Parameters
    ----------
    codes:
        ``[(code, market), ...]``，market 是 ``TWSE`` 或 ``TPEX``。
    """
    if not codes:
        return []
    ex_ch = "|".join(channel(c, m) for c, m in codes)
    resp = fetcher.get(
        URL,
        params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
        headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"},
    )
    return parse(resp.text)


def batched(items: list, size: int):
    """把清單切成固定大小的批次（MIS 單次請求不宜帶太多檔）."""
    for i in range(0, len(items), size):
        yield items[i : i + size]
