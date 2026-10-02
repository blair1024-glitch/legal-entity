"""盤中輪詢：把 MIS 即時報價轉成資金流並寫進資料庫.

## 輪詢節奏

MIS 的速率限制約 3 requests / 5 秒。上市＋上櫃約 1,800 檔，每批 50 檔就是
36 個請求，一輪大約 60 秒。這是預設節奏——想要更快就把 ``universe`` 設成
``watchlist`` 只掃自選股。

## 為什麼一輪的長度會影響推估品質

輪詢間隔越長，兩次快照之間累積的成交量越多，用「單一成交價」代表整段
區間的誤差就越大。60 秒是資料涵蓋度與推估精度之間的折衷；只掃自選股時
間隔可以縮到 5–10 秒，推估會明顯更準。

## 某些環境（例如 GitHub Actions）命中率偏低，而且跟批次大小無關

2026-10-02 實測發現：在 GitHub Actions runner 上，MIS 單次請求經常回 200、
rtcode 正常、完全沒有錯誤訊息，但 ``msgArray`` 裡的檔數只有請求數的
一成多——而且把 ``batch_size`` 從 50 縮到 10 之後，命中率幾乎沒變
（都在 10-16% 之間），199 批裡每一批都缺、缺的比例也沒有隨批次大小
變化。這代表瓶頸不是「單次請求帶太多檔」，比較像是這個來源 IP（雲端
runner）被 MIS 用某種抽樣／降級方式對待，每一檔被成功回應的機率大概
就是固定的一成多，跟怎麼切批次無關。

既然縮小批次沒用，``poll_once`` 改成對同一輪裡還缺的檔案最多再補抓
``poll.max_passes - 1`` 次——命中率如果真的是獨立的一成多，缺的檔案
補兩輪大概可以把覆蓋率從 15% 拉到快 40%。這不是修好了「為什麼」，
只是在不知道確切原因的情況下，用統計上站得住腳的方式盡量多要到幾檔。
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

    requested: int = 0
    batches: int = 0
    passes: int = 0
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

    def _fetch_pass(
        self, to_fetch: list[tuple[str, str]], batch_size: int, stats: PollStats, pass_num: int
    ) -> tuple[list[Quote], list[tuple[str, str]]]:
        """掃一輪（可能是補抓）的所有批次，回傳 (拿到的報價, 還缺的 code/market)."""
        quotes_out: list[Quote] = []
        still_missing: list[tuple[str, str]] = []
        for batch in mis.batched(to_fetch, batch_size):
            stats.batches += 1
            try:
                quotes = mis.fetch_batch(self.fetcher, batch)
            except (FetchError, ParseError) as exc:
                # 單一批次失敗不該中斷整輪——下一輪會補回來
                stats.errors.append(f"第 {pass_num} 輪 批次 {stats.batches}: {exc}")
                still_missing.extend(batch)
                continue
            # 診斷用：MIS 偶爾會整批回 200、rtcode 正常，但 msgArray 裡的
            # 檔數比請求的少很多，而且沒有任何錯誤訊息可看。實測跟
            # batch_size 無關（50 檔/批跟 10 檔/批命中率都在 10-16%），
            # 逐批記下「請求幾檔、實際拿回幾檔」才看得出真實的缺量比例。
            got = {q.code for q in quotes}
            missing = [(c, m) for c, m in batch if c not in got]
            if missing:
                log.info(
                    "第 %d 輪 批次 %d: 請求 %d 檔／拿回 %d 檔，缺 %d 檔（例如 %s）",
                    pass_num, stats.batches, len(batch), len(quotes), len(missing),
                    ", ".join(c for c, _ in missing[:5]),
                )
            quotes_out.extend(quotes)
            still_missing.extend(missing)
        return quotes_out, still_missing

    def poll_once(self, trade_date: str | None = None) -> PollStats:
        """掃一輪 universe，把資金流增量寫進資料庫.

        單一次掃描命中率可能只有一成多（見模組說明），所以對同一輪裡
        還缺的檔案最多再補抓 ``poll.max_passes - 1`` 次——如果命中率是
        獨立事件，補幾次下來能顯著拉高這一輪實際覆蓋到的檔數。
        """
        trade_date = trade_date or now_taipei().date().isoformat()
        batch_size = int(self.config.get("poll.batch_size", 50))
        max_passes = max(1, int(self.config.get("poll.max_passes", 3)))
        stats = PollStats()

        codes = self.universe()
        if not codes:
            stats.errors.append("universe 是空的——請先執行 `twflow sync` 匯入證券清單")
            return stats
        stats.requested = len(codes)

        all_quotes: list[Quote] = []
        to_fetch = codes
        for pass_num in range(1, max_passes + 1):
            if not to_fetch:
                break
            stats.passes = pass_num
            quotes, to_fetch = self._fetch_pass(to_fetch, batch_size, stats, pass_num)
            all_quotes.extend(quotes)
            if to_fetch and pass_num < max_passes:
                log.info(
                    "第 %d 輪結束：累計拿到 %d／%d 檔，還缺 %d 檔，準備補抓",
                    pass_num, len(all_quotes), stats.requested, len(to_fetch),
                )

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
                "輪詢完成: 請求 %d 檔 / %d 輪 %d 批 / 拿回 %d 檔報價"
                "（命中率 %.0f%%）/ %d 筆增量%s",
                stats.requested,
                stats.passes,
                stats.batches,
                stats.quotes,
                100.0 * stats.quotes / stats.requested if stats.requested else 0.0,
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
