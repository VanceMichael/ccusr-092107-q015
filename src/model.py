"""平陆运河双线巡航处置闭环——领域模型与状态机。

核心聚合是 :class:`Risk`（风险/警情），它贯穿七个处置阶段：

    智慧感知发现 -> 远程核验 -> 预警推送 -> 船舶叫应
        -> 巡查艇任务(派艇) -> 登临检查 -> 复查 -> 闭环

误报可在登临前由值班员撤销；登临后若现场判定不属实，则以
"未发现问题/误报"结论直接闭环，不再使用撤销，保证现场结论可追溯。

状态机两条铁律：

1. **只进不退**：阶段只能沿正向顺序推进，任何补报数据都不能把
   风险拉回早期阶段；
2. **人工确认锁定**：一旦经过人工确认（远程核验结论、登临结果、
   人工身份判定等），系统/传感器的迟到数据只能追加为依据，不能
   改写已确认的阶段或结论。人工的新决定可以覆盖人工的旧决定，
   但必须记录决定人与原因。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_ts(value: str | datetime) -> datetime:
    """把 ISO8601 字符串统一为带时区的时间；无时区按 UTC 处理。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class DomainError(ValueError):
    """业务规则冲突（区别于编程错误）。"""


class RiskType(str, enum.Enum):
    ANCHORAGE = "anchorage_violation"   # 违规锚泊/占用禁锚区、泊位
    LOW_UKC = "low_ukc"                 # 水位富余水深不足
    NAV_RULE = "nav_rule_violation"     # 航行规则异常（偏航、追越等）
    ABNORMAL = "vessel_abnormal"        # 船舶异常（失控、失联等）


class Phase(str, enum.Enum):
    DETECTED = "detected"                 # 智慧感知发现
    REMOTE_VERIFIED = "remote_verified"   # 电子巡查组远程核验
    PUSHED = "pushed"                     # 预警推送至值班员/水上巡查组
    CALLED = "called"                     # 船舶叫应（VHF/电话）
    DISPATCHED = "dispatched"             # 巡查艇任务下达
    BOARDED = "boarded"                   # 登临检查完成
    RECHECKED = "rechecked"               # 复查完成
    CLOSED = "closed"                     # 闭环
    REVOKED = "revoked"                   # 误报撤销（登临前）


class Resolution(str, enum.Enum):
    NONE = "none"                     # 尚未结论
    CONFIRMED = "confirmed"           # 确认问题，整改/处罚
    NO_VIOLATION = "no_violation"     # 现场未发现问题
    FALSE_ALARM = "false_alarm"       # 误报（传感器/算法错误）
    RECTIFIED = "rectified"           # 已自行改正，放行


# 阶段正向顺序（终态不参与比较）
ORDER: list[Phase] = [
    Phase.DETECTED,
    Phase.REMOTE_VERIFIED,
    Phase.PUSHED,
    Phase.CALLED,
    Phase.DISPATCHED,
    Phase.BOARDED,
    Phase.RECHECKED,
    Phase.CLOSED,
]

TERMINAL = {Phase.CLOSED, Phase.REVOKED}

# 允许的正向迁移。登临前各阶段均可由人工撤销；登临后只能走向闭环。
FORWARD: dict[Phase, set[Phase]] = {
    Phase.DETECTED: {Phase.REMOTE_VERIFIED, Phase.REVOKED},
    Phase.REMOTE_VERIFIED: {Phase.PUSHED, Phase.REVOKED},
    Phase.PUSHED: {Phase.CALLED, Phase.DISPATCHED, Phase.REVOKED},
    Phase.CALLED: {Phase.DISPATCHED, Phase.REVOKED, Phase.CLOSED},
    Phase.DISPATCHED: {Phase.BOARDED, Phase.REVOKED},
    Phase.BOARDED: {Phase.RECHECKED, Phase.CLOSED},
    Phase.RECHECKED: {Phase.RECHECKED, Phase.CLOSED},
    Phase.CLOSED: set(),
    Phase.REVOKED: set(),
}

# 登临后即可直接闭环的结论（无需复查）
CLOSE_WITHOUT_RECHECK = {Resolution.NO_VIOLATION, Resolution.FALSE_ALARM}

# 判定为"人工动作"的动作类型——执行后锁定当前状态
MANUAL_ACTIONS = {
    "remote_verify",
    "vessel_call",
    "boarding_result",
    "recheck_result",
    "revoke",
    "manual_close",
    "identity_decision",
}


class EvidenceKind(str, enum.Enum):
    RADAR = "radar"
    VIDEO = "video"
    AIS = "ais"
    HYDRO = "hydro"        # 水文（水位、流速）
    WEATHER = "weather"    # 气象（风、能见度）
    VHF = "vhf"
    ONSITE = "onsite"      # 现场回传（照片、笔录）
    MANUAL = "manual"      # 人工操作记录


@dataclass(frozen=True)
class Evidence:
    """一条不可篡改的原始依据。

    ``observed_at`` 是数据实际发生时间（传感器采样时间），
    ``received_at`` 是平台收到时间——断网补传时两者可能相差很大，
    是否迟到以 observed_at 与当前阶段进入时间比较判定。
    """

    evidence_id: str
    kind: EvidenceKind
    device_id: str | None
    observed_at: datetime
    received_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    delayed: bool = False          # 入库时已判定为迟到补报
    batch_id: str | None = None    # 离线补传批次

    def to_line(self) -> str:
        tag = "（迟到补传）" if self.delayed else ""
        device = self.device_id or "人工"
        return f"[{self.observed_at:%Y-%m-%d %H:%M}]{tag}{self.kind.value}@{device}"


@dataclass
class HistoryEntry:
    """风险时间线上的一个动作，可双向追溯。"""

    seq: int
    phase_after: Phase
    action: str
    actor: str
    occurred_at: datetime
    received_at: datetime
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    locked: bool = False
    delayed: bool = False


@dataclass
class Risk:
    """一条警情的完整处置记录。"""

    risk_id: str
    risk_type: RiskType
    cluster_key: str               # 归并后的船舶身份簇
    segment_id: str
    chainage_km: float
    first_seen: datetime
    last_seen: datetime
    phase: Phase = Phase.DETECTED
    entered_at: datetime | None = None
    resolution: Resolution = Resolution.NONE

    # 人工确认锁定：locked=True 后，任何自动/迟到数据不得改变状态
    locked: bool = False
    lock_actor: str | None = None
    lock_action: str | None = None

    severity: str = "medium"       # low / medium / high
    identity_pending: bool = False  # 多源身份仍有歧义，等待人工判定
    title: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    evidence_ids: list[str] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    related_risk_ids: list[str] = field(default_factory=list)
    merged_into: str | None = None     # 去重合并时指向保留的风险
    board_task_id: str | None = None
    recheck_task_id: str | None = None

    def __post_init__(self) -> None:
        if self.entered_at is None:
            self.entered_at = self.first_seen

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL

    @property
    def unclosed(self) -> bool:
        """值班员"未闭环风险"视图的判定口径。"""
        return self.merged_into is None and self.phase not in TERMINAL

    def phase_path(self) -> list[Phase]:
        return [Phase.DETECTED] + [h.phase_after for h in self.history]


def can_transit(current: Phase, target: Phase) -> bool:
    return target in FORWARD.get(current, set())


def is_backward(current: Phase, target: Phase) -> bool:
    """目标阶段在正向顺序上早于当前阶段（含撤销已登临状态等情形）。"""
    if target not in ORDER or current not in ORDER:
        return False
    return ORDER.index(target) < ORDER.index(current)
