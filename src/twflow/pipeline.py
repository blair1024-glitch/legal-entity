"""盤後流程協調：抓官方數據 → 存檔 → 校準.

每個步驟都獨立回報成功或失敗。這裡刻意**不讓單一來源的失敗中斷整批**——
例如期交所改版導致解析失敗時，三大法人與校準仍應照常完成，否則一個次要
來源的問題會讓整天的資料都留空。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .calibrate import calibrate_day, update_coefficients
from .errors import TwflowError
from .httpclient import Fetcher
from .sectors import SectorMap
from .sources import bsr, taifex, tpex_insti, twse_meta, twse_qfiis, twse_t86
from .store import Store
from .tradingcal import mark_non_trading_day


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    rows: int = 0

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  {mark} {self.name}: {self.rows} 筆{suffix}"


@dataclass
class RunReport:
    steps: list[StepResult] = field(default_factory=list)

    def add(self, name: str, fn) -> StepResult:
        """執行一個步驟並記錄結果，例外一律轉成失敗紀錄而不往上拋."""
        try:
            rows = fn()
        except TwflowError as exc:
            step = StepResult(name, False, str(exc))
        except Exception as exc:  # noqa: BLE001 — 未預期的錯誤同樣不該中斷整批
            step = StepResult(name, False, f"未預期的錯誤: {exc!r}")
        else:
            step = StepResult(name, True, rows=rows if isinstance(rows, int) else 0)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def render(self) -> str:
        return "\n".join(s.line() for s in self.steps)


def sync_securities(store: Store, fetcher: Fetcher, markets: list[str]) -> RunReport:
    """匯入上市／上櫃證券清單與官方產業別，並套用自訂細分板塊."""
    report = RunReport()
    collected: list[dict] = []

    for market in markets:
        def _fetch(m=market):
            rows = twse_meta.fetch(fetcher, market=m)
            collected.extend(rows)
            return len(rows)

        report.add(f"{market} 證券清單與產業別", _fetch)

    if collected:
        def _store():
            # 先寫入官方產業別，再用 sectors.yaml 覆蓋細分板塊
            sector_map = SectorMap.load(securities=collected)
            for row in collected:
                row["sector"] = sector_map.sector_of(row["code"])
                row["sector_src"] = sector_map.source.get(row["code"], "official")
            return store.upsert_securities(collected)

        report.add("寫入資料庫", _store)

    return report


def run_eod(
    store: Store,
    fetcher: Fetcher,
    day: dt.date,
    *,
    markets: list[str] | None = None,
    with_bsr: bool = True,
) -> RunReport:
    """執行單日的盤後流程."""
    markets = [m.upper() for m in (markets or ["TWSE"])]
    report = RunReport()

    if "TWSE" in markets:
        report.add(
            "上市三大法人買賣超 (T86)",
            lambda: store.upsert_insti_daily(twse_t86.fetch(fetcher, day)),
        )

    if "TPEX" in markets:
        report.add(
            "上櫃三大法人買賣超",
            lambda: store.upsert_insti_daily(tpex_insti.fetch(fetcher, day)),
        )

    if "TWSE" in markets:
        report.add(
            "外資持股比率 (MI_QFIIS)",
            lambda: store.upsert_foreign_holding(twse_qfiis.fetch(fetcher, day)),
        )

    report.add(
        "期貨三大法人未平倉",
        lambda: store.upsert_futures_oi(taifex.fetch(fetcher, day)),
    )

    if with_bsr:
        # 分點是選配：沒有檔案就是 0 筆，不算失敗
        report.add(
            "券商分點（手動匯入）",
            lambda: store.upsert_broker_branch(bsr.load_directory()),
        )

    # 非交易日的話，把它記起來讓後續統計自動跳過
    insti_step = next((s for s in report.steps if "三大法人買賣超" in s.name), None)
    if insti_step and not insti_step.ok and "非交易日" in insti_step.detail:
        mark_non_trading_day(day)

    report.add("校準推估值", lambda: _calibrate(store, day))
    report.add("更新校準係數", lambda: update_coefficients(store))
    return report


def _calibrate(store: Store, day: dt.date) -> int:
    acc = calibrate_day(store, day)
    return acc.n_stocks if acc else 0


def run_eod_range(
    store: Store,
    fetcher: Fetcher,
    start: dt.date,
    end: dt.date,
    *,
    markets: list[str] | None = None,
    on_day=None,
) -> dict[str, RunReport]:
    """回補一段日期區間的盤後資料.

    校準係數需要每檔至少 5 個交易日的樣本才會生效（見
    :func:`twflow.calibrate.update_coefficients`），一天一天跑太慢，
    所以提供區間回補。

    週末會自動跳過；某一天失敗（非交易日、來源改版）只記錄下來，
    不中斷整個區間——回補二十天不該因為中間有個國定假日就停掉。

    注意：**盤中推估資料無法回補**。證交所沒有提供歷史逐筆或分時報價，
    推估值只能在當天盤中即時累積。所以回補出來的是官方三大法人數據，
    校準要等你實際盤中跑過 ``twflow poll`` 才會有樣本可比。
    """
    markets = markets or ["TWSE"]
    results: dict[str, RunReport] = {}

    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += dt.timedelta(days=1)
            continue
        report = run_eod(store, fetcher, day, markets=markets, with_bsr=False)
        results[day.isoformat()] = report
        if on_day is not None:
            on_day(day, report)
        day += dt.timedelta(days=1)

    return results
