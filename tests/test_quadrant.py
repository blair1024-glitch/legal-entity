"""四象限分類測試.

用構造的資金流序列驗證象限判定——每個案例都設計成「這個板塊在前半段和
後半段各發生了什麼」，然後斷言它應該落在哪一象限。
"""

import datetime as dt

import pytest

from twflow.quadrant import (
    QUADRANT_ACCEL_IN,
    QUADRANT_ACCEL_OUT,
    QUADRANT_SLOWING_IN,
    QUADRANT_SLOWING_OUT,
    QUADRANT_UNKNOWN,
    classify_quadrant,
    compute_quadrants,
    compute_trail,
    rank_stocks,
    resolve_momentum_window,
)
from twflow.sectors import SectorMap, aggregate_by_sector

BASE = dt.datetime(2026, 8, 27, 10, 0, 0)


def smap(mapping=None, official=None):
    m = SectorMap()
    for code, sector in (mapping or {}).items():
        m.by_code[code] = sector
        m.source[code] = "custom"
    for code, sector in (official or {}).items():
        m.by_code[code] = sector
        m.source[code] = "official"
    return m


def row(code, minutes, net, turnover, burst=0.0, price=100.0):
    return {
        "code": code,
        "minute_ts": (BASE + dt.timedelta(minutes=minutes)).isoformat(),
        "net_value": net,
        "turnover_value": turnover,
        "burst_net_value": burst,
        "last_price": price,
    }


class TestClassifyQuadrant:
    def test_inflow_accelerating(self):
        assert classify_quadrant(0.2, 0.1) == QUADRANT_ACCEL_IN

    def test_inflow_slowing(self):
        assert classify_quadrant(0.2, -0.1) == QUADRANT_SLOWING_IN

    def test_outflow_accelerating(self):
        # 賣壓越來越重：強度為負，動能也為負（更負）
        assert classify_quadrant(-0.2, -0.1) == QUADRANT_ACCEL_OUT

    def test_outflow_slowing(self):
        # 還在賣但賣壓收斂：強度為負，動能轉正
        assert classify_quadrant(-0.2, 0.1) == QUADRANT_SLOWING_OUT

    @pytest.mark.parametrize("strength,momentum,expected", [
        (0.0, 0.0, QUADRANT_ACCEL_OUT),
        (0.1, 0.0, QUADRANT_SLOWING_IN),
        (-0.1, 0.0, QUADRANT_ACCEL_OUT),
    ])
    def test_zero_boundaries_lean_conservative(self, strength, momentum, expected):
        # 打平時不宣稱「在加速」——那樣比較容易誤導
        assert classify_quadrant(strength, momentum) == expected


