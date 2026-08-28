"""交易日與盤中時段判斷（台北時間）.

台股常規交易時段是 09:00–13:30。本模組只處理常規盤——盤後定價與零股
不納入資金流推估，因為它們的價量特性和連續競價完全不同。

國定假日沒有公開的機器可讀清單可靠取得，所以採兩層策略：
1. 週末直接排除；
2. ``data/holidays.txt`` 由使用者維護（一行一個 ``YYYY-MM-DD``），
   另外當盤後抓取回傳空資料時，``mark_non_trading_day`` 會把該日記起來，
   讓後續的校準統計自動跳過。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")

SESSION_OPEN = dt.time(9, 0)
SESSION_CLOSE = dt.time(13, 30)
# T86 大約 15:00–16:00 之間才會出來，留一點餘裕。
EOD_READY = dt.time(16, 0)


def now_taipei() -> dt.datetime:
    return dt.datetime.now(TAIPEI)


def today_taipei() -> dt.date:
    return now_taipei().date()


def load_holidays(path: str | Path = "data/holidays.txt") -> set[dt.date]:
    p = Path(path)
    if not p.exists():
        return set()
    out: set[dt.date] = set()
    for line in p.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.add(dt.date.fromisoformat(line))
        except ValueError:
            # 壞掉的一行不該讓整個程式停下來。
            continue
    return out


def mark_non_trading_day(day: dt.date, path: str | Path = "data/holidays.txt") -> None:
    """把某日記為非交易日（盤後抓取拿到空資料時呼叫）."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_holidays(p)
    if day in existing:
        return
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"{day.isoformat()}  # 自動標記：該日無盤後資料\n")


def is_trading_day(day: dt.date | None = None, holidays: set[dt.date] | None = None) -> bool:
    day = day or today_taipei()
    if day.weekday() >= 5:
        return False
    hol = load_holidays() if holidays is None else holidays
    return day not in hol


def is_session_open(at: dt.datetime | None = None) -> bool:
    """現在是否為常規交易時段（09:00–13:30 台北時間）."""
    at = at or now_taipei()
    if at.tzinfo is None:
        at = at.replace(tzinfo=TAIPEI)
    at = at.astimezone(TAIPEI)
    if not is_trading_day(at.date()):
        return False
    return SESSION_OPEN <= at.time() <= SESSION_CLOSE


def eod_data_ready(at: dt.datetime | None = None) -> bool:
    """盤後三大法人數據是否應該已經發布."""
    at = at or now_taipei()
    at = at.astimezone(TAIPEI)
    return is_trading_day(at.date()) and at.time() >= EOD_READY


def session_elapsed_minutes(at: dt.datetime | None = None) -> float:
    """自開盤以來經過的分鐘數，收盤後回傳整段長度（270 分鐘）."""
    at = (at or now_taipei()).astimezone(TAIPEI)
    open_dt = dt.datetime.combine(at.date(), SESSION_OPEN, tzinfo=TAIPEI)
    close_dt = dt.datetime.combine(at.date(), SESSION_CLOSE, tzinfo=TAIPEI)
    if at <= open_dt:
        return 0.0
    if at >= close_dt:
        return (close_dt - open_dt).total_seconds() / 60.0
    return (at - open_dt).total_seconds() / 60.0


def previous_trading_day(day: dt.date | None = None) -> dt.date:
    day = day or today_taipei()
    cur = day - dt.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur -= dt.timedelta(days=1)
    return cur


def roc_to_date(value: str) -> dt.date:
    """把民國年日期轉成 ``date``.

    證交所與櫃買的回應混用 ``114/08/27``（民國）與 ``2025/08/27``（西元），
    兩種都要能吃。
    """
    parts = value.strip().replace("-", "/").split("/")
    if len(parts) != 3:
        raise ValueError(f"無法解析日期: {value!r}")
    year, month, day = (int(p) for p in parts)
    if year < 1911:  # 民國年
        year += 1911
    return dt.date(year, month, day)
