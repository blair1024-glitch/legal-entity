"""盤後校準：用官方三大法人數據檢驗盤中推估到底準不準.

這個模組是整個專案誠實性的關鍵。盤中的資金流是推估值，如果沒有任何
檢驗，它就只是個好看但無從判斷可信度的數字。這裡做兩件事：

1. **量化準確度**（給使用者看）
   把當日各檔的推估淨流與官方三大法人買賣超做**橫斷面等級相關**
   （Spearman）。用等級相關而非絕對誤差，因為儀表板的用途是「看誰在
   流入、誰在流出」的相對排序，而不是預測確切金額。另外算「方向一致
   比例」——推估買超的個股裡，實際也是買超的佔幾成。

2. **修正系統性偏差**（給程式用）
   對每檔做過原點的線性迴歸求出 scale 係數，隔日盤中套用。有些個股
   （例如法人佔比低、當沖比重高的）推估會系統性高估，這個係數把它壓回來。

單位處理：推估值是**元**，官方買賣超是**股**，兩者要先換算到同一個尺度
才能比較——官方股數乘上當日收盤價得到金額。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .store import Store


def _ranks(values: list[float]) -> list[float]:
    """回傳平均排名（同分取平均，這是 Spearman 處理 ties 的標準做法）."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return 0.0
    return num / (dx * dy) ** 0.5


def spearman(xs: list[float], ys: list[float]) -> float:
    """等級相關係數：對離群值穩健，衡量的是排序一致性."""
    if len(xs) < 2:
        return 0.0
    return pearson(_ranks(xs), _ranks(ys))


def sign_agreement(xs: list[float], ys: list[float]) -> float:
    """方向一致比例：推估與實際同為買超或同為賣超的佔比.

    兩邊都恰好為 0 的個股不列入分母——那是「沒有交易」而非「猜對方向」。
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x != 0 or y != 0]
    if not pairs:
        return 0.0
    hits = sum(1 for x, y in pairs if (x > 0) == (y > 0))
    return hits / len(pairs)


def regression_coef(est: list[float], real: list[float]) -> tuple[float, float]:
    """過原點的最小平方迴歸 ``real ≈ coef × est``，回傳 (coef, r²).

    過原點是刻意的：推估為零時實際也應該接近零，沒有理由留一個截距項。
    """
    denom = sum(e * e for e in est)
    if denom <= 0:
        return 1.0, 0.0
    coef = sum(e * r for e, r in zip(est, real)) / denom

    ss_res = sum((r - coef * e) ** 2 for e, r in zip(est, real))
    ss_tot = sum(r * r for r in real)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return coef, r2


@dataclass
class DailyAccuracy:
    trade_date: str
    spearman: float
    sign_match: float
    n_stocks: int

    def to_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "spearman": round(self.spearman, 4),
            "sign_match": round(self.sign_match, 4),
            "n_stocks": self.n_stocks,
        }


def build_daily_pairs(store: Store, trade_date: str) -> list[dict]:
    """把當日的推估淨流與官方買賣超配對.

    官方買賣超是股數，乘上當日最後成交價換算成金額，才能和推估的金額比較。
    """
    flow = store.flow_rows(trade_date)
    if not flow:
        return []

    est: dict[str, float] = {}
    price: dict[str, float] = {}
    for row in flow:
        code = row["code"]
        est[code] = est.get(code, 0.0) + row["net_value"]
        if row["last_price"]:
            price[code] = row["last_price"]

    pairs = []
    for row in store.insti_daily(trade_date):
        code = row["code"]
        if code not in est:
            # 盤中沒掃到這檔（不在 universe 內，或整天沒成交）
            continue
        px = price.get(code, 0.0)
        if px <= 0:
            continue
        pairs.append(
            {
                "code": code,
                "est_net": est[code],
                "real_net": row["total_net"] * px,   # 股 × 元/股 = 元
            }
        )
    return pairs


def calibrate_day(store: Store, day: dt.date | str) -> DailyAccuracy | None:
    """計算並儲存單日的推估準確度與配對資料."""
    trade_date = day.isoformat() if isinstance(day, dt.date) else str(day)
    pairs = build_daily_pairs(store, trade_date)
    if len(pairs) < 2:
        return None

    store.upsert_calibration(
        [
            {
                "trade_date": trade_date,
                "scope": "stock",
                "key": p["code"],
                "est_net": p["est_net"],
                "real_net": p["real_net"],
            }
            for p in pairs
        ]
    )

    est = [p["est_net"] for p in pairs]
    real = [p["real_net"] for p in pairs]
    acc = DailyAccuracy(
        trade_date=trade_date,
        spearman=spearman(est, real),
        sign_match=sign_agreement(est, real),
        n_stocks=len(pairs),
    )
    store.upsert_accuracy(acc.trade_date, acc.spearman, acc.sign_match, acc.n_stocks)
    return acc


def update_coefficients(
    store: Store,
    *,
    lookback_days: int = 60,
    min_samples: int = 5,
    clamp: tuple[float, float] = (0.2, 5.0),
) -> int:
    """依歷史配對資料更新每檔的校準係數.

    ``clamp`` 把係數限制在合理範圍內：樣本少的時候迴歸容易給出極端值，
    讓某一檔在儀表板上暴衝，反而比不校準更糟。
    """
    history = store.calibration_history(scope="stock", limit_days=lookback_days)
    grouped: dict[str, list[tuple[float, float]]] = {}
    for row in history:
        grouped.setdefault(row["key"], []).append((row["est_net"], row["real_net"]))

    updates = []
    lo, hi = clamp
    for code, samples in grouped.items():
        if len(samples) < min_samples:
            continue
        est = [s[0] for s in samples]
        real = [s[1] for s in samples]
        coef, r2 = regression_coef(est, real)
        if coef <= 0:
            # 係數為負代表推估方向和實際長期相反，硬套只會讓圖更錯。
            # 這種情況維持 1.0（不修正），並靠 r² 讓使用者看到它不可靠。
            coef = 1.0
        coef = max(lo, min(hi, coef))
        updates.append({"code": code, "coef": coef, "samples": len(samples), "r2": r2})

    return store.upsert_calibration_coef(updates) if updates else 0


def accuracy_summary(store: Store, days: int = 20) -> dict:
    """儀表板顯示用的準確度摘要."""
    rows = store.recent_accuracy(days)
    if not rows:
        return {"available": False, "days": 0}
    sp = [r["spearman"] for r in rows]
    sm = [r["sign_match"] for r in rows]
    return {
        "available": True,
        "days": len(rows),
        "latest": DailyAccuracy(
            rows[0]["trade_date"], rows[0]["spearman"], rows[0]["sign_match"], rows[0]["n_stocks"]
        ).to_dict(),
        "mean_spearman": round(sum(sp) / len(sp), 4),
        "mean_sign_match": round(sum(sm) / len(sm), 4),
        "history": [
            DailyAccuracy(r["trade_date"], r["spearman"], r["sign_match"], r["n_stocks"]).to_dict()
            for r in reversed(rows)
        ],
    }
