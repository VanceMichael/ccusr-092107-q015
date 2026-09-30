"""感知接入与现场上报网关。

职责：
- 传感器报文去重：同一设备同一时刻同一内容的重复上报只入池一次；
- 现场端命令幂等：断网期间的登临等操作先入本地队列，恢复后按
  client_id 补传，重复重传绝不产生第二条登临记录；
- 迟到数据照实标记观测时间与到达时间，交由上层屏障处理。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .identity import Alias
from .model import World


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class RawObservation:
    obs_id: str
    device_id: str
    obs_ts: datetime
    arrival_ts: datetime
    segment_id: str
    kind: str
    kp: float | None
    sog_kn: float | None
    values: dict[str, Any]
    aliases: tuple[Alias, ...]
    backfill: bool


@dataclass
class QueuedCommand:
    handler: str
    case_id: str
    at: datetime
    actor: str | None
    payload: dict[str, Any]
    client_id: str | None


@dataclass
class IngestReport:
    accepted: int = 0
    duplicates: int = 0
    flushed: int = 0
    duplicate_commands: int = 0


class ObservationGateway:
    def __init__(self, world: World) -> None:
        self.world = world
        self._seen_fingerprints: set[str] = set()
        self._offline = False
        self._queue: list[QueuedCommand] = []
        self._delivered_client_ids: set[str] = set()
        self.report = IngestReport()

    @property
    def offline(self) -> bool:
        return self._offline

    def set_offline(self, offline: bool) -> None:
        self._offline = offline

    def ingest_observation(
        self, raw: dict[str, Any], arrival: datetime
    ) -> RawObservation | None:
        """归一化传感器报文；重复报文返回 None。"""
        device = self.world.devices[raw["device_id"]]
        obs_ts = parse_ts(raw["obs_ts"])
        kind = raw["kind"]
        kp = raw.get("kp")
        sog = raw.get("sog_kn")
        values = dict(raw.get("values", {}))
        aliases = self._aliases(raw)
        fingerprint = self._fingerprint(
            device.device_id, obs_ts, kind, kp, sog, values, raw
        )
        if fingerprint in self._seen_fingerprints:
            self.report.duplicates += 1
            return None
        self._seen_fingerprints.add(fingerprint)
        self.report.accepted += 1
        return RawObservation(
            obs_id=f"OBS-{fingerprint[:10]}",
            device_id=device.device_id,
            obs_ts=obs_ts,
            arrival_ts=arrival,
            segment_id=device.segment_id,
            kind=kind,
            kp=kp,
            sog_kn=sog,
            values=values,
            aliases=tuple(aliases),
            backfill=bool(raw.get("backfill", False)),
        )

    def submit_command(
        self,
        handler: Callable[[QueuedCommand], None],
        queued: QueuedCommand,
    ) -> str:
        """在线则立即执行；离线则入队。已投递过的 client_id 直接去重。"""
        if queued.client_id and queued.client_id in self._delivered_client_ids:
            self.report.duplicate_commands += 1
            return "duplicate"
        if self._offline:
            self._queue.append(queued)
            return "queued"
        self._deliver(handler, queued)
        return "delivered"

    def flush(self, handler: Callable[[QueuedCommand], None]) -> int:
        """网络恢复：按入队顺序补传。"""
        count = 0
        pending = self._queue
        self._queue = []
        for queued in pending:
            if queued.client_id and queued.client_id in self._delivered_client_ids:
                self.report.duplicate_commands += 1
                continue
            self._deliver(handler, queued)
            count += 1
        self.report.flushed += count
        return count

    def _deliver(
        self, handler: Callable[[QueuedCommand], None], queued: QueuedCommand
    ) -> None:
        handler(queued)
        if queued.client_id:
            self._delivered_client_ids.add(queued.client_id)

    def _aliases(self, raw: dict[str, Any]) -> list[Alias]:
        aliases: list[Alias] = []
        if raw.get("mmsi"):
            aliases.append(Alias("ais", str(raw["mmsi"])))
        if raw.get("track_id"):
            aliases.append(Alias("radar", str(raw["track_id"])))
        if raw.get("plate_no"):
            aliases.append(Alias("video", str(raw["plate_no"])))
        return aliases

    def _fingerprint(
        self,
        device_id: str,
        obs_ts: datetime,
        kind: str,
        kp: float | None,
        sog: float | None,
        values: dict[str, Any],
        raw: dict[str, Any],
    ) -> str:
        material = json.dumps(
            {
                "d": device_id,
                "t": obs_ts.isoformat(),
                "k": kind,
                "kp": kp,
                "sog": sog,
                "v": values,
                "mmsi": raw.get("mmsi"),
                "track": raw.get("track_id"),
                "plate": raw.get("plate_no"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()
