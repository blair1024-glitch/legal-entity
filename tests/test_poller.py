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

    def test_genuinely_missing_code_stays_missing_after_default_retries(self, store, config, fetcher, monkeypatch):
        # 模擬實測到的狀況：某檔不管第幾輪、批次長什麼樣子，永遠拿不到，
        # 但不是錯誤（沒有拋 FetchError／ParseError）——即使有預設 3 輪
        # 補抓，quotes 最後還是要如實反映「少」，不能被誤判成全部成功，
        # errors 也要維持是空的。
        monkeypatch.setattr(
            poller_mod.mis, "fetch_batch",
            lambda fetcher, batch: [q(c) for c, _m in batch if c != "1102"],
        )
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.requested == 3
        assert stats.quotes == 2
        assert stats.errors == []
        assert stats.passes == 3  # 1102 一直缺，補抓到用完預設的 max_passes

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


class TestPollOnceRetryPasses:
    """實測發現縮小 batch_size 對命中率沒有幫助（見 poller.py 模組說明），
    真正的緩解是對同一輪裡還缺的檔案補抓——這裡測補抓邏輯本身。
    """

    def test_no_extra_pass_when_first_pass_fully_succeeds(self, store, config, fetcher, monkeypatch):
        calls = []

        def fake_fetch_batch(fetcher, batch):
            calls.append(list(batch))
            return [q(c) for c, _m in batch]

        monkeypatch.setattr(poller_mod.mis, "fetch_batch", fake_fetch_batch)
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.passes == 1
        assert len(calls) == 1  # 沒有浪費多餘的請求去補抓

    def test_missing_codes_are_recovered_on_retry_pass(self, store, config, fetcher, monkeypatch):
        # 第一輪只拿到 1101；補抓那一輪請求的就只剩 1102/1216，而且
        # 這次兩檔都拿到——驗證「缺」真的會被重新請求，不是整個universe
        # 重打一次。
        calls = []

        def fake_fetch_batch(fetcher, batch):
            calls.append([c for c, _m in batch])
            if len(calls) == 1:
                return [q("1101")]
            return [q(c) for c, _m in batch]

        monkeypatch.setattr(poller_mod.mis, "fetch_batch", fake_fetch_batch)
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.passes == 2
        assert stats.quotes == 3
        assert calls[0] == ["1101", "1102", "1216"]
        assert sorted(calls[1]) == ["1102", "1216"]  # 補抓只打還缺的

    def test_respects_configured_max_passes_cap(self, store, config, fetcher, monkeypatch):
        config.data["poll"] = {**config.data["poll"], "max_passes": 1}
        calls = []

        def fake_fetch_batch(fetcher, batch):
            calls.append(batch)
            return [q(batch[0][0])]  # 每次都只回第一檔，本來補抓得到

        monkeypatch.setattr(poller_mod.mis, "fetch_batch", fake_fetch_batch)
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.passes == 1
        assert stats.quotes == 1
        assert len(calls) == 1  # max_passes=1 真的沒有再補抓

    def test_failed_batch_is_retried_on_next_pass(self, store, config, fetcher, monkeypatch):
        from twflow.errors import FetchError

        calls = []

        def fake_fetch_batch(fetcher, batch):
            calls.append([c for c, _m in batch])
            if len(calls) == 1:
                raise FetchError("模擬逾時")
            return [q(c) for c, _m in batch]

        monkeypatch.setattr(poller_mod.mis, "fetch_batch", fake_fetch_batch)
        p = Poller(store=store, fetcher=fetcher, config=config)
        stats = p.poll_once(DAY.isoformat())
        assert stats.quotes == 3  # 第一輪整批失敗，第二輪補回來
        assert len(stats.errors) == 1
