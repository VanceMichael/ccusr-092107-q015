"""接入网关：报文去重、断网入队、恢复补传、重传幂等。"""

import unittest
from datetime import datetime
from pathlib import Path

from src.gateway import ObservationGateway, QueuedCommand
from src.loader import load_world


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ais_raw(mmsi="413000123", when="2026-09-30T06:00:00", kp=31.7, sog=6.0):
    return {
        "device_id": "AIS-MD-01",
        "obs_ts": when,
        "kind": "ais_target",
        "mmsi": mmsi,
        "kp": kp,
        "sog_kn": sog,
    }


class GatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ObservationGateway(load_world(Path("fixtures")))

    def test_duplicate_sensor_report_ingested_once(self):
        raw = ais_raw()
        first = self.gateway.ingest_observation(raw, ts("2026-09-30T06:00:02"))
        second = self.gateway.ingest_observation(raw, ts("2026-09-30T06:00:05"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, self.gateway.report.duplicates)

    def test_different_obs_time_is_distinct_report(self):
        a = self.gateway.ingest_observation(
            ais_raw(when="2026-09-30T06:00:00"), ts("2026-09-30T06:00:02")
        )
        b = self.gateway.ingest_observation(
            ais_raw(when="2026-09-30T06:00:30"), ts("2026-09-30T06:00:32")
        )
        self.assertNotEqual(a.obs_id, b.obs_id)

    def test_late_report_preserves_observation_time(self):
        raw = ais_raw(when="2026-09-30T05:58:30", kp=31.3)
        raw["backfill"] = True
        out = self.gateway.ingest_observation(raw, ts("2026-09-30T08:20:00"))
        self.assertEqual(ts("2026-09-30T05:58:30"), out.obs_ts)
        self.assertEqual(ts("2026-09-30T08:20:00"), out.arrival_ts)
        self.assertTrue(out.backfill)

    def test_offline_command_queues_and_flushes_once_with_dedup(self):
        delivered: list[str] = []

        def handler(cmd: QueuedCommand) -> None:
            delivered.append(cmd.client_id)

        cmd = QueuedCommand(
            handler="board", case_id="CASE-1",
            at=ts("2026-09-30T15:25:00"), actor="OF-LUO",
            payload={"result": "violation"}, client_id="field-1",
        )

        # 在线时立即投递
        self.assertEqual("delivered", self.gateway.submit_command(handler, cmd))
        # 已投递 client_id 的重放直接幂等拒绝
        self.assertEqual("duplicate", self.gateway.submit_command(handler, cmd))

        # 断网后操作入队
        self.gateway.set_offline(True)
        offline_cmd = QueuedCommand(
            handler="board", case_id="CASE-2",
            at=ts("2026-09-30T15:30:00"), actor="OF-LUO",
            payload={"result": "violation"}, client_id="field-2",
        )
        self.assertEqual("queued", self.gateway.submit_command(handler, offline_cmd))

        # 恢复后补传一次；再重放同 client_id 被去重
        self.gateway.set_offline(False)
        self.assertEqual(1, self.gateway.flush(handler))
        self.gateway.flush(handler)
        self.assertEqual(delivered, ["field-1", "field-2"])
        self.assertEqual(1, self.gateway.report.duplicate_commands)


if __name__ == "__main__":
    unittest.main()
