"""巡查资源调度：人员、巡查艇与锚地泊位的时空冲突控制。

三类冲突在任务下达时一次性校验：

1. **艇/人员时间重叠**：同一艘艇、同一名执法者不能在两个任务的
   时段内同时出现；
2. **可达性**：艇从上一任务位置（或停泊基地）赶到目标里程需要
   时间，要求的到场时间早于最快 ETA 时拒绝并给出建议时间；
3. **锚地泊位**：登临锚泊船需要占用泊位水域，同一泊位的任务/
   船舶占用时段不得重叠，冲突时推荐同锚地的空闲泊位。

复查任务默认复用登临原班人马；原班人马有冲突时自动向后顺延
（15 分钟一档），并把顺延结果记录在任务备注中。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .model import DomainError, Risk, parse_ts

SLOT_STEP = timedelta(minutes=15)


def ceil_slot(t: datetime) -> datetime:
    """向上取整到 15 分钟档。"""
    minute = ((t.minute // 15) + 1) * 15
    base = t.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=minute)


@dataclass
class BoatTask:
    task_id: str
    kind: str                    # boarding / recheck
    risk_id: str
    boat_id: str
    person_ids: list[str]
    start: datetime              # 到场时间
    end: datetime
    segment_id: str
    chainage_km: float
    anchorage_id: str | None = None
    berth_id: str | None = None
    status: str = "scheduled"    # scheduled / active / done / cancelled
    created_by: str = ""
    note: str = ""
    cancel_reason: str | None = None


@dataclass
class _BerthHold:
    berth_id: str
    start: datetime
    end: datetime
    occupant: str                # 船名或任务号


class Dispatcher:
    def __init__(self, canal: dict[str, Any], engine: Any | None = None) -> None:
        self.canal = canal
        self.engine = engine
        self.boats = {b["id"]: b for b in canal.get("boats", [])}
        self.persons = {p["id"]: p for p in canal.get("persons", [])}
        self.anchorages = {a["id"]: a for a in canal.get("anchorages", [])}
        self.tasks: list[BoatTask] = []
        self._holds: list[_BerthHold] = []
        self._seq = 0

        for occ in canal.get("berth_occupancy", []):
            self._holds.append(_BerthHold(
                berth_id=occ["berth_id"],
                start=parse_ts(occ["start"]),
                end=parse_ts(occ["end"]),
                occupant=occ["vessel"],
            ))

    # ------------------------------------------------------------------ #
    # 任务下达
    # ------------------------------------------------------------------ #

    def assign(self, *, risk: Risk, boat_id: str, person_ids: list[str],
               start: datetime, duration_min: int, actor: str,
               anchorage_id: str | None = None,
               berth_id: str | None = None,
               kind: str = "boarding") -> BoatTask:
        boat = self.boats.get(boat_id)
        if boat is None:
            raise DomainError(f"巡查艇 {boat_id} 不存在")
        if boat.get("status") == "repair":
            raise DomainError(f"巡查艇 {boat_id} 维修中，不可排班")
        self._check_crew(person_ids)

        end = start + timedelta(minutes=duration_min)

        # 1) 艇/人员时间冲突
        busy = self._busy_conflicts(boat_id, person_ids, start, end)
        if busy:
            raise DomainError("资源时间冲突：" + "；".join(busy))

        # 2) 可达性：沿运河里程线性估算，位置取该时刻之前最后一个任务的终点
        speed = float(boat.get("speed_kmh", 20))
        from_seg, from_km, free_at = self._position_at(boat_id, start)
        travel_h = abs(risk.chainage_km - from_km) / speed
        eta = free_at + timedelta(hours=travel_h)
        if start < eta:
            raise DomainError(
                f"{boat_id} 最快 {eta:%H:%M} 才能从 {from_seg}"
                f"（里程{from_km}）赶到现场，"
                f"无法在 {start:%H:%M} 到场；建议不早于 {ceil_slot(eta):%H:%M} 派艇"
            )

        # 3) 锚地泊位
        chosen_berth = None
        if anchorage_id:
            anch = self.anchorages.get(anchorage_id)
            if anch is None:
                raise DomainError(f"锚地 {anchorage_id} 不存在")
            if berth_id:
                if berth_id not in {b["id"] for b in anch["berths"]}:
                    raise DomainError(f"泊位 {berth_id} 不属于锚地 {anch['name']}")
                clash = next((h for h in self._holds
                              if h.berth_id == berth_id
                              and self._overlap(start, end, h.start, h.end)), None)
                if clash:
                    raise DomainError(
                        f"泊位 {berth_id} 在 {start:%H:%M}-{end:%H:%M} 被"
                        f"{clash.occupant} 占用（至 {clash.end:%H:%M}）"
                    )
                chosen_berth = berth_id
            else:
                chosen_berth = self._pick_berth(anch, start, end)

        self._seq += 1
        task = BoatTask(
            task_id=f"TASK-{self._seq:04d}",
            kind=kind, risk_id=risk.risk_id, boat_id=boat_id,
            person_ids=list(person_ids), start=start, end=end,
            segment_id=risk.segment_id, chainage_km=risk.chainage_km,
            anchorage_id=anchorage_id, berth_id=chosen_berth, created_by=actor,
        )
        if chosen_berth:
            self._holds.append(_BerthHold(chosen_berth, start, end, task.task_id))
        self.tasks.append(task)
        return task

    def _position_at(self, boat_id: str, at: datetime
                     ) -> tuple[str, float, datetime]:
        """推算艇在 ``at`` 之前最后一个未取消任务结束时的位置与空档起点。

        没有更早任务时，位置为停泊基地、空档起点为时间零点（始终可用）。
        """
        boat = self.boats[boat_id]
        seg, km = boat["base_segment"], float(boat["base_chainage_km"])
        free_at = datetime.min.replace(tzinfo=timezone.utc)
        prior = [t for t in self._live_tasks()
                 if t.boat_id == boat_id and t.end <= at]
        if prior:
            last = max(prior, key=lambda t: t.end)
            seg, km, free_at = last.segment_id, last.chainage_km, last.end
        return seg, km, free_at

    def schedule_recheck(self, risk: Risk, when: datetime, actor: str) -> BoatTask:
        """复查复用登临原班人马；冲突则顺延至最早可行档。"""
        board = self._task(risk.board_task_id) if risk.board_task_id else None
        if board is None:
            raise DomainError("缺少登临任务，无法自动排复查")
        start = when
        note = ""
        for _ in range(40):  # 最多向后找 10 小时
            try:
                task = self.assign(
                    risk=risk, boat_id=board.boat_id,
                    person_ids=board.person_ids, start=start,
                    duration_min=45, actor=actor, kind="recheck",
                )
                task.note = note or "复用登临原班人马"
                return task
            except DomainError as exc:
                msg = str(exc)
                if not (msg.startswith("资源时间冲突") or msg.startswith(f"{board.boat_id} 最快")):
                    raise
                start += SLOT_STEP
                note = f"原班人马/艇在 {when:%H:%M} 有冲突（{msg}），顺延至 {start:%H:%M}"
        raise DomainError("找不到可行的复查排班时段")

    def cancel_task(self, task_id: str, reason: str) -> BoatTask:
        task = self._task(task_id)
        if task.status in ("done", "cancelled"):
            return task
        task.status = "cancelled"
        task.cancel_reason = reason
        # 释放泊位占用
        self._holds = [h for h in self._holds if h.occupant != task_id]
        if self.engine is not None:
            risk = self.engine.risks.get(task.risk_id)
            if risk is not None:
                risk.notes.append(f"任务 {task_id} 取消：{reason}")
        return task

    def mark_done(self, task_id: str) -> BoatTask:
        task = self._task(task_id)
        task.status = "done"
        return task

    # ------------------------------------------------------------------ #
    # 冲突检查
    # ------------------------------------------------------------------ #

    def _live_tasks(self) -> list[BoatTask]:
        return [t for t in self.tasks if t.status in ("scheduled", "active")]

    def _overlap(self, a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
        return a0 < b1 and b0 < a1

    def _busy_conflicts(self, boat_id: str, person_ids: list[str],
                        start: datetime, end: datetime) -> list[str]:
        problems: list[str] = []
        for t in self._live_tasks():
            if not self._overlap(start, end, t.start, t.end):
                continue
            if t.boat_id == boat_id:
                problems.append(f"{boat_id} 另有任务 {t.task_id}（{t.start:%H:%M}-{t.end:%H:%M}）")
            overlap_people = sorted(set(person_ids) & set(t.person_ids))
            for pid in overlap_people:
                p = self.persons[pid]
                problems.append(
                    f"{p['name']}（{pid}）另有任务 {t.task_id}"
                    f"（{t.start:%H:%M}-{t.end:%H:%M}）"
                )
        return problems

    def _check_crew(self, person_ids: list[str]) -> None:
        if len(set(person_ids)) < 2:
            raise DomainError("登临任务至少安排 2 名执法人员")
        unknown = [p for p in person_ids if p not in self.persons]
        if unknown:
            raise DomainError(f"人员不存在：{','.join(unknown)}")
        roles = {self.persons[p]["role"] for p in person_ids}
        if "coxswain" not in roles:
            raise DomainError("艇组中必须包含 1 名驾艇员（coxswain）")
        if "inspector" not in roles:
            raise DomainError("艇组中必须包含 1 名登临检查员（inspector）")

    def _pick_berth(self, anch: dict[str, Any],
                    start: datetime, end: datetime) -> str:
        berths = [b["id"] for b in anch["berths"]]
        free = [
            bid for bid in berths
            if not any(h.berth_id == bid and self._overlap(start, end, h.start, h.end)
                       for h in self._holds)
        ]
        if not free:
            occupied = [
                f"{bid} 被 {next(h.occupant for h in self._holds if h.berth_id == bid)} 占用"
                for bid in berths
            ]
            raise DomainError(
                f"锚地 {anch['name']} 该时段无空闲泊位（{'；'.join(occupied)}），"
                "请改时或改靠邻近锚地"
            )
        return free[0]

    # ------------------------------------------------------------------ #
    # 建议与视图
    # ------------------------------------------------------------------ #

    def suggest(self, risk: Risk, start: datetime, duration_min: int = 60
                ) -> list[dict[str, Any]]:
        """为值班员推荐能按时到场且无冲突的艇组。"""
        end = start + timedelta(minutes=duration_min)
        crews = self._crews_by_boat()
        options: list[dict[str, Any]] = []
        for bid, boat in self.boats.items():
            if boat.get("status") == "repair":
                continue
            from_seg, from_km, free_at = self._position_at(bid, start)
            travel_h = abs(risk.chainage_km - from_km) \
                / float(boat.get("speed_kmh", 20))
            eta = free_at + timedelta(hours=travel_h)
            for crew in crews.get(bid, []):
                busy = bool(self._busy_conflicts(bid, crew, start, end))
                options.append({
                    "boat_id": bid,
                    "crew": crew,
                    "eta": eta.isoformat(),
                    "feasible": start >= eta and not busy,
                })
        return options

    def _crews_by_boat(self) -> dict[str, list[list[str]]]:
        """每艘艇的常驻班组（fixture 中 group 字段）。"""
        groups: dict[str, list[str]] = {}
        for pid, p in self.persons.items():
            groups.setdefault(p.get("boat_group", ""), []).append(pid)
        result: dict[str, list[list[str]]] = {}
        for bid, boat in self.boats.items():
            crew = groups.get(boat.get("crew_group", ""), [])
            roles = {self.persons[p]["role"] for p in crew}
            if {"coxswain", "inspector"} <= roles:
                result[bid] = [sorted(crew)]
        return result

    def task_board(self) -> list[dict[str, Any]]:
        return [
            {
                "task_id": t.task_id,
                "kind": t.kind,
                "risk_id": t.risk_id,
                "boat_id": t.boat_id,
                "crew": t.person_ids,
                "start": t.start.isoformat(),
                "end": t.end.isoformat(),
                "berth": t.berth_id,
                "status": t.status,
                "note": t.note,
                "cancel_reason": t.cancel_reason,
            }
            for t in self.tasks
        ]

    def anchorage_board(self) -> list[dict[str, Any]]:
        out = []
        for aid, anch in self.anchorages.items():
            berths = []
            for b in anch["berths"]:
                holds = [
                    {"from": h.start.isoformat(), "to": h.end.isoformat(),
                     "occupant": h.occupant}
                    for h in self._holds if h.berth_id == b["id"]
                ]
                berths.append({"berth_id": b["id"], "depth_m": b.get("depth_m"),
                               "occupied": holds})
            out.append({"anchorage_id": aid, "name": anch["name"], "berths": berths})
        return out

    def _task(self, task_id: str) -> BoatTask:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise DomainError(f"任务 {task_id} 不存在")
