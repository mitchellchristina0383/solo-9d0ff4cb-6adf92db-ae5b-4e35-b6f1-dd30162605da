"""Flask API 测试：配置、补录/更正替换、超限退回、快照同源。"""

import tempfile
import unittest

from app import create_app


def hist(bounds, counts):
    return {"bounds": bounds, "counts": counts}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = self.tmp.name + "/test.db"
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, n=100, limit=100.0, ratio=0.9):
        return self.client.post("/api/config", json={
            "gateway_total": n, "latency_limit": limit, "target_ratio": ratio,
        })

    def submit(self, iid, start, end, sm="b1", em="b1"):
        return self.client.post("/api/instances", json={
            "instance_id": iid,
            "start_marker": sm, "end_marker": em,
            "start_histogram": start, "end_histogram": end,
        })

    def test_config_validation(self):
        r = self.client.post("/api/config", json={
            "gateway_total": 0, "latency_limit": 100, "target_ratio": 0.9})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/config", json={
            "gateway_total": 10, "latency_limit": -1, "target_ratio": 0.9})
        self.assertEqual(400, r.status_code)
        r = self.client.post("/api/config", json={
            "gateway_total": 10, "latency_limit": 100, "target_ratio": 1.2})
        self.assertEqual(400, r.status_code)

    def test_submit_before_config_rejected(self):
        r = self.submit("i", hist([100], [0]), hist([100], [5]))
        self.assertEqual(r.status_code, 400)

    def test_happy_path_and_snapshot_shape(self):
        self.assertEqual(self.cfg().status_code, 200)
        r = self.submit("i1", hist([50, 200], [0, 0]), hist([50, 200], [80, 90]))
        self.assertEqual(r.status_code, 200)
        snap = r.get_json()
        self.assertEqual(snap["summary"]["coverage"], 90)
        self.assertEqual(snap["summary"]["good_lower"], 80)
        self.assertEqual(snap["summary"]["unknown"], 20)  # 10 跨桶 + 10 未覆盖
        self.assertEqual(len(snap["instances"]), 1)
        self.assertTrue(snap["instances"][0]["valid"])

    def test_coverage_exceeded_is_rejected_and_state_kept(self):
        self.cfg(n=10)
        r = self.submit("i1", hist([100], [0]), hist([100], [8]))
        self.assertEqual(r.status_code, 200)
        # 再提交一个会让覆盖数达到 15 > 10 的实例
        r = self.submit("i2", hist([100], [0]), hist([100], [7]))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "coverage_exceeded")
        # 原状态与结论保留
        snap = self.client.get("/api/snapshot").get_json()
        self.assertEqual(snap["summary"]["coverage"], 8)
        self.assertEqual(len(snap["instances"]), 1)

    def test_correction_replaces_old_sample(self):
        self.cfg(n=100)
        self.submit("i1", hist([100], [0]), hist([100], [30]))
        # 更正：同 ID，新样本只有 10 个
        r = self.submit("i1", hist([100], [0]), hist([100], [10]))
        snap = r.get_json()
        self.assertEqual(snap["summary"]["coverage"], 10)
        self.assertEqual(len(snap["instances"]), 1)
        # 更正为无效样本对后覆盖归零
        r = self.submit("i1", hist([100], [0]), hist([100], [5]),
                        sm="boot-a", em="boot-b")
        snap = r.get_json()
        self.assertEqual(snap["summary"]["coverage"], 0)
        self.assertFalse(snap["instances"][0]["valid"])

    def test_correction_may_grow_within_total_but_not_past_it(self):
        self.cfg(n=10)
        self.submit("i1", hist([100], [0]), hist([100], [6]))
        # 更正到 10：合法
        r = self.submit("i1", hist([100], [0]), hist([100], [10]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"]["coverage"], 10)
        # 更正到 11：退回，保留覆盖=10 的结论
        r = self.submit("i1", hist([100], [0]), hist([100], [11]))
        self.assertEqual(r.status_code, 409)
        snap = self.client.get("/api/snapshot").get_json()
        self.assertEqual(snap["summary"]["coverage"], 10)

    def test_config_shrink_below_coverage_rejected(self):
        self.cfg(n=100)
        self.submit("i1", hist([100], [0]), hist([100], [50]))
        r = self.client.post("/api/config", json={
            "gateway_total": 40, "latency_limit": 100, "target_ratio": 0.9})
        self.assertEqual(r.status_code, 409)
        snap = self.client.get("/api/snapshot").get_json()
        self.assertEqual(snap["config"]["gateway_total"], 100)

    def test_invalid_histogram_payloads(self):
        self.cfg()
        # 结构性输入错误 => 400
        r = self.submit("i", hist([100, 100], [1, 2]), hist([100, 100], [1, 2]))
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/instances", json={"instance_id": ""})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/instances", json={
            "instance_id": "i", "start_marker": "b", "end_marker": "b",
            "start_histogram": {"bounds": [100], "counts": [1.5]},
            "end_histogram": hist([100], [4])})
        self.assertEqual(r.status_code, 400)
        # 语义性无效样本对（负差值）=> 200 入库但不计覆盖
        r = self.submit("i", hist([100], [5]), hist([100], [4]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["instances"][0]["valid"])
        self.assertEqual(r.get_json()["summary"]["coverage"], 0)

    def test_delete_instance(self):
        self.cfg()
        self.submit("i1", hist([100], [0]), hist([100], [5]))
        r = self.client.delete("/api/instances/i1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"]["coverage"], 0)
        r = self.client.delete("/api/instances/i1")
        self.assertEqual(r.status_code, 404)

    def test_decision_end_to_end(self):
        # 跨桶 + 缺口 => 待确认；补录后 => 达标
        self.cfg(n=10, ratio=0.9)
        r = self.submit("i1", hist([50, 200], [0, 0]), hist([50, 200], [8, 9]))
        self.assertEqual(r.get_json()["summary"]["decision"], "inconclusive")
        r = self.submit("i2", hist([100], [0]), hist([100], [1]))
        self.assertEqual(r.get_json()["summary"]["decision"], "pass")
        self.assertEqual(r.get_json()["summary"]["coverage"], 10)


if __name__ == "__main__":
    unittest.main()
