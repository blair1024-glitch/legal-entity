"""校準統計的測試.

用**已知相關性**的構造資料驗證統計量算得對——完美正相關要得到 1.0、
完美反相關 -1.0、隨機無關接近 0。這是唯一能驗證這類統計實作的方法。
"""

import pytest

from twflow.calibrate import (
    pearson,
    regression_coef,
    sign_agreement,
    spearman,
)


class TestSpearman:
    def test_perfect_positive_rank_correlation(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative_rank_correlation(self):
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_monotonic_but_nonlinear_still_scores_one(self):
        # 這正是選用等級相關的理由：只要排序一致就算對，不要求線性
        assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)

    def test_handles_ties_with_average_ranks(self):
        assert spearman([1, 1, 2, 2], [1, 1, 2, 2]) == pytest.approx(1.0)

    def test_constant_series_yields_zero_not_nan(self):
        # 全部一樣時變異數為 0，必須回傳 0 而不是除以零
        assert spearman([1, 1, 1], [1, 2, 3]) == 0.0

    def test_too_few_points_yields_zero(self):
        assert spearman([1], [1]) == 0.0

    def test_is_robust_to_a_single_outlier(self):
        # Pearson 會被離群值拉走，Spearman 不會——這就是我們用它的原因
        xs = [1, 2, 3, 4, 5]
        ys = [1, 2, 3, 4, 1000]
        assert spearman(xs, ys) == pytest.approx(1.0)
        assert pearson(xs, ys) < 0.9


class TestSignAgreement:
    def test_all_directions_match(self):
        assert sign_agreement([1, -1, 2], [10, -10, 20]) == 1.0

    def test_no_directions_match(self):
        assert sign_agreement([1, -1], [-10, 10]) == 0.0

    def test_half_match(self):
        assert sign_agreement([1, 1], [10, -10]) == 0.5

    def test_double_zero_pairs_are_excluded_from_the_denominator(self):
        # 兩邊都沒交易不算「猜對方向」
        assert sign_agreement([1, 0], [10, 0]) == 1.0

    def test_all_zero_yields_zero(self):
        assert sign_agreement([0, 0], [0, 0]) == 0.0


class TestRegressionCoef:
    def test_recovers_a_known_scale_factor(self):
        est = [1.0, 2.0, 3.0, 4.0]
        real = [2.0, 4.0, 6.0, 8.0]
        coef, r2 = regression_coef(est, real)
        assert coef == pytest.approx(2.0)
        assert r2 == pytest.approx(1.0)

    def test_identity_when_estimate_equals_reality(self):
        coef, _ = regression_coef([1.0, 2.0], [1.0, 2.0])
        assert coef == pytest.approx(1.0)

    def test_detects_systematic_overestimation(self):
        # 推估一律是實際的兩倍 → 係數應為 0.5，把它壓回來
        est = [10.0, 20.0, 30.0]
        real = [5.0, 10.0, 15.0]
        coef, _ = regression_coef(est, real)
        assert coef == pytest.approx(0.5)

    def test_zero_estimates_do_not_divide_by_zero(self):
        coef, r2 = regression_coef([0.0, 0.0], [1.0, 2.0])
        assert coef == 1.0
        assert r2 == 0.0

    def test_r2_drops_when_fit_is_poor(self):
        _, r2 = regression_coef([1.0, 2.0, 3.0], [3.0, -1.0, 2.0])
        assert r2 < 0.5
