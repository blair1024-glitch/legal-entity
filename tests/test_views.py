"""views.py 的純函式測試.

這些函式是 api.py 與 static_export.py 共用的核心——api.py 已經有
TestClient 端到端測試護航，這裡另外直接測 views 本身，確保 static_export
之後呼叫它們時，不必透過 HTTP 就能拿到一樣正確的資料。
"""

import datetime as dt

import pytest

from twflow import views
from twflow.config import Config
from twflow.httpclient import Fetcher
from twflow.pipeline import run_eod, sync_securities
from twflow.store import Store

DAY = dt.date(2026, 8, 27)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def fetcher():
    return Fetcher(mode="fixture", fixture_dir="fixtures")


@pytest.fixture
def config():
    return Config.load(None)


class TestQuadrantView:
    def test_empty_store_yields_no_points_not_an_error(self, store, config):
        out = views.quadrant_view(store, config)
        assert out["points"] == []
        assert out["estimated"] is True

    def test_has_official_reflects_whether_eod_has_run(self, store, config, fetcher):
        assert views.quadrant_view(store, config)["has_official"] is False
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        assert views.quadrant_view(store, config)["has_official"] is True
        assert views.quadrant_view(store, config)["official_date"] == DAY.isoformat()

    def test_carries_the_disclaimer(self, store, config):
        assert "推估值" in views.quadrant_view(store, config)["disclaimer"]


class TestStocksView:
    def test_sector_filter_narrows_results(self, store, config, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        store.add_flow_minute([
            {"trade_date": DAY.isoformat(), "code": "2330",
             "minute_ts": "2026-08-27T10:00:00", "net_value": 100.0,
             "turnover_value": 1000.0, "last_price": 1000.0},
        ])
        all_stocks = views.stocks_view(store, config, date=DAY.isoformat())
        by_sector = views.stocks_view(store, config, date=DAY.isoformat(), sector="晶圓代工")
        assert by_sector["sector"] == "晶圓代工"
        assert len(by_sector["stocks"]) <= len(all_stocks["stocks"])
        assert all(s["sector"] == "晶圓代工" for s in by_sector["stocks"])


class TestWatchlistView:
    def test_pairs_estimate_with_official(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = views.watchlist_view(store, config, date=DAY.isoformat())
        assert out["items"]
        row = out["items"][0]
        assert "est_net_value" in row
        assert "official" in row


class TestInstitutionalView:
    def test_no_data_yields_empty_not_error(self, store, config):
        out = views.institutional_view(store, config)
        assert out["trade_date"] is None
        assert out["buy"] == []

    def test_marks_as_not_estimated(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = views.institutional_view(store, config, date=DAY.isoformat())
        assert out["estimated"] is False


class TestMetaView:
    def test_counts_securities_and_sectors(self, store, config, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        out = views.meta_view(store, config)
        assert out["securities"] > 0
        assert out["sectors"] > 0
