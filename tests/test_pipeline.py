"""run_eod() 的非交易日偵測測試.

實測發現（2026-10-09，國慶補假）：證交所對非交易日查詢，有時用明確的
「非交易日」字樣說明原因（twse_t86 解析後沒有普通股資料的那條路徑），
有時只回通用的「很抱歉，沒有符合條件的資料」（twse_common.extract_table
原樣轉述 stat 欄位的那條路徑）。只比對前者會讓後面這種情況被誤記成
抓取失敗，而不是正常跳過的非交易日。
"""

import datetime as dt

import pytest

from twflow.config import Config
from twflow.errors import ParseError
from twflow.httpclient import Fetcher
from twflow.pipeline import run_eod
from twflow.sources import twse_t86
from twflow.store import Store
from twflow.tradingcal import load_holidays

DAY = dt.date(2026, 10, 9)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def fetcher():
    return Fetcher(mode="fixture", fixture_dir="fixtures")


class TestNonTradingDayDetection:
    def test_generic_no_matching_data_message_marks_holiday(
        self, store, fetcher, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            twse_t86,
            "fetch",
            lambda fetcher, day: (_ for _ in ()).throw(
                ParseError("twse_t86", "證交所回報: 很抱歉，沒有符合條件的資料!")
            ),
        )
        run_eod(store, fetcher, DAY, markets=["TWSE"], with_bsr=False)
        assert DAY in load_holidays()

    def test_explicit_non_trading_day_message_still_marks_holiday(
        self, store, fetcher, monkeypatch, tmp_path
    ):
        # 既有行為不能被這次改動弄壞
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            twse_t86,
            "fetch",
            lambda fetcher, day: (_ for _ in ()).throw(
                ParseError("twse_t86", "解析後沒有任何普通股資料（可能是非交易日）")
            ),
        )
        run_eod(store, fetcher, DAY, markets=["TWSE"], with_bsr=False)
        assert DAY in load_holidays()

    def test_unrelated_failure_does_not_mark_holiday(self, store, fetcher, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            twse_t86,
            "fetch",
            lambda fetcher, day: (_ for _ in ()).throw(
                ParseError("twse_t86", "回應不是合法 JSON: ...")
            ),
        )
        run_eod(store, fetcher, DAY, markets=["TWSE"], with_bsr=False)
        assert DAY not in load_holidays()
