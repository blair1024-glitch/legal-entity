"""把儀表板資料匯出成靜態 JSON，供 GitHub Pages 用.

GitHub Actions 每次執行都是全新環境、沒有辦法像 ``api.py`` 那樣即時回應
帶參數的請求。這裡改成**收盤前預先算好幾種固定情境**，寫成檔案，前端
改成讀檔案而不是打 API。

## 為什麼要「全部算完才寫檔」

``build_export()`` 把 9 份輸出的內容都在記憶體算好才回傳；``write_export()``
是後面單獨的步驟，只做檔案 I/O。任何一步計算失敗就整個 raise、一個檔案
都不寫——呼叫端（GitHub Actions workflow）只要讓 ``export-static`` 指令
失敗時不要進到 commit 步驟，GitHub Pages 上的畫面就會維持上一次成功的
內容，不會被半成品覆蓋掉。這比任何檔案系統層級的原子寫入技巧都可靠，
因為真正的「發布」動作是 git commit，而 git commit 本來就是原子的。

## 三個 quadrant 視窗、全量 stocks

``window`` 只固定算 15/30/60 分鐘三種，對應前端下拉選單本來就有的三個
選項；``sector`` 篩選則是把個股全量匯出，讓瀏覽器自己在拿到資料後用
JS 篩選——上市加上櫃約 1,800 檔，未壓縮頂多幾百 KB，GitHub Pages 預設
會壓縮，負擔得起，也不必為 54 個板塊各存一個檔案。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import views
from .config import Config
from .store import Store

QUADRANT_WINDOWS = (15, 30, 60)

# 對應前端 app.js 目前硬編碼的軌跡參數（見 loadQuadrant 裡的
# `trail=6&trail_step=20&trail_top=6`）。靜態版一次算好含軌跡的版本，
# 前端只是要不要「顯示」軌跡，不影響該抓哪份資料。
TRAIL_STEPS = 6
TRAIL_STEP_MINUTES = 20
TRAIL_TOP = 6


def _pipeline_meta(store: Store) -> dict:
    """讓使用者事後看得出「這是不是第一次真的碰到真實環境」.

    Actions 的資料庫從頭到尾只會被 live 模式寫入（workflow 不會跑
    `twflow demo`），所以 `ever_fetched_intraday`/`ever_fetched_official`
    兩個欄位皆為 false，就代表連一次都還沒成功過——這正是「還不確定
    連不連得上真實資料源」的誠實寫照，不是憑空發明的一個旗標，純粹是
    既有查詢方法的組合。
    """
    return {
        "source": "github-actions",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ever_fetched_intraday": store.latest_flow_date() is not None,
        "ever_fetched_official": store.latest_insti_date() is not None,
    }


def build_export(store: Store, config: Config, *, date: str | None = None) -> dict[str, dict]:
    """算好全部 9 份輸出，回傳 ``{檔名(不含副檔名): payload}``.

    任一步失敗就整個 raise，呼叫端不該收到部分結果。
    """
    outputs: dict[str, dict] = {}

    for window in QUADRANT_WINDOWS:
        outputs[f"quadrant_{window}"] = views.quadrant_view(
            store, config, date=date, window=window,
            trail=TRAIL_STEPS, trail_step=TRAIL_STEP_MINUTES, trail_top=TRAIL_TOP,
        )

    outputs["stocks_full"] = views.stocks_view(store, config, date=date, limit=0)
    outputs["watchlist"] = views.watchlist_view(store, config, date=date)
    outputs["institutional"] = views.institutional_view(store, config, date=date)
    outputs["futures"] = views.futures_view(store, date=date)
    outputs["brokers"] = views.brokers_view(store, config, date=date)
    outputs["accuracy"] = views.accuracy_view(store)

    meta = views.meta_view(store, config)
    meta["pipeline"] = _pipeline_meta(store)
    outputs["meta"] = meta

    return outputs


def write_export(outputs: dict[str, dict], out_dir: str | Path) -> list[Path]:
    """單純寫檔，不含任何計算邏輯——方便獨立測試、獨立失敗。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in outputs.items():
        path = out / f"{name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        written.append(path)
    return written
