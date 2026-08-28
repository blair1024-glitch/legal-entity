"""四象限板塊輪動.

把每個板塊放在兩個座標軸上：

* **X 軸 — 強度**：當日累積淨流入佔該板塊成交值的比重。正值代表資金淨流入。
* **Y 軸 — 動能**：近 W 分鐘的強度減去前 W 分鐘的強度，也就是資金流的「加速度」。

於是四個象限對應四種狀態：

===========  ==========  ==========  ==================================
象限          強度        動能        意義
===========  ==========  ==========  ==================================
右上          > 0         > 0         加速流入 —— 資金持續且越來越積極買進
右下          > 0         < 0         流入但放緩 —— 還在買，但力道在減弱
左下          < 0         < 0         加速流出 —— 賣壓持續且越來越重
左上          < 0         > 0         流出但放緩 —— 還在賣，但賣壓在收斂
===========  ==========  ==========  ==================================

用「佔成交值比重」而不是絕對金額，是為了讓大小板塊可以放在同一張圖上比較；
否則半導體會永遠在圖的極端，其餘板塊全部擠成一團看不出差別。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .sectors import SectorMap

QUADRANT_ACCEL_IN = "加速流入"
QUADRANT_SLOWING_IN = "流入但放緩"
QUADRANT_ACCEL_OUT = "加速流出"
QUADRANT_SLOWING_OUT = "流出但放緩"
# 開盤最初幾分鐘資料太少，算不出有意義的加速度。與其硬塞一個象限，
# 不如誠實說「還不知道」。
QUADRANT_UNKNOWN = "動能待觀察"

QUADRANT_ORDER = [
    QUADRANT_ACCEL_IN,
    QUADRANT_SLOWING_IN,
    QUADRANT_ACCEL_OUT,
    QUADRANT_SLOWING_OUT,
    QUADRANT_UNKNOWN,
]

# 動能至少需要前後各這麼多分鐘的資料才算得出來
MIN_MOMENTUM_HALF_WINDOW = 2.0


def classify_quadrant(strength: float, momentum: float, momentum_known: bool = True) -> str:
    """依強度與動能的正負決定象限.

    邊界（強度或動能剛好為 0）歸類到「放緩」側：資金流剛好打平時，
    說它「在加速」比說它「在放緩」更容易誤導。

    ``momentum_known=False`` 時回傳 :data:`QUADRANT_UNKNOWN`——開盤最初幾分鐘
    還沒有足夠的歷史可以比較，這時把板塊塞進「加速流入」是在編造資訊。
    """
    if not momentum_known:
        return QUADRANT_UNKNOWN
    if strength > 0:
        return QUADRANT_ACCEL_IN if momentum > 0 else QUADRANT_SLOWING_IN
    return QUADRANT_SLOWING_OUT if momentum > 0 else QUADRANT_ACCEL_OUT


def resolve_momentum_window(
    earliest: dt.datetime,
    now: dt.datetime,
    window_minutes: float,
) -> tuple[float, bool]:
    """決定實際可用的動能視窗長度.

    動能是「近 W 分鐘的強度」減「前 W 分鐘的強度」，需要 2W 分鐘的歷史。
    開盤後還不到 2W 分鐘時有三種選擇，這裡採第三種：

    1. 照樣用 W —— 前段視窗是空的，動能會退化成強度本身，於是所有淨流入的
       板塊都被標成「加速流入」。這是**錯的**，而且錯得很有說服力。
    2. 直接不顯示 —— 開盤第一個小時整張圖空白，實用性太差。
    3. **把已經過去的時間對半切** —— 例如開盤 10 分鐘就比較「近 5 分鐘」與
       「前 5 分鐘」。這仍然是誠實的加速度，只是時間尺度較短，
       所以要把實際使用的視窗長度回報給呼叫端顯示出來。

    Returns
    -------
    ``(有效視窗分鐘數, 動能是否可信)``
    """
    elapsed = (now - earliest).total_seconds() / 60.0
    if elapsed >= 2 * window_minutes:
        return float(window_minutes), True
    half = elapsed / 2.0
    if half < MIN_MOMENTUM_HALF_WINDOW:
        return half, False
    return half, True


@dataclass
class SectorPoint:
    """四象限圖上的一個板塊."""

    sector: str
    strength: float = 0.0
    momentum: float = 0.0
    quadrant: str = QUADRANT_SLOWING_IN
    net_value: float = 0.0
    burst_net_value: float = 0.0
    turnover_value: float = 0.0
    constituents: int = 0
    custom: bool = False
    recent_strength: float = 0.0
    prior_strength: float = 0.0
    momentum_known: bool = True
    momentum_window_minutes: float = 0.0

    def to_dict(self) -> dict:
        return {
            "sector": self.sector,
            "strength": round(self.strength, 6),
            "momentum": round(self.momentum, 6),
            "quadrant": self.quadrant,
            "net_value": round(self.net_value, 2),
            "burst_net_value": round(self.burst_net_value, 2),
            "turnover_value": round(self.turnover_value, 2),
            "constituents": self.constituents,
            "custom": self.custom,
            "recent_strength": round(self.recent_strength, 6),
            "prior_strength": round(self.prior_strength, 6),
            "momentum_known": self.momentum_known,
            "momentum_window_minutes": round(self.momentum_window_minutes, 1),
        }


@dataclass
class _Bucket:
    net: float = 0.0
    turnover: float = 0.0

    @property
    def strength(self) -> float:
        return self.net / self.turnover if self.turnover > 0 else 0.0


@dataclass
class _Acc:
    total: _Bucket = field(default_factory=_Bucket)
    recent: _Bucket = field(default_factory=_Bucket)
    prior: _Bucket = field(default_factory=_Bucket)
    burst_net: float = 0.0
    codes: set = field(default_factory=set)
    custom: bool = False


def _parse_ts(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def compute_quadrants(
    flow_rows: list,
    sector_map: SectorMap,
    *,
    now: dt.datetime | None = None,
    window_minutes: int = 30,
    min_constituents: int = 2,
    min_turnover: float = 0.0,
    calibration: dict[str, float] | None = None,
) -> list[SectorPoint]:
    """把逐檔每分鐘資金流換算成四象限座標.

    Parameters
    ----------
    flow_rows:
        ``flow_minute`` 的列，需含 ``code``、``minute_ts``、``net_value``、
        ``turnover_value``、``burst_net_value``。
    now:
        視窗的基準時間；預設取資料中最後一筆的時間，這樣收盤後回看
        歷史某一天也能得到正確的視窗切分。
    min_constituents, min_turnover:
        過濾條件。成分股太少或成交值太小的板塊，單一檔的雜訊就足以
        讓它在圖上亂飛，因此排除。
    """
    calibration = calibration or {}

    rows: list[tuple[dt.datetime, object]] = []
    for row in flow_rows:
        get = row.get if isinstance(row, dict) else row.__getitem__
        ts = _parse_ts(get("minute_ts"))
        if ts is not None:
            rows.append((ts, row))

    if not rows:
        return []

    if now is None:
        now = max(ts for ts, _ in rows)

    # 開盤初期沒有足夠歷史可比，視窗要縮短（並回報縮短後的長度），
    # 否則動能會退化成強度本身，把所有板塊都誤標成「加速」。
    earliest = min(ts for ts, _ in rows)
    effective_window, momentum_known = resolve_momentum_window(earliest, now, window_minutes)
    recent_start = now - dt.timedelta(minutes=effective_window)
    prior_start = recent_start - dt.timedelta(minutes=effective_window)

    acc: dict[str, _Acc] = {}
    for ts, row in rows:
        get = row.get if isinstance(row, dict) else row.__getitem__
        code = get("code")
        sector = sector_map.sector_of(code)
        coef = calibration.get(code, 1.0)

        net = get("net_value") * coef
        turnover = get("turnover_value")

        rec = acc.setdefault(sector, _Acc())
        rec.codes.add(code)
        if sector_map.is_custom(code):
            rec.custom = True

        rec.total.net += net
        rec.total.turnover += turnover
        rec.burst_net += get("burst_net_value") * coef

        if ts > recent_start:
            rec.recent.net += net
            rec.recent.turnover += turnover
        elif ts >= prior_start:
            # 下界要含等號：視窗縮短時 prior_start 會剛好落在最早一筆資料上，
            # 用嚴格大於會把它整個丟掉，前段視窗就空了。
            rec.prior.net += net
            rec.prior.turnover += turnover

    points: list[SectorPoint] = []
    for sector, rec in acc.items():
        if len(rec.codes) < min_constituents or rec.total.turnover < min_turnover:
            continue
        strength = rec.total.strength
        recent_s = rec.recent.strength
        prior_s = rec.prior.strength
        # 前段視窗完全沒有成交時，兩者相減得到的不是加速度而是強度本身，
        # 所以這種板塊也要標為動能不可信（即使整體時間已經夠長）。
        known = momentum_known and rec.prior.turnover > 0
        momentum = recent_s - prior_s if known else 0.0
        points.append(
            SectorPoint(
                sector=sector,
                strength=strength,
                momentum=momentum,
                quadrant=classify_quadrant(strength, momentum, known),
                momentum_known=known,
                momentum_window_minutes=effective_window,
                net_value=rec.total.net,
                burst_net_value=rec.burst_net,
                turnover_value=rec.total.turnover,
                constituents=len(rec.codes),
                custom=rec.custom,
                recent_strength=recent_s,
                prior_strength=prior_s,
            )
        )

    # 依淨流入金額排序，讓排行榜與圖上的重點板塊一致
    points.sort(key=lambda p: p.net_value, reverse=True)
    return points


def rank_stocks(
    flow_rows: list,
    sector_map: SectorMap,
    *,
    names: dict[str, str] | None = None,
    calibration: dict[str, float] | None = None,
    limit: int = 30,
) -> list[dict]:
    """個股資金流排行（依淨流入金額）."""
    calibration = calibration or {}
    names = names or {}
    agg: dict[str, dict] = {}

    for row in flow_rows:
        get = row.get if isinstance(row, dict) else row.__getitem__
        code = get("code")
        coef = calibration.get(code, 1.0)
        rec = agg.setdefault(
            code,
            {
                "code": code,
                "name": names.get(code, ""),
                "sector": sector_map.sector_of(code),
                "net_value": 0.0,
                "burst_net_value": 0.0,
                "turnover_value": 0.0,
                "last_price": 0.0,
            },
        )
        rec["net_value"] += get("net_value") * coef
        rec["burst_net_value"] += get("burst_net_value") * coef
        rec["turnover_value"] += get("turnover_value")
        price = get("last_price")
        if price:
            rec["last_price"] = price

    ranked = sorted(agg.values(), key=lambda r: r["net_value"], reverse=True)
    if limit <= 0:
        return ranked
    # 頭尾各取 limit 檔：買超榜與賣超榜都要看得到
    return ranked[:limit] + ranked[-limit:] if len(ranked) > limit * 2 else ranked