class TestComputeQuadrants:
    def test_accelerating_inflow_is_detected(self):
        # 前 30 分鐘小買，後 30 分鐘大買 → 加速流入
        m = smap({"A1": "測試板塊", "A2": "測試板塊"})
        rows = [
            row("A1", 5, net=10, turnover=1000),
            row("A2", 5, net=10, turnover=1000),
            row("A1", 45, net=300, turnover=1000),
            row("A2", 45, net=300, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert len(pts) == 1
        assert pts[0].quadrant == QUADRANT_ACCEL_IN
        assert pts[0].momentum > 0

    def test_slowing_inflow_is_detected(self):
        # 前段大買、後段小買 → 還是淨流入，但力道減弱
        m = smap({"A1": "測試板塊", "A2": "測試板塊"})
        rows = [
            row("A1", 5, net=400, turnover=1000),
            row("A2", 5, net=400, turnover=1000),
            row("A1", 45, net=20, turnover=1000),
            row("A2", 45, net=20, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].strength > 0
        assert pts[0].momentum < 0
        assert pts[0].quadrant == QUADRANT_SLOWING_IN

    def test_accelerating_outflow_is_detected(self):
        m = smap({"A1": "測試板塊", "A2": "測試板塊"})
        rows = [
            row("A1", 5, net=-10, turnover=1000),
            row("A2", 5, net=-10, turnover=1000),
            row("A1", 45, net=-400, turnover=1000),
            row("A2", 45, net=-400, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].quadrant == QUADRANT_ACCEL_OUT

    def test_slowing_outflow_is_detected(self):
        m = smap({"A1": "測試板塊", "A2": "測試板塊"})
        rows = [
            row("A1", 5, net=-400, turnover=1000),
            row("A2", 5, net=-400, turnover=1000),
            row("A1", 45, net=-10, turnover=1000),
            row("A2", 45, net=-10, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].strength < 0
        assert pts[0].momentum > 0
        assert pts[0].quadrant == QUADRANT_SLOWING_OUT

    def test_strength_is_normalised_so_sectors_are_comparable(self):
        # 大板塊金額大 10 倍但比重相同 → 強度應該一樣，不該霸佔圖的一角
        m = smap({"BIG1": "大", "BIG2": "大", "SML1": "小", "SML2": "小"})
        rows = [
            row("BIG1", 5, net=1000, turnover=10000),
            row("BIG2", 5, net=1000, turnover=10000),
            row("SML1", 5, net=100, turnover=1000),
            row("SML2", 5, net=100, turnover=1000),
        ]
        pts = {p.sector: p for p in compute_quadrants(rows, m, min_constituents=2)}
        assert pts["大"].strength == pytest.approx(pts["小"].strength)

    def test_filters_sectors_with_too_few_constituents(self):
        m = smap({"A1": "單檔板塊", "B1": "雙檔板塊", "B2": "雙檔板塊"})
        rows = [
            row("A1", 5, net=100, turnover=1000),
            row("B1", 5, net=100, turnover=1000),
            row("B2", 5, net=100, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, min_constituents=2)
        assert [p.sector for p in pts] == ["雙檔板塊"]

    def test_filters_sectors_below_turnover_floor(self):
        m = smap({"A1": "冷門", "A2": "冷門"})
        rows = [row("A1", 5, net=1, turnover=10), row("A2", 5, net=1, turnover=10)]
        assert compute_quadrants(rows, m, min_constituents=2, min_turnover=1000) == []

    def test_applies_calibration_coefficients(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [row("A1", 5, net=100, turnover=1000), row("A2", 5, net=100, turnover=1000)]
        plain = compute_quadrants(rows, m, min_constituents=2)[0]
        scaled = compute_quadrants(rows, m, min_constituents=2, calibration={"A1": 0.5})[0]
        assert scaled.net_value == pytest.approx(plain.net_value - 50)
        # 成交值是實測值，不該被校準係數改動
        assert scaled.turnover_value == pytest.approx(plain.turnover_value)

    def test_empty_input_yields_no_points(self):
        assert compute_quadrants([], smap()) == []

    def test_window_uses_latest_timestamp_when_now_absent(self):
        # 收盤後回看歷史某天，視窗要相對於當天最後一筆而不是「現在」
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [
            row("A1", 0, net=10, turnover=1000),
            row("A2", 0, net=10, turnover=1000),
            row("A1", 50, net=500, turnover=1000),
            row("A2", 50, net=500, turnover=1000),
        ]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].momentum > 0


class TestSectorAggregation:
    def test_falls_back_to_official_industry(self):
        m = smap({"2330": "晶圓代工"}, official={"1101": "水泥工業"})
        assert m.sector_of("2330") == "晶圓代工"
        assert m.sector_of("1101") == "水泥工業"
        assert m.is_custom("2330") is True
        assert m.is_custom("1101") is False

    def test_unknown_code_is_not_silently_dropped(self):
        assert smap().sector_of("9999") == "未分類"

    def test_aggregate_counts_distinct_constituents(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [
            row("A1", 5, net=10, turnover=100),
            row("A1", 6, net=10, turnover=100),
            row("A2", 5, net=10, turnover=100),
        ]
        agg = aggregate_by_sector(rows, m)
        assert agg["板塊"].constituents == 2
        assert agg["板塊"].net_value == 30

    def test_sector_strength_handles_zero_turnover(self):
        m = smap({"A1": "板塊"})
        agg = aggregate_by_sector([row("A1", 5, net=0, turnover=0)], m)
        assert agg["板塊"].strength == 0.0


class TestRankStocks:
    def test_ranks_by_net_value_descending(self):
        m = smap({"A": "S", "B": "S", "C": "S"})
        rows = [
            row("A", 1, net=100, turnover=1000),
            row("B", 1, net=-50, turnover=1000),
            row("C", 1, net=300, turnover=1000),
        ]
        out = rank_stocks(rows, m, limit=0)
        assert [r["code"] for r in out] == ["C", "A", "B"]

    def test_accumulates_across_minutes_and_keeps_latest_price(self):
        m = smap({"A": "S"})
        rows = [
            row("A", 1, net=100, turnover=1000, price=10.0),
            row("A", 2, net=50, turnover=500, price=11.0),
        ]
        out = rank_stocks(rows, m, limit=0)
        assert out[0]["net_value"] == 150
        assert out[0]["turnover_value"] == 1500
        assert out[0]["last_price"] == 11.0


class TestMomentumWindowResolution:
    """開盤初期的動能視窗處理.

    這裡防的是一個很有說服力的錯誤：開盤後不到 2W 分鐘時，「前一個視窗」
    是空的，動能會退化成強度本身，於是**所有**淨流入的板塊都被標成
    「加速流入」、所有淨流出的都是「加速流出」——四個象限只剩兩個到得了，
    而畫面看起來完全正常。
    """

    def test_full_window_used_when_history_is_sufficient(self):
        earliest = BASE
        now = BASE + dt.timedelta(minutes=90)
        window, known = resolve_momentum_window(earliest, now, 30)
        assert window == 30.0
        assert known is True

    def test_window_halves_the_available_history_when_short(self):
        # 開盤才 20 分鐘，就比較「近 10 分鐘」與「前 10 分鐘」
        window, known = resolve_momentum_window(BASE, BASE + dt.timedelta(minutes=20), 30)
        assert window == 10.0
        assert known is True

    def test_momentum_unavailable_in_the_first_couple_of_minutes(self):
        window, known = resolve_momentum_window(BASE, BASE + dt.timedelta(minutes=1), 30)
        assert known is False

    def test_exactly_two_windows_of_history_uses_the_full_window(self):
        window, known = resolve_momentum_window(BASE, BASE + dt.timedelta(minutes=60), 30)
        assert window == 30.0

    def test_opening_minutes_are_not_labelled_accelerating(self):
        """回歸測試：開盤 1 分鐘不該把所有板塊標成「加速」."""
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [row(c, t, net=100, turnover=1000)
                for t in range(2) for c in ("A1", "A2")]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].quadrant == QUADRANT_UNKNOWN
        assert pts[0].momentum_known is False
        assert pts[0].momentum == 0.0
        # 強度仍然是可信的——不知道的只有加速度
        assert pts[0].strength > 0

    def test_short_history_still_detects_a_genuine_slowdown(self):
        """開盤 10 分鐘、買盤明顯轉弱 → 應正確判為「流入但放緩」."""
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = []
        for t in range(11):
            net = 300 if t < 5 else 30
            rows += [row("A1", t, net=net, turnover=1000),
                     row("A2", t, net=net, turnover=1000)]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].quadrant == QUADRANT_SLOWING_IN
        assert pts[0].momentum_known is True
        # 有效視窗縮短成 5 分鐘，且要回報出來讓 UI 顯示
        assert pts[0].momentum_window_minutes == pytest.approx(5.0)

    def test_sector_with_no_prior_turnover_is_flagged_individually(self):
        """整體時間夠長，但某板塊前段完全沒成交 → 該板塊動能仍不可信."""
        m = smap({"A1": "活躍", "A2": "活躍", "B1": "冷門", "B2": "冷門"})
        rows = []
        for t in range(0, 120):
            rows += [row("A1", t, net=100, turnover=1000),
                     row("A2", t, net=100, turnover=1000)]
        # 冷門板塊只在最後 10 分鐘有成交
        for t in range(110, 120):
            rows += [row("B1", t, net=100, turnover=1000),
                     row("B2", t, net=100, turnover=1000)]
        pts = {p.sector: p for p in compute_quadrants(rows, m, window_minutes=30,
                                                      min_constituents=2)}
        assert pts["活躍"].momentum_known is True
        assert pts["冷門"].momentum_known is False
        assert pts["冷門"].quadrant == QUADRANT_UNKNOWN

    @pytest.mark.parametrize("switch_at,before,after,expected", [
        (90, 50, 400, QUADRANT_ACCEL_IN),        # 買盤轉強
        (90, 400, 50, QUADRANT_SLOWING_IN),      # 買盤轉弱
        (90, -400, -50, QUADRANT_SLOWING_OUT),   # 賣壓收斂
        (90, -50, -400, QUADRANT_ACCEL_OUT),     # 賣壓加重
    ])
    def test_all_four_quadrants_are_reachable(self, switch_at, before, after, expected):
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [row(c, t, net=(before if t < switch_at else after), turnover=1000)
                for t in range(120) for c in ("A1", "A2")]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].quadrant == expected

    def test_steady_flow_reports_zero_momentum(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = [row(c, t, net=200, turnover=1000) for t in range(120) for c in ("A1", "A2")]
        pts = compute_quadrants(rows, m, window_minutes=30, min_constituents=2)
        assert pts[0].momentum == pytest.approx(0.0)


class TestClassifyQuadrantUnknown:
    def test_unknown_overrides_strength_and_momentum(self):
        assert classify_quadrant(0.5, 0.5, momentum_known=False) == QUADRANT_UNKNOWN
        assert classify_quadrant(-0.5, -0.5, momentum_known=False) == QUADRANT_UNKNOWN

    def test_known_defaults_to_true_for_backwards_compatibility(self):
        assert classify_quadrant(0.2, 0.1) == QUADRANT_ACCEL_IN


class TestRotationTrail:
    """輪動軌跡：板塊在四象限圖上的移動路徑.

    四象限只是某一瞬間的切片，看不出板塊是從哪裡移動過來的——但輪動的
    重點正是移動方向。這裡驗證軌跡點確實是「當時的儀表板會顯示的位置」，
    而不是事後回推的平滑曲線。
    """

    def _rows(self, per_minute, total=180):
        """per_minute(t) 回傳該分鐘的 net_value."""
        return [row(c, t, net=per_minute(t), turnover=1000)
                for t in range(total) for c in ("A1", "A2")]

    def test_returns_points_oldest_first(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        trail = compute_trail(self._rows(lambda t: 100), m,
                              steps=4, step_minutes=10, min_constituents=2)
        path = trail["板塊"]
        assert len(path) == 4
        times = [p["t"] for p in path]
        assert times == sorted(times)

    def test_each_point_matches_a_direct_computation_at_that_time(self):
        """軌跡點必須等於「當時真的會顯示的位置」，不能是事後回推."""
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = self._rows(lambda t: 100 if t < 90 else 400)
        trail = compute_trail(rows, m, steps=3, step_minutes=20,
                              window_minutes=30, min_constituents=2)
        path = trail["板塊"]

        for pt in path:
            cutoff = dt.datetime.fromisoformat(pt["t"])
            upto = [r for r in rows
                    if dt.datetime.fromisoformat(r["minute_ts"]) <= cutoff]
            direct = compute_quadrants(upto, m, now=cutoff,
                                       window_minutes=30, min_constituents=2)[0]
            assert pt["strength"] == pytest.approx(direct.strength, abs=1e-6)
            assert pt["momentum"] == pytest.approx(direct.momentum, abs=1e-6)

    def test_trail_shows_movement_when_flow_changes(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        # 資金流一路增強 → 強度應該單調上升
        rows = self._rows(lambda t: 50 + t * 5)
        path = compute_trail(rows, m, steps=5, step_minutes=20,
                             min_constituents=2)["板塊"]
        strengths = [p["strength"] for p in path]
        assert strengths == sorted(strengths)

    def test_can_restrict_to_selected_sectors(self):
        # 全部板塊都畫線會糊成一團，通常只畫前幾名
        m = smap({"A1": "甲", "A2": "甲", "B1": "乙", "B2": "乙"})
        rows = [row(c, t, net=100, turnover=1000)
                for t in range(60) for c in ("A1", "A2", "B1", "B2")]
        trail = compute_trail(rows, m, steps=3, step_minutes=10,
                              sectors={"甲"}, min_constituents=2)
        assert set(trail) == {"甲"}

    def test_empty_input_yields_empty_trail(self):
        assert compute_trail([], smap(), steps=4) == {}

    def test_zero_steps_yields_empty_trail(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        assert compute_trail(self._rows(lambda t: 100), m, steps=0) == {}

    def test_carries_momentum_confidence_per_point(self):
        """開盤初期的軌跡點要標出動能不可信，不能默默畫成 0."""
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = self._rows(lambda t: 100, total=8)
        path = compute_trail(rows, m, steps=4, step_minutes=2,
                             window_minutes=30, min_constituents=2)["板塊"]
        # 最早的點資料最少，動能必然不可信
        assert path[0]["momentum_known"] is False
        assert path[0]["momentum"] == 0.0

    def test_step_longer_than_available_history_is_tolerated(self):
        m = smap({"A1": "板塊", "A2": "板塊"})
        rows = self._rows(lambda t: 100, total=20)
        # 往回倒帶超出資料範圍的點會被略過，不該爆炸
        trail = compute_trail(rows, m, steps=6, step_minutes=60, min_constituents=2)
        assert all(len(p) >= 1 for p in trail.values())
