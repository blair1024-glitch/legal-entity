"""儀表板資料的純函式版本.

這裡的每個函式對應 ``api.py`` 的一個 endpoint，回傳同樣形狀的 dict，
但不依賴 FastAPI——單純吃 ``Store``／``Config`` 與參數，回傳資料。

存在的理由：``api.py``（即時回應 HTTP 請求）與 ``static_export.py``
（定時算好存成靜態 JSON，給 GitHub Pages 用）需要輸出**完全一樣**的
JSON 形狀。若兩邊各自維護一份組裝邏輯，日後有人幫某個 endpoint 加欄位
卻忘記同步另一邊，兩個版本的畫面會悄悄長不一樣——這裡抽成共用函式，
兩邊呼叫同一份程式碼，保證 bit-for-bit 一致。
"""

from __future__ import annotations

from .calibrate import accuracy_summary
from .config import Config
from .quadrant import compute_quadrants, compute_trail, rank_stocks
from .sectors import SectorMap
from .sources.bsr import state_broker_summary
from .store import Store
from .tradingcal import is_session_open, now_taipei

INTRADAY_DISCLAIMER = (
    "盤中資金流為推估值，非官方法人買賣超。台股沒有公開的盤中法人資料，"
    "證交所三大法人買賣超（T86）收盤後才發布。此處是以成交價相對於委買委賣"
    "的位置推估主動買賣方向，僅供參考，非投資建議。"
)

EOD_NOTE = "此為證交所／櫃買官方公布的三大法人買賣超，非推估值。"


def sector_map(store: Store, config: Config) -> SectorMap:
    # 每次重讀，讓使用者編輯 sectors.yaml 後不必重啟伺服器／重新匯出
    return SectorMap.load(
        config.get("sectors_file", "sectors.yaml"), securities=store.securities()
    )


def calibration(store: Store, config: Config) -> dict[str, float]:
    if not config.get("flow.apply_calibration", True):
        return {}
    return store.calibration_coefs()


def resolve_date(store: Store, date: str | None) -> str:
    if date:
        return date
    return store.latest_flow_date() or now_taipei().date().isoformat()


# ---------- 盤中 ----------


def quadrant_view(
    store: Store,
    config: Config,
    *,
    date: str | None = None,
    window: int | None = None,
    trail: int = 0,
    trail_step: int = 10,
    trail_top: int = 10,
) -> dict:
    """四象限板塊輪動：每個板塊的資金流強度與動能."""
    trade_date = resolve_date(store, date)
    rows = store.flow_rows(trade_date)
    smap = sector_map(store, config)
    coefs = calibration(store, config)
    requested_window = int(window or config.get("quadrant.momentum_window_minutes", 30))

    points = compute_quadrants(
        rows,
        smap,
        window_minutes=requested_window,
        min_constituents=int(config.get("quadrant.min_constituents", 2)),
        min_turnover=float(config.get("quadrant.min_turnover", 0)),
        calibration=coefs,
    )
    # 開盤初期歷史不足時實際用的視窗會被縮短，要如實回報而不是回傳使用者
    # 選的那個數字——否則畫面說「30 分鐘」但其實是拿 5 分鐘算的。
    effective = points[0].momentum_window_minutes if points else float(requested_window)
    unknown = sum(1 for p in points if not p.momentum_known)

    # 輪動軌跡：只算前幾名板塊，全部畫線會糊成一團
    trails: dict = {}
    if trail > 0 and points:
        ranked = sorted(points, key=lambda p: abs(p.net_value), reverse=True)
        trails = compute_trail(
            rows,
            smap,
            window_minutes=requested_window,
            steps=trail,
            step_minutes=trail_step,
            sectors={p.sector for p in ranked[:trail_top]},
            min_constituents=int(config.get("quadrant.min_constituents", 2)),
            min_turnover=float(config.get("quadrant.min_turnover", 0)),
            calibration=coefs,
        )

    return {
        "trade_date": trade_date,
        "as_of": rows[-1]["minute_ts"] if rows else None,
        "session_open": is_session_open(),
        "window_minutes": requested_window,
        "trail": trails,
        "effective_window_minutes": round(effective, 1),
        "window_shortened": effective < requested_window - 0.05,
        "momentum_unknown_sectors": unknown,
        # 沒有盤中資料時，前端要能分辨「還沒開始收集」與「官方數據也沒有」，
        # 兩者該講的話不一樣
        "has_official": bool(store.latest_insti_date()),
        "official_date": store.latest_insti_date(),
        "estimated": True,
        "disclaimer": INTRADAY_DISCLAIMER,
        "accuracy": accuracy_summary(store),
        "points": [p.to_dict() for p in points],
    }


