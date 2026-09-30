import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class RecusalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-201", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria,
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self):
        bid1 = self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.vendor1["id"],
                                       {"报价": 800000, "质量": 90}, 800000)
        bid2 = self.service.submit_bid("vendor2", "vendor", self.tender["id"], self.vendor2["id"],
                                       {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.opened_version = opened["tender"]["version"]
        return bid1, bid2

    def award_version(self):
        return self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["version"]

    def test_recusal_invalidates_scores_keeps_history_and_blocks_award(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})

        result = self.service.recuse_evaluator(
            "eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "临时发现亲属持股"
        )
        self.assertEqual("active", result["recusal"]["status"])
        self.assertEqual(2, len(result["invalidated_evaluations"]))

        # 历史行保留但全部失效
        trace = self.service.review_trace("sup1", "supervisor", self.tender["id"])
        v1 = next(v for v in trace["vendors"] if v["vendor"]["id"] == self.vendor1["id"])
        self.assertEqual(2, len(v1["invalidated_evaluations"]))
        self.assertTrue(all(e["status"] == "invalidated" for e in v1["invalidated_evaluations"]))
        self.assertTrue(all(e["invalidated_at"] for e in v1["invalidated_evaluations"]))
        seat = v1["seats"][0]["seat"]
        self.assertEqual("recused", seat["status"])
        self.assertEqual("eval1", seat["evaluator"])
        self.assertFalse(v1["ready"])
        self.assertTrue(v1["blockers"])

        # 供应商2不受影响
        v2 = next(v for v in trace["vendors"] if v["vendor"]["id"] == self.vendor2["id"])
        self.assertTrue(v2["ready"])

        # 被回避专家不能再用普通评分接口
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)

        # 授标被挡住，且明细指向供应商1
        with self.assertRaises(DomainError) as award_ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        self.assertEqual(409, award_ctx.exception.status)
        blocked = award_ctx.exception.details["blocked_vendors"]
        self.assertEqual([self.vendor1["id"]], [b["vendor_id"] for b in blocked])

    def test_handover_rescore_restores_and_other_vendor_unaffected(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        # 供应商1原本与供应商2同分维度但报价高，回避前供应商2领先
        self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                      self.vendor1["id"], "回避")
        trace = self.service.review_trace("aud1", "auditor", self.tender["id"])
        seat_id = next(v for v in trace["vendors"] if v["vendor"]["id"] == self.vendor1["id"])["seats"][0]["seat"]["id"]

        # 交接给替补
        handover = self.service.confirm_handover("sup1", "supervisor", seat_id, "eval3")
        self.assertEqual("confirmed", handover["handover"]["status"])
        self.assertEqual("active", handover["seat"]["status"])
        self.assertEqual("eval3", handover["seat"]["evaluator"])

        # 替补补评，供应商1恢复可授标
        self.service.rescore_bid("eval3", "evaluator", seat_id, bid1["id"],
                                 {"报价": 800000, "质量": 95})
        trace = self.service.review_trace("aud1", "auditor", self.tender["id"])
        v1 = next(v for v in trace["vendors"] if v["vendor"]["id"] == self.vendor1["id"])
        self.assertTrue(v1["ready"], v1["blockers"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        # 供应商1质量95（eval3与eval2均值92.5）反超供应商2质量80
        self.assertEqual(bid1["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(1, award["award"]["review"]["confirmed_handovers"])
        self.assertEqual(2, award["award"]["review"]["invalidated_evaluations"])

    def test_rescore_before_confirm_then_confirm_converges_single_successor(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        recusal = self.service.recuse_evaluator("sup1", "supervisor", self.tender["id"], "eval1",
                                                self.vendor1["id"], "回避")
        seat_id = recusal["seats"][0]["id"]

        # 替补先补评：产生待确认交接，分数挂起不参与汇总
        pending = self.service.rescore_bid("eval3", "evaluator", seat_id, bid1["id"],
                                           {"报价": 800000, "质量": 90})
        self.assertEqual("pending", pending["status"])
        self.assertIsNotNone(pending["handover_id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())

        # 监督员确认同一人：幂等收敛，挂起分数转有效
        confirmed = self.service.confirm_handover("sup1", "supervisor", seat_id, "eval3")
        self.assertEqual("confirmed", confirmed["handover"]["status"])
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        self.assertEqual("awarded", award["tender"]["status"])

    def _bid2_id(self):
        bids = self.service.get_tender("sup1", "supervisor", self.tender["id"])["bids"]
        return next(b["id"] for b in bids if b["vendor_id"] == self.vendor2["id"])

    def test_concurrent_confirm_and_rescore_keeps_single_successor(self):
        bid1, _ = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        recusal = self.service.recuse_evaluator("proc1", "procurement", self.tender["id"], "eval1",
                                                self.vendor1["id"], "回避")
        seat_id = recusal["seats"][0]["id"]
        barrier = threading.Barrier(2)
        outcomes = []

        def confirm():
            barrier.wait()
            try:
                self.service.confirm_handover("sup1", "supervisor", seat_id, "eval3")
                outcomes.append(("confirm-eval3", None))
            except DomainError as exc:
                outcomes.append(("confirm-eval3", exc))

        def rescore():
            barrier.wait()
            try:
                self.service.rescore_bid("eval4", "evaluator", seat_id, bid1["id"],
                                         {"报价": 800000, "质量": 90})
                outcomes.append(("rescore-eval4", None))
            except DomainError as exc:
                outcomes.append(("rescore-eval4", exc))

        t1 = threading.Thread(target=confirm)
        t2 = threading.Thread(target=rescore)
        t1.start(); t2.start(); t1.join(); t2.join()

        trace = self.service.review_trace("sup1", "supervisor", self.tender["id"])
        seat = trace["vendors"][0]["seats"][0]["seat"]
        # 无论谁先到，席位最终只归属一个接替人
        self.assertIn(seat["evaluator"], {"eval3", "eval4"})
        if seat["evaluator"] == "eval3":
            rescore_outcome = next(o for name, o in outcomes if name == "rescore-eval4")
            seat_full = trace["vendors"][0]["seats"][0]
            if rescore_outcome is not None:
                # 监督员先确认eval3：eval4补评被状态冲突拒绝
                self.assertEqual(409, rescore_outcome.status)
            else:
                # eval4先以挂起提名提交，监督员改派eval3：eval4分数失效、提名失败留痕
                failed = [h for h in seat_full["handovers"] if h["status"] == "failed"]
                self.assertTrue(failed)
                self.assertTrue(any(
                    e["evaluator"] == "eval4" and e["status"] == "invalidated"
                    for e in seat_full["evaluations"]
                ))
        else:
            # eval4先提名且最终生效
            confirm_outcome = next(o for name, o in outcomes if name == "confirm-eval3")
            self.assertIsNone(confirm_outcome)
        # 只有一条 confirmed 交接
        confirmed_rows = [h for v in trace["vendors"] for s in v["seats"] for h in s["handovers"]
                          if h["status"] == "confirmed"]
        self.assertEqual(1, len(confirmed_rows))

    def test_failed_handover_keeps_original_seat_retryable(self):
        bid1, _ = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        # 替补人本身与供应商冲突
        self.service.declare_conflict("eval9", "evaluator", self.tender["id"], "eval9",
                                      self.vendor1["id"], "曾任顾问")
        recusal = self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                                self.vendor1["id"], "回避")
        seat_id = recusal["seats"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_handover("sup1", "supervisor", seat_id, "eval9")
        self.assertIn("利益冲突", str(ctx.exception))
        # 原席位仍是recused，失败已留痕
        trace = self.service.review_trace("sup1", "supervisor", self.tender["id"])
        seat_payload = trace["vendors"][0]["seats"][0]
        self.assertEqual("recused", seat_payload["seat"]["status"])
        self.assertEqual("eval1", seat_payload["seat"]["evaluator"])
        self.assertTrue(any(h["status"] == "failed" for h in seat_payload["handovers"]))

        # 从原席位重试，换人成功
        self.service.confirm_handover("sup1", "supervisor", seat_id, "eval3")
        self.service.rescore_bid("eval3", "evaluator", seat_id, bid1["id"],
                                 {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", self._bid2_id(),
                                  {"报价": 700000, "质量": 80})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        self.assertEqual("awarded", award["tender"]["status"])

    def test_same_expert_cannot_hold_two_seats_one_vendor_but_can_cover_other(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                      self.vendor1["id"], "回避")
        trace = self.service.review_trace("sup1", "supervisor", self.tender["id"])
        seat_id = trace["vendors"][0]["seats"][0]["seat"]["id"]
        # eval2已经是供应商1的席位专家，不能再接同一供应商的回避席位
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_handover("sup1", "supervisor", seat_id, "eval2")
        self.assertIn("已占用", str(ctx.exception))
        # 但eval2同时给供应商2评分是允许的（不同供应商不同席位）
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})

    def test_revoke_recusal_restores_pending_only(self):
        bid1, _ = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        recusal = self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                                self.vendor1["id"], "回避")
        seat_id = recusal["seats"][0]["id"]
        # 撤销后席位与评分恢复
        self.service.revoke_recusal("proc1", "procurement", recusal["recusal"]["id"])
        trace = self.service.review_trace("sup1", "supervisor", self.tender["id"])
        v1 = trace["vendors"][0]
        self.assertEqual("active", v1["seats"][0]["seat"]["status"])
        self.assertTrue(all(e["status"] == "valid" for e in
                            self.service.get_tender("sup1", "supervisor", self.tender["id"])["review"][0]["seats"][0]["evaluations"]))

        # 再次回避并完成交接后，不能撤销
        recusal2 = self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                                 self.vendor1["id"], "再次回避")
        seat_id2 = recusal2["seats"][0]["id"]
        self.service.confirm_handover("sup1", "supervisor", seat_id2, "eval3")
        with self.assertRaises(DomainError) as ctx:
            self.service.revoke_recusal("proc1", "procurement", recusal2["recusal"]["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_tender_wide_recusal_touches_all_vendors(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        result = self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                               None, "整体回避")
        self.assertIsNone(result["recusal"]["vendor_id"])
        self.assertEqual(4, len(result["invalidated_evaluations"]))
        trace = self.service.review_trace("aud1", "auditor", self.tender["id"])
        for vendor in trace["vendors"]:
            self.assertFalse(vendor["ready"])
            self.assertTrue(any(r["vendor_id"] is None for r in vendor["recusals"]))
        # 审计可按时间线还原
        state = self.service.state("aud1", "auditor")
        actions = [t["action"] for t in state["timeline"]]
        self.assertIn("evaluator.recused", actions)
        self.assertEqual(1, len([r for r in state["recusals"] if r["status"] == "active"]))

    def test_rescore_missing_criteria_still_blocks_award(self):
        bid1, bid2 = self.prepare()
        self.service.evaluate_bid("eval1", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", bid2["id"], {"报价": 700000, "质量": 80})
        recusal = self.service.recuse_evaluator("eval1", "evaluator", self.tender["id"], "eval1",
                                                self.vendor1["id"], "回避")
        seat_id = recusal["seats"][0]["id"]
        # 缺项补评被拒
        with self.assertRaises(DomainError):
            self.service.rescore_bid("eval3", "evaluator", seat_id, bid1["id"], {"报价": 800000})
        self.service.confirm_handover("sup1", "supervisor", seat_id, "eval3")
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        # 补齐后放行
        self.service.rescore_bid("eval3", "evaluator", seat_id, bid1["id"],
                                 {"报价": 800000, "质量": 90})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], self.award_version())
        self.assertEqual("awarded", award["tender"]["status"])


if __name__ == "__main__":
    unittest.main()
