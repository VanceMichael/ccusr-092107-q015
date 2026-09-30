"""命令行：打印四个样例场景的处置闭环、身份解释与溯源链。

用法：python -m src.report
"""

from __future__ import annotations

from pathlib import Path

from .scenario import ScenarioRunner


def main() -> None:
    runner = ScenarioRunner(Path("fixtures"))
    results = runner.run_all()
    for result in results:
        svc = result.service
        print("=" * 72)
        print(f"{result.scenario_id}  {result.title}")
        print("-" * 72)
        for case in result.cases:
            vessel = svc._vessel_name(case) or "不明目标"
            if case.status == "REVOKED" and case.late_evidence:
                vessel += "（注：船舶为撤销后迟到报文归并，未翻案）"
            print(f"案件 {case.case_id}  [{case.status}]  {vessel}")
            print(f"  航段：{svc.world.segments[case.segment_id].name}")
            print(f"  风险规则：{'、'.join(case.rule_ids)}")

            explanations = svc.ledger.explain(case.cluster_id)
            if explanations:
                print("  身份归并依据：")
                for line in explanations:
                    print(f"    - {line}")

            if case.dispatch:
                d = case.dispatch
                substitute = "（同锚地改派）" if d["berth_substituted"] else ""
                cross = "（跨航段支援）" if d["cross_segment"] else ""
                print(
                    f"  巡查任务：{d['boat_name']} 登临组{','.join(d['officer_ids'])}"
                    f" → 锚地{d['anchorage_id']} 泊位{d['berth_id']}{substitute}{cross}"
                )
            if case.call:
                print(f"  船舶叫应：{case.call['instruction']}（应答：{case.call['ack']}）")
            if case.boarding:
                late = "［断网补传］" if case.boarding.get("offline_queued") else ""
                print(
                    f"  登临检查：{case.boarding['result']} {late}"
                    f"——{case.boarding['findings']}"
                )
            if case.recheck:
                print(f"  复查：{'合格' if case.recheck['compliant'] else '不合格'}")
            if case.revocation:
                print(f"  误报撤销：{case.revocation['reason']}（已发解除通知）")

            if case.late_evidence:
                print(f"  迟到/补报证据（不改变状态）：{len(case.late_evidence)} 条")
            if case.quarantined:
                print("  终态后隔离复核：")
                for q in case.quarantined:
                    print(f"    - {q.obs_id}：{q.detail}")

            lineage = svc.lineage(case)
            chain = " → ".join(
                f"{b['device_name']}@{b['obs_ts']:%H:%M:%S}"
                + ("(补)" if b["late"] else "")
                for b in lineage["basis"]
            )
            print(f"  溯源链：{chain}")

        if result.notes:
            print("  过程：")
            for note in result.notes:
                print(f"    · {note}")
        print()

    pending = [
        item
        for result in results
        for item in result.service.review_queue()
    ]
    if pending:
        print("=" * 72)
        print("终态后隔离、待人工复核队列（状态不倒退，数据不丢失）")
        print("-" * 72)
        for item in pending:
            print(f"  [{item['case_id']} {item['case_status']}] {item['detail']}")


if __name__ == "__main__":
    main()
