"""盤中資金流推估：內外盤主動買賣分類.

## 這在推估什麼，以及它的極限

台股**沒有**公開的盤中法人買賣超資料。證交所的三大法人逐檔買賣超（T86）
要收盤後才發布。盤中真正拿得到的只有成交價、累積成交量與五檔委買委賣。

所以這裡做的是：把兩次輪詢之間新增的成交量，依成交價相對於**前一刻**最佳
買賣價的位置，分類成「主動買」（外盤成交）或「主動賣」（內盤成交），
再乘上成交價得到淨資金流。這是 Lee-Ready 演算法的快照版變體。

必須誠實說明的三個近似：

1. **快照不是逐筆。** MIS 每 5 秒才更新一次，一個區間內可能有數十筆成交，
   我們只看得到期末的成交價與累積量，因此是用單一價格代表整個區間。
2. **主動買 ≠ 法人買。** 主動買方可能是散戶、當沖客、程式單。資金流只是
   「誰比較急著成交」的代理指標，不是法人身分的辨識。
3. **爆量 ≠ 大單。** 快照無法辨識個別委託大小，``burst_*`` 欄位代表的是
   「該區間成交量超過門檻」，只能當作大額進出的粗略代理。

這些近似的實際偏差有多大，由 :mod:`twflow.calibrate` 用盤後真實 T86 數據
量化出來（等級相關係數），並顯示在儀表板上，讓使用者自己判斷可信度。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# 台股一張 = 1000 股
SHARES_PER_LOT = 1000


@dataclass
class Quote:
    """單一檔股票在某一時刻的快照."""

    code: str
    ts: dt.datetime
    price: float          # 最新成交價
    cum_volume: float     # 當日累積成交量（張）
    bid1: float = 0.0     # 最佳買價
    ask1: float = 0.0     # 最佳賣價

    @property
    def has_book(self) -> bool:
        """五檔是否可用。漲跌停鎖死或盤前時可能只有單邊或完全沒有."""
        return self.bid1 > 0 and self.ask1 > 0 and self.ask1 >= self.bid1


@dataclass
class FlowIncrement:
    """兩次快照之間的資金流增量."""

    code: str
    minute_ts: str
    buy_lots: float = 0.0
    sell_lots: float = 0.0
    net_value: float = 0.0
    burst_buy_lots: float = 0.0
    burst_sell_lots: float = 0.0
    burst_net_value: float = 0.0
    turnover_value: float = 0.0
    last_price: float = 0.0
    # 分類依據，供除錯與診斷用
    method: str = "book"          # book | tick | midpoint
    buy_ratio: float = 0.0


def _minute_key(ts: dt.datetime) -> str:
    return ts.replace(second=0, microsecond=0).isoformat()


def buy_ratio_from_book(price: float, bid1: float, ask1: float, rule: str = "proportional") -> float:
    """成交價落在買賣價之間時，判斷多少比例算主動買.

    * ``price >= ask1`` → 全部視為外盤成交（主動買）
    * ``price <= bid1`` → 全部視為內盤成交（主動賣）
    * 之間 → ``proportional`` 按線性比例分配；``midpoint_neutral`` 一律五五分
    """
    if ask1 <= bid1:
        return 0.5
    if price >= ask1:
        return 1.0
    if price <= bid1:
        return 0.0
    if rule == "midpoint_neutral":
        return 0.5
    return (price - bid1) / (ask1 - bid1)


def classify(
    prev: Quote,
    cur: Quote,
    *,
    burst_threshold_lots: float = 100.0,
    midpoint_rule: str = "proportional",
    prev_direction: float | None = None,
) -> FlowIncrement | None:
    """把兩次快照之間的成交量增量分類成主動買 / 主動賣.

    Parameters
    ----------
    prev, cur:
        前後兩次快照。分類用 ``prev`` 的五檔，因為那是這些成交發生**之前**
        掛在盤上的價位。
    prev_direction:
        上一次的買方比例，供 zero-tick 情境（價格沒變且無五檔）沿用方向。

    Returns
    -------
    ``None`` 表示這個區間沒有成交（或資料無效），呼叫端應略過。
    """
    dvol = cur.cum_volume - prev.cum_volume

    # 沒有新成交、累積量倒退（換日或資料異常）、無效價格 → 不產生增量
    if dvol <= 0 or cur.price <= 0:
        return None

    price = cur.price

    if prev.has_book:
        ratio = buy_ratio_from_book(price, prev.bid1, prev.ask1, midpoint_rule)
        method = "book"
    elif prev.price > 0:
        # 五檔不可用時退回經典 tick rule：比前一筆成交價高算主動買、低算主動賣，
        # 持平則沿用上一次的方向（zero tick）。
        if price > prev.price:
            ratio, method = 1.0, "tick"
        elif price < prev.price:
            ratio, method = 0.0, "tick"
        else:
            ratio = 0.5 if prev_direction is None else prev_direction
            method = "tick"
    else:
        ratio, method = 0.5, "midpoint"

    buy_lots = dvol * ratio
    sell_lots = dvol * (1.0 - ratio)
    net_value = (buy_lots - sell_lots) * price * SHARES_PER_LOT
    turnover_value = dvol * price * SHARES_PER_LOT

    inc = FlowIncrement(
        code=cur.code,
        minute_ts=_minute_key(cur.ts),
        buy_lots=buy_lots,
        sell_lots=sell_lots,
        net_value=net_value,
        turnover_value=turnover_value,
        last_price=price,
        method=method,
        buy_ratio=ratio,
    )

    # 爆量區間：整段增量都計入 burst 統計。
    if dvol >= burst_threshold_lots:
        inc.burst_buy_lots = buy_lots
        inc.burst_sell_lots = sell_lots
        inc.burst_net_value = net_value

    return inc


@dataclass
class FlowTracker:
    """維護每檔股票的前一次快照，把連續的快照串流轉成資金流增量.

    盤中輪詢器持有一個 tracker，每輪把新快照餵進 :meth:`update`，
    拿到可以直接寫進資料庫的增量清單。
    """

    burst_threshold_lots: float = 100.0
    midpoint_rule: str = "proportional"
    _prev: dict[str, Quote] = field(default_factory=dict)
    _direction: dict[str, float] = field(default_factory=dict)

    def seed(self, quotes: list[Quote]) -> None:
        """設定初始狀態而不產生增量（程式啟動或重啟時用）."""
        for q in quotes:
            self._prev[q.code] = q

    def update(self, quotes: list[Quote]) -> list[FlowIncrement]:
        out: list[FlowIncrement] = []
        for q in quotes:
            prev = self._prev.get(q.code)
            self._prev[q.code] = q
            if prev is None:
                # 第一次看到這檔，只記狀態。累積量的基準是「開盤到現在」，
                # 若從盤中才啟動，這段成交量無從分類，只能捨棄。
                continue
            inc = classify(
                prev,
                q,
                burst_threshold_lots=self.burst_threshold_lots,
                midpoint_rule=self.midpoint_rule,
                prev_direction=self._direction.get(q.code),
            )
            if inc is not None:
                self._direction[q.code] = inc.buy_ratio
                out.append(inc)
        return out

    def state(self, trade_date: str) -> list[dict]:
        """匯出目前狀態，供寫入 ``quote_state`` 表以便重啟後接續."""
        return [
            {
                "code": q.code,
                "trade_date": trade_date,
                "ts": q.ts.isoformat(),
                "price": q.price,
                "cum_volume": q.cum_volume,
                "bid1": q.bid1,
                "ask1": q.ask1,
            }
            for q in self._prev.values()
        ]