def stocks_view(
    store: Store,
    config: Config,
    *,
    date: str | None = None,
    limit: int = 30,
    sector: str | None = None,
) -> dict:
    """個股資金流排行（推估）.

    帶 ``sector`` 可只看某個板塊的成分股——從板塊圖點進來時用，
    回答「這個板塊是被哪幾檔帶動的」。
    """
    trade_date = resolve_date(store, date)
    smap = sector_map(store, config)
    rows = store.flow_rows(trade_date)
    if sector:
        rows = [r for r in rows if smap.sector_of(r["code"]) == sector]
    ranked = rank_stocks(
        rows, smap, names=store.security_names(),
        calibration=calibration(store, config),
        # 看單一板塊時不做頭尾裁切——成分股本來就不多，全部列出來
        limit=0 if sector else limit,
    )
    return {
        "trade_date": trade_date,
        "estimated": True,
        "sector": sector,
        "disclaimer": INTRADAY_DISCLAIMER,
        "stocks": ranked,
    }


def watchlist_view(store: Store, config: Config, *, date: str | None = None) -> dict:
    """自選股：盤中推估資金流 + 盤後真實買賣超 + 外資持股比率.

    刻意把「推估」與「官方」兩種數字並排顯示，讓使用者一眼看出哪個
    欄位可以信到什麼程度。
    """
    trade_date = resolve_date(store, date)
    codes = [str(c) for c in config.get("watchlist", [])]
    smap = sector_map(store, config)
    names = store.security_names()
    ratios = store.latest_foreign_holding()
    coefs = calibration(store, config)

    flow_by_code: dict[str, dict] = {}
    for row in store.flow_rows(trade_date):
        code = row["code"]
        rec = flow_by_code.setdefault(code, {"net_value": 0.0, "last_price": 0.0})
        rec["net_value"] += row["net_value"] * coefs.get(code, 1.0)
        if row["last_price"]:
            rec["last_price"] = row["last_price"]

    insti_date = store.latest_insti_date()
    insti = {r["code"]: r for r in store.insti_daily(insti_date)} if insti_date else {}

    out = []
    for code in codes:
        flow = flow_by_code.get(code, {})
        official = insti.get(code)
        out.append(
            {
                "code": code,
                "name": names.get(code, ""),
                "sector": smap.sector_of(code),
                "last_price": flow.get("last_price", 0.0),
                "est_net_value": round(flow.get("net_value", 0.0), 2),
                "foreign_ratio": ratios.get(code),
                "official": {
                    "trade_date": insti_date,
                    "foreign_net": official["foreign_net"] if official else None,
                    "trust_net": official["trust_net"] if official else None,
                    "dealer_net": official["dealer_net"] if official else None,
                    "total_net": official["total_net"] if official else None,
                },
            }
        )
    return {
        "trade_date": trade_date,
        "disclaimer": INTRADAY_DISCLAIMER,
        "eod_note": EOD_NOTE,
        "items": out,
    }


# ---------- 盤後（官方數據） ----------


