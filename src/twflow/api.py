"""儀表板的 HTTP 後端.

所有回傳資金流的端點都會附帶 ``disclaimer`` 與 ``accuracy`` 欄位，前端據此
在畫面上標示「這是推估值」以及目前的推估準確度。這是刻意的設計——把免責
說明放進資料本身，就不會有某個畫面忘記標示的情況。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .calibrate import accuracy_summary
from .config import Config
from .quadrant import compute_quadrants, rank_stocks
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

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def create_app(store: Store, config: Config) -> FastAPI:
    app = FastAPI(title="twflow 台股法人資金流向", version="0.1.0")

    def sector_map() -> SectorMap:
        # 每次請求重讀，讓使用者編輯 sectors.yaml 後不必重啟伺服器
        return SectorMap.load(
            config.get("sectors_file", "sectors.yaml"), securities=store.securities()
        )

    def calibration() -> dict[str, float]:
        if not config.get("flow.apply_calibration", True):
            return {}
        return store.calibration_coefs()

    def resolve_date(date: str | None) -> str:
        if date:
            return date
        return store.latest_flow_date() or now_taipei().date().isoformat()

    # ---------- 盤中 ----------

    @app.get("/api/quadrant")
    def quadrant(date: str | None = None, window: int | None = None):
        """四象限板塊輪動：每個板塊的資金流強度與動能."""
        trade_date = resolve_date(date)
        rows = store.flow_rows(trade_date)
        smap = sector_map()
        points = compute_quadrants(
            rows,
            smap,
            window_minutes=int(window or config.get("quadrant.momentum_window_minutes", 30)),
            min_constituents=int(config.get("quadrant.min_constituents", 2)),
            min_turnover=float(config.get("quadrant.min_turnover", 0)),
            calibration=calibration(),
        )
        return {
            "trade_date": trade_date,
            "as_of": rows[-1]["minute_ts"] if rows else None,
            "session_open": is_session_open(),
            "window_minutes": int(window or config.get("quadrant.momentum_window_minutes", 30)),
            "estimated": True,
            "disclaimer": INTRADAY_DISCLAIMER,
            "accuracy": accuracy_summary(store),
            "points": [p.to_dict() for p in points],
        }

    @app.get("/api/stocks")
    def stocks(date: str | None = None, limit: int = Query(30, ge=1, le=200)):
        """個股資金流排行（推估）."""
        trade_date = resolve_date(date)
        rows = store.flow_rows(trade_date)
        ranked = rank_stocks(
            rows, sector_map(), names=store.security_names(),
            calibration=calibration(), limit=limit,
        )
        return {
            "trade_date": trade_date,
            "estimated": True,
            "disclaimer": INTRADAY_DISCLAIMER,
            "stocks": ranked,
        }

    @app.get("/api/watchlist")
    def watchlist(date: str | None = None):
        """自選股：盤中推估資金流 + 盤後真實買賣超 + 外資持股比率.

        刻意把「推估」與「官方」兩種數字並排顯示，讓使用者一眼看出哪個
        欄位可以信到什麼程度。
        """
        trade_date = resolve_date(date)
        codes = [str(c) for c in config.get("watchlist", [])]
        smap = sector_map()
        names = store.security_names()
        ratios = store.latest_foreign_holding()
        coefs = calibration()

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

    @app.get("/api/institutional")
    def institutional(date: str | None = None, limit: int = Query(30, ge=1, le=200)):
        """官方三大法人買賣超排行（非推估）."""
        trade_date = date or store.latest_insti_date()
        if not trade_date:
            return {"trade_date": None, "note": EOD_NOTE, "buy": [], "sell": []}

        smap = sector_map()
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

    @app.get("/api/futures")
    def futures(date: str | None = None):
        """期貨三大法人未平倉——法人多空方向最直接的官方數字."""
        trade_date = date or store.latest_futures_date()
        if not trade_date:
            return {"trade_date": None, "rows": []}
        return {
            "trade_date": trade_date,
            "estimated": False,
            "rows": [dict(r) for r in store.futures_oi(trade_date)],
        }

    @app.get("/api/brokers")
    def brokers(date: str | None = None, limit: int = Query(30, ge=1, le=200)):
        """官股動向：公股行庫券商分點的買賣超彙總（選配資料）."""
        trade_date = date or store.latest_broker_date()
        if not trade_date:
            return {
                "trade_date": None,
                "available": False,
                "note": "尚未匯入分點資料。分點需自 BSR 手動下載後放進 data/bsr/，詳見 README。",
                "rows": [],
            }
        smap = sector_map()
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

    @app.get("/api/accuracy")
    def accuracy():
        """推估準確度：與官方數據比對的等級相關與方向一致率."""
        return accuracy_summary(store, days=60)

    @app.get("/api/meta")
    def meta():
        smap = sector_map()
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

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---------- 靜態前端 ----------

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"))
    else:
        @app.get("/")
        def index_missing():
            return JSONResponse({"error": f"找不到前端目錄: {WEB_DIR}"}, status_code=500)

    return app
