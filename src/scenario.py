"""样例事件流运行器：把 fixtures/events.json 跑成处置闭环。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .case import Case
from .gateway import QueuedCommand
from .loader import load_world
from .service import ClosureService


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class ScenarioResult:
    scenario_id: str
    title: str
    cases: list[Case] = field(default_factory=list)
    case_refs: dict[int, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    service: Any | None = None
    flushed_commands: int = 0


class ScenarioRunner:
    def __init__(self, fixture_dir: Path) -> None:
        self.world = load_world(fixture_dir)
        events_doc = json.loads(
            (fixture_dir / "events.json").read_text(encoding="utf-8")
        )
        self.scenarios = events_doc["scenarios"]
        self.now: datetime | None = None

    def run_all(self) -> list[ScenarioResult]:
        return [self.run_one(raw) for raw in self.scenarios]

    def run_one(
        self,
        raw: dict[str, Any],
        stop_after=None,
    ) -> ScenarioResult:
        """运行单个场景。

        stop_after(event) 返回 True 时在该事件处理完后立即停止，
        供测试观察处置中途的值班看板。
        """
        service = ClosureService(self.world)
        result = ScenarioResult(scenario_id=raw["id"], title=raw["title"])
        risk_seq = 0

        def handle_command(queued: QueuedCommand) -> None:
            # 补传到达时间取恢复时刻（self.now），业务时间保留在命令里
            self._apply_command(service, queued, result, arrival=self.now)

        ordered = sorted(
            enumerate(raw["events"]),
            key=lambda pair: self._sort_key(pair[1]),
        )
        for _, event in ordered:
            kind = event["t"]
            if kind == "obs":
                arrival = _ts(event["arrival"])
                self.now = arrival
                outcome = service.ingest(event["obs"], arrival)
                if outcome.case_created:
                    result.case_refs[risk_seq] = outcome.case_id
                    risk_seq += 1
            elif kind == "net_down":
                self.now = _ts(event["at"])
                service.gateway.set_offline(True)
                result.notes.append(f"{self.now:%H:%M} 现场网络中断，登临结果进入本地队列")
            elif kind == "net_up":
                self.now = _ts(event["at"])
                service.gateway.set_offline(False)
                flushed = service.gateway.flush(handle_command)
                result.flushed_commands = flushed
                result.notes.append(
                    f"{self.now:%H:%M} 网络恢复，补传{flushed}条现场命令"
                )
            else:
                self.now = _ts(event["at"])
                case = service.store.get(result.case_refs[event["ref"]])
                self._human_command(service, kind, event, case, handle_command, result)

            if stop_after is not None and stop_after(event):
                break

        result.cases = [service.store.get(cid) for cid in result.case_refs.values()]
        result.service = service
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _sort_key(event: dict[str, Any]) -> str:
        """处理顺序以到达平台时间为准：观测看 arrival，其余看 at。"""
        return event["arrival"] if event["t"] == "obs" else event["at"]

    def _human_command(
        self,
        service: ClosureService,
        kind: str,
        event: dict[str, Any],
        case: Case,
        handle_command,
        result: ScenarioResult,
    ) -> None:
        at = _ts(event["at"])
        if kind == "confirm":
            service.confirm(case, at, event["actor"], event.get("note", ""))
        elif kind == "push":
            service.push(case, at, event.get("actor"))
        elif kind == "call":
            service.call(
                case, at, event["instruction"],
                actor=event.get("actor"), ack=event.get("ack"),
            )
        elif kind == "dispatch":
            service.dispatch(case, at, event["berth_id"])
        elif kind == "revoke":
            service.revoke(case, at, event["actor"], event["reason"])
        elif kind == "recheck":
            service.recheck(case, at, event["actor"], event["compliant"],
                            event.get("note", ""))
        elif kind == "close":
            service.close(case, at, event.get("actor"))
        elif kind == "board":
            queued = QueuedCommand(
                handler="board",
                case_id=case.case_id,
                at=at,
                actor=event["actor"],
                payload={
                    "result": event["result"],
                    "findings": event.get("findings", ""),
                    "corrective": event.get("corrective", ""),
                    "offline_queued": event.get("offline_queued", False),
                },
                client_id=event.get("client_id"),
            )
            status = service.gateway.submit_command(handle_command, queued)
            if status == "queued":
                result.notes.append(
                    f"{at:%H:%M} 登临完成但网络中断，已在现场端暂存（{event['client_id']}）"
                )
            elif status == "duplicate":
                result.notes.append(
                    f"{self.now:%H:%M} 收到重复补传（{event['client_id']}），幂等去重"
                )

    def _apply_command(
        self,
        service: ClosureService,
        queued: QueuedCommand,
        result: ScenarioResult,
        arrival: datetime | None,
    ) -> None:
        if queued.handler != "board":
            raise ValueError(f"未知现场命令 {queued.handler}")
        case = service.store.get(queued.case_id)
        service.board(
            case,
            ts=queued.at,
            actor=queued.actor,
            result=queued.payload["result"],
            findings=queued.payload["findings"],
            corrective=queued.payload["corrective"],
            arrival=arrival or queued.at,
            client_id=queued.client_id,
            offline_queued=queued.payload.get("offline_queued", False),
        )
