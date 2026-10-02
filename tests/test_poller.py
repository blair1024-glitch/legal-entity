"""Poller 的單元測試.

重點場景：MIS 一批回應 200、rtcode 正常、完全沒有拋錯，但 ``msgArray``
裡實際拿到的檔數遠少於請求的檔數——這是 2026-10-02 在 GitHub Actions 上
實測到的真實狀況（一輪 40 批 × 50 檔，最後全市場只拿回不到 300 檔）。
這裡要確保 ``PollStats`` 跟新增的診斷 log 把這個落差如實呈現出來，
不會被誤算成「全部成功」或悄悄蓋掉。
"""

import datetime as dt
import logging

import pytest

from twflow import poller as poller_mod
from twflow.config import Config
from twflow.flow import Quote
from twflow.httpclient import Fetcher
from twflow.poller import Poller
from twflow.store import Store

DAY = dt.date(2026, 10, 2)
TS = dt.datetime(2026, 10, 2, 10, 0, 0)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_securities([
        {"code": "1101", "name": "台泥", "market": "TWSE"},
        {"code": "1102", "name": "亞泥", "market": "TWSE"},
        {"code": "1216", "name": "統一", "market": "TWSE"},
    ])
    yield s
    s.close()


@pytest.fixture
def config():
    return Config.load(None)


@pytest.fixture
def fetcher():
    return Fetcher(mode="fixture", fixture_dir="fixtures")


def q(code: str, price: float = 100.0, cum: float = 10.0) -> Quote:
    return Quote(code=code, ts=TS, price=price, cum_volume=cum, bid1=99.0, ask1=101.0)


class TestPollOnceYieldVisibility:
    def test_requested_reflects_full_universe_on_full_yield(self, store, config, fetcher, monkeypatch):
        monkeypatch.setattr(
            poller_mod.mis, "fetch_batch",
            lambda fetcher, batch: [q(c) for c, _m in batch],
        )
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.requested == 3
        assert stats.quotes == 3

    def test_partial_batch_yield_is_not_masked(self, store, config, fetcher, monkeypatch):
        # 模擬實測到的狀況：一批 3 檔只真的拿回 1 檔，但不是錯誤
        # （沒有拋 FetchError／ParseError）——quotes 要如實反映「少」，
        # 不能被誤判成跟 requested 一樣多，errors 也要維持是空的。
        monkeypatch.setattr(
            poller_mod.mis, "fetch_batch",
            lambda fetcher, batch: [q(batch[0][0])],
        )
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.requested == 3
        assert stats.quotes == 1
        assert stats.errors == []

    def test_missing_codes_are_logged_for_diagnosis(self, store, config, fetcher, monkeypatch, caplog):
        monkeypatch.setattr(
            poller_mod.mis, "fetch_batch",
            lambda fetcher, batch: [q(batch[0][0])],
        )
        p = Poller(store=store, fetcher=fetcher, config=config)
        with caplog.at_level(logging.INFO, logger="twflow.poller"):
            p.poll_once(DAY.isoformat())
        msgs = [r.getMessage() for r in caplog.records]
        assert any("拿回" in m and "缺" in m for m in msgs)

    def test_full_yield_logs_nothing_extra(self, store, config, fetcher, monkeypatch, caplog):
        # 全部拿到時不該有缺量的診斷 log 冒出來干擾判讀
        monkeypatch.setattr(
            poller_mod.mis, "fetch_batch",
            lambda fetcher, batch: [q(c) for c, _m in batch],
        )
        p = Poller(store=store, fetcher=fetcher, config=config)
        with caplog.at_level(logging.INFO, logger="twflow.poller"):
            p.poll_once(DAY.isoformat())
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("缺" in m for m in msgs)
