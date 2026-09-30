"""可解释的多源船舶身份归并。

不同系统看到的是同一艘船的不同侧面：

- 雷达只给批号（如 ``RADAR-03#T1087``）；
- 视频 AI 给牌证/船名 OCR（可能误识）；
- AIS 给 MMSI；
- 登记库把 MMSI 与船名、船舶登记号绑定；
- 叫应记录来自 VHF 自述船名。

归并采用"规则打分 + 并查集"：

* 每条候选关联给出**规则证据**（登记绑定 / 时空邻近 / 牌证相似 /
  航迹连续）与分值，全部保留在解释链里，可逐条人工复核；
* 强证据（登记绑定，或高分且无歧义）自动归并；
* 证据不足或两个候选互相冲突时，身份标记为 *待人工判定*，
  不猜不并；
* 人工确认/否认立即锁定，**任何迟到数据不能推翻**，
  但会追加到解释链中供审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import timedelta
from typing import Iterable

from .model import parse_ts


# ---- 可调阈值（集中在一处，便于复盘调参）---------------------------------

TIME_WINDOW = timedelta(minutes=3)      # 时空邻近的时间窗
DISTANCE_KM = 0.6                       # 航段内里程邻近阈值（公里）
NAME_MATCH = 0.6                        # 船名 OCR 相似度阈值
AUTO_MERGE_SCORE = 60.0                 # 自动归并分数线
STRONG_SCORE = 100.0                    # 登记库绑定直接满分


@dataclass
class SightToken:
    """一次观测中出现的船舶标识及其时空上下文。"""

    token: str
    system: str                 # radar / video / ais / vhf / registry
    observed_at: str
    segment_id: str
    chainage_km: float
    name_hint: str | None = None
    mmsi_hint: str | None = None


@dataclass
class MergeLink:
    """两个标识之间的一条归并依据（解释链的最小单元）。"""

    a: str
    b: str
    rule: str
    score: float
    detail: str
    decided_by: str = "system"          # system / 人工账号
    decided_at: str | None = None
    rejected: bool = False              # 人工否认：保留记录但不连接
    observed_at: str = ""               # 候选产生时间（挂起排序用）

    def render(self) -> str:
        verdict = "（人工已否认）" if self.rejected else ""
        who = "人工" if self.decided_by != "system" else "系统"
        return f"{self.a} ≈ {self.b}：{self.rule} {self.score:.0f}分{verdict}[{who}] —— {self.detail}"


class IdentityResolver:
    """并查集 + 规则解释链。

    用法::

        resolver.observe(tokens)          # 喂入一批同屏观测
        resolver.bind_registry(...)       # 登记库权威绑定
        key = resolver.cluster_key(token) # 取归并后的稳定船舶键
        resolver.explain(key)             # 查看完整依据链
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self.links: list[MergeLink] = []
        self._manual_locks: set[frozenset[str]] = set()   # 人工确认对
        self._manual_rejects: set[frozenset[str]] = set()  # 人工否任对
        self._tokens: dict[str, list[SightToken]] = {}
        self._registry: dict[str, dict[str, str]] = {}
        self.pending: list[tuple[str, str, list[MergeLink]]] = []  # 冲突挂起

    # ---- 并查集 ----------------------------------------------------------

    def _find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def _union(self, a: str, b: str) -> None:
        self._parent.setdefault(a, a)
        self._parent.setdefault(b, b)
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            # 让登记船名/MMSI 倾向成为根，簇键更稳定
            if ra.startswith(("RADAR-", "VID-", "VHF:")) and not rb.startswith(
                    ("RADAR-", "VID-", "VHF:")):
                ra, rb = rb, ra
            self._parent[rb] = ra

    def _members(self, root: str) -> set[str]:
        return {x for x in self._parent if self._find(x) == root}

    # ---- 数据接入 --------------------------------------------------------

    def observe(self, tokens: Iterable[SightToken]) -> list[MergeLink]:
        """登记一批观测，并在新老标识之间尝试自动归并。"""
        accepted: list[MergeLink] = []
        tokens = list(tokens)
        for tok in tokens:
            self._parent.setdefault(tok.token, tok.token)
            self._tokens.setdefault(tok.token, []).append(tok)

        for tok in tokens:
            candidates = self._candidate_links(tok)
            chosen = self._choose_links(tok, candidates)
            for link, apply in chosen:
                if apply and self._apply_link(link):
                    accepted.append(link)
        return accepted

    def _candidate_links(self, tok: SightToken) -> list[MergeLink]:
        links: list[MergeLink] = []
        links += self._match_registry(tok)
        links += self._match_spatiotemporal(tok)
        links += self._match_name(tok)
        # 同一对标识取最高分候选
        best: dict[frozenset[str], MergeLink] = {}
        for link in links:
            key = frozenset((link.a, link.b))
            if key not in best or link.score > best[key].score:
                best[key] = link
        return list(best.values())

    def _choose_links(self, tok: SightToken, candidates: list[MergeLink]
                      ) -> list[tuple[MergeLink, bool]]:
        """决定候选是直接应用还是挂起。

        只有"时空邻近"这类弱语义证据会产生歧义：当一个自身不带
        牌证/MMSI 的批号（典型：雷达点）在窗内邻近两艘不同登记
        船舶时，全部候选挂起、批号保持孤立。牌证识别→登记库的
        匹配语义明确，不在此列（它引发的跨簇冲突会在
        :meth:`_apply_link` 中挂起）。
        """
        st_strong = [l for l in candidates
                     if l.rule == "时空邻近" and AUTO_MERGE_SCORE <= l.score < STRONG_SCORE]
        registry_hits = {self._mmsi_via_link(l, tok.token) for l in st_strong}
        registry_hits.discard(None)
        ambiguous = (
            len(registry_hits) >= 2
            and not tok.name_hint and not tok.mmsi_hint
        )

        # 语义强的规则优先应用（牌证识别 80 先于时空邻近 ~60），
        # 避免匿名批号借弱证据先并入错误的簇
        candidates.sort(key=lambda l: l.score, reverse=True)
        result: list[tuple[MergeLink, bool]] = []
        for link in candidates:
            if ambiguous:
                self.links.append(link)
                self._pend(link)
                result.append((link, False))
            else:
                result.append((link, True))
        return result

    def _root_has_pending(self, root: str) -> bool:
        members = self._members(root)
        return any(bool(members & set(key.split("|")))
                   for key, _, _ in self.pending)

    def _mmsi_via_link(self, link: MergeLink, token: str) -> str | None:
        other = link.b if link.a == token else (link.a if link.b == token else None)
        if other is None:
            return None
        if other.startswith("MMSI:"):
            return other.removeprefix("MMSI:")
        for mmsi, rec in self._registry.items():
            if other in (rec["name"], rec.get("reg_no")):
                return mmsi
        return None

    def bind_registry(self, mmsi: str, *, name: str, reg_no: str | None = None) -> None:
        """录入登记库权威绑定；若相关簇已被人工锁定则只追加依据，不改动。"""
        self._registry[mmsi] = {"name": name, "reg_no": reg_no or ""}
        for ident in (name, reg_no):
            if not ident:
                continue
            link = MergeLink(
                f"MMSI:{mmsi}", ident, "登记库绑定", STRONG_SCORE,
                f"船舶登记库记载 MMSI {mmsi} 对应「{name}」",
            )
            self._apply_link(link)

    # ---- 规则实现 --------------------------------------------------------

    def _match_registry(self, tok: SightToken) -> list[MergeLink]:
        links: list[MergeLink] = []
        mmsi = tok.mmsi_hint or (tok.token if tok.token.startswith("MMSI:") else None)
        if mmsi and mmsi.removeprefix("MMSI:") in self._registry:
            rec = self._registry[mmsi.removeprefix("MMSI:")]
            for ident in (rec["name"], rec.get("reg_no")):
                if ident:
                    links.append(MergeLink(
                        tok.token, ident, "登记库绑定", STRONG_SCORE,
                        f"{tok.system} 观测携带 MMSI {mmsi}，登记库对应「{rec['name']}」",
                    ))
        if tok.name_hint:
            for mmsi_key, rec in self._registry.items():
                if _name_similar(tok.name_hint, rec["name"]) >= NAME_MATCH:
                    links.append(MergeLink(
                        tok.token, f"MMSI:{mmsi_key}", "牌证→登记库匹配", 80.0,
                        f"识别名「{tok.name_hint}」与登记名「{rec['name']}」相似度"
                        f"{_name_similar(tok.name_hint, rec['name']):.2f}",
                    ))
        return links

    def _match_spatiotemporal(self, tok: SightToken) -> list[MergeLink]:
        links: list[MergeLink] = []
        t = parse_ts(tok.observed_at)
        # 雷达批号与 AIS/VHF 等异源标识互补，才做时空配对
        complementary = {"radar", "video", "ais", "vhf"}
        for other_token, others in self._tokens.items():
            if other_token == tok.token:
                continue
            for o in others:
                if o.system == tok.system or o.segment_id != tok.segment_id:
                    continue
                if {tok.system, o.system} - complementary:
                    continue
                dt = abs((parse_ts(o.observed_at) - t).total_seconds())
                dd = abs(o.chainage_km - tok.chainage_km)
                if dt <= TIME_WINDOW.total_seconds() and dd <= DISTANCE_KM:
                    score = 65.0 - dt / 60.0 * 2 - dd * 10
                    links.append(MergeLink(
                        tok.token, other_token, "时空邻近",
                        max(score, 20.0),
                        f"{tok.system}/{o.system} 相隔{dt:.0f}秒、{dd:.2f}公里"
                        f"（阈值{TIME_WINDOW.seconds//60}分钟/{DISTANCE_KM}公里）",
                    ))
        return links

    def _match_name(self, tok: SightToken) -> list[MergeLink]:
        if not tok.name_hint:
            return []
        links: list[MergeLink] = []
        for other_token, others in self._tokens.items():
            if other_token == tok.token:
                continue
            for o in others:
                if not o.name_hint:
                    continue
                ratio = _name_similar(tok.name_hint, o.name_hint)
                if ratio >= NAME_MATCH:
                    links.append(MergeLink(
                        tok.token, other_token, "船名相似",
                        50.0 + ratio * 30.0,
                        f"「{tok.name_hint}」≈「{o.name_hint}」相似度{ratio:.2f}",
                    ))
        return links

    # ---- 决策：应用/挂起/锁定 -------------------------------------------

    def _apply_link(self, link: MergeLink) -> bool:
        """对一条候选关联做决策；返回是否真正连通。"""
        root_a, root_b = self._find(link.a), self._find(link.b)
        # 人工否认按簇生效：两簇之间曾被人工否认，任何迟到证据都不得连通
        if root_a != root_b and self._cluster_rejected(root_a, root_b):
            mark = MergeLink(link.a, link.b, link.rule + "（被人工否认拦截）",
                             link.score, link.detail,
                             decided_by="system:blocked_by_manual_deny")
            self.links.append(mark)
            return False
        # 已连通：只追加依据
        if root_a == root_b:
            self.links.append(link)
            return True

        # 启发式证据（非登记库绑定）不得自动合并"两个不同登记船舶"的簇——
        # 一个雷达批号同时像 X 轮又像 Y 轮，必须挂起由人工判定
        if link.score < STRONG_SCORE:
            vessels_a = self._registry_vessels(root_a)
            vessels_b = self._registry_vessels(root_b)
            if vessels_a and vessels_b and vessels_a.isdisjoint(vessels_b):
                self.links.append(link)
                self._pend(link)
                return False
            # 不得把"自身身份未决"的匿名批号（雷达/视频批号）经
            # 启发式证据拉进登记船舶簇。按链接方向检查：只有本链接
            # 的匿名侧自己挂着待判项才拦截（牌证精确匹配的新批号
            # 不受同簇另一个批号的歧义牵连）
            anon_root = root_a if not vessels_a else (root_b if not vessels_b else None)
            if anon_root is not None and self._root_has_pending(anon_root):
                self.links.append(link)
                self._pend(link)
                return False

        if link.score >= AUTO_MERGE_SCORE:
            self._union(link.a, link.b)
            self.links.append(link)
            return True

        # 弱证据：保留为线索，不自动并
        self.links.append(link)
        return False

    def _registry_vessels(self, root: str) -> set[str]:
        """簇成员能对应到登记库的船舶 MMSI 集合。"""
        members = self._members(root)
        vessels: set[str] = set()
        for mmsi, rec in self._registry.items():
            if f"MMSI:{mmsi}" in members or rec["name"] in members \
                    or rec.get("reg_no") in members:
                vessels.add(mmsi)
        return vessels

    def _cluster_rejected(self, root_a: str, root_b: str) -> bool:
        ma, mb = self._members(root_a), self._members(root_b)
        return any(frozenset((x, y)) in self._manual_rejects
                   for x in ma for y in mb)

    def _pend(self, link: MergeLink) -> None:
        key = "|".join(sorted({link.a, link.b}))
        # 同一对簇只挂一次，后续候选追加进同一条待判记录
        for k, _, candidates in self.pending:
            kset = set(k.split("|"))
            if (link.a in kset or link.b in kset) and \
                    self._find(link.a) != self._find(link.b):
                candidates.append(link)
                return
        self.pending.append((key, link.observed_at, [link]))

    # ---- 人工判定 --------------------------------------------------------

    def confirm(self, a: str, b: str, actor: str, at: str, reason: str = "") -> None:
        """人工确认两个标识同船：立即并簇、锁定并清除相关待判。"""
        pair = frozenset((a, b))
        self._manual_locks.add(pair)
        self._manual_rejects.discard(pair)
        self._union(a, b)
        self.links.append(MergeLink(a, b, "人工确认", 999.0, reason or "值班员判定同船",
                                    decided_by=actor, decided_at=at))
        self._prune_pending(a, b, confirmed=True)

    def deny(self, a: str, b: str, actor: str, at: str, reason: str = "") -> None:
        """人工否认同船：永久拦截两个标识（及其所在簇）间的任何自动归并。"""
        pair = frozenset((a, b))
        self._manual_rejects.add(pair)
        self.links.append(MergeLink(a, b, "人工否认", -999.0, reason or "值班员判定非同船",
                                    decided_by=actor, decided_at=at, rejected=True))
        self._prune_pending(a, b, confirmed=False)

    def _prune_pending(self, a: str, b: str, *, confirmed: bool) -> None:
        decided_root = self._find(a)
        kept: list[tuple[str, str, list[MergeLink]]] = []
        for key, at, candidates in self.pending:
            roots = {self._find(t) for t in key.split("|") if t in self._parent}
            if len(roots) == 1:
                continue  # 候选项已同簇，歧义随人工确认消除
            remaining = []
            for link in candidates:
                if frozenset((link.a, link.b)) == frozenset((a, b)):
                    continue  # 被本次人工判定直接处理
                if confirmed and (
                        self._find(link.a) == decided_root
                        or self._find(link.b) == decided_root):
                    # 人工已把目标定为本簇，指向其他登记船舶的悬置候选视为排除
                    other = link.b if self._find(link.a) == decided_root else link.a
                    self.links.append(MergeLink(
                        link.a, link.b, link.rule + "（人工确认他船后排除）",
                        -998.0, link.detail, decided_by="system:after_manual_confirm",
                        rejected=True))
                    continue
                remaining.append(link)
            if remaining:
                kept.append((key, at, remaining))
        self.pending = kept

    # ---- 查询与解释 ------------------------------------------------------

    def cluster_key(self, token: str) -> str:
        """返回稳定簇键：优先登记船名，其次 MMSI，最后取最小标识。"""
        if token not in self._parent:
            self._parent[token] = token
        root = self._find(token)
        members = self._members(root)
        registry_names = {rec["name"] for rec in self._registry.values()}
        named = sorted(members & registry_names)
        if named:
            return named[0]
        mmsi = sorted(m for m in members if m.startswith("MMSI:"))
        if mmsi:
            return mmsi[0]
        return sorted(members)[0]

    def has_pending(self, token: str) -> bool:
        if token not in self._parent:
            return False
        root = self._find(token)
        members = self._members(root)
        for key, _, _ in self.pending:
            if members & set(key.split("|")):
                return True
        return False

    def explain(self, key: str) -> list[str]:
        """打印一个身份簇的完整归并依据链（含被拒/迟到来源）。"""
        root = self._find(key)
        members = self._members(root)
        lines = [f"船舶身份簇「{self.cluster_key(key)}」，成员：{sorted(members)}"]
        for link in self.links:
            if {link.a, link.b} & members:
                lines.append("  - " + link.render())
        if self.has_pending(key):
            lines.append("  ! 存在待人工判定的歧义关联")
        return lines

    def pending_items(self) -> list[dict[str, str | list[str]]]:
        return [
            {
                "key": key,
                "candidates": [l.render() for l in links],
            }
            for key, _, links in self.pending
        ]


def _name_similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()
