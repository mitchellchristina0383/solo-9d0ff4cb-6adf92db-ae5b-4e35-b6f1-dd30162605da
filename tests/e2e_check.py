"""端到端人工场景：跨桶 / 不同桶界 / 重启 / 更正替换 / 超限退回 / 结论流转。"""
import json
import sys
import urllib.error
import urllib.request

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
failures = []


def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        failures.append(msg)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        B + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def hist(b, c):
    return {"bounds": b, "counts": c}


# 0. 复位：清掉残留实例，保证脚本可重复运行（配置随后显式重写）
_, stale = call("GET", "/api/snapshot")
for inst in stale.get("instances", []):
    call("DELETE", "/api/instances/" + inst["instance_id"])

# 1. 快照接口可用、实例已清空
st, snap = call("GET", "/api/snapshot")
check(st == 200 and not snap["instances"], "快照可用且无残留实例")

# 2. 配置 N=1000, L=200, 目标 99%
st, _ = call("POST", "/api/config",
             {"gateway_total": 1000, "latency_limit": 200, "target_ratio": 0.99})
check(st == 200, "写入窗口参数 N=1000 L=200 目标99%")
st, body = call("POST", "/api/config",
                {"gateway_total": 0, "latency_limit": 200, "target_ratio": 0.99})
check(st == 400, "N=0 被拒绝（必须大于零）")

# 3. 实例A：桶 [100,300,500]，跨 L 桶 (200,300] 未知
#    桶内请求：<=100 共900；(100,300] 80（跨L）；(300,500] 10（超限）
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-a", "start_marker": "boot-1", "end_marker": "boot-1",
    "start_histogram": hist([100, 300, 500], [0, 0, 0]),
    "end_histogram": hist([100, 300, 500], [900, 980, 990])})
a = next(i for i in body["instances"] if i["instance_id"] == "gw-a")
check(st == 200 and a["valid"], "实例A 样本有效")
check(a["good_lower"] == 900, f"实例A 达标下界 900（实际 {a['good_lower']}）")
check(a["bad_lower"] == 10, f"实例A 超限 10（实际 {a['bad_lower']}）")
check(a["unknown"] == 80, f"实例A 跨桶未知 80（实际 {a['unknown']}）")
check(body["summary"]["coverage"] == 990, "覆盖数=990")
check(body["summary"]["decision"] == "inconclusive",
      f"结论=待确认（实际 {body['summary']['decision']}）")

# 4. 实例B：不同桶界 [50,150,200]，全部达标，贡献 5
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-b", "start_marker": "boot-9", "end_marker": "boot-9",
    "start_histogram": hist([50, 150, 200], [0, 0, 0]),
    "end_histogram": hist([50, 150, 200], [2, 3, 5])})
check(body["summary"]["coverage"] == 995, "不同桶界实例B 提交后覆盖=995")

# 5. 实例C：窗口内重启 => 入库但不计覆盖
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-c", "start_marker": "boot-x", "end_marker": "boot-y",
    "start_histogram": hist([200], [0]), "end_histogram": hist([200], [7])})
c = next(i for i in body["instances"] if i["instance_id"] == "gw-c")
check(not c["valid"] and "启动标识" in c["reason"], "实例C 重启样本对不计覆盖")
check(body["summary"]["coverage"] == 995, "覆盖数仍为 995")

# 6. 实例D：桶界不一致
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-d", "start_marker": "b", "end_marker": "b",
    "start_histogram": hist([200], [0]), "end_histogram": hist([250], [4])})
d = next(i for i in body["instances"] if i["instance_id"] == "gw-d")
check(not d["valid"] and "桶界" in d["reason"], "实例D 桶界不一致不计覆盖")

# 7. 超限退回：再提交贡献 10 的实例 => 995+10=1005 > 1000
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-e", "start_marker": "b", "end_marker": "b",
    "start_histogram": hist([200], [0]), "end_histogram": hist([200], [10])})
check(st == 409 and body["code"] == "coverage_exceeded",
      "会使覆盖超 N 的提交被退回（409）")
st, snap = call("GET", "/api/snapshot")
check(snap["summary"]["coverage"] == 995 and len(snap["instances"]) == 4,
      "退回后原状态与结论保留（覆盖仍 995、无 gw-e）")

# 8. 更正 gw-a：替换为只贡献 5 的样本，释放容量
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-a", "start_marker": "boot-2", "end_marker": "boot-2",
    "start_histogram": hist([100, 300], [0, 0]),
    "end_histogram": hist([100, 300], [3, 5])})
check(len(body["instances"]) == 4, "更正不新增实例")
check(body["summary"]["coverage"] == 10, f"更正后覆盖=10（实际 {body['summary']['coverage']}）")
a2 = next(i for i in body["instances"] if i["instance_id"] == "gw-a")
check(a2["valid"] and a2["total"] == 5, "gw-a 旧样本已被新样本替换")

# 9. 现在 gw-e 的 10 个可以提交了（10+10=20 <= 1000）
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-e", "start_marker": "b", "end_marker": "b",
    "start_histogram": hist([200], [0]), "end_histogram": hist([200], [10])})
check(st == 200 and body["summary"]["coverage"] == 20, "释放容量后 gw-e 可提交，覆盖=20")

# 10. 达标结论：N=1000 下补一个 990 全达标样本 => good=1000，下界 100% => 达标
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-f", "start_marker": "b", "end_marker": "b",
    "start_histogram": hist([200], [0]), "end_histogram": hist([200], [980])})
# 当前覆盖 20，980 会超 1000？20+980=1000 恰好
check(st == 200, f"gw-f 恰好补齐到 N=1000（HTTP {st}）")
s = body["summary"]
check(s["coverage"] == 1000, f"覆盖=1000（实际 {s['coverage']}）")
check(s["decision"] == "pass", f"结论=达标（实际 {s['decision']}：{s['decision_reason']}）")

# 11. 未达标：先把 gw-b 更正为含 1 个确认超限请求（桶 (200,500] 整体在 L 之上），
#     此时达标上界 = 999/1000 < 100%，目标改 100% => 未达标
st, body = call("POST", "/api/instances", {
    "instance_id": "gw-b", "start_marker": "boot-9", "end_marker": "boot-9",
    "start_histogram": hist([200, 500], [0, 0]),
    "end_histogram": hist([200, 500], [4, 5])})
check(st == 200 and body["summary"]["coverage"] == 1000, "gw-b 更正后覆盖仍为 1000")
st, body = call("POST", "/api/config",
                {"gateway_total": 1000, "latency_limit": 200, "target_ratio": 1.0})
check(st == 200 and body["summary"]["decision"] == "fail",
      f"存在确认超限且目标100% => 未达标（实际 {body['summary']['decision']}："
      f"{body['summary']['decision_reason']}）")
check(body["summary"]["bad_lower"] >= 1, "确认超限请求数 >= 1")

# 12. 同源性：快照覆盖数 == 逐有效实例 total 之和
st, snap = call("GET", "/api/snapshot")
pair_sum = sum(i["total"] for i in snap["instances"] if i["valid"])
check(pair_sum == snap["summary"]["coverage"],
      f"快照同源：逐实例之和 {pair_sum} == 覆盖 {snap['summary']['coverage']}")

print()
print("全部通过" if not failures else f"{len(failures)} 项失败")
sys.exit(1 if failures else 0)
