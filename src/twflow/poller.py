"""盤中輪詢：把 MIS 即時報價轉成資金流並寫進資料庫.

## 輪詢節奏

MIS 的速率限制約 3 requests / 5 秒。上市＋上櫃約 1,800 檔，每批 50 檔就是
36 個請求，一輪大約 60 秒。這是預設節奏——想要更快就把 ``universe`` 設成
``watchlist`` 只掃自選股。

## 為什麼一輪的長度會影響推估品質

輪詢間隔越長，兩次快照之間累積的成交量越多，用「單一成交價」代表整段
區間的誤差就越大。60 秒是資料涵蓋度與推估精度之間的折衷；只掃自選股時
間隔可以縮到 5–10 秒，推估會明顯更準。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from .config import Config
from .errors import FetchError, ParseError
from .flow import FlowTracker, Quote
from .httpclient import Fetcher
from .sources import mis
from .store import Store
from .tradingcal import TAIPEI, is_session_open, now_taipei

log = logging.getLogger(__name__)


@dataclass
class PollStats:
    """單輪輪詢的結果，供 CLI 與診斷輸出."""

    batches: int = 0
    quotes: int = 0
    increments: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Poller:
    store: Store
    fetcher: Fetcher
    config: Config
    tracker: FlowTracker = field(init=False)
    names: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tracker = FlowTracker(
            burst_threshold_lots=float(self.config.get("flow.burst_lot_threshold", 100)),
            midpoint_rule=str(self.config.get("flow.midpoint_rule", "proportional")),
        )

    # ---------- universe ----------

    def universe(self) -> list[tuple[str, str]]:
        """決定要掃哪些股票，回傳 ``[(code, market), ...]``."""
        if str(self.config.get("poll.universe", "all")) == "watchlist":
            watch = [str(c) for c in self.config.get("watchlist", [])]
            known = {r["code"]: r["market"] for r in self.store.securities()}
            return [(c, known.get(c, "TWSE")) for c in watch]

        markets = {str(m).upper() for m in self.config.get("markets", ["TWSE"])}
        return [
            (r["code"], r["market"])
            for r in self.store.securities()
            if r["market"].upper() in markets
        ]

    # ---------- state ----------

    def restore_state(self, trade_date: str) -> int:
        """從資料庫還原上一次的快照，讓重啟後不必丟掉一個區間的成交量."""
        saved = self.store.load_quote_state(trade_date)
        quotes = [
            Quote(
                code=r["code"],
                ts=dt.datetime.fromisoformat(r["ts"]),
                price=r["price"],
                cum_volume=r["cum_volume"],
                bid1=r["bid1"],
                ask1=r["ask1"],
            )
            for r in saved.values()
        ]
        self.tracker.seed(quotes)
        return len(quotes)

    # ---------- one round ----------

    def poll_once(self, trade_date: str | None = None) -> PollStats:
        """掃一輪 universe，把資金流增量寫進資料庫."""
        trade_date = trade_date or now_taipei().date().isoformat()
        batch_size = int(self.config.get("poll.batch_size", 50))
        stats = PollStats()

        codes = self.universe()
        if not codes:
            stats.errors.append("universe 是空的——請先執行 `twflow sync` 匯入證券清單")
            return stats

        all_quotes: list[Quote] = []
        for batch in mis.batched(codes, batch_size):
            stats.batches += 1
            try:
                quotes = mis.fetch_batch(self.fetcher, batch)
            except (FetchError, ParseError) as exc:
                # 單一批次失敗不該中斷整輪——下一輪會補回來
                stats.errors.append(f"批次 {stats.batches}: {exc}")
                continue
            all_quotes.extend(quotes)

        stats.quotes = len(all_quotes)
        if not all_quotes:
            return stats

        increments = self.tracker.update(all_quotes)
        stats.increments = len(increments)

        if increments:
            self.store.add_flow_minute(
                [
                    {
                        "trade_date": trade_date,
                        "code": inc.code,
                        "minute_ts": inc.minute_ts,
                        "buy_lots": inc.buy_lots,
                        "sell_lots": inc.sell_lots,
                        "net_value": inc.net_value,
                        "burst_buy_lots": inc.burst_buy_lots,
                        "burst_sell_lots": inc.burst_sell_lots,
                        "burst_net_value": inc.burst_net_value,
                        "turnover_value": inc.turnover_value,
                        "last_price": inc.last_price,
                    }
                    for inc in increments
                ]
            )

        self.store.save_quote_state(self.tracker.state(trade_date))
        return stats

    # ---------- loop ----------

    def run(self, *, once: bool = False, ignore_session: bool = False) -> None:
        """持續輪詢直到收盤.

        Parameters
        ----------
        ignore_session:
            忽略「是否在交易時段」的判斷。用於測試，或想在盤後空跑看看
            管線是否正常。
        """
        interval = float(self.config.get("poll.interval_seconds", 60))
        trade_date = now_taipei().date().isoformat()
        restored = self.restore_state(trade_date)
        if restored:
            log.info("還原 %d 檔的前次快照", restored)

        while True:
            started = time.monotonic()

            if not ignore_session and not is_session_open():
                log.info("非交易時段，停止輪詢")
                return

            stats = self.poll_once(trade_date)
            log.info(
                "輪詢完成: %d 批 / %d 檔報價 / %d 筆增量%s",
                stats.batches,
                stats.quotes,
                stats.increments,
                f" / {len(stats.errors)} 個錯誤" if stats.errors else "",
            )
            for err in stats.errors[:3]:
                log.warning("  %s", err)

            if once:
                return

            # 扣掉這一輪實際花掉的時間，讓節奏維持在 interval 上
            elapsed = time.monotonic() - started
            time.sleep(max(interval - elapsed, 1.0))
