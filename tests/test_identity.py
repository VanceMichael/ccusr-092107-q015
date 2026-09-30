"""多源身份归并：可解释、不误并、迟到报文可回溯归并。"""

import unittest
from datetime import datetime

from src.identity import Alias, IdentityLedger
from src.loader import load_world
from pathlib import Path


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class IdentityMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = IdentityLedger(load_world(Path("fixtures")))

    def test_registry_mmsi_and_plate_identify_registered_vessel(self):
        cid = self.ledger.observe(
            "o1", "SEG-MD", 32.0, ts("2026-09-30T06:00:00"),
            [Alias("ais", "413000123")],
        )
        self.assertEqual("V-GUIYU01", self.ledger.vessel_of(cid))
        self.assertEqual(
            "registry:mmsi", self.ledger.decisions[-1].rule
        )

        cid2 = self.ledger.observe(
            "o2", "SEG-MD", 32.01, ts("2026-09-30T06:00:20"),
            [Alias("video", "桂平货2021-0456")],
        )
        self.assertEqual(cid, cid2)
        self.assertEqual(2, len(self.ledger.aliases_of(cid)))
        rules = {d.rule for d in self.ledger.decisions}
        self.assertIn("registry:plate", rules)
        self.assertIn("co-observation", rules)

    def test_every_merge_is_explainable_with_gap_and_witness(self):
        cid = self.ledger.observe(
            "r1", "SEG-QN", 46.0, ts("2026-09-30T08:00:00"),
            [Alias("radar", "TRK-X")],
        )
        self.ledger.observe(
            "a1", "SEG-QN", 46.02, ts("2026-09-30T08:00:10"),
            [Alias("ais", "413000789")],
        )
        merge = next(d for d in self.ledger.decisions if d.rule == "co-observation")
        self.assertEqual({"r1", "a1"}, set(merge.witness_obs))
        self.assertIsNotNone(merge.kp_gap_km)
        self.assertIsNotNone(merge.dt_seconds)
        text = merge.explain()
        self.assertIn("co-observation", text)
        self.assertIn("TRK-X", text)
        # 溯源：簇的解释链覆盖全部决策
        self.assertTrue(self.ledger.explain(cid))

    def test_two_distinct_ais_targets_never_merge(self):
        self.ledger.observe(
            "v1", "SEG-QS", 57.0, ts("2026-09-30T14:00:00"),
            [Alias("ais", "413000555")],
        )
        cid2 = self.ledger.observe(
            "v2", "SEG-QS", 57.01, ts("2026-09-30T14:00:30"),
            [Alias("ais", "413000900")],
        )
        self.assertEqual("V-HENGFENG66", self.ledger.vessel_of(cid2))
        self.assertEqual(1, len(self.ledger.aliases_of(cid2)))

    def test_targets_outside_window_do_not_merge(self):
        c1 = self.ledger.observe(
            "r1", "SEG-MD", 32.0, ts("2026-09-30T06:00:00"),
            [Alias("radar", "TRK-A")],
        )
        c2 = self.ledger.observe(
            "a1", "SEG-MD", 32.0, ts("2026-09-30T06:05:00"),
            [Alias("ais", "413000789")],
        )
        self.assertNotEqual(c1, c2)

    def test_backfill_correlates_to_historical_target(self):
        cid = self.ledger.observe(
            "r1", "SEG-QN", 45.4, ts("2026-09-30T10:00:00"),
            [Alias("radar", "TRK-9100")],
        )
        # 40 分钟后才补报的 AIS，按观测时间回溯归并
        cid_late = self.ledger.observe(
            "a-late", "SEG-QN", 45.4, ts("2026-09-30T09:59:50"),
            [Alias("ais", "413000789")], backfill=True,
        )
        self.assertEqual(cid, cid_late)
        rules = {d.rule for d in self.ledger.decisions}
        self.assertIn("backfill-co-observation", rules)
        self.assertEqual("V-NANHAI08", self.ledger.vessel_of(cid))


if __name__ == "__main__":
    unittest.main()
