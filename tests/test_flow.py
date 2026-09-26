import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def test_full_investigation_retest_rework_and_release(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, batch["revision"])
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, dev["batch_id"] and self.s.batch_detail(batch["id"])["batch"]["revision"])
        self.assertFalse(failed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, current)
        self.assertTrue(passed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "返工包装", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，复测合格", current)
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(1, len(result["batch"] and self.s.batch_detail(batch["id"])["decisions"]))

    def test_critical_block_conditional_exception_and_factory_conflict(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-2", "胶囊", "2026-02-01", "2028-02-01")
        current = batch["revision"]
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        crit = self.s.add_deviation("inspector", "inspector", self.f1, batch["id"], "critical", "无菌数据异常", self.future, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", current)
        self.assertIn("关键偏差", blocked.exception.message)
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, current)
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, batch["id"], "水分", 1, 0, 2, current)
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "l.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def mk(self, no, qty, factory=None):
        return self.s.create_batch("operator", "operator", factory or self.f1, no, "中间体", "2026-01-01", "2028-01-01", qty)

    def test_link_registration_guards(self):
        up, down = self.mk("U-1", 100), self.mk("D-1", 50)
        link = self.s.add_link("operator", "operator", self.f1, down["id"], up["id"], 40)
        self.assertEqual(40, link["quantity"])
        state = self.s.state()
        self.assertEqual(60, [b for b in state["batches"] if b["id"] == up["id"]][0]["available_quantity"])
        with self.assertRaises(ApiError) as over:
            self.s.add_link("operator", "operator", self.f1, self.mk("D-2", 1)["id"], up["id"], 61)
        self.assertEqual(409, over.exception.status)
        other = self.mk("X-1", 10, self.f2)
        with self.assertRaises(ApiError) as cross:
            self.s.add_link("operator", "operator", self.f2, other["id"], up["id"], 1)
        self.assertEqual(409, cross.exception.status)
        down2 = self.mk("D-3", 10)
        self.s.add_link("operator", "operator", self.f1, down2["id"], down["id"], 10)
        with self.assertRaises(ApiError) as cyc:
            self.s.add_link("operator", "operator", self.f1, up["id"], down2["id"], 5)
        self.assertEqual(409, cyc.exception.status)
        with self.assertRaises(ApiError):
            self.s.add_link("operator", "operator", self.f1, down["id"], up["id"], 1)
        self.assertEqual(2, len(self.s.state()["links"]))

    def test_reject_recalls_downstream_and_withdraws_released(self):
        up, mid, leaf, rel = self.mk("U-2", 100), self.mk("M-2", 50), self.mk("L-2", 20), self.mk("R-2", 30)
        self.s.add_link("op", "operator", self.f1, mid["id"], up["id"], 30)
        self.s.add_link("op", "operator", self.f1, leaf["id"], mid["id"], 10)
        self.s.add_link("op", "operator", self.f1, rel["id"], up["id"], 20)
        self.s.record_test("lab", "lab", self.f1, rel["id"], "含量", 99, 95, 105, rel["revision"])
        cur = self.s.batch_detail(rel["id"])["batch"]["revision"]
        self.assertEqual("released", self.s.decide("qa", "qa", rel["id"], "release", "检验合格", cur)["batch"]["state"])
        cur = self.s.batch_detail(up["id"])["batch"]["revision"]
        self.s.decide("qa", "qa", up["id"], "reject", "严重质量缺陷", cur)
        for b in (mid, leaf, rel):
            self.assertEqual("recall_review", self.s.batch_detail(b["id"])["batch"]["state"])
        decisions = self.s.batch_detail(rel["id"])["decisions"]
        self.assertEqual("release", decisions[-1]["decision"])
        mid_links = self.s.batch_detail(mid["id"])["links"]
        self.assertTrue(mid_links["upstream"][0]["blocking"])
        leaf_links = self.s.batch_detail(leaf["id"])["links"]
        self.assertFalse(leaf_links["upstream"][0]["blocking"])
        self.assertEqual([up["id"]], [s["batch_id"] for s in leaf_links["recall_sources"]])

    def test_critical_deviation_recalls_and_link_to_blocked_upstream(self):
        up, down = self.mk("U-3", 100), self.mk("D-4", 10)
        self.s.add_link("op", "operator", self.f1, down["id"], up["id"], 10)
        cur = self.s.batch_detail(up["id"])["batch"]["revision"]
        self.s.add_deviation("inspector", "inspector", self.f1, up["id"], "critical", "无菌保证失效", self.future, cur)
        self.assertEqual("recall_review", self.s.batch_detail(down["id"])["batch"]["state"])
        up2 = self.mk("U-4", 10)
        self.s.decide("qa", "qa", up2["id"], "reject", "不合格", up2["revision"])
        late = self.mk("L-4", 5)
        self.s.add_link("op", "operator", self.f1, late["id"], up2["id"], 5)
        self.assertEqual("recall_review", self.s.batch_detail(late["id"])["batch"]["state"])


if __name__ == "__main__": unittest.main()
