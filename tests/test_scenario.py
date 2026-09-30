"""用 fixtures/events.json 的四个样例场景验收完整处置闭环。"""

import unittest
from pathlib import Path

from src.scenario import ScenarioRunner

FIXTURES = Path("fixtures")


class ScenarioAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = ScenarioRunner(FIXTURES)
        cls.results = {r.scenario_id: r for r in cls.runner.run_all()}

    def _case(self, scenario_id: str, ref: int = 0):
        result = self.results[scenario_id]
        return result.service.store.get(result.case_refs[ref])

    def test_a_fog_full_closure_keeps_human_state(self):
        result = self.results["A-FOG-MD"]
        case = self._case("A-FOG-MD")

        self.assertEqual(1, len(result.cases))
        self.assertEqual("CLOSED", case.status)
        self.assertEqual("V-GUIYU01", result.service.ledger.vessel_of(case.cluster_id))
        self.assertEqual(3, len(result.service.ledger.aliases_of(case.cluster_id)))
        self.assertEqual(case.dispatch["berth_id"], "B-07")
        self.assertEqual(case.dispatch["boat_id"], "PT-101")
        self.assertEqual(case.boarding["result"], "violation")
        self.assertTrue(case.recheck_passed)
        # 闭环后补报的 AIS 只作迟到证据，状态不倒退
        self.assertEqual(1, len(case.late_evidence))
        self.assertEqual("CLOSED", case.status)
        lineage = result.service.lineage(case)
        devices = {b["device_id"] for b in lineage["basis"]}
        self.assertEqual(
            {"RADAR-MD-01", "CAM-MD-02", "AIS-MD-01", "WD-MD-01"}, devices
        )
        # 身份归并可解释：三种规则都有决策留痕
        rules = {d.rule for d in result.service.ledger.decisions}
        self.assertIn("registry:mmsi", rules)
        self.assertIn("registry:plate", rules)
        self.assertIn("co-observation", rules)

    def _expect(self, scenario_id: str):
        for raw in self.runner.scenarios:
            if raw["id"] == scenario_id:
                return raw["expect"]
        raise KeyError(scenario_id)

    def test_b_late_ais_merges_into_single_case(self):
        result = self.results["B-SPEED-QN"]
        case = self._case("B-SPEED-QN")
        # 雷达先建单，迟到 AIS 补报归并后不得开第二单
        self.assertEqual(1, len(result.cases))
        self.assertEqual("CLOSED", case.status)
        self.assertEqual("V-NANHAI08", result.service.ledger.vessel_of(case.cluster_id))
        self.assertEqual(2, len(result.service.ledger.aliases_of(case.cluster_id)))
        self.assertEqual(case.dispatch["berth_id"], "B-09")
        self.assertEqual(case.dispatch["boat_id"], "PT-202")
        self.assertEqual(1, len(case.late_evidence))
        # 传感器重复报文被去重
        self.assertEqual(1, result.service.gateway.report.duplicates)

    def test_c_berth_conflict_cross_segment_and_offline_flush(self):
        result = self.results["C-LEVEL-QS"]
        first = self._case("C-LEVEL-QS", 0)
        second = self._case("C-LEVEL-QS", 1)

        self.assertEqual(2, len(result.cases))
        self.assertEqual("CLOSED", first.status)
        self.assertEqual("V-YUJIANG7", result.service.ledger.vessel_of(first.cluster_id))
        self.assertEqual("B-11", first.dispatch["berth_id"])
        self.assertEqual("PT-303", first.dispatch["boat_id"])
        self.assertFalse(first.dispatch["cross_segment"])

        # 第二艘船申请同一泊位 → 同锚地改派 B-12
        self.assertEqual("B-11", second.dispatch["requested_berth_id"])
        self.assertEqual("B-12", second.dispatch["berth_id"])
        self.assertTrue(second.dispatch["berth_substituted"])
        # 本航段巡查艇仍被前案占用 → 邻段 PT-202 支援
        self.assertEqual("PT-202", second.dispatch["boat_id"])
        self.assertTrue(second.dispatch["cross_segment"])
        self.assertEqual("violation", second.boarding["result"])

        # 断网补传：业务时间保留 15:25，到达时间为恢复后
        self.assertEqual(1, result.flushed_commands)
        self.assertEqual("2026-09-30T15:25:00", second.boarding["ts"].isoformat())
        self.assertGreater(second.boarding["arrival_ts"], second.boarding["ts"])
        self.assertTrue(second.boarding["offline_queued"])
        # 重传被幂等去重：只有一条登临事件
        board_events = [e for e in second.events if e.type == "BoardingCompleted"]
        self.assertEqual(1, len(board_events))
        self.assertEqual(1, result.service.gateway.report.duplicate_commands)

        lineage = result.service.lineage(second)
        devices = {b["device_id"] for b in lineage["basis"]}
        self.assertEqual({"RADAR-QS-02", "AIS-QS-01", "HYD-QS-01"}, devices)

    def test_d_false_alarm_revoked_late_data_does_not_reopen(self):
        result = self.results["D-FALSE-QN"]
        case = self._case("D-FALSE-QN")
        self.assertEqual(1, len(result.cases))
        self.assertEqual("REVOKED", case.status)
        self.assertIsNone(case.dispatch)
        # 撤销同时发出解除通知
        self.assertEqual("cancellation", case.notices[-1]["kind"])
        # 迟到 40 分钟的 AIS 报文不得翻案
        self.assertEqual(1, len(case.late_evidence))
        self.assertEqual("REVOKED", case.status)
        # 撤销依据（视频复核）可反查
        lineage = result.service.lineage(case)
        supporting = [b for b in lineage["basis"] if b["role"] == "supporting"]
        self.assertEqual("CAM-QN-03", supporting[0]["device_id"])

    def test_open_board_shows_every_unclosed_risk_mid_flight(self):
        """处置过程中值班看板必须能看到所有未闭环风险。"""
        raw = next(s for s in self.runner.scenarios if s["id"] == "C-LEVEL-QS")
        # 停在第一案登临完成、第二案已派单未登临的时点
        result = self.runner.run_one(
            raw, stop_after=lambda e: e.get("t") == "board" and e.get("ref") == 0
        )
        svc = result.service
        board = svc.open_risk_board()
        statuses = {row["status"] for row in board}
        self.assertEqual(2, len(board))
        self.assertIn("BOARDED", statuses)
        self.assertIn("DISPATCHED", statuses)
        for row in board:
            self.assertTrue(row["awaiting"])


if __name__ == "__main__":
    unittest.main()
