"""双线巡航处置闭环服务。

一条风险在本服务中的完整生命：
智慧感知 → 电子巡查组发现(DETECTED) → 远程核验确认 → 预警推送 →
船舶叫应 → 巡查艇任务(人员/艇/泊位协调) → 登临检查 → 复查 → 闭环；
误报在派单前可撤销（已推送则同步发解除通知）。

三条铁律：
1. 多源身份归并全部经 IdentityLedger 留痕，结论可逐条解释；
2. 案件状态只能由人工命令推进，自动数据只附证据；
3. 已人工确认/终态的案件，迟到或补报数据只作附挂/隔离，绝不倒退状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .case import (
    Case,
    CaseStore,
    QuarantineItem,
)
from .evidence import EvidencePool, LATE_THRESHOLD, Observation, RiskEngine, RiskFinding
from .gateway import ObservationGateway
from .identity import IdentityLedger
from .model import World
from .resources import Scheduler


class RuleViolation(Exception):
    """业务规则不满足，例如违规案件未经复查合格不得闭环。"""


@dataclass
class IngestOutcome:
    obs_id: str | None
    cluster_id: str | None
    findings: list[RiskFinding] = field(default_factory=list)
    case_id: str | None = None
    case_created: bool = False


class ClosureService:
    def __init__(self, world: World) -> None:
        self.world = world
        self.ledger = IdentityLedger(world)
        self.pool = EvidencePool(world, resolver=self.ledger.canonical)
        self.engine = RiskEngine(world, self.pool)
        self.store = CaseStore()
        self.scheduler = Scheduler(world)
        self.gateway = ObservationGateway(world)

    # ------------------------------------------------------------------
    # 感知接入
    # ------------------------------------------------------------------

    def ingest(self, raw: dict[str, Any], arrival: datetime) -> IngestOutcome:
        raw_obs = self.gateway.ingest_observation(raw, arrival)
        if raw_obs is None:
            return IngestOutcome(obs_id=None, cluster_id=None)

        is_late = (
            raw_obs.arrival_ts - raw_obs.obs_ts > LATE_THRESHOLD
            or raw_obs.backfill
        )
        cluster_id = self.ledger.observe(
            raw_obs.obs_id,
            raw_obs.segment_id,
            raw_obs.kp,
            raw_obs.obs_ts,
            list(raw_obs.aliases),
            backfill=is_late,
        )
        obs = Observation(
            obs_id=raw_obs.obs_id,
            device_id=raw_obs.device_id,
            obs_ts=raw_obs.obs_ts,
            arrival_ts=raw_obs.arrival_ts,
            segment_id=raw_obs.segment_id,
            kind=raw_obs.kind,
            kp=raw_obs.kp,
            sog_kn=raw_obs.sog_kn,
            values=raw_obs.values,
            aliases=raw_obs.aliases,
            cluster_id=cluster_id,
            backfill=raw_obs.backfill,
        )
        self.pool.add(obs)

        outcome = IngestOutcome(obs_id=obs.obs_id, cluster_id=cluster_id)
        findings = self.engine.evaluate(obs)
        for finding in findings:
            outcome.findings.append(finding)
            self._handle_finding(obs, finding, outcome)
        outcome.case_id = self._attach_plain_observation(obs, outcome.case_id)
        return outcome

    def _attach_supporting(self, obs: Observation, case: Case) -> None:
        if obs.obs_id not in case.supporting_obs and obs.obs_id not in case.basis_obs:
            case.supporting_obs.append(obs.obs_id)

    def _open_case_in_segment(self, segment_id: str) -> Case | None:
        for case in self.store.open_cases():
            if case.segment_id == segment_id:
                return case
        return None

    def _find_case(
        self, cluster_id: str, open_only: bool = False
    ) -> Case | None:
        """按规范簇反查案件（簇可能在案件创建后被并入更大的簇）。"""
        canonical = self.ledger.canonical(cluster_id)
        for case in self.store.all():
            if self.ledger.canonical(case.cluster_id) != canonical:
                continue
            if open_only and case.is_terminal:
                continue
            return case
        return None

    def _handle_finding(
        self, obs: Observation, finding: RiskFinding, outcome: IngestOutcome
    ) -> None:
        open_case = self._find_case(finding.cluster_id, open_only=True)
        if open_case:
            outcome.case_id = open_case.case_id
            self._append_basis(open_case, finding)
            if obs.is_late:
                self.store.attach_late_evidence(open_case, obs.obs_id)
            return

        terminal_case = self._find_case(finding.cluster_id)
        if terminal_case is not None and terminal_case.is_terminal:
            # 终态后到达的反证/新风险：隔离留痕，不得翻案。
            # 以观测为单位隔离（一条补报可能命中多条规则，只建一个复核条目）。
            if not any(q.obs_id == obs.obs_id for q in terminal_case.quarantined):
                self.store.quarantine(
                    terminal_case,
                    QuarantineItem(
                        obs_id=obs.obs_id,
                        reason="terminal-late-finding",
                        detail=(
                            f"案件已{terminal_case.status}后到达规则[{finding.rule_id}]"
                            f"（观测于{obs.obs_ts:%H:%M:%S}，{obs.arrival_ts:%H:%M:%S}到达），"
                            "状态不倒退，转人工复核"
                        ),
                        at=obs.arrival_ts,
                    ),
                )
            if obs.is_late:
                self.store.attach_late_evidence(terminal_case, obs.obs_id)
            outcome.case_id = terminal_case.case_id
            return

        case = self.store.create(
            finding.cluster_id, obs.segment_id, finding.at, obs.arrival_ts
        )
        self._append_basis(case, finding)
        if obs.is_late:
            self.store.attach_late_evidence(case, obs.obs_id)
        outcome.case_id = case.case_id
        outcome.case_created = True

    def _attach_plain_observation(
        self, obs: Observation, case_id: str | None
    ) -> str | None:
        """把不产生新风险的观测挂到相关案件上留痕。

        - 视频复核等无身份标识观测：作为撤销/佐证挂到同航段在办案件；
        - 迟到的身份观测：附为迟到证据（不改状态）。
        """
        if obs.kind == "video_check":
            case = self._open_case_in_segment(obs.segment_id)
            if case is not None:
                self._attach_supporting(obs, case)
                return case.case_id
        if obs.cluster_id and obs.is_late:
            case = self._find_case(obs.cluster_id)
            if case is not None:
                self.store.attach_late_evidence(case, obs.obs_id)
                return case.case_id
        return case_id

    def _append_basis(self, case: Case, finding: RiskFinding) -> None:
        if finding.rule_id not in case.rule_ids:
            case.rule_ids.append(finding.rule_id)
        for obs_id in finding.basis_obs:
            if obs_id not in case.basis_obs:
                case.basis_obs.append(obs_id)

    # ------------------------------------------------------------------
    # 人工处置命令
    # ------------------------------------------------------------------

    def confirm(
        self, case: Case, at: datetime, actor: str, note: str = ""
    ) -> Case:
        # 确认瞬间快照该身份簇已汇聚的全部观测（含视频等非触发证据）
        for obs_id in self.ledger.observations_of(case.cluster_id):
            if obs_id not in case.basis_obs:
                case.basis_obs.append(obs_id)
        self.store.apply(
            case, "RiskConfirmed", at, at, actor, {"note": note}
        )
        return case

    def push(self, case: Case, at: datetime, actor: str | None = None) -> Case:
        vessel = self._vessel_name(case)
        rules = "、".join(case.rule_ids)
        self.store.apply(
            case,
            "AlertPushed",
            at,
            at,
            actor,
            {"summary": f"{vessel or '不明目标'} 触发{rules}", "channel": "指挥平台预警+短信"},
        )
        return case

    def call(
        self,
        case: Case,
        at: datetime,
        instruction: str,
        actor: str | None = None,
        ack: str | None = None,
    ) -> Case:
        self.store.apply(
            case,
            "VesselCalled",
            at,
            at,
            actor,
            {"instruction": instruction, "ack": ack},
        )
        return case

    def dispatch(
        self, case: Case, at: datetime, requested_berth_id: str
    ) -> Case:
        vessel_id = self.ledger.vessel_of(case.cluster_id)
        assignment = self.scheduler.assign(
            case.case_id,
            case.segment_id,
            requested_berth_id,
            at,
            vessel_id=vessel_id,
        )
        payload = assignment.to_payload()
        payload["task"] = (
            f"登临核验{self._vessel_name(case) or '不明目标'}："
            f"{'、'.join(case.rule_ids)}"
        )
        self.store.apply(case, "PatrolDispatched", at, at, None, payload)
        return case

    def board(
        self,
        case: Case,
        ts: datetime,
        actor: str,
        result: str,
        findings: str,
        corrective: str = "",
        arrival: datetime | None = None,
        client_id: str | None = None,
        offline_queued: bool = False,
    ) -> Case:
        if result not in ("violation", "compliant"):
            raise RuleViolation("登临结论必须是 violation 或 compliant")
        self.store.apply(
            case,
            "BoardingCompleted",
            ts,
            arrival or ts,
            actor,
            {
                "result": result,
                "findings": findings,
                "corrective": corrective,
                "offline_queued": offline_queued,
            },
            client_id=client_id,
        )
        # 登临完成，现场编组释放；涉案泊位保留到关闭
        self.scheduler.release_field_crew(case.case_id)
        return case

    def recheck(
        self,
        case: Case,
        ts: datetime,
        actor: str,
        compliant: bool,
        note: str = "",
    ) -> Case:
        self.store.apply(
            case,
            "RecheckCompleted",
            ts,
            ts,
            actor,
            {"compliant": compliant, "note": note},
        )
        return case

    def close(self, case: Case, at: datetime, actor: str | None = None) -> Case:
        if case.boarding and case.boarding["result"] == "violation":
            if not case.recheck_passed:
                raise RuleViolation("违规案件须复查合格后方可闭环")
        self.store.apply(case, "CaseClosed", at, at, actor, {})
        self.scheduler.release_berth(case.case_id)
        return case

    def revoke(
        self, case: Case, at: datetime, actor: str, reason: str
    ) -> Case:
        self.store.apply(case, "RiskRevoked", at, at, actor, {"reason": reason})
        self.scheduler.cancel_dispatch(case.case_id)
        return case

    # ------------------------------------------------------------------
    # 值班视图与溯源
    # ------------------------------------------------------------------

    def open_risk_board(self) -> list[dict[str, Any]]:
        """值班员视角：所有未闭环风险，按状态与时间排列。"""
        board = []
        for case in self.store.open_cases():
            board.append(
                {
                    "case_id": case.case_id,
                    "status": case.status,
                    "segment": self.world.segments[case.segment_id].name,
                    "vessel": self._vessel_name(case),
                    "rules": list(case.rule_ids),
                    "created_ts": case.created_ts,
                    "boat": (case.dispatch or {}).get("boat_name"),
                    "berth": (case.dispatch or {}).get("berth_id"),
                    "boarding_result": (case.boarding or {}).get("result"),
                    "awaiting": self._awaiting(case),
                    "late_evidence": len(case.late_evidence),
                    "quarantine": len(case.quarantined),
                }
            )
        board.sort(key=lambda r: (r["status"], r["created_ts"] or datetime.min))
        return board

    def review_queue(self) -> list[dict[str, Any]]:
        """值班员视角：终态后到达、被屏障隔离待人工复核的数据。

        迟到数据不翻案，但必须可见——否则撤销/闭环后补报的反证会丢失。
        """
        queue = []
        for case in self.store.all():
            for item in case.quarantined:
                queue.append(
                    {
                        "case_id": case.case_id,
                        "case_status": case.status,
                        "obs_id": item.obs_id,
                        "reason": item.reason,
                        "detail": item.detail,
                        "at": item.at,
                        "vessel": self._vessel_name(case),
                    }
                )
        queue.sort(key=lambda r: r["at"])
        return queue

    def lineage(self, case: Case) -> dict[str, Any]:
        """从现场结果反查最初的雷达、视频、AIS 与水文气象依据。"""
        basis = []
        basis_ids = set(case.basis_obs)
        for obs_id in list(case.basis_obs) + list(case.supporting_obs):
            try:
                obs = self.pool.by_id(obs_id)
            except KeyError:
                continue
            device = self.world.devices.get(obs.device_id)
            basis.append(
                {
                    "obs_id": obs.obs_id,
                    "role": "basis" if obs_id in basis_ids else "supporting",
                    "device_id": obs.device_id,
                    "device_name": device.name if device else obs.device_id,
                    "device_kind": device.kind if device else None,
                    "obs_ts": obs.obs_ts,
                    "arrival_ts": obs.arrival_ts,
                    "late": obs.is_late,
                    "kp": obs.kp,
                    "sog_kn": obs.sog_kn,
                    "values": obs.values,
                    "aliases": [str(a) for a in obs.aliases],
                }
            )
        # 从最初依据开始：先判据后佐证，各自按观测时间排列
        basis.sort(key=lambda b: (b["role"] != "basis", b["obs_ts"]))
        timeline = [
            {
                "type": e.type,
                "ts": e.ts,
                "arrival_ts": e.arrival_ts,
                "actor": e.actor,
                "automatic": e.automatic,
                "payload": e.payload,
            }
            for e in case.events
        ]
        return {
            "case_id": case.case_id,
            "status": case.status,
            "vessel": self._vessel_name(case),
            "risk_rules": list(case.rule_ids),
            "identity_explanations": self.ledger.explain(case.cluster_id),
            "basis": basis,
            "late_evidence": list(case.late_evidence),
            "quarantined": [
                {"obs_id": q.obs_id, "reason": q.reason, "detail": q.detail}
                for q in case.quarantined
            ],
            "timeline": timeline,
        }

    # ------------------------------------------------------------------

    def _awaiting(self, case: Case) -> str:
        mapping = {
            "DETECTED": "等待电子巡查组远程核验确认",
            "CONFIRMED": "等待预警推送",
            "PUSHED": "等待船舶叫应",
            "CALLED": "等待巡查艇派单",
            "DISPATCHED": "等待现场登临检查",
            "BOARDED": "等待复查合格后闭环"
            if (case.boarding or {}).get("result") == "violation"
            else "等待闭环",
        }
        return mapping.get(case.status, "")

    def _vessel_name(self, case: Case) -> str | None:
        vessel_id = self.ledger.vessel_of(case.cluster_id)
        if not vessel_id:
            return None
        return self.world.vessels[vessel_id].name
