"""观测证据与风险规则。

智慧感知侧的雷达航迹、AIS 报文、视频目标与水文气象读数统一进入证据池。
风险结论只由规则产生，且每条结论都回指作为依据的原始观测，
保证现场结果能够反查到最初的雷达、视频或水文依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .identity import Alias
from .model import World

# 迟到阈值：到达时间晚于观测时间 60 秒即视为补报
LATE_THRESHOLD = timedelta(seconds=60)
# 环境读数与船舶目标相互关联的有效窗口
ENV_WINDOW = timedelta(minutes=10)
# 能见度雾航阈值（米）
FOG_VISIBILITY_M = 1000
# 水位陡涨阈值（米/小时）
RAPID_RISE_MH = 0.5

VESSEL_KINDS = {"track", "ais_target", "video_target"}


@dataclass(frozen=True)
class Observation:
    obs_id: str
    device_id: str
    obs_ts: datetime
    arrival_ts: datetime
    segment_id: str
    kind: str
    kp: float | None
    sog_kn: float | None
    values: dict[str, object]
    aliases: tuple[Alias, ...]
    cluster_id: str | None = None
    backfill: bool = False

    @property
    def is_late(self) -> bool:
        return self.arrival_ts - self.obs_ts > LATE_THRESHOLD or self.backfill

    @property
    def is_vessel(self) -> bool:
        return self.kind in VESSEL_KINDS

    @property
    def underway(self) -> bool:
        return self.sog_kn is not None and self.sog_kn >= 0.5


@dataclass(frozen=True)
class RiskFinding:
    rule_id: str
    title: str
    severity: str  # high | medium | low
    cluster_id: str
    at: datetime
    basis_obs: tuple[str, ...]
    detail: str


@dataclass
class EvidencePool:
    world: World
    resolver: Callable[[str], str] | None = None
    _obs: list[Observation] = field(default_factory=list)

    def _canonical(self, cluster_id: str) -> str:
        return self.resolver(cluster_id) if self.resolver else cluster_id

    def add(self, obs: Observation) -> None:
        self._obs.append(obs)

    def all(self) -> list[Observation]:
        return list(self._obs)

    def by_id(self, obs_id: str) -> Observation:
        for o in self._obs:
            if o.obs_id == obs_id:
                return o
        raise KeyError(obs_id)

    def latest_env(self, segment_id: str, key: str, at: datetime) -> Observation | None:
        candidates = [
            o
            for o in self._obs
            if o.segment_id == segment_id
            and o.kind == "env"
            and key in o.values
            and o.obs_ts <= at
            and at - o.obs_ts <= ENV_WINDOW
        ]
        return candidates[-1] if candidates else None

    def recent_vessels(self, segment_id: str, at: datetime) -> list[Observation]:
        return [
            o
            for o in self._obs
            if o.segment_id == segment_id
            and o.is_vessel
            and o.cluster_id
            and o.obs_ts <= at
            and at - o.obs_ts <= ENV_WINDOW
        ]

    def observations_of_cluster(self, cluster_id: str) -> list[Observation]:
        canonical = self._canonical(cluster_id)
        return [
            o
            for o in self._obs
            if o.cluster_id and self._canonical(o.cluster_id) == canonical
        ]


class RiskEngine:
    """对新到达的观测求值，输出零至多条风险结论。"""

    def __init__(self, world: World, pool: EvidencePool) -> None:
        self.world = world
        self.pool = pool

    def evaluate(self, obs: Observation) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        if obs.is_vessel:
            findings.extend(self._vessel_rules(obs))
            findings.extend(self._env_rules_for_vessel(obs))
        elif obs.kind in ("env", "video_check"):
            findings.extend(self._vessels_under_env(obs))
        return findings

    # ---- 单目标规则 -------------------------------------------------

    def _vessel_rules(self, obs: Observation) -> list[RiskFinding]:
        out: list[RiskFinding] = []
        if not obs.cluster_id:
            return out
        segment = self.world.segments.get(obs.segment_id)

        if obs.kind == "track" and obs.sog_kn is not None and segment:
            if obs.sog_kn > segment.speed_limit_kn:
                out.append(
                    RiskFinding(
                        rule_id="OVERSPEED",
                        title="超过航段限速",
                        severity="high",
                        cluster_id=obs.cluster_id,
                        at=obs.obs_ts,
                        basis_obs=(obs.obs_id,),
                        detail=f"实测{obs.sog_kn:.1f}节，限速{segment.speed_limit_kn:.0f}节",
                    )
                )
            if not self._has_ais(obs.cluster_id):
                out.append(
                    RiskFinding(
                        rule_id="UNIDENTIFIED_TARGET",
                        title="雷达目标无AIS同源信号",
                        severity="medium",
                        cluster_id=obs.cluster_id,
                        at=obs.obs_ts,
                        basis_obs=(obs.obs_id,),
                        detail="雷达航迹暂未关联到AIS或船牌，目标身份待明",
                    )
                )

        if obs.kp is not None and segment:
            zone = segment.zone_at(obs.kp)
            if zone and zone.restricted and obs.underway:
                out.append(
                    RiskFinding(
                        rule_id="ZONE_INTRUSION",
                        title="闯入施工限制区",
                        severity="high",
                        cluster_id=obs.cluster_id,
                        at=obs.obs_ts,
                        basis_obs=(obs.obs_id,),
                        detail=f"目标位于{zone.name}（{zone.from_kp}~{zone.to_kp}公里桩）",
                    )
                )
        return out

    def _env_rules_for_vessel(self, obs: Observation) -> list[RiskFinding]:
        out: list[RiskFinding] = []
        vis = self.pool.latest_env(obs.segment_id, "visibility_m", obs.obs_ts)
        if vis and obs.underway and float(vis.values["visibility_m"]) < FOG_VISIBILITY_M:
            out.append(
                RiskFinding(
                    rule_id="FOG_NAV",
                    title="低能见度雾航",
                    severity="high",
                    cluster_id=obs.cluster_id,
                    at=obs.obs_ts,
                    basis_obs=(obs.obs_id, vis.obs_id),
                    detail=f"能见度{float(vis.values['visibility_m']):.0f}米仍在航行",
                )
            )
        depth = self.pool.latest_env(obs.segment_id, "water_depth_m", obs.obs_ts)
        if depth:
            finding = self._low_ukc(obs.cluster_id, obs, depth)
            if finding:
                out.append(finding)
        return out

    # ---- 环境到达后反查窗口内目标 -----------------------------------

    def _vessels_under_env(self, env: Observation) -> list[RiskFinding]:
        out: list[RiskFinding] = []
        if env.kind == "video_check" and env.values.get("target_found") is False:
            return out
        seen: set[str] = set()
        for v in self.pool.recent_vessels(env.segment_id, env.obs_ts):
            canonical = self.pool._canonical(v.cluster_id) if v.cluster_id else None
            if canonical in seen or not v.underway:
                continue
            if "visibility_m" in env.values and float(env.values["visibility_m"]) < FOG_VISIBILITY_M:
                seen.add(canonical)
                out.append(
                    RiskFinding(
                        rule_id="FOG_NAV",
                        title="低能见度雾航",
                        severity="high",
                        cluster_id=v.cluster_id,
                        at=env.obs_ts,
                        basis_obs=(v.obs_id, env.obs_id),
                        detail=f"能见度{float(env.values['visibility_m']):.0f}米，目标仍在航行",
                    )
                )
            if "water_depth_m" in env.values:
                finding = self._low_ukc(v.cluster_id, v, env)
                if finding and canonical not in seen:
                    seen.add(canonical)
                    out.append(finding)
        return out

    def _low_ukc(
        self, cluster_id: str, vessel_obs: Observation, depth_obs: Observation
    ) -> RiskFinding | None:
        vessel = self._vessel_for(cluster_id)
        if vessel is None:
            return None
        depth = float(depth_obs.values["water_depth_m"])
        ukc = depth - vessel.draft_m
        if ukc >= vessel.min_ukc_m:
            return None
        rise = depth_obs.values.get("level_rise_mh")
        rise_text = f"，水位涨速{float(rise):.1f}米/小时" if rise is not None else ""
        return RiskFinding(
            rule_id="LOW_UKC",
            title="水位陡涨、富余水深不足",
            severity="high",
            cluster_id=cluster_id,
            at=depth_obs.obs_ts,
            basis_obs=(vessel_obs.obs_id, depth_obs.obs_id),
            detail=f"水深{depth:.2f}米、吃水{vessel.draft_m:.1f}米，富余水深{ukc:.2f}米"
            f"（要求≥{vessel.min_ukc_m:.1f}米）{rise_text}",
        )

    # ---- 辅助 -------------------------------------------------------

    def _has_ais(self, cluster_id: str) -> bool:
        for o in self.pool.observations_of_cluster(cluster_id):
            if any(a.system == "ais" for a in o.aliases):
                return True
        return False

    def _vessel_for(self, cluster_id: str):
        for o in self.pool.observations_of_cluster(cluster_id):
            for alias in o.aliases:
                if alias.system == "ais":
                    for v in self.world.vessels.values():
                        if v.mmsi == alias.value:
                            return v
                if alias.system == "video":
                    for v in self.world.vessels.values():
                        if v.plate_no == alias.value:
                            return v
        return None
