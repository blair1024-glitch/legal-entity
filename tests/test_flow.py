"""資金流分類的單元測試.

策略：用**已知答案的構造輸入**驗證分類邏輯，而不是拿真實行情比對——
真實行情沒有標準答案（我們也不知道哪筆是法人做的），只有構造的輸入
才能斷言「這個成交價在這個五檔下應該算主動買」。
"""

import datetime as dt

import pytest

from twflow.flow import (
    SHARES_PER_LOT,
    FlowTracker,
    Quote,
    buy_ratio_from_book,
    classify,
)

TS0 = dt.datetime(2026, 8, 27, 9, 30, 0)
TS1 = dt.datetime(2026, 8, 27, 9, 30, 5)


def q(code="2330", ts=TS0, price=1000.0, cum=100.0, bid=999.0, ask=1001.0):
    return Quote(code=code, ts=ts, price=price, cum_volume=cum, bid1=bid, ask1=ask)


class TestBuyRatio:
    def test_at_or_above_ask_is_fully_active_buy(self):
        assert buy_ratio_from_book(1001.0, 999.0, 1001.0) == 1.0
        assert buy_ratio_from_book(1005.0, 999.0, 1001.0) == 1.0

    def test_at_or_below_bid_is_fully_active_sell(self):
        assert buy_ratio_from_book(999.0, 999.0, 1001.0) == 0.0
        assert buy_ratio_from_book(990.0, 999.0, 1001.0) == 0.0

    def test_midpoint_splits_evenly(self):
        assert buy_ratio_from_book(1000.0, 999.0, 1001.0) == pytest.approx(0.5)

    def test_proportional_is_linear_between_bid_and_ask(self):
        # 買 100 / 賣 104，成交在 103 → 距離賣價較近，偏主動買
        assert buy_ratio_from_book(103.0, 100.0, 104.0) == pytest.approx(0.75)

    def test_midpoint_neutral_rule_ignores_position(self):
        assert buy_ratio_from_book(103.0, 100.0, 104.0, "midpoint_neutral") == 0.5

    def test_crossed_book_falls_back_to_neutral(self):
        # 賣價低於買價是異常資料，不該讓它產生極端的方向判斷
        assert buy_ratio_from_book(100.0, 105.0, 95.0) == 0.5


class TestClassify:
    def test_no_new_volume_yields_no_increment(self):
        assert classify(q(cum=100.0), q(ts=TS1, cum=100.0)) is None

    def test_volume_going_backwards_yields_no_increment(self):
        # 換日或資料異常時累積量會倒退，不能算成負的成交
        assert classify(q(cum=500.0), q(ts=TS1, cum=100.0)) is None

    def test_zero_price_yields_no_increment(self):
        assert classify(q(cum=100.0), q(ts=TS1, price=0.0, cum=150.0)) is None

    def test_trade_at_ask_is_active_buy(self):
        inc = classify(q(cum=100.0), q(ts=TS1, price=1001.0, cum=150.0))
        assert inc is not None
        assert inc.buy_lots == pytest.approx(50.0)
        assert inc.sell_lots == pytest.approx(0.0)
        assert inc.net_value == pytest.approx(50.0 * 1001.0 * SHARES_PER_LOT)
        assert inc.method == "book"

    def test_trade_at_bid_is_active_sell(self):
        inc = classify(q(cum=100.0), q(ts=TS1, price=999.0, cum=150.0))
        assert inc.sell_lots == pytest.approx(50.0)
        assert inc.buy_lots == pytest.approx(0.0)
        assert inc.net_value == pytest.approx(-50.0 * 999.0 * SHARES_PER_LOT)

    def test_classification_uses_previous_book_not_current(self):
        # 成交發生在「前一刻」掛出的五檔上；用當下五檔分類會有前視偏誤
        prev = q(cum=100.0, bid=999.0, ask=1001.0)
        cur = Quote("2330", TS1, price=1001.0, cum_volume=150.0, bid1=1001.0, ask1=1003.0)
        inc = classify(prev, cur)
        # 對 prev 的 ask=1001 而言是外盤成交 → 全數主動買
        assert inc.buy_lots == pytest.approx(50.0)

    def test_turnover_is_independent_of_direction(self):
        inc = classify(q(cum=100.0), q(ts=TS1, price=999.0, cum=150.0))
        assert inc.turnover_value == pytest.approx(50.0 * 999.0 * SHARES_PER_LOT)

    def test_falls_back_to_tick_rule_without_book(self):
        # 漲停鎖死時賣方五檔會消失
        prev = Quote("2330", TS0, price=1000.0, cum_volume=100.0, bid1=1000.0, ask1=0.0)
        cur = Quote("2330", TS1, price=1010.0, cum_volume=150.0, bid1=1010.0, ask1=0.0)
        inc = classify(prev, cur)
        assert inc.method == "tick"
        assert inc.buy_lots == pytest.approx(50.0)

    def test_zero_tick_reuses_previous_direction(self):
        prev = Quote("2330", TS0, price=1000.0, cum_volume=100.0, bid1=0.0, ask1=0.0)
        cur = Quote("2330", TS1, price=1000.0, cum_volume=150.0, bid1=0.0, ask1=0.0)
        inc = classify(prev, cur, prev_direction=1.0)
        assert inc.buy_lots == pytest.approx(50.0)

    def test_burst_flagged_only_above_threshold(self):
        small = classify(q(cum=100.0), q(ts=TS1, price=1001.0, cum=150.0), burst_threshold_lots=100)
        assert small.burst_net_value == 0.0

        big = classify(q(cum=100.0), q(ts=TS1, price=1001.0, cum=300.0), burst_threshold_lots=100)
        assert big.burst_buy_lots == pytest.approx(200.0)
        assert big.burst_net_value == big.net_value

    def test_minute_key_is_aligned_to_the_minute(self):
        cur = Quote("2330", dt.datetime(2026, 8, 27, 9, 30, 47), 1001.0, 150.0, 999.0, 1001.0)
        inc = classify(q(cum=100.0), cur)
        assert inc.minute_ts == "2026-08-27T09:30:00"


class TestFlowTracker:
    def test_first_observation_produces_no_increment(self):
        # 從盤中才啟動時，開盤到現在的累積量無從分類，只能捨棄
        t = FlowTracker()
        assert t.update([q(cum=100.0)]) == []

    def test_second_observation_produces_increment(self):
        t = FlowTracker()
        t.update([q(cum=100.0)])
        out = t.update([q(ts=TS1, price=1001.0, cum=150.0)])
        assert len(out) == 1
        assert out[0].buy_lots == pytest.approx(50.0)

    def test_seed_avoids_discarding_the_first_interval(self):
        t = FlowTracker()
        t.seed([q(cum=100.0)])
        out = t.update([q(ts=TS1, price=1001.0, cum=150.0)])
        assert len(out) == 1

    def test_tracks_each_code_independently(self):
        t = FlowTracker()
        t.seed([q(code="2330", cum=100.0), q(code="2317", cum=200.0)])
        out = t.update([
            q(code="2330", ts=TS1, price=1001.0, cum=150.0),
            q(code="2317", ts=TS1, price=999.0, cum=260.0),
        ])
        by_code = {i.code: i for i in out}
        assert by_code["2330"].buy_lots == pytest.approx(50.0)
        assert by_code["2317"].sell_lots == pytest.approx(60.0)

    def test_state_roundtrip_shape(self):
        t = FlowTracker()
        t.seed([q(cum=100.0)])
        state = t.state("2026-08-27")
        assert state[0]["code"] == "2330"
        assert state[0]["cum_volume"] == 100.0
