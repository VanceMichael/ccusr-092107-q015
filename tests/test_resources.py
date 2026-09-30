"""巡查艇、执法人员、锚地泊位的时空冲突协调。"""

import unittest
from datetime import datetime
from pathlib import Path

from src.loader import load_world
from src.resources import Scheduler, SchedulingConflict


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = Scheduler(load_world(Path("fixtures")))

    def test_first_assignment_gets_requested_berth_and_local_boat(self):
        a = self.scheduler.assign(
            "CASE-1", "SEG-MD", "B-07", ts("2026-09-30T06:05:00")
        )
        self.assertEqual("B-07", a.berth.berth_id)
        self.assertFalse(a.berth_substituted)
        self.assertEqual("PT-101", a.boat.boat_id)
        self.assertFalse(a.cross_segment)

    def test_occupied_berth_substitutes_within_same_anchorage(self):
        self.scheduler.assign(
            "CASE-1", "SEG-QS", "B-11", ts("2026-09-30T14:06:00")
        )
        second = self.scheduler.assign(
            "CASE-2", "SEG-QS", "B-11", ts("2026-09-30T14:18:00")
        )
        self.assertEqual("B-11", second.requested_berth_id)
        self.assertEqual("B-12", second.berth.berth_id)
        self.assertTrue(second.berth_substituted)

    def test_when_anchorage_full_raises_conflict(self):
        self.scheduler.assign(
            "CASE-1", "SEG-MD", "B-07", ts("2026-09-30T06:05:00")
        )
        self.scheduler.assign(
            "CASE-2", "SEG-MD", "B-08", ts("2026-09-30T06:20:00")
        )
        with self.assertRaises(SchedulingConflict):
            self.scheduler.assign(
                "CASE-3", "SEG-MD", "B-07", ts("2026-09-30T06:40:00")
            )

    def test_busy_local_boat_triggers_nearest_cross_segment_support(self):
        # 第一案占用企石 PT-303
        self.scheduler.assign(
            "CASE-1", "SEG-QS", "B-11", ts("2026-09-30T14:06:00")
        )
        second = self.scheduler.assign(
            "CASE-2", "SEG-QS", "B-12", ts("2026-09-30T14:18:00")
        )
        # 本航段无艇，按到企石锚地的距离选最近邻段 → 青年 PT-202
        self.assertTrue(second.cross_segment)
        self.assertEqual("PT-202", second.boat.boat_id)
        # 跨段艇配其母港航段（青年）空闲执法人员
        self.assertEqual("SEG-QN", second.officers[0].home_segment_id)

    def test_boarding_releases_boat_but_keeps_berth_until_close(self):
        a = self.scheduler.assign(
            "CASE-1", "SEG-QS", "B-11", ts("2026-09-30T14:06:00")
        )
        self.scheduler.release_field_crew("CASE-1")
        # 艇已释放，可被新任务使用
        self.assertNotIn(a.boat.boat_id, self.scheduler._boat_busy)
        # 泊位仍归案件
        self.assertEqual("CASE-1", self.scheduler.berth_owner("B-11"))
        self.scheduler.release_berth("CASE-1")
        self.assertIsNone(self.scheduler.berth_owner("B-11"))

    def test_cancel_dispatch_frees_everything(self):
        a = self.scheduler.assign(
            "CASE-1", "SEG-QN", "B-09", ts("2026-09-30T10:03:00")
        )
        self.scheduler.cancel_dispatch("CASE-1")
        self.assertIsNone(self.scheduler.assignment_of("CASE-1"))
        self.assertNotIn(a.boat.boat_id, self.scheduler._boat_busy)
        self.assertIsNone(self.scheduler.berth_owner("B-09"))


if __name__ == "__main__":
    unittest.main()
