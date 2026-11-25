"""滚动发布延迟达标判定的纯函数模块。

数据模型
========
一个实例的直方图是一条“累计直方图”：桶界 ``bounds`` 为严格递增的有限非负
浮点数（单位毫秒），``counts`` 为等长的非负整数序列，语义为

    counts[i] = 已观测到的、延迟 <= bounds[i] 的请求累计数。

因此桶 i（最后一个桶除外）覆盖延迟区间 (bounds[i-1], bounds[i]]；最后一个
桶覆盖 (bounds[-2], bounds[-1]]，而延迟大于 bounds[-1] 的请求不出现在任何桶
里（直方图本身存在观测缺口）。

同一实例在窗口首、尾各取一个样本，两者同次启动、桶界一致且每个桶的差值
都是有效非负累计计数（含整体单调）时，差值直方图才计入覆盖；否则整条样本
对都不可用，其窗口内请求全部视为观测缺口（未知）。

未知请求有两个来源：
1. 跨延迟上限 L 的桶——桶内同时存在 <= L 与 > L 的请求，无法拆分；
2. 观测缺口——包括不可用样本对，以及直方图最大桶界之外的请求。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

# 浮点数比较容差：桶界与 L 的比较、桶界一致性都按该容差判定，
# 避免 99.999999999 与 100 这类表示误差影响结论。
EPS = 1e-9


class HistogramError(ValueError):
    """直方图/样本输入不合法。"""


@dataclass(frozen=True)
class Histogram:
    bounds: tuple[float, ...]
    counts: tuple[int, ...]


@dataclass
class BucketContribution:
    """单个桶差值对判定的贡献。"""

    index: int
    upper_bound: float
    count: int
    classification: str  # "good" | "bad" | "crossing"


@dataclass
class PairEvaluation:
    instance_id: str
    valid: bool
    reason: str | None = None
    start_marker: str | None = None
    end_marker: str | None = None
    total: int = 0  # 本样本对覆盖的请求数（最后桶累计差值）
    good_lower: int = 0  # 可确认 <= L 的请求数
    bad_lower: int = 0  # 可确认 > L 的请求数
    unknown: int = 0  # 跨 L 桶的请求数（观测缺口在汇总层统一计算）
    buckets: list[BucketContribution] = field(default_factory=list)


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistogramError(f"{field_name} 必须为非负整数")
    if value < 0:
        raise HistogramError(f"{field_name} 必须为非负整数")
    return value


def parse_histogram(payload: Any, label: str) -> Histogram:
    """校验并解析一次提交里的直方图 JSON。

    接受两种形态：
      {"bounds": [...], "counts": [...]}
      [{"bound": x, "count": n}, ...]
    """
    if isinstance(payload, dict):
        bounds_raw = payload.get("bounds")
        counts_raw = payload.get("counts")
        if not isinstance(bounds_raw, list) or not isinstance(counts_raw, list):
            raise HistogramError(f"{label} 必须包含 bounds 与 counts 两个数组")
    elif isinstance(payload, list):
        bounds_raw, counts_raw = [], []
        for item in payload:
            if not isinstance(item, dict) or "bound" not in item or "count" not in item:
                raise HistogramError(
                    f"{label} 的数组元素必须包含 bound 与 count 字段"
                )
            bounds_raw.append(item["bound"])
            counts_raw.append(item["count"])
    else:
        raise HistogramError(f"{label} 必须是对象或数组")

    if len(bounds_raw) == 0:
        raise HistogramError(f"{label} 至少需要一个桶")
    if len(bounds_raw) != len(counts_raw):
        raise HistogramError(f"{label} 的 bounds 与 counts 长度必须一致")

    bounds: list[float] = []
    for i, raw in enumerate(bounds_raw):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HistogramError(f"{label}.bounds[{i}] 必须是数字")
        bound = float(raw)
        if not math.isfinite(bound) or bound < 0:
            raise HistogramError(f"{label}.bounds[{i}] 必须是有限的非负数")
        if bounds and bound <= bounds[-1] + EPS:
            raise HistogramError(f"{label}.bounds 必须严格递增")
        bounds.append(bound)

    counts = [
        _coerce_int(raw, f"{label}.counts[{i}]") for i, raw in enumerate(counts_raw)
    ]
    return Histogram(tuple(bounds), tuple(counts))


def validate_submission(
    instance_id: Any,
    start_marker: Any,
    end_marker: Any,
    start_payload: Any,
    end_payload: Any,
) -> tuple[str, str, str, Histogram, Histogram]:
    """校验一次实例补录的全部字段，返回规范化后的元组。"""
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise HistogramError("instance_id 必须是非空字符串")
    if not isinstance(start_marker, str) or not start_marker.strip():
        raise HistogramError("start_marker 必须是非空字符串")
    if not isinstance(end_marker, str) or not end_marker.strip():
        raise HistogramError("end_marker 必须是非空字符串")
    start = parse_histogram(start_payload, "start_histogram")
    end = parse_histogram(end_payload, "end_histogram")
    return instance_id.strip(), start_marker.strip(), end_marker.strip(), start, end


def _bounds_equal(a: Sequence[float], b: Sequence[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= EPS for x, y in zip(a, b))


def evaluate_pair(
    instance_id: str,
    start_marker: str,
    end_marker: str,
    start: Histogram,
    end: Histogram,
    latency_limit: float,
) -> PairEvaluation:
    """判定单个实例首尾样本对的贡献。

    不可用条件（整条样本对不计覆盖，全部归入未知/观测缺口）：
      * 首尾启动标识不同（窗口内发生过重启，非同次启动）；
      * 桶界不一致；
      * 任一桶差值为负，或差值不满足累计单调性。
    """
    result = PairEvaluation(
        instance_id=instance_id,
        valid=False,
        start_marker=start_marker,
        end_marker=end_marker,
    )

    if start_marker != end_marker:
        result.reason = "首尾启动标识不同，窗口内可能发生重启"
        return result
    if not _bounds_equal(start.bounds, end.bounds):
        result.reason = "首尾样本桶界不一致"
        return result

    deltas: list[int] = []
    prev_start = 0
    prev_end = 0
    for i, (c0, c1) in enumerate(zip(start.counts, end.counts)):
        # 累计计数直接差值 = 窗口内延迟 <= bounds[i] 的请求数，
        # 仍须保持非负且随桶界单调不减。
        cum_delta = c1 - c0
        if cum_delta < 0:
            result.reason = f"桶 {i} 的首尾差值为负，不是有效累计计数"
            return result
        if cum_delta < (deltas[-1] if deltas else 0):
            result.reason = f"桶 {i} 的差值小于前一桶，累计单调性被破坏"
            return result
        deltas.append(cum_delta)
        # 桶 i 自身覆盖区间 (bounds[i-1], bounds[i]] 内的窗口请求数
        bucket_count = (c1 - prev_end) - (c0 - prev_start)
        if bucket_count < 0:
            result.reason = f"桶 {i} 内请求数为负，累计单调性被破坏"
            return result
        prev_start, prev_end = c0, c1

    result.valid = True
    result.total = deltas[-1]

    for i, (bound, cum_count) in enumerate(zip(end.bounds, deltas)):
        # 桶 i 自身区间 (bounds[i-1], bounds[i]] 内的窗口请求数
        count = cum_count - (deltas[i - 1] if i > 0 else 0)
        # 桶 i 覆盖 (bounds[i-1], bounds[i]]。
        if bound <= latency_limit + EPS:
            classification = "good"
            result.good_lower += count
        elif i == 0 or end.bounds[i - 1] >= latency_limit - EPS:
            # 整个桶都在 L 之上：区间 (b_{i-1}, b_i] 的下界 >= L，
            # 下界恰好等于 L 时桶内请求也全部严格大于 L。
            classification = "bad"
            result.bad_lower += count
        else:
            # 区间下界 < L < 上界：桶跨越延迟上限，无法拆分。
            classification = "crossing"
            result.unknown += count
        result.buckets.append(
            BucketContribution(
                index=i, upper_bound=bound, count=count, classification=classification
            )
        )

    return result


@dataclass
class OverallEvaluation:
    coverage: int
    good_lower: int
    good_upper: int
    bad_lower: int
    unknown: int
    decision: str  # "pass" | "fail" | "inconclusive" | "no_data"
    decision_reason: str
    pairs: list[PairEvaluation]


def evaluate_overall(pairs: Sequence[PairEvaluation], gateway_total: int | None,
                     target_ratio: float | None) -> OverallEvaluation:
    """汇总所有实例样本对，按达标率上下界给出结论。

    覆盖数 C = 各可用样本对最后桶差值之和；
    未知 U = 跨 L 桶请求 + 观测缺口（含不可用样本对的请求无法获知，
    其中没有任何可计数的观测值，因此只在缺口层面体现为“未覆盖”）；
    达标率下界 = good_lower / N，上界 = (good_lower + unknown_observed) / N，
    其中 unknown_observed 仅统计已覆盖请求里的未知部分（跨 L 桶）。
    未覆盖部分 N - C 同样按未知处理，统一并入 good_upper 的分子。
    """
    coverage = sum(p.total for p in pairs if p.valid)
    good_lower = sum(p.good_lower for p in pairs if p.valid)
    bad_lower = sum(p.bad_lower for p in pairs if p.valid)
    crossing_unknown = sum(
        sum(b.count for b in p.buckets if b.classification == "crossing")
        for p in pairs
        if p.valid
    )

    if gateway_total is None or target_ratio is None:
        return OverallEvaluation(
            coverage=coverage,
            good_lower=good_lower,
            good_upper=good_lower,
            bad_lower=bad_lower,
            unknown=crossing_unknown,
            decision="no_data",
            decision_reason="尚未配置网关总数、延迟上限与目标达标率",
            pairs=list(pairs),
        )

    uncovered = max(0, gateway_total - coverage)
    # 上界假设：所有跨桶请求与所有未覆盖请求全部达标。
    good_upper = good_lower + crossing_unknown + uncovered
    unknown_total = crossing_unknown + uncovered

    if coverage == 0 and gateway_total > 0:
        decision = "inconclusive"
        reason = "窗口内没有任何可用样本对，无法判断达标情况"
    else:
        lower_ratio = good_lower / gateway_total
        upper_ratio = good_upper / gateway_total
        if lower_ratio + EPS >= target_ratio:
            decision = "pass"
            reason = (
                f"达标率下界 {lower_ratio:.4%} 已不低于目标 {target_ratio:.4%}"
            )
        elif upper_ratio < target_ratio - EPS:
            decision = "fail"
            reason = (
                f"达标率上界 {upper_ratio:.4%} 仍低于目标 {target_ratio:.4%}"
            )
        else:
            decision = "inconclusive"
            reason = (
                f"达标率落在 [{lower_ratio:.4%}, {upper_ratio:.4%}]，"
                f"目标 {target_ratio:.4%} 位于区间内，需继续补录"
            )

    return OverallEvaluation(
        coverage=coverage,
        good_lower=good_lower,
        good_upper=good_upper,
        bad_lower=bad_lower,
        unknown=unknown_total,
        decision=decision,
        decision_reason=reason,
        pairs=list(pairs),
    )