def institutional_view(store: Store, config: Config, *, date: str | None = None, limit: int = 30) -> dict:
    """官方三大法人買賣超排行（非推估）."""
    trade_date = date or store.latest_insti_date()
    if not trade_date:
        return {"trade_date": None, "note": EOD_NOTE, "buy": [], "sell": []}

    smap = sector_map(store, config)
    names = store.security_names()
    rows = [
        {
            "code": r["code"],
            "name": names.get(r["code"], ""),
            "sector": smap.sector_of(r["code"]),
            "market": r["market"],
            "foreign_net": r["foreign_net"],
            "trust_net": r["trust_net"],
            "dealer_net": r["dealer_net"],
            "total_net": r["total_net"],
        }
        for r in store.insti_daily(trade_date)
    ]
    rows.sort(key=lambda r: r["total_net"], reverse=True)
    return {
        "trade_date": trade_date,
        "estimated": False,
        "note": EOD_NOTE,
        "buy": rows[:limit],
        "sell": list(reversed(rows[-limit:])),
    }


def watchlist_lookup_view(store: Store, config: Config) -> dict:
    """全市場的官方數字＋外資持股比率，以代號查詢（不分頁、不排序）.

    ``institutional_view()`` 只回傳買超／賣超前後 N 檔，給榜單用；這裡是
    給 GitHub Pages 版「使用者自己輸入代號加自選股」用的——使用者可能
    輸入任何一檔，不能假設它落在買賣超排行的前後幾十名之內。
    """
    smap = sector_map(store, config)
    names = store.security_names()
    ratios = store.latest_foreign_holding()

    trade_date = store.latest_insti_date()
    insti = {r["code"]: r for r in store.insti_daily(trade_date)} if trade_date else {}

    out: dict[str, dict] = {}
    for code in set(insti) | set(ratios):
        official = insti.get(code)
        out[code] = {
            "name": names.get(code, ""),
            "sector": smap.sector_of(code),
            "foreign_ratio": ratios.get(code),
            "official": {
                "trade_date": trade_date,
                "foreign_net": official["foreign_net"] if official else None,
                "trust_net": official["trust_net"] if official else None,
                "dealer_net": official["dealer_net"] if official else None,
                "total_net": official["total_net"] if official else None,
            },
        }
    return out


def futures_view(store: Store, *, date: str | None = None) -> dict:
    """期貨三大法人未平倉——法人多空方向最直接的官方數字."""
    trade_date = date or store.latest_futures_date()
    if not trade_date:
        return {"trade_date": None, "rows": []}
    return {
        "trade_date": trade_date,
        "estimated": False,
        "rows": [dict(r) for r in store.futures_oi(trade_date)],
    }


def brokers_view(store: Store, config: Config, *, date: str | None = None, limit: int = 30) -> dict:
    """官股動向：公股行庫券商分點的買賣超彙總（選配資料）."""
    trade_date = date or store.latest_broker_date()
    if not trade_date:
        return {
            "trade_date": None,
            "available": False,
            "note": "尚未匯入分點資料。分點需自 BSR 手動下載後放進 data/bsr/，詳見 README。",
            "rows": [],
        }
    smap = sector_map(store, config)
    rows = [dict(r) for r in store.broker_branch(trade_date)]
    summary = state_broker_summary(rows, smap.state_brokers or None)
    names = store.security_names()
    for s in summary:
        s["name"] = names.get(s["code"], "")
        s["sector"] = smap.sector_of(s["code"])
    return {
        "trade_date": trade_date,
        "available": True,
        "estimated": False,
        "rows": summary[:limit],
    }


# ---------- 診斷與說明 ----------


def accuracy_view(store: Store) -> dict:
    """推估準確度：與官方數據比對的等級相關與方向一致率."""
    return accuracy_summary(store, days=60)


def meta_view(store: Store, config: Config) -> dict:
    smap = sector_map(store, config)
    custom = sum(1 for c in smap.by_code if smap.is_custom(c))
    return {
        "now": now_taipei().isoformat(),
        "session_open": is_session_open(),
        "mode": config.get("mode"),
        "markets": config.get("markets"),
        "securities": len(store.securities()),
        "sectors": len(smap.sectors()),
        "custom_classified": custom,
        "latest_flow_date": store.latest_flow_date(),
        "latest_insti_date": store.latest_insti_date(),
        "disclaimer": INTRADAY_DISCLAIMER,
    }
