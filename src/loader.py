"""从 fixtures 读取航段、设备、船舶与巡查资源，构建 World。"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Anchorage, Berth, Device, Officer, PatrolBoat, Segment, Vessel, World, Zone


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_world(fixture_dir: Path) -> World:
    world = World()

    geo = _load(fixture_dir / "segments.json")
    for raw in geo["segments"]:
        zones = tuple(
            Zone(
                zone_id=z["zone_id"],
                name=z["name"],
                from_kp=z["from_kp"],
                to_kp=z["to_kp"],
                restricted=z.get("restricted", False),
            )
            for z in raw.get("zones", [])
        )
        world.segments[raw["segment_id"]] = Segment(
            segment_id=raw["segment_id"],
            name=raw["name"],
            from_kp=raw["from_kp"],
            to_kp=raw["to_kp"],
            speed_limit_kn=raw["speed_limit_kn"],
            zones=zones,
            shallow=raw.get("shallow", False),
        )
    for raw in geo["anchorages"]:
        berths = tuple(
            Berth(
                berth_id=b["berth_id"],
                name=b["name"],
                kp=b["kp"],
                anchorage_id=raw["anchorage_id"],
                segment_id=raw["segment_id"],
            )
            for b in raw["berths"]
        )
        for berth in berths:
            world.berths[berth.berth_id] = berth
        world.anchorages[raw["anchorage_id"]] = Anchorage(
            anchorage_id=raw["anchorage_id"],
            segment_id=raw["segment_id"],
            name=raw["name"],
            berths=berths,
        )

    for raw in _load(fixture_dir / "devices.json")["devices"]:
        device = Device(
            device_id=raw["device_id"],
            kind=raw["kind"],
            name=raw["name"],
            segment_id=raw["segment_id"],
            kp=raw["kp"],
            status=raw.get("status", "online"),
        )
        world.devices[device.device_id] = device

    for raw in _load(fixture_dir / "vessels.json")["vessels"]:
        vessel = Vessel(
            vessel_id=raw["vessel_id"],
            name=raw["name"],
            mmsi=raw["mmsi"],
            plate_no=raw["plate_no"],
            length_m=raw["length_m"],
            draft_m=raw["draft_m"],
            min_ukc_m=raw["min_ukc_m"],
        )
        world.vessels[vessel.vessel_id] = vessel

    resources = _load(fixture_dir / "resources.json")
    for raw in resources["patrol_boats"]:
        boat = PatrolBoat(
            boat_id=raw["boat_id"],
            name=raw["name"],
            home_anchorage_id=raw["home_anchorage_id"],
            segment_id=raw["segment_id"],
            speed_kn=raw["speed_kn"],
            officer_capacity=raw["officer_capacity"],
        )
        world.boats[boat.boat_id] = boat
    for raw in resources["officers"]:
        officer = Officer(
            officer_id=raw["officer_id"],
            name=raw["name"],
            role=raw["role"],
            home_segment_id=raw.get("home_segment_id"),
        )
        world.officers[officer.officer_id] = officer

    return world
