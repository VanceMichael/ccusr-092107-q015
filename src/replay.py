"""剧本回放：按时间顺序喂入样例事件与处置指令并校验终态。

剧本是一份 JSON（见 ``fixtures/events.json``），步骤分两类：

* ``ingest``：感知事件（``event`` 字段）；同一断网批次可写
  ``ingest_batch``，回放时给批次内所有事件统一盖上到达时间戳，
  用来模拟"现场先处置、数据后补传"；
* 处置指令：``remote_verify / push / call / dispatch / boarding /
  recheck / revoke / decide_identity``。

风险用剧本内的 ``ref`` 引用（首个检测事件声明 ``ref``），回放器
解析为实际 ``RISK-xxxx`` 编号。``expect`` 段声明每条风险的终态、
结论以及必须满足的性质（如存在迟到依据、含被撤任务等），
:meth:`Replay.run` 返回逐条核对结果。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .engine import ClosureEngine
from .model import parse_ts


class Replay:
    def __init__(self, canal: dict[str, Any], script: dict[str, Any]) -> None:
        self.engine = ClosureEngine(canal)
        self.script = script
        self.log: list[dict[str, Any]] = []

    @classmethod
    def from_files(cls, canal_path: str | Path, script_path: str | Path) -> "Replay":
        canal = json.loads(Path(canal_path).read_text(encoding="utf-8"))
        script = json.loads(Path(script_path).read_text(encoding="utf-8"))
        return cls(canal, script)

    def _resolve_risk(self, ref: str) -> str:
        if ref in self.engine.risks:
            return ref
        if ref in self.engine.aliases:
            return self.engine.aliases[ref]
        raise KeyError(f"剧本引用了未定义的风险 ref：{ref}")

    def run(self) -> dict[str, Any]:
        for i, step in enumerate(self.script["steps"], 1):
            self._do_step(i, step)
        checks = self._check_expectations()
        return {
            "scenario": self.script.get("scenario", "未命名剧本"),
            "steps_run": len(self.log),
            "checks": checks,
            "passed": all(c["ok"] for c in checks),
            "board": self.engine.board(),
        }

    # ------------------------------------------------------------------ #

    def _do_step(self, index: int, step: dict[str, Any]) -> None:
        op = step["op"]
        at = step.get("at", "")
        try:
            if op == "ingest":
                result = self.engine.ingest(step["event"])
            elif op == "ingest_batch":
                result = self._ingest_batch(step)
            elif op == "remote_verify":
                result = self.engine.remote_verify(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    verdict=step.get("verdict", "confirmed"),
                    reason=step.get("reason", ""),
                )
            elif op == "push":
                result = self.engine.push_warning(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    target=step.get("target", "水上巡查组"),
                    channel=step.get("channel", "指挥平台"),
                )
            elif op == "call":
                result = self.engine.call_vessel(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    answered=step.get("answered"),
                    channel=step.get("channel", "VHF16"),
                    self_rectified=step.get("self_rectified", False),
                )
            elif op == "dispatch":
                result = self.engine.dispatch_boat(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    boat_id=step["boat"], person_ids=step["crew"],
                    duration_min=step.get("duration_min", 60),
                    override_identity=step.get("override_identity", False),
                    anchorage_id=step.get("anchorage"),
                    berth_id=step.get("berth"),
                )
            elif op == "boarding":
                result = self.engine.boarding_result(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    resolution=step["resolution"],
                    reason=step.get("reason", ""),
                    evidence_events=step.get("evidence"),
                )
            elif op == "recheck":
                result = self.engine.recheck_result(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    passed=step["passed"],
                    reason=step.get("reason", ""),
                    evidence_events=step.get("evidence"),
                )
            elif op == "revoke":
                result = self.engine.revoke(
                    self._resolve_risk(step["risk"]), step["actor"], at,
                    reason=step["reason"],
                )
            elif op == "decide_identity":
                result = self.engine.decide_identity(
                    step["a"], step["b"], step["actor"], at,
                    confirm=step["confirm"], reason=step.get("reason", ""),
                )
            else:
                raise ValueError(f"未知操作：{op}")
            self.log.append({"step": index, "op": op, "ok": True,
                             "message": result.message})
        except Exception as exc:  # noqa: BLE001 - 剧本失败要显式记录
            if step.get("expect_error"):
                self.log.append({"step": index, "op": op, "ok": True,
                                 "message": f"按预期被拒绝：{exc}"})
                return
            self.log.append({"step": index, "op": op, "ok": False,
                             "message": str(exc)})
            raise

    def _ingest_batch(self, step: dict[str, Any]) -> Any:
        """断网补传：observed_at 保持原值，recv 统一盖为批次到达时间。"""
        arrived = parse_ts(step["arrived_at"]) if step.get("arrived_at") \
            else parse_ts(step["at"])
        first = None
        for ev in step["events"]:
            ev = dict(ev)
            ev["recv"] = arrived.isoformat()
            ev.setdefault("batch", step.get("batch", f"BATCH-{arrived:%H%M}"))
            if isinstance(ev.get("risk_id"), str) and ev["risk_id"].startswith("ref:"):
                ev["risk_id"] = self._resolve_risk(ev["risk_id"][4:])
            first = self.engine.ingest(ev)
        return first

    # ------------------------------------------------------------------ #

    def _check_expectations(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for exp in self.script.get("expect", []):
            rid = self._resolve_risk(exp["risk"])
            risk = self.engine.risks[rid]
            problems: list[str] = []

            if "phase" in exp and risk.phase.value != exp["phase"]:
                problems.append(f"阶段={risk.phase.value}，期望 {exp['phase']}")
            if "resolution" in exp and risk.resolution.value != exp["resolution"]:
                problems.append(f"结论={risk.resolution.value}，期望 {exp['resolution']}")
            if exp.get("merged_into"):
                target = self._resolve_risk(exp["merged_into"])
                if risk.merged_into != target:
                    problems.append(f"未并入 {target}（实际 {risk.merged_into}）")
            if exp.get("has_delayed_evidence"):
                delayed = [self.engine.evidence[e] for e in risk.evidence_ids
                           if self.engine.evidence[e].delayed]
                if not delayed:
                    problems.append("缺少迟到补传依据")
            if exp.get("evidence_kinds"):
                kinds = {self.engine.evidence[e].kind.value
                         for e in risk.evidence_ids}
                missing = set(exp["evidence_kinds"]) - kinds
                if missing:
                    problems.append(f"依据类型缺失：{sorted(missing)}（现有 {sorted(kinds)}）")
            if exp.get("locked") is not None:
                if risk.locked != exp["locked"]:
                    problems.append(f"人工锁定={risk.locked}，期望 {exp['locked']}")
            if "task_cancelled" in exp:
                cancelled = {t.task_id for t in self.engine.dispatcher.tasks
                             if t.status == "cancelled"}
                wanted = exp["task_cancelled"]
                hit = wanted in cancelled or any(
                    (t.risk_id == rid or t.task_id == wanted) and t.status == "cancelled"
                    for t in self.engine.dispatcher.tasks
                )
                if not hit:
                    problems.append(f"期望任务 {wanted} 已撤销，实际没有")
            if exp.get("unclosed_count") is not None:
                n = len(self.engine.unclosed_risks())
                if n != exp["unclosed_count"]:
                    problems.append(f"未闭环风险数={n}，期望 {exp['unclosed_count']}")

            checks.append({
                "risk": exp["risk"],
                "risk_id": rid,
                "ok": not problems,
                "problems": problems,
            })
        return checks
