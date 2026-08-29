"""端到端整合測試：fixture → parser → 資料庫 → 彙總 → API.

這些測試把整條管線串起來跑，補上單元測試看不到的接縫問題——例如資料庫
欄位名稱和 parser 輸出的鍵對不起來、或 API 回傳的形狀跟前端預期不符。
全程離線，用 ``fixtures/`` 的合成樣本。
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from twflow.api import create_app
from twflow.calibrate import calibrate_day
from twflow.config import Config
from twflow.flow import FlowTracker, Quote
from twflow.httpclient import Fetcher, fixture_key
from twflow.pipeline import run_eod, sync_securities
from twflow.quadrant import compute_quadrants
from twflow.sectors import SectorMap
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


class TestFixtureKey:
    def test_date_params_do_not_change_the_key(self):
        # 昨天錄的樣本今天還要找得到
        a = fixture_key("https://x/T86", {"date": "20260827", "selectType": "ALL"})
        b = fixture_key("https://x/T86", {"date": "20260101", "selectType": "ALL"})
        assert a == b

    def test_mis_stock_list_does_not_change_the_key(self):
        # 否則全市場 36 個批次就要 36 份樣本
        a = fixture_key("https://x/q", {"ex_ch": "tse_2330.tw", "json": "1"})
        b = fixture_key("https://x/q", {"ex_ch": "tse_2317.tw|tse_1101.tw", "json": "1"})
        assert a == b

    def test_meaningful_params_still_change_the_key(self):
        a = fixture_key("https://x/T86", {"selectType": "ALL"})
        b = fixture_key("https://x/T86", {"selectType": "ALLBUT0999"})
        assert a != b

    def test_different_urls_differ(self):
        assert fixture_key("https://x/a") != fixture_key("https://x/b")


class TestPipelineOffline:
    def test_sync_populates_securities_with_industries(self, store, fetcher):
        report = sync_securities(store, fetcher, ["TWSE", "TPEX"])
        assert report.ok, report.render()
        secs = store.securities()
        assert len(secs) >= 6
        by_code = {r["code"]: r for r in secs}
        assert by_code["2330"]["industry"] == "半導體業"
        # 上市與上櫃都要進來
        assert {r["market"] for r in secs} == {"TWSE", "TPEX"}

    def test_sync_applies_custom_sector_over_official_industry(self, store, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        row = {r["code"]: r for r in store.securities()}["2330"]
        # sectors.yaml 把 2330 歸到「晶圓代工」，比官方的「半導體業」更細
        assert row["sector"] == "晶圓代工"
        assert row["sector_src"] == "custom"

    def test_eod_loads_all_official_sources(self, store, fetcher):
        report = run_eod(store, fetcher, DAY, markets=["TWSE", "TPEX"])
        assert report.ok, report.render()

        insti = store.insti_daily(DAY.isoformat())
        assert len(insti) >= 6
        assert {r["market"] for r in insti} == {"TWSE", "TPEX"}

        assert store.latest_foreign_holding()["2330"] > 0
        assert len(store.futures_oi(DAY.isoformat())) > 0

    def test_eod_is_idempotent(self, store, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        first = len(store.insti_daily(DAY.isoformat()))
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        # upsert 而非 append，重跑同一天不該產生重複列
        assert len(store.insti_daily(DAY.isoformat())) == first

    def test_a_failing_source_does_not_abort_the_batch(self, store, fetcher):
        # 期交所 fixture 抽掉後，三大法人與校準仍應完成
        broken = Fetcher(mode="fixture", fixture_dir="does-not-exist")
        report = run_eod(store, broken, DAY, markets=["TWSE"])
        assert not report.ok
        # 每個步驟都有被執行過（沒有因為前面失敗就中斷）
        assert len(report.steps) >= 5


class TestFlowToQuadrant:
    def test_quotes_become_sector_flow(self, store, fetcher):
        """從報價快照一路走到四象限座標."""
        sync_securities(store, fetcher, ["TWSE"])
        smap = SectorMap.load("sectors.yaml", securities=store.securities())

        t0 = dt.datetime(2026, 8, 27, 10, 0)
        t1 = t0 + dt.timedelta(minutes=1)
        tracker = FlowTracker(burst_threshold_lots=100)

        # 兩檔晶圓代工：一檔外盤成交（主動買）、一檔內盤成交（主動賣）
        tracker.seed([
            Quote("2330", t0, 1000.0, 100.0, 999.0, 1001.0),
            Quote("2303", t0, 50.0, 200.0, 49.9, 50.1),
        ])
        incs = tracker.update([
            Quote("2330", t1, 1001.0, 400.0, 1000.0, 1002.0),   # 成交在賣價 → 主動買
            Quote("2303", t1, 49.9, 300.0, 49.8, 50.0),         # 成交在買價 → 主動賣
        ])
        assert len(incs) == 2

        store.add_flow_minute([
            {
                "trade_date": "2026-08-27", "code": i.code, "minute_ts": i.minute_ts,
                "buy_lots": i.buy_lots, "sell_lots": i.sell_lots, "net_value": i.net_value,
                "burst_buy_lots": i.burst_buy_lots, "burst_sell_lots": i.burst_sell_lots,
                "burst_net_value": i.burst_net_value,
                "turnover_value": i.turnover_value, "last_price": i.last_price,
            }
            for i in incs
        ])

        rows = store.flow_rows("2026-08-27")
        assert len(rows) == 2

        pts = compute_quadrants(rows, smap, min_constituents=2)
        sectors = {p.sector: p for p in pts}
        assert "晶圓代工" in sectors
        assert sectors["晶圓代工"].constituents == 2
        # 2330 買 300 張 @1001 遠大於 2303 賣 100 張 @49.9 → 板塊淨流入為正
        assert sectors["晶圓代工"].net_value > 0

    def test_repeated_polls_accumulate_within_a_minute(self, store):
        base = {
            "trade_date": "2026-08-27", "code": "2330",
            "minute_ts": "2026-08-27T10:00:00", "turnover_value": 1000.0,
            "last_price": 1000.0,
        }
        store.add_flow_minute([{**base, "net_value": 100.0}])
        store.add_flow_minute([{**base, "net_value": 50.0}])
        rows = store.flow_rows("2026-08-27")
        assert len(rows) == 1
        assert rows[0]["net_value"] == 150.0        # 累加
        assert rows[0]["turnover_value"] == 2000.0


class TestCalibrationRoundTrip:
    def test_estimate_and_official_data_produce_accuracy(self, store, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])

        # 造一組與官方買賣超同向的推估值
        official = {r["code"]: r["total_net"] for r in store.insti_daily(DAY.isoformat())}
        store.add_flow_minute([
            {
                "trade_date": DAY.isoformat(), "code": code,
                "minute_ts": "2026-08-27T10:00:00",
                "net_value": net * 1000.0,      # 股數 → 概略金額
                "turnover_value": abs(net) * 2000.0,
                "last_price": 1000.0,
            }
            for code, net in official.items()
        ])

        acc = calibrate_day(store, DAY)
        assert acc is not None
        assert acc.n_stocks == len(official)
        # 完全同向的輸入 → 等級相關與方向一致率都應該是滿分
        assert acc.spearman == pytest.approx(1.0)
        assert acc.sign_match == pytest.approx(1.0)


class TestApi:
    @pytest.fixture
    def client(self, store, fetcher, tmp_path):
        sync_securities(store, fetcher, ["TWSE"])
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        store.add_flow_minute([{
            "trade_date": DAY.isoformat(), "code": "2330",
            "minute_ts": "2026-08-27T10:00:00", "net_value": 5_000_000.0,
            "turnover_value": 20_000_000.0, "last_price": 1005.0,
        }, {
            "trade_date": DAY.isoformat(), "code": "2454",
            "minute_ts": "2026-08-27T10:00:00", "net_value": -2_000_000.0,
            "turnover_value": 8_000_000.0, "last_price": 1420.0,
        }])
        cfg = Config.load(None)
        cfg.data["watchlist"] = ["2330", "2454"]
        return TestClient(create_app(store, cfg))

    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"ok": True}

    @pytest.mark.parametrize("path", [
        "/api/meta", "/api/quadrant", "/api/stocks", "/api/watchlist",
        "/api/institutional", "/api/futures", "/api/brokers", "/api/accuracy",
    ])
    def test_endpoints_return_200(self, client, path):
        assert client.get(path).status_code == 200

    def test_intraday_endpoints_always_carry_the_disclaimer(self, client):
        # 免責說明跟著資料走，前端才不會有畫面漏標
        for path in ("/api/quadrant", "/api/stocks", "/api/watchlist"):
            body = client.get(path).json()
            assert "推估值" in body["disclaimer"]

    def test_quadrant_marks_data_as_estimated(self, client):
        body = client.get("/api/quadrant?date=2026-08-27").json()
        assert body["estimated"] is True

    def test_institutional_is_marked_as_not_estimated(self, client):
        # 官方數據不能和推估值混為一談
        body = client.get("/api/institutional").json()
        assert body["estimated"] is False
        assert "官方" in body["note"]

    def test_watchlist_shows_estimate_and_official_side_by_side(self, client):
        items = client.get("/api/watchlist?date=2026-08-27").json()["items"]
        row = {i["code"]: i for i in items}["2330"]
        assert row["est_net_value"] == 5_000_000.0        # 推估
        assert row["official"]["total_net"] is not None    # 官方
        assert row["foreign_ratio"] is not None

    def test_brokers_explains_itself_when_no_data_imported(self, client):
        body = client.get("/api/brokers").json()
        assert body["available"] is False
        assert "BSR" in body["note"]

    def test_quadrant_window_parameter_is_honoured(self, client):
        body = client.get("/api/quadrant?window=15").json()
        assert body["window_minutes"] == 15

    def test_stocks_can_be_filtered_to_one_sector(self, client):
        """從板塊圖點進來時，要能回答「這個板塊是被哪幾檔帶動的」."""
        all_stocks = client.get("/api/stocks").json()["stocks"]
        sectors = {s["sector"] for s in all_stocks}
        assert "晶圓代工" in sectors      # 2330 在 sectors.yaml 裡

        filtered = client.get("/api/stocks?sector=晶圓代工").json()
        assert filtered["sector"] == "晶圓代工"
        assert filtered["stocks"]
        assert {s["sector"] for s in filtered["stocks"]} == {"晶圓代工"}

    def test_unfiltered_stocks_report_no_sector(self, client):
        assert client.get("/api/stocks").json()["sector"] is None

    def test_unknown_sector_yields_empty_list_not_error(self, client):
        res = client.get("/api/stocks?sector=不存在的板塊")
        assert res.status_code == 200
        assert res.json()["stocks"] == []

    def test_sector_filter_still_carries_the_disclaimer(self, client):
        body = client.get("/api/stocks?sector=晶圓代工").json()
        assert "推估值" in body["disclaimer"]

    def test_index_page_is_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "板塊輪動" in res.text


class TestBackfill:
    """盤後資料的區間回補.

    校準係數需要每檔至少 5 個交易日的樣本，一天一天跑太慢，所以要能回補。
    """

    def test_skips_weekends(self, store, fetcher):
        from twflow.pipeline import run_eod_range

        # 2026-08-14(五) → 2026-08-18(二)，中間的 15、16 是週末
        results = run_eod_range(
            store, fetcher, dt.date(2026, 8, 14), dt.date(2026, 8, 18), markets=["TWSE"]
        )
        assert sorted(results) == ["2026-08-14", "2026-08-17", "2026-08-18"]

    def test_one_bad_day_does_not_stop_the_range(self, store, fetcher):
        from twflow.pipeline import run_eod_range

        broken = Fetcher(mode="fixture", fixture_dir="nope")
        results = run_eod_range(
            store, broken, dt.date(2026, 8, 17), dt.date(2026, 8, 19), markets=["TWSE"]
        )
        # 每一天都跑到了，只是全部失敗——不該在第一天就中斷
        assert len(results) == 3
        assert all(not r.ok for r in results.values())

    def test_calls_the_progress_callback_per_day(self, store, fetcher):
        from twflow.pipeline import run_eod_range

        seen = []
        run_eod_range(
            store, fetcher, dt.date(2026, 8, 17), dt.date(2026, 8, 18),
            markets=["TWSE"], on_day=lambda d, r: seen.append(d),
        )
        assert seen == [dt.date(2026, 8, 17), dt.date(2026, 8, 18)]

    def test_single_day_range_works(self, store, fetcher):
        from twflow.pipeline import run_eod_range

        results = run_eod_range(
            store, fetcher, dt.date(2026, 8, 17), dt.date(2026, 8, 17), markets=["TWSE"]
        )
        assert list(results) == ["2026-08-17"]

    def test_range_ending_before_start_yields_nothing(self, store, fetcher):
        from twflow.pipeline import run_eod_range

        assert run_eod_range(
            store, fetcher, dt.date(2026, 8, 20), dt.date(2026, 8, 17), markets=["TWSE"]
        ) == {}


class TestFetchErrorMessages:
    """抓取失敗時，訊息要說得出「為什麼」.

    實機遇到上櫃來源在 doctor 通過、幾分鐘後卻失敗，但訊息只有
    「抓取失敗（重試 3 次）」——分不清是自己網路斷了、對方限流、
    還是端點改版，而這三者的處理方式完全不同。
    """

    def _fetcher(self):
        f = Fetcher(mode="live")
        f.max_retries = 1
        return f

    def test_timeout_is_named(self, monkeypatch):
        import requests

        f = self._fetcher()
        monkeypatch.setattr(
            f.session, "request",
            lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()),
        )
        with pytest.raises(Exception, match="逾時"):
            f.get("https://example.invalid/x")

    def test_connection_error_is_named(self, monkeypatch):
        import requests

        f = self._fetcher()
        monkeypatch.setattr(
            f.session, "request",
            lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
        )
        with pytest.raises(Exception, match="連不上伺服器"):
            f.get("https://example.invalid/x")

    @pytest.mark.parametrize("status,expected", [
        (429, "限流"),
        (503, "暫時無法服務"),
    ])
    def test_retryable_status_codes_explain_themselves(self, monkeypatch, status, expected):
        f = self._fetcher()

        class Resp:
            status_code = status
            url = "https://example.invalid/x"
            text = ""

        monkeypatch.setattr(f.session, "request", lambda *a, **k: Resp())
        with pytest.raises(Exception, match=expected):
            f.get("https://example.invalid/x")

    @pytest.mark.parametrize("status,expected", [
        (403, "被拒絕"),
        (404, "端點不存在"),
    ])
    def test_non_retryable_status_codes_fail_fast_with_reason(self, monkeypatch, status, expected):
        f = self._fetcher()

        class Resp:
            status_code = status
            url = "https://example.invalid/x"
            text = ""

        monkeypatch.setattr(f.session, "request", lambda *a, **k: Resp())
        with pytest.raises(Exception, match=expected):
            f.get("https://example.invalid/x")
