"""案件状态机：人工屏障、终态不可倒退、复查前置。"""

import unittest
from datetime import datetime

from src.case import (
    CLOSED,
    DETECTED,
    REVOKED,
    CaseStore,
    IllegalTransition,
)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class CaseLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CaseStore()
        self.case = self.store.create("CL-1", "SEG-MD",
                                      ts("2026-09-30T06:00:00"),
                                      ts("2026-09-30T06:00:02"))

    def _advance_to_dispatched(self) -> None:
        t = ts("2026-09-30T06:03:00")
        self.store.apply(self.case, "RiskConfirmed", t, t, "OF-HUANG")
        t = ts("2026-09-30T06:03:30")
        self.store.apply(self.case, "AlertPushed", t, t, "OF-HUANG")
        t = ts("2026-09-30T06:04:00")
        self.store.apply(self.case, "VesselCalled", t, t, "OF-HUANG")
        t = ts("2026-09-30T06:05:00")
        self.store.apply(self.case, "PatrolDispatched", t, t, None, {})

    def test_starts_detected_and_requires_human_confirm(self):
        self.assertEqual(DETECTED, self.case.status)

    def test_commands_must_follow_order(self):
        t = ts("2026-09-30T06:03:00")
        # 未确认不能直接推送
        with self.assertRaises(IllegalTransition):
            self.store.apply(self.case, "AlertPushed", t, t, "OF-HUANG")
        # 更不能跳过登临直接关闭
        with self.assertRaises(IllegalTransition):
            self.store.apply(self.case, "CaseClosed", t, t, "OF-HUANG")
        self.assertEqual(DETECTED, self.case.status)

    def test_compliant_boarding_can_close_without_recheck(self):
        self._advance_to_dispatched()
        t = ts("2026-09-30T06:30:00")
        self.store.apply(
            self.case, "BoardingCompleted", t, t, "OF-LIN",
            {"result": "compliant"},
        )
        t = ts("2026-09-30T06:35:00")
        self.store.apply(self.case, "CaseClosed", t, t, "OF-HUANG")
        self.assertEqual(CLOSED, self.case.status)

    def test_terminal_rejects_every_command(self):
        self._advance_to_dispatched()
        t = ts("2026-09-30T06:30:00")
        self.store.apply(self.case, "BoardingCompleted", t, t, "OF-LIN",
                         {"result": "compliant"})
        t = ts("2026-09-30T06:35:00")
        self.store.apply(self.case, "CaseClosed", t, t, "OF-HUANG")
        with self.assertRaises(IllegalTransition):
            self.store.apply(
                self.case, "RiskConfirmed", t, t, "OF-HUANG"
            )
        with self.assertRaises(IllegalTransition):
            self.store.apply(
                self.case, "BoardingCompleted", t, t, "OF-LIN", {}
            )

    def test_revoke_records_cancellation_notice(self):
        t = ts("2026-09-30T10:02:00")
        self.store.apply(self.case, "RiskConfirmed", t, t, "OF-HUANG")
        t = ts("2026-09-30T10:02:30")
        self.store.apply(self.case, "AlertPushed", t, t, "OF-HUANG")
        t = ts("2026-09-30T10:05:00")
        self.store.apply(
            self.case, "RiskRevoked", t, t, "OF-HUANG",
            {"reason": "视频复核无目标"},
        )
        self.assertEqual(REVOKED, self.case.status)
        kinds = [n["kind"] for n in self.case.notices]
        self.assertIn("alert", kinds)
        self.assertIn("cancellation", kinds)

    def test_revoke_allowed_before_dispatch_only(self):
        self._advance_to_dispatched()
        t = ts("2026-09-30T06:30:00")
        with self.assertRaises(IllegalTransition):
            self.store.apply(
                self.case, "RiskRevoked", t, t, "OF-HUANG",
                {"reason": "不应在派单后撤销"},
            )

    def test_client_id_dedup_for_offline_retry(self):
        self._advance_to_dispatched()
        t = ts("2026-09-30T15:25:00")
        payload = {"result": "violation"}
        self.store.apply(
            self.case, "BoardingCompleted", t, t, "OF-LUO", payload,
            client_id="field-1",
        )
        # 同一现场端操作补传重放：拒绝第二条
        with self.assertRaises(KeyError):
            self.store.apply(
                self.case, "BoardingCompleted", t,
                ts("2026-09-30T16:06:00"), "OF-LUO", payload,
                client_id="field-1",
            )
        board_events = [
            e for e in self.case.events if e.type == "BoardingCompleted"
        ]
        self.assertEqual(1, len(board_events))


if __name__ == "__main__":
    unittest.main()
