"""平陆运河双线巡航的世界模型：航段、锚地泊位、设备、船舶与巡查资源。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Zone:
    zone_id: str
    name: str
    from_kp: float
    to_kp: float
    restricted: bool = False

    def contains(self, kp: float) -> bool:
        return self.from_kp <= kp <= self.to_kp


@dataclass(frozen=True)
class Segment:
    segment_id: str
    name: str
    from_kp: float
    to_kp: float
    speed_limit_kn: float
    zones: tuple[Zone, ...] = ()
    shallow: bool = False

    def zone_at(self, kp: float) -> Zone | None:
        for zone in self.zones:
            if zone.contains(kp):
                return zone
        return None


@dataclass(frozen=True)
class Berth:
    berth_id: str
    name: str
    kp: float
    anchorage_id: str
    segment_id: str


@dataclass(frozen=True)
class Anchorage:
    anchorage_id: str
    segment_id: str
    name: str
    berths: tuple[Berth, ...]


@dataclass(frozen=True)
class Device:
    device_id: str
    kind: str
    name: str
    segment_id: str
    kp: float
    status: str = "online"


@dataclass(frozen=True)
class Vessel:
    vessel_id: str
    name: str
    mmsi: str
    plate_no: str
    length_m: float
    draft_m: float
    min_ukc_m: float


@dataclass(frozen=True)
class PatrolBoat:
    boat_id: str
    name: str
    home_anchorage_id: str
    segment_id: str
    speed_kn: float
    officer_capacity: int


@dataclass(frozen=True)
class Officer:
    officer_id: str
    name: str
    role: str  # boarding | duty
    home_segment_id: str | None


@dataclass
class World:
    segments: dict[str, Segment] = field(default_factory=dict)
    devices: dict[str, Device] = field(default_factory=dict)
    anchorages: dict[str, Anchorage] = field(default_factory=dict)
    berths: dict[str, Berth] = field(default_factory=dict)
    vessels: dict[str, Vessel] = field(default_factory=dict)
    boats: dict[str, PatrolBoat] = field(default_factory=dict)
    officers: dict[str, Officer] = field(default_factory=dict)

    def berths_of_anchorage(self, anchorage_id: str) -> list[Berth]:
        return list(self.anchorages[anchorage_id].berths)
