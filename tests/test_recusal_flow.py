import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


CRITERIA = [
    {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
    {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
]
SCORES = {"报价": 800000, "质量": 90}


class RecusalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "vendor2")
        tender = self.service.create_tender(
            "proc1", "procurement", "T-101", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), CRITERIA,
        )
        tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        self.bid1 = self.service.submit_bid("vendor1", "vendor", tender["id"], self.vendor1["id"], {"报价": 800000, "质量": 90}, 800000)
        self.bid2 = self.service.submit_bid("vendor2", "vendor", tender["id"], self.vendor2["id"], {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", tender["id"], tender["version"])
        self.tender_id = tender["id"]
        self.bid_ids = {b["vendor_id"]: b["id"] for b in opened["bids"]}
        # 两位专家固定席位，对两家供应商都已评分。
        self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval1", "S01")
        self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval2", "S02")
        for vendor_id in (self.vendor1["id"], self.vendor2["id"]):
            self.service.evaluate_bid("eval1", "evaluator", self.bid_ids[vendor_id],
                                      {"报价": 800000, "质量": 90})
            self.service.evaluate_bid("eval2", "evaluator", self.bid_ids[vendor_id],
                                      {"报价": 700000, "质量": 80})

    def tearDown(self):
        self.tmp.cleanup()

    def current_tender(self):
        return self.service.get_tender("sup1", "supervisor", self.tender_id)

    def test_same_evaluator_cannot_hold_two_seats(self):
        # 重复分配返回同一席位；显式使用其他席位号也被拒绝。
        again = self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval1")
        self.assertEqual("S01", again["seat_no"])
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval1", "S99")
        self.assertEqual(409, ctx.exception.status)
        # 已接手回避的专家不能再占独立席位（交接后验证，见下方用例间接覆盖）。

    def test_recusal_invalidates_scores_but_keeps_history(self):
        recusal = self.service.declare_recusal(
            "eval1", "evaluator", self.tender_id, "eval1", self.vendor1["id"], "临时发现亲属任职")
        self.assertEqual("recused", recusal["status"])
        detail = self.current_tender()
        trace1 = detail["review_trace"][str(self.vendor1["id"])]
        invalidated = [e for e in trace1["invalidated_evaluations"] if e["evaluator"] == "eval1"]
        self.assertEqual(2, len(invalidated))
        self.assertEqual("invalidated", invalidated[0]["status"])
        self.assertEqual(recusal["id"], invalidated[0]["invalidated_recusal_id"])
        # 历史记录仍在库里，只是不参与有效评分。
        self.assertEqual(2, len(trace1["valid_evaluations"]))
        # 其他供应商不受影响：eval1 对 vendor2 的评分仍然有效。
        trace2 = detail["review_trace"][str(self.vendor2["id"])]
        self.assertEqual(0, len(trace2["invalidated_evaluations"]))
        self.assertEqual(4, len(trace2["valid_evaluations"]))
        # 回避专家不能再对该供应商评分。
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", self.bid_ids[self.vendor1["id"]], SCORES)
        self.assertEqual(403, ctx.exception.status)

    def test_pending_handover_or_missing_repair_blocks_award_for_that_vendor_only(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "临时发现亲属任职")
        version = self.current_tender()["tender"]["version"]
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender_id, version)
        self.assertIn("未完成专家交接", str(ctx.exception))
        # 交接完成但补评没补齐，同样挡住授标。
        self.service.transfer_recusal("sup1", "supervisor", recusal["id"], "eval3")
        version = self.current_tender()["tender"]["version"]
        with self.assertRaises(DomainError) as ctx2:
            self.service.award_tender("sup1", "supervisor", self.tender_id, version)
        self.assertIn("回避补评未完成", str(ctx2.exception))
        # 但另一家供应商自身的回避视图是干净的。
        trace2 = self.current_tender()["review_trace"][str(self.vendor2["id"])]
        self.assertEqual([], trace2["recusals"])

    def test_handover_replacement_and_repair_then_award_excludes_invalidated_scores(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "临时发现亲属任职")
        transferred = self.service.transfer_recusal("sup1", "supervisor", recusal["id"], "eval3")
        self.assertEqual("done", transferred["handover_status"])
        self.assertEqual("transferred", transferred["status"])
        # 非接替人不能凭该回避记录补评。
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval4", "evaluator", self.bid_ids[self.vendor1["id"]], SCORES,
                                      recusal_id=recusal["id"])
        self.assertEqual(403, ctx.exception.status)
        # 未交接的回避不能补评。
        other = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval2", self.vendor2["id"], "其他回避")
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval3", "evaluator", self.bid_ids[self.vendor2["id"]], SCORES,
                                      recusal_id=other["id"])
        self.assertEqual(409, ctx2.exception.status)
        # 接替人补评 vendor1（只补该供应商，不占独立席位）。
        result = self.service.evaluate_bid("eval3", "evaluator", self.bid_ids[self.vendor1["id"]],
                                           {"报价": 810000, "质量": 85}, recusal_id=recusal["id"])
        self.assertEqual(recusal["id"], result["recusal_id"])
        detail = self.current_tender()
        self.assertNotIn("eval3", [s["evaluator"] for s in detail["evaluation_seats"]])
        trace1 = detail["review_trace"][str(self.vendor1["id"])]
        repaired = trace1["replacement_evaluations"]
        self.assertEqual(2, len(repaired))
        self.assertTrue(all(e["source_recusal_id"] == recusal["id"] for e in repaired))
        # vendor2 的回避撤回后（未交接允许撤回），其原评分恢复有效。
        self.service.withdraw_recusal("proc1", "procurement", other["id"])
        detail = self.current_tender()
        trace2 = detail["review_trace"][str(self.vendor2["id"])]
        self.assertEqual("withdrawn", trace2["recusals"][0]["status"])
        self.assertEqual(4, len(trace2["valid_evaluations"]))
        # vendor1：eval1 的失效分不进汇总，有效分为 eval2(两项) + eval3(两项)，可授标。
        award = self.service.award_tender("sup1", "supervisor", self.tender_id,
                                          detail["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        summary = award["award"]["recusal_review"][str(self.vendor1["id"])]
        self.assertEqual(1, summary["recusals"][0]["id"])
        self.assertEqual(2, summary["invalidated_count"])
        self.assertEqual(2, summary["replacement_count"])

    def test_withdraw_restores_invalidated_scores(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "误报回避")
        self.service.withdraw_recusal("proc1", "procurement", recusal["id"])
        trace1 = self.current_tender()["review_trace"][str(self.vendor1["id"])]
        self.assertEqual(0, len(trace1["invalidated_evaluations"]))
        self.assertEqual(4, len(trace1["valid_evaluations"]))
        # 已完成交接的不能撤回。
        recusal2 = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval2", self.vendor2["id"], "回避二")
        self.service.transfer_recusal("sup1", "supervisor", recusal2["id"], "eval4")
        with self.assertRaises(DomainError) as ctx:
            self.service.withdraw_recusal("proc1", "procurement", recusal2["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_replacement_cannot_occupy_two_seats(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "回避")
        self.service.transfer_recusal("sup1", "supervisor", recusal["id"], "eval3")
        # 接替人不能再拿独立席位。
        with self.assertRaises(DomainError) as ctx:
            self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval3", "S03")
        self.assertEqual(409, ctx.exception.status)
        # 同一接替人不能接手第二个回避席位。
        recusal2 = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval2", self.vendor2["id"], "回避二")
        with self.assertRaises(DomainError) as ctx2:
            self.service.transfer_recusal("sup1", "supervisor", recusal2["id"], "eval3")
        self.assertEqual(409, ctx2.exception.status)
        # 已有独立席位的专家不能当接替人。
        self.service.assign_evaluation_seat("proc1", "procurement", self.tender_id, "eval5", "S05")
        with self.assertRaises(DomainError) as ctx3:
            self.service.transfer_recusal("sup1", "supervisor", recusal2["id"], "eval5")
        self.assertEqual(409, ctx3.exception.status)
        # 被回避本人不能接替自己；有利益冲突也不行。
        with self.assertRaises(DomainError):
            self.service.transfer_recusal("sup1", "supervisor", recusal2["id"], "eval2")
        self.service.declare_conflict("eval6", "evaluator", self.tender_id, "eval6",
                                      self.vendor2["id"], "持股")
        with self.assertRaises(DomainError) as ctx4:
            self.service.transfer_recusal("sup1", "supervisor", recusal2["id"], "eval6")
        self.assertEqual(409, ctx4.exception.status)

    def test_concurrent_transfer_keeps_single_replacement_and_allows_retry(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "回避")
        barrier = threading.Barrier(2)
        outcomes: list[dict[str, str]] = []

        def confirm(replacement: str) -> None:
            try:
                barrier.wait()
                row = self.service.transfer_recusal("sup1", "supervisor", recusal["id"], replacement)
                outcomes.append({"replacement": replacement, "ok": row["replacement_evaluator"]})
            except DomainError as exc:
                outcomes.append({"replacement": replacement, "err": str(exc)})

        t1 = threading.Thread(target=confirm, args=("eval3",))
        t2 = threading.Thread(target=confirm, args=("eval4",))
        t1.start(); t2.start(); t1.join(); t2.join()
        winners = [o for o in outcomes if "ok" in o]
        losers = [o for o in outcomes if "err" in o]
        self.assertEqual(1, len(winners), outcomes)
        self.assertEqual(1, len(losers))
        winner_name = winners[0]["replacement"]
        row = self.service.get_tender("sup1", "supervisor", self.tender_id)
        stored = next(r for r in row["recusals"] if r["id"] == recusal["id"])
        self.assertEqual(winner_name, stored["replacement_evaluator"])
        # 失败监督员用同一原回避记录重试：相同接替人幂等成功；换另一人被拒（只留一个接替人）。
        same = self.service.transfer_recusal("sup1", "supervisor", recusal["id"], winner_name)
        self.assertEqual("done", same["handover_status"])
        loser_name = losers[0]["replacement"]
        with self.assertRaises(DomainError) as ctx:
            self.service.transfer_recusal("sup1", "supervisor", recusal["id"], loser_name)
        self.assertEqual(409, ctx.exception.status)
        # 失败方可接手另一条回避记录（从其他原席位重试）。
        other = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval2", self.vendor2["id"], "回避二")
        second = self.service.transfer_recusal("sup1", "supervisor", other["id"], loser_name)
        self.assertEqual(loser_name, second["replacement_evaluator"])

    def test_only_supervisor_can_confirm_handover(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "回避")
        with self.assertRaises(DomainError) as ctx:
            self.service.transfer_recusal("proc1", "procurement", recusal["id"], "eval3")
        self.assertEqual(403, ctx.exception.status)

    def test_failed_transfer_leaves_audit_trail(self):
        recusal = self.service.declare_recusal(
            "proc1", "procurement", self.tender_id, "eval1", self.vendor1["id"], "回避")
        with self.assertRaises(DomainError):
            self.service.transfer_recusal("sup1", "supervisor", recusal["id"], "eval2")
        state = self.service.state("aud1", "auditor")
        failed = [t for t in state["timeline"] if t["action"] == "recusal.transfer"]
        self.assertTrue(failed, "失败交接应写入审计")
        self.assertIn("failed", failed[0]["details"])


if __name__ == "__main__":
    unittest.main()
