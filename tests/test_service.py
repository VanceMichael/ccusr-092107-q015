"""服务层规则：复查前置、迟到屏障、终态隔离、溯源链。"""

import unittest
from datetime import datetime
from pathlib import Path

from src.loader import load_world
from src.service import ClosureService, RuleViolation


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ClosureServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ClosureService(load_world(Path("fixtures")))

    def _ingest(self, raw: dict, arrival: str) -> object:
        return self.service.ingest(raw, ts(arrival))

    def _track(self, device="RADAR-QN-01", when="2026-09-30T08:00:00",
               kp=46.0, sog=13.5, track="TRK-T1") -> dict:
        return {
            "device_id": device, "obs_ts": when, "kind": "track",
            "track_id": track, "kp": kp, "sog_kn": sog,
        }

    def test_violation_requires_passed_recheck_before_close(self):
        outcome = self._ingest(self._track(), "2026-09-30T08:00:03")
        case = self.service.store.get(outcome.case_id)
        self._drive_to_dispatch(case, "B-09", "2026-09-30T08:05:00")
        self.service.board(
            case, ts("2026-09-30T08:25:00"), "OF-WEI",
            "violation", "超速",
        )
        with self.assertRaises(RuleViolation):
            self.service.close(case, ts("2026-09-30T08:30:00"), "OF-HUANG")
        # 复查不合格仍不能关
        self.service.recheck(
            case, ts("2026-09-30T08:40:00"), "OF-WEI", compliant=False
        )
        with self.assertRaises(RuleViolation):
            self.service.close(case, ts("2026-09-30T08:41:00"), "OF-HUANG")
        # 复查合格后闭环
        self.service.recheck(
            case, ts("2026-09-30T09:00:00"), "OF-WEI", compliant=True
        )
        self.service.close(case, ts("2026-09-30T09:05:00"), "OF-HUANG")
        self.assertEqual("CLOSED", case.status)

    def _drive_to_dispatch(self, case, berth: str, at: str) -> None:
        t0 = ts(at)
        from datetime import timedelta
        self.service.confirm(case, t0 - timedelta(minutes=2), "OF-HUANG", "核验")
        self.service.push(case, t0 - timedelta(minutes=1, seconds=30), "OF-HUANG")
        self.service.call(case, t0 - timedelta(minutes=1), "减速接受检查",
                          actor="OF-HUANG", ack="收到")
        self.service.dispatch(case, t0, berth)

    def test_late_ais_after_confirm_merges_but_never_regresses(self):
        raw_track = self._track(
            device="RADAR-MD-01", when="2026-09-30T06:00:00",
            kp=31.7, sog=6.0, track="TRK-7781",
        )
        outcome = self._ingest(raw_track, "2026-09-30T06:00:02")
        case = self.service.store.get(outcome.case_id)
        self.service.confirm(
            case, ts("2026-09-30T06:03:00"), "OF-HUANG", "确认目标"
        )
        self.assertEqual("CONFIRMED", case.status)

        # 迟到 11 分钟的 AIS 补报
        late_ais = {
            "device_id": "AIS-MD-01", "obs_ts": "2026-09-30T05:59:10",
            "kind": "ais_target", "mmsi": "413000123",
            "kp": 31.55, "sog_kn": 6.0, "backfill": True,
        }
        self._ingest(late_ais, "2026-09-30T06:14:00")

        # 身份归并成功，但状态停在人工确认的位置
        self.assertEqual("CONFIRMED", case.status)
        self.assertEqual("V-GUIYU01", self.service.ledger.vessel_of(case.cluster_id))
        self.assertEqual(1, len(case.late_evidence))

    def test_new_finding_after_close_is_quarantined_not_reopened(self):
        outcome = self._ingest(self._track(), "2026-09-30T08:00:03")
        case = self.service.store.get(outcome.case_id)
        self._drive_to_dispatch(case, "B-09", "2026-09-30T08:05:00")
        self.service.board(
            case, ts("2026-09-30T08:25:00"), "OF-WEI",
            "compliant", "现场正常",
        )
        self.service.close(case, ts("2026-09-30T08:30:00"), "OF-HUANG")

        # 闭环后补报同一航迹更早的超速报文（不同观测时刻、不同指纹）
        late_track = self._track(when="2026-09-30T07:58:00", sog=14.2)
        late_track["backfill"] = True
        self._ingest(late_track, "2026-09-30T09:30:00")

        self.assertEqual("CLOSED", case.status)
        self.assertEqual(1, len(case.quarantined))
        self.assertEqual("terminal-late-finding", case.quarantined[0].reason)

        # 隔离数据必须进值班员复核队列，保持可见
        queue = self.service.review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual(case.case_id, queue[0]["case_id"])
        self.assertEqual(case.quarantined[0].obs_id, queue[0]["obs_id"])

    def test_lineage_traces_field_result_back_to_radar_and_ais(self):
        outcome = self._ingest(self._track(), "2026-09-30T08:00:03")
        late_ais = {
            "device_id": "AIS-QN-01", "obs_ts": "2026-09-30T07:59:50",
            "kind": "ais_target", "mmsi": "413000789",
            "kp": 45.9, "sog_kn": 13.2, "backfill": True,
        }
        self._ingest(late_ais, "2026-09-30T08:02:30")
        case = self.service.store.get(outcome.case_id)
        self._drive_to_dispatch(case, "B-09", "2026-09-30T08:05:00")

        lineage = self.service.lineage(case)
        kinds = {b["device_kind"] for b in lineage["basis"]}
        self.assertIn("radar", kinds)
        self.assertIn("ais_station", kinds)
        # 时间线从 RiskDetected 起按处置顺序排列
        types = [e["type"] for e in lineage["timeline"]]
        self.assertEqual(
            ["RiskDetected", "RiskConfirmed", "AlertPushed",
             "VesselCalled", "PatrolDispatched"],
            types,
        )
        # 身份归并解释链可从现场结果反查
        self.assertTrue(
            any("co-observation" in line for line in lineage["identity_explanations"])
        )


if __name__ == "__main__":
    unittest.main()
