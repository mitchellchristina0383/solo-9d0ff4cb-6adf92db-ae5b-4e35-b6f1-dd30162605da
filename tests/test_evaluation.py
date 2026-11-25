"""核心判定逻辑单元测试：跨桶、观测缺口、无效样本对、达标率上下界。"""

import unittest

from app.evaluation import (
    Histogram,
    HistogramError,
    evaluate_overall,
    evaluate_pair,
    parse_histogram,
)


def H(bounds, counts):
    return Histogram(tuple(float(b) for b in bounds), tuple(counts))


class TestParse(unittest.TestCase):
    def test_valid_dict_form(self):
        h = parse_histogram({"bounds": [10, 20], "counts": [1, 2]}, "x")
        self.assertEqual(h.bounds, (10.0, 20.0))
        self.assertEqual(h.counts, (1, 2))

    def test_valid_rows_form(self):
        h = parse_histogram([{"bound": 1, "count": 2}], "x")
        self.assertEqual(h.bounds, (1.0,))

    def test_empty_rejected(self):
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [], "counts": []}, "x")

    def test_length_mismatch(self):
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [1], "counts": [1, 2]}, "x")

    def test_bounds_must_increase(self):
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [10, 10], "counts": [1, 2]}, "x")
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [10, 5], "counts": [1, 2]}, "x")

    def test_negative_or_non_integer_counts(self):
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [10], "counts": [-1]}, "x")
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [10], "counts": [1.5]}, "x")
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [10], "counts": [True]}, "x")

    def test_bound_must_be_finite_non_negative(self):
        with self.assertRaises(HistogramError):
            parse_histogram({"bounds": [-1], "counts": [1]}, "x")


class TestPairEvaluation(unittest.TestCase):
    def test_mixed_buckets_crossing_is_unknown(self):
        # L=150。桶 (...,50]、(50,100] 全达标；(100,200] 跨越 L => 未知；
        # (200,500] 整体超限 => 坏。
        start = H([50, 100, 200, 500], [10, 20, 30, 40])
        end = H([50, 100, 200, 500], [60, 120, 200, 240])
        pair = evaluate_pair("i1", "b1", "b1", start, end, 150.0)
        self.assertTrue(pair.valid)
        self.assertEqual(pair.total, 200)  # 最后桶累计差值 240 - 40
        self.assertEqual(pair.good_lower, 100)  # 区间桶 0、1 全在 L 之下
        self.assertEqual(pair.bad_lower, 30)  # 末桶 (200,500] 全超限
        classes = [b.classification for b in pair.buckets]
        self.assertEqual(classes, ["good", "good", "crossing", "bad"])
        # 各区间桶内窗口请求数：50、50、70（跨 L）、30
        self.assertEqual([b.count for b in pair.buckets], [50, 50, 70, 30])
        self.assertEqual(pair.unknown, 70)

    def test_lower_bound_equal_to_limit_is_bad_bucket(self):
        # 桶 (100,200] 的下界恰好等于 L=100：桶内请求全部 > L => 超限桶
        start = H([100, 200], [0, 0])
        end = H([100, 200], [7, 9])
        pair = evaluate_pair("i", "b", "b", start, end, 100.0)
        self.assertEqual(
            [b.classification for b in pair.buckets], ["good", "bad"])
        self.assertEqual(pair.good_lower, 7)
        self.assertEqual(pair.bad_lower, 2)
        self.assertEqual(pair.unknown, 0)

    def test_restart_invalidates_pair(self):
        start = H([100], [5])
        end = H([100], [9])
        pair = evaluate_pair("i", "boot-1", "boot-2", start, end, 100.0)
        self.assertFalse(pair.valid)
        self.assertEqual(pair.total, 0)
        self.assertIn("启动标识", pair.reason)

    def test_bounds_mismatch_invalidates(self):
        start = H([100], [5])
        end = H([200], [9])
        pair = evaluate_pair("i", "b", "b", start, end, 100.0)
        self.assertFalse(pair.valid)
        self.assertIn("桶界", pair.reason)

    def test_negative_delta_invalidates(self):
        start = H([100], [5])
        end = H([100], [4])
        pair = evaluate_pair("i", "b", "b", start, end, 100.0)
        self.assertFalse(pair.valid)
        self.assertIn("负", pair.reason)

    def test_monotonic_break_invalidates(self):
        # 桶0 差值 10，桶1 差值 5：累计单调性被破坏
        start = H([100, 200], [0, 10])
        end = H([100, 200], [10, 15])
        pair = evaluate_pair("i", "b", "b", start, end, 500.0)
        self.assertFalse(pair.valid)
        self.assertIn("单调", pair.reason)

    def test_boundary_equality_l_equals_bound(self):
        # L 恰好等于桶界：该桶整体算达标
        start = H([100], [0])
        end = H([100], [7])
        pair = evaluate_pair("i", "b", "b", start, end, 100.0)
        self.assertEqual(pair.buckets[0].classification, "good")
        self.assertEqual(pair.good_lower, 7)

    def test_different_instances_may_use_different_bounds(self):
        p1 = evaluate_pair("a", "b", "b", H([100], [0]), H([100], [5]), 100.0)
        p2 = evaluate_pair("b", "b", "b", H([50, 150], [0, 0]), H([50, 150], [3, 8]), 100.0)
        self.assertTrue(p1.valid and p2.valid)
        self.assertEqual(p2.buckets[1].classification, "crossing")


