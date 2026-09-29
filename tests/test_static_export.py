from pathlib import Path
"""static_export.py 的測試.

build_export() 是給 GitHub Actions 用的核心：Actions 每次執行都是全新
環境，沒辦法像 api.py 那樣即時回應。這裡驗證匯出的 11 份 JSON 形狀正確，
以及「全部算完才回傳、任一步失敗就整個 raise」這個承諾真的成立——
因為 workflow 的容錯完全依賴這個承諾：export-static 失敗就不寫檔、
不進 commit，Pages 才不會被半成品覆蓋。
"""

import datetime as dt
import json

import pytest

from twflow.config import Config
from twflow.httpclient import Fetcher
from twflow.pipeline import run_eod, sync_securities
from twflow.static_export import QUADRANT_WINDOWS, build_export, write_export
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


class TestBuildExport:
    def test_produces_all_eleven_files(self, store, config):
        outputs = build_export(store, config)
        expected = {f"quadrant_{w}" for w in QUADRANT_WINDOWS} | {
            "stocks_full", "watchlist", "watchlist_lookup", "institutional",
            "futures", "brokers", "accuracy", "meta",
        }
        assert set(outputs) == expected

    def test_watchlist_lookup_covers_codes_beyond_the_configured_list(self, store, config, fetcher):
        # watchlist.json 只有 config 設定的少數幾檔；watchlist_lookup.json
        # 是給前端「使用者自己輸入任意代號」查的，範圍要更廣，不能只是
        # watchlist.json 的另一種格式。
        sync_securities(store, fetcher, ["TWSE"])
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = build_export(store, config, date=DAY.isoformat())
        configured_codes = {it["code"] for it in out["watchlist"]["items"]}
        lookup_codes = set(out["watchlist_lookup"])
        assert len(lookup_codes) > len(configured_codes)

    def test_each_quadrant_file_uses_its_own_window(self, store, config):
        outputs = build_export(store, config)
        for w in QUADRANT_WINDOWS:
            assert outputs[f"quadrant_{w}"]["window_minutes"] == w

    def test_stocks_full_is_not_truncated(self, store, config, fetcher):
        sync_securities(store, fetcher, ["TWSE"])
        rows = [
            {"trade_date": DAY.isoformat(), "code": code,
             "minute_ts": "2026-08-27T10:00:00", "net_value": 100.0,
             "turnover_value": 1000.0, "last_price": 100.0}
            for code in ("2330", "2317", "2454")
        ]
        store.add_flow_minute(rows)
        out = build_export(store, config, date=DAY.isoformat())
        assert len(out["stocks_full"]["stocks"]) == 3
        # sector 篩選交給前端 JS，匯出時不該帶篩選參數
        assert out["stocks_full"]["sector"] is None

    def test_meta_pipeline_flags_are_false_on_a_fresh_empty_store(self, store, config):
        """空 db（等同 Actions 第一次執行、還沒真正連上任何資料源）時，
        兩個旗標都必須是 false——這是前端判斷「首次真實環境執行」的依據。
        """
        out = build_export(store, config)
        pipeline = out["meta"]["pipeline"]
        assert pipeline["ever_fetched_intraday"] is False
        assert pipeline["ever_fetched_official"] is False
        assert pipeline["source"] == "github-actions"

    def test_meta_pipeline_flags_flip_once_data_exists(self, store, config, fetcher):
        run_eod(store, fetcher, DAY, markets=["TWSE"])
        out = build_export(store, config)
        assert out["meta"]["pipeline"]["ever_fetched_official"] is True
        # 這次只跑了 eod，沒有任何盤中資料，這個旗標該還是 false
        assert out["meta"]["pipeline"]["ever_fetched_intraday"] is False

    def test_a_failure_partway_through_raises_rather_than_returning_partial(
        self, store, config, monkeypatch
    ):
        """任一步算失敗要整個 raise，不能吞掉錯誤回傳部分結果——
        呼叫端（workflow）靠這個判斷該不該發布。
        """
        import twflow.static_export as mod

        def boom(*a, **k):
            raise RuntimeError("算壞了")

        monkeypatch.setattr(mod.views, "futures_view", boom)
        with pytest.raises(RuntimeError, match="算壞了"):
            build_export(store, config)


class TestWriteExport:
    def test_writes_one_json_file_per_key(self, store, config, tmp_path):
        outputs = build_export(store, config)
        written = write_export(outputs, tmp_path / "out")
        assert len(written) == len(outputs)
        for path in written:
            assert path.exists()
            # 每個檔案都要是合法 JSON，這是 Pages 前端唯一能依賴的保證
            json.loads(path.read_text("utf-8"))

    def test_creates_the_output_directory(self, store, config, tmp_path):
        out_dir = tmp_path / "nested" / "docs" / "data"
        assert not out_dir.exists()
        write_export(build_export(store, config), out_dir)
        assert out_dir.exists()

    def test_filenames_match_dict_keys(self, store, config, tmp_path):
        outputs = build_export(store, config)
        written = write_export(outputs, tmp_path / "out")
        names = {p.stem for p in written}
        assert names == set(outputs)


class TestSkipIfNotReady:
    """cli.py 的 `eod --skip-if-not-ready` 分支——排程用，非交易日或
    還沒到官方資料通常就緒的時間（16:00 台北）就該直接略過。
    """

    def test_skips_outside_trading_hours(self, monkeypatch, capsys):
        from twflow import cli

        monkeypatch.setattr(cli, "eod_data_ready", lambda: False, raising=False)
        # cmd_eod 內部是 `from .tradingcal import eod_data_ready`（區域匯入），
        # 所以要 patch 的是 tradingcal 模組本身的名稱
        import twflow.tradingcal as tradingcal

        monkeypatch.setattr(tradingcal, "eod_data_ready", lambda: False)

        class Args:
            since = None
            date = None
            skip_if_not_ready = True

        rc = cli.cmd_eod(Args(), Config.load(None))
        assert rc == 0
        assert "略過" in capsys.readouterr().out

    def test_proceeds_when_ready(self, monkeypatch, tmp_path):
        import twflow.tradingcal as tradingcal
        from twflow import cli

        monkeypatch.setattr(tradingcal, "eod_data_ready", lambda: True)
        monkeypatch.chdir(tmp_path)
        # 需要有 fixtures/ 可讀；直接指向專案目錄下的真實 fixtures
        cfg = Config.load(None)
        cfg.data["db_path"] = str(tmp_path / "t.db")
        cfg.data["fixture_dir"] = str(Path(__file__).resolve().parents[1] / "fixtures")

        class Args:
            since = None
            date = "2026-08-27"
            skip_if_not_ready = True
            mode = None

        rc = cli.cmd_eod(Args(), cfg)
        assert rc == 0
        # 真的跑了，不是被跳過
        assert Store(cfg.get("db_path")).insti_daily("2026-08-27")
