"""auto 模式的排程判斷測試.

重點是「在什麼時間點該做什麼」——這關係到使用者會不會漏掉某一天的資料。
時間全部用構造的 datetime，不依賴當下時鐘。
"""

import datetime as dt

import pytest

from twflow.config import Config
from twflow.httpclient import Fetcher
from twflow.scheduler import IDLE_SLEEP_SECONDS, Scheduler
from twflow.store import Store
from twflow.tradingcal import TAIPEI

MON = dt.date(2026, 8, 24)      # 星期一
SAT = dt.date(2026, 8, 22)      # 星期六


def at(day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=TAIPEI)


@pytest.fixture
def sched(tmp_path, monkeypatch):
    # 假日清單改指到 tmp，避免讀到本機的 data/holidays.txt 影響判斷
    monkeypatch.setattr("twflow.tradingcal.load_holidays", lambda *a, **k: set())
    store = Store(tmp_path / "t.db")
    s = Scheduler(store, Fetcher(mode="fixture", fixture_dir="fixtures"), Config.load(None))
    yield s
    store.close()


class TestNextAction:
    @pytest.mark.parametrize("hour,minute", [(9, 0), (10, 30), (13, 30)])
    def test_polls_during_the_session(self, sched, hour, minute):
        action, sleep = sched.next_action(at(MON, hour, minute))
        assert action == "poll"
        assert sleep == 0.0

    def test_idles_before_the_open(self, sched):
        action, sleep = sched.next_action(at(MON, 7, 0))
        assert action == "idle"
        assert sleep > 0

    def test_idles_between_close_and_eod_readiness(self, sched):
        # 13:30 收盤，但 T86 要 15:00-16:00 才發布——這段時間不該急著抓
        action, _ = sched.next_action(at(MON, 14, 0))
        assert action == "idle"

    def test_runs_eod_once_official_data_should_be_out(self, sched):
        action, sleep = sched.next_action(at(MON, 16, 30))
        assert action == "eod"
        assert sleep == 0.0

    def test_does_not_repeat_eod_once_done(self, sched):
        sched.store.upsert_insti_daily([
            {"trade_date": MON.isoformat(), "code": "2330", "total_net": 1000}
        ])
        action, _ = sched.next_action(at(MON, 16, 30))
        assert action == "idle"

    def test_idles_all_day_on_a_weekend(self, sched):
        for hour in (9, 12, 16, 20):
            action, sleep = sched.next_action(at(SAT, hour))
            assert action == "idle"
            assert sleep == IDLE_SLEEP_SECONDS

    def test_sleep_before_open_is_capped(self, sched):
        # 半夜三點不該一路睡到開盤——中斷訊號要能及時反應
        _, sleep = sched.next_action(at(MON, 3, 0))
        assert sleep <= IDLE_SLEEP_SECONDS

    def test_eod_completion_is_read_from_the_database(self, sched):
        """重啟後要能正確判斷今天的盤後跑過了沒."""
        assert sched.eod_done(MON) is False
        sched.store.upsert_insti_daily([
            {"trade_date": MON.isoformat(), "code": "2330", "total_net": 1}
        ])
        assert sched.eod_done(MON) is True

    def test_eod_completion_is_per_day(self, sched):
        sched.store.upsert_insti_daily([
            {"trade_date": MON.isoformat(), "code": "2330", "total_net": 1}
        ])
        assert sched.eod_done(MON + dt.timedelta(days=1)) is False


class TestRunLoop:
    def test_runs_eod_when_the_clock_says_so(self, sched, monkeypatch):
        monkeypatch.setattr("twflow.scheduler.now_taipei", lambda: at(MON, 16, 30))
        sched.run(max_iterations=1)
        # 盤後流程確實跑過了（fixture 模式下會寫入三大法人資料）
        assert sched.eod_done(MON) is True

    def test_idle_iteration_does_nothing(self, sched, monkeypatch):
        monkeypatch.setattr("twflow.scheduler.now_taipei", lambda: at(SAT, 11, 0))
        sched.run(max_iterations=2)
        assert sched.eod_done(SAT) is False
