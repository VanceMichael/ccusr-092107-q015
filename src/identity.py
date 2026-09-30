"""多源船舶身份归并。

雷达航迹号、AIS 的 MMSI、视频识别的船牌属于三套系统的异构标识。
归并必须可解释：任意两个标识被并入同一簇，都要留下决策记录
（依据规则、作证观测、时间差与距离），供值班员与复查反查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import World

# 时空关联窗口：同一航段、180 秒内、0.3 公里以内且来自不同系统，
# 方判为同一目标；阈值过宽会把前后两艘船误并为一条任务。
CORRELATE_WINDOW = timedelta(seconds=180)
CORRELATE_DISTANCE_KM = 0.3


@dataclass(frozen=True)
class Alias:
    """异构系统中的船舶标识。"""

    system: str  # ais | radar | video
    value: str

    def __str__(self) -> str:
        return f"{self.system}:{self.value}"


@dataclass(frozen=True)
class MergeDecision:
    """一次归并决策，本身就是解释链上的一环。"""

    rule: str  # registry:mmsi | registry:plate | co-observation | same-report
    aliases: tuple[str, ...]
    witness_obs: tuple[str, ...]
    kp_gap_km: float | None
    dt_seconds: float | None
    reason: str
    at: datetime

    def explain(self) -> str:
        src = "、".join(self.aliases)
        where = ""
        if self.kp_gap_km is not None and self.dt_seconds is not None:
            where = f"（相距{self.kp_gap_km:.2f}公里、时差{self.dt_seconds:.0f}秒）"
        return f"{self.at:%H:%M:%S} 依据[{self.rule}]归并 {src}{where}：{self.reason}"


@dataclass
class _Cluster:
    cluster_id: str
    aliases: set[Alias] = field(default_factory=set)
    observations: list[str] = field(default_factory=list)

    @property
    def alias_systems(self) -> set[str]:
        return {a.system for a in self.aliases}


@dataclass
class _RecentObs:
    obs_id: str
    cluster_id: str
    segment_id: str
    kp: float
    ts: datetime
    alias: Alias


class IdentityConflict(Exception):
    """迟到数据试图把已人工确认的簇改判给另一艘船。"""


class IdentityLedger:
    """身份归并台账：簇、反查索引与全部决策记录。"""

    def __init__(self, world: World) -> None:
        self._world = world
        self._clusters: dict[str, _Cluster] = {}
        self._index: dict[Alias, str] = {}
        self._decisions: list[MergeDecision] = []
        self._recent: list[_RecentObs] = []
        self._history: list[_RecentObs] = []
        self._redirects: dict[str, str] = {}
        self._seq = 0

    def canonical(self, cluster_id: str) -> str:
        """簇被并入其他簇后，沿重定向链返回当前规范簇。"""
        seen = set()
        cur = cluster_id
        while cur in self._redirects and cur not in seen:
            seen.add(cur)
            cur = self._redirects[cur]
        return cur

    @property
    def decisions(self) -> list[MergeDecision]:
        return list(self._decisions)

    def cluster_id_of(self, alias: Alias) -> str | None:
        return self._index.get(alias)

    def aliases_of(self, cluster_id: str) -> set[Alias]:
        return set(self._clusters[self.canonical(cluster_id)].aliases)

    def observations_of(self, cluster_id: str) -> list[str]:
        return list(self._clusters[self.canonical(cluster_id)].observations)

    def explain(self, cluster_id: str) -> list[str]:
        """返回涉及该簇的全部归并解释，按时间排列。"""
        cluster_id = self.canonical(cluster_id)
        alias_strings = {str(a) for a in self._clusters[cluster_id].aliases}
        lines = [
            d.explain()
            for d in self._decisions
            if any(a in alias_strings for a in d.aliases)
        ]
        return lines

    def vessel_of(self, cluster_id: str) -> str | None:
        """依据登记资料给出簇的权威船舶；无登记命中返回 None（待明目标）。"""
        cluster_id = self.canonical(cluster_id)
        for alias in self._clusters[cluster_id].aliases:
            if alias.system == "ais":
                for v in self._world.vessels.values():
                    if v.mmsi == alias.value:
                        return v.vessel_id
            if alias.system == "video":
                for v in self._world.vessels.values():
                    if v.plate_no == alias.value:
                        return v.vessel_id
        return None

    def observe(
        self,
        obs_id: str,
        segment_id: str,
        kp: float | None,
        ts: datetime,
        aliases: list[Alias],
        backfill: bool = False,
    ) -> str | None:
        """登记一条带身份标识的观测，返回其所属簇（无标识返回 None）。

        backfill 表示补报/迟到数据：除近期窗口外，再按观测时间
        在历史观测中关联，保证迟到报文仍能并回原目标（仅身份归并，
        是否影响案件状态由上层屏障决定）。
        """
        if not aliases:
            return None
        self._prune(ts)

        # 1) 已被任一标识指向的既有簇（同条报文多标识、历史已归并）
        known: list[str] = []
        for alias in aliases:
            cid = self._index.get(alias)
            if cid is not None:
                cid = self.canonical(cid)
                if cid not in known:
                    known.append(cid)

        # 2) 时空关联到的既有簇（仅跨系统，取时空最接近的一个）
        match = None
        if kp is not None:
            match = self._best_correlation(
                aliases, segment_id, kp, ts, backfill
            )

        # 3) 选定存活簇：优先既有最老簇，保证簇号稳定不翻转
        survivor: str | None = known[0] if known else None
        join_match: tuple[str, float, float] | None = None
        join_rule = "backfill-co-observation" if backfill else "co-observation"

        if survivor is not None:
            for other in known[1:]:
                survivor = self._merge(
                    survivor, other, "same-report", (obs_id,), ts, None, None
                )
            if match and self.canonical(match[0]) != self.canonical(survivor):
                join_match = match
        elif match:
            survivor = self.canonical(match[0])
            join_match = match
        else:
            survivor = self._new_cluster()

        # 4) 把本次标识绑入存活簇（登记命中在此留痕）
        for alias in aliases:
            self._bind(survivor, alias, obs_id, ts)

        # 5) 绑定完成后记录跨系统时空关联（别名快照此时才完整）
        if join_match is not None:
            if self.canonical(join_match[0]) != self.canonical(survivor):
                # known 分支尚未结构合并的情形
                survivor = self._merge_with_match(
                    survivor, join_match, join_rule, obs_id, ts
                )
            else:
                self._record_join_decision(
                    survivor, join_match, join_rule, obs_id, ts
                )

        cluster = self._clusters[survivor]
        if obs_id not in cluster.observations:
            cluster.observations.append(obs_id)
        if kp is not None:
            for alias in aliases:
                item = _RecentObs(obs_id, survivor, segment_id, kp, ts, alias)
                self._recent.append(item)
                self._history.append(item)
        return survivor

    def _best_correlation(
        self,
        aliases: list[Alias],
        segment_id: str,
        kp: float,
        ts: datetime,
        backfill: bool,
    ) -> tuple[str, float, float] | None:
        """在（历史或近期）观测中找时空最接近、且系统不同的既有簇。

        返回 (簇号, 距离公里, 时差秒)。
        """
        incoming_systems = {a.system for a in aliases}
        pool = self._history if backfill else self._recent
        best: tuple[float, str, float, float] | None = None
        for item in pool:
            if item.segment_id != segment_id:
                continue
            if item.alias.system in incoming_systems:
                continue  # 同系统时空接近仍是两艘不同的船
            cid = self.canonical(item.cluster_id)
            cluster_systems = {a.system for a in self._clusters[cid].aliases}
            if cluster_systems & incoming_systems:
                continue  # 该簇已含同系统标识，不并
            dt = abs((ts - item.ts).total_seconds())
            gap = abs(kp - item.kp)
            if dt > CORRELATE_WINDOW.total_seconds() or gap > CORRELATE_DISTANCE_KM:
                continue
            score = gap + dt / CORRELATE_WINDOW.total_seconds() * CORRELATE_DISTANCE_KM
            if best is None or score < best[0]:
                best = (score, cid, gap, dt)
        return (best[1], best[2], best[3]) if best else None

    def _merge_with_match(
        self,
        survivor: str,
        match: tuple[str, float, float],
        rule: str,
        obs_id: str,
        ts: datetime,
    ) -> str:
        other, gap, dt = match
        witness_ids: set[str] = set()
        for item in self._history:
            if self.canonical(item.cluster_id) == self.canonical(other):
                witness_ids.add(item.obs_id)
        witness = tuple(sorted(witness_ids))[:2] or (obs_id,)
        return self._merge(
            survivor, other, rule, (obs_id, *witness), ts, gap, dt
        )

    def _record_join_decision(
        self,
        cluster_id: str,
        match: tuple[str, float, float],
        rule: str,
        obs_id: str,
        ts: datetime,
    ) -> None:
        """纯关联并入时补一条解释记录（簇本身已存在，不改动其结构）。"""
        other, gap, dt = match
        witness_ids = {
            item.obs_id
            for item in self._history
            if self.canonical(item.cluster_id) == self.canonical(other)
        }
        witness = tuple(sorted(witness_ids))[:2] or (obs_id,)
        self._decisions.append(
            MergeDecision(
                rule=rule,
                aliases=tuple(sorted(str(a) for a in self._clusters[cluster_id].aliases)),
                witness_obs=(obs_id, *witness),
                kp_gap_km=gap,
                dt_seconds=dt,
                reason=self._merge_reason(rule),
                at=ts,
            )
        )

    def _bind(
        self, cluster_id: str, alias: Alias, obs_id: str, ts: datetime
    ) -> None:
        existing = self._index.get(alias)
        if existing == cluster_id:
            return
        if existing is not None:
            self._merge(cluster_id, existing, "same-report", (obs_id,), ts, None, None)
            return
        self._index[alias] = cluster_id
        self._clusters[cluster_id].aliases.add(alias)
        rule, vessel = self._registry_lookup(alias)
        if rule:
            self._decisions.append(
                MergeDecision(
                    rule=rule,
                    aliases=(str(alias), vessel),
                    witness_obs=(obs_id,),
                    kp_gap_km=None,
                    dt_seconds=None,
                    reason="标识与船舶登记资料直接匹配",
                    at=ts,
                )
            )

    def _merge(
        self,
        a_id: str,
        b_id: str,
        rule: str,
        witness: tuple[str, ...],
        ts: datetime,
        gap: float | None,
        dt: float | None,
    ) -> str:
        if a_id == b_id:
            return a_id
        a, b = self._clusters[a_id], self._clusters[b_id]
        a.aliases |= b.aliases
        a.observations.extend(o for o in b.observations if o not in a.observations)
        for alias in b.aliases:
            self._index[alias] = a_id
        del self._clusters[b_id]
        self._redirects[b_id] = a_id
        for item in self._recent:
            if item.cluster_id == b_id:
                item.cluster_id = a_id
        self._decisions.append(
            MergeDecision(
                rule=rule,
                aliases=tuple(sorted(str(x) for x in a.aliases)),
                witness_obs=witness,
                kp_gap_km=gap,
                dt_seconds=dt,
                reason=self._merge_reason(rule),
                at=ts,
            )
        )
        return a_id

    def _merge_reason(self, rule: str) -> str:
        if rule == "co-observation":
            return "两系统上报目标在时空关联窗口内重合，判为同一船舶"
        if rule == "backfill-co-observation":
            return "补报报文按观测时间回溯，与原目标时空重合，并入既有身份簇（不影响已确认状态）"
        if rule == "same-report":
            return "多个标识在同一上报链路中共同出现"
        return "身份归并"

    def _registry_lookup(self, alias: Alias) -> tuple[str | None, str | None]:
        for vessel in self._world.vessels.values():
            if alias.system == "ais" and vessel.mmsi == alias.value:
                return "registry:mmsi", vessel.name
            if alias.system == "video" and vessel.plate_no == alias.value:
                return "registry:plate", vessel.name
        return None, None

    def _new_cluster(self) -> str:
        self._seq += 1
        cid = f"CL-{self._seq:03d}"
        self._clusters[cid] = _Cluster(cid)
        return cid

    def _prune(self, now: datetime) -> None:
        cutoff = now - CORRELATE_WINDOW
        self._recent = [r for r in self._recent if r.ts >= cutoff]
