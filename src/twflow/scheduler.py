"""``twflow auto``：一個指令跑完整天.

沒有這個的話，使用者每天要記得：09:00 開 ``twflow poll``、13:30 之後關掉、
15:00 之後再跑 ``twflow eod``。忘記任何一步，當天的推估或校準就缺一塊——
而校準需要連續累積才有意義。

這裡用一個監督迴圈依時鐘決定該做什麼：

* 盤中（09:00–13:30）→ 輪詢即時報價
* 收盤後且官方資料應已發布（16:00 之後）→ 跑當日盤後流程
* 其餘時間 → 睡到下一個該做事的時間點

跨日、非交易日、程式中途重啟都要能正確接續，所以「今天的盤後跑過了沒」
是去資料庫查當日有沒有三大法人資料，而不是記在記憶體裡。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

from .config import Config
from .httpclient import Fetcher
from .pipeline import run_eod
from .poller import Poller
from .store import Store
from .tradingcal import (
    EOD_READY,
    SESSION_CLOSE,
    SESSION_OPEN,
    TAIPEI,
    is_session_open,
    is_trading_day,
    now_taipei,
)

log = logging.getLogger(__name__)

# 非交易時段的檢查間隔。不需要太密——反正在等一個固定的時鐘時間。
IDLE_SLEEP_SECONDS = 300


@dataclass
class Scheduler:
    store: Store
    fetcher: Fetcher
    config: Config

    def eod_done(self, day: dt.date) -> bool:
        """當日盤後流程是否已完成（查資料庫，不靠記憶體狀態）."""
        return bool(self.store.insti_daily(day.isoformat()))

    def _seconds_until(self, target: dt.time, at: dt.datetime) -> float:
        """距離今天的 ``target`` 還有幾秒；已經過了就回傳 0."""
        target_dt = dt.datetime.combine(at.date(), target, tzinfo=TAIPEI)
        return max((target_dt - at).total_seconds(), 0.0)

    def next_action(self, at: dt.datetime | None = None) -> tuple[str, float]:
        """決定現在該做什麼，以及若無事可做該睡多久.

        Returns
        -------
        ``(action, sleep_seconds)``，action 為 ``poll`` / ``eod`` / ``idle``。
        """
        at = (at or now_taipei()).astimezone(TAIPEI)
        today = at.date()

        if not is_trading_day(today):
            return "idle", IDLE_SLEEP_SECONDS

        if is_session_open(at):
            return "poll", 0.0

        if at.time() < SESSION_OPEN:
            # 開盤前：睡到開盤（最多睡 IDLE_SLEEP_SECONDS，好讓中斷能及時反應）
            return "idle", min(self._seconds_until(SESSION_OPEN, at), IDLE_SLEEP_SECONDS)

        # 已收盤
        if at.time() >= EOD_READY and not self.eod_done(today):
            return "eod", 0.0

        if at.time() < EOD_READY:
            return "idle", min(self._seconds_until(EOD_READY, at), IDLE_SLEEP_SECONDS)

        return "idle", IDLE_SLEEP_SECONDS

    def run(self, *, max_iterations: int | None = None) -> None:
        """監督迴圈。``max_iterations`` 供測試使用."""
        markets = [str(m) for m in self.config.get("markets", ["TWSE"])]
        poller = Poller(self.store, self.fetcher, self.config)
        interval = float(self.config.get("poll.interval_seconds", 60))
        seeded_for: dt.date | None = None
        iterations = 0

        log.info("auto 模式啟動。盤中輪詢、收盤後自動跑盤後流程。Ctrl-C 結束。")

        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            at = now_taipei()
            action, sleep_for = self.next_action(at)

            if action == "poll":
                today = at.date()
                if seeded_for != today:
                    # 換日或剛啟動：還原前次快照，避免丟掉一個區間的成交量
                    restored = poller.restore_state(today.isoformat())
                    log.info("盤中輪詢開始（還原 %d 檔前次快照）", restored)
                    seeded_for = today

                started = time.monotonic()
                stats = poller.poll_once(today.isoformat())
                log.info(
                    "輪詢: %d 批 / %d 檔 / %d 筆增量%s",
                    stats.batches, stats.quotes, stats.increments,
                    f" / {len(stats.errors)} 個錯誤" if stats.errors else "",
                )
                for err in stats.errors[:2]:
                    log.warning("  %s", err)
                sleep_for = max(interval - (time.monotonic() - started), 1.0)

            elif action == "eod":
                day = at.date()
                log.info("執行 %s 的盤後流程…", day)
                report = run_eod(self.store, self.fetcher, day, markets=markets)
                for line in report.render().splitlines():
                    log.info("%s", line)
                removed = self.store.purge_old(int(self.config.get("retention_days", 30)))
                if removed:
                    log.info("清理過期盤中資料 %d 筆", removed)
                sleep_for = IDLE_SLEEP_SECONDS

            else:
                log.debug("無事可做，睡 %.0f 秒", sleep_for)

            if max_iterations is not None:
                # 測試模式下不要真的睡
                continue
            time.sleep(max(sleep_for, 1.0))
