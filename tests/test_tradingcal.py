"""target_eod_day() / eod_data_ready() 的測試.

實測踩過兩次（2026-10-05、2026-10-12）：twflow-eod.yml 的排程延遲到
隔天凌晨才真正執行，當時「現在時刻 >= 16:00」這個比較，把「上一個
交易日早就就緒」誤判成「今天還沒到 16:00」，整天的校準資料因此空掉。
這裡直接用構造的時間點驗證跨午夜後算出來的目標日是正確的那一天。
"""

import datetime as dt

from twflow.tradingcal import TAIPEI, eod_data_ready, target_eod_day

# 2026-10-12 是週一、2026-10-13 是週二，兩天都是平常交易日，
# 中間沒有週末或假日可以干擾判斷。
MON = dt.date(2026, 10, 12)
TUE = dt.date(2026, 10, 13)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TAIPEI)


class TestTargetEodDay:
    def test_after_1600_on_a_trading_day_targets_today(self):
        assert target_eod_day(at(TUE, 16, 10)) == TUE

    def test_before_1600_on_a_trading_day_targets_previous_trading_day(self):
        assert target_eod_day(at(TUE, 15, 0)) == MON

    def test_delayed_past_midnight_still_targets_the_prior_trading_day(self):
        # 排程原本該在週一 16:10 跑，結果延遲到週二凌晨 01:46 才真正執行——
        # 這是實測發生過的狀況。週二本身也是交易日，但還沒到 16:00，
        # 正確目標應該是週一（早就收盤、資料早就就緒），不是週二。
        assert target_eod_day(at(TUE, 1, 46)) == MON

    def test_delayed_several_hours_past_midnight_same_result(self):
        assert target_eod_day(at(TUE, 2, 10)) == MON

    def test_weekend_targets_previous_trading_day(self):
        saturday = dt.date(2026, 10, 17)
        assert target_eod_day(at(saturday, 20, 0)) == dt.date(2026, 10, 16)


class TestEodDataReady:
    def test_always_ready_once_target_day_logic_handles_it(self):
        # target_eod_day 本身保證選到的日期一定就緒，eod_data_ready 不該
        # 再用「現在時刻」自己重算一次、重蹈跨午夜誤判的覆轍。
        assert eod_data_ready(at(TUE, 1, 46)) is True
        assert eod_data_ready(at(TUE, 16, 10)) is True
        assert eod_data_ready(at(TUE, 8, 0)) is True
