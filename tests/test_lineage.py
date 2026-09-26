import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def _batch(self, factory, no, qty=100):
        return self.s.create_batch("operator", "operator", factory, no, "药片", "2026-01-01", "2028-01-01", qty)

    def _release(self, batch):
        bid = batch["id"] if isinstance(batch, dict) else batch
        rev = self.s.batch_detail(bid)["batch"]["revision"]
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, rev)
        rev = self.s.batch_detail(bid)["batch"]["revision"]
        return self.s.decide("qa", "qa", bid, "release", "合格放行", rev)

    # ---------- 登记与校验 ----------
    def test_register_lineage_and_available_quantity(self):
        a, b = self._batch(self.f1, "A"), self._batch(self.f1, "B")
        edge = self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 60, a["revision"])
        self.assertEqual(60, edge["quantity"])
        self.assertEqual(40, edge["upstream_available_remaining"])
        self.assertEqual(40, self.s.batch_detail(a["id"])["available_quantity"])
        detail = self.s.batch_detail(b["id"])
        self.assertEqual(1, len(detail["upstream_lineage"]))
        self.assertEqual("A", detail["upstream_lineage"][0]["upstream_batch_no"])

    def test_cross_factory_lineage_rejected_not_persisted(self):
        a, b = self._batch(self.f1, "A"), self._batch(self.f2, "B")
        with self.assertRaises(ApiError) as err:
            self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 10, a["revision"])
        self.assertEqual(403, err.exception.status)
        self.assertEqual(0, len(self.s.state()["lineage"]))
        self.assertEqual(100, self.s.batch_detail(a["id"])["available_quantity"])

    def test_over_consumption_rejected(self):
        a, b = self._batch(self.f1, "A", 100), self._batch(self.f1, "B")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 80, a["revision"])
        rev = self.s.batch_detail(a["id"])["batch"]["revision"]
        c = self._batch(self.f1, "C")
        with self.assertRaises(ApiError) as err:
            self.s.register_lineage("operator", "operator", self.f1, a["id"], c["id"], 21, rev)
        self.assertEqual(409, err.exception.status)
        self.assertIn("可用量", err.exception.message)
        self.assertEqual(20, self.s.batch_detail(a["id"])["available_quantity"])

    def test_duplicate_and_self_and_cycle_rejected(self):
        a, b = self._batch(self.f1, "A"), self._batch(self.f1, "B")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 10, a["revision"])
        rev = self.s.batch_detail(a["id"])["batch"]["revision"]
        with self.assertRaises(ApiError):  # 重复关系
            self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 5, rev)
        with self.assertRaises(ApiError) as loop:  # 自环
            self.s.register_lineage("operator", "operator", self.f1, b["id"], b["id"], 5, rev)
        self.assertIn("成环", loop.exception.message)
        # B -> A 与既有 A -> B 构成环
        rev_b = self.s.batch_detail(b["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as cyc:
            self.s.register_lineage("operator", "operator", self.f1, b["id"], a["id"], 5, rev_b)
        self.assertIn("成环", cyc.exception.message)
        self.assertEqual(1, len(self.s.state()["lineage"]))

    def test_stale_upstream_revision_rejected(self):
        a, b = self._batch(self.f1, "A"), self._batch(self.f1, "B")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 10, a["revision"])
        c = self._batch(self.f1, "C")
        with self.assertRaises(ApiError) as err:
            self.s.register_lineage("operator", "operator", self.f1, a["id"], c["id"], 5, a["revision"])
        self.assertEqual(409, err.exception.status)

    # ---------- 召回传播 ----------
    def test_reject_propagates_recall_and_withdraws_release(self):
        a, b, c = self._batch(self.f1, "A"), self._batch(self.f1, "B"), self._batch(self.f1, "C")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 40, a["revision"])
        b_rev = self.s.batch_detail(b["id"])["batch"]["revision"]
        self.s.register_lineage("operator", "operator", self.f1, b["id"], c["id"], 40, b_rev)
        self._release(b)
        rev = self.s.batch_detail(b["id"])["batch"]["revision"]
        self.s.decide("qa", "qa", a["id"], "reject", "原料不合格", self.s.batch_detail(a["id"])["batch"]["revision"])
        for bid, prior in ((b["id"], "released"), (c["id"], "manufactured")):
            detail = self.s.batch_detail(bid)
            self.assertEqual("recall_pending", detail["batch"]["state"])
            event = detail["recall_events"][-1]
            self.assertEqual(a["id"], event["source_id"]); self.assertEqual(prior, event["prior_state"])
        # 已放行批撤回，但原放行决定保留
        withdrawn = self.s.batch_detail(b["id"])
        self.assertTrue(withdrawn["recall_events"][-1]["withdrawn_from_release"])
        self.assertEqual("release", withdrawn["decisions"][-1]["decision"])
        # 阻塞来源可定位
        blockers = {x["kind"] for x in withdrawn["active_blockers"]}
        self.assertIn("rejected", blockers)

    def test_critical_open_deviation_propagates_and_close_allows_rerelease(self):
        a, b = self._batch(self.f1, "A"), self._batch(self.f1, "B")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 40, a["revision"])
        rev = self.s.batch_detail(a["id"])["batch"]["revision"]
        dev = self.s.add_deviation("inspector", "inspector", self.f1, a["id"], "critical", "无菌异常", self.future, rev)
        self.assertEqual("recall_pending", self.s.batch_detail(b["id"])["batch"]["state"])
        detail_b = self.s.batch_detail(b["id"])
        self.assertIn("critical_deviation", {x["kind"] for x in detail_b["active_blockers"]})
        # 阻塞存在时不能放行
        rev_b = detail_b["batch"]["revision"]
        self.s.record_test("lab", "lab", self.f1, b["id"], "含量", 100, 95, 105, rev_b)
        rev_b = self.s.batch_detail(b["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", b["id"], "release", "尝试放行", rev_b)
        self.assertIn("阻塞来源", blocked.exception.message)
        # 关闭关键偏差后，QA 审查并可重新放行；原撤回事件仍保留
        rev_a = self.s.batch_detail(a["id"])["batch"]["revision"]
        self.s.close_deviation("qa", "qa", dev["id"], "灭菌重做", rev_a)
        self.assertEqual([], self.s.batch_detail(b["id"])["active_blockers"])
        rev_b = self.s.batch_detail(b["id"])["batch"]["revision"]
        out = self.s.review_recall("qa", "qa", b["id"], "note", "上游偏差已关闭，转正常放行流程")
        self.assertEqual("recall_pending", out["batch"]["state"])
        self.s.decide("qa", "qa", b["id"], "release", "上游解除，重新放行", rev_b)
        self.assertEqual("released", self.s.batch_detail(b["id"])["batch"]["state"])
        self.assertTrue(self.s.batch_detail(b["id"])["recall_events"])

    def test_linking_to_blocked_upstream_propagates_and_rejected_stays_terminal(self):
        a, b, c = self._batch(self.f1, "A"), self._batch(self.f1, "B"), self._batch(self.f1, "C")
        self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 10, a["revision"])
        self.s.decide("qa", "qa", a["id"], "reject", "拒收", self.s.batch_detail(a["id"])["batch"]["revision"])
        self.assertEqual("recall_pending", self.s.batch_detail(b["id"])["batch"]["state"])
        # 拒收后再登记 C 与 A 的血缘，C 立即进入召回待审；投入量仍受 A 剩余量约束
        self.s.register_lineage("operator", "operator", self.f1, a["id"], c["id"], 5,
                                self.s.batch_detail(a["id"])["batch"]["revision"])
        self.assertEqual("recall_pending", self.s.batch_detail(c["id"])["batch"]["state"])
        # QA 在召回审查中直接拒收，状态保持 rejected 终态
        self.s.review_recall("qa", "qa", b["id"], "reject", "召回确认销毁")
        self.assertEqual("rejected", self.s.batch_detail(b["id"])["batch"]["state"])
        # A 拒收后不能再向其登记投入
        with self.assertRaises(ApiError):
            self.s.register_lineage("operator", "operator", self.f1, a["id"], b["id"], 1,
                                    self.s.batch_detail(a["id"])["batch"]["revision"])


if __name__ == "__main__": unittest.main()
