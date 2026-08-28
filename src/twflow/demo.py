"""產生合成示範資料.

**這些不是真實市場資料。** 它的用途只有兩個：

1. 讓開發環境（連不到台股資料源）能端到端驗證整條管線
2. 讓使用者在盤後或假日也能看到儀表板長什麼樣子

為了讓四象限圖有東西可看，這裡刻意讓不同板塊走不同的資金流形狀——
四種象限各有板塊落在上面。盤後的「官方」買賣超則是由推估值加上雜訊
生成的，因此校準統計會得出一個合理但明顯非 1.0 的相關係數，正好示範
準確度指標在真實情況下應該長什麼樣。
"""

from __future__ import annotations

import datetime as dt
import math
import random
from pathlib import Path

import yaml

from .store import Store
from .tradingcal import TAIPEI

SESSION_MINUTES = 270          # 09:00–13:30
STEP_MINUTES = 5               # 示範資料用 5 分鐘顆粒度，產生得快一些

# 四種資金流形狀，對應四個象限
SHAPES = ("accel_in", "slowing_in", "accel_out", "slowing_out")


def _shape_value(shape: str, progress: float) -> float:
    """回傳 [-1, 1] 區間的資金流率；``progress`` 是 0→1 的盤中進度."""
    ramp = progress
    decay = 1.0 - progress
    if shape == "accel_in":
        return 0.2 + 0.8 * ramp
    if shape == "slowing_in":
        return 0.2 + 0.8 * decay
    if shape == "accel_out":
        return -(0.2 + 0.8 * ramp)
    return -(0.2 + 0.8 * decay)


def _load_universe(sectors_file: str | Path = "sectors.yaml") -> dict[str, list[str]]:
    p = Path(sectors_file)
    if not p.exists():
        return {"示範板塊": [f"{9000 + i}" for i in range(6)]}
    data = yaml.safe_load(p.read_text("utf-8")) or {}
    return {str(k): [str(c) for c in (v or [])] for k, v in (data.get("sectors") or {}).items()}


def generate(store: Store, *, days: int = 3, seed: int = 20260827) -> list[str]:
    """產生 ``days`` 個交易日的合成資料並寫入資料庫."""
    rng = random.Random(seed)
    universe = _load_universe()
    summary: list[str] = []

    # 證券清單：板塊名稱同時當作官方產業別，讓分類鏈完整
    securities = []
    prices: dict[str, float] = {}
    for sector, codes in universe.items():
        for code in codes:
            securities.append(
                {
                    "code": code,
                    "name": f"示範{code}",
                    "market": "TWSE",
                    "industry": sector,
                    "sector": sector,
                    "sector_src": "custom",
                }
            )
            prices[code] = round(rng.uniform(20, 900), 1)
    store.upsert_securities(securities)
    summary.append(f"證券清單 {len(securities)} 檔 / {len(universe)} 個板塊")

    today = dt.datetime.now(TAIPEI).date()
    flow_rows: list[dict] = []
    insti_rows: list[dict] = []
    holding_rows: list[dict] = []
    futures_rows: list[dict] = []

    for day_offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=day_offset)
        # 跳過週末，讓日期看起來像真的交易日
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        trade_date = day.isoformat()
        open_dt = dt.datetime.combine(day, dt.time(9, 0), tzinfo=TAIPEI)

        # 每個板塊每天抽一種形狀，並給一個強度尺度
        sector_shape = {s: SHAPES[(i + day_offset) % len(SHAPES)] for i, s in enumerate(universe)}
        sector_scale = {s: rng.uniform(0.05, 0.30) for s in universe}

        est_by_code: dict[str, float] = {}

        for sector, codes in universe.items():
            shape = sector_shape[sector]
            scale = sector_scale[sector]
            for code in codes:
                px = prices[code]
                # 每檔的活躍度不同，成交值差異拉開才像真的市場
                activity = rng.uniform(0.3, 3.0)
                for step in range(0, SESSION_MINUTES, STEP_MINUTES):
                    progress = step / SESSION_MINUTES
                    ts = open_dt + dt.timedelta(minutes=step)

                    turnover = activity * rng.uniform(2e6, 1.2e7)
                    rate = _shape_value(shape, progress) * scale
                    # 加上個股層級的雜訊，否則同板塊的股票會完全同步
                    rate += rng.gauss(0, 0.08)
                    net = turnover * rate

                    burst = net if turnover > 8e6 else 0.0
                    lots = turnover / (px * 1000)

                    flow_rows.append(
                        {
                            "trade_date": trade_date,
                            "code": code,
                            "minute_ts": ts.replace(tzinfo=None).isoformat(),
                            "buy_lots": lots * (0.5 + rate / 2),
                            "sell_lots": lots * (0.5 - rate / 2),
                            "net_value": net,
                            "burst_buy_lots": 0.0,
                            "burst_sell_lots": 0.0,
                            "burst_net_value": burst,
                            "turnover_value": turnover,
                            "last_price": round(px * (1 + math.sin(progress * 3) * 0.01), 2),
                        }
                    )
                    est_by_code[code] = est_by_code.get(code, 0.0) + net

        # 「官方」三大法人：由推估值加雜訊生成，相關但不完全一致。
        #
        # 雜訊刻意用**全體的典型規模**而非每檔自己的規模——若雜訊與個股金額
        # 成比例，排序會被保留，Spearman 會漂亮到 0.9 以上，反而讓人對真實
        # 推估的準度產生錯誤期待。實務上盤中推估與官方買賣超的等級相關大約
        # 落在 0.3–0.6，這裡的參數就是朝那個區間校的。
        typical = (
            sum(abs(v) for v in est_by_code.values()) / len(est_by_code)
            if est_by_code else 1.0
        )
        for code, est in est_by_code.items():
            px = prices[code]
            real_value = est * rng.uniform(0.2, 1.8) + rng.gauss(0, typical * 1.1)
            total_shares = real_value / px
            foreign = total_shares * rng.uniform(0.5, 0.8)
            trust = total_shares * rng.uniform(0.05, 0.3)
            insti_rows.append(
                {
                    "trade_date": trade_date,
                    "code": code,
                    "market": "TWSE",
                    "foreign_net": round(foreign),
                    "trust_net": round(trust),
                    "dealer_net": round(total_shares - foreign - trust),
                    "total_net": round(total_shares),
                }
            )
            holding_rows.append(
                {
                    "trade_date": trade_date,
                    "code": code,
                    "foreign_ratio": round(rng.uniform(5, 80), 2),
                    "issued_shares": 0.0,
                    "foreign_shares": 0.0,
                }
            )

        for party in ("外資", "投信", "自營商"):
            long_oi = rng.uniform(1000, 40000)
            short_oi = rng.uniform(1000, 40000)
            futures_rows.append(
                {
                    "trade_date": trade_date,
                    "contract": "臺股期貨",
                    "party": party,
                    "long_oi": round(long_oi),
                    "short_oi": round(short_oi),
                    "net_oi": round(long_oi - short_oi),
                    "net_value": round((long_oi - short_oi) * 200 * 1000),
                }
            )

    store.add_flow_minute(flow_rows)
    store.upsert_insti_daily(insti_rows)
    store.upsert_foreign_holding(holding_rows)
    store.upsert_futures_oi(futures_rows)

    summary.append(f"盤中資金流 {len(flow_rows):,} 筆（{days} 個交易日 × {STEP_MINUTES} 分鐘顆粒度）")
    summary.append(f"官方三大法人 {len(insti_rows):,} 筆")
    summary.append(f"期貨未平倉 {len(futures_rows)} 筆")
    return summary
