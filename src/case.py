"""风险处置案件：只进事件流上的状态机。

关键约束：
- 状态只能由人工命令推进；自动感知数据在 DETECTED 阶段可累积，
  案件一旦人工确认，任何迟到/补报数据都不能倒退或改写状态。
- REVOKED/CLOSED 为终态；迟到数据只允许作为证据附挂或被隔离复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 状态
DETECTED = "DETECTED"        # 电子巡查组发现风险，待人工确认
CONFIRMED = "CONFIRMED"      # 值班员已远程核验确认
PUSHED = "PUSHED"            # 预警已推送
CALLED = "CALLED"            # 已船舶叫应并取得应答
DISPATCHED = "DISPATCHED"    # 巡查艇任务已下达
BOARDED = "BOARDED"          # 登临检查完成（若为违规，待复查）
CLOSED = "CLOSED"            # 闭环
REVOKED = "REVOKED"          # 误报撤销

TERMINAL = {CLOSED, REVOKED}
HUMAN_LOCKED = {CONFIRMED, PUSHED, CALLED, DISPATCHED, BOARDED} | TERMINAL

# 人工命令 -> 要求的前置状态
TRANSITIONS: dict[str, set[str]] = {
    "RiskConfirmed": {DETECTED},
    "AlertPushed": {CONFIRMED},
    "VesselCalled": {PUSHED},
    "PatrolDispatched": {CALLED},
    "BoardingCompleted": {DISPATCHED},
    "RecheckCompleted": {BOARDED},
    "CaseClosed": {BOARDED},
    "RiskRevoked": {DETECTED, CONFIRMED, PUSHED, CALLED},
}


@dataclass(frozen=True)
class DomainEvent:
    seq: int
    event_id: str
    case_id: str
    type: str
    ts: datetime                     # 业务发生时间（现场时间）
    arrival_ts: datetime             # 到达平台时间
    actor: str | None                # 人工操作者；自动事件为 None
    payload: dict[str, Any] = field(default_factory=dict)
    client_id: str | None = None     # 现场端幂等标识（断网补传去重）

    @property
    def automatic(self) -> bool:
        return self.actor is None


@dataclass
class QuarantineItem:
    """被屏障挡下、需人工复核的数据（不影响案件状态）。"""

    obs_id: str
    reason: str
    detail: str
    at: datetime


@dataclass
class Case:
    case_id: str
    cluster_id: str
    segment_id: str
    status: str = DETECTED
    rule_ids: list[str] = field(default_factory=list)
    basis_obs: list[str] = field(default_factory=list)
    supporting_obs: list[str] = field(default_factory=list)
    late_evidence: list[str] = field(default_factory=list)
    quarantined: list[QuarantineItem] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    call: dict[str, Any] | None = None
    dispatch: dict[str, Any] | None = None
    boarding: dict[str, Any] | None = None
    recheck: dict[str, Any] | None = None
    recheck_passed: bool = False
    revocation: dict[str, Any] | None = None
    created_ts: datetime | None = None
    closed_ts: datetime | None = None
    events: list[DomainEvent] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_human_confirmed(self) -> bool:
        return self.status in HUMAN_LOCKED and self.status != DETECTED

    def has_event(self, event_type: str) -> bool:
        return any(e.type == event_type for e in self.events)


class IllegalTransition(Exception):
    """命令与当前状态不符（例如复查早于登临、终态后再来命令）。"""


class CaseStore:
    """案件集合：按到达顺序追加事件，重建当前状态。"""

    def __init__(self) -> None:
        self._cases: dict[str, Case] = {}
        self._case_seq = 0
        self._event_seq = 0
        self._event_ids: set[str] = set()

    def all(self) -> list[Case]:
        return list(self._cases.values())

    def get(self, case_id: str) -> Case:
        return self._cases[case_id]

    def open_cases(self) -> list[Case]:
        return [c for c in self._cases.values() if not c.is_terminal]

    def create(self, cluster_id: str, segment_id: str, ts: datetime, arrival: datetime) -> Case:
        self._case_seq += 1
        case_id = f"CASE-{self._case_seq:04d}"
        case = Case(case_id=case_id, cluster_id=cluster_id, segment_id=segment_id,
                    created_ts=ts)
        self._cases[case_id] = case
        self._record(case, "RiskDetected", ts, arrival, None, {})
        return case

    def case_for_cluster(self, cluster_id: str) -> Case | None:
        for case in self._cases.values():
            if case.cluster_id == cluster_id and not case.is_terminal:
                return case
        return None

    def existing_case_for_cluster(self, cluster_id: str) -> Case | None:
        """任意已存在（含终态）的案件，用于防重复开单。"""
        for case in self._cases.values():
            if case.cluster_id == cluster_id:
                return case
        return None

    def apply(
        self,
        case: Case,
        event_type: str,
        ts: datetime,
        arrival: datetime,
        actor: str | None,
        payload: dict[str, Any] | None = None,
        client_id: str | None = None,
        event_id: str | None = None,
    ) -> DomainEvent:
        payload = dict(payload or {})
        if event_id and event_id in self._event_ids:
            raise KeyError(f"重复事件: {event_id}")
        if client_id and any(
            e.client_id == client_id for e in case.events
        ):
            raise KeyError(f"幂等拦截 client_id={client_id}")

        if case.is_terminal:
            raise IllegalTransition(
                f"{case.case_id} 已终态 {case.status}，拒绝 {event_type}（状态不可倒退）"
            )

        allowed = TRANSITIONS.get(event_type, set())
        if case.status not in allowed:
            raise IllegalTransition(
                f"{case.case_id} 当前 {case.status}，不接受 {event_type}"
            )

        self._event_seq += 1
        event = DomainEvent(
            seq=self._event_seq,
            event_id=event_id or f"EVT-{self._event_seq:06d}",
            case_id=case.case_id,
            type=event_type,
            ts=ts,
            arrival_ts=arrival,
            actor=actor,
            payload=payload,
            client_id=client_id,
        )
        if event.event_id in self._event_ids:
            raise KeyError(f"重复事件: {event.event_id}")
        self._event_ids.add(event.event_id)
        case.events.append(event)
        self._project(case, event)
        return event

    def attach_late_evidence(self, case: Case, obs_id: str) -> None:
        """登记一条迟到/补报证据。

        与 basis_obs 正交：补报报文本身可能也触发了风险结论而进入
        basis，但仍需在迟到证据清单中留痕，供值班员识别后到数据。
        """
        if obs_id not in case.late_evidence:
            case.late_evidence.append(obs_id)

    def quarantine(self, case: Case, item: QuarantineItem) -> None:
        case.quarantined.append(item)

    def _record(self, case: Case, *args) -> None:
        self._event_seq += 1
        event = DomainEvent(
            seq=self._event_seq,
            event_id=f"EVT-{self._event_seq:06d}",
            case_id=case.case_id,
            type=args[0],
            ts=args[1],
            arrival_ts=args[2],
            actor=args[3],
            payload=dict(args[4]),
        )
        self._event_ids.add(event.event_id)
        case.events.append(event)
        self._project(case, event)

    def _project(self, case: Case, event: DomainEvent) -> None:
        p = event.payload
        if event.type == "RiskDetected":
            pass
        elif event.type == "RiskConfirmed":
            case.status = CONFIRMED
        elif event.type == "AlertPushed":
            case.status = PUSHED
            case.notices.append({
                "kind": "alert", "ts": event.ts, "actor": event.actor,
                "channel": p.get("channel", "指挥平台预警"),
                "summary": p.get("summary", ""),
            })
        elif event.type == "VesselCalled":
            case.status = CALLED
            case.call = {
                "ts": event.ts, "actor": event.actor,
                "instruction": p.get("instruction", ""),
                "ack": p.get("ack"),
            }
        elif event.type == "PatrolDispatched":
            case.status = DISPATCHED
            case.dispatch = {
                "ts": event.ts,
                "boat_id": p.get("boat_id"),
                "boat_name": p.get("boat_name"),
                "officer_ids": list(p.get("officer_ids", [])),
                "anchorage_id": p.get("anchorage_id"),
                "berth_id": p.get("berth_id"),
                "requested_berth_id": p.get("requested_berth_id"),
                "berth_substituted": p.get("berth_substituted", False),
                "cross_segment": p.get("cross_segment", False),
                "task": p.get("task", ""),
            }
        elif event.type == "BoardingCompleted":
            case.status = BOARDED
            case.boarding = {
                "ts": event.ts, "arrival_ts": event.arrival_ts,
                "actor": event.actor,
                "result": p.get("result"),
                "findings": p.get("findings", ""),
                "corrective": p.get("corrective", ""),
                "offline_queued": p.get("offline_queued", False),
            }
        elif event.type == "RecheckCompleted":
            case.status = BOARDED  # 复查不是独立状态，通过与否决定可否关闭
            case.recheck_passed = bool(p.get("compliant"))
            case.recheck = {
                "ts": event.ts, "actor": event.actor,
                "compliant": case.recheck_passed, "note": p.get("note", ""),
            }
        elif event.type == "CaseClosed":
            case.status = CLOSED
            case.closed_ts = event.ts
        elif event.type == "RiskRevoked":
            case.status = REVOKED
            case.closed_ts = event.ts
            case.revocation = {
                "ts": event.ts, "actor": event.actor, "reason": p.get("reason", "")
            }
            case.notices.append({
                "kind": "cancellation", "ts": event.ts, "actor": event.actor,
                "reason": p.get("reason", ""),
            })
