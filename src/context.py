"""读取并检查项目领域资料。

- :func:`load_context`：读取领域背景资料（domain/version/facts）；
- :func:`load_canal`：读取并校验航段、设备、登记库、艇组与锚地泊位
  等处置闭环所依赖的基础资料，校验规则与业务代码的引用保持一致。
"""

import json
from pathlib import Path
from typing import Any


def load_context(path: Path) -> dict:
    """返回字段完整的领域资料。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "facts", "sample_id"}
    if not required.issubset(data):
        raise ValueError("领域资料缺少必要字段")
    return data


def load_canal(path: Path) -> dict[str, Any]:
    """读取运河基础资料并做引用完整性校验。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_canal(data)


def validate_canal(data: dict[str, Any]) -> dict[str, Any]:
    """校验航段/设备/艇组/锚地泊位等基础资料的引用完整性。"""
    errors: list[str] = []

    segments = {s["id"] for s in data.get("segments", [])}
    if not segments:
        errors.append("至少需要一个航段")
    for d in data.get("devices", []):
        if d["segment_id"] not in segments:
            errors.append(f"设备 {d['id']} 引用了不存在的航段 {d['segment_id']}")

    persons = {p["id"] for p in data.get("persons", [])}
    for b in data.get("boats", []):
        if b["base_segment"] not in segments:
            errors.append(f"巡查艇 {b['id']} 的停泊航段 {b['base_segment']} 不存在")

    for a in data.get("anchorages", []):
        if a["segment_id"] not in segments:
            errors.append(f"锚地 {a['id']} 引用了不存在的航段 {a['segment_id']}")
        berth_ids = {b["id"] for b in a.get("berths", [])}
        if len(berth_ids) != len(a.get("berths", [])):
            errors.append(f"锚地 {a['id']} 的泊位编号重复")
        for occ in data.get("berth_occupancy", []):
            if occ["berth_id"] not in {x for x in berth_ids}:
                # 泊位可能属于其他锚地
                all_berths = {b["id"] for an in data.get("anchorages", [])
                              for b in an.get("berths", [])}
                if occ["berth_id"] not in all_berths:
                    errors.append(f"占用记录引用了不存在的泊位 {occ['berth_id']}")

    for p in data.get("persons", []):
        if p["role"] not in {"coxswain", "inspector", "assistant"}:
            errors.append(f"人员 {p['id']} 角色 {p['role']} 非法")

    if errors:
        raise ValueError("运河基础资料校验失败：" + "；".join(errors))
    return data
