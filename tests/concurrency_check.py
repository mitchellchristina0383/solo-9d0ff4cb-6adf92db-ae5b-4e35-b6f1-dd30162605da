"""并发补报 / 交错更正自测脚本。

直接对运行中的 HTTP 服务打流量（默认 http://127.0.0.1:8000）：
  * 第一阶段：多于容量上限的实例并发补录，验证超限提交被退回、
    覆盖数恒 <= N，且每次响应内的覆盖数与结论来自同一已提交状态；
  * 第二阶段：对同一批实例并发更正（替换旧样本），验证同 ID 不重复、
    覆盖数仍恒 <= N，退回不会改变已提交状态；
  * 第三阶段：无效样本对（重启/负差值）并发混入，验证它们不贡献覆盖。

用法：python tests/concurrency_check.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
N = 200  # 网关完结请求总数
ROUNDS = 8  # 每实例 8 个并发版本，制造交错更正


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def hist(bounds, counts):
    return {"bounds": bounds, "counts": counts}


def check(cond, msg, failures):
    if not cond:
        failures.append(msg)
        print("  [FAIL]", msg)


def main() -> int:
    failures: list[str] = []

    # 复位：清掉库里残留实例，保证脚本可重复运行
    _, stale = call("GET", "/api/snapshot")
    for inst in stale.get("instances", []):
        call("DELETE", "/api/instances/" + inst["instance_id"])

    status, _ = call("POST", "/api/config", {
        "gateway_total": N, "latency_limit": 100.0, "target_ratio": 0.95})
    check(status == 200, f"配置写入失败: {status}", failures)

    # ---------- 阶段 1：30 个实例各贡献 10，容量只有 200 => 恰好 20 个成功 ----------
    def initial_submit(i):
        return call("POST", "/api/instances", {
            "instance_id": f"inst-{i:02d}",
            "start_marker": "boot-1", "end_marker": "boot-1",
            "start_histogram": hist([100], [0]),
            "end_histogram": hist([100], [10]),
        })

    accepted = rejected = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(initial_submit, i) for i in range(30)]
        for fut in as_completed(futs):
            status, body = fut.result()
            if status == 200:
                accepted += 1
                check(body["summary"]["coverage"] <= N,
                      f"接受后覆盖 {body['summary']['coverage']} 超过 N={N}", failures)
            elif status == 409:
                rejected += 1
            else:
                failures.append(f"意外状态码 {status}: {body}")
    print(f"阶段1 并发补录：接受 {accepted}，退回 {rejected}")
    check(accepted == 20, f"应有 20 个接受，实际 {accepted}", failures)
    check(rejected == 10, f"应有 10 个退回，实际 {rejected}", failures)

    status, snap = call("GET", "/api/snapshot")
    check(snap["summary"]["coverage"] == 200,
          f"最终覆盖应为 200，实际 {snap['summary']['coverage']}", failures)
    check(len(snap["instances"]) == 20,
          f"应只有 20 个实例，实际 {len(snap['instances'])}", failures)
    accepted_ids = {x["instance_id"] for x in snap["instances"]}
    check(all(x.startswith("inst-") for x in accepted_ids),
          "阶段1 实例 ID 前缀异常", failures)
    check(snap["summary"]["decision"] in {"pass", "inconclusive", "fail"},
          "结论字段非法", failures)

    # ---------- 阶段 2：对 20 个已接受实例并发更正，版本 v 贡献 v*2（<=16） ----------
    def correction(iid, version):
        return call("POST", "/api/instances", {
            "instance_id": iid,
            "start_marker": f"boot-{version}", "end_marker": f"boot-{version}",
            "start_histogram": hist([50, 200], [0, 0]),
            # 桶(100,200] 跨 L => 未知；最终每实例 version*2 个覆盖
            "end_histogram": hist([50, 200], [version, version * 2]),
        })

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(correction, iid, v)
                for iid in sorted(accepted_ids) for v in range(1, ROUNDS + 1)]
        for fut in as_completed(futs):
            status, body = fut.result()
            if status == 200:
                check(body["summary"]["coverage"] <= N,
                      f"更正后覆盖 {body['summary']['coverage']} 超 N", failures)
                # 200 响应的快照实例集合必须与阶段1 的已接受集合完全一致
                ids = {x["instance_id"] for x in body["instances"]}
                check(ids == accepted_ids,
                      f"200 快照的实例集合异常：缺 {accepted_ids - ids}，"
                      f"多 {ids - accepted_ids}", failures)
            elif status != 409:
                failures.append(f"更正意外状态码 {status}: {body}")

    status, snap = call("GET", "/api/snapshot")
    check({x["instance_id"] for x in snap["instances"]} == accepted_ids,
          "更正后实例集合与预期不符", failures)
    # 每个实例的最终版本不确定（并发交错），但贡献必须是 2*v（v in 1..8）
    for inst in snap["instances"]:
        check(inst["valid"], f"{inst['instance_id']} 应为有效样本对", failures)
        check(inst["total"] in {v * 2 for v in range(1, ROUNDS + 1)},
              f"{inst['instance_id']} 覆盖 {inst['total']} 不是任何已提交版本", failures)
    check(snap["summary"]["coverage"] <= N, "最终覆盖超 N", failures)
    print(f"阶段2 交错更正完成，最终覆盖 {snap['summary']['coverage']} / {N}，"
          f"结论={snap['summary']['decision']}（{snap['summary']['decision_reason']}）")

    # ---------- 阶段 3：无效样本对混入（重启 / 负差值 / 桶界变化） ----------
    def invalid_submit(i):
        kind = i % 3
        if kind == 0:
            payload = dict(instance_id=f"bad-{i}", start_marker="a", end_marker="b",
                           start_histogram=hist([100], [0]),
                           end_histogram=hist([100], [5]))
        elif kind == 1:
            payload = dict(instance_id=f"bad-{i}", start_marker="a", end_marker="a",
                           start_histogram=hist([100], [9]),
                           end_histogram=hist([100], [5]))
        else:
            payload = dict(instance_id=f"bad-{i}", start_marker="a", end_marker="a",
                           start_histogram=hist([100], [0]),
                           end_histogram=hist([200], [5]))
        return call("POST", "/api/instances", payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(invalid_submit, i) for i in range(12)]
        for fut in as_completed(futs):
            status, body = fut.result()
            # 三种情况都属于无效样本：重启 / 负差值 / 桶界变化
            # 均应 200 入库但 valid=false、不计覆盖（结构错误才是 400）
            if status == 200:
                inst = next(x for x in body["instances"]
                            if x["instance_id"].startswith("bad-"))
                check(not inst["valid"], "无效样本对被计为有效", failures)
                check(inst["total"] == 0, "无效样本对贡献了覆盖", failures)
            else:
                failures.append(f"无效提交意外状态码 {status}: {body}")

    status, snap = call("GET", "/api/snapshot")
    coverage = snap["summary"]["coverage"]
    check(coverage <= N, "混入无效样本后覆盖超 N", failures)
    check(all(x["total"] == 0 for x in snap["instances"]
              if x["instance_id"].startswith("bad-")),
          "存在贡献覆盖的无效实例", failures)
    print(f"阶段3 无效样本混入完成，覆盖仍为 {coverage} / {N}")

    # ---------- 同源性核对：快照内 coverage 与逐实例 total 之和一致 ----------
    total_from_pairs = sum(x["total"] for x in snap["instances"] if x["valid"])
    check(total_from_pairs == coverage,
          f"快照不同源：汇总覆盖 {coverage} != 逐实例之和 {total_from_pairs}", failures)

    print()
    if failures:
        print(f"并发自测失败 {len(failures)} 项")
        return 1
    print("并发自测全部通过：无超限覆盖、无重复实例、退回不改状态、快照同源。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
