"""SQLite 儲存層.

設計取捨：盤中原始快照（每 5–60 秒一筆 × 1800 檔）量太大且沒有分析價值，
所以只保留「每分鐘彙總」的資金流（``flow_minute``），原始快照僅在記憶體中
作為計算增量的前一狀態，另存一份 ``quote_state`` 供程式重啟後接續。

每分鐘一列 × 1800 檔 × 270 分鐘 ≈ 每個交易日 50 萬列，SQLite 完全吃得下，
``purge_old`` 依設定的保留天數清理。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 證券基本資料：代號、名稱、市場別、官方產業別、自訂細分板塊
CREATE TABLE IF NOT EXISTS securities (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    market      TEXT NOT NULL DEFAULT 'TWSE',   -- TWSE / TPEX
    industry    TEXT NOT NULL DEFAULT '',        -- 官方產業別
    sector      TEXT NOT NULL DEFAULT '',        -- 自訂細分板塊
    sector_src  TEXT NOT NULL DEFAULT 'official',-- official / custom
    updated_at  TEXT NOT NULL DEFAULT ''
);

-- 盤中每分鐘資金流推估（單位：張 / 元）
-- burst_* = 該分鐘內成交量超過門檻的「爆量區間」，用來近似法人大額進出。
-- 注意：MIS 是快照而非逐筆，無法辨識個別大單，只能辨識爆量區間。
CREATE TABLE IF NOT EXISTS flow_minute (
    trade_date     TEXT NOT NULL,
    code           TEXT NOT NULL,
    minute_ts      TEXT NOT NULL,   -- ISO8601，分鐘對齊
    buy_lots       REAL NOT NULL DEFAULT 0,
    sell_lots      REAL NOT NULL DEFAULT 0,
    net_value      REAL NOT NULL DEFAULT 0,
    burst_buy_lots   REAL NOT NULL DEFAULT 0,
    burst_sell_lots  REAL NOT NULL DEFAULT 0,
    burst_net_value  REAL NOT NULL DEFAULT 0,
    turnover_value REAL NOT NULL DEFAULT 0,
    last_price     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, code, minute_ts)
);
CREATE INDEX IF NOT EXISTS idx_flow_date ON flow_minute(trade_date, minute_ts);

-- 輪詢狀態：重啟後接續計算成交量增量用
CREATE TABLE IF NOT EXISTS quote_state (
    code        TEXT PRIMARY KEY,
    trade_date  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    price       REAL NOT NULL DEFAULT 0,
    cum_volume  REAL NOT NULL DEFAULT 0,
    bid1        REAL NOT NULL DEFAULT 0,
    ask1        REAL NOT NULL DEFAULT 0
);

-- 盤後官方三大法人買賣超（單位：股）
CREATE TABLE IF NOT EXISTS insti_daily (
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    market       TEXT NOT NULL DEFAULT 'TWSE',
    foreign_net  REAL NOT NULL DEFAULT 0,   -- 外資及陸資
    trust_net    REAL NOT NULL DEFAULT 0,   -- 投信
    dealer_net   REAL NOT NULL DEFAULT 0,   -- 自營商
    total_net    REAL NOT NULL DEFAULT 0,   -- 三大法人合計
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_insti_date ON insti_daily(trade_date);

-- 期貨三大法人未平倉（單位：口）
CREATE TABLE IF NOT EXISTS futures_oi (
    trade_date  TEXT NOT NULL,
    contract    TEXT NOT NULL,
    party       TEXT NOT NULL,   -- 外資 / 投信 / 自營商
    long_oi     REAL NOT NULL DEFAULT 0,
    short_oi    REAL NOT NULL DEFAULT 0,
    net_oi      REAL NOT NULL DEFAULT 0,
    net_value   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, contract, party)
);

-- 券商分點進出（選配，單位：股）
CREATE TABLE IF NOT EXISTS broker_branch (
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    broker_id    TEXT NOT NULL,
    broker_name  TEXT NOT NULL DEFAULT '',
    buy_shares   REAL NOT NULL DEFAULT 0,
    sell_shares  REAL NOT NULL DEFAULT 0,
    net_shares   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, code, broker_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_date ON broker_branch(trade_date);

-- 外資及陸資持股比率（盤後）
CREATE TABLE IF NOT EXISTS foreign_holding (
    trade_date     TEXT NOT NULL,
    code           TEXT NOT NULL,
    foreign_ratio  REAL NOT NULL DEFAULT 0,
    issued_shares  REAL NOT NULL DEFAULT 0,
    foreign_shares REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, code)
);

-- 校準結果：推估 vs 真實
CREATE TABLE IF NOT EXISTS calibration (
    trade_date  TEXT NOT NULL,
    scope       TEXT NOT NULL,   -- stock / sector / market
    key         TEXT NOT NULL,
    est_net     REAL NOT NULL DEFAULT 0,
    real_net    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, scope, key)
);

-- 每檔的校準係數（由歷史迴歸得出，隔日盤中修正推估值用）
CREATE TABLE IF NOT EXISTS calibration_coef (
    code        TEXT PRIMARY KEY,
    coef        REAL NOT NULL DEFAULT 1.0,
    samples     INTEGER NOT NULL DEFAULT 0,
    r2          REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT ''
);

-- 每日整體推估準確度（橫斷面等級相關）
CREATE TABLE IF NOT EXISTS accuracy_daily (
    trade_date  TEXT PRIMARY KEY,
    spearman    REAL NOT NULL DEFAULT 0,
    sign_match  REAL NOT NULL DEFAULT 0,   -- 方向一致比例
    n_stocks    INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    """SQLite 封裝。所有寫入都是 upsert，重跑同一天不會產生重複資料."""

    def __init__(self, path: str | Path = "data/twflow.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- securities ----------

    def upsert_securities(self, rows: Iterable[dict]) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        payload = [
            (
                r["code"],
                r.get("name", ""),
                r.get("market", "TWSE"),
                r.get("industry", ""),
                r.get("sector", ""),
                r.get("sector_src", "official"),
                now,
            )
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO securities (code, name, market, industry, sector, sector_src, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, market=excluded.market,
                 industry=excluded.industry, sector=excluded.sector,
                 sector_src=excluded.sector_src, updated_at=excluded.updated_at""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def securities(self, market: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM securities"
        args: Sequence = ()
        if market:
            sql += " WHERE market = ?"
            args = (market,)
        return list(self.conn.execute(sql + " ORDER BY code", args))

    # ---------- intraday flow ----------

    def add_flow_minute(self, rows: Iterable[dict]) -> int:
        """累加每分鐘資金流（同一分鐘多次輪詢會累加，而非覆蓋）."""
        payload = [
            (
                r["trade_date"], r["code"], r["minute_ts"],
                r.get("buy_lots", 0.0), r.get("sell_lots", 0.0), r.get("net_value", 0.0),
                r.get("burst_buy_lots", 0.0), r.get("burst_sell_lots", 0.0),
                r.get("burst_net_value", 0.0), r.get("turnover_value", 0.0),
                r.get("last_price", 0.0),
            )
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO flow_minute (trade_date, code, minute_ts, buy_lots, sell_lots,
                   net_value, burst_buy_lots, burst_sell_lots, burst_net_value, turnover_value, last_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, code, minute_ts) DO UPDATE SET
                 buy_lots       = flow_minute.buy_lots       + excluded.buy_lots,
                 sell_lots      = flow_minute.sell_lots      + excluded.sell_lots,
                 net_value      = flow_minute.net_value      + excluded.net_value,
                 burst_buy_lots   = flow_minute.burst_buy_lots   + excluded.burst_buy_lots,
                 burst_sell_lots  = flow_minute.burst_sell_lots  + excluded.burst_sell_lots,
                 burst_net_value  = flow_minute.burst_net_value  + excluded.burst_net_value,
                 turnover_value = flow_minute.turnover_value + excluded.turnover_value,
                 last_price     = excluded.last_price""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def flow_rows(self, trade_date: str, since_ts: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM flow_minute WHERE trade_date = ?"
        args: list = [trade_date]
        if since_ts:
            sql += " AND minute_ts >= ?"
            args.append(since_ts)
        return list(self.conn.execute(sql + " ORDER BY minute_ts", args))

    def save_quote_state(self, rows: Iterable[dict]) -> None:
        payload = [
            (r["code"], r["trade_date"], r["ts"], r.get("price", 0.0),
             r.get("cum_volume", 0.0), r.get("bid1", 0.0), r.get("ask1", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO quote_state (code, trade_date, ts, price, cum_volume, bid1, ask1)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 trade_date=excluded.trade_date, ts=excluded.ts, price=excluded.price,
                 cum_volume=excluded.cum_volume, bid1=excluded.bid1, ask1=excluded.ask1""",
            payload,
        )
        self.conn.commit()

    def load_quote_state(self, trade_date: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM quote_state WHERE trade_date = ?", (trade_date,)
        )
        return {r["code"]: r for r in rows}

    # ---------- end-of-day ----------

    def upsert_insti_daily(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["trade_date"], r["code"], r.get("market", "TWSE"),
             r.get("foreign_net", 0.0), r.get("trust_net", 0.0),
             r.get("dealer_net", 0.0), r.get("total_net", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO insti_daily (trade_date, code, market, foreign_net, trust_net, dealer_net, total_net)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, code) DO UPDATE SET
                 market=excluded.market, foreign_net=excluded.foreign_net,
                 trust_net=excluded.trust_net, dealer_net=excluded.dealer_net,
                 total_net=excluded.total_net""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def insti_daily(self, trade_date: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM insti_daily WHERE trade_date = ?", (trade_date,))
        )

    def upsert_foreign_holding(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["trade_date"], r["code"], r.get("foreign_ratio", 0.0),
             r.get("issued_shares", 0.0), r.get("foreign_shares", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO foreign_holding (trade_date, code, foreign_ratio, issued_shares, foreign_shares)
               VALUES (?,?,?,?,?)
               ON CONFLICT(trade_date, code) DO UPDATE SET
                 foreign_ratio=excluded.foreign_ratio, issued_shares=excluded.issued_shares,
                 foreign_shares=excluded.foreign_shares""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def latest_foreign_holding(self) -> dict[str, float]:
        """取每檔最新一筆的外資持股比率."""
        rows = self.conn.execute(
            """SELECT code, foreign_ratio FROM foreign_holding
               WHERE (code, trade_date) IN (
                   SELECT code, MAX(trade_date) FROM foreign_holding GROUP BY code
               )"""
        )
        return {r["code"]: r["foreign_ratio"] for r in rows}

    def latest_insti_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(trade_date) AS d FROM insti_daily").fetchone()
        return row["d"] if row and row["d"] else None

    def futures_oi(self, trade_date: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM futures_oi WHERE trade_date = ? ORDER BY contract, party",
                (trade_date,),
            )
        )

    def latest_futures_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(trade_date) AS d FROM futures_oi").fetchone()
        return row["d"] if row and row["d"] else None

    def broker_branch(self, trade_date: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM broker_branch WHERE trade_date = ?", (trade_date,))
        )

    def latest_broker_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(trade_date) AS d FROM broker_branch").fetchone()
        return row["d"] if row and row["d"] else None

    def latest_flow_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(trade_date) AS d FROM flow_minute").fetchone()
        return row["d"] if row and row["d"] else None

    def security_names(self) -> dict[str, str]:
        return {r["code"]: r["name"] for r in self.conn.execute("SELECT code, name FROM securities")}

    def upsert_futures_oi(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["trade_date"], r["contract"], r["party"], r.get("long_oi", 0.0),
             r.get("short_oi", 0.0), r.get("net_oi", 0.0), r.get("net_value", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO futures_oi (trade_date, contract, party, long_oi, short_oi, net_oi, net_value)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, contract, party) DO UPDATE SET
                 long_oi=excluded.long_oi, short_oi=excluded.short_oi,
                 net_oi=excluded.net_oi, net_value=excluded.net_value""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def upsert_broker_branch(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["trade_date"], r["code"], r["broker_id"], r.get("broker_name", ""),
             r.get("buy_shares", 0.0), r.get("sell_shares", 0.0), r.get("net_shares", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO broker_branch (trade_date, code, broker_id, broker_name,
                   buy_shares, sell_shares, net_shares)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, code, broker_id) DO UPDATE SET
                 broker_name=excluded.broker_name, buy_shares=excluded.buy_shares,
                 sell_shares=excluded.sell_shares, net_shares=excluded.net_shares""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    # ---------- calibration ----------

    def upsert_calibration(self, rows: Iterable[dict]) -> int:
        payload = [
            (r["trade_date"], r["scope"], r["key"], r.get("est_net", 0.0), r.get("real_net", 0.0))
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO calibration (trade_date, scope, key, est_net, real_net)
               VALUES (?,?,?,?,?)
               ON CONFLICT(trade_date, scope, key) DO UPDATE SET
                 est_net=excluded.est_net, real_net=excluded.real_net""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def calibration_history(self, scope: str = "stock", limit_days: int = 60) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM calibration WHERE scope = ?
                   AND trade_date >= date('now', ?) ORDER BY trade_date""",
                (scope, f"-{limit_days} days"),
            )
        )

    def upsert_calibration_coef(self, rows: Iterable[dict]) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        payload = [
            (r["code"], r.get("coef", 1.0), r.get("samples", 0), r.get("r2", 0.0), now)
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO calibration_coef (code, coef, samples, r2, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 coef=excluded.coef, samples=excluded.samples,
                 r2=excluded.r2, updated_at=excluded.updated_at""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def calibration_coefs(self) -> dict[str, float]:
        return {
            r["code"]: r["coef"]
            for r in self.conn.execute("SELECT code, coef FROM calibration_coef")
        }

    def upsert_accuracy(self, trade_date: str, spearman: float, sign_match: float, n: int) -> None:
        self.conn.execute(
            """INSERT INTO accuracy_daily (trade_date, spearman, sign_match, n_stocks)
               VALUES (?,?,?,?)
               ON CONFLICT(trade_date) DO UPDATE SET
                 spearman=excluded.spearman, sign_match=excluded.sign_match,
                 n_stocks=excluded.n_stocks""",
            (trade_date, spearman, sign_match, n),
        )
        self.conn.commit()

    def recent_accuracy(self, days: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM accuracy_daily ORDER BY trade_date DESC LIMIT ?", (days,)
            )
        )

    # ---------- maintenance ----------

    def purge_old(self, keep_days: int = 30) -> int:
        cur = self.conn.execute(
            "DELETE FROM flow_minute WHERE trade_date < date('now', ?)", (f"-{keep_days} days",)
        )
        self.conn.commit()
        return cur.rowcount
