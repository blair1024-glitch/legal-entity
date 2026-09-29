"""儀表板的 HTTP 後端.

實際的資料組裝邏輯在 ``views.py``——那份是純函式，這裡的 route handler
只負責把 querystring 參數轉成呼叫參數，讓 ``views.py`` 可以同時被這裡
（即時回應）與 ``static_export.py``（定時算好存成靜態 JSON）共用，保證
兩邊輸出的 JSON 形狀完全一致。

所有回傳資金流的端點都會附帶 ``disclaimer`` 與 ``accuracy`` 欄位，前端據此
在畫面上標示「這是推估值」以及目前的推估準確度。這是刻意的設計——把免責
說明放進資料本身，就不會有某個畫面忘記標示的情況。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import views
from .config import Config
from .store import Store

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def create_app(store: Store, config: Config) -> FastAPI:
    app = FastAPI(title="twflow 台股法人資金流向", version="0.1.0")

    @app.get("/api/quadrant")
    def quadrant(
        date: str | None = None,
        window: int | None = None,
        trail: int = Query(0, ge=0, le=12),
        trail_step: int = Query(10, ge=1, le=60),
        trail_top: int = Query(10, ge=1, le=40),
    ):
        return views.quadrant_view(
            store, config, date=date, window=window,
            trail=trail, trail_step=trail_step, trail_top=trail_top,
        )

    @app.get("/api/stocks")
    def stocks(
        date: str | None = None,
        limit: int = Query(30, ge=1, le=200),
        sector: str | None = None,
    ):
        return views.stocks_view(store, config, date=date, limit=limit, sector=sector)

    @app.get("/api/watchlist")
    def watchlist(date: str | None = None):
        return views.watchlist_view(store, config, date=date)

    @app.get("/api/institutional")
    def institutional(date: str | None = None, limit: int = Query(30, ge=1, le=200)):
        return views.institutional_view(store, config, date=date, limit=limit)

    @app.get("/api/futures")
    def futures(date: str | None = None):
        return views.futures_view(store, date=date)

    @app.get("/api/brokers")
    def brokers(date: str | None = None, limit: int = Query(30, ge=1, le=200)):
        return views.brokers_view(store, config, date=date, limit=limit)

    @app.get("/api/accuracy")
    def accuracy():
        return views.accuracy_view(store)

    @app.get("/api/meta")
    def meta():
        return views.meta_view(store, config)

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