class TestOverall(unittest.TestCase):
    def _pair(self, iid, start, end, markers=("b", "b")):
        return evaluate_pair(iid, markers[0], markers[1], start, end, 100.0)

    def test_pass_when_lower_bound_meets_target(self):
        # N=10，目标 80%。桶 (50,100] 达标 8，跨 L 桶 (100,200] 未知 1，未覆盖 1
        # 下界 0.8 => 达标；上界 1.0
        p = self._pair("i", H([50, 200], [0, 0]), H([50, 200], [8, 9]))
        result = evaluate_overall([p], 10, 0.8)
        self.assertEqual(result.coverage, 9)
        self.assertEqual(result.good_lower, 8)
        self.assertEqual(result.good_upper, 10)  # 8 + 1 跨桶 + 1 未覆盖
        self.assertEqual(result.decision, "pass")

    def test_fail_when_upper_bound_below_target(self):
        # L=80、N=10、目标 90%。桶 (50,90] 跨越 L => 不能这样构造；
        # 改为：达标桶 (...,50] 7 个；桶 (90,200] 整体在 L 之上 => 超限 2；
        # 未覆盖 1；无跨桶。上界 = (7 + 0 + 1)/10 = 0.8 < 0.9 => 未达标
        p = evaluate_pair(
            "i", "b", "b",
            H([50, 90, 200], [0, 0, 0]), H([50, 90, 200], [7, 7, 9]),
            latency_limit=80.0,
        )
        result = evaluate_overall([p], 10, 0.9)
        self.assertEqual(result.decision, "fail")
        self.assertEqual(result.good_upper, 8)
        self.assertEqual(result.bad_lower, 2)

    def test_inconclusive_when_target_inside_interval(self):
        # 达标 8，跨桶未知 1，覆盖 9；N=10，目标 90%
        # 下界 0.8 < 0.9 <= 上界 1.0 => 待确认
        p = self._pair("i", H([50, 200], [0, 0]), H([50, 200], [8, 9]))
        result = evaluate_overall([p], 10, 0.9)
        self.assertEqual(result.decision, "inconclusive")

    def test_invalid_pair_contributes_no_coverage(self):
        good = self._pair("ok", H([100], [0]), H([100], [5]))
        bad = self._pair("x", H([100], [0]), H([100], [9]), markers=("b1", "b2"))
        result = evaluate_overall([good, bad], 10, 0.5)
        self.assertEqual(result.coverage, 5)
        self.assertEqual(result.unknown, 5)  # 全部未覆盖
        self.assertEqual(result.decision, "pass")  # 下界 5/10 = 0.5

    def test_no_data_without_config(self):
        result = evaluate_overall([], None, None)
        self.assertEqual(result.decision, "no_data")

    def test_all_above_limit_fails(self):
        p = self._pair("i", H([200, 500], [0, 0]), H([200, 500], [3, 5]))
        result = evaluate_overall([p], 5, 0.5)
        self.assertEqual(result.good_lower, 0)
        self.assertEqual(result.good_upper, 0)  # 未覆盖为 0，无跨桶
        self.assertEqual(result.decision, "fail")


if __name__ == "__main__":
    unittest.main()
