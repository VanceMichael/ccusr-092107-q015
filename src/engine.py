"""处置闭环引擎：把七个阶段串成同一条风险时间线。

输入分两类：

* **感知事件**（雷达/视频/AIS/水文/气象/VHF/现场回传）：负责发现与佐证；
* **处置指令**（值班员/巡查员动作）：负责推进阶段。

引擎保证：

1. 同一艘船、同一航段、同一类风险在时间窗内只形成**一条**风险，
   多源事件全部挂为依据；身份事后归并时会把已分开的风险去重合并，
   并撤销重复派艇；
2. 传感器事件带 ``observed_at``（发生时间）与 ``received_at``
   （到达时间）。断网补传时后者显著晚于前者，依据照收并打"迟到"标，
   但**不新建重复风险、不回退阶段、不改写人工结论**；
3. 所有人工动作（核验、叫应、登临、复查、撤销、身份判定）锁定状态；
4. 任意阶段都能沿 ``risk_id`` 反查到最初的雷达点迹、视频片段或
   水文读数及其设备。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .model import (
    DomainError,
    Evidence,
    EvidenceKind,
    HistoryEntry,
    Phase,
    Resolution,
    Risk,
    RiskType,
    can_transit,
    is_backward,
    parse_ts,
    MANUAL_ACTIONS,
    CLOSE_WITHOUT_RECHECK,
)
from .identity import IdentityResolver, SightToken

CORRELATE_WINDOW = timedelta(hours=2)   # 同一风险的归票时间窗
RECHECK_AFTER = timedelta(hours=2)      # 确认问题后的复查间隔（样例）

RISK_TYPE_BY_EVENT = {
    "anchorage": RiskType.ANCHORAGE,
    "nav": RiskType.NAV_RULE,
    "abnormal": RiskType.ABNORMAL,
    "low_ukc": RiskType.LOW_UKC,
}


@dataclass
class Notification:
    """一次对外触达：预警推送 / 叫应 / 撤销通知。"""

    notice_id: str
    risk_id: str
    kind: str               # push / call / revoke
    target: str
    at: datetime
    channel: str = ""
    content: str = ""
    ack: bool | None = None  # 叫应应答；None 表示尚不需要回执
    revoked: bool = False    # 误报撤销后，原预警标记失效


@dataclass
class CommandResult:
    ok: bool
    risk_id: str | None = None
    phase: str | None = None
    message: str = ""
    task_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class ClosureEngine:
    def __init__(self, canal: dict[str, Any]) -> None:
        self.canal = canal
        self.segments = {s["id"]: s for s in canal.get("segments", [])}
        self.devices = {d["id"]: d for d in canal.get("devices", [])}
        self.resolver = IdentityResolver()
        for mmsi, rec in canal.get("registry", {}).items():
            self.resolver.bind_registry(mmsi, name=rec["name"], reg_no=rec.get("reg_no"))

        self.risks: dict[str, Risk] = {}
        self.evidence: dict[str, Evidence] = {}
        self.notifications: list[Notification] = []
        self.notes: list[str] = []
        self.aliases: dict[str, str] = {}   # 剧本 ref -> risk_id
        self.clock: datetime | None = None  # 已摄入数据中的最晚发生时间
        self._seq = 0
        self._ev_seq = 0
        self._notice_seq = 0

        # 延迟注入 Dispatcher（避免与调度模块循环依赖）
        from .dispatch import Dispatcher
        self.dispatcher = Dispatcher(canal, self)

    # ------------------------------------------------------------------ #
    # 事件摄入
    # ------------------------------------------------------------------ #

    def ingest(self, event: dict[str, Any]) -> CommandResult:
        """感知事件统一入口。``at`` 为发生时间，``recv`` 为到达时间。"""
        etype = event.get("etype", "detection")
        if etype == "detection":
            return self._ingest_detection(event)
        if etype == "sight":
            return self._ingest_sight(event)
        if etype == "water_level":
            return self._ingest_water_level(event)
        if etype == "weather":
            return self._ingest_weather(event)
        if etype == "onsite":
            return self._ingest_onsite(event)
        raise DomainError(f"未知事件类型：{etype}")

    def _make_evidence(self, event: dict[str, Any],
                       kind: EvidenceKind | None = None) -> Evidence:
        self._ev_seq += 1
        observed = parse_ts(event["at"])
        received = parse_ts(event.get("recv", event["at"]))
        kind_map = {
            "radar": EvidenceKind.RADAR, "video": EvidenceKind.VIDEO,
            "ais": EvidenceKind.AIS, "vhf": EvidenceKind.VHF,
            "hydro": EvidenceKind.HYDRO, "weather": EvidenceKind.WEATHER,
            "onsite": EvidenceKind.ONSITE, "manual": EvidenceKind.MANUAL,
        }
        return Evidence(
            evidence_id=event.get("eid") or f"EV-{self._ev_seq:04d}",
            kind=kind or kind_map[event["source"]],
            device_id=event.get("device_id"),
            observed_at=observed,
            received_at=received,
            data=event.get("data", {}),
            delayed=received - observed > timedelta(minutes=5),
            batch_id=event.get("batch"),
        )

    def _sight_from_event(self, event: dict[str, Any]) -> SightToken:
        d = event.get("data", {})
        token = d.get("token") or f"{event['source'].upper()}:{event.get('device_id')}@{event['at']}"
        return SightToken(
            token=token,
            system=event["source"],
            observed_at=event["at"],
            segment_id=event["segment_id"],
            chainage_km=float(event["chainage_km"]),
            name_hint=d.get("name_hint"),
            mmsi_hint=d.get("mmsi"),
        )

    def _ingest_detection(self, event: dict[str, Any]) -> CommandResult:
        ev = self._make_evidence(event)
        self.evidence[ev.evidence_id] = ev
        sight = self._sight_from_event(event)

        roots_before = {t: self.resolver._find(t) for t in self.resolver._parent}
        self.resolver.observe([sight])
        self._consolidate_after_merge(roots_before)

        cluster = self.resolver.cluster_key(sight.token)
        rtype = RISK_TYPE_BY_EVENT[event["event_type"]]
        pending = self.resolver.has_pending(sight.token)
        risk, created = self._correlate_or_create(
            cluster, event["segment_id"], rtype, ev, pending, event
        )
        self._track_clock(ev.observed_at)
        if created:
            self._attach_prior_sight_evidence(risk, ev.observed_at)
        if ev.evidence_id not in risk.evidence_ids:
            risk.evidence_ids.append(ev.evidence_id)
        risk.last_seen = max(risk.last_seen, ev.observed_at)
        # 新证据可能改变任意在办风险的歧义状态，统一刷新
        for open_risk in self.risks.values():
            if not open_risk.merged_into:
                open_risk.identity_pending = self.resolver.has_pending(
                    open_risk.cluster_key)
        if event.get("ref"):
            self.aliases.setdefault(event["ref"], risk.risk_id)

        if not created and ev.delayed:
            risk.notes.append(
                f"{ev.observed_at:%H:%M} 的{ev.kind.value}补报于"
                f" {ev.received_at:%H:%M} 到达，仅追加依据，状态不变更"
            )
        tag = "新建风险" if created else ("迟到补报" if ev.delayed else "归并到在办风险")
        return CommandResult(True, risk.risk_id, risk.phase.value, tag)

    def _ingest_sight(self, event: dict[str, Any]) -> CommandResult:
        """纯航迹/身份观测（如 AIS 点迹）：归并身份、留存依据，不立案。"""
        ev = self._make_evidence(event)
        self.evidence[ev.evidence_id] = ev
        sight = self._sight_from_event(event)
        roots_before = {t: self.resolver._find(t) for t in self.resolver._parent}
        self.resolver.observe([sight])
        self._consolidate_after_merge(roots_before)
        self._track_clock(ev.observed_at)
        cluster = self.resolver.cluster_key(sight.token)
        # 航迹作为同船在办风险的补充依据（不挂已终态风险，避免事后扰动）
        attached = 0
        for risk in self.risks.values():
            if (not risk.merged_into and not risk.is_terminal
                    and risk.cluster_key == cluster
                    and ev.evidence_id not in risk.evidence_ids):
                risk.evidence_ids.append(ev.evidence_id)
                attached += 1
        return CommandResult(True, None, None,
                             f"航迹已记录，身份簇={cluster}"
                             + (f"，佐证{attached}条在办风险" if attached else ""))

    def _ingest_water_level(self, event: dict[str, Any]) -> CommandResult:
        ev = self._make_evidence(event, EvidenceKind.HYDRO)
        self.evidence[ev.evidence_id] = ev
        self._track_clock(ev.observed_at)
        threshold = float(event["data"]["threshold_m"])
        value = float(event["data"]["level_m"])
        segment = event["segment_id"]
        if value >= threshold:
            self.notes.append(f"{segment} 水位{value}m 高于阈值，不构成低水深风险")
            return CommandResult(True, None, None, "水位正常，忽略")

        # 低水位：对该航段内在航/锚泊船舶逐一形成低水深风险
        targets = self._vessels_in_segment(segment)
        if not targets:
            self.notes.append(f"{segment} 水位{value}m 低于{threshold}m，当前无船舶，记录环境预警")
            return CommandResult(True, None, None, "环境预警已记录")
        touched: list[str] = []
        for cluster in targets:
            risk, created = self._correlate_or_create(
                cluster, segment, RiskType.LOW_UKC, ev, False, event,
                title=f"富余水深不足：{cluster} @ {segment}",
            )
            if created:
                self._attach_prior_sight_evidence(risk, ev.observed_at)
            risk.detail["level_m"] = value
            risk.detail["threshold_m"] = threshold
            if ev.evidence_id not in risk.evidence_ids:
                risk.evidence_ids.append(ev.evidence_id)
            touched.append(risk.risk_id)
        if event.get("ref"):
            self.aliases[event["ref"]] = touched[0]
        return CommandResult(True, touched[0] if len(touched) == 1 else None,
                             None, f"低水位关联{len(touched)}艘船舶")

    def _ingest_weather(self, event: dict[str, Any]) -> CommandResult:
        ev = self._make_evidence(event, EvidenceKind.WEATHER)
        self.evidence[ev.evidence_id] = ev
        self._track_clock(ev.observed_at)
        cluster = f"ENV:{event['segment_id']}"
        risk, created = self._correlate_or_create(
            cluster, event["segment_id"], RiskType.NAV_RULE, ev, False, event,
            title=f"气象航行风险 @ {event['segment_id']}",
        )
        risk.detail.update(event["data"])
        if ev.evidence_id not in risk.evidence_ids:
            risk.evidence_ids.append(ev.evidence_id)
        if event.get("ref"):
            self.aliases.setdefault(event["ref"], risk.risk_id)
        return CommandResult(True, risk.risk_id, risk.phase.value,
                             "新建气象风险" if created else "气象数据已归并")

    def _ingest_onsite(self, event: dict[str, Any]) -> CommandResult:
        """巡查员现场回传（照片/笔录）。断网时可随补传批次迟到。"""
        ev = self._make_evidence(event, EvidenceKind.ONSITE)
        self.evidence[ev.evidence_id] = ev
        risk = self.risks[event["risk_id"]]
        risk.evidence_ids.append(ev.evidence_id)
        if ev.delayed:
            risk.notes.append("现场回传断网补传，已归入依据链")
        return CommandResult(True, risk.risk_id, risk.phase.value, "现场回传已归档")

    # ------------------------------------------------------------------ #
    # 关联与去重
    # ------------------------------------------------------------------ #

    def _correlate_or_create(
        self, cluster: str, segment_id: str, rtype: RiskType,
        ev: Evidence, pending: bool, event: dict[str, Any], title: str = "",
    ) -> tuple[Risk, bool]:
        open_match: Risk | None = None
        span_match: Risk | None = None
        for risk in self.risks.values():
            if risk.merged_into or risk.cluster_key != cluster:
                continue
            if risk.segment_id != segment_id or risk.risk_type != rtype:
                continue
            age = abs((ev.observed_at - risk.first_seen).total_seconds())
            if age <= CORRELATE_WINDOW.total_seconds() and not risk.is_terminal:
                if open_match is None:
                    open_match = risk
            # 迟到补报：观测时刻落在该风险存续区间内（首见到末次处置后留一个窗口）
            if ev.delayed and risk.is_terminal:
                span_end = risk.last_seen
                if risk.history:
                    span_end = max(span_end, risk.history[-1].occurred_at)
                span_end += CORRELATE_WINDOW
                span_start = risk.first_seen - CORRELATE_WINDOW
                if span_start <= ev.observed_at <= span_end:
                    span_match = risk

        if open_match is not None:
            return open_match, False

        if span_match is not None:
            # 迟到数据只追加，绝不在闭环/撤销后重新立案，也不能复活风险
            span_match.evidence_ids.append(ev.evidence_id)
            span_match.notes.append(
                f"迟到数据（{ev.to_line()}）属于已{span_match.phase.value}风险的存续时段，"
                "仅补入依据链，状态不变更"
            )
            return span_match, False

        self._seq += 1
        rid = f"RISK-{self._seq:04d}"
        risk = Risk(
            risk_id=rid,
            risk_type=rtype,
            cluster_key=cluster,
            segment_id=segment_id,
            chainage_km=float(event["chainage_km"]),
            first_seen=ev.observed_at,
            last_seen=ev.observed_at,
            entered_at=ev.observed_at,
            severity=event.get("severity", "high" if rtype == RiskType.LOW_UKC else "medium"),
            identity_pending=pending,
            title=title or event.get("data", {}).get("title", ""),
            detail={},
        )
        risk.evidence_ids.append(ev.evidence_id)
        self.risks[rid] = risk
        return risk, True

    def _vessels_in_segment(self, segment_id: str) -> list[str]:
        seen: dict[str, datetime] = {}
        for tok_list in self.resolver._tokens.values():
            for tok in tok_list:
                if tok.segment_id == segment_id:
                    key = self.resolver.cluster_key(tok.token)
                    t = parse_ts(tok.observed_at)
                    seen[key] = max(seen.get(key, t), t)
        # 以数据时钟为准，取近 CORRELATE_WINDOW 内出现过的船舶；无时钟时全取
        if self.clock is None:
            return list(seen)
        cutoff = self.clock - CORRELATE_WINDOW
        recent = [k for k, t in seen.items() if t >= cutoff]
        return recent or list(seen)

    def _track_clock(self, when: datetime) -> None:
        self.clock = when if self.clock is None else max(self.clock, when)

    def _attach_prior_sight_evidence(self, risk: Risk, when: datetime) -> None:
        """立案时把时间窗内同船的航迹/AIS 观测补挂为依据。"""
        for tok_list in self.resolver._tokens.values():
            for tok in tok_list:
                if tok.segment_id != risk.segment_id:
                    continue
                if self.resolver.cluster_key(tok.token) != risk.cluster_key:
                    continue
                dt = abs((parse_ts(tok.observed_at) - when).total_seconds())
                if dt > CORRELATE_WINDOW.total_seconds():
                    continue
                # 该 token 对应的已存档依据（按设备与 token 反查）
                for ev_id, ev in self.evidence.items():
                    if ev_id in risk.evidence_ids:
                        continue
                    if ev.kind == EvidenceKind.AIS and ev.data.get("token") == tok.token:
                        risk.evidence_ids.append(ev_id)

    def _consolidate_after_merge(self, roots_before: dict[str, str]) -> None:
        """身份归并把两簇连通后：在办同票风险合并，其余风险更名/交叉关联。"""
        merged_tokens = {t for t, old in roots_before.items()
                         if self.resolver._find(t) != old}
        if not merged_tokens:
            return
        any_token = next(iter(merged_tokens))
        cluster = self.resolver.cluster_key(any_token)
        members = self.resolver._members(self.resolver._find(any_token))

        affected = [r for r in self.risks.values()
                    if not r.merged_into and r.cluster_key in members]

        # 已终态的风险不回退、不合并，只与同船的在办风险互相留痕
        for term in (r for r in affected if r.is_terminal):
            peers = [r for r in affected if not r.is_terminal]
            for peer in peers:
                if term.risk_id not in peer.related_risk_ids:
                    peer.related_risk_ids.append(term.risk_id)
                if peer.risk_id not in term.related_risk_ids:
                    term.related_risk_ids.append(peer.risk_id)
            term.cluster_key = cluster

        # 单条在办风险：仅更新簇键（船名事后确认）
        open_risks = [r for r in affected if not r.is_terminal]
        for r in open_risks:
            r.cluster_key = cluster
        by_type: dict[tuple[Any, str], list[Risk]] = {}
        for r in open_risks:
            by_type.setdefault((r.risk_type, r.segment_id), []).append(r)
        for group in by_type.values():
            if len(group) < 2:
                continue
            keep = min(group, key=lambda r: r.first_seen)
            for dup in group:
                if dup is keep:
                    continue
                dup.merged_into = keep.risk_id
                keep.related_risk_ids.append(dup.risk_id)
                keep.evidence_ids = list(dict.fromkeys(keep.evidence_ids + dup.evidence_ids))
                keep.last_seen = max(keep.last_seen, dup.last_seen)
                keep.notes.append(
                    f"风险 {dup.risk_id} 经身份归并并入本单"
                    f"（簇键 {dup.cluster_key} → {cluster}）"
                )
                if dup.board_task_id:
                    self.dispatcher.cancel_task(
                        dup.board_task_id,
                        reason=f"身份归并去重，并入 {keep.risk_id}",
                    )
                dup.board_task_id = dup.recheck_task_id = None

    # ------------------------------------------------------------------ #
    # 处置指令（状态推进）
    # ------------------------------------------------------------------ #

    def _transition(self, risk: Risk, target: Phase, action: str, actor: str,
                    at: datetime, reason: str = "", evidence: list[str] | None = None,
                    manual: bool = True, delayed: bool = False) -> None:
        if risk.is_terminal:
            raise DomainError(f"风险 {risk.risk_id} 已终态（{risk.phase.value}），不可变更")
        if is_backward(risk.phase, target):
            raise DomainError("迟到/补报数据不得使处置阶段倒退："
                              f"{risk.phase.value} → {target.value}")
        if not can_transit(risk.phase, target):
            raise DomainError(f"非法阶段迁移：{risk.phase.value} → {target.value}")

        risk.history.append(HistoryEntry(
            seq=len(risk.history) + 1,
            phase_after=target,
            action=action,
            actor=actor,
            occurred_at=at,
            received_at=parse_ts(datetime.now(tz=timezone.utc)),
            reason=reason,
            evidence_ids=evidence or [],
            locked=manual and action in MANUAL_ACTIONS,
            delayed=delayed,
        ))
        risk.phase = target
        risk.entered_at = at
        if manual and action in MANUAL_ACTIONS:
            risk.locked = True
            risk.lock_actor = actor
            risk.lock_action = action

    def remote_verify(self, risk_id: str, actor: str, at: str,
                      verdict: str = "confirmed", reason: str = "") -> CommandResult:
        risk = self.risks[risk_id]
        when = parse_ts(at)
        if verdict == "dismiss":
            # 远程核验即排除：按误报撤销处理（登临前允许）
            return self.revoke(risk_id, actor, at, reason or "远程核验排除")
        self._transition(risk, Phase.REMOTE_VERIFIED, "remote_verify", actor, when,
                         reason or f"电子巡查组核验{verdict}")
        return CommandResult(True, risk_id, risk.phase.value, "远程核验完成")

    def push_warning(self, risk_id: str, actor: str, at: str,
                     target: str = "水上巡查组", channel: str = "指挥平台") -> CommandResult:
        risk = self.risks[risk_id]
        when = parse_ts(at)
        self._transition(risk, Phase.PUSHED, "push", actor, when,
                         f"预警推送至{target}（{channel}）", manual=False)
        self._notice_seq += 1
        self.notifications.append(Notification(
            notice_id=f"N-{self._notice_seq:04d}", risk_id=risk_id, kind="push",
            target=target, at=when, channel=channel,
            content=risk.title or risk.risk_type.value,
        ))
        return CommandResult(True, risk_id, risk.phase.value, f"预警已推送：{target}")

    def call_vessel(self, risk_id: str, actor: str, at: str,
                    answered: bool | None = None, channel: str = "VHF16",
                    self_rectified: bool = False) -> CommandResult:
        risk = self.risks[risk_id]
        when = parse_ts(at)
        self._transition(risk, Phase.CALLED, "vessel_call", actor, when,
                         "船舶叫应" + ("，已应答" if answered else "，无应答，升级派艇"),
                         manual=True)
        self._notice_seq += 1
        self.notifications.append(Notification(
            notice_id=f"N-{self._notice_seq:04d}", risk_id=risk_id, kind="call",
            target=risk.cluster_key, at=when, channel=channel,
            content="指挥中心叫应", ack=answered,
        ))
        if self_rectified:
            # 船舶叫应后当场自改（如起锚离开禁锚区），经电子巡查组视频复核属实
            self._do_close(risk, actor, when, Resolution.RECTIFIED,
                           "叫应后船舶当场改正，远程复核属实，无需登临")
            return CommandResult(True, risk_id, risk.phase.value, "当场自改，闭环")
        return CommandResult(True, risk_id, risk.phase.value,
                             "叫应成功" if answered else "无应答")

    def dispatch_boat(self, risk_id: str, actor: str, at: str, boat_id: str,
                      person_ids: list[str], duration_min: int = 60,
                      override_identity: bool = False,
                      anchorage_id: str | None = None,
                      berth_id: str | None = None) -> CommandResult:
        risk = self.risks[risk_id]
        if risk.identity_pending and not override_identity:
            raise DomainError("船舶身份存在歧义（待人工判定），派艇可能重复登临；"
                              "请先判定身份，或显式 override_identity 并注明原因")
        when = parse_ts(at)
        task = self.dispatcher.assign(
            risk=risk, boat_id=boat_id, person_ids=person_ids,
            start=when, duration_min=duration_min, actor=actor,
            anchorage_id=anchorage_id, berth_id=berth_id,
        )
        # 允许从 PUSHED 直接派艇（叫应无应答的升级路径）
        if risk.phase == Phase.PUSHED:
            self._transition(risk, Phase.CALLED, "vessel_call", actor, when,
                             "叫应无应答，直接升级派艇", manual=True)
        self._transition(risk, Phase.DISPATCHED, "dispatch", actor, when,
                         f"派 {task.boat_id}，艇员{','.join(task.person_ids)}",
                         manual=False)
        risk.board_task_id = task.task_id
        return CommandResult(True, risk_id, risk.phase.value,
                             f"巡查艇任务 {task.task_id} 已下达", task_id=task.task_id)

    def boarding_result(self, risk_id: str, actor: str, at: str,
                        resolution: str, reason: str = "",
                        evidence_events: list[dict[str, Any]] | None = None) -> CommandResult:
        risk = self.risks[risk_id]
        when = parse_ts(at)
        for ev_event in evidence_events or []:
            ev_event.setdefault("etype", "onsite")
            ev_event.setdefault("risk_id", risk_id)
            self.ingest(ev_event)
        res = Resolution(resolution)
        self._transition(risk, Phase.BOARDED, "boarding_result", actor, when,
                         reason or f"登临结论：{res.value}",
                         evidence=[e.get("eid", "") for e in evidence_events or []])
        risk.resolution = res
        if risk.board_task_id:
            self.dispatcher.mark_done(risk.board_task_id)

        if res in CLOSE_WITHOUT_RECHECK or res == Resolution.RECTIFIED:
            self._do_close(risk, actor, when, res,
                           "现场结论明确，无需复查")
            return CommandResult(True, risk_id, risk.phase.value, "登临后闭环")

        # 问题属实：安排复查任务
        task = self.dispatcher.schedule_recheck(risk, when + RECHECK_AFTER, actor)
        risk.recheck_task_id = task.task_id
        return CommandResult(True, risk_id, risk.phase.value,
                             f"问题属实，复查任务 {task.task_id} 已排程",
                             task_id=task.task_id)

    def recheck_result(self, risk_id: str, actor: str, at: str,
                       passed: bool, reason: str = "",
                       evidence_events: list[dict[str, Any]] | None = None) -> CommandResult:
        risk = self.risks[risk_id]
        when = parse_ts(at)
        for ev_event in evidence_events or []:
            ev_event.setdefault("etype", "onsite")
            ev_event.setdefault("risk_id", risk_id)
            self.ingest(ev_event)
        self._transition(risk, Phase.RECHECKED, "recheck_result", actor, when,
                         reason or ("复查通过，隐患已消除" if passed else "复查未通过，问题仍在"),
                         evidence=[e.get("eid", "") for e in evidence_events or []])
        if risk.recheck_task_id:
            self.dispatcher.mark_done(risk.recheck_task_id)
        if passed:
            self._do_close(risk, actor, when, Resolution.RECTIFIED, "复查通过")
            return CommandResult(True, risk_id, risk.phase.value, "复查通过，闭环")
        risk.resolution = Resolution.CONFIRMED
        task = self.dispatcher.schedule_recheck(risk, when + RECHECK_AFTER, actor)
        risk.recheck_task_id = task.task_id
        risk.notes.append("复查未通过，再次排程复查")
        return CommandResult(True, risk_id, risk.phase.value,
                             f"复查未通过，新复查任务 {task.task_id}", task_id=task.task_id)

    def revoke(self, risk_id: str, actor: str, at: str, reason: str) -> CommandResult:
        """误报撤销：仅允许登临前；撤销通知自动下发，撤销后迟到数据不得复活。"""
        risk = self.risks[risk_id]
        when = parse_ts(at)
        self._transition(risk, Phase.REVOKED, "revoke", actor, when, reason)
        risk.resolution = Resolution.FALSE_ALARM
        for task_id in (risk.board_task_id, risk.recheck_task_id):
            if task_id:
                self.dispatcher.cancel_task(task_id, reason=f"风险 {risk_id} 误报撤销")
        risk.board_task_id = risk.recheck_task_id = None
        for n in self.notifications:
            if n.risk_id == risk_id and n.kind == "push" and not n.revoked:
                n.revoked = True
        self._notice_seq += 1
        self.notifications.append(Notification(
            notice_id=f"N-{self._notice_seq:04d}", risk_id=risk_id, kind="revoke",
            target="水上巡查组", at=when, channel="指挥平台",
            content=f"撤销：{reason}",
        ))
        return CommandResult(True, risk_id, risk.phase.value, "误报已撤销并通知")

    def _do_close(self, risk: Risk, actor: str, when: datetime,
                  resolution: Resolution, reason: str) -> None:
        self._transition(risk, Phase.CLOSED, "manual_close", actor, when, reason)
        risk.resolution = resolution

    def decide_identity(self, a: str, b: str, actor: str, at: str,
                        confirm: bool, reason: str = "") -> CommandResult:
        """值班员对歧义身份做人工判定；判定后立即合并/拦截并锁定。"""
        roots_before = {t: self.resolver._find(t) for t in self.resolver._parent}
        if confirm:
            self.resolver.confirm(a, b, actor, at, reason)
            self._consolidate_after_merge(roots_before)
        else:
            self.resolver.deny(a, b, actor, at, reason)
        for risk in self.risks.values():
            root_members = self.resolver._members(self.resolver._find(risk.cluster_key)) \
                if risk.cluster_key in self.resolver._parent else set()
            risk.identity_pending = self.resolver.has_pending(
                next(iter(root_members))) if root_members else False
        return CommandResult(True, None, None,
                             "身份已确认并归并" if confirm else "身份已否认，候选已拦截")

    # ------------------------------------------------------------------ #
    # 值班员视图与反查
    # ------------------------------------------------------------------ #

    def unclosed_risks(self) -> list[Risk]:
        """值班员待办：所有未闭环（含被撤销前的在办）风险，按严重度、时间排序。"""
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            (r for r in self.risks.values() if r.unclosed),
            key=lambda r: (order.get(r.severity, 3), r.first_seen),
        )

    def board(self) -> dict[str, Any]:
        """值班台一屏总览。"""
        unclosed = self.unclosed_risks()
        return {
            "unclosed_count": len(unclosed),
            "risks": [
                {
                    "risk_id": r.risk_id,
                    "phase": r.phase.value,
                    "type": r.risk_type.value,
                    "vessel": r.cluster_key,
                    "segment": r.segment_id,
                    "severity": r.severity,
                    "identity_pending": r.identity_pending,
                    "boat_task": r.board_task_id,
                    "recheck_task": r.recheck_task_id,
                }
                for r in unclosed
            ],
            "identity_pending": self.resolver.pending_items(),
            "tasks": self.dispatcher.task_board(),
            "anchorage": self.dispatcher.anchorage_board(),
        }

    def lineage(self, risk_id: str) -> dict[str, Any]:
        """从现场结论反查最初依据：时间线 + 每条依据的设备与原始报文。"""
        risk = self.risks[risk_id]
        timeline: list[dict[str, Any]] = []
        for ev_id in risk.evidence_ids:
            ev = self.evidence[ev_id]
            device = self.devices.get(ev.device_id or "", {})
            timeline.append({
                "kind": "evidence",
                "evidence_id": ev.evidence_id,
                "source": ev.kind.value,
                "device_id": ev.device_id,
                "device_name": device.get("name"),
                "observed_at": ev.observed_at.isoformat(),
                "received_at": ev.received_at.isoformat(),
                "delayed": ev.delayed,
                "batch": ev.batch_id,
                "raw": ev.data,
            })
        for h in risk.history:
            timeline.append({
                "kind": "action",
                "seq": h.seq,
                "action": h.action,
                "phase": h.phase_after.value,
                "actor": h.actor,
                "at": h.occurred_at.isoformat(),
                "reason": h.reason,
                "locked": h.locked,
                "delayed": h.delayed,
                "evidence_ids": h.evidence_ids,
            })
        timeline.sort(key=lambda x: x.get("observed_at") or x.get("at"))
        return {
            "risk_id": risk_id,
            "vessel_cluster": risk.cluster_key,
            "identity_chain": self.resolver.explain(risk.cluster_key)
            if risk.cluster_key in self.resolver._parent else [],
            "phase_path": [p.value for p in risk.phase_path()],
            "resolution": risk.resolution.value,
            "locked": risk.locked,
            "timeline": timeline,
            "related_risks": risk.related_risk_ids,
            "notes": risk.notes,
        }

    def get(self, risk_id: str) -> Risk:
        return self.risks[risk_id]
