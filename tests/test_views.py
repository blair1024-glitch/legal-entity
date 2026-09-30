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

    def test_flags_limit_up_lock_from_quote_state(self, store, config, fetcher):
        # 五檔單邊掛滿（買方排隊、賣方掛不出來）＝鎖漲停，要在排行上標示出來，
        # 不能讓使用者誤以為淨流卡在 0 是「今天沒動靜」。
        sync_securities(store, fetcher, ["TWSE"])
        store.add_flow_minute([
            {"trade_date": DAY.isoformat(), "code": "2330",
             "minute_ts": "2026-08-27T10:00:00", "net_value": 0.0,
             "turnover_value": 1000.0, "last_price": 1000.0},
        ])
        store.save_quote_state([
            {"code": "2330", "trade_date": DAY.isoformat(), "ts": "2026-08-27T10:00:00",
             "price": 1000.0, "cum_volume": 500.0, "bid1": 1000.0, "ask1": 0.0},
        ])
        out = views.stocks_view(store, config, date=DAY.isoformat())
        row = next(s for s in out["stocks"] if s["code"] == "2330")
        assert row["limit_lock"] == "up"

    def test_normal_book_yields_no_lock(self, store, config, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        store.add_flow_minute([
            {"trade_date": DAY.isoformat(), "code": "2330",
             "minute_ts": "2026-08-27T10:00:00", "net_value": 100.0,
             "turnover_value": 1000.0, "last_price": 1000.0},
        ])
        store.save_quote_state([
            {"code": "2330", "trade_date": DAY.isoformat(), "ts": "2026-08-27T10:00:00",
             "price": 1000.0, "cum_volume": 500.0, "bid1": 999.0, "ask1": 1000.0},
        ])
        out = views.stocks_view(store, config, date=DAY.isoformat())
        row = next(s for s in out["stocks"] if s["code"] == "2330")
        assert row["limit_lock"] is None

    def test_no_quote_state_row_yields_no_lock_not_an_error(self, store, config, fetcher):
        # 沒輪詢到五檔（例如重啟後第一筆、或這檔今天沒被輪詢器看過）
        # 一樣要能正常回傳，不該因為缺資料就整個報錯。
        sync_securities(store, fetcher, ["TWSE"])
        store.add_flow_minute([
            {"trade_date": DAY.isoformat(), "code": "2330",
             "minute_ts": "2026-08-27T10:00:00", "net_value": 100.0,
             "turnover_value": 1000.0, "last_price": 1000.0},
        ])
        out = views.stocks_view(store, config, date=DAY.isoformat())
        row = next(s for s in out["stocks"] if s["code"] == "2330")
        assert row["limit_lock"] is None


class TestWatchlistView:
    def test_pairs_estimate_with_official(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = views.watchlist_view(store, config, date=DAY.isoformat())
        assert out["items"]
        row = out["items"][0]
        assert "est_net_value" in row
        assert "official" in row

    def test_flags_limit_lock_for_watchlist_codes(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        configured_code = views.watchlist_view(store, config, date=DAY.isoformat())["items"][0]["code"]
        store.save_quote_state([
            {"code": configured_code, "trade_date": DAY.isoformat(), "ts": "2026-08-27T10:00:00",
             "price": 1000.0, "cum_volume": 500.0, "bid1": 0.0, "ask1": 900.0},
        ])
        out = views.watchlist_view(store, config, date=DAY.isoformat())
        row = next(it for it in out["items"] if it["code"] == configured_code)
        assert row["limit_lock"] == "down"

    def test_no_quote_state_yields_no_lock_not_an_error(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = views.watchlist_view(store, config, date=DAY.isoformat())
        assert out["items"][0]["limit_lock"] is None


class TestWatchlistLookupView:
    def test_empty_store_yields_empty_dict_not_error(self, store, config):
        assert views.watchlist_lookup_view(store, config) == {}

    def test_covers_any_code_with_official_or_ratio_data(self, store, config, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = views.watchlist_lookup_view(store, config)
        assert out
        code, entry = next(iter(out.items()))
        assert "name" in entry
        assert "sector" in entry
        assert "foreign_ratio" in entry
        assert set(entry["official"]) == {"trade_date", "foreign_net", "trust_net", "dealer_net", "total_net"}

    def test_shape_matches_watchlist_view_official_block(self, store, config, fetcher):
        # 這份是給自助新增自選股查任意代號用的，形狀要跟預設清單的
        # watchlist_view() 一致，前端才能共用同一個渲染邏輯。
        sync_securities(store, fetcher, ["TWSE"])
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        lookup = views.watchlist_lookup_view(store, config)
        watchlist = views.watchlist_view(store, config, date=DAY.isoformat())
        configured_code = watchlist["items"][0]["code"]
        assert configured_code in lookup
        assert set(lookup[configured_code]["official"]) == set(watchlist["items"][0]["official"])


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
