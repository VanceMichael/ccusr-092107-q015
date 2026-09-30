"""巡查资源调度：人员、巡查艇与锚地泊位的时空冲突协调。

规则：
- 巡查艇与执法人员在“派单 → 登临完成”期间被占用；违规案件复查期间
  艇与人员可释放执行别的任务，泊位仍被涉案船舶占用至案件关闭/撤销。
- 目标泊位被占时，优先在同一锚地内改派空闲泊位并登记改派原因；
  本航段无可用艇时按预计到达时间选择邻段支援艇（跨航段支援）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import Anchorage, Berth, Officer, PatrolBoat, World

KNOT_TO_KMH = 1.852


class SchedulingConflict(Exception):
    """无艇、无执法人员或无可用泊位，需要值班员人工协调。"""


@dataclass
class Assignment:
    case_id: str
    boat: PatrolBoat
    officers: list[Officer]
    anchorage: Anchorage
    berth: Berth
    requested_berth_id: str
    berth_substituted: bool
    cross_segment: bool
    eta_minutes: float

    def to_payload(self) -> dict:
        return {
            "boat_id": self.boat.boat_id,
            "boat_name": self.boat.name,
            "officer_ids": [o.officer_id for o in self.officers],
            "anchorage_id": self.anchorage.anchorage_id,
            "berth_id": self.berth.berth_id,
            "requested_berth_id": self.requested_berth_id,
            "berth_substituted": self.berth_substituted,
            "cross_segment": self.cross_segment,
            "eta_minutes": round(self.eta_minutes, 1),
        }


@dataclass
class _BerthUse:
    case_id: str
    vessel_id: str | None
    since: datetime


class Scheduler:
    def __init__(self, world: World) -> None:
        self.world = world
        self._assignments: dict[str, Assignment] = {}
        self._boat_busy: dict[str, str] = {}
        self._officer_busy: dict[str, str] = {}
        self._berth_use: dict[str, _BerthUse] = {}

    # ------------------------------------------------------------------

    def assign(
        self,
        case_id: str,
        segment_id: str,
        requested_berth_id: str,
        at: datetime,
        vessel_id: str | None = None,
    ) -> Assignment:
        berth, anchorage, substituted = self._resolve_berth(requested_berth_id)
        boat = self._pick_boat(segment_id, anchorage, at)
        officers = self._pick_officers(boat)

        self._berth_use[berth.berth_id] = _BerthUse(case_id, vessel_id, at)
        self._boat_busy[boat.boat_id] = case_id
        for officer in officers:
            self._officer_busy[officer.officer_id] = case_id

        eta = self._eta_minutes(boat, anchorage)
        assignment = Assignment(
            case_id=case_id,
            boat=boat,
            officers=officers,
            anchorage=anchorage,
            berth=berth,
            requested_berth_id=requested_berth_id,
            berth_substituted=substituted,
            cross_segment=boat.segment_id != segment_id,
            eta_minutes=eta,
        )
        self._assignments[case_id] = assignment
        return assignment

    def assignment_of(self, case_id: str) -> Assignment | None:
        return self._assignments.get(case_id)

    def release_field_crew(self, case_id: str) -> None:
        """登临完成：释放巡查艇与人员（泊位继续保留）。"""
        assignment = self._assignments.get(case_id)
        if not assignment:
            return
        self._boat_busy.pop(assignment.boat.boat_id, None)
        for officer in assignment.officers:
            self._officer_busy.pop(officer.officer_id, None)

    def release_berth(self, case_id: str) -> None:
        """案件关闭/撤销：释放泊位。"""
        assignment = self._assignments.get(case_id)
        if not assignment:
            return
        self._berth_use.pop(assignment.berth.berth_id, None)

    def cancel_dispatch(self, case_id: str) -> None:
        """派单后撤销：全部资源立即释放。"""
        assignment = self._assignments.pop(case_id, None)
        if not assignment:
            return
        self._boat_busy.pop(assignment.boat.boat_id, None)
        for officer in assignment.officers:
            self._officer_busy.pop(officer.officer_id, None)
        self._berth_use.pop(assignment.berth.berth_id, None)

    def berth_owner(self, berth_id: str) -> str | None:
        use = self._berth_use.get(berth_id)
        return use.case_id if use else None

    # ------------------------------------------------------------------

    def _resolve_berth(self, requested_berth_id: str) -> tuple[Berth, Anchorage, bool]:
        if requested_berth_id not in self.world.berths:
            raise SchedulingConflict(f"未知泊位 {requested_berth_id}")
        requested = self.world.berths[requested_berth_id]
        anchorage = self.world.anchorages[requested.anchorage_id]
        if requested_berth_id not in self._berth_use:
            return requested, anchorage, False
        # 同锚地改派空闲泊位
        for berth in sorted(anchorage.berths, key=lambda b: b.berth_id):
            if berth.berth_id not in self._berth_use:
                return berth, anchorage, True
        raise SchedulingConflict(
            f"锚地 {anchorage.name} 无空闲泊位（申请 {requested_berth_id}）"
        )

    def _pick_boat(
        self, segment_id: str, target: Anchorage, at: datetime
    ) -> PatrolBoat:
        free = [b for b in self.world.boats.values() if b.boat_id not in self._boat_busy]
        if not free:
            raise SchedulingConflict("所有巡查艇均在执行任务")
        target_kp = min(b.kp for b in target.berths)

        def score(boat: PatrolBoat) -> tuple[int, float, str]:
            home = self.world.anchorages[boat.home_anchorage_id]
            home_kp = min(b.kp for b in home.berths)
            same_segment = 0 if boat.segment_id == segment_id else 1
            return same_segment, abs(home_kp - target_kp), boat.boat_id

        return sorted(free, key=score)[0]

    def _pick_officers(self, boat: PatrolBoat) -> list[Officer]:
        crew = sorted(
            (
                o
                for o in self.world.officers.values()
                if o.role == "boarding"
                and o.home_segment_id == boat.segment_id
                and o.officer_id not in self._officer_busy
            ),
            key=lambda o: o.officer_id,
        )
        if not crew:
            raise SchedulingConflict(f"{boat.name} 无空闲执法人员编组")
        return [crew[0]]

    def _eta_minutes(self, boat: PatrolBoat, target: Anchorage) -> float:
        home = self.world.anchorages[boat.home_anchorage_id]
        home_kp = min(b.kp for b in home.berths)
        target_kp = min(b.kp for b in target.berths)
        distance_km = abs(home_kp - target_kp)
        return distance_km / (boat.speed_kn * KNOT_TO_KMH) * 60.0
